# app/config.py

# --- 数据源配置 ---
GIS_SERVER_URL = "http://bmcr1-wtr-r1:8080/zoom-earth-tiles"
ZOOM_LEVEL = 7  # 建议使用更高的缩放级别以获得更清晰的图像

# --- 目标海域配置 ---
# 定义你感兴趣的海域边界 (北, 南, 西, 东)
TARGET_AREA = {
    "north": 31.290,
    "south": 29.400,
    "west": 121.200,
    "east": 123.400
}

# --- GeoJSON配置 ---
GEOJSON_PATH = "data/geojson/china.geojson"

# --- 时间配置 ---
TIME_ZONE = "Asia/Shanghai"  # 设置当地时区
NIGHT_START_HOUR = 17  # 定义黑夜开始的小时
NIGHT_END_HOUR = 7     # 定义黑夜结束的小时

# --- 图像分析参数 ---
CLOUD_THRESHOLD = 144      # 判断为云的像素亮度阈值 (0-255)
THICK_CLOUD_COVERAGE = 0.7 # 浓云覆盖率阈值 (70%)
BLUE_LOWER_BOUND = [100, 40, 20] # 蓝色HSV下限
BLUE_UPPER_BOUND = [140, 255, 255] # 蓝色HSV上限

RESULTS_JSON_PATH = "data/analysis_results.json"
TIMESTAMPS_URL = f"{GIS_SERVER_URL}/himawari/timestamps.json"