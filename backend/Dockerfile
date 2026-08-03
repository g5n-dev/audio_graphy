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
    pip install --prefix=/install .

# --- runtime ---------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/usr/local/bin:$PATH"

# Runtime system deps. ffmpeg provides both ffmpeg and ffprobe for auditable
# physical reception assembly.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 app

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --chown=app:app . /app

# WORKING_DIR's mount point must exist in the image and be owned by app. Docker
# seeds a fresh named volume from the image path — including its ownership — but
# when the path is absent it creates the mount point root-owned, and this
# container runs as uid 1000 with no way to chown it. The result is a backend
# that starts, answers /health, and fails /health/readiness with
# "not writable: /data/working_dir" — healthy-looking but unable to store audio.
RUN mkdir -p /data/working_dir && chown -R app:app /data

# Alembic + uvicorn live in /usr/local/bin (copied from builder). Test and
# lint tooling stay outside the runtime image and run in CI/the local venv.

USER app

EXPOSE 8000

# Default command — overridden by docker-compose to run alembic then uvicorn --reload
CMD ["uvicorn", "audio_graphy.main:app", "--host", "0.0.0.0", "--port", "8000"]
