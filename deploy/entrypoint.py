"""Prepare private runtime configuration, then exec Supervisor. No downloads."""
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

UID = GID = 10001
RUNTIME = Path('/run/h3')


def private_file(path, content, owner=True):
    path.write_text(content, encoding='utf-8')
    path.chmod(0o600)
    if owner:
        os.chown(path, UID, GID)


def nginx_config(tls_mode):
    tls = '' if tls_mode == 'proxy' else '''
        ssl_certificate /run/h3/tls.crt;
        ssl_certificate_key /run/h3/tls.key;
        ssl_protocols TLSv1.2 TLSv1.3;
    '''
    listeners = ''
    for public, internal in ((8080, 8765), (8081, 8188)):
        host = '127.0.0.1:8765' if internal == 8765 else '$http_host'
        listeners += f'''
    server {{
        listen {public}{' ssl' if tls_mode != 'proxy' else ''};
        server_name _;
        {tls}
        auth_basic "H3";
        auth_basic_user_file /run/h3/htpasswd;
        if ($blocked_cross_site) {{ return 403; }}
        location / {{
            proxy_pass http://127.0.0.1:{internal};
            proxy_http_version 1.1;
            proxy_set_header Host {host};
            proxy_set_header Authorization "";
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
            proxy_set_header X-Forwarded-For "";
            proxy_set_header X-Forwarded-Host "";
            proxy_buffering off;
            proxy_request_buffering off;
            proxy_read_timeout 3600s;
        }}
    }}
'''
    return '''pid /run/h3/nginx.pid;
error_log stderr warn;
events { worker_connections 512; }
http {
    include /etc/nginx/mime.types;
    access_log off;
    server_tokens off;
    client_max_body_size 34m;
    client_body_temp_path /run/h3/client_temp;
    proxy_temp_path /run/h3/proxy_temp;
    fastcgi_temp_path /run/h3/fastcgi_temp;
    uwsgi_temp_path /run/h3/uwsgi_temp;
    scgi_temp_path /run/h3/scgi_temp;
    map $http_upgrade $connection_upgrade { default upgrade; '' close; }
    map "$http_sec_fetch_site:$http_sec_fetch_mode:$request_method:$uri" $blocked_cross_site {
        default 0;
        "cross-site:navigate:GET:/" 0;
        ~^cross-site: 1;
    }
''' + listeners + '\n}\n'


def supervisor_config(root, ssh=False):
    programs = {
        'ollama': ('/usr/bin/ollama serve', '/opt/h3', 'h3'),
        'comfy': (f'/opt/venv/bin/python /opt/comfy/main.py --listen 127.0.0.1 --port 8188 '
                  f'--disable-auto-launch --input-directory {root}/comfy/input --output-directory {root}/comfy/output '
                  f'--temp-directory {root}/comfy/temp --user-directory {root}/comfy/user '
                  '--extra-model-paths-config /run/h3/models.yaml', '/opt/comfy', 'h3'),
        'app': ('/opt/venv/bin/python /opt/h3/start.py --no-browser', '/opt/h3', 'h3'),
        'nginx': ('/usr/sbin/nginx -c /run/h3/nginx.conf -g "daemon off;"', '/opt/h3', 'h3'),
    }
    if ssh:
        programs['ssh'] = ('/usr/sbin/sshd -D -e -f /run/h3/sshd_config', '/opt/h3', 'root')
    config = '[supervisord]\nnodaemon=true\nlogfile=/dev/null\npidfile=/run/h3/supervisor.pid\nuser=root\n'
    for name, (command, directory, user) in programs.items():
        config += f'''
[program:{name}]
command={command}
directory={directory}
user={user}
autostart=true
autorestart=unexpected
startsecs=5
startretries=3
stopasgroup=true
killasgroup=true
stopwaitsecs=45
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0
'''
    return config


def configure(env=None):
    env = os.environ if env is None else env
    user, password = env.get('H3_AUTH_USER', ''), env.get('H3_AUTH_PASSWORD', '')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', user) or len(password) < 16 or any(c in password for c in '\r\n\0'):
        raise ValueError('H3_AUTH_USER und H3_AUTH_PASSWORD (mindestens 16 Zeichen, eine Zeile) sind erforderlich.')
    mode = env.get('H3_TLS_MODE', 'selfsigned')
    if mode not in ('proxy', 'selfsigned', 'certificate'):
        raise ValueError('H3_TLS_MODE muss proxy, selfsigned oder certificate sein.')
    root = Path(env.get('H3_ROOT', '/workspace/h3'))
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(root)) or '..' in root.parts:
        raise ValueError('H3_ROOT muss ein absoluter Linux-Pfad ohne Leerzeichen sein.')
    root = root.resolve()
    for path in (RUNTIME, root, *(root / p for p in ('app', 'downloads', 'comfy/models', 'comfy/input',
                 'comfy/output', 'comfy/temp', 'comfy/user', 'ollama', 'ollama/models', 'home', 'tls'))):
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, UID, GID)
    RUNTIME.chmod(0o700)
    hashed = subprocess.run(['htpasswd', '-niB', user], input=password+'\n', text=True,
                            capture_output=True, check=True).stdout
    private_file(RUNTIME / 'htpasswd', hashed)
    if mode != 'proxy':
        if mode == 'certificate':
            cert, key = Path(env['H3_TLS_CERT']), Path(env['H3_TLS_KEY'])
        else:
            cert, key = root / 'tls/server.crt', root / 'tls/server.key'
            if cert.exists() != key.exists():
                raise ValueError('TLS-Zertifikat und Schlüssel müssen gemeinsam vorhanden sein.')
            if not cert.exists():
                host = env.get('H3_TLS_HOST', 'localhost')
                if not re.fullmatch(r'[A-Za-z0-9.:-]+', host):
                    raise ValueError('Ungültiger H3_TLS_HOST.')
                try:
                    ipaddress.ip_address(host)
                    san = 'IP:' + host
                except ValueError:
                    san = 'DNS:' + host
                subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-days', '365',
                                '-subj', '/CN=' + host, '-addext', 'subjectAltName=' + san,
                                '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True)
                key.chmod(0o600)
            subprocess.run(['openssl', 'x509', '-in', str(cert), '-noout', '-fingerprint', '-sha256'], check=True)
        for source, target in ((cert, 'tls.crt'), (key, 'tls.key')):
            shutil.copyfile(source, RUNTIME / target)
            (RUNTIME / target).chmod(0o600)
            os.chown(RUNTIME / target, UID, GID)
    env.update(H3_CONTAINER='1', H3_ROOT=str(root), H3_DATA_DIR=str(root / 'app'),
               HOME=str(root / 'home'), OLLAMA_MODELS=str(root / 'ollama/models'),
               OLLAMA_HOST='127.0.0.1:11434', OLLAMA_MAX_LOADED_MODELS='1', OLLAMA_NUM_PARALLEL='1',
               OLLAMA_KEEP_ALIVE='0', OLLAMA_NO_CLOUD='1', HF_HUB_DISABLE_TELEMETRY='1')
    allow = ('H3_CONTAINER', 'H3_ROOT', 'H3_DATA_DIR', 'H3_MEMORY_MODE', 'H3_RAM_BUDGET_GIB',
             'HF_TOKEN', 'HF_HUB_DISABLE_TELEMETRY', 'HOME', 'OLLAMA_MODELS')
    private_file(RUNTIME / 'model-env.json', json.dumps({k: env[k] for k in allow if k in env}))
    private_file(RUNTIME / 'models.yaml', f'h3:\n  base_path: {root}/comfy/models\n  diffusion_models: diffusion_models\n  text_encoders: text_encoders\n  vae: vae\n')
    private_file(RUNTIME / 'nginx.conf', nginx_config(mode))
    public_key = env.get('H3_SSH_PUBLIC_KEY', '')
    if public_key:
        if not re.fullmatch(r'(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]+(?: [^\r\n]*)?', public_key):
            raise ValueError('H3_SSH_PUBLIC_KEY muss ein einzelner öffentlicher SSH-Schlüssel sein.')
        sshdir = root / 'ssh'
        sshdir.mkdir(mode=0o700, exist_ok=True)
        sshdir.chmod(0o700)
        hostkey = sshdir / 'host_ed25519'
        if not hostkey.exists():
            subprocess.run(['ssh-keygen', '-t', 'ed25519', '-N', '', '-f', str(hostkey)], check=True, capture_output=True)
        private_file(RUNTIME / 'authorized_keys', public_key+'\n')
        # The account has no usable password; key access must not be blocked by a locked account.
        subprocess.run(['usermod', '-p', '*', 'h3'], check=True)
        Path('/run/sshd').mkdir(exist_ok=True)
        private_file(RUNTIME / 'sshd_config', f'''Port 22
HostKey {hostkey}
AuthorizedKeysFile /run/h3/authorized_keys
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers h3
UsePAM no
Subsystem sftp internal-sftp
''', owner=False)
    private_file(RUNTIME / 'supervisord.conf', supervisor_config(root, bool(public_key)), owner=False)
    # Nginx -t creates its PID/temp files too: use the eventual service owner.
    subprocess.run(['nginx', '-t', '-c', str(RUNTIME / 'nginx.conf')], check=True,
                   user=UID, group=GID, extra_groups=[])


def main():
    try:
        configure()
        # Credentials are no longer needed by the managed service processes.
        for key in ('H3_AUTH_PASSWORD', 'HF_TOKEN', 'OLLAMA_API_KEY'):
            os.environ.pop(key, None)
        os.execv('/usr/bin/supervisord', ['supervisord', '-c', '/run/h3/supervisord.conf'])
    except Exception as exc:
        print('Containerstart fehlgeschlagen: ' + type(exc).__name__ +
              '. Zugangsdaten, TLS, Volume-Rechte und Startkonfiguration prüfen.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
