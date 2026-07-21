# audiography-clap-service — GPU-mandatory FastAPI wrapper around laion_clap.
# Build context = ./backend so audio_graphy/services/clap_service.py is available.
FROM pytorch/pytorch:2.3.1-cuda12.1-cudnn8-runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps (FastAPI + laion_clap + librosa + torch already in base).
COPY docker/clap-service/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Pre-download CLAP checkpoint (~600 MB) at build time so cold start is fast.
RUN python -c "from laion_clap import CLAP_Module; CLAP_Module(enable_fusion=False, amodel='HTSAT-base').load_ckpt()" \
    || echo "[warn] CLAP checkpoint pre-download failed; will retry at startup"

# Copy only what the service needs (keep image lean).
COPY audio_graphy/__init__.py /app/audio_graphy/__init__.py
COPY audio_graphy/services/clap_service.py /app/audio_graphy/services/clap_service.py
COPY audio_graphy/services/__init__.py /app/audio_graphy/services/__init__.py

# Empty __init__.py files are needed for the audio_graphy package import.
RUN touch /app/audio_graphy/__init__.py /app/audio_graphy/services/__init__.py

ENV CUDA_VISIBLE_DEVICES=0
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8006
CMD ["uvicorn", "audio_graphy.services.clap_service:app", "--host", "0.0.0.0", "--port", "8006"]
