FROM m.daocloud.io/ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# 设置容器内的工作目录
WORKDIR /app

# --- 1. 配置 APT 镜像源  ---
RUN echo "\
Types: deb\n\
URIs: https://mirrors.tuna.tsinghua.edu.cn/debian/\n\
Suites: bookworm bookworm-updates bookworm-backports\n\
Components: main contrib non-free non-free-firmware\n\
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\
" > /etc/apt/sources.list.d/debian.sources

# --- 关键修复 2: 创建用户时，为其指定一个有效的主目录 ---
# 使用 --home /app 将用户的主目录设置为工作目录 /app

# 更新 apt 包列表并安装 cartopy 等库可能需要的系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libgeos-dev \
    tzdata \
    build-essential \
    libeccodes-dev \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
# Enable bytecode compilation
    UV_COMPILE_BYTECODE=1 \
# Copy from the cache instead of linking since it's a mounted volume
    PYTHONUNBUFFERED=1 \
    HOME=/app
# Ensure installed tools can be executed out of the box
    UV_TOOL_BIN_DIR=/usr/local/bin

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# 安装 Python 依赖
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# 声明容器运行时监听的端口
EXPOSE 8000

# --- 新增: 为数据和输出目录声明卷 ---
# 这明确表示这些目录用于存储持久化数据
VOLUME /app/data

# 容器启动时运行的命令
ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]