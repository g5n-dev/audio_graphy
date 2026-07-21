# audiography-campplus-service — FastAPI wrapper around funasr CAM++.
# CPU-default; set CAMPPLUS_DEVICE=cuda at runtime for GPU.
# Build context = ./backend so audio_graphy/services/campplus_service.py is available.
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY docker/campplus-service/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy only what the service needs.
COPY audio_graphy/__init__.py /app/audio_graphy/__init__.py
COPY audio_graphy/services/__init__.py /app/audio_graphy/services/__init__.py
COPY audio_graphy/services/campplus_service.py /app/audio_graphy/services/campplus_service.py

RUN touch /app/audio_graphy/__init__.py /app/audio_graphy/services/__init__.py

ENV CAMPPLUS_DEVICE=cpu
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8007
CMD ["uvicorn", "audio_graphy.services.campplus_service:app", "--host", "0.0.0.0", "--port", "8007"]
