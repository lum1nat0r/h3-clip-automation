"""Bounded, job-scoped streaming for the local video player.

ComfyUI rejects cross-site browser requests. Keep that protection enabled;
the controller reads only a completed job's saved output on the user's behalf.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

from engine import JobError, relative_path, view_url

MEDIA_ROUTE = re.compile(r'/media/([0-9]{8}_[0-9]{6}_[0-9a-f]{8})/final\.mp4')
STREAMS = threading.BoundedSemaphore(4)
CHUNK_SIZE = 64 * 1024


def output_url(state):
    if state.get('status') != 'complete' or not state.get('final_path'):
        raise JobError('Dieses Projekt hat noch kein fertiges Video.')
    path = relative_path(state['final_path'])
    folder = relative_path(state['folder'])
    if not path.startswith(folder + '/') or not path.lower().endswith('.mp4'):
        raise JobError('Ungültiger Videopfad im Projekt.')
    return view_url(state['settings'], path)


def media_key(state, token):
    data = state['id'] + '\n' + output_url(state)
    return hmac.new(token.encode(), data.encode(), hashlib.sha256).hexdigest()


def playback_url(state, token):
    if state.get('status') != 'complete' or not state.get('final_path'):
        return None
    return f"/media/{state['id']}/final.mp4?key={media_key(state, token)}"


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def serve_video(handler, state, token, head=False):
    """Forward bytes, never browser cookies/origins or a user-supplied URL."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    keys = query.get('key', [])
    if (handler.headers.get('Sec-Fetch-Site') == 'cross-site' or len(keys) != 1
            or not re.fullmatch(r'[0-9a-f]{64}', keys[0])
            or not hmac.compare_digest(keys[0], media_key(state, token))):
        return handler.send({'error': 'Videolink abgelaufen. Oberfläche bitte neu laden.'}, 403)
    headers = {'Accept': 'video/mp4', 'Accept-Encoding': 'identity'}
    byte_range = handler.headers.get('Range')
    if byte_range:
        if not re.fullmatch(r'bytes=(?:[0-9]+-[0-9]*|-[0-9]+)', byte_range):
            return handler.send({'error': 'Ungültiger Videoabschnitt.'}, 400)
        headers['Range'] = byte_range
    if not STREAMS.acquire(blocking=False):
        return handler.send({'error': 'Mehrere Videos werden geladen. Bitte kurz warten.'}, 503)
    started = False
    try:
        request = urllib.request.Request(output_url(state), method='HEAD' if head else 'GET', headers=headers)
        try:
            response = urllib.request.build_opener(NoRedirects()).open(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            if response.status == 416:
                handler.send_response(416)
                if re.fullmatch(r'bytes \*/[0-9]+', response.headers.get('Content-Range', '')):
                    handler.send_header('Content-Range', response.headers['Content-Range'])
                handler.send_header('Content-Length', '0')
                handler.end_headers()
                return
            if response.status not in (200, 206) or response.headers.get_content_type() != 'video/mp4':
                return handler.send({'error': f'ComfyUI liefert kein MP4 (HTTP {response.status}).'}, 502)
            handler.send_response(response.status)
            handler.send_header('Content-Type', 'video/mp4')
            handler.send_header('Content-Disposition', 'inline; filename="final.mp4"')
            handler.send_header('Cache-Control', 'private, no-store')
            handler.send_header('Referrer-Policy', 'no-referrer')
            handler.send_header('X-Content-Type-Options', 'nosniff')
            handler.send_header('Cross-Origin-Resource-Policy', 'same-origin')
            for name in ('Content-Length', 'Content-Range', 'Accept-Ranges'):
                if response.headers.get(name):
                    handler.send_header(name, response.headers[name])
            handler.end_headers()
            started = True
            if not head:
                while block := response.read(CHUNK_SIZE):
                    handler.wfile.write(block)
    except (OSError, TimeoutError):
        if not started:
            handler.send({'error': 'ComfyUI ist für die Videowiedergabe nicht erreichbar.'}, 502)
    finally:
        STREAMS.release()
