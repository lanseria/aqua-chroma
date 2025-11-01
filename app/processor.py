# app/processor.py

import os # 导入os模块
import numpy as np
import cv2
from datetime import datetime, timezone, timedelta
from typing import Dict, Any
from PIL import Image # 导入Image模块
from . import config

def is_night(timestamp: int) -> bool:
    """根据时间戳和时区判断是否为黑夜"""
    tz = timezone(timedelta(hours=8)) # 假设为北京时间
    dt_local = datetime.fromtimestamp(timestamp, tz=tz)
    return not (config.NIGHT_END_HOUR <= dt_local.hour < config.NIGHT_START_HOUR)

# --- 修改函数签名，增加 output_dir 参数 ---
def analyze_ocean_color(image_array: np.ndarray, output_dir: str) -> Dict[str, Any]:
    """
    分析经过蒙版处理后的海洋图像，返回分析结果，并保存中间过程图像。
    """
    # 1. 检查无数据 (全黑图像)
    if not np.any(image_array):
        return {"status": "无数据", "details": "图像数据全黑"}

    # 2. 计算有效像素 (非黑色部分)
    gray_image = cv2.cvtColor(image_array, cv2.COLOR_RGB2GRAY)
    total_pixels = np.count_nonzero(gray_image)
    if total_pixels == 0:
        return {"status": "无数据", "details": "裁剪后无有效海洋区域"}

    # 3. 云层分析
    _, cloud_mask = cv2.threshold(gray_image, config.CLOUD_THRESHOLD, 255, cv2.THRESH_BINARY)
    
    # --- 新增：保存云层蒙版 ---
    cloud_mask_path = os.path.join(output_dir, "04_cloud_mask.png") # 序号+1
    Image.fromarray(cloud_mask).save(cloud_mask_path)
    
    cloud_pixels = np.count_nonzero(cloud_mask)
    cloud_coverage = cloud_pixels / total_pixels
    
    if cloud_coverage > config.THICK_CLOUD_COVERAGE:
        return {"status": "云层过厚", "details": f"cloud_coverage: {cloud_coverage:.2%}"}

    # 4. 移除稀薄云层并计算海蓝程度
    ocean_mask = gray_image > 0
    cloud_free_mask = ocean_mask & (cloud_mask == 0)

    # --- 新增：保存移除云层后的图像 ---
    cloud_free_image = cv2.bitwise_and(image_array, image_array, mask=cloud_free_mask.astype(np.uint8))
    cloud_free_path = os.path.join(output_dir, "05_cloud_free.png") # 序号+1
    Image.fromarray(cloud_free_image).save(cloud_free_path)
    
    hsv_image = cv2.cvtColor(image_array, cv2.COLOR_RGB2HSV)
    lower_blue = np.array(config.BLUE_LOWER_BOUND)
    upper_blue = np.array(config.BLUE_UPPER_BOUND)
    blue_mask = cv2.inRange(hsv_image, lower_blue, upper_blue)
    
    # --- 新增：保存原始蓝色区域蒙版 ---
    blue_mask_path = os.path.join(output_dir, "05_raw_blue_mask.png")
    Image.fromarray(blue_mask).save(blue_mask_path)

    final_blue_ocean_mask = cv2.bitwise_and(blue_mask, blue_mask, mask=cloud_free_mask.astype(np.uint8))

    # --- 新增：保存最终用于计算的蓝色海洋区域蒙版 ---
    final_blue_mask_path = os.path.join(output_dir, "06_final_blue_ocean_mask.png")
    Image.fromarray(final_blue_ocean_mask).save(final_blue_mask_path)

    blue_pixel_count = np.count_nonzero(final_blue_ocean_mask)
    cloud_free_pixel_count = np.count_nonzero(cloud_free_mask)

    if cloud_free_pixel_count == 0:
        return {"status": "云层稀薄", "details": "移除云层后无有效海域"}

    blueness_ratio = blue_pixel_count / cloud_free_pixel_count
    
    return {
        "status": "completed", # 使用英文状态
        "seaBlueness": blueness_ratio, # 使用英文键名和原始浮点数
        "cloudCoverage": cloud_coverage # 使用英文键名和原始浮点数
    }