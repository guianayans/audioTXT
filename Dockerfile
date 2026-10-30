# Base CUDA 12.8 + cuDNN 9 (runtime) — compatível com a RTX 5070 Ti (Blackwell/sm_120)
# e com o CTranslate2/faster-whisper, que exigem CUDA 12 + cuDNN 9.
FROM nvidia/cuda:12.8.0-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/data/hf \
    HF_HUB_ENABLE_HF_TRANSFER=0 \
    HF_HUB_DISABLE_PROGRESS_BARS=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

# Vendoriza o Chart.js (sem depender de CDN em produção)
RUN mkdir -p web/vendor \
    && curl -fsSL -o web/vendor/chart.min.js \
       "https://cdn.jsdelivr.net/npm/chart.js@4.4.6/dist/chart.umd.min.js"

EXPOSE 8000

HEALTHCHECK --interval=20s --timeout=5s --retries=6 --start-period=40s \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "75"]
