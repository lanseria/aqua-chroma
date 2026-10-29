# app/config.py

import os
from pathlib import Path

import torch
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
# 海洋蒙版用行政区划边界（数据源: 阿里 DataV GeoAtlas，浙江+上海子区划级，
# 海岸线精度远高于旧的全国级 china.geojson）。
# 更新边界: python scripts/update_geojson.py [省份adcode...]
GEOJSON_PATH = "geojson/monitor_area.geojson"
# 蒙版渲染超采样倍率：先在高倍画布上填充多边形再缩小取阈值，
# 使海岸线具有亚像素精度（小岛屿轮廓更平滑、不易整块丢失）。1 表示关闭。
MASK_SUPERSAMPLE = 4
TIME_ZONE = "Asia/Shanghai"
# --- 动态日照分析配置 ---
# 用于计算太阳高度角的观测点坐标 (用户指定: [122.020814, 30.349802])
MONITOR_LON = "122.020814"
MONITOR_LAT = "30.349802"
# 判定为有效白天的最小太阳高度角（度）。
# 直接按该时间戳监测点上空的太阳高度角判断，低于阈值视为光照不足（夜间/晨昏），跳过分析。
# 相比固定时间缓冲，窗口随季节逐日自动变化；可在线上通过环境变量调整而无需重建镜像。
MIN_SUN_ELEVATION_DEG = float(os.getenv("MIN_SUN_ELEVATION_DEG", "15"))

# 定义判定为“云层过厚”的云量覆盖阈值 (50%)
CLOUD_COVERAGE_THRESHOLD = 0.5

# --- 能见度估算配置 ---
# 能见度上限（公里）。晴好天气下卫星图的反演上限受图像分辨率限制，
# 超出部分没有区分度，封顶为 35 km（气象上的"极好能见度"量级）。
VISIBILITY_MAX_KM = float(os.getenv("VISIBILITY_MAX_KM", "35"))
# 无云水面像素占海洋总像素的参考比例。无云水面充足（≥该值）时完全信任无云水面的
# 测量；低于该值时不做硬截断记 0，而是按占比把"整个海洋区域（含云顶）"的测量
# 线性混入——云顶亮而平，天然落在低能见度端，云越厚权重越大，数值曲线随云量连续过渡。
VISIBILITY_MIN_CLEAR_FRACTION = float(os.getenv("VISIBILITY_MIN_CLEAR_FRACTION", "0.3"))

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
#
# 【重要】HSV 阈值是「运行时配置」，权威值存放在数据库 app_settings 表
# （key = "hsv_color_ranges"，由 app/settings_store.py 读写），可在网页
# /tools/color_thresholds 上实时调整，保存后下一次分析立即生效，无需重启。
# 下面这份字典只是「数据库中没有有效记录」时的兜底默认值（以及页面上的"恢复默认"目标），
# 不要把它当作现行阈值，也不要为了改阈值而改这里——请到页面/API 修改。
DEFAULT_COLOR_CLASSIFICATION_HSV_RANGES = {
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

# --- 分类结果图着色配置 (仅影响 04_hsv_classification.png 的可视化) ---
# 云/海蓝/海黄的三分类判定逻辑与阈值（DEFAULT_COLOR_CLASSIFICATION_HSV_RANGES）完全不受本配置影响，
# 这里只改变分类图怎么画：不再用 3 种纯色平涂，而是在每个类别内部按像素明度 V 分出深浅，
# 直观呈现云层厚薄、海水颜色深浅与浑浊程度。着色只借用已分类像素的 V 通道，不改变像素归属。
#
# palettes 中每个列表按「弱/浅 → 强/深」排列，列表长度即该类别的分级档数（各类可不同）：
#   CLOUD        : 灰(薄云/碎云) → 白(厚云/浓云)
#   BLUE_WATER   : 浅青 → 深蓝
#   YELLOW_WATER : 浅沙黄 → 深褐
HSV_CLASSIFICATION_RENDER = {
    # "levels"  = 按 palettes 分档着色（默认，深浅界限清晰）;
    # "gradient" = 在色阶之间连续渐变（更平滑，但不再有明确分档）
    "mode": "levels",
    # 是否在图片下方追加色阶图例（每类的深浅方向说明，中文字体缺失时回退英文）
    "legend": False,
    # 水体明度的深浅映射区间：V >= 上限视为该类"最浅"端，V <= 下限视为"最深"端。
    # 仅影响 BLUE_WATER / YELLOW_WATER；云层用判定阈值的 V 下界映射到 255。
    "water_value_range": [20, 140],
    "palettes": {
        "CLOUD": [(150, 150, 150), (198, 198, 198), (228, 228, 228), (255, 255, 255)],
        "BLUE_WATER": [(150, 214, 240), (0, 89, 138), (0, 55, 96), (3, 24, 60)],
        "YELLOW_WATER": [(238, 214, 151), (212, 176, 96), (161, 117, 9), (76, 45, 6)],
    },
}

# --- 图像预处理配置 ---
# 在进行任何分析之前，对输入图像进行放大的倍率。
# 1.0 表示不进行任何缩放。
# 2.0 表示将图像的宽度和高度都放大到原来的2倍。
# 推荐使用高质量的 Bicubic 插值算法，以获得更好的效果。
PRE_ANALYSIS_SCALE_FACTOR = 2.0

# --- Real-ESRGAN 图像超分（高清化）配置 ---
# 开启后，预处理阶段用 Real-ESRGAN x4 超分替代 bicubic 放大，
# 高清化结果保存为 01b_superresolved.png 并作为后续分析的输入。
# 模型加载失败或权重缺失时自动降级为 bicubic，不阻断主流程。
ESRGAN_ENABLED = str(os.getenv("ESRGAN_ENABLED", "true")).lower() in ("true", "1", "t")
ESRGAN_MODEL_PATH = Path(os.getenv("ESRGAN_MODEL_PATH", "models/RealESRGAN_x4plus.pth"))
ESRGAN_SCALE = 4
# RRDBNet 结构参数（必须与官方 RealESRGAN_x4plus 权重一致，勿改）
ESRGAN_NUM_FEAT = 64
ESRGAN_NUM_GROW_CH = 32
# 大图分块推理的块边长（像素），控制 CPU 推理的内存与单块耗时
ESRGAN_TILE_SIZE = 256
# torch CPU 推理线程数；0 表示使用默认值
ESRGAN_TORCH_THREADS = int(os.getenv("ESRGAN_TORCH_THREADS", "0")) or torch.get_num_threads()


# --- 定义调试图片的基准输出目录 ---
OUTPUT_BASE_DIR = "data/output"

# --- 颜色阈值智能调优实验台（/tools/color_thresholds）---
# 新流程：导入卫星图片（单张 / 多张 / ZIP 批量）→ 用当前 HSV 阈值自动分类
# （云层 / 蓝海 / 黄海）→ 用户点击结果图上分错的「色块」指定真实类别 →
# 后端汇总所有图片的纠正样本，自动搜索更优的 HSV 阈值 → 用户确认后保存即生效。
# 设计说明见 docs/color-thresholds-lab.md。
TOOL_UPLOAD_DIR = "data/tool_uploads/color_thresholds"
# 同时保留的导入图片数量上限（超出时导入接口返回中文错误，需先删除旧图）
TOOL_UPLOAD_MAX_IMAGES = 60
# 单次上传请求体上限（单张图片或一个 ZIP 的原始字节数）
TOOL_UPLOAD_MAX_BYTES = 60 * 1024 * 1024
# 导入图片等比缩放后的最长边（px）。控制后续色块聚类与实时预览的计算量。
TOOL_UPLOAD_MAX_SIDE = 1600
# 允许导入的图片扩展名（ZIP 内条目按扩展名过滤；单文件按能否解码判定）
TOOL_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
# ZIP 批量导入限制：条目数、解压后总字节数
TOOL_ZIP_MAX_ENTRIES = 120
TOOL_ZIP_MAX_TOTAL_BYTES = 300 * 1024 * 1024
# 色块聚类数（Lab 颜色空间 K-Means），以及色块最小像素数（更小的碎片不单独成块）
TOOL_BLOCK_COUNT = 14
TOOL_BLOCK_MIN_PIXELS = 24
# 色块纠正图例：key 与页面一致；IGNORE 表示排除出分析（陆地/非海域）
TOOL_CORRECTION_LABELS = (
    ("CLOUD", "云层"),
    ("BLUE_WATER", "蓝海"),
    ("YELLOW_WATER", "黄海"),
    ("IGNORE", "忽略"),
)
# 自动优化：参与优化的最大标注像素数、单块最多采样像素数、最低样本量
TOOL_OPTIMIZE_MAX_SAMPLES = 40000
TOOL_OPTIMIZE_MAX_SAMPLES_PER_BLOCK = 4000
TOOL_OPTIMIZE_MIN_SAMPLES = 300
# 预览图返回给页面的最长边（分类图会等比下采样，点击坐标按归一化值回传）
TOOL_PREVIEW_MAX_SIDE = 1000
# 小尺寸导入图在预览里的放大下限：161px 这类裁切图直接按原尺寸显示太小，
# 等比放大到这个最长边（≤ TOOL_PREVIEW_MAX_SIDE）后再返回，便于悬停/点击色块。
TOOL_PREVIEW_MIN_SIDE = 640
# 导入图与监控范围（TARGET_AREA）长宽比的容许偏差；超出则不套用陆地遮罩，
# 避免把不同取景范围的图按监控范围硬切（比例一致时按同一套经纬度 → 像素映射）。
TOOL_GEO_MASK_ASPECT_TOLERANCE = 0.03
# 缩略图最长边
TOOL_THUMB_MAX_SIDE = 240

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