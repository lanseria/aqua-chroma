# 使用官方的 uv 基础镜像
FROM m.daocloud.io/ghcr.io/astral-sh/uv:0.9.11-python3.13-bookworm

# 设置容器内的工作目录
WORKDIR /app

# --- 1. 配置 APT 镜像源 ---
RUN echo "\
Types: deb\n\
URIs: https://mirrors.tuna.tsinghua.edu.cn/debian/\n\
Suites: bookworm bookworm-updates bookworm-backports\n\
Components: main contrib non-free non-free-firmware\n\
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\
" > /etc/apt/sources.list.d/debian.sources

# --- 2. 安装系统依赖 ---
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    # --- Geospatial Libraries (for rasterio, shapely) ---
    libgdal-dev \
    libgeos-dev \
    # --- OpenCV Dependencies ---
    libgl1-mesa-glx \
    libglib2.0-0 \
    # --- Other System Dependencies ---
    libeccodes-dev \
    tzdata \
    # 清理APT缓存以减小镜像体积
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# --- 4. 设置环境变量 (已修正) ---
ENV \
    # 新增：将项目根目录添加到Python的模块搜索路径中
    PYTHONPATH=/app \
    # --- 其他环境变量保持不变 ---
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    HOME=/app \
    UV_TOOL_BIN_DIR=/usr/local/bin

ENV PATH="/app/.venv/bin:$PATH"

# --- 5. 依赖安装流程 ---
COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev


# 拷贝整个项目代码
COPY . .

# 声明端口和卷
EXPOSE 8000
VOLUME /app/data

# 临时修改：使用此入口点进行调试，以查看确切的导入错误
# ENTRYPOINT ["python", "-c", "import app.main"]

# 原始入口点 (暂时注释掉)
ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]