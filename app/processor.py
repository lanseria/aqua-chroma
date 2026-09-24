# app/processor.py

import os # 导入os模块
import math
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import ephem
import numpy as np
import cv2
from PIL import Image # 导入Image模块

from . import config

def is_night(timestamp: int) -> bool:
    """
    根据太阳高度角判断该时间戳是否处于“有效白天”。
    直接计算监测点上空此时刻的太阳高度角，低于 MIN_SUN_ELEVATION_DEG（见 config）
    视为夜间/晨昏——光照不足以支撑 HSV 颜色分析。
    高度角随季节逐日变化，有效窗口自动收放，无需维护时间缓冲表。
    """
    try:
        observer = ephem.Observer()
        observer.lat = config.MONITOR_LAT
        observer.lon = config.MONITOR_LON
        observer.elevation = 0
        observer.date = datetime.fromtimestamp(timestamp, tz=timezone.utc)

        sun = ephem.Sun()
        sun.compute(observer)
        altitude_deg = math.degrees(sun.alt)

        return altitude_deg < config.MIN_SUN_ELEVATION_DEG

    except Exception as e:
        print(f"Error calculating sun altitude: {e}. Fallback to processing.")
        # 如果计算出错，为了保险起见，暂时认为是可以处理的（或者根据需求改为 True 跳过）
        return False

def analyze_ocean_color(image_array: np.ndarray, ocean_mask: np.ndarray, output_dir: str, hsv_ranges_override: Optional[Dict] = None) -> Dict[str, Any]:
    """
    使用基于 HSV 颜色范围的阈值法对海洋图像进行分类和分析。
    新增: hsv_ranges_override 参数，用于接收临时的HSV阈值。
    """
    total_ocean_pixels = np.count_nonzero(ocean_mask)
    if total_ocean_pixels == 0:
        # 即使没有海洋像素，也写出一张全黑分类图，保证输出目录的产物文件齐全
        Image.fromarray(np.zeros_like(image_array, dtype=np.uint8)).save(
            os.path.join(output_dir, "04_hsv_classification.png")
        )
        return {"status": "无数据", "seaBlueness": None, "cloudCoverage": None, "bluenessIndex": None, "bluePercentage": None, "yellowPercentage": None}

    # --- 1. 转换到 HSV 颜色空间 ---
    hsv_image = cv2.cvtColor(image_array, cv2.COLOR_RGB2HSV)

    # --- 2. 根据配置定义 HSV 范围 ---
    # 优先使用传入的 hsv_ranges_override，否则回退到 config 文件中的默认值
    ranges = hsv_ranges_override if hsv_ranges_override is not None else config.COLOR_CLASSIFICATION_HSV_RANGES
    print(f"--- [Processor] Using HSV Ranges: {ranges} ---")
    
    cloud_lower = np.array(ranges["CLOUD"]["lower"])
    cloud_upper = np.array(ranges["CLOUD"]["upper"])
    blue_lower = np.array(ranges["BLUE_WATER"]["lower"])
    blue_upper = np.array(ranges["BLUE_WATER"]["upper"])
    
    # --- 3. 像素分类 ---
    # 规则应用有优先级：首先判断是不是云，然后在非云像素中判断是不是蓝水。
    
    # 3.1 识别所有符合“云”颜色范围的像素
    cloud_mask_hsv = cv2.inRange(hsv_image, cloud_lower, cloud_upper)
    # 最终的云像素必须同时在海洋区域内和HSV颜色范围内
    final_cloud_mask = cv2.bitwise_and(cloud_mask_hsv, cloud_mask_hsv, mask=ocean_mask)

    # 3.2 识别所有符合“蓝水”颜色范围的像素
    blue_mask_hsv = cv2.inRange(hsv_image, blue_lower, blue_upper)
    # 最终的蓝水像素必须在海洋区域内、在HSV颜色范围内，且【不是】云
    non_cloud_mask = cv2.bitwise_not(final_cloud_mask)
    final_blue_mask = cv2.bitwise_and(blue_mask_hsv, blue_mask_hsv, mask=non_cloud_mask)
    final_blue_mask = cv2.bitwise_and(final_blue_mask, final_blue_mask, mask=ocean_mask) # 再次确认在海洋区

    # 3.3 “黄水”是海洋区域内所有非云、非蓝水的像素
    non_cloud_blue_mask = cv2.bitwise_not(cv2.bitwise_or(final_cloud_mask, final_blue_mask))
    final_yellow_mask = cv2.bitwise_and(ocean_mask, non_cloud_blue_mask)

    # --- 4. 统计各类像素数量 ---
    cloud_pixels = np.count_nonzero(final_cloud_mask)
    blue_pixels = np.count_nonzero(final_blue_mask)
    yellow_pixels = np.count_nonzero(final_yellow_mask)

    print("\n--- [Processor] HSV Thresholding Pixel Count ---")
    print(f"  - Cloud Pixels      : {cloud_pixels}")
    print(f"  - Blue Water Pixels : {blue_pixels}")
    print(f"  - Yellow Water Pixels: {yellow_pixels}")
    print("--------------------------------------------------\n")

    # --- 5. 生成并保存分类调试图 ---
    classification_map_bgr = np.zeros_like(image_array, dtype=np.uint8)
    classification_map_bgr[final_cloud_mask > 0] = (255, 255, 255)  # 白色
    classification_map_bgr[final_blue_mask > 0] = (138, 89, 0)     # 蓝色 (BGR)
    classification_map_bgr[final_yellow_mask > 0] = (9, 117, 161)  # 棕色 (BGR)
    classification_map_rgb = cv2.cvtColor(classification_map_bgr, cv2.COLOR_BGR2RGB)
    Image.fromarray(classification_map_rgb).save(os.path.join(output_dir, "04_hsv_classification.png"))

    # --- 6. 计算各项指标 ---
    # 口径拆分（metric_version=2）:
    #   seaBlueness = blue / (blue + yellow)，即"可见水体"中蓝色占比，纯水色指标，与云量无关；
    #   cloudCoverage = cloud / total_ocean，大气/云属性；
    #   bluenessIndex = seaBlueness * (1 - cloudCoverage)，保留旧口径"云会压低海蓝分"的综合观感语义。
    # 旧口径 (metric_version=1) 的 seaBlueness = blue / total_ocean，可与新指标互相换算:
    #   v1_sea_blueness = blueness_index, v1_blue_percentage = sea_blueness * (1 - cloud_coverage)
    visible_water_pixels = blue_pixels + yellow_pixels
    sea_blueness = (blue_pixels / visible_water_pixels) if visible_water_pixels > 0 else None

    cloud_coverage = cloud_pixels / total_ocean_pixels if total_ocean_pixels > 0 else 0.0
    blueness_index = (
        sea_blueness * (1.0 - cloud_coverage) if sea_blueness is not None else None
    )
    blue_percentage = blue_pixels / total_ocean_pixels if total_ocean_pixels > 0 else 0.0
    yellow_percentage = yellow_pixels / total_ocean_pixels if total_ocean_pixels > 0 else 0.0

    # 云量超过阈值时标记为 cloudy，便于查询端区分低质量样本（云主导场景下的水色值不可信）
    status = "cloudy" if cloud_coverage >= config.CLOUD_COVERAGE_THRESHOLD else "completed"

    return {
        "status": status,
        "seaBlueness": sea_blueness,
        "cloudCoverage": cloud_coverage,
        "bluenessIndex": blueness_index,
        "bluePercentage": blue_percentage,
        "yellowPercentage": yellow_percentage,
        "bluePixels": int(blue_pixels),
        "yellowPixels": int(yellow_pixels),
        "cloudPixels": int(cloud_pixels),
        "totalOceanPixels": int(total_ocean_pixels),
    }