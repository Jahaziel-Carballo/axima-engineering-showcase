# ==============================================================================
# Axima GPU Base Image — Multi-stage optimization for ML inference
# ==============================================================================
# This Dockerfile (anonymized and commented) illustrates the patterns used to
# create the immutable base image for Axima's GPU document workers.
#
# Design goals:
#   - Reproducible builds with exact version pinning
#   - Minimal cold-start time (models baked into the image)
#   - Non-root execution for security
#   - Single-layer optimization for system dependencies
#   - Explicit CUDA/cuDNN version alignment with NVIDIA drivers
# ==============================================================================

# -------------------------------------------------------------------
# Stage 1: Base CUDA runtime from NVIDIA (official image)
# -------------------------------------------------------------------
# Version 12.6.3 aligns with the NVIDIA L4 driver branch (560+)
# used on Google Cloud g2-standard-12 instances.
FROM nvidia/cuda:12.6.3-cudnn9-runtime-ubuntu22.04

# -------------------------------------------------------------------
# Environment: suppress interactive prompts, enable unbuffered Python
# -------------------------------------------------------------------
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    XDG_CACHE_HOME=/home/workeruser/.cache

# -------------------------------------------------------------------
# Layer 1: System dependencies + Python + clean up (single RUN)
# -------------------------------------------------------------------
# Combining apt operations in one RUN reduces final image size and
# layer count. The rm -rf at the end ensures package caches don't
# bloat the image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.10 \
    python3-pip \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/bin/python3.10 /usr/bin/python

# -------------------------------------------------------------------
# Security: create non-root user BEFORE installing models
# -------------------------------------------------------------------
# If models are downloaded as root, they belong to root and the
# runtime user (workeruser) cannot write to cache directories.
# Creating the user here ensures model files are owned by workeruser.
RUN useradd -m -u 1000 workeruser
WORKDIR /app

# -------------------------------------------------------------------
# Layer 2: Python ML dependencies (exact versions pinned)
# -------------------------------------------------------------------
# Using --no-cache-dir keeps the image smaller.
# paddlepaddle-gpu is installed from a specific index for CUDA 12.6.
# orjson is preferred over stdlib json for its 3-5x serialization speed.
RUN pip install --no-cache-dir \
    paddlepaddle-gpu==3.4.0 \
    --extra-index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/ \
    && pip install --no-cache-dir \
    paddlex==3.4.2 \
    paddleocr==3.4.0 \
    opencv-python-headless \
    pypdfium2 \
    fastapi \
    uvicorn \
    google-cloud-storage \
    google-cloud-firestore \
    google-cloud-pubsub \
    pynvml>=11.5.0 \
    orjson>=3.10.0

# -------------------------------------------------------------------
# Layer 3: MODEL BAKING (the "oven")
# -------------------------------------------------------------------
# This is the critical optimization: models are downloaded and
# cached during the image build, not at container startup.
#
# By switching to the non-root user first, all model files under
# /home/workeruser/.cache/paddleocr are owned by workeruser.
#
# The forced CPU mode (use_gpu=False, device='cpu') ensures models
# download even on a build machine without a GPU, while keeping
# the image GPU-ready for runtime.
USER workeruser
RUN python -c "from paddleocr import PaddleOCR, PPStructureV3; \
    PaddleOCR(use_textline_orientation=True, lang='es', use_gpu=False); \
    PPStructureV3(use_doc_orientation_classify=True, device='cpu')"