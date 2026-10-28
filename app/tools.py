# app/tools.py

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from PIL import Image
from starlette.concurrency import run_in_threadpool

# 假设主应用中已经定义了 templates
# 如果没有，您需要在 main.py 中进行配置
templates = Jinja2Templates(directory="templates")

# 导入新的、独立的 pipeline 模块
from .pipeline import process_image_pipeline
from . import color_lab, settings_store

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


class LabPreviewRequest(BaseModel):
    image_id: str
    ranges: Optional[Dict[str, Any]] = None
    selection: Optional[Dict[str, Any]] = None
    show_corrections: bool = True


class LabBlockRequest(BaseModel):
    image_id: str
    x: float
    y: float
    ranges: Optional[Dict[str, Any]] = None


class LabCorrectionRequest(BaseModel):
    image_id: str
    block_id: int
    label: Optional[str] = None
    auto_optimize: bool = True
    # 可选：页面当前表单里的阈值（可能尚未保存），作为预览与自动优化的起点
    ranges: Optional[Dict[str, Any]] = None


class LabOptimizeRequest(BaseModel):
    image_id: Optional[str] = None
    # 可选：优化起点 / 对照基准，缺省用当前生效阈值
    ranges: Optional[Dict[str, Any]] = None


class LabClearCorrectionsRequest(BaseModel):
    image_id: Optional[str] = None
    # 可选：页面当前表单里的阈值，用于渲染清空后的预览
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


# =================================================================
#  颜色阈值智能调优实验台（导入卫星图片 → 点击色块纠正 → 自动优化 HSV）
#  页面：/tools/color_thresholds    设计/接口说明：docs/color-thresholds-lab.md
# =================================================================

def _lab_error_response(exc: Exception) -> JSONResponse:
    """把实验台的业务异常映射成 {success:false, error, details?} 的 HTTP 响应。"""
    status = getattr(exc, "status_code", 400)
    content: Dict[str, Any] = {"success": False, "error": str(exc)}
    if isinstance(exc, settings_store.SettingsValidationError):
        content["error"] = "HSV 阈值不合法"
        content["details"] = exc.errors
    return JSONResponse(status_code=int(status), content=content)


@router.get("/api/color_thresholds/lab")
def api_lab_state():
    """实验台初始状态：已导入图片列表、上传限制、可用的纠正类别。"""
    try:
        data = color_lab.get_lab_state()
    except Exception as exc:  # noqa: BLE001
        return _lab_error_response(exc)
    return {"success": True, "data": data}


@router.post("/api/color_thresholds/images")
async def api_lab_import_image(request: Request, filename: str = ""):
    """
    导入图片：请求体是原始文件二进制（单张图片或一个 ZIP），filename 从查询参数传入。

    刻意不走 multipart 表单，避免引入 python-multipart 依赖；ZIP 内的条目逐个导入。
    """
    raw = await request.body()
    try:
        data = await run_in_threadpool(color_lab.import_upload, raw, filename or "upload.png")
    except Exception as exc:  # noqa: BLE001
        return _lab_error_response(exc)
    return {"success": True, "data": data}


@router.delete("/api/color_thresholds/images/{image_id}")
def api_lab_delete_image(image_id: str):
    """删除一张已导入图片（含其纠正数据）。"""
    try:
        data = color_lab.delete_image(image_id)
    except Exception as exc:  # noqa: BLE001
        return _lab_error_response(exc)
    return {"success": True, "data": data}


@router.post("/api/color_thresholds/images/clear")
def api_lab_clear_images():
    """清空全部已导入图片。"""
    try:
        data = color_lab.clear_images()
    except Exception as exc:  # noqa: BLE001
        return _lab_error_response(exc)
    return {"success": True, "data": data}


@router.post("/api/color_thresholds/preview")
def api_lab_preview(payload: LabPreviewRequest):
    """
    用给定阈值（缺省 = 当前生效阈值）渲染某张导入图片的分类结果与指标。

    ranges 只用于预览，不会保存；保存阈值请用 POST /api/color_thresholds。
    """
    try:
        data = color_lab.render_preview(
            payload.image_id,
            payload.ranges,
            payload.selection,
            payload.show_corrections,
        )
    except Exception as exc:  # noqa: BLE001
        return _lab_error_response(exc)
    return {"success": True, "data": data}


@router.post("/api/color_thresholds/block")
def api_lab_pick_block(payload: LabBlockRequest):
    """点击色块：x/y 为相对预览图的归一化坐标（0~1），返回整块色块信息与高亮预览。"""
    try:
        data = color_lab.pick_block(payload.image_id, payload.x, payload.y, payload.ranges)
    except Exception as exc:  # noqa: BLE001
        return _lab_error_response(exc)
    return {"success": True, "data": data}


@router.post("/api/color_thresholds/corrections")
def api_lab_set_correction(payload: LabCorrectionRequest):
    """
    保存 / 取消某个色块的纠正（label=null 表示取消）。

    auto_optimize=true 时会顺带跑一次自动优化，把建议参数放在返回的 optimize 字段里；
    优化结果不会落库，需要页面另行「保存并立即生效」。
    """
    try:
        data = color_lab.set_correction(
            payload.image_id,
            payload.block_id,
            payload.label,
            payload.auto_optimize,
            payload.ranges,
        )
    except Exception as exc:  # noqa: BLE001
        return _lab_error_response(exc)
    return {"success": True, "data": data}


@router.post("/api/color_thresholds/corrections/clear")
def api_lab_clear_corrections(payload: LabClearCorrectionsRequest):
    """清空某张图（传 image_id）或全部图片的纠正。"""
    try:
        data = color_lab.clear_corrections(payload.image_id, payload.ranges)
    except Exception as exc:  # noqa: BLE001
        return _lab_error_response(exc)
    return {"success": True, "data": data}


@router.post("/api/color_thresholds/optimize")
def api_lab_optimize(payload: LabOptimizeRequest):
    """根据全部图片上的纠正样本自动搜索更优的 HSV 阈值（只返回建议，不落库）。"""
    try:
        optimize = color_lab.optimize_ranges(payload.ranges)
    except Exception as exc:  # noqa: BLE001
        return _lab_error_response(exc)
    return {"success": True, "data": {
        "optimize": optimize,
        "images_used": optimize.get("images_used", 0),
        "corrections_used": optimize.get("corrections_used", 0),
    }}