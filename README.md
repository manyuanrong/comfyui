# Kera2 — Krea 2 Turbo Serverless Worker for RunPod

> 在 RunPod Serverless 上运行 **Krea 2 Turbo (moody-krea-mix)** 的 ComfyUI 工作流。
> 按 GPU 使用时间计费，空闲时 scale-to-zero，大幅降低成本。

基于 [runpod-workers/worker-comfyui](https://github.com/runpod-workers/worker-comfyui) 定制。

## 技术栈

| 组件 | 详情 |
|------|------|
| **模型** | [catlover1937/moody-krea-mix](https://huggingface.co/catlover1937/moody-krea-mix) v3G — Krea 2 Turbo 微调 |
| **量化** | INT8 tensorwise (13.5GB)，CUDA 13.0 原生高效推理 |
| **Text Encoder** | Qwen3-VL 4B fp8 (`qwen3vl_4b_fp8_scaled.safetensors`) |
| **VAE** | Qwen Image VAE (`qwen_image_vae.safetensors`) |
| **CUDA** | 13.0.3 + PyTorch nightly |
| **ComfyUI** | ≥ v0.27.0（原生 INT8 + Krea 2 支持） |
| **推理设置** | 8 steps, CFG 0.0, 1024-2048px |

## 架构

```
Docker Container
├── start.sh          → SSH + GPU预检 → 启动 ComfyUI (后台) → 启动 Handler
├── handler.py        → 接收 API 请求，通过 WebSocket 提交/监控 ComfyUI 工作流
├── src/
│   ├── extra_model_paths.yaml   → 模型路径映射
│   └── network_volume.py        → Network Volume 诊断工具
└── /comfyui/
    └── models/
        ├── text_encoders/    ← Qwen3-VL 4B (baked in, ~4GB)
        ├── vae/              ← Qwen Image VAE (baked in, ~200MB)
        └── diffusion_models/ ← moody-krea-mix (HF Model Cache, ~13.5GB)
```

## 模型策略

- **主模型** (`Moody-Krea-Mix-v3G_00001__int8_tensorwise.safetensors`): RunPod HuggingFace Model Cache
  - 在 RunPod Console 的 Endpoint 配置中添加 `catlover1937/moody-krea-mix`
  - 模型自动下载到 `/runpod-volume/huggingface-cache/hub/`
  - 不计费下载时间
- **Text Encoder + VAE**: 直接烘焙在 Docker 镜像中（相对稳定，不会频繁变更）

## 快速开始

### 1. 推送代码到 GitHub

```bash
git init && git add -A && git commit -m "Kera2: Krea 2 Turbo serverless worker"
git remote add origin git@github.com:<你的用户名>/kera2.git
git push -u origin main
```

### 2. 在 RunPod Console 部署（GitHub 集成，零本地构建）

1. 打开 [RunPod Settings → Connections](https://console.runpod.io/user/settings)，授权 GitHub 访问你的仓库
2. 进入 [Serverless](https://console.runpod.io/serverless) → **New Endpoint**
3. 选择 **Import Git Repository**，搜索并选择 `kera2`
4. 配置：
   - **Branch**: `main`
   - **Dockerfile Path**: `Dockerfile`（根目录，默认）
5. 点击 Next，配置 Endpoint：
   - **GPU**: 推荐 NVIDIA RTX 4090 / L40S / A40（至少 24GB VRAM）
   - **Model Cache**: 添加 `catlover1937/moody-krea-mix`
   - **Environment Variables**（可选）:
     - `COMFY_LOG_LEVEL=INFO`
     - `REFRESH_WORKER=true`
6. 点击 **Deploy Endpoint**，RunPod 自动构建并部署

> ⚠️ **构建限制**：Docker build ≤ 30 分钟，镜像 ≤ 80GB，CPU-only。我们的 Dockerfile 已为此优化（预计 ~15 分钟）

### 3. 更新代码（发布新版本）

修改代码后 push 到 GitHub，然后在 GitHub 仓库页面 **创建 Release**，RunPod 会自动触发重新构建。

### 4. 调用 API

```bash
curl -X POST \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "input": {
      "workflow": {
        "1": {"inputs": {"unet_name": "Moody-Krea-Mix-v3G_00001__int8_tensorwise.safetensors", "weight_dtype": "default"}, "class_type": "UNETLoader"},
        "2": {"inputs": {"clip_name": "qwen3vl_4b_fp8_scaled.safetensors", "type": "krea2"}, "class_type": "CLIPLoader"},
        "3": {"inputs": {"vae_name": "qwen_image_vae.safetensors"}, "class_type": "VAELoader"},
        "4": {"inputs": {"text": "your prompt here", "clip": ["2", 0]}, "class_type": "CLIPTextEncode"},
        "5": {"inputs": {"text": "", "clip": ["2", 0]}, "class_type": "CLIPTextEncode"},
        "6": {"inputs": {"width": 1024, "height": 1024, "batch_size": 1}, "class_type": "EmptySD3LatentImage"},
        "7": {"inputs": {"seed": 42, "steps": 8, "cfg": 0.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0, "model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0], "latent_image": ["6", 0]}, "class_type": "KSampler"},
        "8": {"inputs": {"samples": ["7", 0], "vae": ["3", 0]}, "class_type": "VAEDecode"},
        "9": {"inputs": {"filename_prefix": "Kera2", "images": ["8", 0]}, "class_type": "SaveImage"}
      }
    }
  }' \
  https://api.runpod.ai/v2/<ENDPOINT_ID>/runsync
```

## Krea 2 Turbo 推理参数

| 参数 | 推荐值 | 说明 |
|------|--------|------|
| `steps` | 8 | Turbo 版本只需 8 步 |
| `cfg` | 0.0 | CFG 对 Turbo 关闭 |
| `sampler_name` | `euler` | 推荐采样器 |
| `scheduler` | `simple` | 推荐调度器 |
| `width/height` | 1024-2048 | Krea 2 支持高分辨率 |

## 工作流节点说明

| 节点 | class_type | 说明 |
|------|------------|------|
| Load Diffusion Model | `UNETLoader` | 加载 int8 量化 Krea 2 模型 |
| Load CLIP | `CLIPLoader` | 加载 Qwen3-VL text encoder (type=krea2) |
| Load VAE | `VAELoader` | 加载 Qwen Image VAE |
| Positive Prompt | `CLIPTextEncode` | 正面提示词编码 |
| Negative Prompt | `CLIPTextEncode` | 负面提示词编码（可为空） |
| Latent Image | `EmptySD3LatentImage` | 创建 latent（Krea 2 使用 SD3/Flux 格式） |
| KSampler | `KSampler` | 扩散采样器 |
| VAE Decode | `VAEDecode` | latent → 图片 |
| Save Image | `SaveImage` | 保存输出 |

## 获取 ComfyUI 工作流 JSON

在 ComfyUI 桌面版中：
1. 设计好 Krea 2 工作流
2. 菜单：**Workflow → Export (API)**
3. 下载的 JSON 即为 API 可用的 `workflow` 字段

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `COMFY_LOG_LEVEL` | `DEBUG` | ComfyUI 日志级别 |
| `REFRESH_WORKER` | `false` | 每次任务后清理 worker 状态 |
| `COMFY_API_AVAILABLE_INTERVAL_MS` | `50` | API 可用性检查间隔（毫秒） |
| `COMFY_API_AVAILABLE_MAX_RETRIES` | `0` | 最大重试次数（0=等待进程存活） |
| `WEBSOCKET_RECONNECT_ATTEMPTS` | `5` | WebSocket 断线重连次数 |
| `WEBSOCKET_RECONNECT_DELAY_S` | `3` | 重连间隔（秒） |
| `WEBSOCKET_TRACE` | `false` | 启用 WebSocket 详细日志 |
| `NETWORK_VOLUME_DEBUG` | `false` | 启用网络卷诊断 |
| `SERVE_API_LOCALLY` | `false` | 启动本地 API 服务器（开发用） |
| `PUBLIC_KEY` | — | SSH 公钥（启用 SSH 访问） |
| `BUCKET_ENDPOINT_URL` | — | S3 兼容存储端点（启用 S3 上传） |

## 计费优势

| 状态 | 计费 |
|------|------|
| Initializing | ❌ 不计费 |
| Idle (scale to zero) | ❌ 不计费 |
| Running (处理请求) | ✅ 按秒计费 |
| Unhealthy | ❌ 不计费 |

相比 Pod 模式（24/7 计费），Serverless 可节省 **70-90%** 成本。

## HF Model Cache 路径

当 RunPod 缓存模型后，文件位于：
```
/runpod-volume/huggingface-cache/hub/models--catlover1937--moody-krea-mix/snapshots/<hash>/
```

如果需要手动复制到 ComfyUI 目录（非必须，`extra_model_paths.yaml` 已配置路径映射）：
```bash
cp /runpod-volume/huggingface-cache/hub/models--catlover1937--moody-krea-mix/snapshots/*/Moody-Krea-Mix-v3G_00001__int8_tensorwise.safetensors \
   /comfyui/models/diffusion_models/
```

## License

基于 [worker-comfyui](https://github.com/runpod-workers/worker-comfyui) (AGPL-3.0)
Krea 2 模型使用 [Krea 2 Community License](https://krea.ai/krea-2-licensing)
