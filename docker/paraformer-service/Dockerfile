# audiography-paraformer-service — FastAPI wrapper around funasr Paraformer,
# speaking the OpenAI /v1/audio/transcriptions contract the ASR adapter expects.
#
# Replaces image `funasr/server:1.0.5`, which does not exist on Docker Hub
# (repository 404s, no tags at all) and therefore could never be pulled — on
# arm64 or anything else. Built from the funasr Python package instead, which
# is arm64-native, same as campplus-service.
#
# CPU-default; set FUNASR_DEVICE=cuda at runtime for GPU.
# Main build context = ./backend; "model_files" is the Compose additional context.
FROM python:3.11.11-slim-bookworm

ARG APP_UID=10001
ARG APP_GID=10001

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" audiography \
    && useradd \
        --uid "${APP_UID}" \
        --gid "${APP_GID}" \
        --create-home \
        --shell /usr/sbin/nologin \
        audiography

# CPU-only torch by default. PyPI's torch wheel depends on the full nvidia_*
# CUDA stack — ~4 GB of wheels that are dead weight on any CPU deployment and
# unusable on arm64 in the first place. Pulling them also made this build fail
# repeatedly on transient PyPI stalls and one truncated-download hash mismatch.
# For a CUDA host, override with the default PyPI index:
#   --build-arg TORCH_INDEX_URL=https://pypi.org/simple
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
# PyPI_INDEX_URL exists so slow-link deployments can point at a mirror, e.g.
#   --build-arg PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PYPI_INDEX_URL=https://pypi.org/simple
# The cache mounts matter more than they look: these downloads are hundreds of
# MB and this link stalls mid-stream often enough that several builds died on
# read timeouts (and once on a truncated wheel that failed its hash check).
# With a persistent pip cache a retried build resumes from what already landed
# instead of starting the whole set over.
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install --timeout 120 --retries 10 \
    --index-url "${TORCH_INDEX_URL}" \
    torch torchaudio

COPY --from=model_files paraformer-service/requirements.txt /tmp/requirements.txt
# torch is already satisfied above, so pip leaves it alone here.
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install --timeout 120 --retries 10 \
    --index-url "${PYPI_INDEX_URL}" \
    -r /tmp/requirements.txt

WORKDIR /app

# Copy only what the service needs.
COPY --chown=audiography:audiography audio_graphy/__init__.py /app/audio_graphy/__init__.py
COPY --chown=audiography:audiography audio_graphy/services/__init__.py /app/audio_graphy/services/__init__.py
COPY --chown=audiography:audiography audio_graphy/services/paraformer_service.py /app/audio_graphy/services/paraformer_service.py

ENV FUNASR_DEVICE=cpu
# librosa computes duration through numba, which insists on a writable cache
# directory. The root filesystem is read-only, so without this every request
# dies with "cannot cache function ... no locator available". /tmp is tmpfs.
ENV NUMBA_CACHE_DIR=/tmp/numba_cache
ENV XDG_CACHE_HOME=/tmp/xdg_cache
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV HOME=/home/audiography
ENV MODELSCOPE_CACHE=/home/audiography/.cache/modelscope

RUN mkdir -p "${MODELSCOPE_CACHE}" \
    && chown -R audiography:audiography /app /home/audiography

USER audiography:audiography

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=300s --retries=20 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()"

CMD ["uvicorn", "audio_graphy.services.paraformer_service:app", "--host", "0.0.0.0", "--port", "8000"]
