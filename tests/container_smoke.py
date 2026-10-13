"""Linux image integration: real Nginx/app/SSH, fake AI APIs, no GPU or downloads.

Run only in a disposable container with --network none and an empty test volume.
"""
import base64
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode

sys.path.insert(0, '/opt/h3')
sys.path.insert(0, '/opt/h3/deploy')
import entrypoint
from engine import DEFAULTS, atomic_json

CONTENT = b'fake-mp4-data' * 1024
JOB = '20000101_120000_12345678'


class AI(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, content, status=200, mime='application/json', extra=None):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(content)

    def do_GET(self):
        assert self.headers.get('Authorization') is None, 'Proxy leaked Basic credentials upstream'
        if self.path == '/ws':
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            accept = base64.b64encode(hashlib.sha1((self.headers['Sec-WebSocket-Key'] +
                '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            self.send_header('Sec-WebSocket-Accept', accept)
            self.end_headers()
        elif self.path.startswith('/view'):
            if self.headers.get('Range') == 'bytes=2-5':
                self.send(CONTENT[2:6], 206, 'video/mp4', {'Content-Range':f'bytes 2-5/{len(CONTENT)}'})
            else:
                self.send(CONTENT, mime='video/mp4', extra={'Accept-Ranges':'bytes'})
        elif self.path == '/api/tags':
            self.send(b'{"models":[]}')
        elif self.path == '/object_info':
            self.send(json.dumps({k:{} for k in ('MiniMaxH3ReferenceToVideo','H3VramHeadroom',
                'CacheBackendData //Inspire','RetrieveBackendData //Inspire')}).encode())
        elif self.path == '/queue':
            self.send(b'{"queue_running":[],"queue_pending":[]}')
        elif self.path == '/object_info/LoraLoaderModelOnly':
            names = sorted(p.name for p in Path(os.environ['H3_LORA_DIR']).glob('*.safetensors'))
            self.send(json.dumps({'LoraLoaderModelOnly': {'input': {'required': {
                'lora_name': [names], 'strength_model': ['FLOAT', {
                    'default': 1.0, 'min': -100.0, 'max': 100.0, 'step': 0.01}]}}}}).encode())
        else:
            self.send(b'{}')

    do_HEAD = do_GET

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.send(b'{"name":"uploaded.png","subfolder":"h3_automatik"}')


def main():
    root = Path('/workspace/h3')
    if (root/'app/settings.json').exists():
        raise SystemExit('Use a NEW disposable test volume; existing data detected.')
    secret = secrets.token_urlsafe(24)
    os.environ.update(H3_AUTH_USER='test', H3_AUTH_PASSWORD=secret, H3_TLS_MODE='selfsigned',
                      H3_ROOT=str(root), H3_MEMORY_MODE='budget', H3_RAM_BUDGET_GIB='8',
                      HF_TOKEN='smoke-value-never-persist')
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', '/tmp/test-ssh'], check=True)
    os.environ['H3_SSH_PUBLIC_KEY'] = Path('/tmp/test-ssh.pub').read_text().strip()
    entrypoint.configure()
    import yaml
    lora_directory = root / 'comfy/models/loras'
    assert lora_directory.is_dir()
    assert (lora_directory.stat().st_uid, lora_directory.stat().st_gid) == (10001, 10001)
    assert os.environ['H3_LORA_DIR'] == str(lora_directory)
    assert os.environ['H3_LORA_COMFY_URL'] == 'http://127.0.0.1:8188'
    model_paths = yaml.safe_load(Path('/run/h3/models.yaml').read_text())['h3']
    assert Path(model_paths['base_path']) / model_paths['loras'] == lora_directory
    installer_env = json.loads(Path('/run/h3/model-env.json').read_text())
    assert 'H3_LORA_DIR' not in installer_env and 'H3_LORA_COMFY_URL' not in installer_env
    persisted_lora = lora_directory / 'configuration-smoke.safetensors'
    subprocess.run([sys.executable, '-c',
                    'import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b"configuration-smoke")',
                    str(persisted_lora)], check=True, user=entrypoint.UID, group=entrypoint.GID,
                   extra_groups=[])
    assert not any('smoke-value-never-persist' in p.read_text(errors='ignore')
                   for p in root.rglob('*.json'))
    certificate = (root/'tls/server.crt').read_bytes()
    for port in (8188, 11434):
        server = ThreadingHTTPServer(('127.0.0.1', port), AI)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    atomic_json(root/'app/runs'/JOB/'job.json', dict(id=JOB, status='complete', master_prompt='test',
        folder='h3_automatik/'+JOB, final_path='h3_automatik/'+JOB+'/final.mp4',
        settings=dict(DEFAULTS), takes=[{}], slots=[{}]))
    auth = 'Basic ' + base64.b64encode(('test:' + secret).encode()).decode()
    context = ssl._create_unverified_context()  # Only this freshly generated test certificate.

    def fetch(port, path='/', method='GET', data=None, headers=None, authenticate=True):
        connection = http.client.HTTPSConnection('127.0.0.1', port, context=context, timeout=120)
        hdr = {'Authorization':auth} if authenticate else {}
        hdr.update(headers or {})
        body = data if isinstance(data, bytes) else json.dumps(data) if data is not None else None
        connection.request(method, path, body=body, headers=hdr)
        response = connection.getresponse()
        payload = response.read() if response.status != 101 else b''
        result = response.status, dict(response.getheaders()), payload
        connection.close()
        return result

    def drop_privileges():
        os.setgroups([])
        os.setgid(entrypoint.GID)
        os.setuid(entrypoint.UID)

    services = []
    try:
        nginx = subprocess.Popen(['nginx', '-c', '/run/h3/nginx.conf', '-g', 'daemon off;'], preexec_fn=drop_privileges)
        services.append(nginx)
        app = subprocess.Popen([sys.executable, '/opt/h3/start.py', '--no-browser'], preexec_fn=drop_privileges)
        services.append(app)
        ssh = subprocess.Popen(['/usr/sbin/sshd', '-D', '-e', '-f', '/run/h3/sshd_config'])
        services.append(ssh)
        for _ in range(60):
            try:
                if fetch(8080, '/healthz')[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(.2)
        else:
            raise AssertionError('App did not become ready')
        for port in (8080, 8081):
            assert fetch(port, authenticate=False)[0] == 401
            assert fetch(port, headers={'Authorization':'Basic invalid'})[0] == 401
            assert fetch(port)[0] == 200
            assert fetch(port, headers={'Sec-Fetch-Site':'cross-site'})[0] == 403
            # Provider dashboards open the homepage as a cross-site navigation.
            assert fetch(port, headers={'Sec-Fetch-Site':'cross-site', 'Sec-Fetch-Mode':'navigate'})[0] == 200
            assert fetch(port, '/api/status', headers={'Sec-Fetch-Site':'cross-site', 'Sec-Fetch-Mode':'navigate'})[0] == 403
        html = fetch(8080)[2].decode()
        assert 'id="shutdown" hidden' in html
        token = re.search("const token='([^']+)'", html)[1]
        headers = {'X-H3-Token':token, 'Content-Type':'application/json'}
        upload_path = '/api/lora-upload?' + urlencode(
            {'name': 'large.safetensors', 'comfy_url': 'http://127.0.0.1:8188'})
        upload_headers = {'X-H3-Token': token, 'Content-Type': 'application/octet-stream'}
        before = set(lora_directory.iterdir())
        assert fetch(8080, upload_path, 'POST', b'test', upload_headers, authenticate=False)[0] == 401
        assert fetch(8080, upload_path, 'POST', b'test',
                     dict(upload_headers, **{'Sec-Fetch-Site': 'cross-site'}))[0] == 403
        assert fetch(8080, upload_path, 'POST', b'test',
                     {'Content-Type': 'application/octet-stream'})[0] == 403
        assert set(lora_directory.iterdir()) == before
        # Header-only oversized requests: the normal JSON/image and ComfyUI
        # listeners reject immediately, without allocating or transmitting 35 MiB.
        oversized = dict(headers, **{'Content-Length': str(35 * 1024 * 1024)})
        assert fetch(8080, '/api/upload', 'POST', None, oversized)[0] == 413
        assert fetch(8081, '/api/lora-upload', 'POST', None, oversized)[0] == 413
        weights = b'upload-stream-smoke' * ((35 * 1024 * 1024) // 19 + 1)
        code, _, response = fetch(8080, upload_path, 'POST', weights, upload_headers)
        uploaded = json.loads(response)
        assert code == 200 and uploaded['listed'], response
        uploaded_path = lora_directory / uploaded['name']
        uploaded_hash = hashlib.sha256(weights).hexdigest()
        assert hashlib.sha256(uploaded_path.read_bytes()).hexdigest() == uploaded_hash
        # An external ComfyUI may use another UID; completed files must be readable.
        subprocess.run([sys.executable, '-c',
                        'import hashlib,pathlib,sys; assert hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest() == sys.argv[2]',
                        str(uploaded_path), uploaded_hash], check=True,
                       user=65534, group=65534, extra_groups=[])
        assert not list(lora_directory.glob('*.part'))
        assert fetch(8080, '/api/shutdown', 'POST', {}, headers)[0] == 400
        assert fetch(8080, '/api/upload', 'POST', dict(settings=DEFAULTS, name='test.png',
            base64=base64.b64encode(b'image').decode()), headers)[0] == 200
        jobs = json.loads(fetch(8080, '/api/jobs', headers=headers)[2])
        media = jobs[0]['final_url']
        code, hdr, body = fetch(8080, media, headers={'Range':'bytes=2-5'})
        assert code == 206 and body == CONTENT[2:6] and hdr['Content-Range'].startswith('bytes 2-5/')
        assert fetch(8080, media, method='HEAD')[2] == b''
        code, hdr, _ = fetch(8081, '/ws', headers={'Upgrade':'websocket','Connection':'Upgrade',
            'Sec-WebSocket-Key':'dGhlIHNhbXBsZSBub25jZQ==','Sec-WebSocket-Version':'13'})
        assert code == 101 and hdr['Upgrade'].lower() == 'websocket'
        subprocess.run(['ssh', '-i', '/tmp/test-ssh', '-o', 'StrictHostKeyChecking=no', '-o',
            'UserKnownHostsFile=/dev/null', '-o', 'BatchMode=yes', 'h3@127.0.0.1', 'h3-models --help'],
            check=True, stdout=subprocess.DEVNULL)
        assert fetch(8080, '/api/save', 'POST', {'settings':DEFAULTS}, headers)[0] == 200
        saved = (root/'app/settings.json').read_bytes()
        app.terminate()
        app.wait(10)
        entrypoint.configure()
        assert certificate == (root/'tls/server.crt').read_bytes()
        assert persisted_lora.read_bytes() == b'configuration-smoke'
        assert hashlib.sha256(uploaded_path.read_bytes()).hexdigest() == uploaded_hash
        app = subprocess.Popen([sys.executable, '/opt/h3/start.py', '--no-browser'], preexec_fn=drop_privileges)
        services.append(app)
        for _ in range(60):
            response = fetch(8080)
            if response[0] == 200:
                break
            time.sleep(.2)
        token = re.search("const token='([^']+)'", response[2].decode())[1]
        status = json.loads(fetch(8080, '/api/status', headers={'X-H3-Token':token})[2])
        assert not status['running'] and status['job'] is None
        assert saved == (root/'app/settings.json').read_bytes()
        print('PASS: HTTPS, both logins, upload, Range/HEAD, WebSocket, SSH installer, persistence, no job auto-resume')
    finally:
        for service in reversed(services):
            if service.poll() is None:
                service.terminate()
                try:
                    service.wait(10)
                except subprocess.TimeoutExpired:
                    service.kill()
                    service.wait()


if __name__ == '__main__':
    main()
