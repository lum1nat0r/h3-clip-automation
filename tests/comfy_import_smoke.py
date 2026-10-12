"""Check real installed ComfyUI nodes on CPU; no weights, downloads or renders."""
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, '/opt/h3')
from engine import DEFAULTS, build_graph, schedule, validate_graph
from staging import split_graph
from hardware import ROLES, SOURCES


def main():
    # Empty filename fixtures exercise real model discovery; never submit/load them.
    for role in ROLES:
        source = SOURCES[DEFAULTS[role]]
        path = Path('/opt/comfy/models') / source['target'] / source['filename']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    process = subprocess.Popen([sys.executable, '/opt/comfy/main.py', '--cpu', '--listen', '127.0.0.1',
                                '--port', '8188', '--disable-auto-launch'], cwd='/opt/comfy')
    try:
        for _ in range(120):
            if process.poll() is not None:
                raise RuntimeError('ComfyUI failed to start on CPU')
            try:
                with urllib.request.urlopen('http://127.0.0.1:8188/object_info', timeout=2) as response:
                    objects = json.load(response)
                break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError('ComfyUI startup timeout')
        for previous in (None, 'h3_automatik/check/take.mp4'):
            graph = build_graph(DEFAULTS, 'Test', schedule(30)[1 if previous else 0], 'h3_automatik/check', 1, previous)
            validate_graph(graph, objects, skip_file_choices=True)
            for stage in split_graph(graph, 'a'*32):
                validate_graph(stage, objects, skip_file_choices=True)
        with urllib.request.urlopen('http://127.0.0.1:8188/inspire/cache/list', timeout=2) as response:
            assert b'String Key Caches' in response.read()
        print('PASS: actual ComfyUI, Timeline and Inspire imports; start/extension/staged graph schemas')
    finally:
        process.terminate()
        process.wait(30)


if __name__ == '__main__':
    main()
