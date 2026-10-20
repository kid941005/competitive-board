# syntax=docker/dockerfile:1
# 竞品情报看板 —— 单镜像承载全部功能（对比页 + 规格书导入 + REST API）。
# 展示层是服务端渲染（Jinja2 + 原生 JS），因此不需要 Node 构建阶段，也不需要第二个镜像。
FROM python:3.12-slim

LABEL org.opencontainers.image.title="competitive-board" \
      org.opencontainers.image.description="压电领域友商竞品参数对比看板" \
      org.opencontainers.image.version="0.2.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    CIB_DATA_DIR=/app/data

# 仅安装时区数据；pymupdf 官方提供 manylinux wheel，无需额外的系统库。
# DEBIAN_FRONTEND 必须显式关掉交互，否则 tzdata 在无 TTY 的构建里可能卡住等输入。
RUN DEBIAN_FRONTEND=noninteractive apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tzdata \
 && ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime \
 && echo "${TZ}" > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖单独一层：改业务代码不会触发重装
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY tools ./tools
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# 非 root 运行。命名卷首次创建时会继承镜像内 /app/data 的属主，因此可写。
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
 && mkdir -p /app/data/datasheets \
 && chmod +x /usr/local/bin/docker-entrypoint.sh \
 && chown -R appuser:appuser /app

USER appuser

VOLUME ["/app/data"]
EXPOSE 8000

# start-period 给足首次建表与灌示例数据的时间
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--workers", "1"]
