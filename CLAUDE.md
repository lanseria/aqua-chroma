# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Aqua-Chroma 是一个自动化海洋颜色监控系统，定时从卫星数据源获取图像，通过 HSV 颜色分析计算海蓝程度和云层覆盖率，结果持久化到 PostgreSQL 并通过 Web 仪表盘可视化展示。

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
- **config.py** — 所有配置中心：数据源选择（环境变量 `ACTIVE_DATA_SOURCE`）、HSV 阈值、地理范围、调度参数
- **downloader.py** — 瓦片坐标计算、多源卫星图像下载与拼接裁剪
- **pipeline.py** — 图像预处理编排（缩放→CLAHE色彩均衡→海洋蒙版→颜色分析），被主任务和调试工具共享
- **processor.py** — 日夜判断（ephem 天文算法）、HSV 阈值颜色分类、暗通道去雾算法
- **geo_utils.py** — GeoJSON 到像素蒙版的转换（墨卡托投影）
- **database.py / models.py / crud.py** — SQLAlchemy ORM，单表 `analysis_results`，upsert 语义
- **tools.py** — 开发调试工具（HSV 参数在线调优、批量测试图处理），挂载在 `/tools` 路由下

### 数据源切换

通过 `.env` 中 `ACTIVE_DATA_SOURCE` 控制，支持 `ZOOM_EARTH`（公开卫星服务）和 `LOCAL_SERVER`（本地 GIS 服务器），配置定义在 `config.py` 的 `DATA_SOURCES` 字典中。

### Web 层

- FastAPI 应用，Jinja2 模板渲染，ECharts 前端图表
- 静态文件：`data/` 挂载为 `/data`，`test_results/` 挂载为 `/test_results`
- 统一响应格式：`R_success()` / `R_fail()`

## 开发注意事项

- Python 3.12+，使用 `uv` 包管理器，清华 PyPI 镜像源
- 图像分析中间结果保存在 `data/output/{timestamp}/` 目录下，编号前缀标识处理阶段（01-04）
- GeoJSON 海洋蒙版在 `geojson/china.geojson`，陆地区域填充为黑色（mask=0）
- `main.py` 中有重复的 `_process_image_pipeline` 函数，实际主流程使用的是 `pipeline.py` 中的 `process_image_pipeline`（含 CLAHE 色彩均衡步骤）
- `database.py` 使用同步 SQLAlchemy（非 async），尽管 `pyproject.toml` 中安装了 `sqlalchemy[asyncio]`
