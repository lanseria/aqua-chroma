# app/main.py

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from sqlalchemy.orm import Session

from . import config, crud, downloader, geo_utils, processor, schemas
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
#  Database Dependency
# =================================================================

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

def run_analysis_and_persist(timestamp: int, db: Session) -> Dict[str, Any] | None:
    """
    对单个时间戳执行完整的分析，返回一个包含所有信息的字典。
    """
    print(f"\n--- [Core Logic] Processing timestamp: {timestamp} ---")
    
    output_dir_path = Path("data") / "output" / str(timestamp)
    os.makedirs(output_dir_path, exist_ok=True)
    output_dir_web_format = output_dir_path.as_posix()

    # 初始化将要存入数据库的数据结构
    analysis_data = {
        "timestamp": timestamp,
        "status": "unknown",
        "sea_blueness": None,
        "cloud_coverage": None,
    }
    if processor.is_night(timestamp):
        analysis_data["status"] = "night"
    else:
        stitched_image = downloader.download_stitched_image(timestamp)
        if stitched_image is None:
            return None # 下载失败则中止

        raw_image_path = output_dir_path / "01_downloaded_cropped.png"
        stitched_image.save(raw_image_path)

        try:
            image_array = np.array(stitched_image)
            ocean_mask = geo_utils.create_ocean_mask(
                image_shape=image_array.shape,
                geojson_path=config.GEOJSON_PATH,
                bounds=config.TARGET_AREA
            )
            mask_path = output_dir_path / "02_generated_mask.png"
            Image.fromarray(ocean_mask).save(mask_path)
            
            ocean_only_image_array = geo_utils.apply_mask(stitched_image, ocean_mask)
            masked_image_path = output_dir_path / "03_ocean_only.png"
            Image.fromarray(ocean_only_image_array).save(masked_image_path)
            
            analysis_result = processor.analyze_ocean_color(np.array(stitched_image), str(output_dir_path))
            analysis_data.update({
                "status": analysis_result.get("status", "error"),
                "sea_blueness": analysis_result.get("seaBlueness"),
                "cloud_coverage": analysis_result.get("cloudCoverage"),
            })

        except Exception as e:
            print(f"[{timestamp}] An unexpected error occurred during analysis: {e}")
            analysis_data["status"] = "error"

    # --- 持久化过程 ---
    result_to_persist = schemas.AnalysisResultCreate(**analysis_data)
    crud.upsert_analysis_result(db, result_data=result_to_persist)
    print(f"[{timestamp}] Data for timestamp has been upserted to the database.")
    
    # 返回一个包含动态生成路径的完整结果，用于API响应
    return {
        **analysis_data,
        "output_directory": output_dir_path.as_posix()
    }

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
            # 调度任务只处理新数据，所以这里实际上总是 "insert"
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
    
    if str(os.getenv("SKIP_INITIAL_TASK", "false")).lower() in ('true', '1', 't'):
        print("[Lifespan] Skipping initial task run as per SKIP_INITIAL_TASK env var.")
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

# --- Mount Static Files ---
app.mount("/data", StaticFiles(directory="data"), name="data")

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
        # 1. 使用 AnalysisResultFromDB 模型来安全地验证从数据库取出的数据
        #    这一步现在不会再报错了，因为模型和数据是匹配的
        db_data = schemas.AnalysisResultFromDB.model_validate(result)
        
        # 2. 手动创建最终的响应模型，并动态添加 output_directory
        response_item = schemas.AnalysisResultResponse(
            **db_data.model_dump(), # 复制所有已验证的字段
            output_directory=f"{config.OUTPUT_BASE_DIR}/{db_data.timestamp}"
        )
        response_data.append(response_item)
        
    return R_success(data=response_data)

@app.post("/api/debug/analyze/{timestamp}", summary="Debug/Re-run Analysis for a Timestamp")
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