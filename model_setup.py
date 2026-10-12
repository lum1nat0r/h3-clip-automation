"""Interactive, resumable model installation for the bundled stack."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import urllib.request

import container_memory
import hardware
from engine import DEFAULTS, atomic_json, normalize_settings, preflight, request, ensure_idle
from runtime_lock import exclusive

GIB = 1024 ** 3


def verified(path, source):
    if not path.is_file() or path.stat().st_size != source['size']:
        return False
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024**2), b''):
            digest.update(chunk)
    return digest.hexdigest() == source['sha256']


def download(source, root, fetch=None):
    target = root / 'comfy/models' / source['target'] / source['filename']
    if verified(target, source):
        print('Geprüft vorhanden: ' + source['filename'])
        return target
    if fetch is None:
        from huggingface_hub import hf_hub_download
        fetch = hf_hub_download
    try:
        staged = Path(fetch(repo_id=source['repo'], filename=source['path'], revision=source['revision'],
                            local_dir=str(root / 'downloads' / source['repo'].split('/')[0]),
                            token=os.environ.get('HF_TOKEN') or False))
    except Exception:
        # HTTP exceptions can contain signed URLs or request headers.
        raise RuntimeError('Download fehlgeschlagen: ' + source['filename'] +
                           '. Netzwerk, Speicher und gegebenenfalls HF_TOKEN/Modellfreigabe prüfen; erneut starten.') from None
    if not verified(staged, source):
        staged.unlink(missing_ok=True)
        raise RuntimeError('Prüfsumme stimmt nicht: ' + source['filename'] + '. Erneut herunterladen.')
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged, target)
    return target


def model_name(value):
    value = value.strip()
    if not re.fullmatch(r'(?:[A-Za-z0-9][A-Za-z0-9_.-]*/)?[A-Za-z0-9][A-Za-z0-9_.-]*:[A-Za-z0-9_.-]+', value):
        raise ValueError('Exakten öffentlichen Ollama-Namen einschließlich :Tag angeben.')
    if 'cloud' in value.lower():
        raise ValueError('Dieser Assistent installiert ausschließlich lokale Ollama-Modelle.')
    return value


def ollama_manifest(name, opener=urllib.request.urlopen):
    model, tag = model_name(name).rsplit(':', 1)
    if '/' not in model:
        model = 'library/' + model
    req = urllib.request.Request(f'https://registry.ollama.ai/v2/{model}/manifests/{tag}',
                                 headers={'Accept': 'application/vnd.docker.distribution.manifest.v2+json'})
    try:
        with opener(req, timeout=30) as response:
            value = json.load(response)
        layers = [value['config'], *value['layers']]
        if not layers or any(type(x['size']) is not int or x['size'] < 0 or
                             not re.fullmatch(r'sha256:[0-9a-f]{64}', x['digest']) for x in layers):
            raise ValueError()
        return layers
    except Exception:
        raise RuntimeError('Öffentliches Ollama-Modell nicht prüfbar. Namen, Tag und Netzwerk prüfen.') from None


def require_space(root, amount):
    free = shutil.disk_usage(root).free
    if free < amount:
        raise RuntimeError(f'Zu wenig freier Speicher: {free/GIB:.1f} GiB; benötigt {amount/GIB:.1f} GiB.')


def pull(name, root, layers, opener=urllib.request.urlopen):
    missing = sum(layer['size'] for layer in layers
                  if not (root / 'ollama/models/blobs' / layer['digest'].replace(':', '-')).is_file())
    require_space(root, missing + 2 * GIB)
    req = urllib.request.Request(DEFAULTS['ollama_url'] + '/api/pull',
        data=json.dumps({'model': name, 'stream': True}).encode(), headers={'Content-Type': 'application/json'})
    try:
        with opener(req, timeout=1800) as response:
            done = False
            for line in response:
                event = json.loads(line)
                if event.get('error'):
                    raise RuntimeError()
                if event.get('total'):
                    print(f"Ollama: {event.get('completed', 0)/GIB:.2f} / {event['total']/GIB:.2f} GiB", end='\r', flush=True)
                done = event.get('status') == 'success'
            if not done:
                raise RuntimeError()
    except Exception:
        raise RuntimeError('Ollama-Download unterbrochen. Netzwerk und freien Speicher prüfen; erneut starten.') from None
    print('\nOllama-Download abgeschlossen.')


def apply_selection(root, config, call=request):
    # Validate real node schemas, filenames and tags before touching preferences.
    detail = call(config['ollama_url'], '/api/show', {'model': config['ollama_model']})
    if detail.get('remote_host') or detail.get('remote_model'):
        raise ValueError('Das gewählte Ollama-Modell ist ein Cloud-Modell.')
    preflight(config, call=call, require_image=False)
    atomic_json(root / 'app/settings.json', config)


def install(root, profile_id=None, name=None, assume_yes=False, call=request, fetch=None):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    app = root / 'app'
    with exclusive(app):
        saved = app / 'settings.json'
        config = normalize_settings(json.loads(saved.read_text('utf-8')) if saved.exists() else {})
        ensure_idle(config, call=call)
        if call(config['ollama_url'], '/api/ps').get('models'):
            raise RuntimeError('Ollama ist noch belegt. Erst laufende Arbeit beenden und Modelle entladen.')
        memory = container_memory.snapshot()
        stats = call(config['comfy_url'], '/system_stats')
        gpu = stats['devices'][0]
        if gpu.get('type') != 'cuda':
            raise RuntimeError('Keine CUDA-GPU verfügbar.')
        profile = dict(hardware.DEFAULT_PROFILE, ram_available_gib=memory['budget_gib'],
                       vram_gib=math.ceil(gpu['vram_total'] / GIB), gpu_arch='auto')
        config = hardware.bind_settings(dict(config, hardware_profile=profile), memory['budget_gib'])
        print(f"GPU: {gpu['name']} / RAM-Budget: {memory['budget_gib']:.1f} GiB")
        if memory['warning']:
            print(memory['warning'])
        eligible = {}
        for preset in hardware.PRESETS:
            models = {role: hardware.MODELS[role][index][0] for role, index in zip(hardware.ROLES, preset['indexes'])}
            issues = hardware.selection_issues(dict(config, **models), memory['budget_gib'], stats)
            if not issues:
                eligible[preset['id']] = models
                print(f"  {preset['id']}: {preset['title']}")
        if not eligible:
            raise RuntimeError('Kein H3-Profil passt zu dieser GPU und dem RAM-Budget.')
        recommended = list(eligible)[-1]
        selected = profile_id or input(f'H3-Profil [{recommended}]: ').strip() or recommended
        if selected not in eligible:
            raise ValueError('Profil ist für diese Hardware nicht verfügbar.')
        previous = config['ollama_model'] if config['ollama_model'] != 'YOUR_OLLAMA_MODEL' else ''
        name = model_name(name or input(f'Ollama-Modell mit :Tag [{previous}]: ').strip() or previous)
        layers = ollama_manifest(name)
        sources = [hardware.SOURCES[f] for f in eligible[selected].values()]
        pending = []
        for source in sources:
            target = root / 'comfy/models' / source['target'] / source['filename']
            exists = verified(target, source)
            print(f"{source['repo']} / {source['path']} ({source['size']/GIB:.2f} GiB)" + (' · geprüft vorhanden' if exists else ''))
            if not exists:
                pending.append(source)
        ollama_size = sum(x['size'] for x in layers if not
                          (root / 'ollama/models/blobs' / x['digest'].replace(':', '-')).is_file())
        needed = sum(x['size'] for x in pending) + ollama_size + 2 * GIB
        print(f'Ollama: {name}; zusätzlicher Speicher inklusive Reserve: {needed/GIB:.1f} GiB')
        require_space(root, needed)
        if not assume_yes and input('Modelle herunterladen und Auswahl speichern? [j/N] ').strip().lower() not in ('j', 'ja'):
            print('Abgebrochen. Einstellungen unverändert.')
            return
        for source in pending:
            ensure_idle(config, call=call)
            require_space(root, source['size'] + 2 * GIB)
            download(source, root, fetch=fetch)
        pull(name, root, layers)
        ensure_idle(config, call=call)
        config.update(eligible[selected], ollama_model=name)
        apply_selection(root, config, call=call)
        print('Modelle geprüft und gespeichert. Die Anwendung ist bereit für ein neues Projekt.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=os.environ.get('H3_ROOT', '/workspace/h3'))
    parser.add_argument('--profile', choices=[p['id'] for p in hardware.PRESETS])
    parser.add_argument('--ollama-model')
    parser.add_argument('--yes', action='store_true')
    args = parser.parse_args()
    if not container_memory.enabled():
        parser.error('h3-models muss im neuen Stack ausgeführt werden.')
    if not sys.stdin.isatty() and not (args.yes and args.profile and args.ollama_model):
        parser.error('Interaktives Terminal oder --yes --profile ... --ollama-model ... erforderlich.')
    try:
        install(args.root, args.profile, args.ollama_model, args.yes)
    except KeyboardInterrupt:
        print('\nAbgebrochen. Downloads können fortgesetzt werden.', file=sys.stderr)
        return 130
    except Exception as exc:
        # Only our controlled messages are shown; third party tracebacks may expose tokens.
        message = str(exc)
        for secret in (os.environ.get('HF_TOKEN'), os.environ.get('H3_AUTH_PASSWORD')):
            if secret:
                message = message.replace(secret, '[redacted]')
        print('Installation fehlgeschlagen: ' + message, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
