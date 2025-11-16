# app/processor.py

import os # 导入os模块
import numpy as np
import cv2
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional
from PIL import Image # 导入Image模块
from . import config

def is_night(timestamp: int) -> bool:
    """根据时间戳和时区判断是否为黑夜"""
    tz = timezone(timedelta(hours=8)) # 假设为北京时间
    dt_local = datetime.fromtimestamp(timestamp, tz=tz)
    return not (config.NIGHT_END_HOUR <= dt_local.hour < config.NIGHT_START_HOUR)

def analyze_ocean_color(image_array: np.ndarray, ocean_mask: np.ndarray, output_dir: str, hsv_ranges_override: Optional[Dict] = None) -> Dict[str, Any]:
    """
    使用基于 HSV 颜色范围的阈值法对海洋图像进行分类和分析。
    新增: hsv_ranges_override 参数，用于接收临时的HSV阈值。
    """
    total_ocean_pixels = np.count_nonzero(ocean_mask)
    if total_ocean_pixels == 0:
        return {"status": "无数据", "seaBlueness": 0.0, "cloudCoverage": 0.0, "bluePercentage": 0.0, "yellowPercentage": 0.0}

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
    classification_map_bgr[final_yellow_mask > 0] = (1, 25, 70)  # 棕色 (BGR)
    classification_map_rgb = cv2.cvtColor(classification_map_bgr, cv2.COLOR_BGR2RGB)
    Image.fromarray(classification_map_rgb).save(os.path.join(output_dir, "04_hsv_classification.png"))

    # --- 6. 计算各项指标 ---
    # 修复：sea_blueness_score 的分母应该是总的海洋像素，而不仅仅是可见水体像素。
    # 这确保了云层覆盖率会正确地降低海蓝分数。
    sea_blueness_score = (blue_pixels / total_ocean_pixels) if total_ocean_pixels > 0 else 0.0
    
    cloud_coverage = cloud_pixels / total_ocean_pixels if total_ocean_pixels > 0 else 0.0
    blue_percentage = blue_pixels / total_ocean_pixels if total_ocean_pixels > 0 else 0.0 # 该指标与sea_blueness现在一致
    yellow_percentage = yellow_pixels / total_ocean_pixels if total_ocean_pixels > 0 else 0.0

    return {
        "status": "completed",
        "seaBlueness": sea_blueness_score,
        "cloudCoverage": cloud_coverage,
        "bluePercentage": blue_percentage,
        "yellowPercentage": yellow_percentage,
        "bluePixels": int(blue_pixels),
        "yellowPixels": int(yellow_pixels),
        "cloudPixels": int(cloud_pixels),
    }

def dehaze_dark_channel(image_bgr: np.ndarray, patch_size: int = 15, omega: float = 0.95, t0: float = 0.1) -> np.ndarray:
    """
    使用暗通道先验算法对图像进行去雾处理。
    :param image_bgr: 输入的BGR格式图像 (OpenCV默认格式)。
    :param patch_size: 用于计算暗通道的窗口大小。
    :param omega: 保留的雾的比例，用于更自然的效果。
    :param t0: 透射率的下限，防止结果过暗。
    :return: 去雾后的BGR格式图像。
    """
    print("[Processor] Starting dehazing process using Dark Channel Prior...")
    
    # 1. 将图像转换为float类型，并归一化到[0, 1]
    img_float = image_bgr.astype('float64') / 255

    # 2. 计算暗通道
    # 2.1 找到每个像素的最小颜色通道值
    min_channel_img = np.min(img_float, axis=2)
    # 2.2 使用一个矩形核在最小通道图上进行腐蚀操作，等效于在patch内取最小值
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (patch_size, patch_size))
    dark_channel = cv2.erode(min_channel_img, kernel)

    # 3. 估算大气光 A
    # 将暗通道图像扁平化
    flat_dark = dark_channel.ravel()
    # 找到暗通道中最亮的0.1%像素的索引
    search_idx = (-flat_dark).argsort()[:int(flat_dark.size * 0.001)]
    # 将索引转换回二维坐标
    rows, cols = np.unravel_index(search_idx, dark_channel.shape)
    
    A = np.zeros(3)
    # 在原始图像中找到这些最亮像素，并取其平均值作为大气光
    for i in range(3):
        A[i] = np.mean(img_float[rows, cols, i])

    # 4. 估算透射率 t(x)
    transmission = 1 - omega * dark_channel / np.max(A)
    # 对透射率进行限幅，防止其值过小导致图像过曝
    transmission = np.maximum(transmission, t0)

    # 5. 恢复无雾图像 J(x)
    dehazed_img = np.empty(img_float.shape, img_float.dtype)
    for i in range(3):
        dehazed_img[:, :, i] = (img_float[:, :, i] - A[i]) / transmission + A[i]

    # 将结果裁剪到[0, 1]范围，并转换回uint8格式
    dehazed_img = np.clip(dehazed_img, 0, 1)
    dehazed_img = (dehazed_img * 255).astype(np.uint8)
    
    print("[Processor] Dehazing process completed.")
    return dehazed_img