#!/usr/bin/env python3
"""FLUX/ComfyUI API client. Python 3 standard library; no pip packages required."""
import argparse
import copy
import ipaddress
import json
import math
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a proxy API key to a different address.
        return None


class ComfyAPIError(RuntimeError):
    """An explicit HTTP rejection; a transport timeout does not use this class."""

    def __init__(self, http_status, path, detail, error_type=""):
        self.http_status = http_status
        self.error_type = error_type
        self.detail = detail
        super().__init__(f"HTTP {http_status} for {path}: {detail}")


class GenerationFailed(RuntimeError):
    """The submitted job ended unsuccessfully; waiting again cannot resume it."""

    def __init__(self, prompt_id, detail):
        self.prompt_id = prompt_id
        self.detail = detail
        super().__init__(f"Job {prompt_id} failed: {detail}")


class Client:
    def __init__(self, base_url):
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise ValueError("--url must be an http(s) base URL without credentials, query or fragment")
        _ = parsed.port  # Reject malformed ports.
        self.base = base_url.rstrip("/")
        self.headers = {"Accept": "application/json"}
        key = os.environ.get("COMFY_API_KEY", "").strip()
        if "\r" in key or "\n" in key:
            raise ValueError("COMFY_API_KEY contains a newline")
        if key:
            self.headers["Authorization"] = "Bearer " + key
        host = parsed.hostname.lower().rstrip(".")
        bypass_proxy = host == "localhost" or host.endswith(".ts.net")
        try:
            bypass_proxy = bypass_proxy or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        handlers = [NoRedirect()]
        if bypass_proxy:
            # Local and Tailscale traffic must not pass through a system VPN proxy.
            handlers.insert(0, urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)

    def request(self, path, body=None, *, image=False, timeout=30):
        headers = dict(self.headers)
        if image:
            headers["Accept"] = "image/png"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        limit = 64 * 1024 * 1024 if image else 16 * 1024 * 1024
        try:
            with self.opener.open(request, timeout=max(0.01, timeout)) as response:
                content_type = response.headers.get_content_type()
                raw = response.read(limit + 1)
        except urllib.error.HTTPError as exc:
            detail = exc.read(8192).decode("utf-8", errors="replace")
            error_type = ""
            try:
                error_body = json.loads(detail)
            except ValueError:
                error_body = None
            if isinstance(error_body, dict):
                error = error_body.get("error")
                if isinstance(error, dict):
                    error_type = error.get("type", "")
                    detail = error.get("message") or detail
                elif isinstance(error, str):
                    error_type = error
                    detail = error_body.get("message") or detail
            raise ComfyAPIError(exc.code, path, detail, error_type) from exc
        except (urllib.error.URLError, OSError) as exc:
            detail = getattr(exc, "reason", str(exc))
            if body is not None:
                raise RuntimeError("Submission outcome unknown; check ComfyUI queue before "
                                   f"resubmitting. Transport error: {detail}") from exc
            raise RuntimeError(f"Cannot reach {self.base}: {detail}") from exc
        if len(raw) > limit:
            raise RuntimeError("Response exceeded the client's size limit")
        if image:
            if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError(f"Expected PNG, received {content_type}")
            return raw
        if content_type != "application/json" and not content_type.endswith("+json"):
            raise RuntimeError(f"Expected API JSON, received {content_type}; check URL/authentication")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise RuntimeError("Expected a JSON object from ComfyUI")
        return result


def check(client, workflow):
    stats = client.request("/system_stats")
    for device in stats.get("devices", []):
        print("Device:", device.get("name", "unknown"))
    info = client.request("/object_info")
    missing = []
    for node_id, node in workflow.items():
        kind = node["class_type"]
        if kind not in info:
            missing.append(f"Node {kind} (workflow ID {node_id})")
            continue
        schema = info[kind].get("input", {})
        fields = {**schema.get("required", {}), **schema.get("optional", {})}
        for name in ("unet_name", "clip_name1", "clip_name2", "vae_name"):
            if name not in node["inputs"]:
                continue
            spec = fields.get(name)
            choices = spec[0] if isinstance(spec, list) and spec else None
            value = node["inputs"][name]
            if not isinstance(choices, list):
                missing.append(f"Cannot verify {kind}.{name}: unexpected server schema")
            elif value not in choices:
                missing.append(f"File {value} ({kind}.{name})")
    if missing:
        raise RuntimeError("Missing or unverified requirements:\n  " + "\n  ".join(missing))
    safety = client.request("/flux-safety/health")
    if safety.get("active") is not True or safety.get("ready") is not True:
        raise RuntimeError("FLUX safety checks are not ready. Start FLUX on the main PC.")
    print("OK: API, required nodes and model filenames are available.")
    print("OK: server-side prompt and image safety checks are active and ready.")
    print("This checks configuration only; no image was generated.")


def wait_for_images(client, prompt_id, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        history = client.request("/history/" + urllib.parse.quote(prompt_id, safe=""),
                                 timeout=min(30, remaining))
        entry = history.get(prompt_id)
        if entry:
            status = entry.get("status", {})
            errors = [data for event, data in status.get("messages", [])
                      if event in ("execution_error", "execution_interrupted")]
            if status.get("status_str") == "error" or errors:
                detail = "\n".join(str(error.get("exception_message") or error.get("exception_type") or "")
                                   for error in errors if isinstance(error, dict)).strip()
                raise GenerationFailed(prompt_id, detail or json.dumps(errors or status, ensure_ascii=False))
            images = entry.get("outputs", {}).get("10", {}).get("images", [])
            images = [item for item in images if item.get("type") == "output"]
            if status.get("completed") is True or ("status" not in entry and images):
                if not images:
                    raise RuntimeError("Job ended without an output image in SaveImage node 10")
                return images
        time.sleep(max(0, min(1, deadline - time.monotonic())))
    raise TimeoutError(f"Wait timed out. Job may still be running. Resume with --prompt-id {prompt_id}")


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8188")
    parser.add_argument("--workflow", type=Path,
                        default=Path(__file__).with_name("flux_4070ti_api.json"))
    prompts = parser.add_mutually_exclusive_group()
    prompts.add_argument("--prompt")
    prompts.add_argument("--prompt-file", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--prompt-id", help="Resume an existing job without submitting another")
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--guidance", type=float, default=3.5)
    parser.add_argument("--out", type=Path, default=Path("generated"))
    parser.add_argument("--timeout", type=float, default=1800,
                        help="Seconds to wait for a queued job (default: 1800)")
    args = parser.parse_args()
    if not args.check and not args.prompt_id and args.prompt is None and args.prompt_file is None:
        parser.error("provide --prompt or --prompt-file (or use --check / --prompt-id)")
    if args.prompt_id and (args.prompt is not None or args.prompt_file is not None):
        parser.error("--prompt-id resumes an existing job; do not also provide a prompt")
    for name in ("width", "height"):
        value = getattr(args, name)
        if value < 256 or value > 2048 or value % 64:
            parser.error(f"--{name} must be a multiple of 64, from 256 to 2048")
    if not 1 <= args.steps <= 80:
        parser.error("--steps must be 1..80")
    if not math.isfinite(args.guidance) or not 0 <= args.guidance <= 20:
        parser.error("--guidance must be 0..20")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be positive and finite")
    if args.seed is not None and not 0 <= args.seed <= 2**63 - 1:
        parser.error("--seed must be 0..9223372036854775807")
    return args


def main():
    args = arguments()
    client = Client(args.url)
    if args.check or not args.prompt_id:
        with args.workflow.open(encoding="utf-8-sig") as handle:
            workflow = json.load(handle)
        if not isinstance(workflow, dict) or "nodes" in workflow or "prompt" in workflow:
            raise ValueError("Use flux_4070ti_api.json, the API-format workflow")
    if args.check:
        check(client, workflow)
        return
    prompt_id = args.prompt_id
    if not prompt_id:
        text = args.prompt_file.read_text(encoding="utf-8-sig") if args.prompt_file else args.prompt
        if not text or not text.strip():
            raise ValueError("Prompt cannot be empty")
        graph = copy.deepcopy(workflow)
        seed = args.seed if args.seed is not None else secrets.randbits(63)
        graph["4"]["inputs"]["text"] = text
        graph["5"]["inputs"]["guidance"] = args.guidance
        graph["7"]["inputs"].update(width=args.width, height=args.height, batch_size=1)
        graph["8"]["inputs"].update(seed=seed, steps=args.steps)
        response = client.request("/prompt", {"prompt": graph, "client_id": str(uuid.uuid4())}, timeout=120)
        if response.get("error") or response.get("node_errors") or not response.get("prompt_id"):
            raise RuntimeError("Workflow rejected: " + json.dumps(response, ensure_ascii=False))
        prompt_id = str(response["prompt_id"])
        print("Seed:", seed)
    print("Prompt ID:", prompt_id, flush=True)
    images = wait_for_images(client, prompt_id, args.timeout)
    args.out.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(c for c in prompt_id if c.isascii() and (c.isalnum() or c in "-_"))[:80]
    safe_id = safe_id or uuid.uuid4().hex
    for index, image in enumerate(images, 1):
        query = urllib.parse.urlencode({"filename": image["filename"],
                                       "subfolder": image.get("subfolder", ""), "type": "output"})
        data = client.request("/view?" + query, image=True)
        target = args.out / f"{safe_id}_{index}.png"
        target.write_bytes(data)
        print("Saved:", target.resolve())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped waiting. The server job may continue; no interrupt was sent.", file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print("Error:", exc, file=sys.stderr)
        sys.exit(1)
