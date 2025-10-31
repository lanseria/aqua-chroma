# app/config.py

import os # 导入 os 模块

# --- 1. 定义所有可用数据源的配置 ---
DATA_SOURCES = {
    "LOCAL_SERVER": {
        "display_name": "本地GIS服务器",
        "timestamps_url": "http://bmcr1-wtr-r1:8080/zoom-earth-tiles/himawari/timestamps.json",
        "tile_url_template": "http://bmcr1-wtr-r1:8080/zoom-earth-tiles/himawari/{zoom}/{y}/{x}/{timestamp}.jpg",
        "timestamp_json_key": None
    },
    "ZOOM_EARTH": {
        "display_name": "Zoom.earth (Geocolor)",
        "timestamps_url": "https://tiles.zoom.earth/times/geocolor.json",
        "tile_url_template": "https://tiles.zoom.earth/geocolor/himawari/{date_str}/{time_str}/{zoom}/{y}/{x}.jpg",
        "timestamp_json_key": "himawari"
    }
}

# --- 2. 从环境变量中读取激活的数据源 ---
# #############################################################
# ##                                                         ##
# ##   数据源现在由环境变量 'ACTIVE_DATA_SOURCE' 控制。        ##
# ##   如果环境变量未设置，将使用下面的 'default' 值。         ##
# ##                                                         ##
# #############################################################
ACTIVE_DATA_SOURCE = os.getenv("ACTIVE_DATA_SOURCE", default="LOCAL_SERVER")


# --- 3. 根据激活的源，自动加载配置 ---
if ACTIVE_DATA_SOURCE not in DATA_SOURCES:
    raise ValueError(f"错误: 无效的数据源 '{ACTIVE_DATA_SOURCE}'。请从 {list(DATA_SOURCES.keys())} 中选择一个。")

ACTIVE_CONFIG = DATA_SOURCES[ACTIVE_DATA_SOURCE]
print(f"--- 系统已启动，当前使用的数据源: {ACTIVE_CONFIG['display_name']} ({ACTIVE_DATA_SOURCE}) ---")


# --- 通用配置 ---
ZOOM_LEVEL = 9
TARGET_AREA = {
    "north": 31.532,
    "south": 28.960,
    "west": 121.333,
    "east": 123.431
}
GEOJSON_PATH = "data/geojson/your_sea_area.geojson"
TIME_ZONE = "Asia/Shanghai"
NIGHT_START_HOUR = 18
NIGHT_END_HOUR = 6
CLOUD_THRESHOLD = 200
THICK_CLOUD_COVERAGE = 0.7
BLUE_LOWER_BOUND = [100, 40, 20]
BLUE_UPPER_BOUND = [140, 255, 255]
RESULTS_JSON_PATH = "data/analysis_results.json"