## 项目概述

Aqua-Chroma 是一个自动化海洋颜色监控系统，定时从卫星数据源获取图像，通过 HSV 颜色分析计算海蓝程度、云层覆盖率和能见度，结果持久化到 PostgreSQL 并通过 Web 仪表盘可视化展示。

## 常用命令

```bash
# 本地开发启动
uvicorn app.main:app --reload

# Docker 构建与启动
docker compose up --build -d

# 查看日志
docker compose logs -f

# 安装依赖（需要 uv 包管理器）
uv sync --locked
```

## 架构

### 数据流水线（核心处理流程）

```
定时调度(10分钟) → 获取新时间戳 → downloader 下载拼接卫星瓦片图
→ pipeline 预处理(缩放+CLAHE色彩均衡) → geo_utils 生成海洋蒙版(去除陆地)
→ processor HSV颜色分类(云/蓝水/黄水) → 计算指标 → crud 持久化到 PostgreSQL
```

关键模块职责：
- **config.py** — 基础设施配置中心：数据源选择（环境变量 `ACTIVE_DATA_SOURCE`）、分类图着色（`HSV_CLASSIFICATION_RENDER`）、地理范围、调度参数、地图标注样式与城市点位（`CITY_POINTS`）。**HSV 颜色阈值不再是权威配置**：`DEFAULT_COLOR_CLASSIFICATION_HSV_RANGES` 仅为数据库无记录时的兜底默认值
- **settings_store.py** — 运行时设置存储：HSV 阈值持久化在 `app_settings` 表（key=`hsv_color_ranges`），带中文校验、约 1 秒 TTL 缓存与保存即失效；`processor` 每次分析都从它取生效阈值，因此页面保存后无需重启即生效
- **threshold_preview.py** — 颜色阈值实时预览：**仅支持分析结果源**（`data/output/<ts>/03_ocean_only.png`，任意历史时间点），预处理结果按源缓存，每次预览只跑 HSV 分类与指标，复用 `processor` 的生产判定逻辑；同时为页面提供时间点列表与 tag 归类数据（tag 存 `preview_tags` 表，预设见 `config.PREVIEW_TAG_PRESETS`）
- **downloader.py** — 瓦片坐标计算、多源卫星图像下载与拼接裁剪
- **pipeline.py** — 图像预处理编排（缩放→CLAHE色彩均衡→海洋蒙版→颜色分析，另生成带地图标注的 `01_input_annotated.png` 可视化图），被主任务和调试工具共享
- **processor.py** — 日夜判断（ephem 天文算法）、HSV 阈值颜色分类（阈值来自 `settings_store`，可传 override）、能见度估算（`estimate_visibility`：在无云水面上用暗通道亮度/局部RMS对比度/边缘密度三项浑浊度代理量按 Koschmieder 定律折算公里数；无云占比不足时按比例混入含云顶的全场景测量，云越厚能见度自然越低，不做硬截断记 0）
- **geo_utils.py** — GeoJSON 到像素蒙版的转换（墨卡托投影）；`01_input_annotated.png` 的地图标注：陆地描边（从海洋蒙版提取海岸线轮廓）与城市点位+中文标注（需中文字体，路径见 `config.FONT_CANDIDATES`）
- **database.py / models.py / crud.py** — SQLAlchemy ORM，`analysis_results`（分析结果，upsert 语义）+ `app_settings`（运行时键值设置，JSON 文本）+ `preview_tags`（预览源时间点的分类 tag，如 晴朗天空/多云）
- **tools.py** — 开发/调优工具（HSV 批量调参 `/tools/hsv_tuner`、颜色阈值实时调优 `/tools/color_thresholds` 及其 API），挂载在 `/tools` 路由下

### 数据源切换

通过 `.env` 中 `ACTIVE_DATA_SOURCE` 控制，支持 `ZOOM_EARTH`（公开卫星服务）和 `LOCAL_SERVER`（本地 GIS 服务器），配置定义在 `config.py` 的 `DATA_SOURCES` 字典中。

### Web 层

- FastAPI 应用，Jinja2 模板渲染，ECharts 前端图表
- 静态文件：`data/` 挂载为 `/data`，`test_results/` 挂载为 `/test_results`（调优页面为单文件内联 CSS/JS，不依赖未挂载的 `/static`）
- 统一响应格式：`R_success()` / `R_fail()`；`/tools` 下的调优 API 沿用工具自身约定 `{"success": bool, "data": ...}` / `{"success": false, "error": ..., "details": [...]}`
- 阈值实时调优：页面 `/tools/color_thresholds`，API 为 `/tools/api/color_thresholds`（GET/POST）、`/reset`、`/tools/api/preview_sources`、`/tools/api/preview_tags`（GET/POST，替换式保存时间点 tag）、`/tools/api/threshold_preview`

## 开发注意事项

- Python 3.12+，使用 `uv` 包管理器，清华 PyPI 镜像源
- 图像分析中间结果保存在 `data/output/{timestamp}/` 目录下，编号前缀标识处理阶段（01-05）
- `01_input_processed.png` 保持干净无标注，与分析口径一致；`01_input_annotated.png` 叠加陆地描边与城市点位标注，仅供可视化；`05_dark_channel.png` 为能见度估算的暗通道调试图（亮=大气浑浊，仅陆地置黑，云保留显示）；`04_hsv_classification.png` 在分类判定不变的前提下，按类别内部明度分档着色（`config.HSV_CLASSIFICATION_RENDER`，默认 4 档 + 底部图例）
- GeoJSON 海洋蒙版在 `geojson/china.geojson`，陆地区域填充为黑色（mask=0）
- 高精度海洋蒙版边界在 `geojson/monitor_area.geojson`（阿里 DataV 浙沪子区划级，用 `scripts/update_geojson.py` 刷新），陆地区域填充为黑色（mask=0）
- `database.py` 使用同步 SQLAlchemy（非 async），尽管 `pyproject.toml` 中安装了 `sqlalchemy[asyncio]`
- 数据库 Schema 变更时需同步编写 `scripts/migrate_*.py` 幂等迁移脚本（参考 `migrate_add_visibility_columns.py`），存量数据无法回填的列保持 NULL
- HSV 颜色阈值是运行时配置（`app_settings` 表中的 JSON），不要写进 `.env` 或硬编码进 `config.py`；新增可调参数时按 `settings_store.py` 的模式扩展（校验 + 持久化 + 缓存失效），调试用临时阈值通过 `hsv_ranges_override` 参数传入，落到生产上的改动一律走页面/API 保存
- 阈值实时预览只接受分析结果源：`data/output/<ts>/03_ocean_only.png`（预处理缓存命中后毫秒级）；`test_images/` 只服务于旧的 `/tools/hsv_tuner` 批量调参页，不再出现在实时调优页的预览源中
- 预览源 tag 是用户数据（`preview_tags` 表，一个时间点可多个 tag）：用于把真实分析结果按场景归类（晴朗天空/多云/薄雾…）当"分类型测试样本"；新增/修改预设 tag 改 `config.PREVIEW_TAG_PRESETS`，校验常量在同处（长度/数量上限）
- 新增数据库表时同时更新 `app/models.py` 与迁移脚本；`init_db()` 启动时也会 `create_all` 自动建表
