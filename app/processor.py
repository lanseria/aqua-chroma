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

def _visibility_edge_density(edge_map: np.ndarray, ocean_mask: np.ndarray) -> float:
    """统计海洋区域内有效边缘像素占比（返回值域 0~1）。"""
    ocean_pixels = np.count_nonzero(ocean_mask)
    if ocean_pixels == 0:
        return 0.0
    return float(np.count_nonzero(edge_map[ocean_mask > 0])) / ocean_pixels


def estimate_visibility(image_rgb: np.ndarray, ocean_mask: np.ndarray, cloud_coverage: float,
                        cloud_mask: Optional[np.ndarray] = None,
                        output_dir: Optional[str] = None) -> Dict[str, Any]:
    """
    基于单张卫星图像估算海面水平能见度（公里）。

    原理：卫星图无法直接测能见度，改用大气光学代理量折算。
    - 云层过厚（云量 ≥ config.VISIBILITY_ZERO_CLOUD_THRESHOLD）：视场被云顶完全占据，
      海面不可见，卫星图上判断不了能见度，直接记 0。
    - 浓雾低云：雾在图像上表现为"亮而平"的大面积覆盖——暗通道（最小通道）被雾抬亮、
      局部 RMS 对比度被压平、细纹理边缘消失。三者加权得到大气浑浊度得分。
    - 晴好天气：暗通道接近 0（水面阴影/暗色水体），对比度高、海面纹理（波痕、云影、
      岛屿轮廓）清晰，浑浊度得分低，能见度取高值。

    测量只在"无云水面"上进行（cloud_mask 提供时剔除云及其边缘膨胀区），
    否则碎云自身的纹理会被误计为大气通透，导致能见度虚高。
    无云水面占比低于 config.VISIBILITY_MIN_CLEAR_FRACTION 时同样按云层遮蔽记 0。

    折算采用 Koschmieder 定律的经验离散化（V = 3.912 / β，β 为大气消光系数），
    用浑浊度得分非线性映射到 β，保证晴好 ≈ 20-35km、霾 ≈ 5-10km、雾 < 2km。

    Returns:
        {"visibilityKm": float|None, "visibilityLevel": str,
         "hazeScore": float, "darkChannelBrightness": float, "contrastScore": float, "edgeScore": float}
    """
    total_ocean_pixels = np.count_nonzero(ocean_mask)
    if total_ocean_pixels == 0:
        return {"visibilityKm": None, "visibilityLevel": "无数据", "hazeScore": None,
                "darkChannelBrightness": None, "contrastScore": None, "edgeScore": None}

    # --- 0. 云层过厚：海面被云顶遮挡，能见度无从谈起，直接 0 ---
    if cloud_coverage >= config.VISIBILITY_ZERO_CLOUD_THRESHOLD:
        return {"visibilityKm": 0.0, "visibilityLevel": "云层遮蔽",
                "hazeScore": None, "darkChannelBrightness": None,
                "contrastScore": None, "edgeScore": None}

    # --- 0.1 测量区域 = 海洋 - 云（含边缘膨胀区，避免云的过渡像素混入）---
    measure_mask = ocean_mask
    if cloud_mask is not None:
        cloud_dilated = cv2.dilate(cloud_mask, np.ones((9, 9), np.uint8))
        measure_mask = cv2.bitwise_and(ocean_mask, cv2.bitwise_not(cloud_dilated))

    clear_pixels = np.count_nonzero(measure_mask)
    if clear_pixels < total_ocean_pixels * config.VISIBILITY_MIN_CLEAR_FRACTION:
        # 剩余无云水面过少，样本不足以判断大气状况
        return {"visibilityKm": 0.0, "visibilityLevel": "云层遮蔽",
                "hazeScore": None, "darkChannelBrightness": None,
                "contrastScore": None, "edgeScore": None}

    gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY).astype(np.float64)

    # --- 1. 暗通道亮度（大气浑浊度代理）---
    # 窗口取 15px：图像已超分放大 4 倍，15px 对应原分辨率上可观的局部邻域
    patch = 15
    min_channel = cv2.erode(image_rgb.min(axis=2), np.ones((patch, patch), np.uint8))
    dark_channel = cv2.dilate(min_channel, np.ones((3, 3), np.uint8))
    ocean_dark = dark_channel[measure_mask > 0]
    dark_brightness = float(ocean_dark.mean()) / 255.0  # 归一化到 0~1

    # --- 2. 局部 RMS 对比度（雾会压平明暗起伏）---
    # 与暗通道同尺寸的局部均值图，取逐像素偏差的 RMS
    local_mean = cv2.boxFilter(gray, ddepth=-1, ksize=(patch, patch))
    ocean_contrast = float(((gray - local_mean)[measure_mask > 0] ** 2).mean() ** 0.5)
    # 经验归一：晴好海面 RMS 对比度约 8~12 灰阶，浓雾场景 < 2
    contrast_score = min(1.0, ocean_contrast / 10.0)

    # --- 3. 细纹理边缘密度（雾天纹理消失）---
    edges = cv2.Canny(gray.astype(np.uint8), 40, 120)
    edges_ocean = cv2.bitwise_and(edges, edges, mask=measure_mask)
    edge_ratio = _visibility_edge_density(edges_ocean, measure_mask)
    # 经验归一：晴好海面边缘占比约 5%~8%，浓雾 < 0.5%
    edge_score = min(1.0, edge_ratio / 0.05)

    # --- 4. 合成大气浑浊度得分（0=极通透，1=浓雾）---
    # 暗通道是气溶胶光学厚度最直接的代理，权重最高
    haze_score = 0.5 * dark_brightness + 0.3 * (1.0 - contrast_score) + 0.2 * (1.0 - edge_score)
    haze_score = float(np.clip(haze_score, 0.0, 1.0))

    # --- 5. 映射到能见度（Koschmieder 经验离散化）---
    # haze_score → 消光系数 β ∈ [0.06(极清), 4.0(浓雾)]，V = 3.912 / β
    beta = 0.06 * math.exp(haze_score * math.log(4.0 / 0.06))
    visibility_km = 3.912 / beta
    visibility_km = float(np.clip(visibility_km, 0.0, config.VISIBILITY_MAX_KM))

    # --- 6. 等级标签（参考气象观测口径）---
    if visibility_km >= 20:
        level = "良好"
    elif visibility_km >= 10:
        level = "中等"
    elif visibility_km >= 4:
        level = "轻度霾/雾"
    else:
        level = "浓雾"

    # --- 7. 调试图：暗通道图（雾区亮 = 大气浑浊），便于在线调参 ---
    if output_dir:
        dark_vis = np.clip(dark_channel * 2, 0, 255).astype(np.uint8)
        dark_rgb = cv2.cvtColor(dark_vis, cv2.COLOR_GRAY2RGB)
        dark_rgb[measure_mask == 0] = 0  # 陆地与云置黑，与测量口径一致
        Image.fromarray(dark_rgb).save(os.path.join(output_dir, "05_dark_channel.png"))

    return {"visibilityKm": round(visibility_km, 2), "visibilityLevel": level,
            "hazeScore": round(haze_score, 4),
            "darkChannelBrightness": round(dark_brightness, 4),
            "contrastScore": round(contrast_score, 4),
            "edgeScore": round(edge_score, 4)}


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

    # --- 7. 能见度估算（依赖云量判据：云厚直接记 0，晴好按大气浑浊度反演）---
    # 传入云蒙版，让测量只在无云水面上进行，避免碎云纹理被误计为大气通透
    visibility_result = estimate_visibility(
        image_rgb=image_array,
        ocean_mask=ocean_mask,
        cloud_coverage=cloud_coverage,
        cloud_mask=final_cloud_mask,
        output_dir=output_dir,
    )

    return {
        "status": status,
        "seaBlueness": sea_blueness,
        "cloudCoverage": cloud_coverage,
        "bluenessIndex": blueness_index,
        "bluePercentage": blue_percentage,
        "yellowPercentage": yellow_percentage,
        "visibilityKm": visibility_result["visibilityKm"],
        "visibilityLevel": visibility_result["visibilityLevel"],
        "hazeScore": visibility_result["hazeScore"],
        "darkChannelBrightness": visibility_result["darkChannelBrightness"],
        "contrastScore": visibility_result["contrastScore"],
        "edgeScore": visibility_result["edgeScore"],
        "bluePixels": int(blue_pixels),
        "yellowPixels": int(yellow_pixels),
        "cloudPixels": int(cloud_pixels),
        "totalOceanPixels": int(total_ocean_pixels),
    }