# audiography-silero-vad-service — batch voice-activity detection over HTTP.
#
# The last pipeline stage that had no container: without it `models-cpu` still
# left segmentation on the mock adapter, which derives cut points from file size.
#
# Main build context = ./backend; "model_files" is the Compose additional context.
#
# The ONNX weights are NOT baked in. They are ~2 MB and freely redistributable
# (silero-vad is MIT), but bundling them would mean this repository ships and
# implicitly vouches for a model binary it does not build. The operator supplies
# the same file the streaming overlay already asks for, bind-mounted read-only —
# see SILERO_VAD_MODEL_FILE in .env.example for the one-line curl.
FROM python:3.11.11-slim-bookworm

ARG APP_UID=10001
ARG APP_GID=10001

# ffmpeg only: the service decodes whatever container the caller uploads into
# 16 kHz mono PCM. No libsndfile, no build toolchain — onnxruntime ships wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" audiography \
    && useradd \
        --uid "${APP_UID}" \
        --gid "${APP_GID}" \
        --create-home \
        --shell /usr/sbin/nologin \
        audiography

ARG PYPI_INDEX_URL=https://pypi.org/simple

COPY --from=model_files silero-vad-service/requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install --timeout 120 --retries 10 \
        --index-url "${PYPI_INDEX_URL}" \
        -r /tmp/requirements.txt

WORKDIR /app

# Only what the service imports. It shares the streaming adapter's constants
# and ONNX conventions, so that module comes along — importing it must not drag
# in the rest of the package, which is why the copy list is this short.
COPY --chown=audiography:audiography audio_graphy/__init__.py /app/audio_graphy/__init__.py
COPY --chown=audiography:audiography audio_graphy/services/__init__.py /app/audio_graphy/services/__init__.py
COPY --chown=audiography:audiography audio_graphy/services/silero_vad_service.py /app/audio_graphy/services/silero_vad_service.py
COPY --chown=audiography:audiography audio_graphy/adapters/__init__.py /app/audio_graphy/adapters/__init__.py
COPY --chown=audiography:audiography audio_graphy/adapters/real/__init__.py /app/audio_graphy/adapters/real/__init__.py
COPY --chown=audiography:audiography audio_graphy/adapters/real/streaming_vad_silero.py /app/audio_graphy/adapters/real/streaming_vad_silero.py
COPY --chown=audiography:audiography audio_graphy/adapters/exceptions.py /app/audio_graphy/adapters/exceptions.py
COPY --chown=audiography:audiography audio_graphy/adapters/protocols.py /app/audio_graphy/adapters/protocols.py

ENV SILERO_VAD_MODEL_PATH=/models/silero_vad.onnx
# One ONNX thread and one in-flight request. The graph is small and the loop is
# sequential over 32 ms windows, so extra threads buy little; what they would
# cost is unbounded memory when several long recordings arrive together.
ENV SILERO_VAD_ONNX_THREADS=1
ENV SILERO_VAD_MAX_CONCURRENCY=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV HOME=/home/audiography

RUN chown -R audiography:audiography /app /home/audiography

USER audiography:audiography

EXPOSE 8008

# /health returns 200 with model_loaded=false when the ONNX file is missing, so
# gate the container on the body rather than the status: an unhealthy container
# is the honest signal that the operator has not supplied the model yet.
HEALTHCHECK --interval=15s --timeout=10s --start-period=30s --retries=6 \
    CMD python -c "\
import json, urllib.request, sys; \
body = json.load(urllib.request.urlopen('http://127.0.0.1:8008/health', timeout=5)); \
sys.exit(0 if body.get('model_loaded') else 'model not loaded: ' + str(body.get('error')))"

CMD ["uvicorn", "audio_graphy.services.silero_vad_service:app", "--host", "0.0.0.0", "--port", "8008"]
