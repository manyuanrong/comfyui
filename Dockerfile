FROM runpod/worker-comfyui:5.7.1-base

RUN comfy-node-install comfyui-kjnodes rgthree-comfy

RUN comfy model download \
    --url https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/main/split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors \ 
    --relative-path models/text_encoders \ 
    --filename qwen_2.5_vl_7b_fp8_scaled.safetensors

RUN comfy model download \
    --url https://huggingface.co/lightx2v/Qwen-Image-Edit-2511-Lightning/resolve/main/Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors \
    --relative-path models/loras \
    --filename Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors

RUN comfy model download \
    --url https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/main/split_files/vae/qwen_image_vae.safetensors \
    --relative-path models/vae \
    --filename qwen_image_vae.safetensors

RUN comfy model download \
    --url https://huggingface.co/Comfy-Org/Qwen-Image-Edit_ComfyUI/resolve/main/split_files/diffusion_models/qwen_image_edit_2511_bf16.safetensors \
    --relative-path models/diffusion_models \
    --filename qwen_image_edit_2511_bf16.safetensors