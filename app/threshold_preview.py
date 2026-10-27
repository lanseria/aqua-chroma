# app/threshold_preview.py
"""
HSV 阈值实时预览（仅分析结果源）。

复用生产链路的分析产物与 processor 的判定/着色/指标逻辑，只对「已准备好的
单张图像」做 HSV 分类与渲染，不写任何文件，供 /tools/color_thresholds 页面
拖动阈值时实时查看效果。

图像源
------
仅支持 analysis:<timestamp>：取 data/output/<ts>/03_ocean_only.png
（生产链路已完成超分 + CLAHE + 海洋蒙版），海洋蒙版按该尺寸用
geo_utils.create_ocean_mask 重新生成（确定性结果，与生产完全一致）。

任意历史时间点都可作为预览源；每个时间点可在页面上打 tag（如 晴朗天空/多云/薄雾，
持久化在数据库 preview_tags 表），把真实分析结果当作"分类型测试样本"并按 tag 筛选。
本模块只负责读取 tag 用于展示与筛选，写入见 tools.py 的 /tools/api/preview_tags。

缓存
----
- 预处理结果（RGB / HSV / 蒙版 / 源图预览 data URL）按 source 缓存，LRU 最多 4 个；
- 海洋蒙版按 (宽, 高) 缓存，多个同尺寸分析结果源共用。
"""

import base64
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import cv2
import numpy as np
from PIL import Image

from . import config, crud, geo_utils, processor, settings_store
from .database import SessionLocal

# 分析结果预览源目录（与 main.run_analysis_and_persist 的输出目录一致）
OUTPUT_BASE_DIR = Path(config.OUTPUT_BASE_DIR)
# 分析结果目录中用于预览的产物：已完成超分 + CLAHE + 海洋蒙版
ANALYSIS_ARTIFACT = "03_ocean_only.png"
# 预处理缓存容量（LRU）
MAX_PREPARED_CACHE = 4
# 蒙版缓存容量
MAX_MASK_CACHE = 8
# 传输前图片最大边长
MAX_PREVIEW_SIDE = 1000
# PNG 编码压缩级别：1 = 速度优先（预览是交互操作，体积略大无妨）
_PNG_COMPRESSION = 1

# 预览源 id 只接受 analysis:<数字时间戳>
_ANALYSIS_SOURCE_PATTERN = re.compile(r"^analysis:(\d+)$")

_TZ = ZoneInfo(config.TIME_ZONE)

# 缓存与锁
_cache_lock = threading.RLock()
_prepared_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_mask_cache: "OrderedDict[Tuple[int, int], np.ndarray]" = OrderedDict()


# --- 分析结果预览源 ---

def analysis_artifact_path(timestamp: int) -> Path:
    """返回某个时间点的预览图路径：data/output/<timestamp>/03_ocean_only.png。"""
    return OUTPUT_BASE_DIR / str(int(timestamp)) / ANALYSIS_ARTIFACT


def analysis_source_exists(timestamp: int) -> bool:
    """判断某个时间点是否有可用的分析结果预览图。"""
    return analysis_artifact_path(timestamp).is_file()


def _scan_analysis_timestamps() -> List[int]:
    """
    扫描 data/output 下所有可用的分析结果时间点（按时间倒序）。

    只认「目录名为纯数字」且存在 03_ocean_only.png 的目录；
    debug_* 等非数字目录（调试产物）一律排除。
    """
    if not OUTPUT_BASE_DIR.is_dir():
        return []

    timestamps: List[int] = []
    for entry in OUTPUT_BASE_DIR.iterdir():
        if not entry.is_dir() or not entry.name.isdigit():
            continue
        if not (entry / ANALYSIS_ARTIFACT).is_file():
            continue
        timestamp = int(entry.name)
        try:
            # 目录名是纯数字但可能不是合法时间戳（如调试目录），格式化不了就跳过
            datetime.fromtimestamp(timestamp, tz=_TZ)
        except (OverflowError, OSError, ValueError):
            continue
        timestamps.append(timestamp)
    timestamps.sort(reverse=True)
    return timestamps


def _format_datetime(ts: int) -> str:
    """把 UNIX 时间戳（秒）格式化为 Asia/Shanghai 的 ISO8601（带 +08:00）。"""
    return datetime.fromtimestamp(ts, tz=_TZ).isoformat()


def _format_label(ts: int) -> str:
    """把 UNIX 时间戳（秒）格式化为页面展示用的 YYYY-MM-DD HH:MM（Asia/Shanghai）。"""
    return datetime.fromtimestamp(ts, tz=_TZ).strftime("%Y-%m-%d %H:%M")


def _load_tag_data(
    timestamps: List[int],
) -> Tuple[Dict[int, List[str]], List[Dict[str, Any]]]:
    """
    从数据库读取预览源 tag 数据（一次批量查询 + 一次全库统计）。

    Returns:
        (tag_map, tag_counts)
        tag_map    = {timestamp: [tag, ...]}
        tag_counts = [{"tag": tag, "count": 打该 tag 的时间戳数}, ...]

    数据库不可用时打印警告并返回空标签（页面仍可正常列出预览源），
    不让标签数据影响阈值调参主流程。
    """
    db = None
    try:
        db = SessionLocal()
        tag_map = crud.get_preview_tags_map(db, timestamps)
        counts = crud.list_preview_tag_counts(db)
    except Exception as exc:
        print(f"[ThresholdPreview] 读取预览标签失败，按无标签返回: {exc}")
        return {}, []
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass

    tag_counts = [{"tag": tag, "count": int(count)} for tag, count in counts]
    return tag_map, tag_counts


def list_sources() -> Dict[str, Any]:
    """
    列出全部可预览的分析结果源（时间倒序，最多 PREVIEW_SOURCE_MAX_RETURN 个）。

    Returns:
        {
          "sources": [{"id", "kind", "timestamp", "datetime", "label", "tags"}, ...],
          "default": "analysis:<ts>" | None,
          "tags": [{"tag", "count"}, ...],
          "presets": [...],
          "total": 全部可用时间点数量,
          "truncated": 是否因数量上限被截断
        }
    """
    all_timestamps = _scan_analysis_timestamps()
    total = len(all_timestamps)
    max_return = max(1, int(config.PREVIEW_SOURCE_MAX_RETURN))
    selected = all_timestamps[:max_return]

    tag_map, tag_counts = _load_tag_data(selected)

    sources = [
        {
            "id": f"analysis:{ts}",
            "kind": "analysis",
            "timestamp": ts,
            "datetime": _format_datetime(ts),
            "label": _format_label(ts),
            "tags": tag_map.get(ts, []),
        }
        for ts in selected
    ]

    return {
        "sources": sources,
        "default": sources[0]["id"] if sources else None,
        "tags": tag_counts,
        "presets": list(config.PREVIEW_TAG_PRESETS),
        "total": total,
        "truncated": total > len(selected),
    }


def _parse_analysis_source(source_id: Any) -> int:
    """
    解析 analysis:<timestamp> 形式的预览源 id，返回时间戳。

    只接受分析结果源；test: 等其他来源或非法格式抛 ValueError（中文提示）。
    """
    match = _ANALYSIS_SOURCE_PATTERN.match(source_id) if isinstance(source_id, str) else None
    if match is None:
        raise ValueError(
            f"仅支持分析结果预览源（analysis:<timestamp>）：{source_id}"
        )
    return int(match.group(1))


# --- 预处理（每个源只做一次）---

def _ocean_mask_for_shape(height: int, width: int) -> np.ndarray:
    """按图像尺寸生成生产口径的海洋蒙版，按 (宽, 高) 缓存。调用方需持有 _cache_lock。"""
    key = (width, height)
    cached = _mask_cache.get(key)
    if cached is not None:
        _mask_cache.move_to_end(key)
        return cached

    mask = geo_utils.create_ocean_mask(
        image_shape=(height, width),
        geojson_path=config.GEOJSON_PATH,
        bounds=config.TARGET_AREA,
        supersample=config.MASK_SUPERSAMPLE,
    )
    _mask_cache[key] = mask
    while len(_mask_cache) > MAX_MASK_CACHE:
        _mask_cache.popitem(last=False)
    return mask


def _downscale(rgb: np.ndarray, max_side: int = MAX_PREVIEW_SIDE) -> np.ndarray:
    """把图片按最大边长等比缩小（INTER_AREA），用于减少传输体积。"""
    height, width = rgb.shape[:2]
    longest = max(height, width)
    if longest <= max_side:
        return rgb
    scale = max_side / float(longest)
    return cv2.resize(
        rgb,
        (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
        interpolation=cv2.INTER_AREA,
    )


def _png_data_url(rgb: np.ndarray) -> str:
    """把 RGB 数组编码为 data:image/png;base64,... 字符串。"""
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    ok, buffer = cv2.imencode(".png", bgr, [cv2.IMWRITE_PNG_COMPRESSION, _PNG_COMPRESSION])
    if not ok:
        raise RuntimeError("预览图 PNG 编码失败")
    return "data:image/png;base64," + base64.b64encode(buffer.tobytes()).decode("ascii")


def _prepare_analysis_source(timestamp: int) -> Dict[str, Any]:
    """准备 analysis 源：读取 03_ocean_only.png，重建海洋蒙版，缓存 RGB/HSV/预览图。"""
    artifact_path = analysis_artifact_path(timestamp)
    if not artifact_path.is_file():
        raise ValueError(f"分析结果图不存在（可能已被清理）: {artifact_path.as_posix()}")

    image = Image.open(artifact_path).convert("RGB")
    rgb = np.array(image)
    height, width = rgb.shape[:2]
    ocean_mask = _ocean_mask_for_shape(height, width)
    print(f"[ThresholdPreview] 已加载分析结果预览源 analysis:{timestamp} ({width}x{height})")
    return _build_prepared(rgb, ocean_mask)


def _build_prepared(rgb: np.ndarray, ocean_mask: np.ndarray) -> Dict[str, Any]:
    """构造预处理缓存条目：全分辨率 RGB/HSV + 蒙版 + 已编码的源图预览。"""
    preview_rgb = _downscale(rgb)
    return {
        "rgb": rgb,
        "hsv": cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV),
        "mask": ocean_mask,
        "source_preview": _png_data_url(preview_rgb),
        "source_preview_width": int(preview_rgb.shape[1]),
        "source_preview_height": int(preview_rgb.shape[0]),
    }


def _get_prepared(timestamp: int) -> Dict[str, Any]:
    """获取（必要时构建）某个分析结果源的预处理结果；LRU 缓存，最多保留 4 个源。"""
    cache_key = f"analysis:{timestamp}"
    with _cache_lock:
        cached = _prepared_cache.get(cache_key)
        if cached is not None:
            _prepared_cache.move_to_end(cache_key)
            return cached

        prepared = _prepare_analysis_source(timestamp)
        _prepared_cache[cache_key] = prepared
        while len(_prepared_cache) > MAX_PREPARED_CACHE:
            _prepared_cache.popitem(last=False)
        return prepared


# --- 实时预览 ---

def render_preview(source_id: str, ranges: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    对指定分析结果源用给定阈值做一次 HSV 分类 + 着色 + 指标计算，返回可传输的预览结果。

    Args:
        source_id: 只支持 analysis:<timestamp>（timestamp 为 data/output/ 下的时间戳目录名）。
        ranges: 可选 HSV 阈值；为 None 时使用当前生效阈值（settings_store）。

    Returns:
        {
          "image": {"classification": data URL, "source_preview": data URL},
          "metrics": {...与 processor.analyze_ocean_color 相同的指标键...},
          "source": source_id,
          "width": int, "height": int,      # 返回的分类图尺寸（已按 ~1000px 下采样）
          "elapsedMs": int
        }

    非分析结果源抛 ValueError（400），阈值校验失败抛 SettingsValidationError（400）。
    调用过程不写任何文件。
    """
    started = time.perf_counter()

    # 1. 阈值校验（未传则使用当前生效值；校验复用 settings_store 的规则）
    if ranges is not None:
        effective_ranges = settings_store.validate_hsv_ranges(ranges)
    else:
        effective_ranges = None

    # 2. 只接受 analysis:<timestamp>（test: 等其他来源直接报错，不再做 ESRGAN 预处理）
    timestamp = _parse_analysis_source(source_id)

    # 3. 取预处理结果（首次读图 + 建蒙版，命中缓存后为毫秒级）
    prepared = _get_prepared(timestamp)

    # 4. 分类 + 着色 + 指标，全部复用生产实现
    if effective_ranges is None:
        effective_ranges = settings_store.get_hsv_ranges()

    hsv_image = prepared["hsv"]
    ocean_mask = prepared["mask"]
    class_masks = processor.classify_hsv(hsv_image, ocean_mask, effective_ranges)

    classification_rgb = processor.render_classification_map(
        hsv_image=hsv_image,
        class_masks=class_masks,
        output_path=None,  # 预览不落盘
        cloud_v_low=effective_ranges["CLOUD"]["lower"][2],
    )
    metrics = processor.compute_ocean_color_metrics(
        image_array=prepared["rgb"],
        ocean_mask=ocean_mask,
        class_masks=class_masks,
        output_dir=None,   # 不写 05_dark_channel.png
        verbose=False,
    )

    # 5. 下采样 + PNG 编码（分类图体积很小，源图预览已在预处理时编码好）
    classification_preview = _downscale(classification_rgb)
    classification_url = _png_data_url(classification_preview)

    elapsed_ms = int(round((time.perf_counter() - started) * 1000))

    return {
        "image": {
            "classification": classification_url,
            "source_preview": prepared["source_preview"],
        },
        "metrics": metrics,
        "source": source_id,
        "width": int(classification_preview.shape[1]),
        "height": int(classification_preview.shape[0]),
        "elapsedMs": elapsed_ms,
    }