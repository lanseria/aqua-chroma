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

def analyze_ocean_color(image_array: np.ndarray, ocean_mask: np.ndarray, output_dir: str) -> Dict[str, Any]:
    """
    使用优化的全局 K-Means (在缩放图像上运行) 进行分析，并提供详细的调试信息。
    """
    original_h, original_w = image_array.shape[:2]

    if np.count_nonzero(ocean_mask) == 0:
        return {"status": "无数据", "seaBlueness": 0.0, "cloudCoverage": 0.0, "bluePercentage": 0.0, "yellowPercentage": 0.0}

    target_width = 500
    scale = target_width / original_w
    small_image = cv2.resize(image_array, (target_width, int(original_h * scale)))
    small_mask = cv2.resize(ocean_mask, (target_width, int(original_h * scale)), interpolation=cv2.INTER_NEAREST)

    ocean_pixels = small_image[small_mask > 0]
    total_ocean_pixels_small = ocean_pixels.shape[0]
    if total_ocean_pixels_small < config.K_MEANS_CLUSTERS:
        return {"status": "像素不足", "seaBlueness": 0.0, "cloudCoverage": 0.0, "bluePercentage": 0.0, "yellowPercentage": 0.0}

    pixels_float = np.float32(ocean_pixels)
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 10, 1.0)
    _, labels, centers_rgb = cv2.kmeans(
        pixels_float, config.K_MEANS_CLUSTERS, None, criteria, 10, cv2.KMEANS_RANDOM_CENTERS
    )
    centers_rgb = np.uint8(centers_rgb)
    centers_hsv = cv2.cvtColor(centers_rgb.reshape(1, -1, 3), cv2.COLOR_RGB2HSV).reshape(-1, 3)

    # --- 5. 智能识别 + 详细打印 (新版) ---
    print("\n--- [Processor] K-Means Cluster Analysis ---")
    blue_label, yellow_label, cloud_label = -1, -1, -1
    
    # 创建一个身份列表，用于追踪每个簇的识别结果
    identities = ["Undetermined"] * config.K_MEANS_CLUSTERS
    unassigned_labels = list(range(config.K_MEANS_CLUSTERS))

    # 5.1 第一轮：识别云/白沫
    for i in unassigned_labels[:]: # 使用副本进行迭代，以便安全地修改原列表
        s, v = centers_hsv[i][1], centers_hsv[i][2]
        if s < config.CLOUD_CLUSTER_SATURATION_MAX and v > config.CLOUD_CLUSTER_VALUE_MIN:
            cloud_label = i
            identities[i] = "Cloud/White"
            unassigned_labels.remove(i)
            break # 通常只有一个云簇，找到就跳出

    # 5.2 第二轮：在剩下的簇中识别蓝水
    for i in unassigned_labels[:]:
        h = centers_hsv[i][0]
        if h > config.BLUE_CLUSTER_HUE_THRESHOLD:
            blue_label = i
            identities[i] = "Blue Water"
            unassigned_labels.remove(i)
            break # 通常只有一个蓝水簇

    # 5.3 第三轮：剩下的最后一个簇被认为是黄水
    if len(unassigned_labels) == 1:
        yellow_label = unassigned_labels[0]
        identities[yellow_label] = "Yellow Water"

    # 5.4 打印最终识别结果
    for i in range(config.K_MEANS_CLUSTERS):
        print(f"  - Cluster {i}: RGB={centers_rgb[i]}, HSV={centers_hsv[i]} -> Identified as: {identities[i]}")


    # --- 6. 统计各类像素数量 + 详细打印 ---
    pixel_counts = np.bincount(labels.flatten())
    blue_pixels = int(pixel_counts[blue_label]) if blue_label != -1 and blue_label < len(pixel_counts) else 0
    yellow_pixels = int(pixel_counts[yellow_label]) if yellow_label != -1 and yellow_label < len(pixel_counts) else 0
    cloud_pixels = int(pixel_counts[cloud_label]) if cloud_label != -1 and cloud_label < len(pixel_counts) else 0

    print("\n--- [Processor] Pixel Count Summary ---")
    print(f"  - Blue Pixels   : {blue_pixels} (Label: {blue_label})")
    print(f"  - Yellow Pixels : {yellow_pixels} (Label: {yellow_label})")
    print(f"  - Cloud Pixels  : {cloud_pixels} (Label: {cloud_label})")
    print("-----------------------------------------\n")


    # --- 7. 生成并保存分类调试图 ---
    segmented_pixels = centers_rgb[labels.flatten()]
    small_classification_map = np.zeros_like(small_image)
    small_classification_map[small_mask > 0] = segmented_pixels
    full_classification_map = cv2.resize(
        small_classification_map, (original_w, original_h), interpolation=cv2.INTER_NEAREST
    )
    Image.fromarray(full_classification_map).save(os.path.join(output_dir, "04_kmeans_classification.png"))

    # --- 8. 计算各项指标 ---
    total_water_pixels = blue_pixels + yellow_pixels
    sea_blueness_score = (blue_pixels / total_water_pixels) if total_water_pixels > 0 else 0.0
    
    cloud_coverage = cloud_pixels / total_ocean_pixels_small
    blue_percentage = blue_pixels / total_ocean_pixels_small
    yellow_percentage = yellow_pixels / total_ocean_pixels_small

    return {
        "status": "completed",
        "seaBlueness": sea_blueness_score,
        "cloudCoverage": cloud_coverage,
        "bluePercentage": blue_percentage,
        "yellowPercentage": yellow_percentage,
        # 新增: 返回原始像素计数值
        "bluePixels": blue_pixels,
        "yellowPixels": yellow_pixels,
        "cloudPixels": cloud_pixels,
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