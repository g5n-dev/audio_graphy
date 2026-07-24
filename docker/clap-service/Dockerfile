# audiography-clap-service — GPU-mandatory FastAPI wrapper around laion_clap.
# Main build context = ./backend; "model_files" is the Compose additional context.
FROM pytorch/pytorch:2.3.1-cuda12.1-cudnn8-runtime

ARG APP_UID=10001
ARG APP_GID=10001

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" audiography \
    && useradd \
        --uid "${APP_UID}" \
        --gid "${APP_GID}" \
        --create-home \
        --shell /usr/sbin/nologin \
        audiography

# Install Python deps (FastAPI + laion_clap + librosa + torch already in base).
COPY --from=model_files clap-service/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app

# Copy only what the service needs (keep image lean).
COPY --chown=audiography:audiography audio_graphy/__init__.py /app/audio_graphy/__init__.py
COPY --chown=audiography:audiography audio_graphy/services/clap_service.py /app/audio_graphy/services/clap_service.py
COPY --chown=audiography:audiography audio_graphy/services/__init__.py /app/audio_graphy/services/__init__.py

ENV CUDA_VISIBLE_DEVICES=0
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV HOME=/home/audiography
ENV TORCH_HOME=/home/audiography/.cache/torch

RUN mkdir -p "${TORCH_HOME}" \
    && chown -R audiography:audiography /app /home/audiography

USER audiography:audiography

EXPOSE 8006

HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --retries=20 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8006/health', timeout=2).read()"

CMD ["uvicorn", "audio_graphy.services.clap_service:app", "--host", "0.0.0.0", "--port", "8006"]
