# Aqua-Chroma Monitor (海蓝之心监控)

**Aqua-Chroma Monitor** 是一个自动化的海洋颜色与状况监控系统。它以无人值守的方式定时从卫星数据源获取指定海域的图像，通过图像处理和地理空间分析计算出该区域的**海蓝程度**、**云层覆盖率**、**能见度**等关键指标，结果持久化到 PostgreSQL，并提供 REST API 与在线调试工具进行查询和算法调优。

[![Python Version](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Framework](https://img.shields.io/badge/framework-FastAPI-green.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](https://www.docker.com/)

---

## ✨ 功能特性

- **自动化数据处理**: 无需人工干预，服务启动后立即执行一轮分析，之后每 10 分钟自动获取最新的卫星时间戳并进行分析入库。
- **失败重试**: 对下载失败的时间戳，会在调度周期内间隔重试若干轮（等待瓦片数据上线），仍失败则留待下个周期继续。
- **多数据源支持**: 可通过环境变量 `ACTIVE_DATA_SOURCE` 轻松切换不同的卫星图像数据源（`LOCAL_SERVER` 本地 GIS 服务器或 `ZOOM_EARTH` 公开服务）。
- **精确地理分析**:
    - **精确裁剪**: 仅下载并处理目标地理区域（`TARGET_AREA`）的图像。
    - **陆地遮罩**: 使用 GeoJSON 文件精确移除图像中的陆地和岛屿部分，只分析海洋区域。
- **多维度图像分析**:
    - 基于太阳高度角（`MIN_SUN_ELEVATION_DEG`）智能判断 **黑夜** 时段并跳过分析。
    - 分析 **云层覆盖率**，并能识别 **云层过厚** 的情况。
    - 移除稀薄云层后，计算海洋的 **海蓝程度** 指标。
    - 估算 **海面能见度**（公里）：在无云水面上以暗通道亮度、局部 RMS 对比度、边缘纹理密度三项大气浑浊度代理量反演，并按 Koschmieder 定律（V = 3.912/β）折算为公里；无云水面占比不足时按比例混入含云顶的全场景测量，云越厚能见度自然越低，数值随云量连续过渡（无硬截断记 0），输出"良好/中等/轻度霾雾/浓雾"等级。
- **HSV 参数在线调优（批量对比）**: 内置批量调参工具（`/tools/hsv_tuner`），可视化调整云/蓝水的 HSV 阈值并对测试图集批量重处理，用于快速对比不同参数下的分类效果（只作用于本次调试，不影响线上配置）。
- **颜色阈值实时调优（保存即生效）**: 内置网页控制台（`/tools/color_thresholds`）——拖动滑块实时预览分类结果与指标变化，保存后阈值直接写入 PostgreSQL（`app_settings` 表），下一次定时分析立即采用新阈值；无需重启服务，也不再需要把阈值写进 `.env` 或 `config.py`。
- **预览取样与 tag 归类**: 调优页的预览源可自选**任意一次历史分析结果的时间点**（不再依赖测试图片），并给时间点打 tag（内置 `晴朗天空` / `少云` / `多云` / `阴天` / `薄雾` / `浓雾`，也支持自定义），按 tag 过滤即可用真实场景样本对比不同阈值的分类效果；tag 持久化在 `preview_tags` 表。
- **调试友好**: 每次分析都会将处理过程中的中间图像（原始图、色彩均衡图、海洋蒙版图、HSV 分类图等）按阶段编号保存到本地，便于调试和验证算法效果。
- **容器化部署**: 提供 `Dockerfile` 和 `docker-compose.yml`，使用 `uv` 作为包管理器，实现一键构建和部署。
- **灵活配置**: 数据源、目标区域、调度与重试参数等基础设施配置由环境变量 / `app/config.py` 管理；**HSV 颜色阈值是数据库中的运行时配置**（`app_settings` 表，页面 `/tools/color_thresholds` 编辑，保存即生效），`config.py` 中的数值仅作首次启动的兜底默认值。

---

## 🛠️ 技术栈

- **后端**: FastAPI, Uvicorn, APScheduler, SQLAlchemy (PostgreSQL)
- **包管理**: uv
- **数据处理**: OpenCV, NumPy, Pillow, Rasterio, Shapely, ephem（天文计算）
- **模板**: Jinja2（调试工具页面）
- **部署**: Docker, Docker Compose

---

## 🚀 快速开始 (使用 Docker)

使用 Docker 是启动此项目最推荐的方式。

### 1. 准备工作

- 安装 [Docker](https://www.docker.com/get-started) 和 [Docker Compose](https://docs.docker.com/compose/install/)。
- 克隆本项目:
  ```bash
  git clone https://github.com/lanseria/aqua-chroma.git
  cd aqua-chroma
  ```

### 2. 配置项目

#### a. GeoJSON 文件

项目自带 `geojson/monitor_area.geojson`（浙江+上海子区划级行政边界，数据源为阿里 DataV GeoAtlas，海岸线精度满足杭州湾/舟山海域的小岛屿刻画）。更新或更换监测海域时，运行 `python scripts/update_geojson.py [省份adcode...]` 重新下载合并（如 `330000` 浙江、`310000` 上海），或替换为您自己的 GeoJSON 文件并修改 `app/config.py` 中的 `GEOJSON_PATH`。

#### b. 核心配置 (`app/config.py`)

根据您的需求修改以下关键部分：

- **`TARGET_AREA`**: 目标海域的经纬度边界。
- **`GEOJSON_PATH`**: GeoJSON 文件路径。
- **`CITY_POINTS`**: 地图标注的城市点位（用于生成可视化标注图）。
- **`HSV_CLASSIFICATION_RENDER`**: 分类结果图（`04_hsv_classification.png`）的着色方式——每个类别内部按像素明度分出多档深浅（云层薄→厚、海蓝浅→深、海黄淡→浓），不再用 3 种纯色平涂；可切换 `levels` 分档 / `gradient` 渐变、调整色阶列表与是否附图例。该配置只影响可视化，不改变分类判定与任何指标口径。

> **HSV 颜色阈值不在本文件中维护**：`config.DEFAULT_COLOR_CLASSIFICATION_HSV_RANGES` 只是数据库无记录时的兜底默认值（用于首次启动）。实际生效值以 PostgreSQL `app_settings` 表（`key = hsv_color_ranges`）为准，请通过 `/tools/color_thresholds` 页面在线调整——保存后立即对后续分析生效，无需重启服务、无需修改 `.env` 或代码。

#### c. 环境变量 (`.env.production`)

Docker 部署读取根目录下的 `.env.production` 文件（本地开发则使用 `.env`）：

```env
# 数据库连接（必需），格式: postgresql://user:password@host:5432/dbname
DATABASE_URL=postgresql://user:password@host:5432/aqua_chroma

# 数据源: "LOCAL_SERVER" 或 "ZOOM_EARTH"（默认）
ACTIVE_DATA_SOURCE=ZOOM_EARTH

# 昼夜判断的最小太阳高度角（度，默认 10）
MIN_SUN_ELEVATION_DEG=10

# 启动时是否跳过首次分析任务（默认 false）
SKIP_INITIAL_TASK=false
```

> **注意**: `docker-compose.yml` 依赖名为 `shared-db-network` 的外部 Docker 网络（用于连接数据库容器），启动前请确认该网络已存在：`docker network create shared-db-network`。

### 3. 构建并启动服务

在项目根目录下，运行以下命令：

```bash
docker compose up --build -d
```

- `--build`: 首次运行时，会根据 `Dockerfile` 构建镜像。
- `-d`: 在后台（detached mode）运行服务。

### 4. 访问服务

服务映射在宿主机 **`http://localhost:8010`**，可访问：

| 地址 | 说明 |
| --- | --- |
| `/docs` | Swagger 交互式 API 文档，可直接在线调试所有接口 |
| `/` | 健康检查（返回 JSON） |
| `/api/results` | 查询数据库中全部分析结果 |
| `/tools/hsv_tuner` | HSV 参数在线批量调优工具（调试用，不影响线上阈值） |
| `/tools/color_thresholds` | 颜色阈值实时调优控制台（自选分析时间点、tag 分类、保存即生效） |
| `/data/output/{timestamp}/` | 浏览某次分析的中间处理图像 |

### 5. 查看日志和数据

- **查看实时日志**:
  ```bash
  docker compose logs -f
  ```
- **查看持久化数据**:
  分析结果存储在 PostgreSQL 的 `analysis_results` 表（按时间戳 upsert），可在网页上实时调整的 HSV 阈值存储在 `app_settings` 表；调试图片存储在 Docker 卷 `aqua-chroma-data` 中。您可以通过以下命令查看其在主机上的具体位置：
  ```bash
  docker volume inspect aqua-chroma-data
  ```

---

## 🔧 本地开发 (不使用 Docker)

### 1. 环境准备

- 安装 Python 3.12+
- 安装 [uv](https://docs.astral.sh/uv/) 包管理器:
  ```bash
  pip install uv
  ```

### 2. 安装依赖

```bash
uv sync --locked
```

### 3. 配置环境变量

在项目根目录创建 `.env` 文件（必需项见上文「环境变量」一节，至少需要 `DATABASE_URL`）。

### 4. 安装系统依赖

本地运行 `rasterio` 和 `opencv-python` 可能需要手动安装系统库，例如在 Debian/Ubuntu 上：
```bash
sudo apt-get update
sudo apt-get install libgdal-dev libgl1-mesa-glx
```

### 5. 启动服务

```bash
uvicorn app.main:app --reload
```

服务将在 `http://127.0.0.1:8000` 上运行，启动后即可访问 `http://127.0.0.1:8000/docs` 查看和调试 API。

> HSV 取值可借助在线换算工具辅助调试: https://www.qtccolor.com/secaiku/tool/convert?m=hsv

---

## 🌐 API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 健康检查 |
| GET | `/api/results` | 返回全部分析结果（海蓝程度、云层覆盖率、能见度、状态、时间戳等） |
| GET | `/api/debug/analyze/{timestamp}` | 对指定时间戳手动重跑完整分析（下载→处理→upsert 入库） |
| GET | `/tools/hsv_tuner` | HSV 参数在线调优页面 |
| GET | `/tools/color_thresholds` | 颜色阈值实时调优页面（实时预览，保存即生效） |
| GET | `/tools/api/color_thresholds` | 读取当前生效的 HSV 阈值、来源（数据库/默认值）与更新时间 |
| POST | `/tools/api/color_thresholds` | 校验并保存 HSV 阈值到数据库（立即生效，无需重启） |
| POST | `/tools/api/color_thresholds/reset` | 恢复内置默认阈值（删除数据库记录） |
| GET | `/tools/api/preview_sources` | 列出全部可预览的分析结果时间点（含 datetime、tag、总数/截断标记）与 tag 统计、预设 |
| POST | `/tools/api/threshold_preview` | 用给定阈值对某个分析结果时间点做实时分类预览（返回图像与指标） |
| GET | `/tools/api/preview_tags` | 读取所有 tag 及其样本计数、内置预设 tag |
| POST | `/tools/api/preview_tags` | 替换式保存某个分析结果时间点的 tag 列表（如把该时刻标记为「多云」） |
| GET | `/tools/api/test_images` | 列出 `test_images/` 下的测试图 |
| POST | `/tools/api/reprocess_all_hsv` | 用指定 HSV 参数批量重处理所有测试图 |

静态资源挂载：

- `/data` — 分析输出目录，每次分析的中间图像位于 `data/output/{timestamp}/`（`01_input_processed.png` 原图、`02_auto_balanced.png` 色彩均衡、`03_ocean_only.png` 海洋蒙版、`04_hsv_classification.png` HSV 分类（类别内按明度分档着色，附图例）、`05_dark_channel.png` 暗通道/大气浑浊度调试图）
- `/test_results` — HSV 调优工具的批量输出结果

### 存量数据库迁移

能见度功能为 `analysis_results` 表新增了 `visibility_km`、`visibility_level`、`haze_score` 三列。已有部署请执行一次性迁移脚本（幂等，可重复运行）：

```bash
uv run python scripts/migrate_add_visibility_columns.py
```

存量记录的能见度字段保持 NULL（表示早于该功能的记录），仪表盘对 NULL 不展示能见度行。

颜色阈值改为运行时配置后新增了 `app_settings` 表（`key`/`value` JSON 文本/`updated_at`，当前只存 `hsv_color_ranges` 一条记录）。应用启动时 `init_db()` 会自动建表，已有部署也可执行幂等迁移脚本：

```bash
uv run python scripts/migrate_add_app_settings.py
```

迁移后表内为空即表示"使用内置默认阈值"；在 `/tools/color_thresholds` 页面保存后，该表出现 `hsv_color_ranges` 记录，并成为唯一生效来源。阈值不再通过环境变量配置。

预览源的 tag 归类使用新表 `preview_tags`（`timestamp` + `tag`，同一时间点可多个 tag）。同样有幂等迁移脚本：

```bash
uv run python scripts/migrate_add_preview_tags.py
```

tag 为空即表示该时间点未归类；调优页可按 tag 过滤时间点。测试图片（`test_images/`）只服务于旧的 `/tools/hsv_tuner` 批量调参页，不再出现在阈值实时调优页的预览源中。

---

## 📂 项目结构

```
.
├── app/                  # FastAPI 应用核心代码
│   ├── config.py         # 基础设施配置中心（数据源、地理范围、调度参数、兜底默认阈值等）
│   ├── main.py           # 应用主入口：路由、定时任务、核心分析编排
│   ├── downloader.py     # 瓦片坐标计算、卫星图像下载与拼接裁剪
│   ├── pipeline.py       # 图像预处理流水线（缩放→CLAHE→海洋蒙版→颜色分析）
│   ├── processor.py      # 日夜判断、HSV 颜色分类（阈值取自运行时设置）、暗通道去雾
│   ├── settings_store.py # 运行时设置存储（HSV 阈值：DB 持久化 + 缓存 + 校验，保存即生效）
│   ├── threshold_preview.py # 阈值实时预览（仅分析结果源；预处理按源缓存；含 tag 过滤数据）
│   ├── geo_utils.py      # GeoJSON 转像素蒙版（墨卡托投影）、地图标注
│   ├── database.py       # SQLAlchemy 数据库连接（同步）
│   ├── models.py         # ORM 模型（analysis_results 分析结果、app_settings 运行时设置、preview_tags 预览源 tag）
│   ├── crud.py           # 数据读写（结果 upsert；运行时设置 get/upsert/delete；tag 读写）
│   ├── schemas.py        # Pydantic 请求/响应模型
│   └── tools.py          # 在线调优工具与阈值 API（挂载在 /tools 路由）
├── geojson/              # GeoJSON 文件（海洋蒙版用）
├── data/                 # 运行时数据目录
│   └── output/           # 每次分析的中间图像（按时间戳分目录）
├── templates/            # Jinja2 模板（HSV 批量调优、颜色阈值实时调优页面）
├── test_images/          # 批量调参工具（/tools/hsv_tuner）用的测试图片
├── test_results/         # 调优工具输出结果
├── scripts/              # 数据迁移等辅助脚本
├── .env                  # 环境变量文件（本地开发）
├── .env.production       # 环境变量文件（Docker 部署）
├── docker-compose.yml    # Docker Compose 配置文件
├── Dockerfile            # Docker 镜像构建文件
├── pyproject.toml        # Python 项目定义与依赖
└── README.md             # 项目说明文档
```

---

## 💡 未来展望

- [ ] 集成更多卫星数据源（如 Sentinel, Landsat）。
- [ ] 引入机器学习模型以提高云层识别的准确率。
- [ ] 开发更丰富的分析功能，如多区域对比、数据导出等。

---

## 🤝 贡献

欢迎提交 Pull Requests 或开启 Issues 参与项目贡献！

## 📄 许可证

本项目采用 [MIT License](LICENSE) 开源。
