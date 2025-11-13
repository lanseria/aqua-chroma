# Aqua-Chroma Monitor (海蓝之心监控)

**Aqua-Chroma Monitor** 是一个自动化的海洋颜色与状况监控系统。它能够定时从卫星数据源获取指定海域的图像，通过一系列图像处理和地理空间分析，计算出该区域的海蓝程度、云层覆盖率等关键指标，并提供一个动态更新的Web仪表盘进行可视化展示。

[![Python Version](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Framework](https://img.shields.io/badge/framework-FastAPI-green.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](https://www.docker.com/)

 
*(提示: 建议您截取一张项目运行时的仪表盘图片，并替换上面的链接，以获得更好的展示效果)*

---

## ✨ 功能特性

- **自动化数据处理**: 无需人工干预，系统以设定的时间间隔（默认为10分钟）自动获取最新的卫星时间戳并进行分析。
- **多数据源支持**: 可通过环境变量轻松切换不同的卫星图像数据源（例如，本地GIS服务器或公开的Zoom.earth服务）。
- **精确地理分析**:
    - **精确裁剪**: 仅下载并处理目标地理区域（`TARGET_AREA`）的图像。
    - **陆地遮罩**: 使用 GeoJSON 文件精确移除图像中的陆地和岛屿部分，只分析海洋区域。
- **多维度图像分析**:
    - 智能判断 **黑夜** 时段并跳过分析。
    - 分析 **云层覆盖率**，并能识别 **云层过厚** 的情况。
    - 移除稀薄云层后，计算海洋的 **海蓝程度** 指标。
- **动态Web仪表盘**:
    - 使用 **FastAPI** 和 **Jinja2** 构建，前后端一体化。
    - 顶部 **ECharts** 折线图展示“海蓝程度”的历史趋势。
    - 采用 **无限滚动**（懒加载）方式展示历史数据卡片，优化性能。
    - 使用动态着色的 **进度条** 直观展示每条记录的海蓝程度和云层覆盖率。
- **调试友好**: 每次分析都会将处理过程中的中间图像（如原始图、蒙版图、云层图等）保存到本地，便于调试和验证算法效果。
- **容器化部署**: 提供 `Dockerfile` 和 `docker-compose.yml`，使用 `uv` 作为包管理器，实现一键构建和部署。
- **灵活配置**: 核心参数（如目标区域、数据源等）均可通过环境变量或配置文件进行修改，无需改动代码。

---

## 🛠️ 技术栈

- **后端**: FastAPI, Uvicorn, APScheduler
- **包管理**: uv
- **数据处理**: OpenCV, NumPy, Pillow, Rasterio, Shapely
- **前端**: HTML5, CSS3, JavaScript, ECharts
- **部署**: Docker, Docker Compose

---

## 🚀 快速开始 (使用 Docker)

使用 Docker 是启动此项目最推荐的方式。

### 1. 准备工作

- 安装 [Docker](https://www.docker.com/get-started) 和 [Docker Compose](https://docs.docker.com/compose/install/)。
- 克隆本项目:
  ```bash
  git clone https://github.com/your-username/aqua-chroma.git
  cd aqua-chroma
  ```

### 2. 配置项目

#### a. GeoJSON 文件
获取您目标海域的 GeoJSON 文件，并将其放入 `data/geojson/` 目录。例如，您可以将其命名为 `my_sea_area.geojson`。

#### b. 核心配置 (`app/config.py`)
打开 `app/config.py` 文件，根据您的需求修改以下关键部分：

- **`TARGET_AREA`**: 设置您想要监控的目标海域的经纬度边界。
- **`GEOJSON_PATH`**: 将路径修改为您自己的 GeoJSON 文件名，例如 `data/geojson/my_sea_area.geojson`。

#### c. 环境变量 (`.env`)
在项目根目录下创建一个 `.env` 文件。这个文件用于控制容器的运行时行为。

```env
# .env

# 设置当前激活的数据源
# 可选值: "LOCAL_SERVER" 或 "ZOOM_EARTH"
ACTIVE_DATA_SOURCE=ZOOM_EARTH
```

### 3. 构建并启动服务

在项目根目录下，运行以下命令：

```bash
docker-compose up --build -d
```- `--build`: 首次运行时，会根据 `Dockerfile` 构建镜像。
- `-d`: 在后台（detached mode）运行服务。

### 4. 访问仪表盘

服务启动后，打开您的浏览器并访问: **`http://localhost:8010`**

### 5. 查看日志和数据

- **查看实时日志**:
  ```bash
  docker-compose logs -f
  ```
- **查看持久化数据**:
  所有分析结果（`analysis_results.json`）和调试图片都存储在 Docker 卷 `aqua-data` 中。您可以通过以下命令查看其在主机上的具体位置：
  ```bash
  docker volume inspect aqua-chroma_aqua-data
  ```

---

## 🔧 本地开发 (不使用 Docker)

### 1. 环境准备
- 安装 Python 3.12+
- 推荐使用 `pyenv` 来管理 Python 版本。
- 安装 `uv`:
  ```bash
  pip install uv
  ```

### 2. 创建虚拟环境并安装依赖
```bash
# 创建虚拟环境
uv venv

# 激活虚拟环境
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 安装依赖
uv pip install -r requirements.txt
```
*(注意: 您可能需要先根据 `pyproject.toml` 生成 `requirements.txt` 文件: `uv pip freeze > requirements.txt`)*

### 3. 安装系统依赖
本地运行 `rasterio` 和 `opencv-python` 可能需要手动安装系统库，例如在 Debian/Ubuntu 上：
```bash
sudo apt-get update
sudo apt-get install libgdal-dev libgl1-mesa-glx
```

### 4. 启动服务
```bash
uvicorn app.main:app --reload
```
服务将在 `http://127.0.0.1:8000` 上运行。

---

## 📂 项目结构

```
.
├── app/                  # FastAPI 应用核心代码
│   ├── config.py         # 核心配置文件
│   ├── downloader.py     # 卫星图像下载与裁剪模块
│   ├── geo_utils.py      # 地理数据处理与蒙版生成
│   ├── main.py           # FastAPI 应用主入口与调度任务
│   └── processor.py      # 图像分析核心算法
├── data/                 # 持久化数据目录
│   ├── analysis_results.json # 存储所有分析结果
│   ├── geojson/          # 存放 GeoJSON 文件
│   └── output/           # 存放每次分析的调试图片
├── static/               # 前端静态文件 (CSS, JS)
├── templates/            # 前端 HTML 模板
├── .env                  # 环境变量文件 (本地)
├── docker-compose.yml    # Docker Compose 配置文件
├── Dockerfile            # Docker 镜像构建文件
├── pyproject.toml        # Python 项目定义与依赖
└── README.md             # 项目说明文档
```

---

## 💡 未来展望

- [ ] 集成更多卫星数据源（如 Sentinel, Landsat）。
- [ ] 引入机器学习模型以提高云层识别的准确率。
- [ ] 将分析结果存储到时序数据库（如 InfluxDB）以提高查询性能。
- [ ] 开发更丰富的仪表盘功能，如多区域对比、数据导出等。

---

## 🤝 贡献

欢迎提交 Pull Requests 或开启 Issues 参与项目贡献！

## 📄 许可证

本项目采用 [MIT License](LICENSE) 开源。