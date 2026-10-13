from contextlib import contextmanager
import copy
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import shutil
import socket
import tempfile
import threading
import unittest
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
import lora_upload
import library
import start
from test_automation import SCHEMA
from runtime_lock import exclusive


class ChunkLimitedStream(io.BytesIO):
    def __init__(self, payload):
        super().__init__(payload)
        self.read_sizes = []

    def read(self, size=-1):
        if not 0 < size <= 1024**2:
            raise AssertionError('Upload must read at most 1 MiB at a time.')
        self.read_sizes.append(size)
        return super().read(size)


class FailingWriter:
    def __init__(self, handle):
        self.handle = handle

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def write(self, data):
        # Fail after a partial disk write, not before creating the temporary file.
        self.handle.write(data[:max(1, len(data) // 2)])
        raise OSError('Injected disk write failure')


class LoraUploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), start.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.folder = self.base / 'loras'
        self.folder.mkdir()
        self.comfy = 'http://127.0.0.1:18188'
        self.queue = {'queue_running': [], 'queue_pending': []}
        self.catalog_names = None
        self.catalog_error = None
        self.upstream_calls = []
        for p in (patch.object(start, 'BASE', self.base),
                  patch.dict(start.CURRENT, running=False),
                  patch.dict(os.environ, H3_CONTAINER='0', H3_LORA_DIR=str(self.folder), H3_LORA_COMFY_URL=self.comfy),
                  patch.object(lora_upload, 'ensure_idle',
                               side_effect=lambda c: engine.ensure_idle(c, self.upstream)),
                  patch.object(start, 'lora_catalog',
                               side_effect=lambda c: library.lora_catalog(c, self.upstream))):
            p.start()
            self.addCleanup(p.stop)

    def post(self, route, body, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=15)
        try:
            connection.request('POST', route, body=body,
                               headers=headers or {'X-H3-Token': start.TOKEN, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def upstream(self, base, route, data=None, timeout=30):
        self.assertEqual(base, self.comfy)
        self.assertIsNone(data)
        self.upstream_calls.append(route)
        if route == '/queue':
            return self.queue
        self.assertEqual(route, '/object_info/LoraLoaderModelOnly')
        self.assertEqual(timeout, 8)
        if self.catalog_error:
            raise engine.JobError(self.catalog_error)
        node = copy.deepcopy(SCHEMA['LoraLoaderModelOnly'])
        names = self.catalog_names
        if names is None:
            names = sorted(path.name for path in self.folder.glob('*.safetensors'))
        node['input']['required']['lora_name'][0] = names
        return {'LoraLoaderModelOnly': node}

    def upload_route(self, name='weights.safetensors', comfy=None):
        return '/api/lora-upload?' + urlencode({
            'name': name, 'comfy_url': self.comfy if comfy is None else comfy})

    def upload(self, body=b'\x00\xffweights', *, route=None, headers=None, length=None,
               finish_early=False):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=15)
        default_headers = [('X-H3-Token', start.TOKEN),
                           ('Content-Type', 'application/octet-stream'),
                           ('Content-Length', str(len(body) if length is None else length))]
        try:
            connection.putrequest('POST', route or self.upload_route(),
                                  skip_host=True, skip_accept_encoding=True)
            selected_headers = default_headers if headers is None else headers
            if not any(key.lower() == 'host' for key, _ in selected_headers):
                connection.putheader('Host', f'127.0.0.1:{self.server.server_port}')
            for key, value in selected_headers:
                connection.putheader(key, value)
            connection.endheaders()
            if hasattr(body, 'read'):
                while chunk := body.read(1024**2):
                    connection.send(chunk)
            elif body:
                connection.send(body)
            if finish_early:
                connection.sock.shutdown(socket.SHUT_WR)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def assert_no_upload(self):
        self.assertEqual(list(self.folder.iterdir()), [])

    def assert_saved(self, result, payload):
        name = result['name']
        self.assertEqual(Path(name).name, name)
        self.assertRegex(name, r'^[0-9a-f]{32}_[A-Za-z0-9_.-]+\.safetensors$')
        self.assertEqual((self.folder / name).read_bytes(), payload)
        self.assertFalse(list(self.folder.glob('*.part')))
        return name

    @contextmanager
    def storage_failure(self, *, opening=False):
        """Inject filesystem failures only for files created in the LoRA folder."""
        real_open, real_io_open = open, io.open
        real_os_open, real_fdopen = os.open, os.fdopen

        def is_target(file):
            return isinstance(file, (str, bytes, os.PathLike)) and Path(
                os.fsdecode(file)).parent.resolve() == self.folder.resolve()

        def wrap_open(opener):
            def injected(file, mode='r', *args, **kwargs):
                target = is_target(file) and any(flag in mode for flag in 'wax+')
                if target and opening:
                    raise PermissionError('Injected folder permission failure')
                handle = opener(file, mode, *args, **kwargs)
                return FailingWriter(handle) if target else handle
            return injected

        def injected_os_open(file, flags, *args, **kwargs):
            if opening and is_target(file) and flags & (os.O_WRONLY | os.O_RDWR):
                raise PermissionError('Injected folder permission failure')
            return real_os_open(file, flags, *args, **kwargs)

        def injected_fdopen(fd, *args, **kwargs):
            return FailingWriter(real_fdopen(fd, *args, **kwargs))

        with patch('builtins.open', side_effect=wrap_open(real_open)), \
                patch.object(io, 'open', side_effect=wrap_open(real_io_open)), \
                patch.object(os, 'open', side_effect=injected_os_open), \
                patch.object(os, 'fdopen', side_effect=injected_fdopen):
            yield

    def test_operator_folder_and_exact_server_mapping(self):
        self.assertEqual(lora_upload.upload_options(self.comfy + '/'),
                         {'available': True, 'reason': '', 'max_bytes': 2147483648})
        self.assertFalse(lora_upload.upload_options('http://localhost:18188')['available'])
        for env in ({'H3_LORA_DIR': ''}, {'H3_LORA_COMFY_URL': ''},
                    {'H3_LORA_COMFY_URL': 'ftp://host'}, {'H3_LORA_DIR': 'relative'},
                    {'H3_LORA_DIR': str(self.base / 'absent')}):
            with self.subTest(env=env), patch.dict(os.environ, env):
                options = lora_upload.upload_options(self.comfy)
                self.assertFalse(options['available'])
                self.assertTrue(options['reason'])
        with patch.object(lora_upload.os, 'access', return_value=False):
            self.assertFalse(lora_upload.upload_options(self.comfy)['available'])

    def test_live_catalog_endpoint_and_auth(self):
        body = json.dumps({'settings': {'comfy_url': self.comfy}}).encode()
        def discovery(base, route, data=None, timeout=30):
            self.assertEqual((base, route, timeout), (self.comfy, '/object_info/LoraLoaderModelOnly', 8))
            return {'LoraLoaderModelOnly': SCHEMA['LoraLoaderModelOnly']}
        with patch.object(start, 'lora_catalog',
                          side_effect=lambda c: library.lora_catalog(c, discovery)):
            status, result = self.post('/api/loras', body)
        self.assertEqual(status, 200)
        self.assertEqual(result['loras'], ['characters/test [v1].safetensors', 'styles/test.safetensors'])
        self.assertEqual(result['strength'], {'default': 1.0, 'min': -100.0, 'max': 100.0, 'step': 0.01})
        self.assertTrue(result['upload']['available'])
        for headers in ({'Content-Type': 'application/json'},
                        {'X-H3-Token': start.TOKEN, 'Host': 'untrusted.example'}):
            status, _ = self.post('/api/loras', body, headers)
            self.assertEqual(status, 403)

    def test_binary_upload_larger_than_json_limit_preserves_hash(self):
        source = self.base / 'payload.bin'
        chunk = bytes(range(256)) * 4096
        digest = hashlib.sha256()
        with source.open('wb') as output:
            for _ in range(35):
                output.write(chunk)
                digest.update(chunk)
            output.write(b'\x00\xfffinal bytes')
            digest.update(b'\x00\xfffinal bytes')
        with source.open('rb') as body:
            status, result = self.upload(body, length=source.stat().st_size)
        self.assertEqual(status, 200)
        self.assertEqual(result['listed'], True)
        self.assertEqual(result['warning'], '')
        saved = self.folder / result['name']
        self.assertGreater(saved.stat().st_size, 34 * 1024**2)
        self.assertEqual(saved.stat().st_size, source.stat().st_size)
        saved_digest = hashlib.sha256()
        with saved.open('rb') as stored:
            while chunk := stored.read(1024**2):
                saved_digest.update(chunk)
        self.assertEqual(saved_digest.digest(), digest.digest())
        self.assertEqual(list(self.folder.iterdir()), [saved])

    def test_same_browser_name_never_overwrites_and_sanitizes_basename(self):
        original = self.folder / 'weights.safetensors'
        original.write_bytes(b'operator-owned weights')
        names = []
        for payload in (b'\x00first', b'\xffsecond'):
            status, result = self.upload(payload, route=self.upload_route(
                r'C:\fakepath\..\styles\my weights [v1].safetensors'))
            self.assertEqual(status, 200)
            self.assertTrue(result['listed'])
            names.append(self.assert_saved(result, payload))
            self.assertTrue(names[-1].endswith('_my_weights__v1_.safetensors'))
        self.assertNotEqual(*names)
        self.assertEqual(original.read_bytes(), b'operator-owned weights')
        self.assertEqual(len(list(self.folder.iterdir())), 3)

    def test_auth_and_host_are_checked_before_body_read(self):
        cases = [
            [('Content-Type', 'application/octet-stream'), ('Content-Length', '100')],
            [('X-H3-Token', 'wrong'), ('Content-Type', 'application/octet-stream'),
             ('Content-Length', '100')],
            [('X-H3-Token', start.TOKEN), ('Host', 'untrusted.example'),
             ('Content-Type', 'application/octet-stream'), ('Content-Length', '100')],
        ]
        for headers in cases:
            with self.subTest(headers=headers):
                status, result = self.upload(b'', headers=headers)
                self.assertEqual(status, 403)
                self.assertTrue(result['error'])
                self.assert_no_upload()
        self.assertEqual(self.upstream_calls, [])

    def test_strict_lengths_and_transfer_encoding_reject_without_body(self):
        required = [('X-H3-Token', start.TOKEN), ('Content-Type', 'application/octet-stream')]
        cases = [[]]
        cases.extend([('Content-Length', value)] for value in
                     ('', '0', '-1', '+1', '1.0', 'not-a-number', '1, 1'))
        cases.extend([
            [('Content-Length', '1'), ('Content-Length', '1')],
            [('Content-Length', '1'), ('Content-Length', '2')],
            [('Transfer-Encoding', 'chunked')],
            [('Content-Length', '100'), ('Transfer-Encoding', 'chunked')],
            [('Content-Length', '100'), ('Transfer-Encoding', 'identity')],
        ])
        for headers in cases:
            with self.subTest(headers=headers):
                status, result = self.upload(b'', headers=required + headers)
                self.assertEqual(status, 400)
                self.assertTrue(result['error'])
                self.assert_no_upload()
        self.assertEqual(self.upstream_calls, [])

    def test_oversize_length_is_413_without_transferring_large_body(self):
        for length in (2147483649, 10**30):
            with self.subTest(length=length):
                status, result = self.upload(b'', length=length)
                self.assertEqual(status, 413)
                self.assertTrue(result['error'])
                self.assert_no_upload()
        self.assertEqual(self.upstream_calls, [])

    def test_invalid_url_parameters_reject_without_body(self):
        valid = urlencode({'name': 'weights.safetensors', 'comfy_url': self.comfy})
        queries = ['', urlencode({'name': 'weights.safetensors'}),
                   urlencode({'comfy_url': self.comfy}),
                   valid + '&name=other.safetensors', valid + '&comfy_url=' + self.comfy,
                   valid + '&directory=' + str(self.folder), valid + '&unexpected=value',
                   urlencode({'name': '', 'comfy_url': self.comfy}),
                   urlencode({'name': 'weights.safetensors', 'comfy_url': ''})]
        for query in queries:
            with self.subTest(query=query):
                status, result = self.upload(b'', length=100, route='/api/lora-upload?' + query)
                self.assertEqual(status, 400)
                self.assertTrue(result['error'])
                self.assert_no_upload()
        self.assertEqual(self.upstream_calls, [])

    def test_invalid_content_type_and_filename_reject_without_body(self):
        for content_type in ('application/json', 'multipart/form-data', ''):
            with self.subTest(content_type=content_type):
                headers = [('X-H3-Token', start.TOKEN), ('Content-Length', '100')]
                if content_type:
                    headers.append(('Content-Type', content_type))
                status, _ = self.upload(b'', headers=headers)
                self.assertEqual(status, 400)
                self.assert_no_upload()
        for name in ('weights.bin', 'weights.safetensors.exe', '..', 'folder/'):
            with self.subTest(name=name):
                status, _ = self.upload(b'', length=100, route=self.upload_route(name))
                self.assertEqual(status, 400)
                self.assert_no_upload()

    def test_missing_configuration_and_server_mismatch_reject_without_body(self):
        for env in ({'H3_LORA_DIR': ''}, {'H3_LORA_COMFY_URL': ''},
                    {'H3_LORA_DIR': str(self.base / 'missing')}):
            with self.subTest(env=env), patch.dict(os.environ, env):
                status, result = self.upload(b'', length=100)
                self.assertEqual(status, 400)
                self.assertTrue(result['error'])
                self.assert_no_upload()
        status, result = self.upload(b'', length=100,
                                     route=self.upload_route(comfy='http://localhost:18188'))
        self.assertEqual(status, 400)
        self.assertTrue(result['error'])
        self.assert_no_upload()

    def test_running_controller_or_activity_lease_rejects_before_reading(self):
        with patch.dict(start.CURRENT, running=True):
            status, result = self.upload(b'', length=100)
        self.assertEqual(status, 400)
        self.assertTrue(result['error'])
        self.assert_no_upload()
        with exclusive(self.base):
            status, result = self.upload(b'', length=100)
        self.assertEqual(status, 400)
        self.assertTrue(result['error'])
        self.assert_no_upload()
        self.assertEqual(self.upstream_calls, [])

    def test_upstream_running_and_pending_queues_reject_without_interrupting(self):
        for field in ('queue_running', 'queue_pending'):
            with self.subTest(field=field):
                self.queue = {'queue_running': [], 'queue_pending': []}
                self.queue[field] = [[1, 'someone-elses-prompt', {}, {}]]
                status, result = self.upload(b'', length=100)
                self.assertEqual(status, 400)
                self.assertTrue(result['error'])
                self.assert_no_upload()
        self.assertEqual(self.upstream_calls, ['/queue', '/queue'])

    def test_short_http_body_removes_temporary_file(self):
        status, result = self.upload(b'partial weights', length=100, finish_early=True)
        self.assertEqual(status, 400)
        self.assertTrue(result['error'])
        self.assert_no_upload()
        # Failure must release the lease as well as removing incomplete weights.
        status, result = self.upload(b'complete weights')
        self.assertEqual(status, 200)
        self.assert_saved(result, b'complete weights')

    def test_saved_but_not_discovered_keeps_bytes_and_reports_warning(self):
        warning = ('Datei gespeichert, aber noch nicht in ComfyUI verfügbar. '
                   'LoRA-Modellpfad prüfen und Liste aktualisieren.')
        for error in (None, 'Discovery unavailable'):
            with self.subTest(error=error):
                self.catalog_names = []
                self.catalog_error = error
                payload = b'persist even without discovery ' + str(error).encode()
                status, result = self.upload(payload)
                self.assertEqual(status, 200)
                self.assertFalse(result['listed'])
                self.assertEqual(result['warning'], warning)
                self.assert_saved(result, payload)

    def test_discovery_requires_exact_name_not_basename(self):
        def prefixed_catalog(settings):
            result = library.lora_catalog(settings, self.upstream)
            result['loras'] = ['styles/' + name for name in result['loras']]
            return result
        with patch.object(start, 'lora_catalog', side_effect=prefixed_catalog):
            status, result = self.upload(b'exact name required')
        self.assertEqual(status, 200)
        self.assertFalse(result['listed'])
        self.assertTrue(result['warning'])
        self.assert_saved(result, b'exact name required')

    def test_direct_store_streams_in_bounded_reads_and_keeps_lease(self):
        payload = bytes(range(256)) * (3 * 4096) + b'\xfftail'
        test = self

        class LeaseCheckingStream(ChunkLimitedStream):
            def read(self, size=-1):
                with test.assertRaises(RuntimeError):
                    with exclusive(test.base):
                        pass
                return super().read(size)

        stream = LeaseCheckingStream(payload)
        name = lora_upload.store_lora(stream, len(payload), 'weights.safetensors',
                                      self.comfy, self.base)
        self.assert_saved({'name': name}, payload)
        self.assertGreater(len(stream.read_sizes), 1)
        self.assertEqual(stream.tell(), len(payload))
        with exclusive(self.base):
            pass

    def test_random_name_collision_retries_without_overwriting(self):
        first_prefix, next_prefix = 'a' * 32, 'b' * 32
        existing = self.folder / (first_prefix + '_weights.safetensors')
        existing.write_bytes(b'existing operator-owned weights')
        payload = b'new weights'
        with patch.object(lora_upload.secrets, 'token_hex',
                          side_effect=[first_prefix, next_prefix]):
            name = lora_upload.store_lora(ChunkLimitedStream(payload), len(payload),
                                          'weights.safetensors', self.comfy, self.base)
        self.assertEqual(name, next_prefix + '_weights.safetensors')
        self.assert_saved({'name': name}, payload)
        self.assertEqual(existing.read_bytes(), b'existing operator-owned weights')
        self.assertEqual({path.name for path in self.folder.iterdir()}, {existing.name, name})

    def test_direct_store_reads_only_declared_length(self):
        stream = ChunkLimitedStream(b'weights' + b'next request')
        name = lora_upload.store_lora(stream, 7, 'weights.safetensors', self.comfy, self.base)
        self.assert_saved({'name': name}, b'weights')
        self.assertEqual(stream.tell(), 7)

    def test_direct_store_eof_and_read_timeout_clean_up(self):
        class TimeoutStream(ChunkLimitedStream):
            def read(self, size=-1):
                if self.tell():
                    raise TimeoutError('Injected read timeout')
                return super().read(min(size, 3))

        for stream in (ChunkLimitedStream(b'short'), TimeoutStream(b'partial')):
            with self.subTest(stream=type(stream).__name__):
                with self.assertRaises((engine.JobError, OSError)):
                    lora_upload.store_lora(stream, 100, 'weights.safetensors',
                                           self.comfy, self.base)
                self.assert_no_upload()
                with exclusive(self.base):
                    pass

    def test_direct_store_disk_write_error_removes_partial_file(self):
        stream = ChunkLimitedStream(b'weights')
        with self.storage_failure(), self.assertRaises((engine.JobError, OSError)):
            lora_upload.store_lora(stream, 7, 'weights.safetensors', self.comfy, self.base)
        self.assertTrue(stream.read_sizes)
        self.assert_no_upload()
        with exclusive(self.base):
            pass

    def test_direct_store_checks_disk_reserve_before_reading(self):
        payload = b'weights'
        required = len(payload) + 2 * 1024**3
        stream = ChunkLimitedStream(payload)
        with patch.object(shutil, 'disk_usage', return_value=SimpleNamespace(
                total=required * 2, used=required + 1, free=required - 1)):
            with self.assertRaises((engine.JobError, OSError)):
                lora_upload.store_lora(stream, len(payload), 'weights.safetensors',
                                       self.comfy, self.base)
        self.assertEqual(stream.read_sizes, [])
        self.assert_no_upload()
        with patch.object(shutil, 'disk_usage', return_value=SimpleNamespace(
                total=required * 2, used=required, free=required)):
            name = lora_upload.store_lora(stream, len(payload), 'weights.safetensors',
                                          self.comfy, self.base)
        self.assert_saved({'name': name}, payload)

    def test_direct_store_rechecks_permissions_and_reports_open_failure(self):
        stream = ChunkLimitedStream(b'weights')
        with patch.object(os, 'access', return_value=False):
            with self.assertRaises((engine.JobError, OSError)):
                lora_upload.store_lora(stream, 7, 'weights.safetensors', self.comfy, self.base)
        self.assertEqual(stream.read_sizes, [])
        self.assert_no_upload()
        with self.storage_failure(opening=True), self.assertRaises((engine.JobError, OSError)):
            lora_upload.store_lora(stream, 7, 'weights.safetensors', self.comfy, self.base)
        self.assert_no_upload()

    @unittest.skipIf(os.name == 'nt', 'POSIX shared-file permissions')
    def test_incomplete_weights_private_and_published_weights_shared_readable(self):
        test = self

        class InspectingStream(ChunkLimitedStream):
            def read(self, size=-1):
                parts = list(test.folder.glob('*.part'))
                test.assertEqual(len(parts), 1)
                test.assertEqual(parts[0].stat().st_mode & 0o077, 0)
                return super().read(size)

        payload = b'shared model weights'
        name = lora_upload.store_lora(InspectingStream(payload), len(payload),
                                      'shared.safetensors', self.comfy, self.base)
        self.assertEqual((self.folder / name).stat().st_mode & 0o777, 0o644)
        self.assert_saved({'name': name}, payload)


if __name__ == '__main__':
    unittest.main()
