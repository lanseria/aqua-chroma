# app/main.py
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import config, crud, downloader, processor, schemas, tools, pipeline
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

def run_analysis_and_persist(timestamp: int, db: Session) -> Dict[str, Any] | None:
    """
    对单个时间戳执行完整的分析，包括下载、处理和持久化。
    返回持久化后的记录状态（"completed"/"night"/"download_failed"/"error" 等）。
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

            # 从处理结果更新要持久化的数据（含像素计数与指标版本，见 processor.analyze_ocean_color）
            if analysis_result.get("status") == "error":
                analysis_data["status"] = "error"
            else:
                analysis_data.update({
                    "status": analysis_result.get("status", "error"),
                    "metric_version": 2,
                    "sea_blueness": analysis_result.get("seaBlueness"),
                    "cloud_coverage": analysis_result.get("cloudCoverage"),
                    "blueness_index": analysis_result.get("bluenessIndex"),
                    "blue_pixels": analysis_result.get("bluePixels"),
                    "yellow_pixels": analysis_result.get("yellowPixels"),
                    "cloud_pixels": analysis_result.get("cloudPixels"),
                    "total_ocean_pixels": analysis_result.get("totalOceanPixels"),
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

    final_response['_record_status'] = db_record.status
    return final_response

# =================================================================
#  Scheduled Task
# =================================================================

def _fetch_available_timestamps() -> List[int]:
    """从数据源拉取可用时间戳列表，失败时返回空列表。"""
    timestamps_url = config.ACTIVE_CONFIG["timestamps_url"]
    response = requests.get(timestamps_url, headers=config.COMMON_HEADERS, timeout=30)
    response.raise_for_status()
    data = response.json()

    timestamp_key = config.ACTIVE_CONFIG["timestamp_json_key"]
    all_timestamps = data.get(timestamp_key) if timestamp_key else data

    if not isinstance(all_timestamps, list):
        print("[Scheduler] Error: Timestamps data is not a list.")
        return []
    return all_timestamps


def scheduled_analysis_task():
    """
    定时任务：获取新时间戳，分析数据，并存入数据库。
    对下载失败的时间戳，在本周期内间隔重试若干轮（等待瓦片数据上线），
    仍失败则保持 download_failed，交由下个调度周期继续重试。
    """
    print("\n>>> [Scheduler] Starting new analysis cycle...")
    db: Session = SessionLocal()
    try:
        processed_timestamps = crud.get_processed_timestamps(db)
        print(f"[Scheduler] Found {len(processed_timestamps)} processed timestamps in DB.")

        all_timestamps = _fetch_available_timestamps()
        if not all_timestamps:
            return

        new_timestamps = sorted([ts for ts in all_timestamps if ts not in processed_timestamps])

        if not new_timestamps:
            print("[Scheduler] No new timestamps to process.")
            return

        print(f"[Scheduler] Found {len(new_timestamps)} new timestamps to process.")

        # 待重试队列：本轮下载失败的时间戳
        pending_retries: List[int] = []
        for ts in new_timestamps:
            result = run_analysis_and_persist(ts, db)
            if result and result.get("_record_status") in crud.RETRYABLE_STATUSES:
                pending_retries.append(ts)

        # 周期内多轮重试，直到成功或轮次用尽
        for round_no in range(2, config.FAILED_TIMESTAMP_RETRY_ROUNDS + 1):
            if not pending_retries:
                break
            print(f"[Scheduler] {len(pending_retries)} timestamp(s) failed, "
                  f"retry round {round_no}/{config.FAILED_TIMESTAMP_RETRY_ROUNDS} "
                  f"in {config.FAILED_TIMESTAMP_RETRY_DELAY_SECONDS}s...")
            time.sleep(config.FAILED_TIMESTAMP_RETRY_DELAY_SECONDS)
            still_failing: List[int] = []
            for ts in pending_retries:
                result = run_analysis_and_persist(ts, db)
                if result and result.get("_record_status") in crud.RETRYABLE_STATUSES:
                    still_failing.append(ts)
            pending_retries = still_failing

        if pending_retries:
            print(f"[Scheduler] {len(pending_retries)} timestamp(s) still failing after "
                  f"{config.FAILED_TIMESTAMP_RETRY_ROUNDS} rounds: {pending_retries}. "
                  f"Will retry in next cycle.")

    except Exception as e:
        print(f"[Scheduler] An error occurred during the scheduled task: {e}")
    finally:
        print(">>> [Scheduler] Analysis cycle finished.")
        db.close()

# =================================================================
#  FastAPI Application Setup
# =================================================================

scheduler = BackgroundScheduler()

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
        result_data.pop("_record_status", None)  # 内部重试标记，不对外暴露
        return R_success(data=result_data, msg=f"Analysis for timestamp {timestamp} has been successfully upserted.")
    else:
        return R_fail(
            msg=f"Failed to analyze timestamp {timestamp}. Check logs for details.", 
            code=404
        )