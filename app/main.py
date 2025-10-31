# app/main.py

# (所有 import 保持不变)
import json
import os
from contextlib import asynccontextmanager
import numpy as np
import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image
from . import config, downloader, geo_utils, processor


def run_analysis_for_timestamp(timestamp: int) -> dict | None:
    print(f"\n--- 开始处理时间戳: {timestamp} ---")
    output_dir = os.path.join("data", "output", str(timestamp))
    os.makedirs(output_dir, exist_ok=True)
    if processor.is_night(timestamp):
        print(f"[{timestamp}] 判断为黑夜，跳过分析。")
        return {"timestamp": timestamp, "output_directory": output_dir, "result": {"status": "黑夜"}}
    stitched_image = downloader.download_stitched_image(timestamp)
    if stitched_image is None:
        print(f"[{timestamp}] 无数据或下载失败，跳过。")
        return None
    raw_image_path = os.path.join(output_dir, "01_downloaded_cropped.png")
    stitched_image.save(raw_image_path)
    try:
        ocean_mask = geo_utils.create_ocean_mask(
            image_shape=np.array(stitched_image).shape,
            geojson_path=config.GEOJSON_PATH,
            bounds=config.TARGET_AREA
        )
        mask_path = os.path.join(output_dir, "02_generated_mask.png")
        Image.fromarray(ocean_mask).save(mask_path)
        ocean_only_image_array = geo_utils.apply_mask(stitched_image, ocean_mask)
        masked_image_path = os.path.join(output_dir, "03_ocean_only.png")
        Image.fromarray(ocean_only_image_array).save(masked_image_path)
    except FileNotFoundError as e:
        print(f"[{timestamp}] 错误: {e}")
        return None
    analysis_result = processor.analyze_ocean_color(ocean_only_image_array, output_dir)
    print(f"[{timestamp}] 分析完成: {analysis_result.get('status')}")
    return {"timestamp": timestamp, "output_directory": output_dir, "result": analysis_result}

# --- 修改 scheduled_analysis_task 函数 ---
def scheduled_analysis_task():
    print("\n>>> [调度任务] 开始执行新一轮检查...")
    try:
        with open(config.RESULTS_JSON_PATH, 'r', encoding='utf-8') as f:
            results_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        results_data = []
    
    processed_timestamps = {item['timestamp'] for item in results_data}
    print(f"[调度任务] 已加载 {len(processed_timestamps)} 条已处理的记录。")

    # 1. 从配置中获取正确的时间戳URL
    timestamps_url = config.ACTIVE_CONFIG["timestamps_url"]
    try:
        print(f"[调度任务] 正在从 {timestamps_url} 获取时间戳列表...")
        response = requests.get(timestamps_url, headers=config.COMMON_HEADERS)
        response.raise_for_status()
        data = response.json()

        # 2. 根据配置，从正确的key中提取时间戳数组
        timestamp_key = config.ACTIVE_CONFIG["timestamp_json_key"]
        if timestamp_key:
            all_timestamps = data.get(timestamp_key, [])
        else:
            all_timestamps = data  # 如果key为None，则JSON本身就是数组
        
        if not isinstance(all_timestamps, list):
            print(f"[调度任务] 错误: 未能从数据源获取到有效的时间戳列表。")
            return

    except requests.RequestException as e:
        print(f"[调度任务] 错误: 无法获取时间戳列表: {e}")
        return
    except (json.JSONDecodeError, AttributeError):
        print(f"[调度任务] 错误: 解析时间戳JSON失败。")
        return

    new_timestamps = sorted([ts for ts in all_timestamps if ts not in processed_timestamps])

    if not new_timestamps:
        print("[调度任务] 没有需要分析的新时间戳。")
        return
    
    print(f"[调度任务] 发现 {len(new_timestamps)} 个新时间戳需要分析。开始处理...")
    for ts in new_timestamps:
        result = run_analysis_for_timestamp(ts)
        if result:
            results_data.append(result)
            with open(config.RESULTS_JSON_PATH, 'w', encoding='utf-8') as f:
                json.dump(results_data, f, indent=4, ensure_ascii=False)
    
    print(">>> [调度任务] 本轮所有新时间戳处理完毕。")

scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 应用启动时
    print("应用启动，添加并启动定时任务...")

    # 1. 直接手动调用任务函数，实现“启动时立即执行一次”
    print("--- 正在执行首次启动任务 ---")
    scheduled_analysis_task()
    print("--- 首次启动任务执行完毕 ---")

    # 2. 添加定时任务，用于未来的周期性执行
    scheduler.add_job(scheduled_analysis_task, 'interval', minutes=10, id="main_task")

    # 3. 启动调度器
    scheduler.start()

    # --- 以下是FastAPI生命周期管理的标准代码 ---
    yield

    # 应用关闭时
    print("应用关闭，停止定时任务...")
    scheduler.shutdown()

app = FastAPI(title="Aqua-Chroma API", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/data", StaticFiles(directory="data"), name="data")
templates = Jinja2Templates(directory="templates")

@app.get("/results")
def get_results():
    try:
        with open(config.RESULTS_JSON_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="结果文件格式错误。")

@app.post("/debug/analyze/{timestamp}", tags=["Debugging"])
async def debug_analyze_by_timestamp(timestamp: int):
    """
    接收一个时间戳，立即执行一次完整的分析流程，用于测试和调试。
    
    - **注意**: 此端点的分析结果【不会】被保存到最终的 results.json 文件中。
    - 它会生成并覆盖对应时间戳的调试图片。
    - 直接返回本次分析的详细结果。
    """
    print(f"\n>>> [调试接口] 收到对时间戳 {timestamp} 的手动分析请求...")
    
    # 我们直接复用已有的核心分析函数
    result = run_analysis_for_timestamp(timestamp)
    
    if result:
        print(f">>> [调试接口] 时间戳 {timestamp} 分析完成。")
        return result
    else:
        # 如果 run_analysis_for_timestamp 返回 None (例如下载失败)
        print(f">>> [调试接口] 时间戳 {timestamp} 分析失败或无数据。")
        raise HTTPException(
            status_code=404, 
            detail=f"无法为时间戳 {timestamp} 生成分析结果。可能原因：下载失败、无数据或处理过程中发生错误。"
        )

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})