# app/geo_utils.py

import json
import math
import os
from typing import Tuple, Dict

import cv2
import numpy as np
from PIL import Image

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

# --- 核心蒙版创建函数 (使用新的坐标转换) ---

def create_ocean_mask(image_shape: Tuple[int, int], geojson_path: str, bounds: Dict[str, float]) -> np.ndarray:
    """
    根据GeoJSON文件在【最终裁剪图】上创建一个精确的海洋蒙版。
    """
    if not os.path.exists(geojson_path):
        raise FileNotFoundError(f"GeoJSON文件未找到: {geojson_path}")

    mask = np.full(image_shape[:2], 255, dtype=np.uint8)

    with open(geojson_path, 'r', encoding='utf-8') as f:
        geojson_data = json.load(f)

    for feature in geojson_data['features']:
        geom = feature['geometry']
        polygons = geom['coordinates'] if geom['type'] == 'MultiPolygon' else [geom['coordinates']]
        
        for polygon in polygons:
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
