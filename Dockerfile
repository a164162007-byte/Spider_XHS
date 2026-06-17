FROM python:3.10-slim

WORKDIR /app

# 安装系统依赖（opencv-python 需要 libgl1 等）
RUN apt-get update && apt-get install -y \
    curl \
    gnupg \
    build-essential \
    git \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 安装 Node.js 20
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# 验证环境
RUN python --version && node --version && npm --version

# 先装 Python 依赖（利用 Docker 缓存层）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 先装 Node.js 依赖（利用 Docker 缓存层）
COPY package.json package-lock.json ./
RUN npm install --production

# 复制项目代码
COPY . .

# 数据输出目录
RUN mkdir -p /app/datas/media_datas /app/datas/excel_datas

EXPOSE 5000

ENV PYTHONUNBUFFERED=1
ENV NODE_ENV=production

# 默认启动原有爬虫入口（原功能不变）
CMD ["python", "-m", "spider.spider"]
