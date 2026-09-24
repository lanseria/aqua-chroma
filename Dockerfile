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

# 更新 apt 包列表
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libgeos-dev \
    tzdata \
    build-essential \
    libeccodes-dev \
    libgl1 \
    libglib2.0-0 \
    fonts-wqy-zenhei \
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

# torch 默认拉取的是含 CUDA 的发行版（体积 ~2GB+），服务器为 CPU 环境时
# 强制替换为 CPU 版，镜像体积从数 GB 降至 ~200MB。
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev && \
    uv pip install --reinstall torch --index-url https://download.pytorch.org/whl/cpu && \
    uv cache prune

# --- Real-ESRGAN 超分权重 (models/*.pth 不入 git，构建时下载) ---
# 如构建机无法访问 GitHub，可预先下载权重后放至目录中，将 ADD 替换为 COPY models/RealESRGAN_x4plus.pth models/
# ADD https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth models/RealESRGAN_x4plus.pth
COPY models/RealESRGAN_x4plus.pth models/


# 拷贝整个项目代码
COPY . .

# 声明端口和卷
EXPOSE 8000
VOLUME /app/data

# 临时修改：使用此入口点进行调试，以查看确切的导入错误
# ENTRYPOINT ["python", "-c", "import app.main"]

# 原始入口点 (暂时注释掉)
ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]