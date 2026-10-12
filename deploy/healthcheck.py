"""Readiness without loading weights or submitting GPU jobs."""
import json
import sys
import urllib.request


def check():
    for url in ('http://127.0.0.1:8765/healthz', 'http://127.0.0.1:11434/api/tags',
                'http://127.0.0.1:8188/object_info'):
        with urllib.request.urlopen(url, timeout=4) as response:
            result = json.load(response)
        if url.endswith('object_info'):
            for node in ('MiniMaxH3ReferenceToVideo', 'H3VramHeadroom', 'CacheBackendData //Inspire', 'RetrieveBackendData //Inspire'):
                if node not in result:
                    raise ValueError('Required node missing: ' + node)


if __name__ == '__main__':
    try:
        check()
    except Exception:
        sys.exit(1)
