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

# --- 2. HSV 颜色空间分类配置 ---
# 基于像素的颜色范围阈值法，取代 K-Means。
# 注意: OpenCV 中的 HSV 范围: H:[0, 179], S:[0, 255], V:[0, 255]
COLOR_CLASSIFICATION_HSV_RANGES = {
    # 云/白沫/高亮反光: 通常具有很低的饱和度(S)和很高的明度(V)。
    "CLOUD": {
        "lower": [0, 0, 128],
        "upper": [179, 40, 255]
    },
    # 蓝色的水体: 具有特定的蓝色色相(H)范围。
    "BLUE_WATER": {
        "lower": [85, 50, 50],
        "upper": [130, 255, 255]
    }
    # "黄色的水体" 将作为 "既不是云也不是蓝水" 的其他所有海洋像素的统称。
}

# --- 图像预处理配置 ---
# 在进行任何分析之前，对输入图像进行放大的倍率。
# 1.0 表示不进行任何缩放。
# 2.0 表示将图像的宽度和高度都放大到原来的2倍。
# 推荐使用高质量的 Bicubic 插值算法，以获得更好的效果。
PRE_ANALYSIS_SCALE_FACTOR = 2.0


# --- 定义调试图片的基准输出目录 ---
OUTPUT_BASE_DIR = "data/output"

# --- 调度器配置 ---
# 是否在应用启动时跳过第一次立即执行的分析任务
# 在 .env 文件中设置 SKIP_INITIAL_TASK=true 来启用
SKIP_INITIAL_TASK = str(os.getenv("SKIP_INITIAL_TASK", "false")).lower() in ('true', '1', 't')
# --- 图像预处理配置 ---
# 在进行任何分析之前，对输入图像进行放大的倍率。
# 1.0 表示不进行任何缩放。
# 2.0 表示将图像的宽度和高度都放大到原来的2倍。
# 推荐使用高质量的 Bicubic 插值算法，以获得更好的效果。
PRE_ANALYSIS_SCALE_FACTOR = 2.0