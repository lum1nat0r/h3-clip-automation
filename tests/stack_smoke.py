"""Real Supervisor stack, CPU only; run init/check in two disposable containers.

Use the same NEW test volume for both phases. No host ports, GPU or network.
"""
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import sys
import time

sys.path.insert(0, '/opt/h3')
from engine import DEFAULTS, atomic_json
import container_memory


def main():
    phase = sys.argv[1]
    assert phase in ('init', 'check')
    root = Path('/workspace/h3')
    marker = root / 'recreation-smoke.json'
    if phase == 'init':
        assert not (root / 'app').exists(), 'Use a NEW disposable volume'
    else:
        expected = json.loads(marker.read_text())
    secret = secrets.token_urlsafe(24)
    env = dict(os.environ, H3_AUTH_USER='test', H3_AUTH_PASSWORD=secret,
               H3_TLS_MODE='selfsigned', H3_MEMORY_MODE='cgroup')
    # Same entrypoint/process supervision; the sole test override disables GPU use.
    bootstrap = """import sys
sys.path.insert(0, '/opt/h3/deploy')
import entrypoint
original = entrypoint.supervisor_config
entrypoint.supervisor_config = lambda *a, **k: original(*a, **k).replace('--disable-auto-launch', '--cpu --disable-auto-launch')
raise SystemExit(entrypoint.main())
"""
    context = ssl._create_unverified_context()  # Only the disposable test certificate.
    auth = 'Basic ' + base64.b64encode(('test:' + secret).encode()).decode()

    def fetch(port, path, headers=None, method='GET', body=None):
        client = http.client.HTTPSConnection('localhost', port, context=context, timeout=10)
        client.request(method, path, headers={'Authorization': auth, **(headers or {})}, body=body)
        response = client.getresponse()
        data = response.read() if response.status != 101 else b''
        status = response.status
        client.close()
        return status, data

    with open('/tmp/stack-smoke.log', 'w+') as log:
        process = subprocess.Popen([sys.executable, '-c', bootstrap], env=env, stdout=log, stderr=log)
        try:
            for _ in range(120):
                assert process.poll() is None, 'Supervisor exited'
                check = subprocess.run([sys.executable, '/opt/h3/deploy/healthcheck.py'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if check.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise AssertionError('Real services never became ready')
            assert fetch(8080, '/healthz')[0] == 200
            assert fetch(8081, '/object_info')[0] == 200
            assert fetch(8081, '/ws', {'Origin':'https://localhost:8081', 'Upgrade':'websocket',
                'Connection':'Upgrade', 'Sec-WebSocket-Key':'dGhlIHNhbXBsZSBub25jZQ==',
                'Sec-WebSocket-Version':'13'})[0] == 101
            png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aPuoAAAAASUVORK5CYII=')
            body = (b'--h3smoke\r\nContent-Disposition: form-data; name="image"; filename="smoke.png"\r\n'
                    b'Content-Type: image/png\r\n\r\n' + png + b'\r\n--h3smoke--\r\n')
            assert fetch(8081, '/upload/image', {'Content-Type':'multipart/form-data; boundary=h3smoke'}, 'POST', body)[0] == 200
            html = fetch(8080, '/')[1].decode()
            token = re.search("const token='([^']+)'", html)[1]
            status = json.loads(fetch(8080, '/api/status', {'X-H3-Token':token})[1])
            assert status['running'] is False and status['job'] is None
            state = container_memory.snapshot()
            assert state['hard_limit_verified'] and state['budget_gib'] == 8
            cert = hashlib.sha256((root/'tls/server.crt').read_bytes()).hexdigest()
            if phase == 'init':
                atomic_json(root/'app/settings.json', DEFAULTS)
                atomic_json(root/'app/runs/20000101_120000_12345678/job.json',
                            dict(id='20000101_120000_12345678', status='running', settings=DEFAULTS))
                atomic_json(marker, {'certificate':cert, 'settings':DEFAULTS})
            else:
                assert cert == expected['certificate']
                assert json.loads((root/'app/settings.json').read_text()) == expected['settings']
                assert (root/'comfy/input/smoke.png').exists()
            print('PASS: real Supervisor/ComfyUI/Ollama/app readiness, HTTPS/WebSocket/upload, cgroup telemetry; phase ' + phase)
        except BaseException:
            log.flush()
            print(Path('/tmp/stack-smoke.log').read_text()[-12000:])
            raise
        finally:
            process.terminate()
            process.wait(55)
            if sys.exc_info()[0] is None:
                assert process.returncode == 0, 'Supervisor failed to shut down cleanly'


if __name__ == '__main__':
    main()
