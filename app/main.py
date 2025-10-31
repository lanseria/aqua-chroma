# app/main.py

import os
from fastapi import FastAPI, HTTPException
from PIL import Image
import numpy as np

from . import downloader
from . import geo_utils
from . import processor
from . import config

app = FastAPI(title="Aqua-Chroma API")

@app.get("/analyze/{timestamp}")
def analyze_by_timestamp(timestamp: int):
    """
    接收一个时间戳，执行完整的海洋颜色分析流程，并保存每一步的调试图像。
    """
    output_dir = os.path.join("data", "output", str(timestamp))
    os.makedirs(output_dir, exist_ok=True)
    print(f"调试图片将保存在: {output_dir}")

    # 步骤1: 判断是否黑夜
    if processor.is_night(timestamp):
        return {"timestamp": timestamp, "result": "黑夜"}

    # 步骤2: 下载卫星图像
    stitched_image = downloader.download_stitched_image(timestamp)
    if stitched_image is None:
        return {"timestamp": timestamp, "result": "无数据 (下载失败)"}
    
    raw_image_path = os.path.join(output_dir, "01_downloaded_raw.png")
    stitched_image.save(raw_image_path)
    print(f"已保存原始图像至: {raw_image_path}")

    # 步骤3: 使用新的函数创建并应用蒙版
    try:
        # --- 调用更新后的函数 ---
        ocean_mask = geo_utils.create_ocean_mask(
            image_shape=np.array(stitched_image).shape,
            geojson_path=config.GEOJSON_PATH,
            bounds=config.TARGET_AREA # 仍然需要 bounds
        )
        
        # --- 新增：保存生成的蒙版图像以供检查 ---
        mask_path = os.path.join(output_dir, "02_generated_mask.png")
        Image.fromarray(ocean_mask).save(mask_path)
        print(f"已保存生成的蒙版至: {mask_path}")

        ocean_only_image_array = geo_utils.apply_mask(stitched_image, ocean_mask)

    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    masked_image_path = os.path.join(output_dir, "03_ocean_only.png")
    Image.fromarray(ocean_only_image_array).save(masked_image_path)
    print(f"已保存裁剪陆地后的图像至: {masked_image_path}")

    # 步骤4: 分析图像 (注意文件名序号已顺延)
    analysis_result = processor.analyze_ocean_color(ocean_only_image_array, output_dir)
    
    return {
        "timestamp": timestamp,
        "output_directory": output_dir,
        "result": analysis_result
    }

@app.get("/")
def read_root():
    return {"message": "欢迎使用 Aqua-Chroma 海洋颜色分析项目！"}