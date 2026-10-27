# app/tools.py

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

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
from . import settings_store, threshold_preview

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
# 线程池执行，避免阻塞事件循环（尤其是首次预览 test_images 时的一次性超分）。
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
    """列出实时预览可用的图像源（最新分析结果优先，其次 test_images 样本）。"""
    return {"success": True, "data": threshold_preview.list_sources()}


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