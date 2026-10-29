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
- **config.py** — 基础设施配置中心：数据源选择（环境变量 `ACTIVE_DATA_SOURCE`）、分类图着色（`HSV_CLASSIFICATION_RENDER`）、地理范围、调度参数、地图标注样式与城市点位（`CITY_POINTS`）、调优工作台上传/色块/优化参数（`TOOL_*`）。**HSV 颜色阈值不再是权威配置**：`DEFAULT_COLOR_CLASSIFICATION_HSV_RANGES` 仅为数据库无记录时的兜底默认值
- **settings_store.py** — 运行时设置存储：HSV 阈值持久化在 `app_settings` 表（key=`hsv_color_ranges`），带中文校验、约 1 秒 TTL 缓存与保存即失效；`processor` 每次分析都从它取生效阈值，因此页面保存后无需重启即生效
- **color_lab.py** — 颜色阈值智能调优后端（`/tools/color_thresholds`）：导入卫星图片（单张/ZIP 批量，原始二进制请求体；等比缩放最长边 1600px、纯黑像素视为无效；与监控范围同比例时按 GeoJSON 套用陆地遮罩）→ Lab 空间 K-Means + 连通域切「色块」（`blocks.png`/`block_stats.json`）→ 复用 `processor` 做分类渲染/指标 → 悬停查色块详情、点击色块写入用户纠正（云层/蓝海/黄海/忽略）→ 坐标下降自动搜索更优 HSV 阈值（目标 = 用户标注准确率 + 未标注区域保真度各 50%，只返回建议不落库）。数据存 `data/tool_uploads/color_thresholds/<image_id>/`，不依赖数据库；设计与接口见 `docs/color-thresholds-lab.md`
- **downloader.py** — 瓦片坐标计算、多源卫星图像下载与拼接裁剪
- **pipeline.py** — 图像预处理编排（缩放→CLAHE色彩均衡→海洋蒙版→颜色分析，另生成带地图标注的 `01_input_annotated.png` 可视化图），被主任务和调试工具共享
- **processor.py** — 日夜判断（ephem 天文算法）、HSV 阈值颜色分类（阈值来自 `settings_store`，可传 override）、能见度估算（`estimate_visibility`：在无云水面上用暗通道亮度/局部RMS对比度/边缘密度三项浑浊度代理量按 Koschmieder 定律折算公里数；无云占比不足时按比例混入含云顶的全场景测量，云越厚能见度自然越低，不做硬截断记 0）
- **geo_utils.py** — GeoJSON 到像素蒙版的转换（墨卡托投影）；`01_input_annotated.png` 的地图标注：陆地描边（从海洋蒙版提取海岸线轮廓）与城市点位+中文标注（需中文字体，路径见 `config.FONT_CANDIDATES`）
- **database.py / models.py / crud.py** — SQLAlchemy ORM，`analysis_results`（分析结果，upsert 语义）+ `app_settings`（运行时键值设置，JSON 文本）。旧版调优页的 `preview_tags` 表与代码已移除（已有数据库中的该表可保留，不再读写）
- **tools.py** — 开发/调优工具（HSV 批量调参 `/tools/hsv_tuner`、颜色阈值智能调优 `/tools/color_thresholds` 及其 API），挂载在 `/tools` 路由下

### 数据源切换

通过 `.env` 中 `ACTIVE_DATA_SOURCE` 控制，支持 `ZOOM_EARTH`（公开卫星服务）和 `LOCAL_SERVER`（本地 GIS 服务器），配置定义在 `config.py` 的 `DATA_SOURCES` 字典中。

### Web 层

- FastAPI 应用，Jinja2 模板渲染，ECharts 前端图表
- 静态文件：`data/` 挂载为 `/data`，`test_results/` 挂载为 `/test_results`（调优页面为单文件内联 CSS/JS，不依赖未挂载的 `/static`）
- 统一响应格式：`R_success()` / `R_fail()`；`/tools` 下的调优 API 沿用工具自身约定 `{"success": bool, "data": ...}` / `{"success": false, "error": ..., "details": [...]}`
- 颜色阈值智能调优：页面 `/tools/color_thresholds`，API 为 `/tools/api/color_thresholds`（GET/POST）、`/reset`，以及实验台接口 `/tools/api/color_thresholds/lab`、`/images`（POST 原始二进制导入，DELETE `/images/{id}`、POST `/images/clear`）、`/preview`、`/block`、`/corrections`（POST 保存/取消，POST `/corrections/clear`）、`/optimize`；请求/响应契约见 `docs/color-thresholds-lab.md`

## 开发注意事项

- Python 3.12+，使用 `uv` 包管理器，清华 PyPI 镜像源
- 图像分析中间结果保存在 `data/output/{timestamp}/` 目录下，编号前缀标识处理阶段（01-05）
- `01_input_processed.png` 保持干净无标注，与分析口径一致；`01_input_annotated.png` 叠加陆地描边与城市点位标注，仅供可视化；`05_dark_channel.png` 为能见度估算的暗通道调试图（亮=大气浑浊，仅陆地置黑，云保留显示）；`04_hsv_classification.png` 在分类判定不变的前提下，按类别内部明度分档着色（`config.HSV_CLASSIFICATION_RENDER`，默认 4 档 + 底部图例）
- GeoJSON 海洋蒙版在 `geojson/china.geojson`，陆地区域填充为黑色（mask=0）
- 高精度海洋蒙版边界在 `geojson/monitor_area.geojson`（阿里 DataV 浙沪子区划级，用 `scripts/update_geojson.py` 刷新），陆地区域填充为黑色（mask=0）
- `database.py` 使用同步 SQLAlchemy（非 async），尽管 `pyproject.toml` 中安装了 `sqlalchemy[asyncio]`
- 数据库 Schema 变更时需同步编写 `scripts/migrate_*.py` 幂等迁移脚本（参考 `migrate_add_visibility_columns.py`），存量数据无法回填的列保持 NULL
- HSV 颜色阈值是运行时配置（`app_settings` 表中的 JSON），不要写进 `.env` 或硬编码进 `config.py`；新增可调参数时按 `settings_store.py` 的模式扩展（校验 + 持久化 + 缓存失效），调试用临时阈值通过 `hsv_ranges_override` 参数传入，落到生产上的改动一律走页面/API 保存
- 调优工作台的导入图片与纠正数据都在 `data/tool_uploads/color_thresholds/<image_id>/`（`source.png` 分析输入、`thumb.jpg`、`meta.json`、`corrections.json`、`blocks.png`、`block_stats.json`），不写数据库；`/data` 已挂载为静态目录，页面缩略图直接走 `/data/...` URL
- 工作台的陆地遮罩按 `config.TARGET_AREA` 生成并缓存：导入图长宽比在 `TOOL_GEO_MASK_ASPECT_TOLERANCE` 内才套用，遮罩口径版本记录在 `block_stats.json` 的 `mask` 字段（口径变化时色块自动重建）；预览图会把小图放大到 `TOOL_PREVIEW_MIN_SIDE`，页面鼠标悬停走 `/tools/api/color_thresholds/block`（`preview=false`）只取色块信息
- 自动优化只返回建议值，绝不自动落库；保存阈值仍然只走 `POST /tools/api/color_thresholds`（用户点「保存并立即生效」）
- 工作台导入接口刻意使用**原始二进制请求体**（`?filename=`），不引入 python-multipart；ZIP 批量导入有条目数/解压总量/单张大小上限（见 `config.TOOL_*`）
- Docker 部署的图片挂载：`TILES_HOST_PATH` 必须写在 compose 项目级 `.env` 或 shell 环境变量（`env_file: .env.production` 里的同名变量不参与卷替换，会静默回落命名卷）；启动日志的 `[Storage]` 行与 `GET /api/debug/storage` 可查看输出目录绝对路径、挂载来源、可写性与已有时间戳目录数
- 新增数据库表时同时更新 `app/models.py` 与迁移脚本；`init_db()` 启动时也会 `create_all` 自动建表
