# audiography-campplus-service — FastAPI wrapper around funasr CAM++.
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

COPY --from=model_files campplus-service/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app

# Copy only what the service needs.
COPY --chown=audiography:audiography audio_graphy/__init__.py /app/audio_graphy/__init__.py
COPY --chown=audiography:audiography audio_graphy/services/__init__.py /app/audio_graphy/services/__init__.py
COPY --chown=audiography:audiography audio_graphy/services/campplus_service.py /app/audio_graphy/services/campplus_service.py

ENV CAMPPLUS_DEVICE=cpu
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV HOME=/home/audiography
ENV MODELSCOPE_CACHE=/home/audiography/.cache/modelscope

RUN mkdir -p "${MODELSCOPE_CACHE}" \
    && chown -R audiography:audiography /app /home/audiography

USER audiography:audiography

EXPOSE 8007

HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=10 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8007/health', timeout=2).read()"

CMD ["uvicorn", "audio_graphy.services.campplus_service:app", "--host", "0.0.0.0", "--port", "8007"]
