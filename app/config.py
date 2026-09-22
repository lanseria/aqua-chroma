# app/config.py

import os
from dotenv import load_dotenv

load_dotenv()

# --- Database Configuration ---
DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    raise ValueError("错误: 未设置 DATABASE_URL 环境变量（格式如 postgresql://user:password@host:5432/dbname）")

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
# --- 动态日照分析配置 ---
# 用于计算日出日落的观测点坐标 (用户指定: [122.2, 30])
MONITOR_LON = "122.2"
MONITOR_LAT = "30"
# 日出后/日落前的缓冲时间（小时），在此时间段内才进行分析
DAYTIME_BUFFER_HOURS = 2

CLOUD_THRESHOLD = 200
# 定义判定为“云层过厚”的云量覆盖阈值 (50%)
CLOUD_COVERAGE_THRESHOLD = 0.5

# --- 瓦片下载质量配置 ---
# 拼接图瓦片下载成功率低于该阈值时视为整体下载失败（返回 download_failed，等待下次重试），
# 避免黑块瓦片混入后产出错误的颜色分析结果。
# 本项目监测范围在 zoom 7 下仅约 1x2=2 块瓦片，0.9 实际上要求全部瓦片下载成功。
MIN_TILE_SUCCESS_RATE = 0.9

# --- 重试配置 ---
# 单块瓦片下载失败时的重试次数与重试间隔（秒），应对瞬时网络抖动。
TILE_DOWNLOAD_RETRIES = 3
TILE_RETRY_DELAY_SECONDS = 2
# 调度周期内，对下载失败的时间戳进行重试的轮数与每轮间隔（秒）。
# 瓦片常因"该时间戳数据尚未发布"而 404，逐轮间隔重试可等待数据上线；
# 全部轮次失败则保持 download_failed 状态，交由下个调度周期继续重试。
FAILED_TIMESTAMP_RETRY_ROUNDS = 3
FAILED_TIMESTAMP_RETRY_DELAY_SECONDS = 30

# --- 2. HSV 颜色空间分类配置 ---
# 基于像素的颜色范围阈值法，取代 K-Means。
# 注意: OpenCV 中的 HSV 范围: H:[0, 179], S:[0, 255], V:[0, 255]
COLOR_CLASSIFICATION_HSV_RANGES = {
    # 云/白沫/高亮反光: 通常具有很低的饱和度(S)和很高的明度(V)。
    "CLOUD": {
        "lower": [0, 0, 121],
        "upper": [179, 40, 255]
    },
    # 蓝色的水体: 具有特定的蓝色色相(H)范围。
    "BLUE_WATER": {
        "lower": [30, 20, 0],
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

# --- 地图标注配置 (输出为 01_input_annotated.png 可视化图，不参与颜色分析) ---
# 陆地描边样式 (颜色统一使用 RGB 元组)
LAND_OUTLINE = {
    "color": (255, 214, 0),       # 描边颜色 (琥珀黄)
    "thickness": 1,               # 描边线宽
    "halo_color": (0, 0, 0),      # 描边外圈光晕颜色 (黑色，增强可读性)
    "halo_thickness": 3,          # 光晕线宽，0 表示不绘制
}
# 城市点位样式
CITY_MARKER = {
    "marker_color": (255, 45, 45),   # 城市点颜色 (红)
    "radius": 3,                     # 城市点半径 (像素，随图像尺寸自适应)
    "label_color": (25, 25, 25),     # 城市名文字颜色
    "label_stroke": 2,               # 文字白色描边宽度 (保证在海面/云层上都可读)
}
# 监测范围内的城市点位 (经纬度；绘制时超出图像可视范围的点位会被自动跳过)
CITY_POINTS = [
    {"name": "枸杞岛", "lon": 122.818, "lat": 30.722},
    {"name": "嵊泗",   "lon": 122.451, "lat": 30.735},
    {"name": "洋山港", "lon": 122.064, "lat": 30.633},
    {"name": "岱山",   "lon": 122.204, "lat": 30.243},
    {"name": "定海",   "lon": 122.107, "lat": 30.020},
    {"name": "沈家门", "lon": 122.304, "lat": 29.949},
    {"name": "宁波",   "lon": 121.551, "lat": 29.869},
]
# 中文字体搜索路径 (用于城市名标注，按顺序取第一个存在的文件；
# 可通过环境变量 CJK_FONT_PATH 优先指定)
CJK_FONT_PATH = os.getenv("CJK_FONT_PATH", "")
FONT_CANDIDATES = [
    CJK_FONT_PATH,
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
]