"""Read-only Linux cgroup telemetry; never manufactures a kernel limit."""
import math
import os
from pathlib import Path, PurePosixPath

GIB = 1024 ** 3


def enabled():
    return os.environ.get('H3_CONTAINER') == '1'


def _unescape(value):
    for a, b in ((r'\040', ' '), (r'\011', '\t'), (r'\134', '\\')):
        value = value.replace(a, b)
    return value


def locate(proc=Path('/proc')):
    groups = {}
    for line in (proc / 'self/cgroup').read_text().splitlines():
        _, controllers, name = line.split(':', 2)
        if '..' in PurePosixPath(name).parts:
            raise ValueError('cgroup-Pfad außerhalb des sichtbaren Namensraums.')
        for controller in controllers.split(','):
            groups[controller] = name
    for line in (proc / 'self/mountinfo').read_text().splitlines():
        left, right = line.split(' - ', 1)
        fields, fs = left.split(), right.split()
        version = 2 if fs[0] == 'cgroup2' else 1 if fs[0] == 'cgroup' and 'memory' in fs[2].split(',') else 0
        controller = '' if version == 2 else 'memory'
        if not version or controller not in groups:
            continue
        root, mount = PurePosixPath(_unescape(fields[3])), Path(_unescape(fields[4]))
        group = PurePosixPath(groups[controller])
        try:
            rel = group.relative_to(root)
        except ValueError:
            # A cgroup namespace reports / relative to its own mount.
            if group != PurePosixPath('/'):
                continue
            rel = PurePosixPath('.')
        return version, mount / str(rel), mount
    raise ValueError('Kein lesbarer gemeinsamer Memory-cgroup gefunden.')


def _number(path, unlimited=False):
    value = path.read_text().strip()
    if unlimited and value == 'max':
        return None
    result = int(value)
    if result < 0:
        raise ValueError('Negativer cgroup-Messwert.')
    return None if unlimited and result >= 2**60 else result


def _working_set(path, usage, version):
    stats = dict(line.split() for line in (path / 'memory.stat').read_text().splitlines())
    reclaimable = int(stats['inactive_file' if version == 2 else 'total_inactive_file'])
    if not 0 <= reclaimable <= usage:
        raise ValueError('Unplausibler cgroup-Dateicache.')
    return usage - reclaimable


def snapshot(proc=Path('/proc'), location=None, environ=None):
    env = os.environ if environ is None else environ
    mode = env.get('H3_MEMORY_MODE', 'cgroup')
    if mode not in ('cgroup', 'budget'):
        raise ValueError('H3_MEMORY_MODE muss cgroup oder budget sein.')
    mem = {}
    for line in (proc / 'meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        mem[key] = int(value.split()[0]) * 1024
    total, available = mem['MemTotal'], mem['MemAvailable']
    if not 0 <= available <= total or total <= 0:
        raise ValueError('Unplausibler Host-Speicher.')
    version, current, root = location or locate(proc)
    usage_name = 'memory.current' if version == 2 else 'memory.usage_in_bytes'
    limit_name = 'memory.max' if version == 2 else 'memory.limit_in_bytes'
    usage = _number(current / usage_name)
    working = _working_set(current, usage, version)
    limits, headrooms = [], [available]
    path = current
    while True:
        limit = _number(path / limit_name, unlimited=True)
        used = _number(path / usage_name)
        used = _working_set(path, used, version)
        if limit is not None:
            if limit <= 0:
                raise ValueError('Ungültiges Kernel-Limit.')
            limits.append(limit)
            headrooms.append(max(0, limit - used))
        if path == root:
            break
        if root not in path.parents:
            raise ValueError('cgroup außerhalb des Mounts.')
        path = path.parent
    hard = min(limits) if limits else None
    configured = env.get('H3_RAM_BUDGET_GIB', '').strip()
    budget = float(configured) * GIB if configured else None
    if budget is not None and (not math.isfinite(budget) or not 8 * GIB <= budget <= 4096 * GIB):
        raise ValueError('H3_RAM_BUDGET_GIB muss zwischen 8 und 4096 liegen.')
    if mode == 'cgroup' and (hard is None or hard > total):
        raise ValueError('Kein endliches Kernel-RAM-Limit. Limit setzen oder Cloud-Budget ausdrücklich aktivieren.')
    if mode == 'budget' and budget is None:
        raise ValueError('Cloud-Modus benötigt H3_RAM_BUDGET_GIB.')
    budget = min(x for x in (total, hard, budget) if x is not None)
    if budget < 8 * GIB:
        raise ValueError('Weniger als 8 GiB RAM-Budget.')
    headrooms.append(max(0, budget - working))
    return dict(mode=mode, budget_gib=budget / GIB, ram_total_gib=budget / GIB,
                ram_free_gib=min(headrooms) / GIB, usage_gib=usage / GIB, working_set_gib=working / GIB,
                hard_limit_gib=hard / GIB if hard is not None else None,
                hard_limit_verified=mode == 'cgroup' and hard is not None,
                source=f'cgroup-v{version}',
                warning='Cloud-Budget: Laufzeitüberwachung, kein verifiziertes hartes RAM-Limit.' if mode == 'budget' else '')


def safety():
    endpoints = dict(comfy_url='http://127.0.0.1:8188', ollama_url='http://127.0.0.1:11434')
    try:
        state = snapshot()
        return dict(state, **endpoints, blocked=False,
                    verified_budget_gib=state['hard_limit_gib'] if state['hard_limit_verified'] else None)
    except (OSError, ValueError, KeyError) as exc:
        return dict(endpoints, blocked=True, hard_limit_verified=False, reason=str(exc))
