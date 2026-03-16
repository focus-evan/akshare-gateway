# ============================================================
# AKShare Gateway — Dockerfile
# ============================================================
# 使用 Python 3.11 slim 镜像，安装 akshare 及 FastAPI
# 运行端口: 9898
# ============================================================

FROM python:3.11-slim

LABEL maintainer="Evan <344983176@qq.com>"
LABEL description="AKShare HTTP API Gateway for ai-stock"

# 时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制应用代码
COPY app.py .

# 暴露端口
EXPOSE 9898

# 启动命令 — 使用 gunicorn + uvicorn worker
# workers=2 足够处理 ai-stock 的请求量
CMD ["gunicorn", "app:app", \
     "--bind", "0.0.0.0:9898", \
     "--workers", "2", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-"]
