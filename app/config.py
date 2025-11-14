# app/config.py

import os
from dotenv import load_dotenv

load_dotenv()

# --- Database Configuration ---
DB_USER = os.getenv("DB_USER", "user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "password")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "aqua_chroma")
DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

COMMON_HEADERS = {
    'accept': '*/*',
    'accept-language': 'zh-CN,zh;q=0.9',
    'origin': 'https://zoom.earth',
    'referer': 'https://zoom.earth/',
    'sec-ch-ua': '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Windows"',
    'sec-fetch-dest': 'empty',
    'sec-fetch-mode': 'cors',
    'sec-fetch-site': 'same-site',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36',
}

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
ACTIVE_DATA_SOURCE = os.getenv("ACTIVE_DATA_SOURCE", default="ZOOM_EARTH")


# --- 3. 根据激活的源，自动加载配置 ---
if ACTIVE_DATA_SOURCE not in DATA_SOURCES:
    raise ValueError(f"错误: 无效的数据源 '{ACTIVE_DATA_SOURCE}'。请从 {list(DATA_SOURCES.keys())} 中选择一个。")

ACTIVE_CONFIG = DATA_SOURCES[ACTIVE_DATA_SOURCE]
print(f"--- 系统已启动，当前使用的数据源: {ACTIVE_CONFIG['display_name']} ({ACTIVE_DATA_SOURCE}) ---")


# --- 通用配置 ---
ZOOM_LEVEL = 7
TARGET_AREA = {
    "north": 31.168,
    "south": 29.609,
    "west": 121.102,
    "east": 122.871
}
GEOJSON_PATH = "geojson/china.geojson"
TIME_ZONE = "Asia/Shanghai"
NIGHT_START_HOUR = 16
NIGHT_END_HOUR = 7
CLOUD_THRESHOLD = 200
# 定义判定为“云层过厚”的云量覆盖阈值 (50%)
CLOUD_COVERAGE_THRESHOLD = 0.5

# --- 2. K-Means 聚类配置 ---
# K-Means 算法将寻找 K 个主要颜色簇：蓝水、黄水、云层。
K_MEANS_CLUSTERS = 3
# 用于识别“云/白沫”簇。饱和度(S)低于此值且明度(V)高于此值的被认为是云。
CLOUD_CLUSTER_SATURATION_MAX = 50
CLOUD_CLUSTER_VALUE_MIN = 180
# 用于在剩下的非云簇中识别“蓝色水体”簇。Hue(H)值大于此阈值的被认为是蓝色。
BLUE_CLUSTER_HUE_THRESHOLD = 80


# --- 定义调试图片的基准输出目录 ---
OUTPUT_BASE_DIR = "data/output"

# --- 调度器配置 ---
# 是否在应用启动时跳过第一次立即执行的分析任务
# 在 .env 文件中设置 SKIP_INITIAL_TASK=true 来启用
SKIP_INITIAL_TASK = str(os.getenv("SKIP_INITIAL_TASK", "false")).lower() in ('true', '1', 't')