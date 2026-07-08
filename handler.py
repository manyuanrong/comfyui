"""
Kera2 - ComfyUI Serverless Handler for RunPod.

Handles incoming workflow jobs by communicating with a local ComfyUI instance
via HTTP and WebSocket. Supports:
- Workflow JSON (ComfyUI API format)
- Optional input images (base64)
- Output images as base64 or S3 URLs
- HuggingFace model caching
"""

import runpod
from runpod.serverless.utils import rp_upload
import json
import urllib.request
import urllib.parse
import time
import os
import requests
import base64
from io import BytesIO
import websocket
import uuid
import tempfile
import socket
import traceback
import logging

from network_volume import (
    is_network_volume_debug_enabled,
    run_network_volume_diagnostics,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (environment-variable driven)
# ---------------------------------------------------------------------------
COMFY_API_AVAILABLE_INTERVAL_MS = int(
    os.environ.get("COMFY_API_AVAILABLE_INTERVAL_MS", 50)
)
COMFY_API_AVAILABLE_MAX_RETRIES = int(
    os.environ.get("COMFY_API_AVAILABLE_MAX_RETRIES", 0)
)
COMFY_API_FALLBACK_MAX_RETRIES = 500
COMFY_PID_FILE = "/tmp/comfyui.pid"

WEBSOCKET_RECONNECT_ATTEMPTS = int(os.environ.get("WEBSOCKET_RECONNECT_ATTEMPTS", 5))
WEBSOCKET_RECONNECT_DELAY_S = int(os.environ.get("WEBSOCKET_RECONNECT_DELAY_S", 3))

if os.environ.get("WEBSOCKET_TRACE", "false").lower() == "true":
    websocket.enableTrace(True)

COMFY_HOST = "127.0.0.1:8188"
REFRESH_WORKER = os.environ.get("REFRESH_WORKER", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _comfy_server_status():
    """Return basic reachability info for the ComfyUI HTTP server."""
    try:
        resp = requests.get(f"http://{COMFY_HOST}/", timeout=5)
        return {"reachable": resp.status_code == 200, "status_code": resp.status_code}
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


def _get_comfyui_pid():
    """Read the ComfyUI process PID from the PID file."""
    try:
        with open(COMFY_PID_FILE, "r") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _is_comfyui_process_alive():
    """Check whether the ComfyUI process is still running."""
    pid = _get_comfyui_pid()
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def check_server(url, retries=0, delay=50):
    """
    Poll ComfyUI until it responds, or the process dies.

    When a PID file is available, polls indefinitely while ComfyUI is alive.
    Falls back to a retry limit when no PID file is found.
    """
    print(f"worker-comfyui - Checking API server at {url}...")
    delay = max(1, delay)
    log_every = max(1, int(10_000 / delay))
    attempt = 0

    while True:
        process_status = _is_comfyui_process_alive()
        if process_status is False:
            print("worker-comfyui - ComfyUI process has exited. Server will not become reachable.")
            return False

        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                print("worker-comfyui - API is reachable")
                return True
        except (requests.Timeout, requests.RequestException):
            pass

        attempt += 1
        fallback = retries if retries > 0 else COMFY_API_FALLBACK_MAX_RETRIES
        if process_status is None and attempt >= fallback:
            print(f"worker-comfyui - Failed to connect after {fallback} attempts (no PID file).")
            return False

        if attempt % log_every == 0:
            elapsed_s = (attempt * delay) / 1000
            print(f"worker-comfyui - Still waiting for API server... ({elapsed_s:.0f}s, attempt {attempt})")

        time.sleep(delay / 1000)


def queue_workflow(workflow, client_id, comfy_org_api_key=None):
    """Submit a workflow to ComfyUI's /prompt endpoint."""
    payload = {"prompt": workflow, "client_id": client_id}

    key_from_env = os.environ.get("COMFY_ORG_API_KEY")
    effective_key = comfy_org_api_key if comfy_org_api_key else key_from_env
    if effective_key:
        payload["extra_data"] = {"api_key_comfy_org": effective_key}

    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}

    response = requests.post(
        f"http://{COMFY_HOST}/prompt", data=data, headers=headers, timeout=30
    )
    response.raise_for_status()
    return response.json()


def get_history(prompt_id):
    """Fetch execution history for a given prompt_id from ComfyUI."""
    response = requests.get(f"http://{COMFY_HOST}/history/{prompt_id}", timeout=30)
    response.raise_for_status()
    return response.json()


def get_image_data(filename, subfolder, image_type):
    """Fetch raw image bytes from ComfyUI's /view endpoint."""
    print(f"worker-comfyui - Fetching image: type={image_type}, subfolder={subfolder}, filename={filename}")
    data = {"filename": filename, "subfolder": subfolder, "type": image_type}
    url_values = urllib.parse.urlencode(data)
    try:
        response = requests.get(f"http://{COMFY_HOST}/view?{url_values}", timeout=60)
        response.raise_for_status()
        print(f"worker-comfyui - Successfully fetched image data for {filename}")
        return response.content
    except requests.Timeout:
        print(f"worker-comfyui - Timeout fetching image data for {filename}")
    except requests.RequestException as e:
        print(f"worker-comfyui - Error fetching image data for {filename}: {e}")
    except Exception as e:
        print(f"worker-comfyui - Unexpected error fetching image data for {filename}: {e}")
    return None


def upload_images(images):
    """Upload base64-encoded images to ComfyUI's /upload/image endpoint."""
    if not images:
        return {"status": "success", "message": "No images to upload", "details": []}

    responses = []
    upload_errors = []
    print(f"worker-comfyui - Uploading {len(images)} image(s)...")

    for image in images:
        try:
            name = image["name"]
            image_data_uri = image["image"]
            if "," in image_data_uri:
                base64_data = image_data_uri.split(",", 1)[1]
            else:
                base64_data = image_data_uri

            blob = base64.b64decode(base64_data)
            files = {
                "image": (name, BytesIO(blob), "image/png"),
                "overwrite": (None, "true"),
            }

            response = requests.post(
                f"http://{COMFY_HOST}/upload/image", files=files, timeout=30
            )
            response.raise_for_status()
            responses.append(f"Successfully uploaded {name}")
            print(f"worker-comfyui - Successfully uploaded {name}")

        except base64.binascii.Error as e:
            error_msg = f"Error decoding base64 for {image.get('name', 'unknown')}: {e}"
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)
        except requests.Timeout:
            error_msg = f"Timeout uploading {image.get('name', 'unknown')}"
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)
        except requests.RequestException as e:
            error_msg = f"Error uploading {image.get('name', 'unknown')}: {e}"
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)
        except Exception as e:
            error_msg = f"Unexpected error uploading {image.get('name', 'unknown')}: {e}"
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)

    if upload_errors:
        print("worker-comfyui - Image upload finished with errors")
        return {"status": "error", "message": "Some images failed to upload", "details": upload_errors}

    print("worker-comfyui - Image upload complete")
    return {"status": "success", "message": "All images uploaded successfully", "details": responses}


def _attempt_websocket_reconnect(ws_url, max_attempts, delay_s, initial_error):
    """Attempt to reconnect to the ComfyUI WebSocket after a disconnect."""
    print(f"worker-comfyui - WebSocket connection closed: {initial_error}. Attempting reconnect...")
    last_error = initial_error

    for attempt in range(max_attempts):
        srv_status = _comfy_server_status()
        if not srv_status["reachable"]:
            print(f"worker-comfyui - ComfyUI HTTP unreachable — aborting reconnect: {srv_status.get('error', 'status ' + str(srv_status.get('status_code')))}")
            raise websocket.WebSocketConnectionClosedException(
                "ComfyUI HTTP unreachable during websocket reconnect"
            )

        print(f"worker-comfyui - Reconnect attempt {attempt + 1}/{max_attempts}... (HTTP status {srv_status.get('status_code')})")
        try:
            new_ws = websocket.WebSocket()
            new_ws.connect(ws_url, timeout=10)
            print("worker-comfyui - WebSocket reconnected successfully.")
            return new_ws
        except (
            websocket.WebSocketException,
            ConnectionRefusedError,
            socket.timeout,
            OSError,
        ) as reconn_err:
            last_error = reconn_err
            print(f"worker-comfyui - Reconnect attempt {attempt + 1} failed: {reconn_err}")
            if attempt < max_attempts - 1:
                print(f"worker-comfyui - Waiting {delay_s}s before next attempt...")
                time.sleep(delay_s)
            else:
                print("worker-comfyui - Max reconnection attempts reached.")

    raise websocket.WebSocketConnectionClosedException(
        f"Connection closed and failed to reconnect. Last error: {last_error}"
    )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def validate_input(job_input):
    """Validate the incoming job input."""
    if job_input is None:
        return None, "Please provide input"

    if isinstance(job_input, str):
        try:
            job_input = json.loads(job_input)
        except json.JSONDecodeError:
            return None, "Invalid JSON format in input"

    workflow = job_input.get("workflow")
    if workflow is None:
        return None, "Missing 'workflow' parameter"

    images = job_input.get("images")
    if images is not None:
        if not isinstance(images, list) or not all(
            "name" in img and "image" in img for img in images
        ):
            return None, "'images' must be a list of objects with 'name' and 'image' keys"

    comfy_org_api_key = job_input.get("comfy_org_api_key")

    return {
        "workflow": workflow,
        "images": images,
        "comfy_org_api_key": comfy_org_api_key,
    }, None


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

def handler(job):
    """
    RunPod serverless handler — process a ComfyUI workflow.

    Input:  {"input": {"workflow": {...}, "images": [...]}}
    Output: {"images": [{"filename": "...", "type": "base64|s3_url", "data": "..."}]}
    """
    if is_network_volume_debug_enabled():
        run_network_volume_diagnostics()

    job_input = job["input"]
    job_id = job["id"]

    validated_data, error_message = validate_input(job_input)
    if error_message:
        return {"error": error_message}

    workflow = validated_data["workflow"]
    input_images = validated_data.get("images")

    # Wait for ComfyUI HTTP API
    if not check_server(
        f"http://{COMFY_HOST}/",
        COMFY_API_AVAILABLE_MAX_RETRIES,
        COMFY_API_AVAILABLE_INTERVAL_MS,
    ):
        return {"error": f"ComfyUI server ({COMFY_HOST}) not reachable after retries."}

    # Upload input images
    if input_images:
        upload_result = upload_images(input_images)
        if upload_result["status"] == "error":
            return {"error": "Failed to upload input images", "details": upload_result["details"]}

    ws = None
    client_id = str(uuid.uuid4())
    prompt_id = None
    output_data = []
    errors = []

    try:
        # Connect WebSocket
        ws_url = f"ws://{COMFY_HOST}/ws?clientId={client_id}"
        print(f"worker-comfyui - Connecting to websocket: {ws_url}")
        ws = websocket.WebSocket()
        ws.connect(ws_url, timeout=10)
        print("worker-comfyui - Websocket connected")

        # Queue workflow
        try:
            queued = queue_workflow(
                workflow,
                client_id,
                comfy_org_api_key=validated_data.get("comfy_org_api_key"),
            )
            prompt_id = queued.get("prompt_id")
            if not prompt_id:
                raise ValueError(f"Missing 'prompt_id' in queue response: {queued}")
            print(f"worker-comfyui - Queued workflow with ID: {prompt_id}")
        except requests.RequestException as e:
            raise ValueError(f"Error queuing workflow: {e}")

        # Wait for execution via WebSocket
        print(f"worker-comfyui - Waiting for workflow execution ({prompt_id})...")
        execution_done = False
        while True:
            try:
                out = ws.recv()
                if isinstance(out, str):
                    message = json.loads(out)
                    msg_type = message.get("type")

                    if msg_type == "status":
                        status_data = message.get("data", {}).get("status", {})
                        queue_remaining = status_data.get("exec_info", {}).get("queue_remaining", "N/A")
                        print(f"worker-comfyui - Status: {queue_remaining} items remaining in queue")

                    elif msg_type == "executing":
                        data = message.get("data", {})
                        if data.get("node") is None and data.get("prompt_id") == prompt_id:
                            print(f"worker-comfyui - Execution finished for prompt {prompt_id}")
                            execution_done = True
                            break

                    elif msg_type == "execution_error":
                        data = message.get("data", {})
                        if data.get("prompt_id") == prompt_id:
                            error_details = (
                                f"Node Type: {data.get('node_type')}, "
                                f"Node ID: {data.get('node_id')}, "
                                f"Message: {data.get('exception_message')}"
                            )
                            print(f"worker-comfyui - Execution error: {error_details}")
                            errors.append(f"Workflow execution error: {error_details}")
                            break

            except websocket.WebSocketTimeoutException:
                print("worker-comfyui - WebSocket receive timed out. Still waiting...")
                continue

            except websocket.WebSocketConnectionClosedException as closed_err:
                ws = _attempt_websocket_reconnect(
                    ws_url, WEBSOCKET_RECONNECT_ATTEMPTS, WEBSOCKET_RECONNECT_DELAY_S, closed_err
                )
                print("worker-comfyui - Resuming message listening after reconnect.")
                continue

            except json.JSONDecodeError:
                print("worker-comfyui - Received invalid JSON via websocket.")

        if not execution_done and not errors:
            raise ValueError("Workflow monitoring exited without confirmation of completion or error.")

        # Fetch history
        print(f"worker-comfyui - Fetching history for prompt {prompt_id}...")
        history = get_history(prompt_id)

        if prompt_id not in history:
            error_msg = f"Prompt ID {prompt_id} not found in history after execution."
            print(f"worker-comfyui - {error_msg}")
            if not errors:
                return {"error": error_msg}
            else:
                errors.append(error_msg)
                return {"error": "Job processing failed, prompt ID not found in history.", "details": errors}

        prompt_history = history.get(prompt_id, {})
        outputs = prompt_history.get("outputs", {})

        if not outputs:
            warning_msg = f"No outputs found in history for prompt {prompt_id}."
            print(f"worker-comfyui - {warning_msg}")
            if not errors:
                errors.append(warning_msg)

        # Process outputs
        print(f"worker-comfyui - Processing {len(outputs)} output nodes...")
        for node_id, node_output in outputs.items():
            if "images" in node_output:
                print(f"worker-comfyui - Node {node_id} contains {len(node_output['images'])} image(s)")
                for image_info in node_output["images"]:
                    filename = image_info.get("filename")
                    subfolder = image_info.get("subfolder", "")
                    img_type = image_info.get("type")

                    if img_type == "temp":
                        print(f"worker-comfyui - Skipping temp image: {filename}")
                        continue

                    if not filename:
                        warn_msg = f"Skipping image in node {node_id} due to missing filename"
                        print(f"worker-comfyui - {warn_msg}")
                        errors.append(warn_msg)
                        continue

                    image_bytes = get_image_data(filename, subfolder, img_type)

                    if image_bytes:
                        file_extension = os.path.splitext(filename)[1] or ".png"

                        if os.environ.get("BUCKET_ENDPOINT_URL"):
                            # S3 upload path
                            try:
                                with tempfile.NamedTemporaryFile(suffix=file_extension, delete=False) as temp_file:
                                    temp_file.write(image_bytes)
                                    temp_file_path = temp_file.name

                                print(f"worker-comfyui - Uploading {filename} to S3...")
                                s3_url = rp_upload.upload_image(job_id, temp_file_path)
                                os.remove(temp_file_path)
                                print(f"worker-comfyui - Uploaded {filename} to S3: {s3_url}")
                                output_data.append({
                                    "filename": filename,
                                    "type": "s3_url",
                                    "data": s3_url,
                                })
                            except Exception as e:
                                error_msg = f"Error uploading {filename} to S3: {e}"
                                print(f"worker-comfyui - {error_msg}")
                                errors.append(error_msg)
                                if "temp_file_path" in locals() and os.path.exists(temp_file_path):
                                    try:
                                        os.remove(temp_file_path)
                                    except OSError as rm_err:
                                        print(f"worker-comfyui - Error removing temp file: {rm_err}")
                        else:
                            # Base64 return path
                            try:
                                base64_image = base64.b64encode(image_bytes).decode("utf-8")
                                output_data.append({
                                    "filename": filename,
                                    "type": "base64",
                                    "data": base64_image,
                                })
                                print(f"worker-comfyui - Encoded {filename} as base64")
                            except Exception as e:
                                error_msg = f"Error encoding {filename} to base64: {e}"
                                print(f"worker-comfyui - {error_msg}")
                                errors.append(error_msg)
                    else:
                        errors.append(f"Failed to fetch image data for {filename}")

            other_keys = [k for k in node_output.keys() if k != "images"]
            if other_keys:
                print(f"worker-comfyui - WARNING: Node {node_id} produced unhandled output keys: {other_keys}")

    except websocket.WebSocketException as e:
        print(f"worker-comfyui - WebSocket Error: {e}")
        print(traceback.format_exc())
        return {"error": f"WebSocket communication error: {e}"}
    except requests.RequestException as e:
        print(f"worker-comfyui - HTTP Request Error: {e}")
        print(traceback.format_exc())
        return {"error": f"HTTP communication error with ComfyUI: {e}"}
    except ValueError as e:
        print(f"worker-comfyui - Value Error: {e}")
        print(traceback.format_exc())
        return {"error": str(e)}
    except Exception as e:
        print(f"worker-comfyui - Unexpected Handler Error: {e}")
        print(traceback.format_exc())
        return {"error": f"An unexpected error occurred: {e}"}
    finally:
        if ws and ws.connected:
            print("worker-comfyui - Closing websocket connection.")
            ws.close()

    # Build final result
    final_result = {}

    if output_data:
        final_result["images"] = output_data

    if errors:
        final_result["errors"] = errors
        print(f"worker-comfyui - Job completed with errors/warnings: {errors}")

    if not output_data and errors:
        return {"error": "Job processing failed", "details": errors}
    elif not output_data and not errors:
        print("worker-comfyui - Job completed but workflow produced no images.")
        final_result["status"] = "success_no_images"
        final_result["images"] = []

    print(f"worker-comfyui - Job completed. Returning {len(output_data)} image(s).")

    if REFRESH_WORKER:
        final_result["refresh_worker"] = True

    return final_result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Kera2 - Starting ComfyUI serverless handler...")
    runpod.serverless.start({"handler": handler})
