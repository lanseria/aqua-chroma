# app/downloader.py

import math
from io import BytesIO
from typing import Optional, Tuple
from datetime import datetime, timezone

import requests
from PIL import Image

from . import config

def deg_to_tile_num(lat_deg: float, lon_deg: float, zoom: int) -> Tuple[int, int]:
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    xtile = int((lon_deg + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return xtile, ytile

def latlon_to_pixel_on_stitched(lat: float, lon: float, zoom: int, x_tile_min: int, y_tile_min: int) -> Tuple[int, int]:
    map_size = 256 * (2 ** zoom)
    world_pixel_x = (lon + 180) / 360 * map_size
    sin_lat = math.sin(math.radians(lat))
    world_pixel_y = (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * map_size
    offset_x = x_tile_min * 256
    offset_y = y_tile_min * 256
    local_pixel_x = int(round(world_pixel_x - offset_x))
    local_pixel_y = int(round(world_pixel_y - offset_y))
    return local_pixel_x, local_pixel_y

def download_stitched_image(timestamp: int) -> Optional[Image.Image]:
    zoom = config.ZOOM_LEVEL
    bounds = config.TARGET_AREA

    x_min, y_min = deg_to_tile_num(bounds["north"], bounds["west"], zoom)
    x_max, y_max = deg_to_tile_num(bounds["south"], bounds["east"], zoom)
    x_range, y_range = range(x_min, x_max + 1), range(y_min, y_max + 1)
    
    tile_size = 256
    stitched_image = Image.new('RGB', (len(x_range) * tile_size, len(y_range) * tile_size))
    downloaded_count = 0
    total_tiles = len(x_range) * len(y_range)

    # 1. 从配置中获取URL模板
    tile_template = config.ACTIVE_CONFIG["tile_url_template"]

    print(f"开始下载时间戳 {timestamp} 的图像...")
    for i, y in enumerate(y_range):
        for j, x in enumerate(x_range):
            # 2. 根据当前激活的数据源，动态构建URL
            if config.ACTIVE_DATA_SOURCE == "ZOOM_EARTH":
                # 对于Zoom.earth，需要将时间戳转换为 YYYY-MM-DD 和 HHMM 格式
                dt_object = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                date_str = dt_object.strftime('%Y-%m-%d')
                time_str = dt_object.strftime('%H%M')
                # https://tiles.zoom.earth/geocolor/himawari/2025-10-31/2330/6/29/49.jpg
                tile_url = tile_template.format(date_str=date_str, time_str=time_str, zoom=zoom, y=y, x=x)
            else: # 默认为 "LOCAL_SERVER" 或其他类似格式
                tile_url = tile_template.format(timestamp=timestamp, zoom=zoom, y=y, x=x)
            print(f"正在下载 {tile_url}...")
            try:
                res = requests.get(tile_url, timeout=5, headers=config.COMMON_HEADERS)
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

    px_west, px_north = latlon_to_pixel_on_stitched(bounds['north'], bounds['west'], zoom, x_min, y_min)
    px_east, px_south = latlon_to_pixel_on_stitched(bounds['south'], bounds['east'], zoom, x_min, y_min)
    crop_box = (px_west, px_north, px_east, px_south)
    cropped_image = stitched_image.crop(crop_box)
    
    print(f"裁剪后最终图像尺寸: {cropped_image.size}")
    return cropped_image