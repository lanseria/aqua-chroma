# app/downloader.py

import math
from io import BytesIO
from typing import Optional, Tuple

import requests
from PIL import Image

from . import config

# --- 坐标转换函数 (模块内部使用) ---

def deg_to_tile_num(lat_deg: float, lon_deg: float, zoom: int) -> Tuple[int, int]:
    """将经纬度转换为瓦片坐标。"""
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    xtile = int((lon_deg + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return xtile, ytile

def latlon_to_pixel_on_stitched(lat: float, lon: float, zoom: int, x_tile_min: int, y_tile_min: int) -> Tuple[int, int]:
    """将经纬度转换为相对于【瓦片拼接大图】左上角的像素坐标。"""
    map_size = 256 * (2 ** zoom)
    world_pixel_x = (lon + 180) / 360 * map_size
    sin_lat = math.sin(math.radians(lat))
    world_pixel_y = (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * map_size

    offset_x = x_tile_min * 256
    offset_y = y_tile_min * 256

    local_pixel_x = int(round(world_pixel_x - offset_x))
    local_pixel_y = int(round(world_pixel_y - offset_y))
    
    return local_pixel_x, local_pixel_y

# --- 更新后的下载与裁剪函数 ---

def download_stitched_image(timestamp: int) -> Optional[Image.Image]:
    """
    根据时间戳，下载、拼接并【精确裁剪】目标区域的卫星图像。
    """
    zoom = config.ZOOM_LEVEL
    bounds = config.TARGET_AREA

    # 1. 计算需要下载的瓦片范围
    x_min, y_min = deg_to_tile_num(bounds["north"], bounds["west"], zoom)
    x_max, y_max = deg_to_tile_num(bounds["south"], bounds["east"], zoom)
    x_range, y_range = range(x_min, x_max + 1), range(y_min, y_max + 1)
    
    # 2. 下载并拼接成一个大的、未裁剪的图像
    tile_size = 256
    stitched_image = Image.new('RGB', (len(x_range) * tile_size, len(y_range) * tile_size))
    downloaded_count = 0
    total_tiles = len(x_range) * len(y_range)

    print(f"开始下载时间戳 {timestamp} 的图像...")
    for i, y in enumerate(y_range):
        for j, x in enumerate(x_range):
            tile_url = f"{config.GIS_SERVER_URL}/himawari/{zoom}/{y}/{x}/{timestamp}.jpg"
            try:
                res = requests.get(tile_url, timeout=5)
                if res.status_code == 200:
                    tile_image = Image.open(BytesIO(res.content))
                    stitched_image.paste(tile_image, (j * tile_size, i * tile_size))
                    downloaded_count += 1
                else:
                    stitched_image.paste(Image.new('RGB', (tile_size, tile_size), color='black'), (j * tile_size, i * tile_size))
            except requests.exceptions.RequestException:
                stitched_image.paste(Image.new('RGB', (tile_size, tile_size), color='black'), (j * tile_size, i * tile_size))

    print(f"下载完成。成功率: {downloaded_count}/{total_tiles}")
    
    if downloaded_count == 0:
        return None

    # 3. --- 新增：精确裁剪 ---
    # 计算 TARGET_AREA 四个角在拼接大图上的像素坐标
    px_west, px_north = latlon_to_pixel_on_stitched(bounds['north'], bounds['west'], zoom, x_min, y_min)
    px_east, px_south = latlon_to_pixel_on_stitched(bounds['south'], bounds['east'], zoom, x_min, y_min)
    
    # 定义裁剪框 (left, upper, right, lower)
    crop_box = (px_west, px_north, px_east, px_south)
    
    print(f"原始拼接图尺寸: {stitched_image.size}")
    print(f"计算出的裁剪框 (像素): {crop_box}")

    # 执行裁剪
    cropped_image = stitched_image.crop(crop_box)
    
    print(f"裁剪后最终图像尺寸: {cropped_image.size}")
    
    return cropped_image