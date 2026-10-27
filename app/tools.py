# app/tools.py

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from PIL import Image

# 假设主应用中已经定义了 templates
# 如果没有，您需要在 main.py 中进行配置
templates = Jinja2Templates(directory="templates")

# 导入新的、独立的 pipeline 模块
from .pipeline import process_image_pipeline
from . import config, crud, settings_store, threshold_preview
from .database import SessionLocal

# 定义测试图片和结果的目录
TEST_IMAGE_DIR = Path("test_images")
TEST_RESULT_DIR = Path("test_results/hsv_tuner_outputs")

# 确保目录存在
TEST_IMAGE_DIR.mkdir(exist_ok=True)
TEST_RESULT_DIR.mkdir(parents=True, exist_ok=True)

router = APIRouter(
    prefix="/tools",
    tags=["[Dev] Tools"],
)

# Pydantic 模型
class HsvProcessSingleRequest(BaseModel):
    image_name: str
    hsv_ranges: Dict[str, Dict[str, List[int]]]

class HsvProcessAllRequest(BaseModel):
    hsv_ranges: Dict[str, Dict[str, List[int]]]


class ColorThresholdsUpdateRequest(BaseModel):
    # 故意用 Dict[str, Any] 接收：结构问题由 settings_store 给出逐条中文错误（400），
    # 而不是 Pydantic 的 422，前端可以直接把 details 展示给用户。
    ranges: Dict[str, Any]


class ThresholdPreviewRequest(BaseModel):
    source: str
    ranges: Optional[Dict[str, Any]] = None


class PreviewTagsUpdateRequest(BaseModel):
    # 故意用 Any + 默认 None 接收：timestamp 非 int/bool、tags 非数组、字段缺失等
    # 结构问题也要转成我们的 400 + 中文 details，而不是 Pydantic 的 422
    #（若声明为 int / List[Any]，bool 会被强转、非数组/缺失会在进入校验前被拦截）。
    timestamp: Any = None
    tags: Any = None


# --- 预览源 tag 校验 ---

def _validate_preview_tags_payload(timestamp: Any, tags: Any) -> Tuple[List[str], List[str]]:
    """
    校验「保存某个分析结果时间点的 tag」请求。

    收集全部错误（不是遇到第一个就返回），错误信息为中文，供前端逐条展示。
    tags 去重时保留首次出现顺序。

    Returns:
        (规范化后的 tags, 错误列表)；错误列表非空时调用方返回 400。
    """
    errors: List[str] = []

    # 1. 时间戳：必须是 int（bool 不算），且该时间点有可用的分析结果图像
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        errors.append(f"timestamp 必须是整数（UNIX 秒），实际为 {timestamp!r}")
    else:
        if not threshold_preview.analysis_source_exists(timestamp):
            artifact = threshold_preview.analysis_artifact_path(timestamp)
            errors.append(
                f"时间戳 {timestamp} 没有可用的分析结果图像（{artifact.as_posix()} 不存在）"
            )

    # 2. tags：必须是 list，元素为 strip 后非空、长度受限的字符串
    normalized: List[str] = []
    if not isinstance(tags, list):
        errors.append(f"tags 必须是字符串数组（list），实际为 {type(tags).__name__}")
        return normalized, errors

    seen = set()
    for index, item in enumerate(tags):
        if not isinstance(item, str):
            errors.append(f"tags[{index}] 必须是字符串，实际为 {item!r}")
            continue
        tag = item.strip()
        if not tag:
            errors.append(f"tags[{index}] 不能为空字符串")
            continue
        if len(tag) > config.PREVIEW_TAG_MAX_LENGTH:
            errors.append(
                f"tags[{index}] 长度 {len(tag)} 超过上限 "
                f"{config.PREVIEW_TAG_MAX_LENGTH} 个字符：{tag}"
            )
            continue
        if tag in seen:
            continue  # 重复项静默去重，保留首次出现顺序
        seen.add(tag)
        normalized.append(tag)

    if len(normalized) > config.PREVIEW_TAG_MAX_COUNT:
        errors.append(
            f"单个时间点最多 {config.PREVIEW_TAG_MAX_COUNT} 个 tag"
            f"（去重后实际 {len(normalized)} 个）"
        )

    return normalized, errors


@router.get("/hsv_tuner", response_class=HTMLResponse)
async def get_hsv_tuner_page(request: Request):
    """
    提供 HSV 参数实时调试工具的 HTML 页面。
    """
    # 初始值取当前生效阈值（数据库优先，数据库无记录时回退 config 默认值）
    initial_hsv_ranges = settings_store.get_hsv_ranges()
    return templates.TemplateResponse("tools/hsv_tuner.html", {
        "request": request,
        "initial_hsv": initial_hsv_ranges
    })

@router.get("/api/test_images")
async def list_test_images():
    """
    获取 `test_images` 目录下的所有可用测试图片列表。
    """
    if not TEST_IMAGE_DIR.is_dir():
        return {"error": f"Directory not found: {TEST_IMAGE_DIR}"}
    
    images = [f.name for f in TEST_IMAGE_DIR.iterdir() if f.suffix.lower() in ('.png', '.jpg', '.jpeg')]
    return {"images": images}


@router.post("/api/reprocess_all_hsv")
async def reprocess_all_with_hsv(payload: HsvProcessAllRequest):
    """
    接收新的 HSV 参数，对 test_images 目录中的所有图片进行处理，并返回结果列表。
    """
    if not TEST_IMAGE_DIR.is_dir():
        return {"success": False, "error": f"Test image directory not found: {TEST_IMAGE_DIR}"}

    image_files = [f for f in TEST_IMAGE_DIR.iterdir() if f.suffix.lower() in ('.png', '.jpg', '.jpeg')]
    if not image_files:
        return {"success": False, "error": f"No images found in {TEST_IMAGE_DIR}"}
        
    # 为本次批量处理创建一个唯一的父目录
    run_timestamp = int(time.time() * 1000)
    batch_output_dir = TEST_RESULT_DIR / f"batch_{run_timestamp}"

    all_results = []

    for image_file in image_files:
        try:
            # 为每个图片创建独立的子目录
            output_dir = batch_output_dir / image_file.stem
            output_dir.mkdir(parents=True, exist_ok=True)
            
            input_image = Image.open(image_file).convert('RGB')
            
            analysis_result = process_image_pipeline(
                image=input_image,
                output_dir_path=output_dir,
                hsv_ranges_override=payload.hsv_ranges
            )
            
            base_web_path = f"/test_results/hsv_tuner_outputs/{batch_output_dir.name}/{image_file.stem}"
            
            all_results.append({
                "image_name": image_file.name,
                "analysis": analysis_result,
                "image_urls": {
                    "input_processed": f"{base_web_path}/01_input_processed.png",
                    "super_resolved": f"{base_web_path}/01b_superresolved.png",
                    "input_annotated": f"{base_web_path}/01_input_annotated.png",
                    "auto_balanced": f"{base_web_path}/02_auto_balanced.png",
                    "ocean_only": f"{base_web_path}/03_ocean_only.png",
                    "classification": f"{base_web_path}/04_hsv_classification.png"
                }
            })
        except Exception as e:
            all_results.append({
                "image_name": image_file.name,
                "error": str(e)
            })

    return {"success": True, "data": all_results}


# =================================================================
#  颜色阈值实时调优（阈值持久化在数据库，保存即生效）
#  页面：/tools/color_thresholds
# =================================================================

@router.get("/color_thresholds", response_class=HTMLResponse)
async def get_color_thresholds_page(request: Request):
    """
    颜色阈值实时调优页面。

    上下文 initial_state 与 GET /tools/api/color_thresholds 的 data 结构一致；
    页面也可以自己再拉一次接口，二者等价。
    """
    return templates.TemplateResponse("tools/color_thresholds.html", {
        "request": request,
        "initial_state": settings_store.get_state(),
    })


# 注意：以下接口内部可能涉及数据库读写或图像计算，用同步 def 让 FastAPI 放入
# 线程池执行，避免阻塞事件循环。
@router.get("/api/color_thresholds")
def api_get_color_thresholds():
    """读取当前生效的 HSV 阈值、兜底默认值、来源与可用通道范围。"""
    return {"success": True, "data": settings_store.get_state()}


@router.post("/api/color_thresholds")
def api_update_color_thresholds(payload: ColorThresholdsUpdateRequest):
    """
    保存新的 HSV 阈值。

    校验通过 -> 写入 app_settings 表并立即失效缓存（下一次分析即用新值），
    返回最新状态；校验不通过 -> 400 + 逐条中文错误。
    """
    try:
        state = settings_store.update_hsv_ranges(payload.ranges)
    except settings_store.SettingsValidationError as exc:
        return JSONResponse(status_code=400, content={
            "success": False,
            "error": "HSV 阈值不合法",
            "details": exc.errors,
        })
    except RuntimeError as exc:
        return JSONResponse(status_code=500, content={
            "success": False,
            "error": str(exc),
        })
    return {"success": True, "data": state}


@router.post("/api/color_thresholds/reset")
def api_reset_color_thresholds():
    """恢复默认 HSV 阈值：删除数据库记录，回退 config 兜底默认值。"""
    try:
        state = settings_store.reset_hsv_ranges()
    except RuntimeError as exc:
        return JSONResponse(status_code=500, content={
            "success": False,
            "error": str(exc),
        })
    return {"success": True, "data": state}


@router.get("/api/preview_sources")
def api_preview_sources():
    """
    列出全部可预览的分析结果时间点（时间倒序、含各自 tags），
    以及全库 tag 统计、页面录入候选、总数/截断标记。
    """
    return {"success": True, "data": threshold_preview.list_sources()}


@router.get("/api/preview_tags")
def api_preview_tags():
    """列出全库预览源 tag 的使用统计（打该 tag 的时间点数量）与页面录入候选。"""
    try:
        with SessionLocal() as db:
            counts = crud.list_preview_tag_counts(db)
    except Exception as exc:
        return JSONResponse(status_code=500, content={
            "success": False,
            "error": f"读取预览标签失败: {exc}",
        })

    return {"success": True, "data": {
        "tags": [{"tag": tag, "count": int(count)} for tag, count in counts],
        "presets": list(config.PREVIEW_TAG_PRESETS),
    }}


@router.post("/api/preview_tags")
def api_update_preview_tags(payload: PreviewTagsUpdateRequest):
    """
    替换式保存某个分析结果时间点的 tag（tags=[] 表示清空该时间点全部 tag）。

    校验不通过 -> 400 + 逐条中文 details；数据库写入失败 -> 500。
    """
    normalized, errors = _validate_preview_tags_payload(payload.timestamp, payload.tags)
    if errors:
        return JSONResponse(status_code=400, content={
            "success": False,
            "error": "预览标签不合法",
            "details": errors,
        })

    try:
        with SessionLocal() as db:
            saved = crud.set_preview_tags(db, payload.timestamp, normalized)
    except Exception as exc:
        return JSONResponse(status_code=500, content={
            "success": False,
            "error": f"保存预览标签失败: {exc}",
        })

    return {"success": True, "data": {
        "timestamp": payload.timestamp,
        "tags": saved,
    }}


@router.post("/api/threshold_preview")
def api_threshold_preview(payload: ThresholdPreviewRequest):
    """
    实时预览：对指定图像源用给定阈值（不传 ranges 则用当前生效值）执行
    分类 + 着色 + 指标计算，返回 base64 PNG 与指标；不写文件、不改配置。
    """
    try:
        data = threshold_preview.render_preview(payload.source, payload.ranges)
    except settings_store.SettingsValidationError as exc:
        # 注意：SettingsValidationError 是 ValueError 的子类，必须放在前面捕获
        return JSONResponse(status_code=400, content={
            "success": False,
            "error": "HSV 阈值不合法",
            "details": exc.errors,
        })
    except ValueError as exc:
        return JSONResponse(status_code=400, content={
            "success": False,
            "error": str(exc),
        })
    return {"success": True, "data": data}