# app/main.py

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


# --- 1. 将核心分析逻辑封装成一个独立的函数 ---
def run_analysis_for_timestamp(timestamp: int) -> dict | None:
    """
    对单个时间戳执行完整的分析、保存图片并返回结果。
    这是被调度任务调用的核心工作单元。
    """
    print(f"\n--- 开始处理时间戳: {timestamp} ---")
    output_dir = os.path.join("data", "output", str(timestamp))
    os.makedirs(output_dir, exist_ok=True)

    # 步骤1: 判断是否黑夜
    if processor.is_night(timestamp):
        print(f"[{timestamp}] 判断为黑夜，跳过分析。")
        return {
            "timestamp": timestamp,
            "output_directory": output_dir,
            "result": {"status": "黑夜"}
        }

    # 步骤2: 下载并精确裁剪图像
    stitched_image = downloader.download_stitched_image(timestamp)
    if stitched_image is None:
        print(f"[{timestamp}] 无数据或下载失败，跳过。")
        return None  # 如果下载失败，我们不记录任何内容

    raw_image_path = os.path.join(output_dir, "01_downloaded_cropped.png")
    stitched_image.save(raw_image_path)

    # 步骤3: 创建并应用GeoJSON蒙版
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

    # 步骤4: 分析图像
    analysis_result = processor.analyze_ocean_color(ocean_only_image_array, output_dir)
    print(f"[{timestamp}] 分析完成: {analysis_result.get('status')}")

    return {
        "timestamp": timestamp,
        "output_directory": output_dir,
        "result": analysis_result
    }


# --- 2. 创建将被 APScheduler 调度的函数 ---
def scheduled_analysis_task():
    """
    定时任务的主函数：获取时间戳、过滤已处理的、然后逐个分析。
    """
    print("\n>>> [调度任务] 开始执行新一轮检查...")

    # 加载已有的分析结果
    try:
        with open(config.RESULTS_JSON_PATH, 'r', encoding='utf-8') as f:
            results_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        results_data = []
    
    processed_timestamps = {item['timestamp'] for item in results_data}
    print(f"[调度任务] 已加载 {len(processed_timestamps)} 条已处理的记录。")

    # 获取所有可用的时间戳
    try:
        response = requests.get(config.TIMESTAMPS_URL)
        response.raise_for_status()
        all_timestamps = response.json()
    except requests.RequestException as e:
        print(f"[调度任务] 错误: 无法获取时间戳列表: {e}")
        return

    # 筛选出需要新分析的时间戳
    new_timestamps = sorted([ts for ts in all_timestamps if ts not in processed_timestamps])

    if not new_timestamps:
        print("[调度任务] 没有需要分析的新时间戳。")
        return
    
    print(f"[调度任务] 发现 {len(new_timestamps)} 个新时间戳需要分析。开始处理...")

    # 循环处理每一个新的时间戳
    for ts in new_timestamps:
        result = run_analysis_for_timestamp(ts)
        if result:
            # 每完成一个，就追加到结果列表并立即保存
            results_data.append(result)
            with open(config.RESULTS_JSON_PATH, 'w', encoding='utf-8') as f:
                json.dump(results_data, f, indent=4)
    
    print(">>> [调度任务] 本轮所有新时间戳处理完毕。")


# --- 3. 设置 FastAPI 的生命周期事件来启动调度器 ---
scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 应用启动时
    print("应用启动，添加并启动定时任务...")
    # 立即执行一次，不等第一个10分钟
    scheduled_analysis_task()
    # 添加定时任务，每10分钟运行一次
    scheduler.add_job(scheduled_analysis_task, 'interval', minutes=10)
    scheduler.start()
    yield
    # 应用关闭时
    print("应用关闭，停止定时任务...")
    scheduler.shutdown()

app = FastAPI(title="Aqua-Chroma API", lifespan=lifespan)

# 1. 挂载 `static` 目录，用于提供CSS和JS
app.mount("/static", StaticFiles(directory="static"), name="static")
# 2. 挂载 `data` 目录，用于提供生成的图片
app.mount("/data", StaticFiles(directory="data"), name="data")

# --- 设置Jinja2模板引擎 ---
templates = Jinja2Templates(directory="templates")

# --- 4. 提供一个API端点来获取最终的JSON数据 ---
@app.get("/results")
def get_results():
    """
    读取并返回包含所有分析结果的JSON文件。
    这是给前端页面使用的数据接口。
    """
    try:
        with open(config.RESULTS_JSON_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return [] # 如果文件还未创建，返回一个空列表
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="结果文件格式错误。")

# --- 根路由，用于提供HTML前端页面 ---
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """
    当用户访问根URL时，返回渲染后的index.html页面。
    """
    return templates.TemplateResponse("index.html", {"request": request})