"""Build/start an isolated Compose project, then enter the model assistant."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def command(args, capture=False):
    return subprocess.run(args, cwd=ROOT, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', default='h3-portable')
    parser.add_argument('--pull', action='store_true', help='Use H3_IMAGE from an existing registry instead of building')
    parser.add_argument('--no-models', action='store_true')
    parser.add_argument('--override', action='append', type=Path, default=[],
                        help='Additional local Compose file, e.g. TLS certificate mounts')
    args = parser.parse_args()
    import re
    if not re.fullmatch('[a-z0-9][a-z0-9_-]*', args.project):
        parser.error('Ungültiger Compose-Projektname.')
    if not shutil.which('docker'):
        parser.error('Docker Engine mit Compose und NVIDIA Container Toolkit wird benötigt.')
    if not (ROOT / '.env').is_file() and not all(os.environ.get(k) for k in ('H3_AUTH_USER', 'H3_AUTH_PASSWORD')):
        parser.error('.env.example nach .env kopieren und Zugangsdaten setzen.')
    docker = ['docker', 'compose', '-p', args.project, '-f', str(ROOT / 'compose.yaml')]
    for override in args.override:
        if not override.is_file():
            parser.error('Compose-Ergänzung nicht gefunden: ' + str(override))
        docker.extend(['-f', str(override.resolve())])
    command(['docker', 'info', '--format', '{{.ServerVersion}}'])
    command([*docker, 'config', '--quiet'])
    # Do not print rendered configuration: it contains credentials.
    configuration = json.loads(command([*docker, 'config', '--format', 'json'], capture=True).stdout)
    service = configuration['services']['h3']
    env = service['environment']
    password = env.get('H3_AUTH_PASSWORD', '')
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', env.get('H3_AUTH_USER', ''))
            or len(password) < 16 or any(c in password for c in '\r\n\0')):
        parser.error('Gültigen H3_AUTH_USER und H3_AUTH_PASSWORD (mindestens 16 Zeichen, eine Zeile) setzen.')
    if env.get('H3_TLS_MODE') not in ('selfsigned', 'certificate', 'proxy'):
        parser.error('H3_TLS_MODE muss selfsigned, certificate oder proxy sein.')
    if env['H3_TLS_MODE'] == 'certificate' and not all(env.get(k) for k in ('H3_TLS_CERT', 'H3_TLS_KEY')):
        parser.error('Zertifikatspfade und Mounts über --override konfigurieren; siehe DEPLOYMENT.md.')
    if int(service['mem_limit']) < 8 * 1024**3:
        parser.error('Mindestens 8 GiB Container-RAM konfigurieren; H3-Profile benötigen mehr.')
    host_memory = int(command(['docker', 'info', '--format', '{{.MemTotal}}'], capture=True).stdout.strip())
    if int(service['mem_limit']) > host_memory - 2 * 1024**3:
        parser.error('RAM-Limit lässt weniger als 2 GiB für den Docker-Host; Limit reduzieren. Andere Dienste zusätzlich berücksichtigen.')
    if env['H3_TLS_MODE'] == 'proxy' and any(p.get('host_ip') != '127.0.0.1' for p in service['ports']):
        parser.error('Öffentlicher Compose-Zugriff benötigt TLS. proxy nur hinter einem eigenen HTTPS-Proxy verwenden.')
    existing = command([*docker, 'ps', '-q'], capture=True).stdout.strip()
    if existing:
        parser.error('Dieses Compose-Projekt läuft bereits. Modellassistent mit docker compose exec h3 h3-models öffnen; Updates bewusst separat durchführen.')
    # Read-only GPU/driver probe on the selected device, without loading weights.
    gpu = service['deploy']['resources']['reservations']['devices'][0]['device_ids'][0]
    base_image = (ROOT/'Dockerfile').read_text('utf-8').splitlines()[0].split()[1]
    probe = ['docker', 'run', '--rm', '--network', 'none', '--memory', '256m', '--cpus', '0.5',
             '--gpus', 'device='+str(gpu), '--entrypoint']
    command([*probe, 'nvidia-smi', base_image, '--query-gpu=name,memory.total,driver_version', '--format=csv,noheader'])
    disk = command([*probe, 'df', base_image, '-PB1', '/'], capture=True).stdout.splitlines()[-1].split()
    if int(disk[3]) < 20 * 1024**3:
        parser.error('Mindestens 20 GiB freier Platz für Image-Build und Laufzeit benötigt; Modelle zusätzlich.')
    print('Erstelle neuen Stack ' + args.project + '. Vorhandene Compose-Projekte werden nicht geändert.')
    command([*docker, 'pull'] if args.pull else [*docker, 'build'])
    command([*docker, 'up', '-d', '--wait', '--wait-timeout', '300'])
    print('Stack läuft. Ports und TLS-Fingerabdruck: docker compose logs h3')
    if not args.no_models:
        command([*docker, 'exec', 'h3', 'h3-models'])


if __name__ == '__main__':
    try:
        main()
    except (subprocess.CalledProcessError, OSError):
        raise SystemExit('Setup fehlgeschlagen. Docker-/Dienstmeldungen prüfen; bestehende Daten wurden nicht gelöscht.')
