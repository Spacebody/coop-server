# 多阶段构建,产出最小镜像
FROM python:3.12-slim AS builder

WORKDIR /build

# 先装依赖,利用 docker 层缓存
COPY pyproject.toml ./
COPY coop_server/ ./coop_server/
COPY cli/ ./cli/

RUN pip install --no-cache-dir --target=/install .

# ---

FROM python:3.12-slim

# 创建非 root 用户运行
RUN useradd -u 10000 -m coop

# 拷贝已装好的 packages
COPY --from=builder /install /usr/local/lib/python3.12/site-packages

# 拷贝代码
COPY --from=builder /build/coop_server /app/coop_server
COPY --from=builder /build/cli /app/cli

# 数据目录(挂载点)
RUN mkdir -p /data && chown coop:coop /data

WORKDIR /app
USER coop

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://localhost:7777/health').read()" \
        || exit 1

EXPOSE 7777

ENTRYPOINT ["python", "-m", "coop_server"]
CMD ["--config", "/app/config.yaml"]
