# app/threshold_preview.py
"""
HSV 阈值实时预览。

复用生产链路的预处理结果与 processor 的判定/着色/指标逻辑，只对「已准备好的
单张图像」做 HSV 分类与渲染，不写任何文件，供 /tools/color_thresholds 页面
拖动阈值时实时查看效果。

图像源
------
- analysis:<ts>：取 data/output/<ts>/03_ocean_only.png（生产链路已完成超分 + CLAHE +
  海洋蒙版），海洋蒙版按该尺寸用 geo_utils.create_ocean_mask 重新生成（确定性结果，
  与生产完全一致）；
- test:<filename>：test_images/ 下的样本，首次预览时跑一次
  super_resolution.enhance + pipeline._auto_balance_color + geo_utils.apply_mask，
  之后走内存缓存。测试图是裁剪样本，用全图海洋蒙版（与 scripts/verify_visibility.py
  同口径），不对应真实地理范围。

缓存
----
- 预处理结果（RGB / HSV / 蒙版 / 源图预览 data URL）按 source 缓存，LRU 最多 4 个；
- 海洋蒙版按 (宽, 高) 缓存，多个同尺寸 analysis 源共用。
"""

import base64
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

from . import config, geo_utils, pipeline, processor, settings_store, super_resolution

# 分析结果预览源目录（与 main.run_analysis_and_persist 的输出目录一致）
OUTPUT_BASE_DIR = Path(config.OUTPUT_BASE_DIR)
# 测试图样本目录（与 tools.TEST_IMAGE_DIR 一致）
TEST_IMAGE_DIR = Path("test_images")
# 分析结果目录中用于预览的产物：已完成超分 + CLAHE + 海洋蒙版
ANALYSIS_ARTIFACT = "03_ocean_only.png"
# 最多展示多少个最新分析结果
MAX_ANALYSIS_SOURCES = 8
# 预处理缓存容量（LRU）
MAX_PREPARED_CACHE = 4
# 蒙版缓存容量
MAX_MASK_CACHE = 8
# 传输前图片最大边长
MAX_PREVIEW_SIDE = 1000
# PNG 编码压缩级别：1 = 速度优先（预览是交互操作，体积略大无妨）
_PNG_COMPRESSION = 1

_TZ = ZoneInfo(config.TIME_ZONE)

# 缓存与锁
_cache_lock = threading.RLock()
_prepared_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_mask_cache: "OrderedDict[Tuple[int, int], np.ndarray]" = OrderedDict()


# --- 图像源枚举 ---

def _format_timestamp(ts: str) -> str:
    """把 UNIX 时间戳（秒）格式化为 Asia/Shanghai 的可读时间。"""
    return datetime.fromtimestamp(int(ts), tz=_TZ).strftime("%Y-%m-%d %H:%M")


def _analysis_sources() -> List[Dict[str, str]]:
    """列出 data/output/<ts>/ 中可用的分析结果预览源（最新在前）。"""
    if not OUTPUT_BASE_DIR.is_dir():
        return []

    found: List[Tuple[int, Dict[str, str]]] = []
    for entry in OUTPUT_BASE_DIR.iterdir():
        if not entry.is_dir() or not entry.name.isdigit():
            continue
        if not (entry / ANALYSIS_ARTIFACT).is_file():
            continue
        timestamp = int(entry.name)
        found.append((
            timestamp,
            {
                "id": f"analysis:{entry.name}",
                "label": f"{_format_timestamp(entry.name)} 分析结果 ({entry.name})",
                "kind": "analysis",
            },
        ))

    found.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in found[:MAX_ANALYSIS_SOURCES]]


def _test_sources() -> List[Dict[str, str]]:
    """列出 test_images/ 下的测试样本预览源（按文件名排序）。"""
    if not TEST_IMAGE_DIR.is_dir():
        return []
    files = sorted(
        p for p in TEST_IMAGE_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg")
    )
    return [
        {"id": f"test:{p.name}", "label": p.name, "kind": "test"}
        for p in files
    ]


def list_sources() -> Dict[str, Any]:
    """
    返回可预览的图像源列表与默认源。

    {"sources": [{"id", "label", "kind"}...], "default": "analysis:<ts>" | None}
    """
    analysis = _analysis_sources()
    tests = _test_sources()
    sources = analysis + tests
    default = analysis[0]["id"] if analysis else (tests[0]["id"] if tests else None)
    return {"sources": sources, "default": default}


def _resolve_source(source_id: str) -> Dict[str, str]:
    """按 id 查找图像源；未知 id 抛 ValueError（中文提示）。"""
    for source in _analysis_sources() + _test_sources():
        if source["id"] == source_id:
            return source
    raise ValueError(f"未知的预览图片源: {source_id}")


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


def _prepare_analysis_source(source_id: str) -> Dict[str, Any]:
    """准备 analysis 源：读取 03_ocean_only.png，重建海洋蒙版，缓存 RGB/HSV/预览图。"""
    timestamp = source_id.split(":", 1)[1]
    artifact_path = OUTPUT_BASE_DIR / timestamp / ANALYSIS_ARTIFACT
    if not artifact_path.is_file():
        raise ValueError(f"分析结果图不存在（可能已被清理）: {artifact_path.as_posix()}")

    image = Image.open(artifact_path).convert("RGB")
    rgb = np.array(image)
    height, width = rgb.shape[:2]
    ocean_mask = _ocean_mask_for_shape(height, width)
    print(f"[ThresholdPreview] 已加载分析结果预览源 {source_id} ({width}x{height})")
    return _build_prepared(rgb, ocean_mask)


def _prepare_test_source(source_id: str) -> Dict[str, Any]:
    """准备 test 源：跑一次超分 + CLAHE + 蒙版（与生产预处理一致），缓存结果。"""
    filename = source_id.split(":", 1)[1]
    image_path = TEST_IMAGE_DIR / filename
    if not image_path.is_file():
        raise ValueError(f"测试图片不存在: {image_path.as_posix()}")

    image = Image.open(image_path).convert("RGB")
    image_array = np.array(image)
    # 与生产链路一致：Real-ESRGAN x4 超分（失败自动降级 bicubic）
    upscaled_rgb = super_resolution.enhance(image_array)
    # 与生产链路一致：在 BGR 空间做 CLAHE 自动色彩均衡
    balanced_bgr = pipeline._auto_balance_color(
        cv2.cvtColor(upscaled_rgb, cv2.COLOR_RGB2BGR)
    )
    balanced_rgb = cv2.cvtColor(balanced_bgr, cv2.COLOR_BGR2RGB)
    # 测试图是裁剪样本，用全图海洋蒙版（不对应真实地理范围，仅供参考调参）
    height, width = balanced_rgb.shape[:2]
    ocean_mask = np.full((height, width), 255, dtype=np.uint8)
    ocean_only = geo_utils.apply_mask(Image.fromarray(balanced_rgb), ocean_mask)
    print(f"[ThresholdPreview] 已准备测试图预览源 {source_id} ({width}x{height})")
    return _build_prepared(ocean_only, ocean_mask)


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


def _get_prepared(source_id: str) -> Dict[str, Any]:
    """获取（必要时构建）某个源的预处理结果；LRU 缓存，最多保留 4 个源。"""
    # 先校验源合法，避免对任意字符串做路径拼接
    source = _resolve_source(source_id)

    with _cache_lock:
        cached = _prepared_cache.get(source_id)
        if cached is not None:
            _prepared_cache.move_to_end(source_id)
            return cached

        if source["kind"] == "analysis":
            prepared = _prepare_analysis_source(source_id)
        else:
            prepared = _prepare_test_source(source_id)

        _prepared_cache[source_id] = prepared
        while len(_prepared_cache) > MAX_PREPARED_CACHE:
            _prepared_cache.popitem(last=False)
        return prepared


# --- 实时预览 ---

def render_preview(source_id: str, ranges: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    对指定图像源用给定阈值做一次 HSV 分类 + 着色 + 指标计算，返回可传输的预览结果。

    Args:
        source_id: list_sources() 返回的 id（analysis:<ts> 或 test:<filename>）。
        ranges: 可选 HSV 阈值；为 None 时使用当前生效阈值（settings_store）。

    Returns:
        {
          "image": {"classification": data URL, "source_preview": data URL},
          "metrics": {...与 processor.analyze_ocean_color 相同的指标键...},
          "source": source_id,
          "width": int, "height": int,      # 返回的分类图尺寸（已按 ~1000px 下采样）
          "elapsedMs": int
        }

    校验失败抛 SettingsValidationError（400），未知源/图片缺失抛 ValueError（400）。
    调用过程不写任何文件。
    """
    started = time.perf_counter()

    # 1. 阈值校验（未传则使用当前生效值；校验复用 settings_store 的规则）
    if ranges is not None:
        effective_ranges = settings_store.validate_hsv_ranges(ranges)
    else:
        effective_ranges = None

    # 2. 取预处理结果（首次数秒，命中缓存后为毫秒级）
    prepared = _get_prepared(source_id)

    # 3. 分类 + 着色 + 指标，全部复用生产实现
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

    # 4. 下采样 + PNG 编码（分类图体积很小，源图预览已在预处理时编码好）
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