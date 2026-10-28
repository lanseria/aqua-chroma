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
#  Storage / Mount Diagnostics
# =================================================================
# Docker 部署时分析图片写入容器内的 data/output/<timestamp>/，该目录由
# docker-compose 的 `${TILES_HOST_PATH:-aqua-chroma-data}:/app/data` 挂载到宿主机。
# 挂载没生效（例如 TILES_HOST_PATH 只写在 env_file 的 .env.production 里，而 compose
# 的变量替换不读取 env_file）时，图片会写进容器内/命名卷，宿主机目录看起来"一张图都没有"。
# 这里把「实际写入绝对路径 + 挂载来源 + 是否可写」打日志并暴露诊断接口，便于快速定位。

# 期望被挂载到宿主机的容器内目录（与 docker-compose.yml 的 target 保持一致）
CONTAINER_DATA_MOUNT_POINT = "/app/data"


def _mount_info_for(mount_point: str) -> str | None:
    """从 /proc/self/mountinfo 读取指定挂载点的原始行；非 Linux 或无权限时返回 None。"""
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                parts = line.split()
                if len(parts) > 4 and parts[4] == mount_point:
                    return line.strip()
    except OSError:
        return None
    return None


def _count_output_timestamps() -> int:
    """统计输出目录下已有的时间戳目录数量（纯数字目录）。"""
    output_dir = Path(config.OUTPUT_BASE_DIR)
    if not output_dir.is_dir():
        return 0
    try:
        return sum(1 for entry in output_dir.iterdir() if entry.is_dir() and entry.name.isdigit())
    except OSError:
        return 0


def storage_diagnostics() -> Dict[str, Any]:
    """
    收集存储相关诊断信息（路径、挂载、可写性、已有产物数量），并打印到日志。

    返回结构同时用于 GET /api/debug/storage，方便在容器外直接查看：
        cwd / data_dir / output_dir / mounts / writable / output_timestamp_count
    """
    data_dir = Path("data").resolve()
    output_dir = Path(config.OUTPUT_BASE_DIR).resolve()

    info: Dict[str, Any] = {
        "cwd": str(Path.cwd()),
        "data_dir": str(data_dir),
        "data_dir_exists": data_dir.is_dir(),
        "output_dir": str(output_dir),
        "output_dir_exists": output_dir.is_dir(),
        "output_timestamp_count": _count_output_timestamps(),
        "mounts": {},
        "writable": None,
        "writable_error": None,
    }

    for mount_point in (str(data_dir), CONTAINER_DATA_MOUNT_POINT):
        line = _mount_info_for(mount_point)
        if line:
            info["mounts"][mount_point] = line

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        probe = output_dir / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        info["writable"] = True
    except Exception as exc:  # noqa: BLE001 - 诊断信息，捕获所有异常
        info["writable"] = False
        info["writable_error"] = str(exc)

    print(f"[Storage] 进程工作目录: {info['cwd']}")
    print(
        f"[Storage] 分析输出目录: {info['output_dir']} "
        f"(存在={info['output_dir_exists']}, 已有时间戳目录={info['output_timestamp_count']} 个, "
        f"可写={info['writable']})"
    )
    if info["mounts"]:
        for mount_point, line in info["mounts"].items():
            print(f"[Storage] 挂载点 {mount_point}: {line}")
    else:
        print("[Storage] 未找到 data 目录的挂载信息（非 Linux 环境，或容器内未挂载 /app/data）")
    if info["writable"] is False:
        print(f"[Storage] 警告：输出目录不可写，分析图片将无法保存: {info['writable_error']}")
    return info


def warn_if_storage_mismatch(storage_info: Dict[str, Any], db_rows: int) -> None:
    """
    启动时交叉校验：数据库已有分析记录、但输出目录下一个时间戳目录都没有，
    基本可以确定挂载没生效或挂载到了空目录（图片写进了别处）。打印明确的排查提示。
    """
    if db_rows > 0 and storage_info["output_timestamp_count"] == 0:
        print(
            "[Storage] 警告：数据库中有 "
            f"{db_rows} 条分析记录，但 {storage_info['output_dir']} 下没有任何时间戳目录。\n"
            "[Storage] 这通常意味着 Docker 卷挂载没有生效（图片被写进了容器内或命名卷）。请检查：\n"
            "  1) TILES_HOST_PATH 必须写在 `docker compose` 会读取的位置——项目目录下与 "
            "docker-compose.yml 同级的 `.env`，或 shell 环境变量；\n"
            "     写在 `env_file` 的 .env.production 里不会参与 compose 的变量替换！\n"
            "  2) `docker compose config` 输出中的 volumes 应为 bind，source 为你设置的宿主路径；\n"
            "  3) `docker inspect aqua-chroma-app --format '{{json .Mounts}}'` 查看实际挂载来源。"
        )

# =================================================================
#  Core Analysis Logic
# =================================================================

def run_analysis_and_persist(timestamp: int, db: Session) -> Dict[str, Any] | None:
    """
    对单个时间戳执行完整的分析，包括下载、处理和持久化。
    返回持久化后的记录状态（"completed"/"night"/"download_failed"/"error" 等）。
    """
    print(f"\n--- [Core Logic] Processing timestamp: {timestamp} ---")

    output_dir_path = Path(config.OUTPUT_BASE_DIR) / str(timestamp)
    output_dir_web_format = output_dir_path.as_posix()
    # 打印解析后的绝对路径：Docker 里能直观看到图片实际写到了容器内哪个目录
    print(f"[Core Logic] 本次分析产物目录: {output_dir_path.resolve()}")

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

            # 明确记录本次落盘位置与产物清单，便于排查"宿主机目录里看不到图片"的问题
            saved_artifacts = (
                sorted(entry.name for entry in output_dir_path.iterdir())
                if output_dir_path.is_dir() else []
            )
            print(
                f"[Core Logic] 已写入调试图 {len(saved_artifacts)} 个 -> "
                f"{output_dir_path.resolve()}: {saved_artifacts}"
            )

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
                    "visibility_km": analysis_result.get("visibilityKm"),
                    "visibility_level": analysis_result.get("visibilityLevel"),
                    "haze_score": analysis_result.get("hazeScore"),
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

    # 启动时打印存储/挂载诊断，并交叉校验 DB 记录与输出目录是否对得上
    storage_info = storage_diagnostics()
    try:
        _diag_db = SessionLocal()
        try:
            db_rows = crud.count_analysis_results(_diag_db)
        finally:
            _diag_db.close()
    except Exception as exc:  # noqa: BLE001 - 诊断不应阻断启动
        db_rows = 0
        print(f"[Storage] 读取分析记录数量失败，跳过挂载校验: {exc}")
    storage_info["analysis_result_rows"] = db_rows
    warn_if_storage_mismatch(storage_info, db_rows)
    
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

@app.get("/api/debug/storage", summary="Storage / Mount Diagnostics")
def debug_storage():
    """
    存储/挂载诊断：返回进程工作目录、分析输出目录（绝对路径）、data 目录挂载来源、
    是否可写、已有时间戳目录数量，以及数据库中的分析记录数。

    排查「宿主机目录里看不到分析图片」时，直接访问该接口或在启动日志中查看对应 [Storage] 行。
    """
    info = storage_diagnostics()
    db = SessionLocal()
    try:
        info["analysis_result_rows"] = crud.count_analysis_results(db)
    except Exception as exc:  # noqa: BLE001 - 诊断接口不因 DB 异常失败
        info["analysis_result_rows"] = None
        info["analysis_result_rows_error"] = str(exc)
    finally:
        db.close()
    return R_success(data=info)


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