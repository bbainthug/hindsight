# Personal Brain 测试/基准/长稳容器（任务包 CAREER-20260915-PB-REPRO-SOAK 任务 A）
#
# 复现边界：
# - 基础镜像 python:3.12-slim（Debian，arm64/amd64 均可构建）；
# - 依赖按 uv.lock 精确安装（uv sync --frozen，uv 版本与宿主机一致 0.12.7）；
# - 只拷贝 pyproject.toml / uv.lock / src / tests / evals/suites / config / docs；
#   evals/private、*.sqlite、真实归档与密钥由 .dockerignore + 显式 COPY 列表双重排除；
# - 非 root 用户 app（uid/gid 1000）运行；tzdata 供 zoneinfo 自然日期解析使用；
# - ENTRYPOINT ["brain"]；compose 中 test 服务以 entrypoint 覆盖直接跑 pytest。

FROM python:3.12-slim

# 依赖层：仅凭 pyproject.toml + uv.lock 安装（含 dev 组：pytest/ruff/mypy）
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.12.7 \
    && uv sync --frozen --no-install-project \
    && uv cache clean

# 源码层（改动最频繁，放最后以命中依赖层缓存）
COPY src ./src
COPY tests ./tests
COPY evals/suites ./evals/suites
COPY config ./config
COPY docs ./docs
RUN uv sync --frozen \
    && uv cache clean

# 运行环境：非 root + 时区数据 + venv 入口
# chmod：宿主机部分文件为 0600（umask 所致），COPY 原样保留会挡住非 root 读取
RUN chmod -R a+rX /app/src /app/tests /app/evals /app/config /app/docs \
    && chmod a+rX /app/pyproject.toml /app/uv.lock

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 --user-group app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

USER app
WORKDIR /app

ENTRYPOINT ["brain"]
CMD ["--help"]
