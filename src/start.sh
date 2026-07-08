#!/usr/bin/env bash
# =============================================================================
# Kera2 - Container startup script
# =============================================================================
set -e

# ---- SSH server (if PUBLIC_KEY is set) ----
if [ -n "$PUBLIC_KEY" ]; then
    mkdir -p ~/.ssh
    echo "$PUBLIC_KEY" > ~/.ssh/authorized_keys
    chmod 700 ~/.ssh
    chmod 600 ~/.ssh/authorized_keys

    for key_type in rsa ecdsa ed25519; do
        key_file="/etc/ssh/ssh_host_${key_type}_key"
        if [ ! -f "$key_file" ]; then
            ssh-keygen -t "$key_type" -f "$key_file" -q -N ''
        fi
    done

    service ssh start && echo "kera2: SSH server started" || echo "kera2: SSH server could not be started" >&2
fi

# ---- libtcmalloc for better memory management ----
TCMALLOC="$(ldconfig -p | grep -Po "libtcmalloc.so.\d" | head -n 1)"
export LD_PRELOAD="${TCMALLOC}"

# ---- GPU pre-flight check ----
echo "kera2: Checking GPU availability..."
if ! GPU_CHECK=$(python3 -c "
import torch
try:
    torch.cuda.init()
    name = torch.cuda.get_device_name(0)
    cap = torch.cuda.get_device_capability(0)
    _ = (torch.zeros(8, device='cuda') + 1).sum().item()
    torch.cuda.synchronize()
    print(f'OK: {name} (sm_{cap[0]}{cap[1]}), torch {torch.__version__}, cuda {torch.version.cuda}')
except Exception as e:
    print(f'FAIL: {e}')
    exit(1)
" 2>&1); then
    echo "kera2: GPU is not available or incompatible with this PyTorch build:"
    echo "kera2: $GPU_CHECK"
    exit 1
fi
echo "kera2: GPU available — $GPU_CHECK"

# ---- ComfyUI log level ----
: "${COMFY_LOG_LEVEL:=DEBUG}"

# ---- PID file for health checks ----
COMFY_PID_FILE="/tmp/comfyui.pid"

# ---- Link HF cached models into ComfyUI directories ----
# RunPod Model Cache stores files at:
#   /runpod-volume/huggingface-cache/hub/models--{org}--{repo}/snapshots/{hash}/
# ComfyUI expects them at /comfyui/models/{type}/ or /runpod-volume/models/{type}/
echo "kera2: Linking HF cached models..."
if [ -d "/runpod-volume/huggingface-cache/hub" ]; then
    # Link moody-krea-mix diffusion models
    HF_CACHE="/runpod-volume/huggingface-cache/hub/models--catlover1937--moody-krea-mix"
    DEST="/comfyui/models/diffusion_models"
    if [ -d "$HF_CACHE" ]; then
        mkdir -p "$DEST"
        for snapshot in "$HF_CACHE"/snapshots/*/; do
            [ -d "$snapshot" ] || continue
            echo "kera2: Linking models from $snapshot to $DEST/"
            for f in "$snapshot"*.safetensors; do
                [ -f "$f" ] || continue
                base="$(basename "$f")"
                if [ ! -e "$DEST/$base" ]; then
                    ln -sf "$f" "$DEST/$base"
                    echo "kera2:   Linked $base"
                fi
            done
        done
    fi
fi

# ---- Start ComfyUI in background ----
echo "kera2: Starting ComfyUI..."
python -u /comfyui/main.py \
    --disable-auto-launch \
    --disable-metadata \
    --verbose "${COMFY_LOG_LEVEL}" \
    --log-stdout &
echo $! > "$COMFY_PID_FILE"

# ---- Start RunPod handler ----
if [ "$SERVE_API_LOCALLY" == "true" ]; then
    echo "kera2: Starting RunPod Handler (local API mode)"
    python -u /handler.py --rp_serve_api --rp_api_host=0.0.0.0
else
    echo "kera2: Starting RunPod Handler"
    python -u /handler.py
fi
