import base64
import http.client
import io
import json
import sys
import tempfile
import threading
import types
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import image_api_server as gateway

# Fixed public test fixture, not a generated or deployed credential.
TEST_KEY = 'nonsecret-test-fixture-key-1234567890'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jH1sAAAAASUVORK5CYII=')

class ComfyAPIError(RuntimeError):
    def __init__(self, status, kind, detail):
        self.http_status, self.error_type, self.detail = status, kind, detail

class GenerationFailed(RuntimeError):
    def __init__(self, detail):
        self.detail = detail

class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.ready = {'active': True, 'ready': True}
        self.queue = {'queue_running': [], 'queue_pending': []}
        self.client = Mock()
        self.client.request.side_effect = lambda path, **kw: self.ready if path == '/flux-safety/health' else self.queue
        self.api = types.SimpleNamespace(LOCAL_API='http://127.0.0.1:8188', Client=Mock(return_value=self.client),
                                         generate_image=Mock(return_value=PNG), ComfyAPIError=ComfyAPIError,
                                         GenerationFailed=GenerationFailed)
        self.server = gateway.GatewayServer(('127.0.0.1', 0), self.api, TEST_KEY)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path='/v1/images/generations', data=None, method='POST', auth=TEST_KEY, headers=None, raw=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        values = {'Content-Type': 'application/json'}
        if auth is not None:
            values['Authorization'] = 'Bearer ' + auth
        values.update(headers or {})
        body = raw if raw is not None else (json.dumps({'model': 'flux1-dev', 'prompt': 'Стеклянный завод'} if data is None else data).encode('utf-8') if method == 'POST' else None)
        connection.request(method, path, body=body, headers=values)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), json.loads(response.read())
        connection.close()
        return result

    def test_auth_rejected_before_backend_work(self):
        for key in (None, 'wrong-key', ''):
            self.assertEqual(401, self.request(auth=key)[0])
        self.api.generate_image.assert_not_called()
        self.api.Client.assert_not_called()

    def test_exact_image_request_and_response(self):
        status, headers, result = self.request(data={'model': 'flux1-dev', 'prompt': 'Прозрачное стекло', 'n': 1, 'output_format': 'png', 'response_format': 'b64_json'})
        self.assertEqual(status, 200)
        self.assertIsInstance(result['created'], int)
        self.assertEqual(set(result), {'created', 'data'})
        self.assertEqual(PNG, base64.b64decode(result['data'][0]['b64_json']))
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.api.generate_image.assert_called_once_with('Прозрачное стекло', url=self.api.LOCAL_API, translate=True,
                                                        width=1024, height=1024, steps=20, timeout=900)
        self.client.request.assert_any_call('/flux-safety/health', timeout=5)

    def test_prefix_routes_and_size(self):
        self.assertEqual(200, self.request('/flux-api/v1/images/generations', {'prompt': 'Glass', 'size': '768x1024'})[0])
        self.assertEqual(self.api.generate_image.call_args.kwargs['width'], 768)
        result = self.request('/flux-api/v1/models', method='GET')[2]
        self.assertEqual('flux1-dev', result['data'][0]['id'])

    def test_models_requires_key(self):
        self.assertEqual(401, self.request('/v1/models', method='GET', auth=None)[0])
        self.api.generate_image.assert_not_called()

    def test_public_health_and_unconfigured(self):
        status, _, body = self.request('/health', method='GET', auth=None)
        self.assertEqual((status, body), (200, {'service': 'flux-image-api', 'ready': True, 'configured': True}))
        self.server.key = ''
        status, _, body = self.request('/flux-api/health', method='GET', auth=None)
        self.assertEqual((status, body['ready'], body['configured']), (503, False, False))

    def test_guard_unavailable_does_not_queue(self):
        for ready in ({'active': False, 'ready': True}, {'active': True, 'ready': False}, {}, {'active': 1, 'ready': True}):
            self.ready = ready
            status, _, body = self.request()
            self.assertEqual((status, body['error']['code']), (503, 'safety_unavailable'))
        self.api.generate_image.assert_not_called()

    def test_unsupported_inputs_do_not_queue(self):
        for changes in ({'model': 'gpt-image-1'}, {'n': 2}, {'n': True}, {'output_format': 'jpeg'},
                        {'response_format': 'url'}, {'size': '255x1024'}, {'size': '1024'}, {'quality': 'high'},
                        {'prompt': ''}, {'prompt': 'x' * 12001}, {'prompt': None}):
            status, _, _ = self.request(data={'prompt': 'glass', **changes})
            self.assertEqual(400, status, changes.keys())
        self.api.generate_image.assert_not_called()

    def test_content_block_and_internal_details_never_echo(self):
        secret_detail = 'PRIVATE_PROMPT_AND_HEADER_SENTINEL'
        for failure, status, code in (
            (ComfyAPIError(422, 'content_blocked', secret_detail), 422, 'content_policy_violation'),
            (GenerationFailed('Материал заблокирован проверкой безопасности. ' + secret_detail), 422, 'content_policy_violation'),
            (GenerationFailed('Проверка безопасности недоступна. ' + secret_detail), 503, 'safety_unavailable'),
            (GenerationFailed(secret_detail), 502, 'generation_failed'),
            (RuntimeError(secret_detail), 503, 'generation_unavailable')):
            self.api.generate_image.side_effect = failure
            actual, _, body = self.request()
            self.assertEqual((actual, body['error']['code']), (status, code))
            self.assertNotIn(secret_detail, json.dumps(body))
        self.assertEqual(5, self.api.generate_image.call_count)

    def test_busy_lock_and_existing_queue_do_not_submit(self):
        self.server.job_lock.acquire()
        try:
            self.assertEqual(429, self.request()[0])
        finally:
            self.server.job_lock.release()
        self.queue['queue_running'] = [['existing-job']]
        self.assertEqual(429, self.request()[0])
        self.api.generate_image.assert_not_called()

    def test_origin_and_host_rejected(self):
        self.assertEqual(403, self.request(headers={'Origin': 'https://example.com'})[0])
        self.assertEqual(403, self.request(headers={'Host': 'attacker.example'})[0])
        self.api.generate_image.assert_not_called()

    def test_framing_rejected_before_queue(self):
        for extras in ([('Content-Length', '2'), ('Content-Length', '2')],
                       [('Content-Length', '2'), ('Transfer-Encoding', 'chunked')],
                       [('Content-Length', str(65537))]):
            conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
            conn.putrequest('POST', '/v1/images/generations')
            conn.putheader('Authorization', 'Bearer ' + TEST_KEY)
            conn.putheader('Content-Type', 'application/json')
            for name, value in extras:
                conn.putheader(name, value)
            conn.endheaders(b'{}')
            response = conn.getresponse()
            self.assertIn(response.status, (400, 413))
            response.read()
            conn.close()
        self.api.generate_image.assert_not_called()

    def test_invalid_or_oversized_output_is_not_returned(self):
        for data, code in ((b'not-png', 'invalid_image'), (PNG + b'x' * gateway.MAX_PNG, 'image_too_large')):
            self.api.generate_image.return_value = data
            status, _, body = self.request()
            self.assertEqual((status, body['error']['code']), (502, code))
            self.assertNotIn('data', body)

    def test_key_file_validation_without_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'api_key.txt'
            self.assertEqual('', gateway.read_key(path))
            for key in ('short', 'x' * 32 + ' spaced', 'ключ' * 16):
                path.write_text(key, encoding='utf-8')
                self.assertEqual('', gateway.read_key(path))
            path.write_text(TEST_KEY + '\n', encoding='utf-8')
            self.assertEqual(TEST_KEY, gateway.read_key(path))

    def test_safety_auth_origin_and_host_before_check(self):
        with patch.object(gateway, 'moderation_request') as check:
            for path in ('/v1/safety/check-text', '/flux-api/v1/safety/check-image'):
                for auth in (None, '', 'wrong-key'):
                    self.assertEqual(401, self.request(path, {'texts': ['text']}, auth=auth)[0])
                for headers in ({'Origin': 'https://public-miniapp.example'}, {'Host': 'public-miniapp.example'}):
                    self.assertEqual(403, self.request(path, {'texts': ['text']}, headers=headers)[0])
            check.assert_not_called()
        self.api.generate_image.assert_not_called()
        self.api.Client.assert_not_called()

    def test_safety_text_allow_and_deny_are_decisions_not_generation(self):
        for allowed in (True, False):
            decision = {'allowed': allowed, 'reason': 'safe' if allowed else 'blocked', 'category': 'test'}
            with patch.object(gateway, 'moderation_request', return_value=decision) as check:
                status, headers, body = self.request('/flux-api/v1/safety/check-text', {'texts': ['Русский', 'English']})
                self.assertEqual((status, body), (200, decision))
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertNotIn('Access-Control-Allow-Origin', headers)
                path, raw, content_type = check.call_args.args
                self.assertEqual((path, content_type), ('/check-text', 'application/json'))
                self.assertEqual(json.loads(raw), {'texts': ['Русский', 'English']})
                self.assertEqual(1, check.call_count)
        self.api.generate_image.assert_not_called()

    def test_safety_bad_texts_unknown_fields_and_invalid_json_fail_before_helper(self):
        payloads = ([], {}, {'texts': []}, {'texts': [None]}, {'texts': ['x'] * 17},
                    {'texts': ['x' * 12001]}, {'texts': ['nul\x00text']}, {'texts': ['ok'], 'url': 'http://elsewhere'})
        with patch.object(gateway, 'moderation_request') as check:
            for payload in payloads:
                self.assertEqual(400, self.request('/v1/safety/check-text', payload)[0])
            for raw in (b'broken', b'\xff'):
                self.assertEqual(400, self.request('/v1/safety/check-text', raw=raw)[0])
            check.assert_not_called()

    def test_safety_limits_and_framing_fail_before_helper(self):
        with patch.object(gateway, 'moderation_request') as check:
            for path, maximum in (('/v1/safety/check-text', gateway.MAX_BODY), ('/v1/safety/check-image', gateway.MAX_PNG)):
                for framing, expected in (([('Content-Length', str(maximum + 1))], 413),
                                          ([('Content-Length', '2'), ('Content-Length', '2')], 400),
                                          ([('Content-Length', '2'), ('Transfer-Encoding', 'chunked')], 400),
                                          ([('Content-Length', '0')], 413)):
                    connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
                    connection.putrequest('POST', path)
                    connection.putheader('Authorization', 'Bearer ' + TEST_KEY)
                    for name, value in framing:
                        connection.putheader(name, value)
                    connection.endheaders(b'{}')
                    response = connection.getresponse()
                    self.assertEqual(expected, response.status)
                    response.read()
                    connection.close()
            check.assert_not_called()

    def test_safety_png_and_jpeg_are_normalized_once_to_png(self):
        decision = {'allowed': True, 'reason': 'safe', 'category': 'safe'}
        for image_format, content_type in (('PNG', 'image/png'), ('JPEG', 'image/jpeg')):
            output = io.BytesIO()
            Image.new('RGB', (8, 8), 'white').save(output, format=image_format)
            with patch.object(gateway, 'moderation_request', return_value=decision) as check:
                status, _, body = self.request('/v1/safety/check-image', raw=output.getvalue(),
                                               headers={'Content-Type': content_type})
                self.assertEqual((status, body), (200, decision))
                path, normalized, mime = check.call_args.args
                self.assertEqual((path, mime), ('/check-image', 'image/png'))
                with Image.open(io.BytesIO(normalized)) as image:
                    self.assertEqual((image.format, image.mode, image.size), ('PNG', 'RGB', (8, 8)))
                self.assertEqual(check.call_count, 1)
        self.api.generate_image.assert_not_called()

    def test_safety_rejects_corrupt_wrong_type_and_multiframe_images(self):
        animation = io.BytesIO()
        Image.new('RGB', (8, 8), 'white').save(animation, format='PNG', save_all=True,
                append_images=[Image.new('RGB', (8, 8), 'black')], duration=100, loop=0)
        with Image.open(io.BytesIO(animation.getvalue())) as image:
            self.assertEqual(2, image.n_frames)
        with patch.object(gateway, 'moderation_request') as check:
            for raw, mime in ((b'broken', 'image/png'), (PNG, 'application/json'),
                              (animation.getvalue(), 'image/png')):
                self.assertEqual(400, self.request('/v1/safety/check-image', raw=raw,
                                                   headers={'Content-Type': mime})[0])
            check.assert_not_called()

    def test_safety_unavailable_is_fixed_error_no_retry_or_generation(self):
        with patch.object(gateway, 'moderation_request', side_effect=RuntimeError('SECRET_BODY ' + TEST_KEY)) as check:
            status, _, body = self.request('/v1/safety/check-text', {'texts': ['Private input']})
            self.assertEqual((status, body['error']['code']), (503, 'safety_unavailable'))
            self.assertNotIn('SECRET_BODY', json.dumps(body))
            self.assertNotIn(TEST_KEY, json.dumps(body))
            self.assertNotIn('Private input', json.dumps(body))
            self.assertEqual(1, check.call_count)
        self.api.generate_image.assert_not_called()

    def test_configurable_prefix_and_private_host_without_opening_other_paths(self):
        self.server.hostname = 'gpu.example.ts.net'
        self.server.route_prefix = '/flux-demo-api'
        remote = {'Host': self.server.hostname}
        for path in ('/flux-demo-api/v1/models', '/v1/models'):
            self.assertEqual(200, self.request(path, method='GET', headers=remote)[0])
        for path in ('/flux-api/v1/models', '/prompt', '/view', '/system_stats', '/mini/'):
            self.assertEqual(404, self.request(path, method='GET', headers=remote)[0])
        self.assertEqual(403, self.request('/v1/models', method='GET', headers={'Host': 'another.example.ts.net'})[0])
        self.server.hostname = ''
        self.assertEqual(403, self.request('/v1/models', method='GET', headers=remote)[0])
        self.assertEqual(200, self.request('/v1/models', method='GET')[0])
        self.api.generate_image.assert_not_called()


class ModerationTransportTests(unittest.TestCase):
    def response(self, raw, status=200, content_type='application/json'):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = status
        response.headers = Message()
        response.headers['Content-Type'] = content_type
        response.read.return_value = raw
        return response

    def test_helper_is_loopback_no_proxy_no_redirect_and_no_bearer_forwarding(self):
        result = {'allowed': True, 'reason': '', 'category': 'safe', 'raw_scores': {'internal': 0.1}}
        opener = Mock()
        opener.open.return_value = self.response(json.dumps(result).encode())
        with patch.object(gateway.urllib.request, 'build_opener', return_value=opener) as build:
            decision = gateway.moderation_request('/check-text', b'{"texts":["text"]}', 'application/json')
        self.assertEqual({'allowed': True, 'reason': '', 'category': 'safe'}, decision)
        request = opener.open.call_args.args[0]
        self.assertEqual('http://127.0.0.1:8192/check-text', request.full_url)
        self.assertIsNone(request.get_header('Authorization'))
        self.assertEqual(opener.open.call_args.kwargs, {'timeout': 90})
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], gateway.NoRedirect)

    def test_helper_non200_wrong_type_oversize_and_bad_verdict_fail_closed(self):
        valid = b'{"allowed":true,"reason":"","category":"safe"}'
        cases = ((valid, 201, 'application/json'), (valid, 200, 'text/html'),
                 (b'x' * 65537, 200, 'application/json'), (b'broken', 200, 'application/json'),
                 (b'{"allowed":1,"reason":"","category":"safe"}', 200, 'application/json'),
                 (b'{"allowed":true}', 200, 'application/json'))
        for raw, status, content_type in cases:
            opener = Mock()
            opener.open.return_value = self.response(raw, status, content_type)
            with patch.object(gateway.urllib.request, 'build_opener', return_value=opener):
                with self.assertRaises(ValueError):
                    gateway.moderation_request('/check-text', b'{}', 'application/json')
            self.assertEqual(1, opener.open.call_count)


class GatewayConfigurationTests(unittest.TestCase):
    def test_default_loopback_and_alternate_instance_configuration(self):
        defaults = gateway.configuration({})
        self.assertEqual((defaults['port'], defaults['hostname'], defaults['prefix']), (8193, '', '/flux-api'))
        selected = gateway.configuration({'FLUX_GATEWAY_PORT': '8194',
                                          'FLUX_GATEWAY_HOSTNAME': 'GPU.Example.TS.Net',
                                          'FLUX_GATEWAY_PREFIX': '/flux-demo-api',
                                          'FLUX_GATEWAY_KEY_FILE': 'local-only-key.txt'})
        self.assertEqual((selected['port'], selected['hostname'], selected['prefix']),
                         (8194, 'gpu.example.ts.net', '/flux-demo-api'))
        self.assertEqual(Path('local-only-key.txt'), selected['key_file'])
        self.assertEqual('', gateway.configuration({'FLUX_GATEWAY_PREFIX': ''})['prefix'])

    def test_invalid_configuration_rejected_without_secret_values_in_errors(self):
        for key, values in (
            ('FLUX_GATEWAY_PORT', ('0', '80', '65536', 'not-an-integer')),
            ('FLUX_GATEWAY_HOSTNAME', ('*', 'public.example.com', 'https://gpu.example.ts.net',
                                      'gpu.example.ts.net:443', 'gpu.example.ts.net/path')),
            ('FLUX_GATEWAY_PREFIX', ('/', '/a/b', '/a?key=PRIVATE', '/a#fragment', 'flux-api')),
        ):
            for value in values:
                with self.assertRaises(ValueError) as error:
                    gateway.configuration({key: value})
                self.assertNotIn('PRIVATE', str(error.exception))

    def test_missing_or_invalid_key_prevents_binding_and_backend_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'key.txt'
            with patch.object(gateway, 'GatewayServer') as server, patch.object(gateway, 'load_backend') as backend:
                for content in (None, '', 'too-short'):
                    if content is not None:
                        path.write_text(content, encoding='utf-8')
                    with self.assertRaises(ValueError) as error:
                        gateway.build_server_from_environment({'FLUX_GATEWAY_KEY_FILE': str(path)})
                    self.assertNotIn(str(path), str(error.exception))
                server.assert_not_called()
                backend.assert_not_called()

    def test_new_instance_binds_only_requested_loopback_port_and_uses_existing_key_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'key.txt'
            path.write_text(TEST_KEY, encoding='utf-8')
            backend = object()
            with patch.object(gateway, 'GatewayServer') as server:
                gateway.build_server_from_environment({'FLUX_GATEWAY_KEY_FILE': str(path),
                    'FLUX_GATEWAY_PORT': '8194', 'FLUX_GATEWAY_HOSTNAME': 'gpu.example.ts.net',
                    'FLUX_GATEWAY_PREFIX': '/flux-demo-api'}, backend=backend)
            server.assert_called_once_with(('127.0.0.1', 8194), backend, TEST_KEY,
                                            hostname='gpu.example.ts.net', route_prefix='/flux-demo-api')

if __name__ == '__main__':
    unittest.main(verbosity=2)
