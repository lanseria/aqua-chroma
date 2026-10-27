# app/settings_store.py
"""
运行时应用设置存储（当前用于 HSV 颜色阈值）。

设计目标
--------
HSV 颜色阈值不再是写死在 config.py / .env 里的常量，而是持久化到 PostgreSQL
的 app_settings 表（key = "hsv_color_ranges"，value 为 JSON 文本），
可以在网页 /tools/color_thresholds 上实时调整，保存后下一次分析立即生效。

读取策略
--------
- 进程内缓存（TTL 约 1 秒）：分析任务每 10 分钟跑一次，1 秒缓存的代价可以忽略，
  但能保证同一批处理中阈值一致、且不产生大量数据库查询；
- 保存/重置时立即失效缓存，因此页面上点保存后马上生效，无需重启；
- 数据库读取失败时打印警告并回退到 config.DEFAULT_COLOR_CLASSIFICATION_HSV_RANGES，
  保证分析主流程永不因设置读取失败而中断；
- 数据库写入失败则抛出 RuntimeError，由 API 层转成明确的 500 错误。

校验
----
validate_hsv_ranges 会收集所有错误（而不是遇到第一个就返回），
错误信息为中文，供前端逐条展示。
"""

import copy
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from . import config, crud
from .database import SessionLocal

# app_settings 表中存放 HSV 阈值的 key
HSV_RANGES_KEY = "hsv_color_ranges"
# 兼容别名（部分验证脚本以 HSV_SETTING_KEY 命名引用同一个 key）
HSV_SETTING_KEY = HSV_RANGES_KEY

# 进程内缓存有效期（秒）
CACHE_TTL_SECONDS = 1.0

# 分类与通道的合法范围（OpenCV HSV：H:0~179, S/V:0~255）
HSV_CHANNEL_BOUNDS = {"H": (0, 179), "S": (0, 255), "V": (0, 255)}
REQUIRED_CLASSES = ("CLOUD", "BLUE_WATER")
_CHANNEL_NAMES = ("H", "S", "V")

# 面向接口/页面的元信息（GET /tools/api/color_thresholds 的 bounds、classes）
HSV_BOUNDS = {"H": [0, 179], "S": [0, 255], "V": [0, 255]}
HSV_CLASSES = list(REQUIRED_CLASSES)

# 设置时间展示时区（与系统配置一致）
_DISPLAY_TZ = ZoneInfo(config.TIME_ZONE)


class SettingsValidationError(ValueError):
    """HSV 阈值校验失败。errors 保存全部逐条中文错误。"""

    def __init__(self, errors: List[str], message: str = "HSV 阈值不合法"):
        self.errors: List[str] = list(errors)
        self.message = message
        super().__init__(message + "：" + "；".join(self.errors))


# --- 校验 ---

def _validate_channel_values(class_key: str, side: str, values: Any,
                             errors: List[str]) -> Optional[List[int]]:
    """
    校验单个 lower/upper 列表：必须是长度为 3 的整数数组，且在通道范围内。
    校验通过返回规范化后的 int 列表，否则把错误追加到 errors 并返回 None。
    """
    if not isinstance(values, (list, tuple)):
        errors.append(f"{class_key}.{side} 必须是长度为 3 的整数数组")
        return None
    if len(values) != 3:
        errors.append(
            f"{class_key}.{side} 必须包含 3 个通道值（H、S、V），实际为 {len(values)} 个"
        )
        return None

    normalized: List[int] = []
    ok = True
    for idx, channel in enumerate(_CHANNEL_NAMES):
        value = values[idx]
        # bool 是 int 的子类，需要显式排除
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(
                f"{class_key}.{side}[{idx}]（{channel} 通道）必须是整数，实际为 {value!r}"
            )
            ok = False
            continue
        low, high = HSV_CHANNEL_BOUNDS[channel]
        if value < low or value > high:
            errors.append(
                f"{class_key}.{side}[{idx}]（{channel} 通道）超出范围 {low}~{high}，实际为 {value}"
            )
            ok = False
            continue
        normalized.append(value)

    return normalized if ok else None


def validate_hsv_ranges(ranges: Any) -> Dict[str, Any]:
    """
    校验并规范化 HSV 阈值，返回深拷贝后的纯字典。

    合法结构（顶层必须且只能包含 CLOUD、BLUE_WATER）：
        {"CLOUD": {"lower": [H,S,V], "upper": [H,S,V]},
         "BLUE_WATER": {"lower": [H,S,V], "upper": [H,S,V]}}

    任何不合法都抛出 SettingsValidationError，其 errors 属性包含全部中文错误。
    """
    errors: List[str] = []

    if not isinstance(ranges, dict):
        raise SettingsValidationError(
            ["HSV 阈值必须是对象，且包含 CLOUD 与 BLUE_WATER 两个分类"]
        )

    keys = set(ranges.keys())
    required = set(REQUIRED_CLASSES)
    for class_key in sorted(required - keys):
        errors.append(f"缺少分类 {class_key}")
    for class_key in sorted(keys - required):
        errors.append(
            f"未知分类 {class_key}（仅支持 {'、'.join(REQUIRED_CLASSES)}）"
        )

    normalized: Dict[str, Any] = {}
    for class_key in REQUIRED_CLASSES:
        if class_key not in ranges:
            continue
        section = ranges[class_key]
        if not isinstance(section, dict):
            errors.append(f"{class_key} 必须是包含 lower 与 upper 的对象")
            continue

        lower = _validate_channel_values(class_key, "lower", section.get("lower"), errors)
        upper = _validate_channel_values(class_key, "upper", section.get("upper"), errors)

        # 仅在两侧都合法时比较下界/上界，避免重复报错
        if lower is not None and upper is not None:
            for idx, channel in enumerate(_CHANNEL_NAMES):
                if lower[idx] > upper[idx]:
                    errors.append(
                        f"{class_key} 的 {channel} 通道下界 {lower[idx]} 不能大于上界 {upper[idx]}"
                    )

        normalized[class_key] = {"lower": lower, "upper": upper}

    if errors:
        raise SettingsValidationError(errors)

    # 深拷贝，避免调用方后续修改影响缓存内容
    return copy.deepcopy(normalized)


# --- 缓存与数据库读写 ---

# 缓存结构：{"ranges": dict, "source": "db"|"default", "updated_at": str|None,
#           "fetched_at": time.monotonic()}
_cache_lock = threading.RLock()
_cache: Optional[Dict[str, Any]] = None


def _default_ranges() -> Dict[str, Any]:
    """兜底默认阈值（深拷贝，防止被调用方原地修改）。"""
    return copy.deepcopy(config.DEFAULT_COLOR_CLASSIFICATION_HSV_RANGES)


def _to_display_iso(dt: Optional[datetime]) -> Optional[str]:
    """把数据库里的 updated_at 转成 Asia/Shanghai 时区的 ISO8601 字符串。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        # 理论上 PostgreSQL 的 TIMESTAMPTZ 一定带时区；若拿到 naive 值按 UTC 处理
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_DISPLAY_TZ).isoformat()


def _load_from_db() -> Dict[str, Any]:
    """
    从数据库读取 HSV 阈值。

    返回 {"ranges": dict, "source": "db"|"default", "updated_at": str|None}；
    任何异常（连不上库、表不存在、JSON 损坏、内容校验失败）都回退默认值，
    并打印警告，绝不向上抛异常。
    """
    row = None
    db = None
    try:
        db = SessionLocal()
        row = crud.get_app_setting(db, HSV_RANGES_KEY)
    except Exception as exc:  # 数据库不可用 / 表未迁移 / 会话创建失败
        print(f"[Settings] 读取 HSV 阈值失败，回退默认值: {exc}")
        return {"ranges": _default_ranges(), "source": "default", "updated_at": None}
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass

    if row is None:
        return {"ranges": _default_ranges(), "source": "default", "updated_at": None}

    updated_at = _to_display_iso(row.updated_at)
    try:
        parsed = json.loads(row.value)
    except (TypeError, ValueError) as exc:
        print(f"[Settings] 数据库中的 HSV 阈值不是合法 JSON，回退默认值: {exc}")
        return {"ranges": _default_ranges(), "source": "default", "updated_at": updated_at}

    try:
        ranges = validate_hsv_ranges(parsed)
    except SettingsValidationError as exc:
        print(f"[Settings] 数据库中的 HSV 阈值不合法，回退默认值: {exc.errors}")
        return {"ranges": _default_ranges(), "source": "default", "updated_at": updated_at}

    return {"ranges": ranges, "source": "db", "updated_at": updated_at}


def _get_effective() -> Dict[str, Any]:
    """返回带缓存的有效设置（TTL 内直接命中，避免每次分析都查库）。"""
    global _cache

    now = time.monotonic()
    with _cache_lock:
        if _cache is not None and now - _cache["fetched_at"] < CACHE_TTL_SECONDS:
            return _cache

    # 缓存过期：在锁外读库，避免数据库慢时阻塞其他请求
    fresh = _load_from_db()
    fresh["fetched_at"] = time.monotonic()

    with _cache_lock:
        _cache = fresh
        return _cache


def _invalidate_cache() -> None:
    """立即失效进程内缓存（保存/重置后调用，保证下一次读取拿到最新值）。"""
    global _cache
    with _cache_lock:
        _cache = None


# --- 对外接口 ---

def get_hsv_ranges() -> Dict[str, Any]:
    """
    获取当前生效的 HSV 阈值（数据库有效值，否则 config 兜底默认值）。

    返回深拷贝，调用方可以安全修改，不会污染缓存。
    """
    return copy.deepcopy(_get_effective()["ranges"])


def get_state() -> Dict[str, Any]:
    """
    获取完整设置状态，供页面与 API 使用：

        {
          "ranges":    当前生效阈值,
          "defaults":  兜底默认阈值,
          "source":    "db" | "default",
          "updated_at": ISO8601 字符串（Asia/Shanghai）| None,
          "bounds":    {"H": [0,179], "S": [0,255], "V": [0,255]},
          "classes":   ["CLOUD", "BLUE_WATER"]
        }
    """
    effective = _get_effective()
    return {
        "ranges": copy.deepcopy(effective["ranges"]),
        "defaults": _default_ranges(),
        "source": effective["source"],
        "updated_at": effective["updated_at"],
        "bounds": copy.deepcopy(HSV_BOUNDS),
        "classes": list(HSV_CLASSES),
    }


def update_hsv_ranges(ranges: Dict[str, Any]) -> Dict[str, Any]:
    """
    校验并持久化新的 HSV 阈值，随后立即失效缓存并返回最新状态。
    校验失败抛 SettingsValidationError；数据库写入失败抛 RuntimeError。
    """
    normalized = validate_hsv_ranges(ranges)
    value_json = json.dumps(normalized, ensure_ascii=False)

    db = None
    try:
        db = SessionLocal()
        crud.upsert_app_setting(db, HSV_RANGES_KEY, value_json)
    except Exception as exc:
        raise RuntimeError(f"保存 HSV 阈值到数据库失败: {exc}") from exc
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass

    _invalidate_cache()
    print(f"[Settings] HSV 阈值已更新并立即生效: {normalized}")
    return get_state()


def reset_hsv_ranges() -> Dict[str, Any]:
    """
    删除数据库中的 HSV 阈值，恢复为 config 兜底默认值。
    数据库写入失败抛 RuntimeError。
    """
    db = None
    try:
        db = SessionLocal()
        crud.delete_app_setting(db, HSV_RANGES_KEY)
    except Exception as exc:
        raise RuntimeError(f"重置 HSV 阈值失败: {exc}") from exc
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass

    _invalidate_cache()
    print("[Settings] HSV 阈值已重置为默认值")
    return get_state()