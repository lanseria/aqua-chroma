# app/tools.py

import time
from pathlib import Path
from typing import Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from PIL import Image

# 假设主应用中已经定义了 templates
# 如果没有，您需要在 main.py 中进行配置
templates = Jinja2Templates(directory="templates")

# 导入新的、独立的 pipeline 模块
from .pipeline import process_image_pipeline
from . import config

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


@router.get("/hsv_tuner", response_class=HTMLResponse)
async def get_hsv_tuner_page(request: Request):
    """
    提供 HSV 参数实时调试工具的 HTML 页面。
    """
    initial_hsv_ranges = config.COLOR_CLASSIFICATION_HSV_RANGES
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