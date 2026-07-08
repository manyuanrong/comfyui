# =============================================================================
# Kera2 — Krea 2 Turbo Serverless Worker for RunPod
# Built via RunPod GitHub Integration (30-min build limit, CPU-only)
#
# Key optimizations for GitHub build:
#   • Skip comfy-cli → manual git clone + direct pip install (no double-PyTorch)
#   • PyTorch nightly cu130 installed ONCE (saves ~2-3GB re-download)
#   • Merged RUN layers to reduce intermediate layer writes
#   • --quick-test-for-ci --cpu smoke test at build time
# =============================================================================

FROM nvidia/cuda:13.0.3-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_PREFER_BINARY=1
ENV PYTHONUNBUFFERED=1
ENV CMAKE_BUILD_PARALLEL_LEVEL=8
ENV PIP_NO_INPUT=1

# ---------------------------------------------------------------------------
# Layer 1: System deps + uv + Python tools
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.12 python3.12-venv python3.12-dev \
    git wget curl \
    libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
    ffmpeg openssh-server \
    && ln -sf /usr/bin/python3.12 /usr/bin/python \
    && ln -sf /usr/bin/pip3 /usr/bin/pip \
    && apt-get autoremove -y && apt-get clean -y && rm -rf /var/lib/apt/lists/* \
    && wget -qO- https://astral.sh/uv/install.sh | sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv \
    && ln -s /root/.local/bin/uvx /usr/local/bin/uvx \
    && uv venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}"

# ---------------------------------------------------------------------------
# Layer 2: PyTorch nightly (CUDA 13.0) + ComfyUI + deps — ONE pip pass
# ---------------------------------------------------------------------------
RUN uv pip install pip setuptools wheel \
    # PyTorch nightly for CUDA 13.0 (single install, no double-download)
    && uv pip install --pre torch torchvision torchaudio \
        --index-url https://download.pytorch.org/whl/nightly/cu130 \
    # Clone ComfyUI
    && git clone --depth 1 --branch v0.27.0 \
        https://github.com/Comfy-Org/ComfyUI.git /comfyui \
    # Install ComfyUI dependencies
    && uv pip install -r /comfyui/requirements.txt \
    && for r in /comfyui/custom_nodes/*/requirements.txt; do \
         [ -f "$r" ] && uv pip install -r "$r" || true; \
       done \
    && uv pip install "transformers>=4.50.3,<5" "huggingface-hub<1.0"

# ---------------------------------------------------------------------------
# Layer 3: Krea 2 models baked into image (text encoder + VAE)
# Main diffusion model loaded via HuggingFace Model Cache at runtime
# ---------------------------------------------------------------------------
RUN mkdir -p /comfyui/models/text_encoders \
             /comfyui/models/vae \
             /comfyui/models/diffusion_models \
    # Qwen3-VL 4B text encoder (fp8, ~4GB)
    && wget -q -O /comfyui/models/text_encoders/qwen3vl_4b_fp8_scaled.safetensors \
        "https://huggingface.co/Comfy-Org/Qwen3-VL/resolve/main/text_encoders/qwen3vl_4b_fp8_scaled.safetensors" \
    # Qwen Image VAE (~200MB)
    && wget -q -O /comfyui/models/vae/qwen_image_vae.safetensors \
        "https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/main/split_files/vae/qwen_image_vae.safetensors"

# ---------------------------------------------------------------------------
# Layer 4: Build-time smoke test (CPU-only — verifies import graph)
# ---------------------------------------------------------------------------
RUN cd /comfyui && timeout 300 python main.py --quick-test-for-ci --cpu

# ---------------------------------------------------------------------------
# Layer 5: Handler deps + application code
# ---------------------------------------------------------------------------
RUN uv pip install runpod requests websocket-client

WORKDIR /comfyui
ADD src/extra_model_paths.yaml ./

WORKDIR /
ADD src/start.sh src/network_volume.py handler.py test_input.json ./
RUN chmod +x /start.sh

CMD ["/start.sh"]
