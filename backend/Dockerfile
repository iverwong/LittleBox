FROM python:3.14-slim

WORKDIR /app

# Install curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency installation
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy pyproject.toml first for dependency caching
COPY pyproject.toml .

# Install dependencies
# TODO 国内开发用清华 PyPI 镜像加速。
# 生产/海外部署请去掉 UV_SYSTEM_PYPI_MIRROR 或改为 https://pypi.org/simple
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_SYSTEM_PYPI_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple \
    uv pip install --system -r pyproject.toml

# Copy application code
COPY app/ ./app/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
