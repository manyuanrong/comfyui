# =============================================================================
# Kera2 — Krea 2 Turbo Serverless Worker for RunPod
# Built via RunPod GitHub Integration (30-min build limit, CPU-only)
#
# Layer strategy (cache-optimized):
#   L1: System deps + uv     ← rarely changes
#   L2: All models (~18GB)   ← pinned version, cached forever
#   L3: PyTorch + ComfyUI    ← stable
#   L4: Smoke test            ← stable
#   L5: Handler + app code    ← changes most often (fast rebuild)
# =============================================================================

FROM nvidia/cuda:13.0.3-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_PREFER_BINARY=1
ENV PYTHONUNBUFFERED=1
ENV CMAKE_BUILD_PARALLEL_LEVEL=8
ENV PIP_NO_INPUT=1

# ===========================================================================
# Layer 1: System deps + uv (cached — rarely changes)
# ===========================================================================
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

# ===========================================================================
# Layer 2: All models baked into image (cached — pinned version)
#   • Text encoder: Qwen3-VL 4B fp8 (~4GB)
#   • VAE: Qwen Image VAE (~200MB)
#   • Main model: moody-krea-mix int8 tensorwise (~13.5GB)
# ===========================================================================
RUN mkdir -p /comfyui/models/text_encoders \
             /comfyui/models/vae \
             /comfyui/models/diffusion_models \
    && echo "Downloading Qwen3-VL text encoder (~4GB)..." \
    && wget -q --show-progress \
        -O /comfyui/models/text_encoders/qwen3vl_4b_fp8_scaled.safetensors \
        "https://huggingface.co/Comfy-Org/Qwen3-VL/resolve/main/text_encoders/qwen3vl_4b_fp8_scaled.safetensors" \
    && echo "Downloading Qwen Image VAE (~200MB)..." \
    && wget -q --show-progress \
        -O /comfyui/models/vae/qwen_image_vae.safetensors \
        "https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/main/split_files/vae/qwen_image_vae.safetensors" \
    && echo "Downloading moody-krea-mix int8 model (~13.5GB)..." \
    && wget -q --show-progress \
        -O /comfyui/models/diffusion_models/Moody-Krea-Mix-v3G_00001__int8_tensorwise.safetensors \
        "https://huggingface.co/catlover1937/moody-krea-mix/resolve/main/Moody-Krea-Mix-v3G_00001__int8_tensorwise.safetensors"

# ===========================================================================
# Layer 3: PyTorch nightly (CUDA 13.0) + ComfyUI + deps (cached — stable)
# ===========================================================================
RUN uv pip install pip setuptools wheel \
    && echo "Installing PyTorch nightly for CUDA 13.0..." \
    && uv pip install --pre torch torchvision torchaudio \
        --index-url https://download.pytorch.org/whl/nightly/cu130 \
    && echo "Cloning ComfyUI v0.27.0..." \
    && git clone --depth 1 --branch v0.27.0 \
        https://github.com/Comfy-Org/ComfyUI.git /comfyui \
    && echo "Installing ComfyUI dependencies..." \
    && uv pip install -r /comfyui/requirements.txt \
    && for r in /comfyui/custom_nodes/*/requirements.txt; do \
         [ -f "$r" ] && uv pip install -r "$r" || true; \
       done \
    && uv pip install "transformers>=4.50.3" "huggingface-hub>=0.26" \
    && echo "ComfyUI + PyTorch installed"

# ===========================================================================
# Layer 4: Build-time smoke test (CPU — verifies import graph)
# ===========================================================================
RUN cd /comfyui && timeout 300 python main.py --quick-test-for-ci --cpu \
    && echo "Smoke test passed"

# ===========================================================================
# Layer 5: Handler deps + application code (changes most often)
# ===========================================================================
RUN uv pip install "runpod~=1.10.0" requests websocket-client

WORKDIR /comfyui
ADD src/extra_model_paths.yaml ./

WORKDIR /
ADD src/start.sh src/network_volume.py handler.py test_input.json ./
ADD .runpod ./.runpod/
RUN chmod +x /start.sh

CMD ["/start.sh"]
