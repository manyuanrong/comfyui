"""
Network Volume diagnostics for Kera2.

Enable with NETWORK_VOLUME_DEBUG=true to debug network volume model path issues.
"""

import os

MODEL_TYPES = {
    "checkpoints": [".safetensors", ".ckpt", ".pt", ".pth", ".bin"],
    "clip": [".safetensors", ".pt", ".bin"],
    "clip_vision": [".safetensors", ".pt", ".bin"],
    "configs": [".yaml", ".json"],
    "controlnet": [".safetensors", ".pt", ".pth", ".bin"],
    "embeddings": [".safetensors", ".pt", ".bin"],
    "loras": [".safetensors", ".pt"],
    "upscale_models": [".safetensors", ".pt", ".pth"],
    "vae": [".safetensors", ".pt", ".bin"],
    "unet": [".safetensors", ".pt", ".bin"],
}


def is_network_volume_debug_enabled():
    """Check if network volume debug mode is enabled."""
    return os.environ.get("NETWORK_VOLUME_DEBUG", "false").lower() == "true"


def run_network_volume_diagnostics():
    """Run comprehensive network volume diagnostics and print results."""
    print("=" * 70)
    print("NETWORK VOLUME DIAGNOSTICS (NETWORK_VOLUME_DEBUG=true)")
    print("=" * 70)

    extra_model_paths_file = "/comfyui/extra_model_paths.yaml"
    print("\n[1] Checking extra_model_paths.yaml configuration...")
    if os.path.isfile(extra_model_paths_file):
        print(f"    FOUND: {extra_model_paths_file}")
        with open(extra_model_paths_file, "r") as f:
            for line in f.read().split("\n"):
                print(f"      {line}")
    else:
        print(f"    NOT FOUND: {extra_model_paths_file}")
        print("    This file is required for ComfyUI to find models on the network volume.")

    runpod_volume = "/runpod-volume"
    print(f"\n[2] Checking network volume mount at {runpod_volume}...")
    if os.path.isdir(runpod_volume):
        print(f"    MOUNTED: {runpod_volume}")
    else:
        print(f"    NOT MOUNTED: {runpod_volume}")
        print("    Make sure you have attached a network volume to your serverless endpoint.")
        print("=" * 70)
        return

    print("\n[3] Checking directory structure...")
    models_dir = os.path.join(runpod_volume, "models")
    if os.path.isdir(models_dir):
        print(f"    FOUND: {models_dir}")
    else:
        print(f"    NOT FOUND: {models_dir}")
        print("\n    Expected directory structure:")
        _print_expected_structure()
        print("=" * 70)
        return

    print("\n[4] Scanning model directories...")
    found_any = False
    for model_type, extensions in MODEL_TYPES.items():
        model_path = os.path.join(models_dir, model_type)
        if os.path.isdir(model_path):
            files = []
            try:
                for f in os.listdir(model_path):
                    file_path = os.path.join(model_path, f)
                    if os.path.isfile(file_path):
                        ext = os.path.splitext(f)[1].lower()
                        if ext in extensions:
                            size = os.path.getsize(file_path)
                            files.append(f"{f} ({_format_size(size)})")
                            found_any = True
                        else:
                            files.append(f"{f} (ignored - invalid extension)")
            except Exception as e:
                print(f"    {model_type}/: Error reading directory - {e}")
                continue

            if files:
                print(f"\n    {model_type}/:")
                for f in files:
                    print(f"      - {f}")
            else:
                print(f"\n    {model_type}/: (empty)")
        else:
            print(f"\n    {model_type}/: (directory not found)")

    print("\n[5] Summary")
    if found_any:
        print("    Models found on network volume.")
    else:
        print("    No valid model files found on network volume!")
    _print_expected_structure()
    print("=" * 70)


def _print_expected_structure():
    """Print the expected directory structure for the network volume."""
    print("\n    Expected directory structure:")
    print("    /runpod-volume/")
    print("    └── models/")
    print("        ├── checkpoints/")
    print("        ├── loras/")
    print("        ├── vae/")
    print("        ├── clip/")
    print("        ├── controlnet/")
    print("        ├── embeddings/")
    print("        └── upscale_models/")


def _format_size(size_bytes):
    """Format bytes into human-readable size."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"
