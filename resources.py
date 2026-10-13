"""Conservative admission checks, not an OS/container memory limit.

Uses only ComfyUI's built-in /system_stats, /free and targeted /interrupt.
Thresholds are policy margins, not a measured H3 peak-memory prediction.
"""
from __future__ import annotations

import copy
import datetime
import json
import math
import time
import threading
from contextlib import contextmanager
from hardware import resolve_profile
import container_memory

GIB = 1024 ** 3
MAX_PIXELS = 736 * 416
MAX_SIDE = 736
START_RAM_GIB = 24
STOP_RAM_GIB = 8
CONTAINER_BUDGET_GIB = 24
CONTAINER_START_FREE_GIB = 18
CONTAINER_STOP_FREE_GIB = 4
START_VRAM_GIB = 12
TIMEOUT = 8


class SafetyError(RuntimeError):
    pass


def ram_policy(values, settings=None):
    """Expect cgroup-aware ComfyUI RAM reporting for the verified budget.

    Requesting 24 GiB *free* inside that budget can never pass because the
    server itself occupies memory. Keep host-only reporting conservative.
    These thresholds supplement the separately verified hard kernel limit.
    """
    total = values['ram_total_gib']
    if container_memory.enabled():
        p, _ = resolve_profile(settings['hardware_profile'], total)
        return p['ram_start_gib'], p['ram_stop_gib'], values['ram_source']
    if settings and settings.get('hardware_profile'):
        limit = settings.get('_verified_ram_budget_gib', CONTAINER_BUDGET_GIB)
        p, _ = resolve_profile(settings['hardware_profile'], limit)
        try:
            version = tuple(int(x) for x in values.get('comfyui_version', '').split('.')[:3])
        except ValueError:
            version = ()
        if abs(total - limit) >= .001 or len(version)!=3 or version < (0,37,0):
            raise SafetyError('RAM-Messwert und geprüfte Containergrenze stimmen nicht überein. Servergrenzen erneut prüfen.')
        return p['ram_start_gib'], p['ram_stop_gib'], f'{limit:g}-GiB-Containerbudget'
    if abs(total - CONTAINER_BUDGET_GIB) < .001:
        try:
            version = tuple(int(p) for p in values.get('comfyui_version', '').split('.')[:3])
        except ValueError:
            version = ()
        if len(version) != 3 or version < (0, 37, 0):
            raise SafetyError('Die Bedeutung des 24-GiB-RAM-Messwerts ist für diese ComfyUI-Version nicht geprüft.')
        return CONTAINER_START_FREE_GIB, CONTAINER_STOP_FREE_GIB, '24-GiB-Containerbudget'
    if total < 28:
        raise SafetyError('Unerwartetes RAM-Budget; die Server-Speichergrenzen müssen erneut geprüft werden.')
    return START_RAM_GIB, max(STOP_RAM_GIB, total * .12), 'gemeldeter RAM'


def fit_resolution(width, height):
    scale = min(1, math.sqrt(MAX_PIXELS / (width * height)), MAX_SIDE / max(width, height))
    return max(128, int(width * scale) // 32 * 32), max(128, int(height * scale) // 32 * 32)


def safe_new_settings(settings):
    c = copy.deepcopy(settings)
    c['width'], c['height'] = fit_resolution(c['width'], c['height'])
    if c.get('hardware_profile'):
        c['hardware_profile'], _ = resolve_profile(c['hardware_profile'], c.get('_ram_budget_gib', c.get('_verified_ram_budget_gib', 24)))
        c['headroom_gb'] = c['hardware_profile']['headroom_gib']
    else:
        c['headroom_gb'] = min(4.0, max(3.0, c['headroom_gb']))
    c['ollama_num_ctx'] = min(c['ollama_num_ctx'], 8192)
    c['ollama_num_predict'] = min(c['ollama_num_predict'], 4096)
    return c


def validate_profile(c, slots):
    if (c['width'] * c['height'] > MAX_PIXELS or max(c['width'], c['height']) > MAX_SIDE):
        w, h = fit_resolution(c['width'], c['height'])
        raise SafetyError(f"Speicherschutz: Dieses ältere Projekt verwendet {c['width']} × {c['height']}. "
                          f"Bitte als neues Projekt mit {w} × {h} starten. Bereits erzeugte Takes und "
                          "die offene Prompt-ID bleiben erhalten; ihre Auflösung wird nicht nachträglich geändert.")
    if any(s['raw_frames'] > 141 for s in slots):
        raise SafetyError('Speicherschutz: Höchstens 141 Rohframes pro Take erlaubt.')
    if c.get('extra_images') or c.get('reference_audio'):
        raise SafetyError('Das Speicherprofil erlaubt vorerst ein Startbild ohne zusätzliche Bild- oder Audioreferenzen. '
                          'Diese benötigen eine gesonderte Speicherprüfung.')


class MemoryGuard:
    def __init__(self, c, directory, call, log, pause_point=lambda: None, sleep=None):
        self.c, self.directory, self.call, self.log = c, directory, call, log
        self.pause_point, self.sleep = pause_point, sleep or time.sleep
        self.latest = None
        self.failure = None

    def record(self, phase, **data):
        self.directory.mkdir(parents=True, exist_ok=True)
        row = {'time': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'phase': phase, **data}
        with (self.directory / 'resources.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')

    def measure(self, phase):
        try:
            stats = self.call(self.c['comfy_url'], '/system_stats', timeout=TIMEOUT)
            system = stats['system']
            device = stats['devices'][0]  # ComfyUI reports its primary GPU first.
            if device['type'] != 'cuda':
                raise ValueError('Das Profil benötigt eine CUDA-GPU.')
            values = {k: system[k] for k in ('ram_total', 'ram_free')}
            values.update({k: device[k] for k in ('vram_total', 'vram_free')})
            for k, v in values.items():
                if type(v) not in (int, float) or not math.isfinite(v) or v < 0:
                    raise ValueError('Ungültiger Messwert: ' + k)
            for prefix in ('ram', 'vram'):
                if values[prefix + '_total'] <= 0 or values[prefix + '_free'] > values[prefix + '_total']:
                    raise ValueError('Unplausible Speicherwerte: ' + prefix)
            if values['vram_total'] < 15 * GIB:
                raise ValueError('Das NVFP4-Profil benötigt eine GPU der 16-GB-Klasse.')
            values = {k + '_gib': round(v / GIB, 3) for k, v in values.items()}
            values.update(gpu=device['name'], comfyui_version=system.get('comfyui_version', ''))
            if container_memory.enabled():
                snapshot = container_memory.snapshot()
                expected = self.c.get('_ram_budget_gib')
                if expected is None or abs(expected - snapshot['budget_gib']) > .001:
                    raise ValueError('RAM-Budget hat sich geändert. Projekt vor Fortsetzung erneut prüfen.')
                values.update(ram_total_gib=snapshot['ram_total_gib'], ram_free_gib=snapshot['ram_free_gib'],
                              ram_source=snapshot['source'] + '/' + snapshot['mode'],
                              hard_limit_verified=snapshot['hard_limit_verified'])
            start_ram, stop_ram, scope = ram_policy(values, self.c)
            vram_start = self.c['hardware_profile']['vram_start_gib'] if self.c.get('hardware_profile') else START_VRAM_GIB
            values.update(ram_scope=scope, ram_start_min_gib=start_ram, ram_stop_min_gib=stop_ram,
                          vram_start_min_gib=vram_start)
            self.latest = values
            self.record(phase, **values)
            return values
        except Exception as exc:
            self.record(phase, error=str(exc))
            raise SafetyError('Speicherschutz: Keine verlässlichen RAM-/VRAM-Messwerte. '
                              'Kein weiterer Auftrag wird gestartet. ' + str(exc)) from exc

    def check(self, v, admission=False):
        start_ram, stop_ram, scope = ram_policy(v, self.c)
        ram = start_ram if admission else stop_ram
        if v['ram_free_gib'] < ram:
            raise SafetyError(f"Speicherschutz: Nur {v['ram_free_gib']:.1f} GiB verfügbar ({scope}); "
                              f"benötigte freie Reserve {ram:.1f} GiB. Ablauf gestoppt.")
        vram_start = self.c['hardware_profile']['vram_start_gib'] if self.c.get('hardware_profile') else START_VRAM_GIB
        if admission and v['vram_free_gib'] < vram_start:
            raise SafetyError(f"Speicherschutz: Nur {v['vram_free_gib']:.1f} GiB VRAM frei; "
                              f"vor dem Start sind {vram_start:g} GiB erforderlich.")

    def idle(self):
        q = self.call(self.c['comfy_url'], '/queue', timeout=TIMEOUT)
        if not isinstance(q.get('queue_running'), list) or not isinstance(q.get('queue_pending'), list):
            raise SafetyError('Warteschlange nicht zuverlässig lesbar; kein Start.')
        if q['queue_running'] or q['queue_pending']:
            raise SafetyError('ComfyUI verarbeitet bereits einen Auftrag. Kein weiterer Start und keine Speicherfreigabe.')

    def ollama_idle(self):
        try:
            result = self.call(self.c['ollama_url'], '/api/ps', timeout=TIMEOUT)
            loaded = result['models']
            if not isinstance(loaded, list):
                raise ValueError('Modellliste fehlt')
        except Exception as exc:
            raise SafetyError('Ollama-Speicherbelegung nicht prüfbar; kein Start. ' + str(exc)) from exc
        if loaded:
            raise SafetyError('In Ollama ist noch ein Modell geladen. Kein gleichzeitiger H3-Start; '
                              'laufende Ollama-Aufträge beenden und Modelle entladen.')

    def prepare(self, phase):
        if self.failure:
            raise self.failure
        self.pause_point()
        self.idle()
        self.ollama_idle()
        self.log('Speicherschutz: Modelle und Zwischenergebnisse freigeben …')
        self.call(self.c['comfy_url'], '/free', {'unload_models': True, 'free_memory': True}, timeout=TIMEOUT)
        # /free only sets worker flags. An HTTP 200 does not confirm cleanup.
        # Wait for two healthy measurements separated in time; fail closed.
        healthy = 0
        last_error = None
        for _ in range(15):
            self.pause_point()
            self.sleep(2)
            self.idle()
            v = self.measure(phase)
            try:
                self.check(v, admission=True)
                healthy += 1
                if healthy >= 2:
                    self.ollama_idle()
                    self.log(f"Speicher bereit: {v['ram_free_gib']:.1f} GiB frei ({v['ram_scope']}) / "
                             f"{v['vram_free_gib']:.1f} GiB VRAM frei.")
                    return
            except SafetyError as exc:
                healthy, last_error = 0, exc
        raise last_error or SafetyError('Speicherfreigabe nicht bestätigt; kein Start.')

    def running(self, phase='take_running'):
        # High VRAM occupancy during sampling is normal. Never interpret it
        # alone as OOM; RAM pressure or lost telemetry stops the chain.
        self.check(self.measure(phase))

    @contextmanager
    def observe(self, phase):
        """Log pressure during long HTTP calls, then prevent subsequent work.

        Ollama and the timeline exporter expose no per-request cancel API.
        This observer cannot enforce a hard memory ceiling on those processes.
        """
        done = threading.Event()
        def watch():
            while not done.wait(5):
                try:
                    v = self.measure(phase)
                    if done.is_set():
                        return
                    self.check(v)
                except SafetyError as exc:
                    if not done.is_set():
                        self.failure = exc
                        self.log(str(exc) + ' Der aktuelle Serveraufruf ist nicht per API abbrechbar; '
                                 'es folgen keine weiteren Aufträge.')
                    return
        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        try:
            yield
            if self.failure:
                raise self.failure
        finally:
            done.set()
            thread.join(timeout=.2)

    def interrupt(self, prompt_id):
        if not prompt_id:
            self.record('interrupt', result='unknown_prompt_id_not_interrupted')
            return
        # Targeted interruption exists in verified ComfyUI 0.37.0. Do not
        # send it to unknown/older servers that might ignore the parameter.
        version = (self.latest or {}).get('comfyui_version', '')
        try:
            supported = tuple(int(p) for p in version.split('.')[:3]) >= (0, 37, 0)
        except ValueError:
            supported = False
        if not supported:
            self.record('interrupt', prompt_id=prompt_id, result='unsupported_version')
            self.log('Automatischer Abbruch nicht bestätigt: ComfyUI-Version für gezielten Stopp unbekannt.')
            return
        try:
            # Delete only our pending item, then interrupt only our running ID.
            self.call(self.c['comfy_url'], '/queue', {'delete': [prompt_id]}, timeout=TIMEOUT)
            self.call(self.c['comfy_url'], '/interrupt', {'prompt_id': prompt_id}, timeout=TIMEOUT)
            self.record('interrupt', prompt_id=prompt_id, result='requested_not_confirmed')
            self.log('Abbruch ausschließlich für den eigenen Take angefordert. '
                     'ComfyUI muss den Abbruch noch verarbeiten; die Prompt-ID bleibt gespeichert.')
        except Exception as exc:
            self.record('interrupt', prompt_id=prompt_id, error=str(exc))
            self.log('Server-Abbruch nicht bestätigt: ' + str(exc))
