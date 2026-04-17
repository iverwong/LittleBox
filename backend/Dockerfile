FROM python:3.14-slim

WORKDIR /app

# Install curl for healthcheck
# 国内开发用清华 Debian 镜像加速；生产/海外部署可删除此 RUN 段
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list; \
    fi

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency installation
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy pyproject.toml first for dependency caching
COPY pyproject.toml .

# 国内开发用清华 PyPI 镜像加速；生产/海外部署请去掉本行或改为 https://pypi.org/simple
ENV UV_SYSTEM_PYPI_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple

ARG INSTALL_DEV=false
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$INSTALL_DEV" = "true" ]; then \
        uv pip install --system -r pyproject.toml --extra dev; \
    else \
        uv pip install --system -r pyproject.toml; \
    fi

# Copy application code
COPY app/ ./app/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
