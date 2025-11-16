# app/main.py
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
import numpy as np
import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image
from sqlalchemy.orm import Session

from . import config, crud, downloader, geo_utils, processor, schemas, tools, pipeline
from .database import SessionLocal, init_db

# =================================================================
#  API Response Structure Helpers
# =================================================================

def R_success(data: Any = None, msg: str = "Success"):
    """
    统一的成功响应格式。
    """
    return {"code": 200, "data": data, "msg": msg}

def R_fail(msg: str = "Fail", code: int = 500, data: Any = None):
    """
    统一的失败响应格式。
    """
    return JSONResponse(
        status_code=code, content={"code": code, "data": data, "msg": msg}
    )

# =================================================================
#  Database Dependency & Template Engine
# =================================================================

# 为调试工具提供Jinja2模板引擎
templates = Jinja2Templates(directory="templates")

def get_db():
    """
    FastAPI 依赖注入，为每个请求提供一个数据库会话。
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# =================================================================
#  Core Analysis Logic
# =================================================================

def _process_image_pipeline(image: Image.Image, output_dir_path: Path, hsv_ranges_override: Optional[Dict] = None) -> Dict[str, Any]:
    """
    接收一个PIL图像，执行完整的分析流程，并保存所有中间调试图。
    这是为测试用例和调试工具设计的核心可重用逻辑。

    Args:
        image: 输入的 PIL.Image.Image 对象。
        output_dir_path: 用于保存所有输出文件的 pathlib.Path 对象。
        hsv_ranges_override: 可选的HSV参数字典，用于覆盖默认配置。

    Returns:
        一个包含详细分析结果的字典。
    """
    # 确保输出目录存在
    output_dir_path.mkdir(parents=True, exist_ok=True)
    
    analysis_result = {}
    try:
        # --- 步骤 1: 根据配置放大图像 (预处理) ---
        scale_factor = config.PRE_ANALYSIS_SCALE_FACTOR
        if scale_factor > 1.0:
            print(f"将图像放大 {scale_factor} 倍...")
            image_bgr = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
            new_width = int(image_bgr.shape[1] * scale_factor)
            new_height = int(image_bgr.shape[0] * scale_factor)
            upscaled_bgr = cv2.resize(image_bgr, (new_width, new_height), interpolation=cv2.INTER_CUBIC)
            image_to_process = Image.fromarray(cv2.cvtColor(upscaled_bgr, cv2.COLOR_BGR2RGB))
        else:
            image_to_process = image
        
        # --- 步骤 2: 保存预处理后的输入图 ---
        input_image_path = output_dir_path / "01_input_processed.png"
        image_to_process.save(input_image_path)

        # --- 步骤 3: 创建并应用地理蒙版 (使用预处理后的图像) ---
        image_array = np.array(image_to_process) 
        ocean_mask = geo_utils.create_ocean_mask(
            image_shape=image_array.shape,
            geojson_path=config.GEOJSON_PATH,
            bounds=config.TARGET_AREA
        )
        
        ocean_only_image_array = geo_utils.apply_mask(image_to_process, ocean_mask)
        masked_image_path = output_dir_path / "03_ocean_only.png"
        Image.fromarray(ocean_only_image_array).save(masked_image_path)
        
        # --- 步骤 4: 核心颜色分析 ---
        analysis_result = processor.analyze_ocean_color(
            image_array=ocean_only_image_array,
            ocean_mask=ocean_mask,
            output_dir=str(output_dir_path),
            hsv_ranges_override=hsv_ranges_override # 将覆盖参数传递给核心函数
        )

    except Exception as e:
        print(f"An unexpected error occurred during image processing pipeline: {e}")
        analysis_result = {"status": "error"}
        
    return analysis_result


def run_analysis_and_persist(timestamp: int, db: Session) -> Dict[str, Any] | None:
    """
    对单个时间戳执行完整的分析，包括下载、处理和持久化。
    """
    print(f"\n--- [Core Logic] Processing timestamp: {timestamp} ---")
    
    output_dir_path = Path("data") / "output" / str(timestamp)
    output_dir_web_format = output_dir_path.as_posix()

    analysis_data = {"timestamp": timestamp}
    analysis_result = {}

    if processor.is_night(timestamp):
        analysis_data["status"] = "night"
    else:
        stitched_image = downloader.download_stitched_image(timestamp)
        if stitched_image is None:
            analysis_data["status"] = "download_failed"
        else:
            # 调用图像处理流水线 (常规任务不传递 hsv_ranges_override)
            analysis_result = pipeline.process_image_pipeline(stitched_image, output_dir_path)
            
            # 从处理结果更新要持久化的数据
            
            analysis_data.update({
                "status": analysis_result.get("status", "error"),
                "sea_blueness": analysis_result.get("seaBlueness"),
                "cloud_coverage": analysis_result.get("cloudCoverage"),
            })

    # --- 持久化过程 ---
    result_to_persist = schemas.AnalysisResultCreate(**analysis_data)
    db_record = crud.upsert_analysis_result(db, result_data=result_to_persist)
    print(f"[{timestamp}] Data for timestamp has been upserted to the database.")
    
    # --- 准备API响应 ---
    final_response = analysis_result.copy()
    final_response.update({
        'id': db_record.id,
        'status': db_record.status,
        'timestamp': db_record.timestamp,
        'output_directory': output_dir_web_format
    })
    
    return final_response

# =================================================================
#  Scheduled Task
# =================================================================

def scheduled_analysis_task():
    """
    定时任务：获取新时间戳，分析数据，并存入数据库。
    """
    print("\n>>> [Scheduler] Starting new analysis cycle...")
    db: Session = SessionLocal()
    try:
        processed_timestamps = crud.get_processed_timestamps(db)
        print(f"[Scheduler] Found {len(processed_timestamps)} processed timestamps in DB.")
        
        timestamps_url = config.ACTIVE_CONFIG["timestamps_url"]
        response = requests.get(timestamps_url, headers=config.COMMON_HEADERS)
        response.raise_for_status()
        data = response.json()
        
        timestamp_key = config.ACTIVE_CONFIG["timestamp_json_key"]
        all_timestamps = data.get(timestamp_key) if timestamp_key else data
        
        if not isinstance(all_timestamps, list):
            print(f"[Scheduler] Error: Timestamps data is not a list.")
            return

        new_timestamps = sorted([ts for ts in all_timestamps if ts not in processed_timestamps])
        
        if not new_timestamps:
            print("[Scheduler] No new timestamps to process.")
            return
            
        print(f"[Scheduler] Found {len(new_timestamps)} new timestamps to process.")
        for ts in new_timestamps:
            run_analysis_and_persist(ts, db)
    
    except Exception as e:
        print(f"[Scheduler] An error occurred during the scheduled task: {e}")
    finally:
        print(">>> [Scheduler] Analysis cycle finished.")
        db.close()

# =================================================================
#  FastAPI Application Setup
# =================================================================

scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理：启动时初始化数据库和调度器。
    """
    print("--- Application starting up ---")
    init_db()
    
    if config.SKIP_INITIAL_TASK:
        print("[Lifespan] Skipping initial task run as per SKIP_INITIAL_TASK configuration.")
    else:
        print("[Lifespan] Performing initial task run...")
        scheduled_analysis_task()
        print("[Lifespan] Initial task run complete.")

    scheduler.add_job(scheduled_analysis_task, 'interval', minutes=10, id="main_task")
    scheduler.start()
    
    yield
    
    print("--- Application shutting down ---")
    scheduler.shutdown()

app = FastAPI(
    title="Aqua-Chroma API", 
    lifespan=lifespan,
    description="An automated service for monitoring ocean color from satellite imagery."
)

# --- Include Routers ---
app.include_router(tools.router)

# --- Mount Static Files ---
app.mount("/data", StaticFiles(directory="data"), name="data")
# 挂载测试工具的输出目录，使其可以在网页上访问
app.mount("/test_results", StaticFiles(directory="test_results"), name="test_results")


# =================================================================
#  API Endpoints
# =================================================================

@app.get("/", summary="Health Check")
def health_check():
    """
    提供一个简单的健康检查端点。
    """
    return R_success(msg="Aqua-Chroma API is running.")

@app.get("/api/results", summary="Get All Analysis Results")
def get_results(db: Session = Depends(get_db)):
    results_from_db = crud.get_all_results(db)
    
    response_data: List[schemas.AnalysisResultResponse] = []
    for result in results_from_db:
        db_data = schemas.AnalysisResultFromDB.model_validate(result)
        
        response_item = schemas.AnalysisResultResponse(
            **db_data.model_dump(),
            output_directory=f"{config.OUTPUT_BASE_DIR}/{db_data.timestamp}"
        )
        response_data.append(response_item)
        
    return R_success(data=response_data)

@app.get("/api/debug/analyze/{timestamp}", summary="Debug/Re-run Analysis for a Timestamp")
async def debug_analyze_by_timestamp(timestamp: int, db: Session = Depends(get_db)):
    """
    对单个时间戳执行分析。
    - 如果该时间戳的数据已存在，则更新。
    - 如果不存在，则创建。
    """
    result_data = run_analysis_and_persist(timestamp, db)
    
    if result_data:
        return R_success(data=result_data, msg=f"Analysis for timestamp {timestamp} has been successfully upserted.")
    else:
        return R_fail(
            msg=f"Failed to analyze timestamp {timestamp}. Check logs for details.", 
            code=404
        )