# app/processor.py

import os # 导入os模块
import math
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import ephem
import numpy as np
import cv2
from PIL import Image, ImageDraw # 导入Image、ImageDraw模块

from . import config, geo_utils, settings_store

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


def _haze_proxies(dark_channel: np.ndarray, gray: np.ndarray, local_mean: np.ndarray,
                  edges: np.ndarray, mask: np.ndarray) -> Optional[tuple]:
    """在给定掩码上聚合三项大气浑浊度代理量。掩码内无像素时返回 None。

    Returns:
        (dark_brightness, contrast_score, edge_score)，各分量均已归一化到 0~1。
    """
    if np.count_nonzero(mask) == 0:
        return None
    # 暗通道亮度（雾/云顶会显著抬亮最小通道）
    dark_brightness = float(dark_channel[mask > 0].mean()) / 255.0
    # 局部 RMS 对比度（经验归一：晴好海面约 8~12 灰阶，浓雾场景 < 2）
    rms_contrast = float(((gray - local_mean)[mask > 0] ** 2).mean() ** 0.5)
    contrast_score = min(1.0, rms_contrast / 10.0)
    # 细纹理边缘密度（经验归一：晴好海面边缘占比约 5%~8%，浓雾 < 0.5%）
    edges_ocean = cv2.bitwise_and(edges, edges, mask=mask)
    edge_score = min(1.0, _visibility_edge_density(edges_ocean, mask) / 0.05)
    return dark_brightness, contrast_score, edge_score


def estimate_visibility(image_rgb: np.ndarray, ocean_mask: np.ndarray,
                        cloud_mask: Optional[np.ndarray] = None,
                        output_dir: Optional[str] = None) -> Dict[str, Any]:
    """
    基于单张卫星图像估算海面水平能见度（公里）。

    原理：卫星图无法直接测能见度，改用大气光学代理量折算。
    - 浓雾低云：雾在图像上表现为"亮而平"的大面积覆盖——暗通道（最小通道）被雾抬亮、
      局部 RMS 对比度被压平、细纹理边缘消失。三者加权得到大气浑浊度得分。
    - 晴好天气：暗通道接近 0（水面阴影/暗色水体），对比度高、海面纹理（波痕、云影、
      岛屿轮廓）清晰，浑浊度得分低，能见度取高值。

    测量优先在"无云水面"上进行（cloud_mask 提供时剔除云及其边缘膨胀区），
    否则碎云自身的纹理会被误计为大气通透，导致能见度虚高。

    对云量不设"云厚直接记 0"的硬截断：无云水面占比低于
    config.VISIBILITY_MIN_CLEAR_FRACTION 时，按占比将"整个海洋区域（含云顶）"
    的测量结果线性混入。云顶亮而平，天然落在浑浊度高分端（低能见度），
    且随云量增减连续变化——输出曲线呈自然过渡而非 0/高值之间的跳变。

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

    gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY).astype(np.float64)

    # --- 1. 全图代理量底图（各掩码只做聚合，避免重复的卷积/边缘计算）---
    # 窗口取 15px：图像已超分放大 4 倍，15px 对应原分辨率上可观的局部邻域
    patch = 15
    min_channel = cv2.erode(image_rgb.min(axis=2), np.ones((patch, patch), np.uint8))
    dark_channel = cv2.dilate(min_channel, np.ones((3, 3), np.uint8))
    # 与暗通道同尺寸的局部均值图，供逐像素 RMS 对比度使用
    local_mean = cv2.boxFilter(gray, ddepth=-1, ksize=(patch, patch))
    edges = cv2.Canny(gray.astype(np.uint8), 40, 120)

    full_proxies = _haze_proxies(dark_channel, gray, local_mean, edges, ocean_mask)
    clear_proxies = full_proxies
    clear_fraction = 1.0

    # --- 2. 无云水面测量区（剔除云及边缘膨胀区，避免云的过渡像素混入）---
    if cloud_mask is not None:
        cloud_dilated = cv2.dilate(cloud_mask, np.ones((9, 9), np.uint8))
        measure_mask = cv2.bitwise_and(ocean_mask, cv2.bitwise_not(cloud_dilated))
        clear_pixels = np.count_nonzero(measure_mask)
        if clear_pixels > 0:
            clear_proxies = _haze_proxies(dark_channel, gray, local_mean, edges, measure_mask)
            clear_fraction = clear_pixels / total_ocean_pixels

    # --- 3. 按无云占比混合两套测量，让云量变化体现为能见度的连续过渡 ---
    # 无云水面充足（≥ VISIBILITY_MIN_CLEAR_FRACTION）时完全信任无云水面的测量；
    # 不足时按比例混入全场景（含云顶）测量：云越厚，权重越偏向云顶的低能见度读数。
    if clear_fraction >= config.VISIBILITY_MIN_CLEAR_FRACTION:
        w = 1.0
    else:
        w = clear_fraction / config.VISIBILITY_MIN_CLEAR_FRACTION
    dark_brightness, contrast_score, edge_score = (
        w * c + (1.0 - w) * f for c, f in zip(clear_proxies, full_proxies)
    )

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

    # --- 7. 调试图：暗通道图（雾/云区亮 = 大气浑浊），便于在线调参 ---
    if output_dir:
        dark_vis = np.clip(dark_channel * 2, 0, 255).astype(np.uint8)
        dark_rgb = cv2.cvtColor(dark_vis, cv2.COLOR_GRAY2RGB)
        dark_rgb[ocean_mask == 0] = 0  # 仅陆地置黑；云保留显示（云顶亮度参与浑浊度评估）
        Image.fromarray(dark_rgb).save(os.path.join(output_dir, "05_dark_channel.png"))

    return {"visibilityKm": round(visibility_km, 2), "visibilityLevel": level,
            "hazeScore": round(haze_score, 4),
            "darkChannelBrightness": round(dark_brightness, 4),
            "contrastScore": round(contrast_score, 4),
            "edgeScore": round(edge_score, 4)}


# --- 分类图着色（仅可视化，不参与判定与指标计算） ---

# 每个分类在图例中的中文方向说明；无中文字体时回退英文说明
_CLASS_LEGEND_LABELS = {
    "CLOUD": ("云层 薄→厚", "Cloud thin-thick"),
    "BLUE_WATER": ("海蓝 浅→深", "Blue pale-deep"),
    "YELLOW_WATER": ("海黄 淡→浓", "Yellow pale-dense"),
}


def _shade_intensities(hsv_image: np.ndarray, cloud_v_low: float,
                       water_value_range) -> Dict[str, np.ndarray]:
    """
    按明度 V 计算每个像素在其类别内部的色阶坐标 (0=色阶起点, 1=色阶终点)。

    色阶顺序与 config.palettes 一致：
    - CLOUD: V 从判定阈值的下界映射到 255，即 灰(薄云/碎云) → 白(厚云/浓云)；
    - 水体 : 用 water_value_range 把 V 映射为 "越暗越靠后"，即 浅 → 深
      （海蓝由浅青到深蓝，海黄由浅沙黄到深褐）。

    该坐标只用于选择色阶，不参与任何分类判定或指标计算。
    """
    v = hsv_image[:, :, 2].astype(np.float32)

    cloud_lo = float(cloud_v_low)
    cloud_u = (v - cloud_lo) / max(1.0, 255.0 - cloud_lo)

    water_dark, water_light = (float(x) for x in water_value_range)
    water_u = (water_light - v) / max(1.0, water_light - water_dark)

    return {
        "CLOUD": np.clip(cloud_u, 0.0, 1.0),
        "BLUE_WATER": np.clip(water_u, 0.0, 1.0),
        "YELLOW_WATER": np.clip(water_u, 0.0, 1.0),
    }


def _shade_palette_color(palette: np.ndarray, intensities: np.ndarray, mode: str) -> np.ndarray:
    """
    把 0~1 的深浅坐标映射成 RGB 颜色。

    mode="levels"  : 按坐标落入的档位取该档纯色（档与档之间界限清晰）；
    mode="gradient": 在相邻色阶之间线性插值（连续渐变）。
    """
    n = len(palette)
    if n == 1:
        return np.repeat(palette[:1], len(intensities), axis=0)

    if mode == "gradient":
        pos = intensities * (n - 1)
        lower = np.clip(np.floor(pos).astype(np.int32), 0, n - 2)
        frac = (pos - lower)[:, None]
        return palette[lower] * (1.0 - frac) + palette[lower + 1] * frac

    # levels: [0,1) 等分为 n 档，1.0 归入最后一档
    index = np.clip(np.floor(intensities * n).astype(np.int32), 0, n - 1)
    return palette[index]


def _append_legend(image_rgb: np.ndarray) -> np.ndarray:
    """
    在分类图下方追加色阶图例：每个分类一排由浅到深（由弱到强）的色块 + 方向说明。
    图例高度与字号按图像高度自适应；中文字体缺失时自动回退英文说明。
    """
    palettes = config.HSV_CLASSIFICATION_RENDER["palettes"]
    h, w = image_rgb.shape[:2]
    legend_h = max(28, int(round(h * 0.11)))
    font_size = max(9, int(round(h / 36)))

    canvas = Image.new("RGB", (w, h + legend_h), (30, 30, 34))
    canvas.paste(Image.fromarray(image_rgb), (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.line([(0, h), (w, h)], fill=(96, 96, 96), width=1)

    font = geo_utils.load_cjk_font(font_size, config.FONT_CANDIDATES)
    col_w = w / float(len(palettes))
    label_top = h + int(round(legend_h * 0.12))
    swatch_top = label_top + font_size + 3
    swatch_h = max(6, h + legend_h - swatch_top - int(round(legend_h * 0.14)))

    for i, (class_key, palette) in enumerate(palettes.items()):
        x0 = i * col_w
        label_cn, label_en = _CLASS_LEGEND_LABELS.get(class_key, (class_key, class_key))
        draw.text((x0 + 5, label_top), label_cn if font is not None else label_en,
                  fill=(235, 235, 235), font=font)

        pad = 5 if col_w >= 60 else max(1, int(col_w * 0.08))
        swatch_w = max(1.0, (col_w - pad * 2) / max(1, len(palette)))
        for j, color in enumerate(palette):
            sx0 = int(round(x0 + pad + j * swatch_w))
            sx1 = max(sx0, int(round(x0 + pad + (j + 1) * swatch_w)) - 1)
            draw.rectangle([sx0, swatch_top, sx1, swatch_top + swatch_h],
                           fill=tuple(int(c) for c in color))
    return np.array(canvas)


def render_classification_map(hsv_image: np.ndarray, class_masks: Dict[str, np.ndarray],
                              output_path: Optional[str] = None,
                              cloud_v_low: Optional[float] = None) -> np.ndarray:
    """
    绘制 HSV 分类结果图：先按类别取色，再在类别内部按深浅坐标取不同色阶。

    class_masks 决定像素归属（与原判定逻辑完全一致），着色只发生在已归类像素上，
    不会改变任何像素的分类结果或指标统计口径。

    - output_path 为 None 时只返回 RGB 数组、不写任何文件（供实时预览复用同一套着色逻辑）；
    - cloud_v_low 为云类别判定阈值的 V 下界（决定云层色阶起点），
      为 None 时取当前生效阈值（settings_store）。
    """
    if cloud_v_low is None:
        cloud_v_low = settings_store.get_hsv_ranges()["CLOUD"]["lower"][2]

    render_cfg = config.HSV_CLASSIFICATION_RENDER
    mode = str(render_cfg.get("mode", "levels")).lower()
    water_range = render_cfg.get("water_value_range", [20, 140])

    intensities = _shade_intensities(hsv_image, cloud_v_low, water_range)
    output = np.zeros(hsv_image.shape[:2] + (3,), dtype=np.uint8)

    for class_key, mask in class_masks.items():
        if not np.any(mask):
            continue
        palette = np.asarray(render_cfg["palettes"][class_key], dtype=np.float32)
        colors = _shade_palette_color(palette, intensities[class_key][mask > 0], mode)
        output[mask > 0] = np.clip(colors, 0, 255).astype(np.uint8)

    if render_cfg.get("legend", True):
        output = _append_legend(output)

    if output_path:
        Image.fromarray(output).save(output_path)
    return output


def classify_hsv(hsv_image: np.ndarray, ocean_mask: np.ndarray,
                 ranges: Dict[str, Any]) -> Dict[str, np.ndarray]:
    """
    按 HSV 阈值把海洋像素分为 云 / 蓝水 / 黄水 三类。

    优先级与生产口径完全一致：
    1. 先判断是否为云（必须同时在海洋区域内）；
    2. 在非云像素中判断是否为蓝水；
    3. 海洋区域内其余非云、非蓝水像素统称黄水。

    Returns:
        {"CLOUD": mask, "BLUE_WATER": mask, "YELLOW_WATER": mask}，均为 uint8 0/255 掩码。
    """
    cloud_lower = np.array(ranges["CLOUD"]["lower"])
    cloud_upper = np.array(ranges["CLOUD"]["upper"])
    blue_lower = np.array(ranges["BLUE_WATER"]["lower"])
    blue_upper = np.array(ranges["BLUE_WATER"]["upper"])

    # 1. 识别所有符合“云”颜色范围的像素
    cloud_mask_hsv = cv2.inRange(hsv_image, cloud_lower, cloud_upper)
    # 最终的云像素必须同时在海洋区域内和HSV颜色范围内
    final_cloud_mask = cv2.bitwise_and(cloud_mask_hsv, cloud_mask_hsv, mask=ocean_mask)

    # 2. 识别所有符合“蓝水”颜色范围的像素
    blue_mask_hsv = cv2.inRange(hsv_image, blue_lower, blue_upper)
    # 最终的蓝水像素必须在海洋区域内、在HSV颜色范围内，且【不是】云
    non_cloud_mask = cv2.bitwise_not(final_cloud_mask)
    final_blue_mask = cv2.bitwise_and(blue_mask_hsv, blue_mask_hsv, mask=non_cloud_mask)
    final_blue_mask = cv2.bitwise_and(final_blue_mask, final_blue_mask, mask=ocean_mask) # 再次确认在海洋区

    # 3. “黄水”是海洋区域内所有非云、非蓝水的像素
    non_cloud_blue_mask = cv2.bitwise_not(cv2.bitwise_or(final_cloud_mask, final_blue_mask))
    final_yellow_mask = cv2.bitwise_and(ocean_mask, non_cloud_blue_mask)

    return {
        "CLOUD": final_cloud_mask,
        "BLUE_WATER": final_blue_mask,
        "YELLOW_WATER": final_yellow_mask,
    }


def compute_ocean_color_metrics(image_array: np.ndarray, ocean_mask: np.ndarray,
                                class_masks: Dict[str, np.ndarray],
                                output_dir: Optional[str] = None,
                                verbose: bool = True) -> Dict[str, Any]:
    """
    基于分类掩码计算海蓝程度/云量/能见度等指标。

    生产分析（analyze_ocean_color）与阈值实验台预览（color_lab）共用本函数，
    保证两边指标口径完全一致。output_dir 为 None 时不写任何调试图
    （预览绝不落盘），verbose=False 时省略像素计数日志。
    """
    total_ocean_pixels = np.count_nonzero(ocean_mask)
    if total_ocean_pixels == 0:
        return {
            "status": "无数据", "seaBlueness": None, "cloudCoverage": None,
            "bluenessIndex": None, "bluePercentage": None, "yellowPercentage": None,
            "visibilityKm": None, "visibilityLevel": "无数据", "hazeScore": None,
            "darkChannelBrightness": None, "contrastScore": None, "edgeScore": None,
            "bluePixels": 0, "yellowPixels": 0, "cloudPixels": 0, "totalOceanPixels": 0,
        }

    # --- 1. 统计各类像素数量 ---
    final_cloud_mask = class_masks["CLOUD"]
    final_blue_mask = class_masks["BLUE_WATER"]
    final_yellow_mask = class_masks["YELLOW_WATER"]

    cloud_pixels = np.count_nonzero(final_cloud_mask)
    blue_pixels = np.count_nonzero(final_blue_mask)
    yellow_pixels = np.count_nonzero(final_yellow_mask)

    if verbose:
        print("\n--- [Processor] HSV Thresholding Pixel Count ---")
        print(f"  - Cloud Pixels      : {cloud_pixels}")
        print(f"  - Blue Water Pixels : {blue_pixels}")
        print(f"  - Yellow Water Pixels: {yellow_pixels}")
        print("--------------------------------------------------\n")

    # --- 2. 计算各项指标 ---
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

    # --- 3. 能见度估算（无云水面为主测量区；云顶测量按无云占比混入，输出连续过渡）---
    visibility_result = estimate_visibility(
        image_rgb=image_array,
        ocean_mask=ocean_mask,
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


def analyze_ocean_color(image_array: np.ndarray, ocean_mask: np.ndarray, output_dir: str, hsv_ranges_override: Optional[Dict] = None) -> Dict[str, Any]:
    """
    使用基于 HSV 颜色范围的阈值法对海洋图像进行分类和分析。
    分类判定（云 / 海蓝 / 海黄）保持不变；分类结果图 04_hsv_classification.png
    在类别内部按像素明度分出深浅色阶（见 config.HSV_CLASSIFICATION_RENDER），
    仅影响可视化，不改变指标口径。

    阈值来源（优先级从高到低）：
    1. hsv_ranges_override：调试工具传入的临时阈值；
    2. settings_store 的生效阈值：数据库 app_settings 表（网页 /tools/color_thresholds
       保存的值）优先，数据库无有效记录时回退 config.DEFAULT_COLOR_CLASSIFICATION_HSV_RANGES。
       因此页面上调整阈值后，生产分析下一次运行立即使用新值，无需重启。
    """
    total_ocean_pixels = np.count_nonzero(ocean_mask)
    if total_ocean_pixels == 0:
        # 即使没有海洋像素，也写出一张全黑分类图（附图例），保证输出目录的产物文件齐全
        blank = np.zeros_like(image_array, dtype=np.uint8)
        if config.HSV_CLASSIFICATION_RENDER.get("legend", True):
            blank = _append_legend(blank)
        Image.fromarray(blank).save(
            os.path.join(output_dir, "04_hsv_classification.png")
        )
        return {"status": "无数据", "seaBlueness": None, "cloudCoverage": None, "bluenessIndex": None, "bluePercentage": None, "yellowPercentage": None}

    # --- 1. 转换到 HSV 颜色空间 ---
    hsv_image = cv2.cvtColor(image_array, cv2.COLOR_RGB2HSV)

    # --- 2. 确定本次使用的 HSV 阈值 ---
    # 优先使用传入的 hsv_ranges_override，否则使用运行时设置（数据库优先，默认值兜底）
    if hsv_ranges_override is not None:
        ranges = hsv_ranges_override
        ranges_source = "override"
    else:
        ranges = settings_store.get_hsv_ranges()
        # 与 get_hsv_ranges 共用同一份缓存，仅用于日志标注阈值来源
        ranges_source = settings_store.get_state()["source"]
    print(f"--- [Processor] Using HSV Ranges (source={ranges_source}): {ranges} ---")

    # --- 3. 像素分类（云 > 蓝水 > 黄水 的优先级逻辑见 classify_hsv）---
    class_masks = classify_hsv(hsv_image, ocean_mask, ranges)

    # --- 4. 生成并保存分类调试图 ---
    # 判定结果（掩码）不变，仅着色方式改变：每个类别内部按像素明度细分深浅色阶，
    # 不再用 3 种纯色平涂。着色与图例参数见 config.HSV_CLASSIFICATION_RENDER。
    render_classification_map(
        hsv_image=hsv_image,
        class_masks=class_masks,
        output_path=os.path.join(output_dir, "04_hsv_classification.png"),
        cloud_v_low=ranges["CLOUD"]["lower"][2],
    )

    # --- 5. 计算各项指标（与实时预览共用同一实现）---
    return compute_ocean_color_metrics(
        image_array=image_array,
        ocean_mask=ocean_mask,
        class_masks=class_masks,
        output_dir=output_dir,
        verbose=True,
    )