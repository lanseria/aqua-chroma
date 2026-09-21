# app/geo_utils.py

import json
import math
import os
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# --- 新的坐标转换函数，用于裁剪后的图像 ---

def mercator_y(lat_deg: float) -> float:
    """将纬度转换为墨卡托Y坐标 (用于线性插值)。"""
    lat_rad = math.radians(lat_deg)
    return math.log(math.tan((math.pi / 4) + (lat_rad / 2)))

def latlon_to_final_pixel(lat: float, lon: float, bounds: Dict[str, float], image_shape: Tuple[int, int]) -> Tuple[int, int]:
    """
    将经纬度转换为相对于【最终裁剪图】的像素坐标。
    """
    height, width = image_shape[:2]

    # 经度是线性映射的
    lon_fraction = (lon - bounds['west']) / (bounds['east'] - bounds['west'])
    pixel_x = int(lon_fraction * width)

    # 纬度在墨卡托投影下不是线性的，需要转换后再进行线性插值
    y_merc_north = mercator_y(bounds['north'])
    y_merc_south = mercator_y(bounds['south'])
    y_merc_lat = mercator_y(lat)

    lat_fraction = (y_merc_lat - y_merc_north) / (y_merc_south - y_merc_north)
    pixel_y = int(lat_fraction * height)

    return pixel_x, pixel_y

# --- GeoJSON 加载 (带缓存，避免批量处理时重复解析) ---

_geojson_cache: Dict[str, List[list]] = {}

def _load_geojson_polygons(geojson_path: str) -> List[list]:
    """加载 GeoJSON 并返回多边形列表 (MultiPolygon 与 Polygon 统一为嵌套结构)。结果按路径缓存。"""
    cached = _geojson_cache.get(geojson_path)
    if cached is not None:
        return cached

    if not os.path.exists(geojson_path):
        raise FileNotFoundError(f"GeoJSON文件未找到: {geojson_path}")

    with open(geojson_path, 'r', encoding='utf-8') as f:
        geojson_data = json.load(f)

    polygons: List[list] = []
    for feature in geojson_data['features']:
        geom = feature['geometry']
        coords = geom['coordinates'] if geom['type'] == 'MultiPolygon' else [geom['coordinates']]
        polygons.extend(coords)

    _geojson_cache[geojson_path] = polygons
    return polygons

# --- 核心蒙版创建函数 (使用新的坐标转换) ---

def create_ocean_mask(image_shape: Tuple[int, int], geojson_path: str, bounds: Dict[str, float]) -> np.ndarray:
    """
    根据GeoJSON文件在【最终裁剪图】上创建一个精确的海洋蒙版。
    对完全落在目标范围外的多边形做外包矩形预过滤，减少无效投影计算。
    """
    height, width = image_shape[:2]
    mask = np.full((height, width), 255, dtype=np.uint8)

    for polygon in _load_geojson_polygons(geojson_path):
        # 外包矩形预过滤：跳过与目标范围完全不相交的多边形
        pts = [pt for ring in polygon for pt in ring]
        if max(p[0] for p in pts) < bounds['west'] or min(p[0] for p in pts) > bounds['east'] \
                or max(p[1] for p in pts) < bounds['south'] or min(p[1] for p in pts) > bounds['north']:
            continue

        for ring in polygon:
            # 使用新的转换函数
            pixel_coords = [latlon_to_final_pixel(lat, lon, bounds, image_shape) for lon, lat in ring]

            pts = np.array(pixel_coords, dtype=np.int32)
            cv2.fillPoly(mask, [pts], 0)

    print(f"已成功从 '{geojson_path}' 创建海洋蒙版。")
    return mask

def apply_mask(image: Image.Image, mask: np.ndarray) -> np.ndarray:
    """将蒙版应用到图像上，裁剪掉陆地部分。"""
    cv_image = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
    if len(mask.shape) > 2:
        mask = cv2.cvtColor(mask, cv2.COLOR_BGR2GRAY)
    masked_image_bgr = cv2.bitwise_and(cv_image, cv_image, mask=mask)
    masked_image_rgb = cv2.cvtColor(masked_image_bgr, cv2.COLOR_BGR2RGB)
    return masked_image_rgb

# --- 地图标注 (陆地描边 + 城市点位) ---

def draw_land_outline(
    image_rgb: np.ndarray,
    ocean_mask: np.ndarray,
    color: Tuple[int, int, int] = (255, 214, 0),
    thickness: int = 1,
    halo_color: Tuple[int, int, int] = (0, 0, 0),
    halo_thickness: int = 3,
) -> np.ndarray:
    """
    沿陆地边界绘制描边 (可带光晕)，返回新的 RGB 数组。
    陆地轮廓直接从海洋蒙版 (mask=0 为陆地) 提取，天然贴合当前视野内的海岸线。
    颜色参数使用 RGB 元组，内部按 OpenCV 要求转为 BGR。
    """
    land_mask = cv2.bitwise_not(ocean_mask)
    contours, _ = cv2.findContours(land_mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    out_bgr = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR)
    if halo_thickness > 0:
        cv2.drawContours(out_bgr, contours, -1, halo_color[::-1], halo_thickness)
    cv2.drawContours(out_bgr, contours, -1, color[::-1], thickness)
    return cv2.cvtColor(out_bgr, cv2.COLOR_BGR2RGB)

_font_cache: Dict[int, Optional[ImageFont.FreeTypeFont]] = {}

def load_cjk_font(size: int, candidates: Optional[List[str]] = None) -> Optional[ImageFont.FreeTypeFont]:
    """
    按顺序加载第一个存在的中文字体文件；全部缺失时返回 None (调用方跳过文字绘制)。
    """
    size = max(8, int(size))
    if size in _font_cache:
        return _font_cache[size]

    font = None
    for path in candidates or []:
        if path and os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size)
                break
            except OSError:
                continue
    if font is None:
        print(f"[geo_utils] 未找到可用中文字体，城市名称标注将跳过。尝试路径: {candidates}")
    _font_cache[size] = font
    return font

def draw_city_markers(
    image_rgb: np.ndarray,
    bounds: Dict[str, float],
    cities: List[Dict],
    marker_color: Tuple[int, int, int] = (255, 45, 45),
    marker_radius: int = 3,
    label_color: Tuple[int, int, int] = (25, 25, 25),
    label_stroke: int = 2,
    font: Optional[ImageFont.FreeTypeFont] = None,
) -> np.ndarray:
    """
    在图上绘制城市点位 (白环圆点) 与中文名称标注，返回新的 RGB 数组。
    超出图像可视范围的点位自动跳过；标注位置依次尝试右侧、左侧、下方、上方，
    与已放置的标签重叠时自动换位，避免相邻城市名称互相遮挡。
    """
    height, width = image_rgb.shape[:2]
    pil_image = Image.fromarray(image_rgb)
    draw = ImageDraw.Draw(pil_image)
    placed_boxes: List[Tuple[float, float, float, float]] = []

    for city in cities:
        name = city.get('name', '')
        x, y = latlon_to_final_pixel(city['lat'], city['lon'], bounds, image_rgb.shape)
        if not (0 <= x < width and 0 <= y < height):
            continue

        # 点位：白色外环 + 实心圆点
        if marker_radius > 0:
            r = marker_radius
            draw.ellipse([x - r - 1, y - r - 1, x + r + 1, y + r + 1], fill=(255, 255, 255))
            draw.ellipse([x - r, y - r, x + r, y + r], fill=marker_color)

        # 名称标注：依次尝试右侧/左侧/下方/上方，跳过与已有标签重叠的位置
        if font is not None and name:
            offset = marker_radius + 3
            candidates = [
                ('lm', x + offset, y),
                ('rm', x - offset, y),
                ('mt', x, y + offset + 1),
                ('mb', x, y - offset - 1),
            ]
            best = candidates[0]
            for anchor, tx, ty in candidates:
                box = draw.textbbox((tx, ty), name, font=font, anchor=anchor, stroke_width=label_stroke)
                # 只接受完整落在图幅内且不与已有标签重叠的位置
                if box[0] >= 0 and box[1] >= 0 and box[2] <= width and box[3] <= height \
                        and not any(_boxes_overlap(box, other) for other in placed_boxes):
                    best = (anchor, tx, ty)
                    break
            draw.text((best[1], best[2]), name, font=font, fill=label_color,
                      stroke_width=label_stroke, stroke_fill=(255, 255, 255), anchor=best[0])
            placed_boxes.append(draw.textbbox((best[1], best[2]), name, font=font,
                                              anchor=best[0], stroke_width=label_stroke))

    return np.array(pil_image)

def _boxes_overlap(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> bool:
    """判断两个 (left, top, right, bottom) 边界框是否相交 (含 1px 间隔)。"""
    return a[0] < b[2] + 1 and a[2] + 1 > b[0] and a[1] < b[3] + 1 and a[3] + 1 > b[1]
