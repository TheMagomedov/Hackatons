"""Small synchronous FLUX API helper. Python 3.9+, standard library only.

In an async bot use asyncio.to_thread(generate_image, prompt, url=API_URL).
Keep submit_image's job ID if you need to resume after a disconnect.
"""

import json
import math
import re
import secrets
import urllib.parse
import uuid
from pathlib import Path

from generate import Client, ComfyAPIError, GenerationFailed, wait_for_images

LOCAL_API = "http://127.0.0.1:8188"
# Supply your own private URL explicitly; this package contains no operator hostname.
REMOTE_API = LOCAL_API
RUSSIAN = re.compile(r"[А-Яа-яЁё]")


def check_text(text, *, url=LOCAL_API):
    """Review a proposed bot message/post; caller must check allowed before sending."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Text must be a non-empty string")
    if len(text) > 12000:
        raise ValueError("Text must contain at most 12000 characters")
    result = Client(url).request("/flux-safety/check-text", {"texts": [text]}, timeout=120)
    if type(result.get("allowed")) is not bool:
        raise RuntimeError("Safety service returned no decision; do not send this text")
    return result


def translate_prompt(text, *, url=LOCAL_API, translator_url=None):
    """Translate Russian locally on the FLUX PC; leave English unchanged."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Prompt must be a non-empty string")
    if len(text) > 12000:
        raise ValueError("Prompt must contain at most 12000 characters")
    if not RUSSIAN.search(text):
        return text
    if translator_url is None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname in ("127.0.0.1", "localhost") and parsed.port == 8188:
            translator_url = "http://127.0.0.1:8190"
        else:
            translator_url = url
    try:
        reply = Client(translator_url).request(
            "/flux-translate/translate", {"text": text}, timeout=120
        )
        translated = reply.get("text")
        if not isinstance(translated, str) or not translated.strip():
            raise ValueError("Translator returned no text")
        return translated
    except ComfyAPIError:
        # Preserve the moderation category/status for the bot's error handler.
        raise
    except (RuntimeError, ValueError, OSError) as exc:
        raise RuntimeError(f"Translation failed; no generation was submitted: {exc}") from exc


def submit_image(prompt, *, url=LOCAL_API, width=1024, height=1024,
                 steps=20, seed=None, translate=True, translator_url=None):
    """Return prompt_id once the job is queued. Never retry POST automatically."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Prompt must be a non-empty string")
    if len(prompt) > 12000:
        raise ValueError("Prompt must contain at most 12000 characters")
    for name, value in (("width", width), ("height", height)):
        if type(value) is not int or not 256 <= value <= 2048 or value % 64:
            raise ValueError(f"{name} must be a multiple of 64, from 256 to 2048")
    if type(steps) is not int or not 1 <= steps <= 80:
        raise ValueError("steps must be an integer from 1 to 80")
    if seed is None:
        seed = secrets.randbits(63)
    if type(seed) is not int or not 0 <= seed <= 2**63 - 1:
        raise ValueError("seed must be an integer from 0 to 9223372036854775807")
    client = Client(url)
    workflow = json.loads(Path(__file__).with_name("flux_4070ti_api.json").read_text(encoding="utf-8-sig"))
    text = translate_prompt(prompt, url=url, translator_url=translator_url) if translate else prompt
    workflow["4"]["inputs"]["text"] = text
    workflow["7"]["inputs"].update(width=width, height=height, batch_size=1)
    workflow["8"]["inputs"].update(seed=seed, steps=steps)
    result = client.request("/prompt", {"prompt": workflow, "client_id": str(uuid.uuid4())}, timeout=120)
    if result.get("error") or result.get("node_errors") or not result.get("prompt_id"):
        raise RuntimeError("Generation rejected: " + json.dumps(result, ensure_ascii=False))
    return str(result["prompt_id"])


def wait_image(prompt_id, *, url=LOCAL_API, timeout=1800):
    """Wait for an existing job and return PNG bytes; does not submit a new job."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    if not isinstance(prompt_id, str) or not prompt_id:
        raise ValueError("prompt_id must be a non-empty string")
    client = Client(url)
    images = wait_for_images(client, prompt_id, timeout)
    image = images[0]
    query = urllib.parse.urlencode({"filename": image["filename"],
                                   "subfolder": image.get("subfolder", ""), "type": "output"})
    return client.request("/view?" + query, image=True)


def generate_image(prompt, *, url=LOCAL_API, timeout=1800, **options):
    """Translate, submit, wait and return PNG bytes. For async bots use to_thread."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    prompt_id = submit_image(prompt, url=url, **options)
    try:
        return wait_image(prompt_id, url=url, timeout=timeout)
    except GenerationFailed:
        raise
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(
            f"Job {prompt_id} was already submitted. Resume with wait_image('{prompt_id}', "
            f"url=...). Do not submit it again automatically. Error: {exc}"
        ) from exc
