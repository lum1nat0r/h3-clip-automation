"""Hardware preferences and conservative H3 planning estimates, not peak predictions.

Weight sizes: Comfy-Org/MiniMax-H3, FenomAI/MiniMax-H3 (2026-10-12).
RAM/VRAM planning margins below are application policy, not measured peaks.
Existing hard limits
remain authoritative and are never changed by user-entered capacities.
"""
import copy
import math
import re
import json
from pathlib import Path
import container_memory

GIB = 1024 ** 3
DEFAULT_PROFILE = dict(ram_available_gib=32.0, vram_gib=16.0, gpu_arch='auto',
                       threshold_mode='auto', ram_start_gib=18.0, ram_stop_gib=4.0,
                       vram_start_gib=12.0, headroom_gib=3.0)
ROLES = ('unet', 'clip', 'video_vae', 'audio_vae')
# filename, label, disk GiB, conservative RAM budget, VRAM class, Blackwell-only recommendation
MODELS = {
    'unet': [
        ('minimax_h3_ref2va_pruned_nvfp4.safetensors', 'NVFP4 · Pruned', 11.64, 24, 16, True),
        ('minimax_h3_ref2va_pruned_int8_convrot.safetensors', 'INT8 ConvRot · Pruned', 19.53, 32, 24, False),
        ('minimax_h3_ref2va_pruned_fp8_scaled.safetensors', 'FP8 Scaled · Pruned', 19.52, 32, 24, False),
        ('minimax_h3_ref2va_pruned_bf16.safetensors', 'No-Quant / BF16 · Pruned', 37.46, 56, 48, False),
        ('minimax_h3_ref2va_bf16.safetensors', 'No-Quant / BF16 · Vollmodell', 61.73, 88, 80, False),
    ],
    'clip': [
        ('qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors', 'NVFP4 AWQ', 14.61, 24, 16, False),
        ('qwen3vl_32b_minimax_h3_int8_convrot.safetensors', 'INT8 ConvRot', 25.28, 40, 24, False),
        ('qwen3vl_32b_minimax_h3_bf16.safetensors', 'No-Quant / BF16', 47.97, 72, 48, False),
    ],
    'video_vae': [
        ('minimax_h3_video_vae_int8_convrot.safetensors', 'INT8 ConvRot', 2.62, 16, 16, False),
        ('minimax_h3_video_vae_fp16.safetensors', 'No-Quant / FP16', 4.85, 24, 16, False),
    ],
    'audio_vae': [('minimax_h3_audio_vae_fp32.safetensors', 'FP32', .56, 16, 16, False)],
}
PRESETS = [
    dict(id='nvfp4', title='NVFP4 · wenig Speicher', indexes=(0,0,0,0),
         detail='Speichersparendes NVFP4-Videomodell für Blackwell-GPUs.'),
    dict(id='int8', title='INT8 · weniger Quantisierung', indexes=(1,0,0,0),
         detail='INT8-Videomodell mit sparsamem NVFP4-Textencoder. CUDA 13 oder neuer empfohlen.'),
    dict(id='bf16', title='No-Quant · BF16 Pruned', indexes=(3,2,1,0),
         detail='BF16 für Video und Text, FP16-Video-VAE. Deutlich höherer Speicherbedarf.'),
    dict(id='bf16_full', title='No-Quant · BF16 Vollmodell', indexes=(4,2,1,0),
         detail='Unquantisiertes Vollmodell für große RAM- und VRAM-Budgets.'),
]

# Pinned remote metadata is shared by the UI inventory and download assistant.
SOURCES = {row['filename']: row for row in json.loads(
    Path(__file__).with_name('model_sources.json').read_text(encoding='utf-8-sig'))}
MODELS = {role: [(s[0], s[1], round(SOURCES[s[0]]['size'] / GIB, 2), *s[3:])
                 for s in specs] for role, specs in MODELS.items()}


def normalize_profile(profile):
    if not isinstance(profile, dict):
        raise ValueError('Hardware-Einstellungen fehlen.')
    p = {k: profile.get(k, v) for k, v in DEFAULT_PROFILE.items()}
    for key in ('ram_available_gib','vram_gib','ram_start_gib','ram_stop_gib','vram_start_gib','headroom_gib'):
        value = p[key]
        if isinstance(value, bool):
            raise ValueError('Bitte gültige Speicherwerte eingeben.')
        try:
            p[key] = float(value)
        except (ValueError, TypeError):
            raise ValueError('Bitte gültige Speicherwerte eingeben.')
        if not math.isfinite(p[key]) or not 0 < p[key] <= 4096:
            raise ValueError('Speicherwerte müssen größer als 0 und höchstens 4096 GiB sein.')
    if p['gpu_arch'] not in ('auto','blackwell','other') or p['threshold_mode'] not in ('auto','manual'):
        raise ValueError('Ungültige Hardware-Auswahl.')
    return p


def resolve_profile(profile, verified_budget=None):
    p = normalize_profile(profile)
    budget = min(p['ram_available_gib'], verified_budget) if verified_budget else p['ram_available_gib']
    if p['threshold_mode'] == 'auto':
        half = lambda x: math.ceil(x * 2) / 2
        p.update(ram_start_gib=half(budget*.75), ram_stop_gib=max(2,half(budget/6)),
                 vram_start_gib=half(p['vram_gib']*.75),
                 headroom_gib=min(24,max(1,half(p['vram_gib']*.1875))))
    if not 2 <= p['ram_stop_gib'] < p['ram_start_gib'] < budget:
        raise ValueError(f'RAM: Abbruchreserve mindestens 2 GiB; Startschwelle darüber und unter {budget:g} GiB Budget wählen.')
    if not .5 <= p['vram_start_gib'] < p['vram_gib']:
        raise ValueError('Die VRAM-Startschwelle muss unter dem angegebenen VRAM liegen.')
    if not 1 <= p['headroom_gib'] <= min(24, p['vram_gib']/2):
        raise ValueError('GPU-Reserve: mindestens 1 GiB, höchstens die Hälfte des VRAM bzw. 24 GiB.')
    return p, budget


def bind_settings(c, verified_budget):
    c = copy.deepcopy(c)
    c['hardware_profile'], _ = resolve_profile(c.get('hardware_profile') or DEFAULT_PROFILE, verified_budget)
    c['_ram_budget_gib'] = verified_budget
    # Container telemetry carries its own proof; a planning cap is not a kernel limit.
    c['_verified_ram_budget_gib'] = None if container_memory.enabled() else verified_budget
    c['headroom_gb'] = c['hardware_profile']['headroom_gib']
    return c


def basename(name):
    return str(name).replace('\\','/').split('/')[-1]


def gpu_architecture(profile, stats):
    devices = (stats or {}).get('devices') or []
    name = devices[0].get('name', '') if devices else ''
    if re.search(r'Blackwell|RTX\s*50\d\d|\bB[23]\d\d\b|\bGB[23]\d\d\b', name, re.I):
        return 'blackwell'
    if re.search(r'RTX\s*[234]\d\d\d|\b[AH]100\b', name, re.I):
        return 'other'
    return profile['gpu_arch']


def catalog(objects):
    result = {}
    for role, kind, field in [('unet','UNETLoader','unet_name'),('clip','CLIPLoader','clip_name'),
                               ('video_vae','VAELoader','vae_name'),('audio_vae','VAELoader','vae_name')]:
        info = (objects or {}).get(kind, {}).get('input', {}).get('required', {}).get(field, [[]])
        names = info[0] if isinstance(info[0], list) else (info[1].get('options', []) if len(info)>1 else [])
        rows = []
        for filename,label,size,ram,vram,blackwell in MODELS[role]:
            matches = sorted(n for n in names if basename(n) == filename)
            # Preserve subdirectories; never silently choose between duplicate basenames.
            for path in matches or [None]:
                rows.append(dict(filename=filename, path=path, label=label, size_gib=size,
                                 ram_gib=ram, vram_gib=vram, blackwell=blackwell, installed=bool(path)))
        result[role] = rows
    return result


def selection_issues(c, budget, stats=None):
    p = c['hardware_profile']
    vram = p['vram_gib']
    devices = (stats or {}).get('devices') or []
    if devices:
        if devices[0].get('type') != 'cuda':
            return ['Dieser Workflow benötigt eine CUDA-GPU.']
        vram = min(vram, math.ceil(devices[0]['vram_total']/GIB))
    arch = gpu_architecture(p, stats)
    issues, selected = [], []
    for role in ROLES:
        spec = next((s for s in MODELS[role] if s[0] == basename(c.get(role,''))), None)
        if spec is None:
            issues.append(f'{role}: Für diese Datei gibt es kein unterstütztes H3-Speicherprofil.')
        else:
            selected.append(spec)
    if selected:
        need_ram = max(s[3] for s in selected)
        need_vram = max(s[4] for s in selected)
        if budget < need_ram:
            issues.append(f'Diese Auswahl benötigt nach dem Planungsrichtwert mindestens {need_ram:g} GiB RAM-Budget; verfügbar sind {budget:g} GiB.')
        if vram < need_vram:
            issues.append(f'Diese Auswahl benötigt nach dem Planungsrichtwert eine GPU mit mindestens {need_vram:g} GB VRAM.')
        if any(s[5] for s in selected) and arch != 'blackwell':
            issues.append('NVFP4 für das Videomodell wird nur bei bestätigter Blackwell-GPU empfohlen. GPU-Typ angeben oder INT8 wählen.')
    return issues


def overview(c, verified_budget, objects=None, stats=None):
    c = bind_settings(c, verified_budget)
    p, budget = resolve_profile(c['hardware_profile'], verified_budget)
    options = catalog(objects)
    presets = []
    for preset in PRESETS:
        chosen, missing = {}, []
        for role, index in zip(ROLES, preset['indexes']):
            filename = MODELS[role][index][0]
            rows = [r for r in options[role] if r['filename'] == filename and r['installed']]
            # If duplicates exist, retain an explicit current selection only.
            picked = next((r for r in rows if r['path']==c.get(role)), rows[0] if len(rows)==1 else None)
            chosen[role] = picked['path'] if picked else filename
            if not picked:
                missing.append(filename + (' (mehrfach vorhanden; bitte auswählen)' if len(rows)>1 else ''))
        sample = {**c, **chosen}
        problems = selection_issues(sample, budget, stats)
        specs = [MODELS[r][i] for r,i in zip(ROLES,preset['indexes'])]
        presets.append({**preset, 'models':chosen, 'missing':missing, 'issues':problems,
                        'ram_gib':max(s[3] for s in specs), 'vram_gib':max(s[4] for s in specs),
                        'applicable':not missing and not problems})
    eligible = [r for r in presets if not r['issues']]
    installed = [r for r in eligible if r['applicable']]
    # Prefer less quantization where the policy budgets support it.
    recommended = (installed or eligible)[-1]['id'] if (installed or eligible) else None
    issues = selection_issues(c, budget, stats)
    if objects is not None:
        for role in ROLES:
            matches = [r for r in options[role] if r['installed'] and r['path']==c.get(role)]
            if not matches:
                issues.append(f'{role}: Ausgewählte Modelldatei ist nicht installiert oder muss mit ihrem Unterordner ausgewählt werden.')
    gpu_name = ((stats or {}).get('devices') or [{}])[0].get('name', '')
    gpu_name = re.sub(r'^cuda:\d+\s*', '', gpu_name).split(' : ')[0]
    return dict(settings=c, effective_ram_gib=budget,
                verified_budget_gib=None if container_memory.enabled() else verified_budget,
                options=options, presets=presets, recommended=recommended, issues=issues,
                gpu_arch=gpu_architecture(p, stats), gpu_name=gpu_name,
                live=objects is not None, stats=stats)
