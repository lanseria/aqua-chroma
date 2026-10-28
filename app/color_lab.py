# app/color_lab.py
"""
颜色阈值智能调优实验台后端（页面 /tools/color_thresholds）。

新流程（详见 docs/color-thresholds-lab.md）
-------------------------------------------
1. **导入卫星图片**：单张、多张或 ZIP 批量。图片会等比缩放到最长边
   ``TOOL_UPLOAD_MAX_SIDE`` 后落盘为 ``source.png``（后续所有分析的实际输入），
   并生成缩略图 ``thumb.jpg``。纯黑像素（max RGB <= 3）视为无效区域自动排除。
2. **色块划分**：在 Lab 颜色空间用 K-Means（K=TOOL_BLOCK_COUNT）聚类，
   再对每个聚类做连通域，得到一块块「颜色相近且空间连通」的色块并持久化
   （blocks.png + block_stats.json），页面点击时命中整块而不是单个像素。
3. **分类预览**：复用 ``processor.classify_hsv`` / ``render_classification_map`` /
   ``compute_ocean_color_metrics``，口径与生产一致（唯一的差别是这里没有地理
   海洋蒙版，按「全图有效区域」分析）。分类图上叠加纠正描边：
   绿色 = 当前参数判定与纠正一致，橙色 = 不一致，灰色 = 已标记忽略。
4. **纠正**：用户在结果图上点色块，指定真实类别
   （CLOUD / BLUE_WATER / YELLOW_WATER / IGNORE），写入 ``corrections.json``。
   IGNORE 的色块会被排除出指标统计（陆地 / 非海域）。
5. **自动优化 HSV**：把所有图片上的纠正色块当作标注样本，用坐标下降法搜索
   CLOUD / BLUE_WATER 的 HSV 阈值，最大化「标注像素上的分类准确率」；
   同分时优先保留离当前参数更近的解。只返回建议值，**绝不写数据库**，
   只有页面点「保存并立即生效」才会经 POST /tools/api/color_thresholds 落库。

本模块不依赖数据库；图片与纠正数据都在 ``TOOL_UPLOAD_DIR/<image_id>/`` 下。
"""

import base64
import io
import json
import re
import shutil
import threading
import time
import uuid
import zipfile
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageOps

from . import config, geo_utils, processor, settings_store

# ---------------------------------------------------------------------------
#  常量与错误类型
# ---------------------------------------------------------------------------

UPLOAD_ROOT = Path(config.TOOL_UPLOAD_DIR)
IMAGE_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_TZ = ZoneInfo(config.TIME_ZONE)

# 纠正类别（与页面、契约一致）
LABELS: "OrderedDict[str, str]" = OrderedDict(config.TOOL_CORRECTION_LABELS)
CLASS_LABELS = {"CLOUD": "云层", "BLUE_WATER": "蓝海", "YELLOW_WATER": "黄海"}
CLASS_ORDER = ("CLOUD", "BLUE_WATER", "YELLOW_WATER")
CLASS_INDEX = {key: i for i, key in enumerate(CLASS_ORDER)}
IGNORE_LABEL = "IGNORE"

# 分类图着色（仅可视化）
INVALID_COLOR = (18, 18, 22)        # 纯黑/无效像素
IGNORED_COLOR = (64, 58, 52)        # 用户标记为忽略的色块
BADGE_CN = {"CLOUD": "云", "BLUE_WATER": "蓝", "YELLOW_WATER": "黄", "IGNORE": "忽"}
BADGE_ASCII = {"CLOUD": "C", "BLUE_WATER": "B", "YELLOW_WATER": "Y", "IGNORE": "X"}
MATCH_COLOR = (34, 197, 94)         # 纠正与当前判定一致
MISMATCH_COLOR = (245, 158, 11)     # 纠正与当前判定不一致（优化目标）
IGNORE_CONTOUR_COLOR = (148, 148, 148)

# 预处理缓存
_prepared_lock = threading.RLock()
_prepared_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
MAX_PREPARED_CACHE = 6

# 自动优化：坐标下降可调参数与粗搜索网格
_SEARCH_KEYS = ("cl_s", "cl_v0", "bl_h0", "bl_h1", "bl_s0", "bl_s1", "bl_v0", "bl_v1")
_COARSE_GRID = {
    "cl_s": tuple(range(0, 256, 4)),
    "cl_v0": tuple(range(0, 256, 4)),
    "bl_h0": tuple(range(0, 180, 2)),
    "bl_h1": tuple(range(0, 180, 2)),
    "bl_s0": tuple(range(0, 256, 4)),
    "bl_s1": tuple(range(0, 256, 4)),
    "bl_v0": tuple(range(0, 256, 4)),
    "bl_v1": tuple(range(0, 256, 4)),
}
_COARSE_SWEEPS = 2
_FINE_SWEEPS = 2
_FINE_RADIUS = 4
_DISTANCE_PENALTY = 0.001  # 同分时偏好离当前参数更近的解


class LabError(Exception):
    """实验台业务错误基类；status_code 供 API 层映射 HTTP 状态码。"""

    status_code = 400


class LabValidationError(LabError):
    status_code = 400


class LabNotFoundError(LabError):
    status_code = 404


class LabTooLargeError(LabError):
    status_code = 413


# ---------------------------------------------------------------------------
#  通用小工具
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now_utc()).astimezone(_TZ).isoformat(timespec="seconds")


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _check_image_id(image_id: Any) -> str:
    """校验图片 id（12 位十六进制），防止路径穿越。"""
    if not isinstance(image_id, str) or IMAGE_ID_RE.match(image_id) is None:
        raise LabValidationError(f"无效的图片 id：{image_id!r}")
    return image_id


def _image_dir(image_id: str) -> Path:
    return UPLOAD_ROOT / _check_image_id(image_id)


def _iter_image_ids() -> List[str]:
    """按创建时间倒序列出全部已导入图片 id。"""
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    items: List[Tuple[float, str]] = []
    for entry in UPLOAD_ROOT.iterdir():
        if not entry.is_dir() or IMAGE_ID_RE.match(entry.name) is None:
            continue
        meta = _read_json(entry / "meta.json", None)
        if not isinstance(meta, dict) or "width" not in meta or "height" not in meta:
            continue
        created = _parse_iso_ts(meta.get("created_at"))
        items.append((created, entry.name))
    items.sort(key=lambda item: item[0], reverse=True)
    return [image_id for _, image_id in items]


def _parse_iso_ts(value: Any) -> float:
    if not isinstance(value, str):
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _read_meta(image_id: str) -> Dict[str, Any]:
    meta = _read_json(_image_dir(image_id) / "meta.json", None)
    if not isinstance(meta, dict):
        raise LabNotFoundError(f"图片 {image_id} 不存在或数据已损坏")
    return meta


def _read_corrections(image_id: str) -> List[Dict[str, Any]]:
    raw = _read_json(_image_dir(image_id) / "corrections.json", [])
    result: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return result
    for item in raw:
        if not isinstance(item, dict):
            continue
        block_id = item.get("block_id")
        label = item.get("label")
        if isinstance(block_id, bool) or not isinstance(block_id, int):
            continue
        if label not in LABELS:
            continue
        result.append({
            "block_id": int(block_id),
            "label": str(label),
            "created_at": item.get("created_at"),
        })
    return result


def _write_corrections(image_id: str, corrections: List[Dict[str, Any]]) -> None:
    path = _image_dir(image_id) / "corrections.json"
    _atomic_write_text(path, json.dumps(corrections, ensure_ascii=False, indent=2))


def _clean_filename(filename: Any) -> str:
    name = str(filename or "").replace("\\", "/").split("/")[-1].strip()
    name = name.replace("\x00", "")
    if not name:
        name = "upload.png"
    if len(name) > 160:
        suffix = Path(name).suffix[-12:]
        name = name[: 160 - len(suffix)] + suffix
    return name


def _new_image_id() -> str:
    for _ in range(10):
        candidate = uuid.uuid4().hex[:12]
        if not (_image_dir(candidate)).exists():
            return candidate
    raise LabValidationError("生成图片 id 失败，请重试")


# ---------------------------------------------------------------------------
#  图片摘要 / 列表
# ---------------------------------------------------------------------------

def _ignored_ratio(block_stats: Optional[Dict[str, Any]], corrections: List[Dict[str, Any]],
                   width: int, height: int) -> Optional[float]:
    if not block_stats or width <= 0 or height <= 0:
        return None
    blocks: Dict[int, Dict[str, Any]] = block_stats.get("blocks") or {}
    total = 0
    for corr in corrections:
        if corr.get("label") != IGNORE_LABEL:
            continue
        info = blocks.get(corr["block_id"])
        if info:
            total += int(info.get("pixel_count", 0))
    return round(total / float(width * height), 6)


def _summary(image_id: str, meta: Dict[str, Any], corrections: List[Dict[str, Any]],
             block_stats: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    width = int(meta.get("width") or 0)
    height = int(meta.get("height") or 0)
    return {
        "id": image_id,
        "name": str(meta.get("name") or image_id),
        "width": width,
        "height": height,
        "thumb_url": f"/{UPLOAD_ROOT.as_posix()}/{image_id}/thumb.jpg",
        "created_at": meta.get("created_at"),
        "source": str(meta.get("source") or "file"),
        "correction_count": len(corrections),
        "block_count": int(block_stats["block_count"]) if block_stats else None,
        "ignored_ratio": _ignored_ratio(block_stats, corrections, width, height),
    }


def list_images() -> List[Dict[str, Any]]:
    """列出全部已导入图片（按导入时间倒序）。"""
    items: List[Dict[str, Any]] = []
    for image_id in _iter_image_ids():
        try:
            meta = _read_meta(image_id)
            corrections = _read_corrections(image_id)
            block_stats = _load_block_stats(image_id)
        except LabNotFoundError:
            continue
        items.append(_summary(image_id, meta, corrections, block_stats))
    return items


def get_lab_state() -> Dict[str, Any]:
    """页面初始状态：已导入图片列表、限制、可用的纠正类别。"""
    images = list_images()
    return {
        "images": images,
        "limits": {
            "max_images": int(config.TOOL_UPLOAD_MAX_IMAGES),
            "max_upload_bytes": int(config.TOOL_UPLOAD_MAX_BYTES),
            "max_side": int(config.TOOL_UPLOAD_MAX_SIDE),
            "zip_max_entries": int(config.TOOL_ZIP_MAX_ENTRIES),
            "allowed_ext": list(config.TOOL_IMAGE_EXTENSIONS),
        },
        "labels": [{"key": key, "label": label} for key, label in LABELS.items()],
        "corrections_total": sum(int(item["correction_count"]) for item in images),
    }


# ---------------------------------------------------------------------------
#  导入
# ---------------------------------------------------------------------------

def _import_image_bytes(raw: bytes, name: str, source: str) -> Dict[str, Any]:
    """把一个图片字节流导入为实验台图片（缩放、落盘、生成缩略图）。"""
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            transposed = ImageOps.exif_transpose(opened)
            image = transposed.convert("RGB")
    except Exception as exc:  # noqa: BLE001 - 解码失败统一转中文错误
        raise LabValidationError("不是可识别的图片（无法解码）") from exc

    width, height = image.size
    if width < 16 or height < 16:
        raise LabValidationError(f"图片尺寸过小（{width}×{height}px，至少 16×16）")

    max_side = max(1, int(config.TOOL_UPLOAD_MAX_SIDE))
    if max(width, height) > max_side:
        scale = max_side / float(max(width, height))
        image = image.resize(
            (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
            Image.LANCZOS,
        )

    image_id = _new_image_id()
    directory = _image_dir(image_id)
    directory.mkdir(parents=True, exist_ok=False)
    try:
        image.save(directory / "source.png")
        thumb = image.copy()
        thumb.thumbnail((int(config.TOOL_THUMB_MAX_SIDE), int(config.TOOL_THUMB_MAX_SIDE)),
                        Image.LANCZOS)
        thumb.save(directory / "thumb.jpg", quality=84, optimize=True)
        meta = {
            "id": image_id,
            "name": _clean_filename(name),
            "source": source,
            "width": int(image.width),
            "height": int(image.height),
            "original_width": int(width),
            "original_height": int(height),
            "created_at": _iso(),
        }
        _atomic_write_text(directory / "meta.json",
                           json.dumps(meta, ensure_ascii=False, indent=2))
        _write_corrections(image_id, [])
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    print(f"[ColorLab] 已导入图片 {image_id}（{image.width}×{image.height}，来源 {source}）")
    return _summary(image_id, meta, [], None)


def _import_zip(data: bytes, remaining: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], bool]:
    """解压并逐个导入 ZIP 内的图片。返回 (imported, skipped, truncated)。"""
    imported: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    truncated = False
    total_bytes = 0

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise LabValidationError("ZIP 文件损坏或不完整") from exc

    with archive:
        entries = [info for info in archive.infolist() if not info.is_dir()]
        for index, info in enumerate(entries):
            raw_name = info.filename
            base = Path(raw_name).name
            if (not base) or base.startswith("._") or "__MACOSX" in raw_name or raw_name.startswith("."):
                continue

            if index >= int(config.TOOL_ZIP_MAX_ENTRIES):
                truncated = True
                skipped.append({"name": raw_name,
                                "reason": f"ZIP 条目数超过上限 {config.TOOL_ZIP_MAX_ENTRIES}"})
                break

            total_bytes += int(info.file_size)
            if total_bytes > int(config.TOOL_ZIP_MAX_TOTAL_BYTES):
                truncated = True
                skipped.append({"name": raw_name, "reason": "ZIP 解压后总大小超过上限"})
                break

            if len(imported) >= remaining:
                truncated = True
                skipped.append({"name": raw_name,
                                "reason": f"已达到图片数量上限 {config.TOOL_UPLOAD_MAX_IMAGES} 张"})
                break

            suffix = Path(base).suffix.lower()
            if suffix not in config.TOOL_IMAGE_EXTENSIONS:
                skipped.append({"name": raw_name, "reason": "不是支持的图片格式"})
                continue
            if int(info.file_size) > int(config.TOOL_UPLOAD_MAX_BYTES):
                skipped.append({"name": raw_name, "reason": "单张图片超过大小上限"})
                continue

            try:
                raw = archive.read(info)
            except Exception as exc:  # noqa: BLE001
                skipped.append({"name": raw_name, "reason": f"读取失败：{exc}"})
                continue
            try:
                imported.append(_import_image_bytes(raw, base, f"zip:{raw_name}"))
            except LabValidationError as exc:
                skipped.append({"name": raw_name, "reason": str(exc)})
            except Exception as exc:  # noqa: BLE001
                skipped.append({"name": raw_name, "reason": f"导入失败：{exc}"})

    return imported, skipped, truncated


def import_upload(data: bytes, filename: str) -> Dict[str, Any]:
    """
    导入上传内容（单张图片或一个 ZIP）。

    返回 ``{"imported": [...], "skipped": [...], "truncated": bool, "selected": id|None}``，
    单文件导入失败时直接抛 LabValidationError（400）。
    """
    if not data:
        raise LabValidationError("上传内容为空")
    if len(data) > int(config.TOOL_UPLOAD_MAX_BYTES):
        limit_mb = config.TOOL_UPLOAD_MAX_BYTES / 1048576.0
        raise LabTooLargeError(
            f"上传文件过大（{len(data) / 1048576.0:.1f} MB），单次上限 {limit_mb:.0f} MB"
        )

    safe_name = _clean_filename(filename)
    remaining = int(config.TOOL_UPLOAD_MAX_IMAGES) - len(_iter_image_ids())
    if remaining <= 0:
        raise LabValidationError(
            f"已达到图片数量上限 {config.TOOL_UPLOAD_MAX_IMAGES} 张，请先删除部分图片"
        )

    imported: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    truncated = False

    if zipfile.is_zipfile(io.BytesIO(data)):
        imported, skipped, truncated = _import_zip(data, remaining)
        if not imported and not skipped:
            raise LabValidationError("ZIP 内没有找到可导入的图片")
    else:
        try:
            imported.append(_import_image_bytes(data, safe_name, "file"))
        except LabValidationError as exc:
            raise LabValidationError(f"无法导入 {safe_name}：{exc}") from exc

    return {
        "imported": imported,
        "skipped": skipped[:20],
        "truncated": bool(truncated),
        "selected": imported[0]["id"] if imported else None,
    }


def delete_image(image_id: str) -> Dict[str, Any]:
    """删除一张导入图片（含纠正数据）。"""
    directory = _image_dir(image_id)
    if not directory.is_dir():
        raise LabNotFoundError(f"图片 {image_id} 不存在")
    shutil.rmtree(directory, ignore_errors=True)
    with _prepared_lock:
        _prepared_cache.pop(image_id, None)
    print(f"[ColorLab] 已删除图片 {image_id}")
    return {"deleted": True, "images": list_images()}


def clear_images() -> Dict[str, Any]:
    """清空全部导入图片。"""
    deleted = 0
    for image_id in _iter_image_ids():
        shutil.rmtree(_image_dir(image_id), ignore_errors=True)
        deleted += 1
    with _prepared_lock:
        _prepared_cache.clear()
    print(f"[ColorLab] 已清空 {deleted} 张导入图片")
    return {"deleted": deleted, "images": []}


# ---------------------------------------------------------------------------
#  预处理缓存与色块划分
# ---------------------------------------------------------------------------

def _load_block_stats(image_id: str) -> Optional[Dict[str, Any]]:
    """读取 block_stats.json，返回 {"block_count", "blocks": {id: {...}}}；不存在返回 None。"""
    raw = _read_json(_image_dir(image_id) / "block_stats.json", None)
    if not isinstance(raw, dict):
        return None
    raw_blocks = raw.get("blocks")
    if not isinstance(raw_blocks, list):
        return None
    blocks: Dict[int, Dict[str, Any]] = {}
    for item in raw_blocks:
        if not isinstance(item, dict):
            continue
        block_id = item.get("id")
        if isinstance(block_id, bool) or not isinstance(block_id, int):
            continue
        blocks[int(block_id)] = {
            "id": int(block_id),
            "pixel_count": int(item.get("pixel_count", 0)),
            "bbox": [int(v) for v in item.get("bbox", [0, 0, 0, 0])],
            "mean_hsv": [int(v) for v in item.get("mean_hsv", [0, 0, 0])],
            "mean_rgb": [int(v) for v in item.get("mean_rgb", [0, 0, 0])],
        }
    return {"block_count": int(raw.get("block_count", len(blocks))), "blocks": blocks}


def _save_block_stats(image_id: str, block_map: np.ndarray,
                      block_stats: Dict[str, Any]) -> None:
    directory = _image_dir(image_id)
    payload = {
        "block_count": int(block_stats["block_count"]),
        "blocks": list(block_stats["blocks"].values()),
    }
    _atomic_write_text(directory / "block_stats.json",
                       json.dumps(payload, ensure_ascii=False))
    cv2.imwrite(str(directory / "blocks.png"), block_map.astype(np.uint16))


def _load_block_map(image_id: str, shape: Tuple[int, int]) -> Optional[np.ndarray]:
    path = _image_dir(image_id) / "blocks.png"
    if not path.is_file():
        return None
    data = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if data is None or data.dtype != np.uint16 or data.shape[:2] != shape:
        return None
    return data.astype(np.int32)


def _nearest_centers(lab_flat: np.ndarray, centers: np.ndarray,
                     chunk: int = 262144) -> np.ndarray:
    """把每个像素分配到最近的聚类中心（分块计算，避免大图内存峰值）。"""
    total = int(lab_flat.shape[0])
    result = np.empty(total, dtype=np.int16)
    centers32 = centers.astype(np.float32)
    center_sq = np.einsum("kd,kd->k", centers32, centers32)[None, :]
    for start in range(0, total, chunk):
        stop = min(total, start + chunk)
        block = lab_flat[start:stop].astype(np.float32)
        block_sq = np.einsum("nd,nd->n", block, block)[:, None]
        cross = block @ centers32.T
        dist = block_sq - 2.0 * cross + center_sq
        result[start:stop] = np.argmin(dist, axis=1).astype(np.int16)
    return result


def _build_blocks(rgb: np.ndarray, hsv: np.ndarray,
                  valid: np.ndarray) -> Tuple[np.ndarray, Dict[str, Any]]:
    """
    在 Lab 空间聚类 + 连通域，生成色块图与色块统计。

    Returns:
        (block_map(int32, 0=不属于任何色块), {"block_count", "blocks": {id: info}})
    """
    height, width = valid.shape
    empty_map = np.zeros((height, width), dtype=np.int32)
    valid_flat = valid.reshape(-1)
    valid_count = int(np.count_nonzero(valid_flat))
    if valid_count < int(config.TOOL_BLOCK_MIN_PIXELS):
        return empty_map, {"block_count": 0, "blocks": {}}

    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).reshape(-1, 3)
    valid_indices = np.flatnonzero(valid_flat)
    rng = np.random.default_rng(20261028)
    sample_size = min(valid_count, 60000)
    if sample_size < valid_count:
        picks = rng.choice(valid_count, size=sample_size, replace=False)
        sample = lab[valid_indices[picks]].astype(np.float32)
    else:
        sample = lab[valid_indices].astype(np.float32)

    k = int(config.TOOL_BLOCK_COUNT)
    k = max(1, min(k, sample_size // 120, 32))

    if k <= 1:
        cluster_flat = np.zeros(lab.shape[0], dtype=np.int16)
    else:
        cv2.setRNGSeed(20261028)
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 0.5)
        _, _, centers = cv2.kmeans(sample, k, None, criteria, 1, cv2.KMEANS_PP_CENTERS)
        cluster_flat = _nearest_centers(lab, centers)

    cluster = cluster_flat.reshape(height, width)
    cluster[~valid] = -1

    block_map = np.zeros((height, width), dtype=np.int32)
    block_infos: Dict[int, Dict[str, Any]] = {}
    next_id = 0
    # 色块最小像素数：小图固定用配置值，大图按面积比例提高，
    # 否则纹理丰富的大图会被切成上千个碎片，点击体验很差。
    min_pixels = max(int(config.TOOL_BLOCK_MIN_PIXELS),
                     int(round(height * width * 0.0004)))
    kernel_size = 3 if min(height, width) < 600 else 5
    kernel = np.ones((kernel_size, kernel_size), dtype=np.uint8)

    for cluster_id in range(k):
        cluster_mask = (cluster == cluster_id) & valid
        if not np.any(cluster_mask):
            continue
        # 先做形态学闭运算，把同一聚类里只差几个像素的碎片连起来，再取连通域
        closed = cv2.morphologyEx(cluster_mask.astype(np.uint8), cv2.MORPH_CLOSE, kernel)
        num, comp, cc_stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
        if num <= 1:
            continue
        lut = np.zeros(num, dtype=np.int32)
        for comp_id in range(1, num):
            area = int(cc_stats[comp_id, cv2.CC_STAT_AREA])
            if area < min_pixels or next_id >= 65535:
                continue
            next_id += 1
            lut[comp_id] = next_id
            x = int(cc_stats[comp_id, cv2.CC_STAT_LEFT])
            y = int(cc_stats[comp_id, cv2.CC_STAT_TOP])
            w = int(cc_stats[comp_id, cv2.CC_STAT_WIDTH])
            h = int(cc_stats[comp_id, cv2.CC_STAT_HEIGHT])
            block_infos[next_id] = {
                "id": next_id,
                "pixel_count": area,
                "bbox": [x, y, x + w - 1, y + h - 1],
                "mean_hsv": [0, 0, 0],
                "mean_rgb": [0, 0, 0],
            }
        if next_id == 0:
            continue
        mapped = lut[comp]
        # 闭运算会略微外扩，最终只把原始有效像素划入色块
        selected = (mapped > 0) & valid
        block_map[selected] = mapped[selected]

    if not block_infos:
        return empty_map, {"block_count": 0, "blocks": {}}

    # 统计每个色块的均值 HSV / RGB
    flat_map = block_map.reshape(-1)
    positions = np.flatnonzero(flat_map)
    ids = flat_map[positions]
    counts = np.bincount(ids, minlength=next_id + 1)
    hsv_flat = hsv.reshape(-1, 3)
    rgb_flat = rgb.reshape(-1, 3)
    hsv_values = hsv_flat[positions].astype(np.float64)
    rgb_values = rgb_flat[positions].astype(np.float64)
    for block_id in range(1, next_id + 1):
        if block_id in block_infos:
            # 以实际划入色块（交集有效区域后）的像素数为准
            block_infos[block_id]["pixel_count"] = int(counts[block_id])
    for index, channel in enumerate("HSV"):
        sums = np.bincount(ids, weights=hsv_values[:, index], minlength=next_id + 1)
        for block_id in range(1, next_id + 1):
            if counts[block_id] > 0 and block_id in block_infos:
                block_infos[block_id]["mean_hsv"][index] = int(
                    round(sums[block_id] / counts[block_id])
                )
    for index in range(3):
        sums = np.bincount(ids, weights=rgb_values[:, index], minlength=next_id + 1)
        for block_id in range(1, next_id + 1):
            if counts[block_id] > 0 and block_id in block_infos:
                block_infos[block_id]["mean_rgb"][index] = int(
                    round(sums[block_id] / counts[block_id])
                )

    return block_map, {"block_count": len(block_infos), "blocks": block_infos}


def _prepare(image_id: str) -> Dict[str, Any]:
    """读取图片 → RGB/HSV/有效掩码/色块图（色块惰性计算并落盘缓存）。"""
    directory = _image_dir(image_id)
    source_path = directory / "source.png"
    if not source_path.is_file():
        raise LabNotFoundError(f"图片 {image_id} 的源文件不存在，可能已被删除")

    meta = _read_meta(image_id)
    with Image.open(source_path) as opened:
        rgb = np.array(opened.convert("RGB"))
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    # 纯黑像素（max RGB <= 3）视为无效：生产产物里陆地/无数据区域就是纯黑，
    # 这样导入 03_ocean_only.png 时与生产的海洋蒙版口径自然对齐。
    valid = rgb.max(axis=2) > 3

    block_stats = _load_block_stats(image_id)
    block_map = _load_block_map(image_id, valid.shape) if block_stats else None
    if block_stats is None or block_map is None:
        block_map, block_stats = _build_blocks(rgb, hsv, valid)
        _save_block_stats(image_id, block_map, block_stats)

    source_preview = _downscale(rgb, int(config.TOOL_PREVIEW_MAX_SIDE))
    print(f"[ColorLab] 已加载图片 {image_id}（{rgb.shape[1]}×{rgb.shape[0]}，"
          f"{block_stats['block_count']} 个色块）")
    return {
        "id": image_id,
        "meta": meta,
        "rgb": rgb,
        "hsv": hsv,
        "valid": valid,
        "valid_mask": (valid.astype(np.uint8) * 255),
        "block_map": block_map,
        "block_stats": block_stats,
        "source_preview": _png_data_url(source_preview),
        "source_preview_size": (int(source_preview.shape[1]), int(source_preview.shape[0])),
    }


def _get_prepared(image_id: str) -> Dict[str, Any]:
    """获取（必要时构建）某张图片的预处理结果；LRU 缓存最多 MAX_PREPARED_CACHE 张。"""
    with _prepared_lock:
        cached = _prepared_cache.get(image_id)
        if cached is not None:
            _prepared_cache.move_to_end(image_id)
            return cached
        prepared = _prepare(image_id)
        _prepared_cache[image_id] = prepared
        while len(_prepared_cache) > MAX_PREPARED_CACHE:
            _prepared_cache.popitem(last=False)
        return prepared


# ---------------------------------------------------------------------------
#  图像处理小工具
# ---------------------------------------------------------------------------

def _downscale(rgb: np.ndarray, max_side: int) -> np.ndarray:
    height, width = rgb.shape[:2]
    longest = max(height, width)
    if longest <= max_side or longest <= 1:
        return rgb
    scale = max_side / float(longest)
    return cv2.resize(
        rgb,
        (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
        interpolation=cv2.INTER_AREA,
    )


def _png_data_url(rgb: np.ndarray) -> str:
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    ok, buffer = cv2.imencode(".png", bgr, [cv2.IMWRITE_PNG_COMPRESSION, 1])
    if not ok:
        raise RuntimeError("预览图 PNG 编码失败")
    return "data:image/png;base64," + base64.b64encode(buffer.tobytes()).decode("ascii")


def _resolve_ranges(ranges: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if ranges is None:
        return settings_store.get_hsv_ranges()
    return settings_store.validate_hsv_ranges(ranges)


def _font_for_badge(size: int):
    try:
        return geo_utils.load_cjk_font(size, config.FONT_CANDIDATES)
    except Exception:  # noqa: BLE001 - 字体缺失不应影响预览
        return None


def _block_mask(block_map: np.ndarray, block_id: int) -> np.ndarray:
    return block_map == int(block_id)


def _predicted_class(class_masks: Dict[str, np.ndarray], mask: np.ndarray) -> Optional[str]:
    """色块在当前阈值下的主要判定类别；色块内无分类像素时返回 None。"""
    best_key = None
    best_count = 0
    for key in CLASS_ORDER:
        mask_arr = class_masks.get(key)
        if mask_arr is None:
            continue
        count = int(np.count_nonzero(mask_arr[mask]))
        if count > best_count:
            best_key, best_count = key, count
    return best_key if best_count > 0 else None


def _draw_corrections(render: np.ndarray, prepared: Dict[str, Any],
                      class_masks: Dict[str, np.ndarray],
                      corrections: List[Dict[str, Any]],
                      selection: Optional[Dict[str, Any]]) -> None:
    """在分类图上叠加纠正描边与类别角标（原地修改）。"""
    block_map = prepared["block_map"]
    texts: List[Tuple[int, int, str, Tuple[int, int, int]]] = []

    for corr in corrections:
        block_id = int(corr["block_id"])
        label = str(corr["label"])
        mask = _block_mask(block_map, block_id)
        if not np.any(mask):
            continue
        contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue

        if label == IGNORE_LABEL:
            color = IGNORE_CONTOUR_COLOR
        else:
            total = int(np.count_nonzero(mask))
            hit = int(np.count_nonzero(class_masks[label][mask])) if label in class_masks else 0
            matches = total > 0 and hit * 2 >= total
            color = MATCH_COLOR if matches else MISMATCH_COLOR

        cv2.drawContours(render, contours, -1, (0, 0, 0), 4, lineType=cv2.LINE_AA)
        cv2.drawContours(render, contours, -1, color, 2, lineType=cv2.LINE_AA)

        contour = max(contours, key=cv2.contourArea)
        moments = cv2.moments(contour)
        if moments["m00"] > 0:
            cx = int(moments["m10"] / moments["m00"])
            cy = int(moments["m01"] / moments["m00"])
        else:
            x, y, _, _ = cv2.boundingRect(contour)
            cx, cy = int(x), int(y)
        texts.append((cx, cy, BADGE_CN.get(label, "?"), color))

    if selection and selection.get("block_id") is not None:
        selection_mask = _block_mask(block_map, int(selection["block_id"]))
        if np.any(selection_mask):
            blended = (render[selection_mask].astype(np.float32) * 0.45
                       + np.array([255.0, 255.0, 255.0], dtype=np.float32) * 0.55)
            render[selection_mask] = np.clip(blended, 0, 255).astype(np.uint8)
            contours, _ = cv2.findContours(selection_mask.astype(np.uint8),
                                           cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if contours:
                cv2.drawContours(render, contours, -1, (255, 255, 255), 4,
                                 lineType=cv2.LINE_AA)
                contour = max(contours, key=cv2.contourArea)
                moments = cv2.moments(contour)
                if moments["m00"] > 0:
                    cx = int(moments["m10"] / moments["m00"])
                    cy = int(moments["m01"] / moments["m00"])
                else:
                    x, y, _, _ = cv2.boundingRect(contour)
                    cx, cy = int(x), int(y)
                texts.append((cx, cy, "选中", (255, 255, 255)))

    if not texts:
        return
    height, width = render.shape[:2]
    font = _font_for_badge(max(14, int(round(min(height, width) * 0.022))))
    pil_image = Image.fromarray(render)
    draw = ImageDraw.Draw(pil_image)
    for cx, cy, text, color in texts:
        if font is not None:
            draw.text((cx, cy), text, font=font, fill=color, anchor="mm",
                      stroke_width=2, stroke_fill=(0, 0, 0))
        else:
            fallback = BADGE_ASCII.get(
                next((key for key, value in BADGE_CN.items() if value == text), ""), "?"
            )
            draw.text((cx, cy), fallback, fill=color, anchor="mm",
                      stroke_width=2, stroke_fill=(0, 0, 0))
    render[:, :, :] = np.array(pil_image)


def _ignore_mask(prepared: Dict[str, Any],
                 corrections: List[Dict[str, Any]]) -> np.ndarray:
    ignored = np.zeros(prepared["block_map"].shape, dtype=np.uint8)
    for corr in corrections:
        if corr.get("label") != IGNORE_LABEL:
            continue
        ignored[_block_mask(prepared["block_map"], corr["block_id"])] = 255
    return ignored


def _analysis_mask(prepared: Dict[str, Any], ignore: np.ndarray) -> np.ndarray:
    mask = prepared["valid_mask"].copy()
    if np.any(ignore):
        mask[ignore > 0] = 0
    return mask


def _correction_accuracy(prepared: Dict[str, Any], corrections: List[Dict[str, Any]],
                         class_masks: Dict[str, np.ndarray]) -> Optional[float]:
    total = 0
    hit = 0
    for corr in corrections:
        label = corr.get("label")
        if label not in CLASS_LABELS:
            continue
        mask = _block_mask(prepared["block_map"], corr["block_id"])
        count = int(np.count_nonzero(mask))
        if count == 0:
            continue
        total += count
        hit += int(np.count_nonzero(class_masks[label][mask]))
    if total == 0:
        return None
    return round(hit / float(total), 4)


def _corrections_rich(prepared: Dict[str, Any], corrections: List[Dict[str, Any]],
                      class_masks: Dict[str, np.ndarray]) -> List[Dict[str, Any]]:
    """把纠正记录补全为页面展示需要的富信息。"""
    blocks: Dict[int, Dict[str, Any]] = prepared["block_stats"].get("blocks") or {}
    total_pixels = max(1, prepared["rgb"].shape[0] * prepared["rgb"].shape[1])
    rich: List[Dict[str, Any]] = []
    for corr in corrections:
        block_id = int(corr["block_id"])
        info = blocks.get(block_id)
        if info is None:
            continue
        label = str(corr["label"])
        mask = _block_mask(prepared["block_map"], block_id)
        predicted = _predicted_class(class_masks, mask)
        pixel_count = int(info["pixel_count"])
        matches = None
        if label in CLASS_LABELS:
            hit = int(np.count_nonzero(class_masks[label][mask]))
            matches = bool(pixel_count > 0 and hit * 2 >= pixel_count)
        rich.append({
            "block_id": block_id,
            "label": label,
            "label_cn": LABELS.get(label, label),
            "pixel_count": pixel_count,
            "pixel_ratio": round(pixel_count / float(total_pixels), 6),
            "mean_hsv": list(info["mean_hsv"]),
            "mean_rgb": list(info["mean_rgb"]),
            "bbox": list(info["bbox"]),
            "current_predicted": predicted,
            "current_predicted_label": CLASS_LABELS.get(predicted) if predicted else None,
            "matches": matches,
            "created_at": corr.get("created_at"),
        })
    return rich


# ---------------------------------------------------------------------------
#  预览渲染
# ---------------------------------------------------------------------------

def _build_preview_payload(prepared: Dict[str, Any], ranges: Dict[str, Any],
                           ignore: np.ndarray, analysis_mask: np.ndarray,
                           class_masks: Dict[str, np.ndarray],
                           corrections: List[Dict[str, Any]],
                           selection: Optional[Dict[str, Any]],
                           show_corrections: bool,
                           started: float) -> Dict[str, Any]:
    render = processor.render_classification_map(
        hsv_image=prepared["hsv"],
        class_masks=class_masks,
        output_path=None,
        cloud_v_low=int(ranges["CLOUD"]["lower"][2]),
    )
    height, width = prepared["hsv"].shape[:2]
    if render.shape[0] != height or render.shape[1] != width:
        render = render[:height, :width].copy()
    else:
        render = render.copy()

    # 忽略 / 无效区域着色（分类只发生在有效区域，这里只影响可视化）
    if np.any(ignore):
        render[ignore > 0] = IGNORED_COLOR
    invalid = ~prepared["valid"]
    if np.any(invalid):
        render[invalid] = INVALID_COLOR

    if show_corrections:
        _draw_corrections(render, prepared, class_masks, corrections, selection)

    preview_rgb = _downscale(render, int(config.TOOL_PREVIEW_MAX_SIDE))
    preview_height, preview_width = preview_rgb.shape[:2]

    metrics = processor.compute_ocean_color_metrics(
        image_array=prepared["rgb"],
        ocean_mask=analysis_mask,
        class_masks=class_masks,
        output_dir=None,
        verbose=False,
    )

    total_pixels = int(height * width)
    ignored_pixels = int(np.count_nonzero(ignore))
    analysis = {
        "analyzed_pixels": int(np.count_nonzero(analysis_mask)),
        "auto_excluded_pixels": int(np.count_nonzero(~prepared["valid"])),
        "ignored_pixels": ignored_pixels,
        "ignored_ratio": round(ignored_pixels / float(max(1, total_pixels)), 6),
        "total_pixels": total_pixels,
        "block_count": int(prepared["block_stats"]["block_count"]),
        "correction_accuracy": _correction_accuracy(prepared, corrections, class_masks),
    }

    return {
        "image": {
            "classification": _png_data_url(preview_rgb),
            "source_preview": prepared["source_preview"],
        },
        "width": int(preview_width),
        "height": int(preview_height),
        "full_width": int(width),
        "full_height": int(height),
        "metrics": metrics,
        "analysis": analysis,
        # 当前图片的完整纠正明细：前端切图 / 刷新 / 改阈值重算后可直接恢复 chips
        "corrections": _corrections_rich(prepared, corrections, class_masks),
        "elapsedMs": int(round((time.perf_counter() - started) * 1000)),
    }


def _state(image_id: str, ranges: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """装配一次渲染所需的内部状态（预处理 + 纠正掩码 + 分类掩码），不生成图片。"""
    started = time.perf_counter()
    effective_ranges = _resolve_ranges(ranges)
    prepared = _get_prepared(image_id)
    corrections = _read_corrections(image_id)
    ignore = _ignore_mask(prepared, corrections)
    analysis_mask = _analysis_mask(prepared, ignore)
    class_masks = processor.classify_hsv(prepared["hsv"], analysis_mask, effective_ranges)
    return {
        "started": started,
        "prepared": prepared,
        "corrections": corrections,
        "ignore": ignore,
        "analysis_mask": analysis_mask,
        "class_masks": class_masks,
        "ranges": effective_ranges,
    }


def _render(image_id: str, ranges: Optional[Dict[str, Any]],
            selection: Optional[Dict[str, Any]] = None,
            show_corrections: bool = True) -> Dict[str, Any]:
    """渲染一张图的分类预览，返回预览 payload 以及内部状态（供点击/纠正复用）。"""
    state = _state(image_id, ranges)
    state["payload"] = _build_preview_payload(
        prepared=state["prepared"],
        ranges=state["ranges"],
        ignore=state["ignore"],
        analysis_mask=state["analysis_mask"],
        class_masks=state["class_masks"],
        corrections=state["corrections"],
        selection=selection,
        show_corrections=show_corrections,
        started=state["started"],
    )
    return state


def render_preview(image_id: str, ranges: Optional[Dict[str, Any]] = None,
                   selection: Optional[Dict[str, Any]] = None,
                   show_corrections: bool = True) -> Dict[str, Any]:
    """实时预览：用给定（或当前生效）阈值分类 + 着色 + 计算指标，不写任何文件。"""
    return _render(image_id, ranges, selection, show_corrections)["payload"]


# ---------------------------------------------------------------------------
#  点击色块 / 纠正
# ---------------------------------------------------------------------------

def _coerce_block_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise LabValidationError(f"block_id 必须是整数，实际为 {value!r}") from exc
    return int(value)


def pick_block(image_id: str, x: Any, y: Any,
               ranges: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    点击坐标（相对预览图的归一化 0~1）命中整块色块，返回色块信息与高亮预览。
    """
    try:
        nx = float(x)
        ny = float(y)
    except (TypeError, ValueError) as exc:
        raise LabValidationError(f"点击坐标必须是 0~1 的数字，实际为 ({x!r}, {y!r})") from exc
    if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
        raise LabValidationError(f"点击坐标必须归一化到 0~1，实际为 ({nx}, {ny})")

    # 先装配状态（含当前阈值的分类结果），点击命中后再生成一次带高亮的预览
    state = _state(image_id, ranges)
    prepared = state["prepared"]
    block_map = prepared["block_map"]
    height, width = block_map.shape

    px = min(width - 1, max(0, int(round(nx * (width - 1)))))
    py = min(height - 1, max(0, int(round(ny * (height - 1)))))

    def _payload(selection: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        return _build_preview_payload(
            prepared=prepared,
            ranges=state["ranges"],
            ignore=state["ignore"],
            analysis_mask=state["analysis_mask"],
            class_masks=state["class_masks"],
            corrections=state["corrections"],
            selection=selection,
            show_corrections=True,
            started=state["started"],
        )

    def _empty(reason: str) -> Dict[str, Any]:
        payload = _payload(None)
        return {
            "block": None,
            "reason": reason,
            "preview": payload["image"],
            "width": payload["width"],
            "height": payload["height"],
        }

    if not prepared["valid"][py, px]:
        return _empty("该像素属于纯黑无效区域（已自动排除），请点击其他位置")
    if state["ignore"][py, px] > 0:
        return _empty("该色块已被标记为忽略，可在纠正列表中点 × 撤销")

    block_id = int(block_map[py, px])
    block_info = (prepared["block_stats"].get("blocks") or {}).get(block_id)
    if block_id <= 0 or block_info is None:
        return _empty("该像素属于过小的色块碎片，请点击相邻的较大色块")

    mask = _block_mask(block_map, block_id)
    # 预测用「有效区域全图」的分类结果，保证被判为忽略的色块也能看到当前阈值下会判成什么
    display_masks = processor.classify_hsv(prepared["hsv"], prepared["valid_mask"],
                                           state["ranges"])
    predicted = _predicted_class(display_masks, mask)
    existing = next((c for c in state["corrections"] if c["block_id"] == block_id), None)
    label = str(existing["label"]) if existing else None

    total_pixels = max(1, height * width)
    payload = _payload({"block_id": block_id})

    return {
        "block": {
            "block_id": block_id,
            "pixel_count": int(block_info["pixel_count"]),
            "pixel_ratio": round(int(block_info["pixel_count"]) / float(total_pixels), 6),
            "bbox": list(block_info["bbox"]),
            "mean_hsv": list(block_info["mean_hsv"]),
            "mean_rgb": list(block_info["mean_rgb"]),
            "predicted": predicted,
            "predicted_label": CLASS_LABELS.get(predicted) if predicted else None,
            "label": label,
            "label_label": LABELS.get(label) if label else None,
        },
        "preview": payload["image"],
        "width": payload["width"],
        "height": payload["height"],
    }


def set_correction(image_id: str, block_id: Any, label: Any,
                   auto_optimize: bool = True,
                   ranges: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    保存 / 取消某个色块的纠正。

    label 为 None → 取消该色块的纠正；否则必须是 CLOUD / BLUE_WATER /
    YELLOW_WATER / IGNORE。

    ranges 可选：页面当前表单里的（可能尚未保存的）阈值。传入时预览与自动优化都以
    它为起点，保证「用户正在看的参数」与「纠正后的建议」连贯；缺省用已保存阈值。
    """
    _check_image_id(image_id)
    prepared = _get_prepared(image_id)
    block_id_int = _coerce_block_id(block_id)
    if block_id_int not in (prepared["block_stats"].get("blocks") or {}):
        raise LabValidationError(f"色块 #{block_id_int} 不存在（图片可能已被替换）")

    if label is None:
        normalized_label = None
    elif isinstance(label, str) and label in LABELS:
        normalized_label = label
    else:
        allowed = "、".join(f"{key}({value})" for key, value in LABELS.items())
        raise LabValidationError(f"label 必须是 null 或 {allowed}，实际为 {label!r}")

    corrections = _read_corrections(image_id)
    filtered = [item for item in corrections if int(item["block_id"]) != block_id_int]
    if normalized_label is not None:
        filtered.append({
            "block_id": block_id_int,
            "label": normalized_label,
            "created_at": _iso(),
        })
    _write_corrections(image_id, filtered)

    state = _render(image_id, ranges, selection=None, show_corrections=True)
    payload = state["payload"]
    optimize_result = None
    if auto_optimize:
        # 以页面正在预览的阈值（可能尚未保存）为优化起点，保持交互连贯
        optimize_result = optimize_ranges(state["ranges"])

    return {
        "corrections": _corrections_rich(state["prepared"], state["corrections"],
                                         state["class_masks"]),
        "preview": payload["image"],
        "width": payload["width"],
        "height": payload["height"],
        "metrics": payload["metrics"],
        "analysis": payload["analysis"],
        "correction_accuracy": payload["analysis"]["correction_accuracy"],
        "optimize": optimize_result,
    }


def clear_corrections(image_id: Optional[str] = None,
                      ranges: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """清空某张图（或全部图片）的纠正；传 ranges 时按该阈值渲染清空后的预览。"""
    if image_id is not None:
        targets = [_check_image_id(image_id)]
        if not _image_dir(targets[0]).is_dir():
            raise LabNotFoundError(f"图片 {image_id} 不存在")
    else:
        targets = _iter_image_ids()

    cleared = 0
    for target in targets:
        corrections = _read_corrections(target)
        cleared += len(corrections)
        if corrections:
            _write_corrections(target, [])

    total = 0
    for target in _iter_image_ids():
        total += len(_read_corrections(target))
    print(f"[ColorLab] 已清空 {cleared} 条纠正（剩余 {total} 条）")
    result: Dict[str, Any] = {"cleared": cleared, "corrections_total": total}

    # 指定图片时顺带返回清空后的预览，页面无需再单独发一次 /preview
    if image_id is not None:
        payload = render_preview(image_id, ranges, None, True)
        result.update({
            "preview": payload["image"],
            "width": payload["width"],
            "height": payload["height"],
            "metrics": payload["metrics"],
            "analysis": payload["analysis"],
            "corrections": payload["corrections"],
            "correction_accuracy": payload["analysis"]["correction_accuracy"],
        })
    return result


# ---------------------------------------------------------------------------
#  自动优化 HSV 阈值
# ---------------------------------------------------------------------------

def _solution_from_ranges(ranges: Dict[str, Any]) -> Dict[str, int]:
    """把阈值字典摊平成坐标下降用的可调参数。"""
    cloud = ranges["CLOUD"]
    blue = ranges["BLUE_WATER"]
    return {
        "cl_h0": int(cloud["lower"][0]), "cl_h1": int(cloud["upper"][0]),
        "cl_s0": int(cloud["lower"][1]), "cl_s": int(cloud["upper"][1]),
        "cl_v0": int(cloud["lower"][2]), "cl_v1": int(cloud["upper"][2]),
        "bl_h0": int(blue["lower"][0]), "bl_h1": int(blue["upper"][0]),
        "bl_s0": int(blue["lower"][1]), "bl_s1": int(blue["upper"][1]),
        "bl_v0": int(blue["lower"][2]), "bl_v1": int(blue["upper"][2]),
    }


def _ranges_from_solution(solution: Dict[str, int],
                          base: Dict[str, Any]) -> Dict[str, Any]:
    ranges = {
        "CLOUD": {
            "lower": [solution["cl_h0"], solution["cl_s0"], solution["cl_v0"]],
            "upper": [solution["cl_h1"], solution["cl_s"], solution["cl_v1"]],
        },
        "BLUE_WATER": {
            "lower": [solution["bl_h0"], solution["bl_s0"], solution["bl_v0"]],
            "upper": [solution["bl_h1"], solution["bl_s1"], solution["bl_v1"]],
        },
    }
    try:
        return settings_store.validate_hsv_ranges(ranges)
    except settings_store.SettingsValidationError:
        return dict(base)


def _predict_classes(solution: Dict[str, int], h: np.ndarray, s: np.ndarray,
                     v: np.ndarray) -> np.ndarray:
    """按生产口径（云优先，其次蓝水，其余黄水）预测每个样本的类别索引。"""
    cloud = ((h >= solution["cl_h0"]) & (h <= solution["cl_h1"])
             & (s >= solution["cl_s0"]) & (s <= solution["cl_s"])
             & (v >= solution["cl_v0"]) & (v <= solution["cl_v1"]))
    blue = ((~cloud)
            & (h >= solution["bl_h0"]) & (h <= solution["bl_h1"])
            & (s >= solution["bl_s0"]) & (s <= solution["bl_s1"])
            & (v >= solution["bl_v0"]) & (v <= solution["bl_v1"]))
    predicted = np.full(h.shape, 2, dtype=np.int8)
    predicted[blue] = 1
    predicted[cloud] = 0
    return predicted


_PARAM_SPAN = {
    "cl_s": 255.0, "cl_v0": 255.0,
    "bl_h0": 179.0, "bl_h1": 179.0,
    "bl_s0": 255.0, "bl_s1": 255.0,
    "bl_v0": 255.0, "bl_v1": 255.0,
}


def _distance_penalty(solution: Dict[str, int], reference: Dict[str, int]) -> float:
    total = 0.0
    for key in _SEARCH_KEYS:
        total += abs(int(solution[key]) - int(reference[key])) / _PARAM_SPAN[key]
    return total / len(_SEARCH_KEYS)


def _split_accuracy(solution: Dict[str, int], part: Dict[str, np.ndarray]) -> Optional[float]:
    """一组样本上的分类准确率；样本为空时返回 None。"""
    target = part.get("y")
    if target is None or target.size == 0:
        return None
    predicted = _predict_classes(solution, part["h"], part["s"], part["v"])
    return float(np.mean(predicted == target))


def _blended_score(solution: Dict[str, int], samples: Dict[str, Any],
                   reference: Dict[str, int]) -> float:
    """
    优化目标 = 0.5 × 用户标注准确率 + 0.5 × 未标注保真准确率 - 距离惩罚。

    未标注样本的目标是「保持当前判定」，避免为了拟合少量标注把大片区域改错。
    """
    labeled_acc = _split_accuracy(solution, samples["labeled"]) or 0.0
    unlabeled_acc = _split_accuracy(solution, samples["unlabeled"])
    if unlabeled_acc is None:
        blended = labeled_acc
    else:
        blended = 0.5 * labeled_acc + 0.5 * unlabeled_acc
    return blended - _DISTANCE_PENALTY * _distance_penalty(solution, reference)


def _candidate_values(key: str, solution: Dict[str, int], fine: bool) -> List[int]:
    if fine:
        base = int(solution[key])
        values = [base + delta for delta in range(-_FINE_RADIUS, _FINE_RADIUS + 1)]
        if key in ("bl_h0", "bl_h1"):
            values = [value for value in values if 0 <= value <= 179]
        else:
            values = [value for value in values if 0 <= value <= 255]
        return sorted(set(values))
    return list(_COARSE_GRID[key])


def _valid_solution(solution: Dict[str, int]) -> bool:
    if solution["cl_s0"] > solution["cl_s"]:
        return False
    if solution["cl_v0"] > solution["cl_v1"]:
        return False
    if solution["bl_h0"] > solution["bl_h1"]:
        return False
    if solution["bl_s0"] > solution["bl_s1"]:
        return False
    if solution["bl_v0"] > solution["bl_v1"]:
        return False
    return True


def _coordinate_descent(solution: Dict[str, int], samples: Dict[str, Any],
                        reference: Dict[str, int]) -> Tuple[Dict[str, int], float]:
    """坐标下降搜索：逐参数扫描网格，取目标函数最优值；先粗后细。"""
    current = dict(solution)
    best_score = _blended_score(current, samples, reference)

    for sweep in range(_COARSE_SWEEPS + _FINE_SWEEPS):
        fine = sweep >= _COARSE_SWEEPS
        improved_any = False
        for key in _SEARCH_KEYS:
            best_value = current[key]
            local_score = best_score
            for value in _candidate_values(key, current, fine):
                if value == current[key]:
                    continue
                candidate = dict(current)
                candidate[key] = value
                if not _valid_solution(candidate):
                    continue
                score = _blended_score(candidate, samples, reference)
                if score > local_score + 1e-9:
                    local_score = score
                    best_value = value
            if best_value != current[key]:
                current[key] = best_value
                best_score = local_score
                improved_any = True
        if not improved_any and fine:
            break
    return current, best_score


def _collect_training_samples(current_ranges: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    汇总标注样本：

    - **用户标注**：有纠正（CLOUD/BLUE_WATER/YELLOW_WATER）的色块像素，目标 = 用户类别；
    - **未标注保真样本**：同一张图里没有纠正的像素，目标 = 当前阈值下的判定。

    为什么要保留未标注样本：只按用户标注做优化，很容易把阈值推到极端、把没标注的
    大片区域改错（例如把蓝水全并进黄水）。把「未标注区域保持当前判定」按 50% 权重
    计入目标函数，可以只修用户指出的错误，避免大面积改坏。

    Returns:
        {"labeled": {...}, "unlabeled": {...}, "class_counts", "images_used",
         "corrections_used"}; 没有任何用户标注时返回 None。
    """
    rng = np.random.default_rng(20261029)
    per_block_limit = max(1, int(config.TOOL_OPTIMIZE_MAX_SAMPLES_PER_BLOCK))
    unlabeled_per_image = 15000
    labeled_hsv: List[np.ndarray] = []
    labeled_y: List[np.ndarray] = []
    unlabeled_hsv: List[np.ndarray] = []
    unlabeled_y: List[np.ndarray] = []
    class_counts = {key: 0 for key in CLASS_ORDER}
    images_used = 0
    corrections_used = 0

    for image_id in _iter_image_ids():
        corrections = _read_corrections(image_id)
        samples = [item for item in corrections if item.get("label") in CLASS_LABELS]
        if not samples:
            continue
        try:
            prepared = _get_prepared(image_id)
        except LabError:
            continue
        images_used += 1
        flat_hsv = prepared["hsv"].reshape(-1, 3)
        flat_map = prepared["block_map"].reshape(-1)
        flat_valid = prepared["valid"].reshape(-1)

        labeled_ids = set()
        excluded_ids = set()
        for corr in corrections:
            block_id = int(corr["block_id"])
            if corr.get("label") in CLASS_LABELS:
                labeled_ids.add(block_id)
            elif corr.get("label") == IGNORE_LABEL:
                excluded_ids.add(block_id)

        # 1) 用户标注样本
        for corr in samples:
            indices = np.flatnonzero(flat_map == int(corr["block_id"]))
            if indices.size == 0:
                continue
            if indices.size > per_block_limit:
                indices = rng.choice(indices, size=per_block_limit, replace=False)
            labeled_hsv.append(flat_hsv[indices])
            index = CLASS_INDEX[str(corr["label"])]
            labeled_y.append(np.full(indices.size, index, dtype=np.int8))
            class_counts[str(corr["label"])] += int(indices.size)
            corrections_used += 1

        # 2) 未标注保真样本（排除已标注 / 已忽略 / 无效像素）
        keep = flat_valid.copy()
        for block_id in labeled_ids | excluded_ids:
            keep &= flat_map != block_id
        positions = np.flatnonzero(keep)
        if positions.size == 0:
            continue
        if positions.size > unlabeled_per_image:
            positions = rng.choice(positions, size=unlabeled_per_image, replace=False)
        current_masks = processor.classify_hsv(
            prepared["hsv"], prepared["valid_mask"], current_ranges
        )
        current_y = np.full(positions.size, 2, dtype=np.int8)
        current_y[current_masks["CLOUD"].reshape(-1)[positions] > 0] = 0
        current_y[current_masks["BLUE_WATER"].reshape(-1)[positions] > 0] = 1
        unlabeled_hsv.append(flat_hsv[positions])
        unlabeled_y.append(current_y)

    if not labeled_hsv:
        return None

    def _stack(chunks: List[np.ndarray], targets: List[np.ndarray],
               max_samples: int) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        if not chunks:
            return None, None
        hsv_data = np.concatenate(chunks, axis=0)
        target = np.concatenate(targets)
        if hsv_data.shape[0] > max_samples:
            picked = rng.choice(hsv_data.shape[0], size=max_samples, replace=False)
            hsv_data = hsv_data[picked]
            target = target[picked]
        return hsv_data, target

    max_samples = max(1, int(config.TOOL_OPTIMIZE_MAX_SAMPLES))
    labeled_data, labeled_target = _stack(labeled_hsv, labeled_y, max_samples)
    unlabeled_data, unlabeled_target = _stack(unlabeled_hsv, unlabeled_y, max_samples)

    # 截断后重新统计类别像素数，保证展示与实际参与优化的一致
    if labeled_target is not None:
        class_counts = {
            key: int(np.count_nonzero(labeled_target == CLASS_INDEX[key]))
            for key in CLASS_ORDER
        }

    def _split(data: Optional[np.ndarray], target: Optional[np.ndarray]) -> Dict[str, np.ndarray]:
        if data is None or target is None:
            return {
                "h": np.zeros(0, np.int16), "s": np.zeros(0, np.int16),
                "v": np.zeros(0, np.int16), "y": np.zeros(0, np.int8),
            }
        return {
            "h": data[:, 0].astype(np.int16),
            "s": data[:, 1].astype(np.int16),
            "v": data[:, 2].astype(np.int16),
            "y": target,
        }

    return {
        "labeled": _split(labeled_data, labeled_target),
        "unlabeled": _split(unlabeled_data, unlabeled_target),
        "class_counts": class_counts,
        "images_used": images_used,
        "corrections_used": corrections_used,
    }


def _empty_optimize_result(message: str, current_ranges: Dict[str, Any],
                           images_used: int = 0,
                           corrections_used: int = 0) -> Dict[str, Any]:
    return {
        "ok": False,
        "improved": False,
        "accuracy_before": None,
        "accuracy_after": None,
        "samples": 0,
        "class_counts": {},
        "images_used": images_used,
        "corrections_used": corrections_used,
        "suggested": current_ranges,
        "diffs": [],
        "message": message,
    }


def optimize_ranges(
    base_ranges: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    自动搜索更优的 HSV 阈值（只返回建议，不落库）。

    Args:
        base_ranges: 可选，优化起点 / 对照基准。页面传入当前表单里的（可能未保存的）
            阈值时，建议值会相对它给出；缺省用数据库中的当前生效阈值。
    """
    current_ranges = _resolve_ranges(base_ranges)
    samples = _collect_training_samples(current_ranges)
    if samples is None:
        return _empty_optimize_result(
            "尚无标注样本：请先在分类结果图上点击色块并指定真实类别", current_ranges
        )

    class_counts = samples["class_counts"]
    labeled_classes = [key for key, count in class_counts.items() if count > 0]
    if len(labeled_classes) < 2:
        only = "、".join(CLASS_LABELS[key] for key in labeled_classes) or "无"
        return _empty_optimize_result(
            f"至少需要标注两种类别（当前只有 {only}），否则无法形成有效约束",
            current_ranges,
            samples["images_used"],
            samples["corrections_used"],
        )

    labeled_total = int(samples["labeled"]["y"].size)
    unlabeled_total = int(samples["unlabeled"]["y"].size)
    if labeled_total < int(config.TOOL_OPTIMIZE_MIN_SAMPLES):
        return _empty_optimize_result(
            f"有效标注像素过少（{labeled_total}），请多标注几个色块再优化",
            current_ranges,
            samples["images_used"],
            samples["corrections_used"],
        )

    reference = _solution_from_ranges(current_ranges)
    accuracy_before = _split_accuracy(reference, samples["labeled"])
    accuracy_before = 0.0 if accuracy_before is None else accuracy_before
    unlabeled_before = _split_accuracy(reference, samples["unlabeled"])

    best_solution, _ = _coordinate_descent(reference, samples, reference)
    accuracy_after = _split_accuracy(best_solution, samples["labeled"])
    accuracy_after = 0.0 if accuracy_after is None else accuracy_after
    unlabeled_after = _split_accuracy(best_solution, samples["unlabeled"])

    suggested = _ranges_from_solution(best_solution, current_ranges)
    diffs: List[Dict[str, Any]] = []
    for class_key in ("CLOUD", "BLUE_WATER"):
        for side in ("lower", "upper"):
            for index, channel in enumerate("HSV"):
                old_value = int(current_ranges[class_key][side][index])
                new_value = int(suggested[class_key][side][index])
                if old_value != new_value:
                    diffs.append({
                        "class": class_key,
                        "class_label": CLASS_LABELS.get(class_key, class_key),
                        "side": side,
                        "channel": channel,
                        "from": old_value,
                        "to": new_value,
                    })

    improved = accuracy_after - accuracy_before > 0.005
    if improved:
        message = (f"已根据 {samples['corrections_used']} 处纠正优化参数，"
                   f"标注像素准确率 {accuracy_before:.1%} → {accuracy_after:.1%}")
        if unlabeled_before is not None and unlabeled_after is not None:
            message += (f"；未标注区域保真度 {unlabeled_before:.1%} → "
                        f"{unlabeled_after:.1%}")
    else:
        # 提升不明显时不建议改动参数：直接回填当前值，避免页面展示一堆无意义的微调
        suggested = current_ranges
        diffs = []
        message = (f"当前参数已能较好满足标注（准确率 {accuracy_before:.1%}），"
                   f"未做明显调整")

    result = {
        "ok": True,
        "improved": improved,
        "accuracy_before": round(accuracy_before, 4),
        "accuracy_after": round(accuracy_after, 4),
        "samples": labeled_total,
        "samples_unlabeled": unlabeled_total,
        "class_counts": class_counts,
        "images_used": samples["images_used"],
        "corrections_used": samples["corrections_used"],
        "suggested": suggested,
        "diffs": diffs,
        "message": message,
    }
    if unlabeled_before is not None and unlabeled_after is not None:
        result["unlabeled_accuracy_before"] = round(unlabeled_before, 4)
        result["unlabeled_accuracy_after"] = round(unlabeled_after, 4)
    return result