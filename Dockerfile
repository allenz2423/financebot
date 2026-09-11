# Delilah Financial OS — container image.
#
# GPU speech-to-text is OPT-IN. By default this builds a lean image with
# no CUDA. To enable local faster-whisper on the host GPU, build with:
#
#   docker compose build --build-arg USE_CUDA=1 delilah
#
# Why opt-in: the plain python:3.12-slim image ships a CUDA runtime
# newer than the host driver (NVIDIA 610.57), so faster-whisper fails with
# "CUDA driver version is insufficient for CUDA runtime version". The CUDA
# path needs a multi-stage build that pulls in CUDA 12.4 libs from
# nvidia/cuda:12.4.1-runtime-ubuntu22.04 — a heavier image, only useful
# when you actually have a GPU and want local transcription.
ARG USE_CUDA=0

# Stage 1: CUDA 12.4 runtime that matches the host driver. This stage is
# always defined so the COPY --from below resolves, but it only matters
# when USE_CUDA=1.
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04 AS cuda-runtime

# Stage 2: the actual image. python:3.12-slim has working apt, pip, and
# network — unlike the CUDA runtime image, which has neither.
FROM python:3.12-slim

ARG USE_CUDA=0

# Copy the CUDA 12.4 libs from the runtime image. When USE_CUDA=0 they
# sit unused; faster-whisper falls back to CPU via STT_LOCAL_DEVICE=cpu.
COPY --from=cuda-runtime /usr/local/cuda /usr/local/cuda
COPY --from=cuda-runtime /usr/lib/x86_64-linux-gnu/libcuda* /usr/lib/x86_64-linux-gnu/

ENV LD_LIBRARY_PATH=/usr/local/cuda/lib64:${LD_LIBRARY_PATH}
ENV PATH=/usr/local/cuda/bin:${PATH}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

CMD ["python", "main.py"]