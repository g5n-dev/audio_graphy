# audiography-campplus-service — combined speech service over funasr:
# voiceprint extraction, speaker diarization and ASR in one container.
# CPU-default; set CAMPPLUS_DEVICE=cuda at runtime for GPU.
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

# CPU-only torch by default, matching paraformer-service. PyPI's torch wheel
# depends on the full nvidia_* CUDA stack — measured at 2.9 GB of
# site-packages/nvidia in this image, and this container never uses it:
# docker-compose.yml hardcodes CAMPPLUS_DEVICE=cpu and reserves no GPU device in
# any of the three model profiles, so torch.cuda.is_available() is False even
# under models-multi-gpu. For a CUDA host, override with the default PyPI index:
#   --build-arg TORCH_INDEX_URL=https://pypi.org/simple
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
# PYPI_INDEX_URL exists so slow-link deployments can point at a mirror, e.g.
#   --build-arg PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PYPI_INDEX_URL=https://pypi.org/simple
# Pinned for the same reason funasr is: unpinned, this layer resolves to whatever
# is newest at build time, so every rebuild silently upgrades the one dependency
# sitting underneath a contractually verified endpoint (192-d, L2-normalized).
# These are the versions the combined pipeline was measured working on, and the
# CPU index publishes both for manylinux_2_28_aarch64 — the specifiers carry no
# local suffix, so they match +cpu here and +cu130 from PyPI alike.
#
# The persistent pip cache is not an optimisation: these wheels are hundreds of
# MB each and this link has stalled mid-stream often enough to kill builds on
# read timeouts and once on a truncated wheel that failed its hash check. With
# the cache a retried build resumes from what already landed.
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install --timeout 120 --retries 10 \
        --index-url "${TORCH_INDEX_URL}" \
        torch==2.13.0 torchaudio==2.11.0

COPY --from=model_files campplus-service/requirements.txt /tmp/requirements.txt
# torch / torchaudio are already satisfied above, so pip leaves them alone here
# and the requirements pins act only as a floor. If a transitive constraint ever
# forces a reinstall, it comes from PyPI and drags the CUDA stack back in — the
# build below asserts that has not happened rather than trusting it.
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install --timeout 120 --retries 10 \
        --index-url "${PYPI_INDEX_URL}" \
        -r /tmp/requirements.txt \
    && python -c "\
import pathlib, site, sys; \
p = pathlib.Path(site.getsitepackages()[0]) / 'nvidia'; \
sys.exit('CUDA stack reinstalled by a transitive constraint: ' + str(p)) if p.exists() else None" \
    && python -c "import torch; print('torch', torch.__version__)"

WORKDIR /app

# Copy only what the service needs.
COPY --chown=audiography:audiography audio_graphy/__init__.py /app/audio_graphy/__init__.py
COPY --chown=audiography:audiography audio_graphy/services/__init__.py /app/audio_graphy/services/__init__.py
COPY --chown=audiography:audiography audio_graphy/services/campplus_service.py /app/audio_graphy/services/campplus_service.py

ENV CAMPPLUS_DEVICE=cpu
# Start loading the ~1.2 GB ASR + speaker pipeline as soon as the app is up,
# rather than on the first /v1/diarize call: a warm load is ~120 s, longer than
# any caller's timeout. Set to 0 on a memory-constrained host to defer it —
# /v1/voiceprint/extract does not need it and never waits on it.
ENV CAMPPLUS_ASR_PRELOAD=1
# librosa compiles kernels through numba, which insists on a writable cache
# directory. The root filesystem is read-only, so without this every request
# that crops audio — which is every voiceprint extraction the sampler makes —
# dies with "no locator available". /tmp is the container's tmpfs.
ENV NUMBA_CACHE_DIR=/tmp/numba_cache
# Also relocates the caches that derive from it onto tmpfs — huggingface_hub
# (HF_HOME) and torch (TORCH_HOME) both default to $XDG_CACHE_HOME/<name>, and
# both are unwritable under the read-only root without this.
ENV XDG_CACHE_HOME=/tmp/xdg_cache
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV HOME=/home/audiography
ENV MODELSCOPE_CACHE=/home/audiography/.cache/modelscope

RUN mkdir -p "${MODELSCOPE_CACHE}" \
    && chown -R audiography:audiography /app /home/audiography

USER audiography:audiography

EXPOSE 8007

# Liveness only, and deliberately so: /health returns 200 once the SV model is
# up, without waiting for the ASR pipeline. Gating container health on the ASR
# pipeline would mark the container unhealthy whenever diarization is
# unavailable and take voiceprint extraction — the verified path — down with it.
# Read asr_loaded / spk_loaded from the body for ASR readiness.
HEALTHCHECK --interval=15s --timeout=10s --start-period=120s --retries=10 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8007/health', timeout=5).read()"

CMD ["uvicorn", "audio_graphy.services.campplus_service:app", "--host", "0.0.0.0", "--port", "8007"]
