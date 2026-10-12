from email.message import Message
import http.client
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
import media
import start

JOB = '20000101_120000_12345678'
STATE = {'id': JOB, 'status': 'complete', 'folder': 'h3_automatik/' + JOB,
         'final_path': 'h3_automatik/' + JOB + '/final_00001.mp4',
         'final_url': 'https://obsolete.example/incorrect', 'master_prompt': 'test',
         'takes': [{}] * 7, 'slots': [{}] * 7, 'settings': engine.DEFAULTS}


class Upstream(io.BytesIO):
    def __init__(self, body=b'video', status=200, mime='video/mp4', **headers):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        for name, value in {'Content-Type': mime, 'Content-Length': str(len(body)),
                            'Accept-Ranges': 'bytes', **headers}.items():
            self.headers[name] = value
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        engine.atomic_json(cls.base / 'runs' / JOB / 'job.json', STATE)
        cls.base_patch = patch.object(start, 'BASE', cls.base)
        cls.base_patch.start()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), start.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.base_patch.stop()
        cls.temp.cleanup()

    def fetch(self, route, method='GET', headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, route, headers=headers or {})
            with connection.getresponse() as response:
                return response.status, response.headers, response.read()
        finally:
            connection.close()

    def test_existing_jobs_receive_fresh_local_links_without_changing_saved_state(self):
        before = (self.base / 'runs' / JOB / 'job.json').read_bytes()
        code, _, body = self.fetch('/api/jobs', headers={'X-H3-Token': start.TOKEN})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)[0]['final_url'], media.playback_url(STATE, start.TOKEN))
        with patch.dict(start.CURRENT, job=JOB, final_url=STATE['final_url']):
            code, _, body = self.fetch('/api/status', headers={'X-H3-Token': start.TOKEN})
            self.assertEqual(json.loads(body)['final_url'], media.playback_url(STATE, start.TOKEN))
        self.assertEqual(before, (self.base / 'runs' / JOB / 'job.json').read_bytes())

    def test_range_streams_in_bounded_chunks_and_does_not_forward_browser_headers(self):
        body = b'x' * (media.CHUNK_SIZE * 3 + 7)
        upstream = Upstream(body, 206, **{'Content-Range': f'bytes 10-{len(body)+9}/999999'})
        with patch.object(media.urllib.request.OpenerDirector, 'open', return_value=upstream) as opening:
            code, headers, actual = self.fetch(media.playback_url(STATE, start.TOKEN), headers={
                'Range': 'bytes=10-200000', 'Origin': self.host, 'Cookie': 'private',
                'Authorization': 'private', 'Sec-Fetch-Site': 'same-origin'})
        self.assertEqual((code, actual), (206, body))
        self.assertEqual(headers['Content-Type'], 'video/mp4')
        self.assertTrue(headers['Content-Range'].startswith('bytes 10-'))
        self.assertEqual(headers['Content-Length'], str(len(body)))
        self.assertTrue(all(n == media.CHUNK_SIZE for n in upstream.read_sizes))
        request = opening.call_args.args[0]
        self.assertEqual(request.get_header('Range'), 'bytes=10-200000')
        self.assertEqual(request.full_url, engine.view_url(STATE['settings'], STATE['final_path']))
        for name in ('Origin', 'Cookie', 'Authorization', 'Sec-fetch-site'):
            self.assertIsNone(request.get_header(name))

    def test_head_returns_metadata_without_reading_video(self):
        upstream = Upstream(b'metadata only')
        with patch.object(media.urllib.request.OpenerDirector, 'open', return_value=upstream) as opening:
            code, headers, body = self.fetch(media.playback_url(STATE, start.TOKEN), method='HEAD')
        self.assertEqual((code, body), (200, b''))
        self.assertEqual(headers['Content-Length'], '13')
        self.assertFalse(upstream.read_sizes)
        self.assertEqual(opening.call_args.args[0].get_method(), 'HEAD')

    def test_missing_expired_or_duplicate_key_and_cross_site_are_rejected_before_upstream(self):
        valid = media.playback_url(STATE, start.TOKEN)
        for route, headers in [(valid.split('?')[0], {}),
                               (media.playback_url(STATE, 'old-session'), {}),
                               (valid + '&key=duplicate', {}),
                               (valid, {'Sec-Fetch-Site': 'cross-site'}),
                               (valid, {'Host': 'untrusted.example'})]:
            with self.subTest(route=route), patch.object(media.urllib.request.OpenerDirector, 'open') as opening:
                self.assertEqual(self.fetch(route, headers=headers)[0], 403)
                opening.assert_not_called()

    def test_only_completed_job_output_paths_can_be_served(self):
        self.assertIsNone(media.playback_url({**STATE, 'status': 'generating'}, start.TOKEN))
        self.assertNotEqual(media.media_key(STATE, start.TOKEN),
                            media.media_key({**STATE, 'id': '20000101_120000_aaaaaaaa'}, start.TOKEN))
        for path in ('../secret.mp4', '/etc/secret.mp4', 'other/job.mp4',
                     STATE['folder'] + '/../../secret.mp4', STATE['folder'] + '/secret.json'):
            with self.subTest(path=path), self.assertRaises(engine.JobError):
                media.playback_url({**STATE, 'final_path': path}, start.TOKEN)

    def test_invalid_ranges_are_rejected_before_upstream(self):
        for value in ('bytes=0-1,4-5', 'bytes=-', 'not bytes'):
            with patch.object(media.urllib.request.OpenerDirector, 'open') as opening:
                self.assertEqual(self.fetch(media.playback_url(STATE, start.TOKEN), headers={'Range': value})[0], 400)
                opening.assert_not_called()

    def test_unsatisfiable_range_is_preserved(self):
        upstream = Upstream(b'', 416, **{'Content-Range': 'bytes */42'})
        with patch.object(media.urllib.request.OpenerDirector, 'open', return_value=upstream):
            code, headers, body = self.fetch(media.playback_url(STATE, start.TOKEN), headers={'Range': 'bytes=99999-'})
        self.assertEqual((code, body, headers['Content-Range']), (416, b'', 'bytes */42'))

    def test_upstream_html_errors_and_redirects_are_not_exposed_as_video(self):
        for upstream in (Upstream(b'<html>login</html>', mime='text/html'), Upstream(b'', 403), Upstream(b'', 302)):
            with patch.object(media.urllib.request.OpenerDirector, 'open', return_value=upstream):
                code, headers, body = self.fetch(media.playback_url(STATE, start.TOKEN))
            self.assertEqual(code, 502)
            self.assertTrue(headers['Content-Type'].startswith('application/json'))
            self.assertNotIn(b'<html>', body)
        self.assertIsNone(media.NoRedirects().redirect_request(None, None, 302, '', {}, 'https://other.example'))

    def test_connection_failure_and_concurrent_limit_release_resources(self):
        with patch.object(media.urllib.request.OpenerDirector, 'open', side_effect=urllib.error.URLError('down')):
            self.assertEqual(self.fetch(media.playback_url(STATE, start.TOKEN))[0], 502)
        for _ in range(4):
            self.assertTrue(media.STREAMS.acquire(blocking=False))
        try:
            self.assertEqual(self.fetch(media.playback_url(STATE, start.TOKEN))[0], 503)
        finally:
            for _ in range(4):
                media.STREAMS.release()


if __name__ == '__main__':
    unittest.main()
