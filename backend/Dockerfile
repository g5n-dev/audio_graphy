# ============================================================
# AudioGraphy backend image
# Multi-stage: builder (deps) + runtime (slim)
# ============================================================
FROM python:3.13-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps for compilation (aiomysql etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        default-libmysqlclient-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY audio_graphy ./audio_graphy

RUN pip install --upgrade pip && \
    pip install --prefix=/install -e ".[dev]"

# --- runtime ---------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/usr/local/bin:$PATH"

# Runtime system deps (libmysqlclient for aiomysql, curl for healthchecks)
RUN apt-get update && apt-get install -y --no-install-recommends \
        default-libmysqlclient-dev \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 app

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --chown=app:app . /app

# Alembic + uvicorn live in /usr/local/bin (copied from builder)
# Pytest / ruff / mypy available too for dev iteration inside container.

USER app

EXPOSE 8000

# Default command — overridden by docker-compose to run alembic then uvicorn --reload
CMD ["uvicorn", "audio_graphy.main:app", "--host", "0.0.0.0", "--port", "8000"]
