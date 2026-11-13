"""Local authenticated Images API adapter; no downloads, credentials or GPU setup here."""
import base64
import hmac
import importlib.util
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, UnidentifiedImageError

HERE = Path(__file__).resolve().parent
MODEL = 'flux1-dev'
MAX_BODY = 64 * 1024
MAX_PNG = 5 * 1024 * 1024


def configuration(environ=None):
    """Loopback listener, optional private hostname and explicit local key path."""
    env = os.environ if environ is None else environ
    port = env.get('FLUX_GATEWAY_PORT', '8193')
    if not isinstance(port, str) or not port.isascii() or not port.isdigit() or not 1024 <= int(port) <= 65535:
        raise ValueError('FLUX_GATEWAY_PORT must be an integer from 1024 to 65535.')
    hostname = env.get('FLUX_GATEWAY_HOSTNAME', '').strip().lower().rstrip('.')
    if hostname and hostname != 'localhost' and not re.fullmatch(
            r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+ts\.net', hostname):
        raise ValueError('FLUX_GATEWAY_HOSTNAME must be empty, localhost or a private Tailscale hostname.')
    if len(hostname) > 253:
        raise ValueError('FLUX_GATEWAY_HOSTNAME is too long.')
    prefix = env.get('FLUX_GATEWAY_PREFIX', '/flux-api')
    if prefix and not re.fullmatch(r'/[A-Za-z0-9_-]{1,80}', prefix):
        raise ValueError('FLUX_GATEWAY_PREFIX must be empty or a single path such as /flux-demo-api.')
    key_path = env.get('FLUX_GATEWAY_KEY_FILE', '')
    return {'port': int(port), 'hostname': hostname, 'prefix': prefix,
            'key_file': Path(key_path) if key_path else HERE / 'api_key.txt'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def moderation_request(path, body, content_type):
    request = urllib.request.Request('http://127.0.0.1:8192' + path, data=body,
                                     headers={'Content-Type': content_type}, method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=90) as response:
        if response.status != 200 or response.headers.get_content_type() != 'application/json':
            raise ValueError('Invalid safety response')
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError('Invalid safety response')
    result = json.loads(raw)
    if (not isinstance(result, dict) or type(result.get('allowed')) is not bool
            or not isinstance(result.get('reason'), str) or not isinstance(result.get('category'), str)):
        raise ValueError('Invalid safety decision')
    return {key: result[key] for key in ('allowed', 'reason', 'category')}


def image_for_moderation(content):
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(content), formats=['PNG', 'JPEG']) as source:
            if (source.format not in ('PNG', 'JPEG') or source.width * source.height > 4096 * 4096
                    or getattr(source, 'n_frames', 1) != 1):
                raise ValueError('Unsupported image')
            source.load()
            normalized = source.convert('RGB')
    output = io.BytesIO()
    normalized.save(output, format='PNG')
    if output.tell() > 24 * 1024 * 1024:
        raise ValueError('Image is too large to check')
    return output.getvalue()

def read_key(path):
    try:
        key = path.read_text(encoding='utf-8').strip()
        return key if len(key) >= 32 and key.isascii() and not any(c.isspace() for c in key) else ''
    except (OSError, UnicodeError):
        return ''

def load_backend():
    parent = HERE.parent
    sys.path.insert(0, str(parent))  # flux_api imports its adjacent generate.py.
    spec = importlib.util.spec_from_file_location('flux_gateway_backend', parent / 'flux_api.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def options(payload):
    fields = {'model', 'prompt', 'n', 'output_format', 'response_format', 'size'}
    if not isinstance(payload, dict) or payload.keys() - fields:
        raise ValueError('Supported fields: model, prompt, n, output_format, response_format, size.')
    if payload.get('model', MODEL) != MODEL:
        raise ValueError('The only supported model is flux1-dev.')
    prompt = payload.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000 or '\x00' in prompt:
        raise ValueError('prompt must be a nonempty string of at most 12000 characters.')
    if type(payload.get('n', 1)) is not int or payload.get('n', 1) != 1:
        raise ValueError('Only n=1 is supported.')
    if payload.get('output_format', 'png') != 'png' or payload.get('response_format', 'b64_json') != 'b64_json':
        raise ValueError('Use output_format=png and response_format=b64_json.')
    size = payload.get('size', '1024x1024')
    if not isinstance(size, str) or not re.fullmatch(r'[0-9]{3,4}x[0-9]{3,4}', size):
        raise ValueError('size must be WIDTHxHEIGHT, for example 1024x1024.')
    width, height = map(int, size.split('x'))
    if any(not 256 <= dimension <= 2048 or dimension % 64 for dimension in (width, height)):
        raise ValueError('Each size dimension must be a multiple of 64 from 256 to 2048.')
    return prompt, width, height

class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, backend, key, *, hostname='', route_prefix='/flux-api'):
        super().__init__(address, Handler)
        self.backend, self.key = backend, key
        self.hostname, self.route_prefix = hostname, route_prefix
        self.job_lock = threading.Lock()

    def safety_ready(self):
        try:
            result = self.backend.Client(self.backend.LOCAL_API).request('/flux-safety/health', timeout=5)
            return isinstance(result, dict) and result.get('active') is True and result.get('ready') is True
        except Exception:
            return False

    def handle_error(self, request, client_address):
        pass  # No request, header, prompt or traceback logging.

class Handler(BaseHTTPRequestHandler):
    server_version = 'FluxImages/1'
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *args):
        pass

    def reply(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        if status == 401:
            self.send_header('WWW-Authenticate', 'Bearer')
        if status == 429:
            self.send_header('Retry-After', '10')
        self.end_headers()
        self.close_connection = True
        self.wfile.write(data)

    def error(self, status, code, message):
        kind = 'invalid_request_error' if status in (400, 401, 403, 404, 413, 422) else 'server_error'
        self.reply(status, {'error': {'message': message, 'type': kind, 'code': code}})

    def route(self):
        path = self.path
        prefix = self.server.route_prefix
        return path[len(prefix):] if prefix and path.startswith(prefix + '/') else path

    def boundary(self):
        hosts = self.headers.get_all('Host', [])
        allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        if self.server.hostname:
            allowed.update({self.server.hostname, self.server.hostname + ':443'})
        if len(hosts) != 1 or hosts[0].lower() not in allowed or self.headers.get('Origin') is not None:
            self.error(403, 'request_not_allowed', 'This endpoint accepts authenticated server requests only.')
            return False
        return True

    def authenticate(self):
        values = self.headers.get_all('Authorization', [])
        value = values[0] if len(values) == 1 else ''
        candidate = value[7:] if value[:7].lower() == 'bearer ' else ''
        valid = hmac.compare_digest(candidate.encode('utf-8'), self.server.key.encode('ascii'))
        if not self.server.key or not valid:
            self.error(401, 'invalid_api_key', 'A valid Bearer API key is required.')
            return False
        return True

    def do_GET(self):
        if not self.boundary():
            return
        route = self.route()
        if route == '/health':
            ready = bool(self.server.key) and self.server.safety_ready()
            self.reply(200 if ready else 503, {'service': 'flux-image-api', 'ready': ready, 'configured': bool(self.server.key)})
        elif self.authenticate():
            if route == '/v1/models':
                self.reply(200, {'object': 'list', 'data': [{'id': MODEL, 'object': 'model', 'created': 0, 'owned_by': 'local'}]})
            else:
                self.error(404, 'not_found', 'Supported routes: /health, /v1/models, /v1/images/generations.')

    def do_POST(self):
        if not self.boundary() or not self.authenticate():
            return
        if self.route() in ('/v1/safety/check-text', '/v1/safety/check-image'):
            self.check_content()
            return
        if self.route() != '/v1/images/generations':
            self.error(404, 'not_found', 'Use POST /v1/images/generations.')
            return
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') is not None or len(lengths) != 1 or not lengths[0].isdigit():
            self.error(400, 'invalid_request', 'One Content-Length header is required; transfer encoding is unsupported.')
            return
        length = int(lengths[0])
        if not 0 < length <= MAX_BODY:
            self.error(413, 'request_too_large', 'JSON body must be between 1 byte and 64 KiB.')
            return
        if self.headers.get_content_type() != 'application/json':
            self.error(400, 'invalid_request', 'Content-Type must be application/json.')
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('Incomplete JSON request body.')
            try:
                payload = json.loads(raw.decode('utf-8'))
            except (ValueError, UnicodeError, RecursionError):
                raise ValueError('Body must be valid UTF-8 JSON.') from None
            prompt, width, height = options(payload)
        except (ValueError, OSError) as exc:
            self.error(400, 'invalid_request', str(exc) if isinstance(exc, ValueError) else 'Could not read request body.')
            return
        if not self.server.job_lock.acquire(blocking=False):
            self.error(429, 'busy', 'An image is already being generated. Wait before starting another request.')
            return
        api = self.server.backend
        try:
            if not self.server.safety_ready():
                self.error(503, 'safety_unavailable', 'Safety checks are unavailable. No image was submitted.')
                return
            # Covers an earlier accepted job that outlived a caller timeout, and other ComfyUI clients.
            queue = api.Client(api.LOCAL_API).request('/queue', timeout=5)
            if not isinstance(queue, dict) or any(not isinstance(queue.get(k), list) for k in ('queue_running', 'queue_pending')):
                raise RuntimeError('Invalid queue response')
            if queue['queue_running'] or queue['queue_pending']:
                self.error(429, 'busy', 'ComfyUI already has a running or queued image. Wait before starting another request.')
                return
            png = api.generate_image(prompt, url=api.LOCAL_API, translate=True,
                                     width=width, height=height, steps=20, timeout=900)
            if not isinstance(png, bytes) or not png.startswith(b'\x89PNG\r\n\x1a\n'):
                self.error(502, 'invalid_image', 'The image backend did not return a PNG.')
            elif len(png) > MAX_PNG:
                self.error(502, 'image_too_large', 'Generated PNG exceeds 5 MiB. Use a smaller size for a new request.')
            else:
                self.reply(200, {'created': int(time.time()), 'data': [{'b64_json': base64.b64encode(png).decode('ascii')}]})
        except api.ComfyAPIError as exc:
            if exc.error_type == 'content_blocked':
                self.error(422, 'content_policy_violation', 'The prompt or image was blocked by safety checks. Change the prompt.')
            else:
                self.error(503, 'backend_unavailable', 'The image backend is unavailable. No automatic retry was made.')
        except api.GenerationFailed as exc:
            if 'Материал заблокирован проверкой безопасности.' in exc.detail:
                self.error(422, 'content_policy_violation', 'The generated image was blocked by safety checks.')
            elif 'Проверка безопасности недоступна.' in exc.detail:
                self.error(503, 'safety_unavailable', 'Safety checks became unavailable. No image will be returned.')
            else:
                self.error(502, 'generation_failed', 'Image generation failed. No automatic retry was made.')
        except Exception:
            self.error(503, 'generation_unavailable', 'Generation could not complete. A job may already exist; check the queue before retrying.')
        finally:
            self.server.job_lock.release()

    def check_content(self):
        is_image = self.route().endswith('/check-image')
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') is not None or len(lengths) != 1 or not lengths[0].isdigit():
            self.error(400, 'invalid_request', 'One Content-Length header is required.')
            return
        length = int(lengths[0])
        if not 0 < length <= (MAX_PNG if is_image else MAX_BODY):
            self.error(413, 'request_too_large', 'The safety request exceeds its size limit.')
            return
        allowed_types = ('image/png', 'image/jpeg') if is_image else ('application/json',)
        if self.headers.get_content_type() not in allowed_types:
            self.error(400, 'invalid_request', 'Use raw PNG/JPEG bytes or a JSON texts array.')
            return
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError('Incomplete request')
            if is_image:
                body = image_for_moderation(body)
                path, content_type = '/check-image', 'image/png'
            else:
                payload = json.loads(body.decode('utf-8'))
                texts = payload.get('texts') if isinstance(payload, dict) else None
                if (not isinstance(payload, dict) or set(payload) != {'texts'}
                        or not isinstance(texts, list) or not 1 <= len(texts) <= 16
                        or any(not isinstance(text, str) or '\x00' in text for text in texts)
                        or sum(map(len, texts)) > 12000):
                    raise ValueError('Invalid texts')
                body = json.dumps({'texts': texts}, ensure_ascii=False).encode('utf-8')
                path, content_type = '/check-text', 'application/json'
        except (ValueError, UnicodeError, RecursionError, OSError, Image.DecompressionBombWarning,
                Image.DecompressionBombError, UnidentifiedImageError):
            self.error(400, 'invalid_request', 'Invalid image or texts array for safety checking.')
            return
        try:
            decision = moderation_request(path, body, content_type)
        except Exception:
            self.error(503, 'safety_unavailable', 'Safety checks are unavailable. Do not send this material.')
            return
        self.reply(200, decision)

    def send_error(self, code, message=None, explain=None):
        self.error(code, 'invalid_request', 'Invalid or unsupported HTTP request.')

def build_server_from_environment(environ=None, *, backend=None):
    config = configuration(environ)
    key = read_key(config['key_file'])
    if not key:
        raise ValueError('The gateway key file is missing or invalid. No listener was started.')
    return GatewayServer(('127.0.0.1', config['port']), backend if backend is not None else load_backend(), key,
                         hostname=config['hostname'], route_prefix=config['prefix'])


if __name__ == '__main__':
    try:
        server = build_server_from_environment()
    except ValueError as error:
        raise SystemExit(str(error)) from None
    print(f'flux-image-api listening on 127.0.0.1:{server.server_port}', flush=True)
    server.serve_forever()
