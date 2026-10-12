"""Readiness without loading weights or submitting GPU jobs."""
import json
import os
import ssl
import sys
import urllib.error
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
    # Also require both proxies to be listening and enforcing authentication.
    scheme = 'http' if os.environ.get('H3_TLS_MODE') == 'proxy' else 'https'
    context = ssl._create_unverified_context()  # Local readiness; supports the self-signed mode.
    for port in (8080, 8081):
        try:
            with urllib.request.urlopen(f'{scheme}://127.0.0.1:{port}/', timeout=2, context=context):
                raise ValueError('Proxy did not require authentication')
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code != 401:
                raise ValueError('Proxy is not ready') from None


if __name__ == '__main__':
    try:
        check()
    except Exception:
        sys.exit(1)
