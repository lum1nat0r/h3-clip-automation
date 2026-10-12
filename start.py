"""Local browser UI for the H3 controller. Run with Python 3.10 or newer."""
from __future__ import annotations

import argparse
import base64
import datetime
import json
import re
import secrets
import threading
import time
import math
import urllib.request
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from engine import (DEFAULTS, JobError, Paused, Runner, atomic_json, choices,
                    input_specs, normalize_settings, preflight, request)
from library import image_catalog, image_preview, job_details, job_file, remember_images
from resources import safe_new_settings, MAX_PIXELS, MAX_SIDE
from media import MEDIA_ROUTE, playback_url, serve_video
import hardware

BASE = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.RLock()
STOP = threading.Event()
CURRENT = {"running": False, "job": None, "messages": [], "error": None, "final_url": None}
HARDWARE_CACHE = {}


def log(message):
    with LOCK:
        CURRENT["messages"].append(str(message))
        CURRENT["messages"] = CURRENT["messages"][-120:]
    print(message, flush=True)


def saved_settings():
    path = BASE / "settings.json"
    c = {**DEFAULTS, **(json.loads(path.read_text(encoding="utf-8")) if path.exists() else {})}
    c['hardware_profile'] = hardware.normalize_profile(c.get('hardware_profile') or hardware.DEFAULT_PROFILE)
    c['_verified_ram_budget_gib'] = verified_budget(c)
    try:
        c = hardware.bind_settings(c, verified_budget(c))
    except ValueError:
        pass  # Show stored inputs so an outdated manual threshold can be corrected.
    return c


def server_safety():
    path = BASE / 'server_safety.json'
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
        budget = state.get('verified_budget_gib')
        if (state.get('blocked') is False and type(budget) in (int,float)
                and math.isfinite(budget) and 8 <= budget <= 4096):
            return state
        return {**state, 'blocked': True}
    except (OSError, ValueError):
        return {'blocked': True, 'reason': 'Server-Speichergrenzen sind noch nicht geprüft. Siehe UNRAID_SPEICHERSCHUTZ.md.'}


def verified_budget(c):
    safety = server_safety()
    if not safety['blocked'] and all(str(c[k]).rstrip('/') == str(safety.get(k,'')).rstrip('/')
                                    for k in ('comfy_url','ollama_url')):
        return safety['verified_budget_gib']
    return None


def hardware_info(c, refresh=False):
    url = c['comfy_url']
    cached = HARDWARE_CACHE.get(url)
    warning = None
    if refresh or not cached or time.monotonic() - cached['time'] > 60:
        try:
            objects = {}
            for kind in ('UNETLoader','CLIPLoader','VAELoader'):
                objects.update(request(url, '/object_info/' + kind, timeout=8))
            stats = request(url, '/system_stats', timeout=8)
            cached = {'objects':objects, 'stats':stats, 'time':time.monotonic()}
            HARDWARE_CACHE[url] = cached
        except JobError as exc:
            # Do not advertise stale models as currently installed.
            cached = None
            warning = 'Server nicht erreichbar; gespeicherte Auswahl bleibt erhalten. ' + str(exc)
    result = hardware.overview(c, verified_budget(c),
                              cached['objects'] if cached else None, cached['stats'] if cached else None)
    result['warning'] = warning
    return result


def save_preferences(c):
    if CURRENT['running']:
        raise JobError('Während der Generierung sind die Einstellungen gesperrt.')
    c = normalize_settings(c)
    c = hardware.bind_settings(c, verified_budget(c))
    atomic_json(BASE / 'settings.json', c)
    return c


def job_path(name):
    if not re.fullmatch(r"[0-9]{8}_[0-9]{6}_[0-9a-f]{8}", str(name)):
        raise JobError("Ungültige Projekt-ID.")
    return BASE / "runs" / name


def work(runner):
    try:
        runner.run()
        with LOCK:
            CURRENT["final_url"] = runner.state.get("final_url")
    except Paused:
        pass  # Runner already persisted and displayed the pause message.
    except Exception as e:
        with LOCK:
            CURRENT["error"] = str(e)
    finally:
        with LOCK:
            CURRENT["running"] = False


def launch(data, resume=False):
    with LOCK:
        safety = server_safety()
        if safety['blocked']:
            raise JobError(safety.get('reason') or 'Server-Speicherschutz muss zuerst eingerichtet werden.')
        if CURRENT["running"]:
            raise JobError("Es läuft bereits ein Projekt.")
        if resume:
            path = job_path(data["job"])
            if not (path / "job.json").exists():
                raise JobError("Projekt wurde nicht gefunden.")
            runner = Runner(path, log, STOP.is_set)
            c = runner.state['settings']
        else:
            c = normalize_settings(data["settings"])
        for key in ('comfy_url', 'ollama_url'):
            if (not safety.get(key) or str(c[key]).rstrip('/') != str(safety[key]).rstrip('/')):
                raise JobError('Die Serveradresse passt nicht zum geprüften Speicherschutz. '
                               'Nach einem Serverwechsel zuerst dessen gemeinsame RAM-Grenzen prüfen.')
        if not resume:
            c = hardware.bind_settings(c, safety['verified_budget_gib'])
        elif c.get('hardware_profile'):
            # Keep this project's choices; rebind only the trusted server ceiling.
            c = hardware.bind_settings(c, safety['verified_budget_gib'])
            runner.state['settings'] = c
        if not resume:
            remember_images(BASE, c["comfy_url"], [c["image"], *c["extra_images"]])
            name = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_") + secrets.token_hex(4)
            runner = Runner(job_path(name), log, STOP.is_set)
            runner.new(c, data["master_prompt"])
            atomic_json(BASE / "settings.json", runner.state['settings'])
        STOP.clear()
        CURRENT.update(running=True, job=runner.directory.name, messages=[], error=None, final_url=None)
        threading.Thread(target=work, args=(runner,), daemon=True).start()
        return {"job": runner.directory.name, 'settings': runner.state['settings']}


def upload_image(data):
    c = normalize_settings(data["settings"])
    name = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(data["name"]).replace("\\", "/").split("/")[-1])
    if Path(name).suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        raise JobError("Bitte PNG, JPG oder WebP verwenden.")
    raw = base64.b64decode(data["base64"], validate=True)
    if not 0 < len(raw) <= 24 * 1024 * 1024:
        raise JobError("Bild muss kleiner als 24 MB sein.")
    name = secrets.token_hex(6) + "_" + name
    boundary = "h3" + secrets.token_hex(16)
    chunks = []
    for key, value in {"type": "input", "subfolder": "h3_automatik", "overwrite": "false"}.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    chunks += [f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode(),
               raw, f'\r\n--{boundary}--\r\n'.encode()]
    req = urllib.request.Request(c["comfy_url"] + "/upload/image", data=b"".join(chunks),
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=120) as response:
        result = json.load(response)
    image = "/".join(p for p in [result.get("subfolder", ""), result["name"]] if p)
    remember_images(BASE, c["comfy_url"], [image], source="uploaded")
    return {"image": image}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, data, status=200, content_type="application/json; charset=utf-8"):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8") if content_type.startswith("application/json") else data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(payload)

    def trusted_host(self):
        return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

    def do_GET(self):
        if not self.trusted_host():
            return self.send({"error": "Host nicht erlaubt."}, 403)
        route = urllib.parse.urlsplit(self.path).path
        match = MEDIA_ROUTE.fullmatch(route)
        if match:
            try:
                state = json.loads((job_path(match[1]) / 'job.json').read_text(encoding='utf-8'))
                return serve_video(self, state, TOKEN, head=self.command == 'HEAD')
            except (OSError, ValueError, KeyError, JobError):
                return self.send({'error': 'Fertiges Video nicht gefunden.'}, 404)
        if self.command == 'HEAD':
            return self.send({'error': 'Nicht gefunden.'}, 404)
        if self.path == "/":
            html = (BASE / "index.html").read_text(encoding="utf-8").replace("__SESSION_TOKEN__", TOKEN)
            return self.send(html.encode("utf-8"), content_type="text/html; charset=utf-8")
        if self.path == "/ui.js":
            return self.send((BASE / "ui.js").read_bytes(), content_type="text/javascript; charset=utf-8")
        if self.headers.get("X-H3-Token") != TOKEN:
            return self.send({"error": "Sitzung bitte neu laden."}, 403)
        if self.path == "/api/settings":
            return self.send(saved_settings())
        if self.path == "/api/status":
            with LOCK:
                result = json.loads(json.dumps(CURRENT))
            result['server_safety'] = server_safety()
            if result["job"]:
                path = job_path(result["job"]) / "job.json"
                if path.exists():
                    s = json.loads(path.read_text(encoding="utf-8"))
                    result.update(status=s["status"], done=len(s["takes"]), total=len(s["slots"]),
                                  final_url=playback_url(s, TOKEN))
            return self.send(result)
        if self.path == "/api/jobs":
            jobs = []
            for path in sorted((BASE / "runs").glob("*/job.json"), reverse=True)[:100]:
                try:
                    s = json.loads(path.read_text(encoding="utf-8"))
                    jobs.append({"id": s["id"], "status": s["status"], "master_prompt": s["master_prompt"],
                                 "done": len(s["takes"]), "total": len(s["slots"]), "final_url": playback_url(s, TOKEN),
                                 'resume_blocked': bool(s.get('safety_hold')) or
                                 s['settings']['width'] * s['settings']['height'] > MAX_PIXELS or
                                 max(s['settings']['width'], s['settings']['height']) > MAX_SIDE})
                except (ValueError, KeyError, JobError):
                    continue
            return self.send(jobs)
        return self.send({"error": "Nicht gefunden."}, 404)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if not self.trusted_host() or self.headers.get("X-H3-Token") != TOKEN:
            return self.send({"error": "Sitzung bitte neu laden."}, 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 34 * 1024 * 1024:
                raise JobError("Anfrage zu groß oder leer.")
            data = json.loads(self.rfile.read(length))
            if self.path == "/api/check":
                c = hardware.bind_settings(normalize_settings(data['settings']), verified_budget(data['settings']))
                objects = preflight(c, require_image=False)
                return self.send({"message": "Verbindungen, H3-Modelle und benötigte Nodes sind bereit.",
                                  "images": choices(input_specs(objects["LoadImage"])["image"]), "settings": c})
            if self.path == "/api/save":
                with LOCK:
                    c = save_preferences(data['settings'])
                remember_images(BASE, c["comfy_url"], [c["image"], *c["extra_images"]])
                return self.send({"message": "Einstellungen gespeichert.", 'settings':c})
            if self.path == '/api/hardware':
                return self.send(hardware_info(normalize_settings(data['settings']), bool(data.get('refresh'))))
            if self.path == '/api/hardware-apply':
                with LOCK:
                    if CURRENT['running']:
                        raise JobError('Während der Generierung sind die Einstellungen gesperrt.')
                    info = hardware_info(normalize_settings(data['settings']), refresh=True)
                    preset = next((p for p in info['presets'] if p['id']==data.get('preset')), None)
                    if not preset or not preset['applicable']:
                        raise JobError('Dieses Modellprofil ist nicht verfügbar oder passt nicht zum Speicherbudget.')
                    c = save_preferences({**info['settings'], **preset['models']})
                    return self.send({'settings':c, 'message':'Modellprofil übernommen und gespeichert.'})
            if self.path == "/api/images":
                return self.send(image_catalog(BASE, normalize_settings(data["settings"])))
            if self.path == "/api/image-preview":
                c = normalize_settings({"comfy_url": data["comfy_url"]})
                raw, content_type = image_preview(c["comfy_url"], data["image"])
                return self.send(raw, content_type=content_type)
            if self.path == "/api/job-details":
                return self.send(job_details(job_path(data["job"])))
            if self.path == "/api/job-file":
                return self.send(job_file(job_path(data["job"]), data["file"]))
            if self.path == '/api/job-copy':
                s = json.loads((job_path(data['job']) / 'job.json').read_text(encoding='utf-8'))
                c = hardware.bind_settings(normalize_settings(s['settings']), verified_budget(s['settings']))
                return self.send({'settings': safe_new_settings(c),
                                  'master_prompt': s['master_prompt']})
            if self.path == "/api/upload":
                return self.send(upload_image(data))
            if self.path == "/api/start":
                return self.send(launch(data))
            if self.path == "/api/resume":
                return self.send(launch(data, resume=True))
            if self.path == "/api/pause":
                STOP.set()
                return self.send({"message": "Stopp vorgemerkt. Der laufende ComfyUI-Take darf fertig werden."})
            if self.path == "/api/shutdown":
                with LOCK:
                    if CURRENT["running"]:
                        raise JobError("Bitte zuerst Anhalten wählen und warten, bis die Steuerung angehalten ist.")
                self.send({"message": "Lokale Steuerung beendet. Mit START_AUTOMATIK.cmd wieder öffnen."})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            return self.send({"error": "Nicht gefunden."}, 404)
        except Exception as e:
            return self.send({"error": str(e)}, 400)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="H3 NVFP4 Automatik – lokale Startoberfläche")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        raise SystemExit(f"Start fehlgeschlagen: {e}. Anderen Port wählen: python start.py --port 8766")
    url = f"http://127.0.0.1:{server.server_port}/"
    print("H3 Automatik: " + url + "\nDieses Fenster während der Generierung geöffnet lassen.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nLokale Steuerung beendet. Ein laufender Take läuft auf ComfyUI weiter; später Fortsetzen wählen.")
    finally:
        server.server_close()
