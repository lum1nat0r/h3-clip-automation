"""Local H3 controller. Python standard library only; no ComfyUI custom node.

Generation graph derived from the supplied NVFP4 workflow (GPL-3.0).
OBVPM owns all latent handling, trimming, lineage and media export.
"""
from __future__ import annotations

import copy
import datetime
import json
import math
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from resources import MemoryGuard, SafetyError, safe_new_settings, validate_profile
from staging import split_graph, cache_present, clear_cache, listed_keys
from hardware import normalize_profile, resolve_profile, selection_issues

FPS = 24
ROOT_FRAMES = 124
EXTEND_FRAMES = 141
CONTEXT_FRAMES = 39
DEFAULTS = {
    "comfy_url": "http://127.0.0.1:8188",
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "YOUR_OLLAMA_MODEL",
    "seconds": 30, "width": 736, "height": 416,
    "steps": 20, "seed": -1, "headroom_gb": 3.0,
    "unet": "minimax_h3_ref2va_pruned_nvfp4.safetensors",
    "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "video_vae": "minimax_h3_video_vae_int8_convrot.safetensors",
    "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
    "image": "", "extra_images": [], "reference_audio": "", "loras": [],
    "output_prefix": "h3_automatik", "crf": 19,
    "ollama_num_ctx": 8192, "ollama_num_predict": 4096,
    "ollama_think": False, "ollama_timeout": 1800,
    "job_timeout_hours": 1,
    "speech_wpm": 150, "speech_pause_seconds": 0.25,
    "hardware_profile": None,
}


class JobError(RuntimeError):
    pass


class Paused(JobError):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def request(base, route, data=None, timeout=30):
    url = base.rstrip("/") + route
    payload = None if data is None else json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            if route == '/inspire/cache/list':
                return raw.decode('utf-8')
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:3000]
        raise JobError(f"{route}: HTTP {e.code}: {detail}") from e
    except (OSError, ValueError) as e:
        raise JobError(f"{route}: {e}") from e


def input_image_url(base, image):
    """Build a URL for a relative input path, including nested folders."""
    if not isinstance(image, str):
        raise JobError("Start-/Referenzbild: Dateipfad als Text erwartet.")
    path = image.replace("\\", "/")
    if (any(part in ("", ".", "..") for part in path.split("/"))
            or any(char in path for char in (":", "\x00", "\r", "\n"))
            or path.endswith((" [input]", " [output]", " [temp]"))):
        raise JobError("Ungültiger Bildpfad relativ zu ComfyUI/input: " + repr(image))
    subfolder, _, filename = path.rpartition("/")
    query = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": "input"})
    return base.rstrip("/") + "/view?" + query


def verify_input_image(base, image):
    """Check the real input file; LoadImage's dropdown omits subdirectories."""
    req = urllib.request.Request(input_image_url(base, image), headers={"Range": "bytes=0-15"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            if not response.headers.get_content_type().startswith("image/") or not response.read(16):
                raise JobError("ComfyUI liefert für das Start-/Referenzbild keine Bilddatei: " + image)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise JobError("Start-/Referenzbild ist in ComfyUI nicht vorhanden: " + image) from e
        raise JobError(f"Start-/Referenzbild konnte nicht geprüft werden (HTTP {e.code}): {image}") from e
    except (OSError, ValueError) as e:
        raise JobError(f"Start-/Referenzbild konnte nicht geprüft werden: {image}: {e}") from e


def relative_path(value):
    p = str(value).replace("\\", "/")
    if not p or p.startswith("/") or any(x in p for x in ["\n", "\r", "@", "[", "]", ":"]):
        raise JobError("Ungültiger relativer ComfyUI-Dateipfad: " + repr(p))
    if any(x in (".", "..", "") for x in p.split("/")):
        raise JobError("Ungültiger relativer ComfyUI-Dateipfad: " + repr(p))
    return p


def normalize_settings(settings):
    import container_memory
    c = copy.deepcopy(DEFAULTS)
    c.update(settings)
    if container_memory.enabled():
        for key, expected in (('comfy_url', DEFAULTS['comfy_url']), ('ollama_url', DEFAULTS['ollama_url'])):
            if str(c[key]).rstrip('/') != expected:
                raise JobError('Containerbetrieb verwendet ausschließlich die internen Dienste.')
    for key in ("comfy_url", "ollama_url"):
        c[key] = str(c[key]).strip().rstrip("/")
        u = urllib.parse.urlsplit(c[key])
        if u.scheme not in ("http", "https") or not u.hostname or u.query or u.fragment or u.username:
            raise JobError(f"{key}: Bitte eine HTTP(S)-Basisadresse ohne Zugangsdaten eingeben.")
    c["ollama_model"] = str(c["ollama_model"]).strip()
    if not c["ollama_model"]:
        raise JobError("Ollama-Modellname fehlt.")
    for key, lo, hi in [("seconds", 30, 60), ("width", 128, 2048), ("height", 128, 2048),
                        ("steps", 1, 100), ("crf", 0, 51), ("ollama_num_ctx", 8192, 65536),
                        ("ollama_num_predict", 2048, 16384)]:
        c[key] = int(c[key])
        if not lo <= c[key] <= hi:
            raise JobError(f"{key} muss zwischen {lo} und {hi} liegen.")
    if c["width"] % 32 or c["height"] % 32:
        raise JobError("Breite und Höhe müssen durch 32 teilbar sein.")
    c["seed"] = int(c["seed"])
    if not -1 <= c["seed"] < 2**63:
        raise JobError("Seed muss -1 oder eine nichtnegative 63-Bit-Zahl sein.")
    c["headroom_gb"] = float(c["headroom_gb"])
    if not 0 <= c["headroom_gb"] <= 24:
        raise JobError("VRAM-Reserve muss zwischen 0 und 24 GB liegen.")
    c["output_prefix"] = relative_path(c["output_prefix"])
    if len(c["output_prefix"].split("/")) > 1:
        raise JobError("Ausgabepräfix bitte als einen Ordnernamen ohne Unterordner angeben.")
    if len(c["extra_images"]) > 3:
        raise JobError("Höchstens drei zusätzliche Bildreferenzen.")
    for key, lo, hi in (("speech_wpm", 60, 240), ("speech_pause_seconds", 0, 1.5)):
        c[key] = float(c[key])
        if not lo <= c[key] <= hi:
            raise JobError(f"{key} muss zwischen {lo} und {hi} liegen.")
    if c.get('hardware_profile') is not None:
        c['hardware_profile'] = normalize_profile(c['hardware_profile'])
    return c


def with_speech_guidance(slots, settings=None):
    """Use retained NEW duration, excluding repeated context and trimmed tails."""
    c = {**DEFAULTS, **(settings or {})}
    result = copy.deepcopy(slots)
    for slot in result:
        slot.pop("speech_word_limit", None)  # Migrate old saved jobs on resume.
        seconds = max(0, slot["keep_frames"] / FPS - c["speech_pause_seconds"])
        slot["speech_seconds"] = round(seconds, 3)
        slot["speech_target_words"] = round(seconds * c["speech_wpm"] / 60)
    return result


def speech_timing(plan, slots, settings=None):
    """Heuristic pacing report, never a validity gate or an audio measurement."""
    c = {**DEFAULTS, **(settings or {})}
    slots = with_speech_guidance(slots, c)
    takes, warnings = [], []
    for slot, take in zip(slots, plan["segments"]):
        words = len(take["dialogue"].split())
        estimated = words * 60 / c["speech_wpm"]
        available = slot["speech_seconds"]
        takes.append({"take": slot["index"], "words": words, "target_words": slot["speech_target_words"],
                      "available_seconds": available, "estimated_seconds": round(estimated, 2)})
        # Allow natural variation; flag only estimates over 125% of the window.
        if estimated > available * 1.25:
            warnings.append(f"Take {slot['index']}: {words} Wörter, geschätzt {estimated:.1f} s Sprechzeit "
                            f"bei {c['speech_wpm']:g} Wörtern/Minute; geplant sind {available:.2f} s. "
                            "Zeitschätzung, kein Abbruch; der Dialog bleibt unverändert.")
    available = sum(slot["speech_seconds"] for slot in slots)
    return {"words_per_minute": c["speech_wpm"], "pause_seconds_per_take": c["speech_pause_seconds"],
            "target_words_total": round(available * c["speech_wpm"] / 60),
            "actual_words_total": sum(take["words"] for take in takes),
            "available_speech_seconds": round(available, 3), "takes": takes, "warnings": warnings}


def schedule(seconds, settings=None):
    target = round(seconds * FPS)
    result, start = [], 0
    while start < target:
        raw = ROOT_FRAMES if not result else EXTEND_FRAMES
        context = 0 if not result else CONTEXT_FRAMES
        delivered = raw - context
        kept = min(delivered, target - start)
        result.append({"index": len(result) + 1, "start_frame": start,
                       "raw_frames": raw, "context_frames": context,
                       "delivered_frames": delivered, "keep_frames": kept,
                       "start_seconds": round(start / FPS, 3),
                       "end_seconds": round((start + kept) / FPS, 3)})
        start += kept
    # Avoid a final sliver: shorten the preceding extension by one 51-frame
    # shared-AV interval, so the ending has at least two usable seconds.
    if len(result) > 2 and result[-1]["keep_frames"] < 48:
        previous, last = result[-2:]
        for key in ("raw_frames", "delivered_frames", "keep_frames"):
            previous[key] -= 51
        last["start_frame"] -= 51
        last["keep_frames"] += 51
        for slot in (previous, last):
            slot["start_seconds"] = round(slot["start_frame"] / FPS, 3)
            slot["end_seconds"] = round((slot["start_frame"] + slot["keep_frames"]) / FPS, 3)
    return with_speech_guidance(result, settings)


PLAN_FIELDS = ("subject", "setting", "look", "voice", "music")
TAKE_FIELDS = ("action", "camera", "dialogue", "ambience")


def audio_constraints(master):
    silent = bool(re.search(r"\b(?:keine?\s+(?:sprache|stimme|dialoge?|gespräche|narration)|ohne\s+(?:sprache|stimme|dialog|gespräch|narration)|no\s+(?:speech|dialogue|dialog|voice|narration)|without\s+(?:speech|dialogue|narration)|stumm|silent\s+(?:clip|video|scene))\b", master, re.I))
    no_music = bool(re.search(r"\b(?:keine?\s+musik|ohne\s+musik|no\s+music|without\s+music)\b", master, re.I))
    return silent, no_music


def plan_schema(count, silent=False, no_music=False):
    def obj(fields):
        return {"type": "object", "properties": {k: {"type": "string"} for k in fields},
                "required": list(fields), "additionalProperties": False}
    schema = obj(PLAN_FIELDS)
    schema["properties"]["segments"] = {"type": "array", "minItems": count, "maxItems": count,
                                          "items": obj(TAKE_FIELDS)}
    schema["required"].append("segments")
    if silent:
        schema["properties"]["segments"]["items"]["properties"]["dialogue"] = {"type": "string", "const": ""}
        schema["properties"]["voice"] = {"type": "string", "const": "No speech or narration."}
    if no_music:
        schema["properties"]["music"] = {"type": "string", "const": "None."}
    return schema


def validate_plan(plan, slots):
    if not isinstance(plan, dict) or set(plan) != set(PLAN_FIELDS) | {"segments"}:
        raise JobError("Plan hat nicht die vereinbarte Struktur.")
    for k in PLAN_FIELDS:
        if not isinstance(plan[k], str) or not 1 <= len(plan[k].strip()) <= 1500:
            raise JobError(f"Planfeld {k} fehlt oder ist zu lang.")
    if not isinstance(plan["segments"], list) or len(plan["segments"]) != len(slots):
        raise JobError(f"Plan braucht genau {len(slots)} Takes.")
    for slot, take in zip(slots, plan["segments"]):
        if not isinstance(take, dict) or set(take) != set(TAKE_FIELDS):
            raise JobError(f"Take {slot['index']}: falsche Felder.")
        for k in TAKE_FIELDS:
            if not isinstance(take[k], str) or len(take[k]) > 1800 or (k != "dialogue" and not take[k].strip()):
                raise JobError(f"Take {slot['index']}: ungültiges Feld {k}.")
    return plan


def create_plan(c, master, slots, log, call=request, attempt_dir=None, before_attempt=None):
    c = {**DEFAULTS, **c}
    slots = with_speech_guidance(slots, c)
    target_words_total = round(sum(s["speech_seconds"] for s in slots) * c["speech_wpm"] / 60)
    silent, no_music = audio_constraints(master)
    schema = plan_schema(len(slots), silent, no_music)
    system = """You plan a COMPLETE continuous 30-60 second MiniMax H3 reference-to-video clip.
Return only the requested JSON. Treat the master prompt as the creative brief.
Plan beginning, development and a definite ending over ALL supplied time slots, not a repeating loop.
Subject identity, outfit, location, light and voice must stay coherent unless the brief asks otherwise.
The reference Picture 1 is supplied separately to H3. You cannot see it: do not invent physical details
that contradict it. Describe its subject as 'the subject in <Picture 1>'. Additional pictures, if any,
are references of that same subject. This setup best supports one continuous scene and one speaker.
All visual direction and descriptions in English; preserve the requested language of spoken dialogue.
Do not add speech unless the brief requests speech. If no speech, dialogue must be the empty string.
Use natural dialogue that fits each take's NEW retained duration, with room for the requested delivery.
The supplied speech_target_words and total_word_target are approximate pacing GUIDANCE, NOT hard limits.
There is NO fixed five-word limit. Six or seven words can be perfectly appropriate in a four-second take.
Use speech_words_per_minute and speech_pause_seconds_per_take as planning assumptions. Distribute the
dialogue across the whole clip; do not squeeze the entire script into one take or force every take to speak.
Vary phrase length naturally. Allow more time for long words, emphasis or slow delivery; do not count
words at the expense of coherent sentences. Finish each spoken phrase within its retained time window.
If the brief asks for German dialogue, the dialogue field contains plain German words, no tags.
If no speech is requested, voice must be 'No speech or narration.' and EVERY dialogue must be ''.
Only when speech is requested, voice identifies speaker S1 and the language, delivery and timbre.
No unrequested subtitles, titles, watermarks, extra speakers, zooms or scene changes.
Music is 'None.' unless requested. Avoid overloading a short take with too many actions.
Each action continues the PREVIOUS delivered ending without resetting to the initial pose.
The last action settles to a satisfying end BEFORE its end_seconds; the tail may be trimmed.
Do not describe preserved context as new action. Each slot's time range is NEW retained video.
Be concise: each segment action <= 70 words, camera <= 20, ambience <= 20.
Global subject/setting/look/voice each <= 60 words. Do not mention JSON or APIs inside descriptions."""
    brief = json.dumps({"master_prompt": master, "seconds": c["seconds"], "slots": slots,
                        "speech_words_per_minute": c["speech_wpm"],
                        "speech_pause_seconds_per_take": c["speech_pause_seconds"],
                        "total_word_target": target_words_total,
                        "additional_same_subject_images": len(c["extra_images"]),
                        "speech_forbidden": silent, "music_forbidden": no_music,
                        "schema": schema}, ensure_ascii=False)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": brief}]
    log(f"Sprachplanung: ca. {target_words_total} Wörter über {c['seconds']} s als Richtwert "
        f"({c['speech_wpm']:g} Wörter/Minute, {c['speech_pause_seconds']:g} s Pausenreserve je Take). "
        "Keine feste Wortgrenze; stille Passagen bleiben möglich.")
    session = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + secrets.token_hex(4)
    for attempt in range(3):
        if before_attempt:
            before_attempt()
        log(f"Ollama plant {len(slots)} Takes (Versuch {attempt + 1}/3) …")
        body = {"model": c["ollama_model"], "messages": messages, "format": schema,
                "stream": False, "keep_alive": 0,
                "options": {"temperature": 0.25, "num_ctx": c["ollama_num_ctx"],
                            "num_predict": c["ollama_num_predict"]}}
        if c.get("ollama_think") is not None:
            body["think"] = c["ollama_think"]
        record = {"attempt": attempt + 1, "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "endpoint": c["ollama_url"] + "/api/chat", "request": copy.deepcopy(body),
                  "response": None, "status": "waiting", "validation_error": None}
        path = Path(attempt_dir) / f"{session}_attempt_{attempt+1:02d}.json" if attempt_dir else None
        def save_attempt():
            if path:
                atomic_json(path, record)
        save_attempt()  # Persist the request before contacting Ollama.
        started = time.monotonic()
        try:
            response = call(c["ollama_url"], "/api/chat", body, timeout=c["ollama_timeout"])
        except Exception as error:
            record.update(status="request_failed", error=str(error), elapsed_seconds=round(time.monotonic() - started, 3))
            save_attempt()
            raise
        record.update(response=response, status="received", elapsed_seconds=round(time.monotonic() - started, 3))
        save_attempt()  # Keep even malformed or rejected model output.
        content = response.get("message", {}).get("content", "")
        try:
            if response.get("done_reason") == "length":
                raise JobError("Modellausgabe am Tokenlimit abgeschnitten; kürzer schreiben.")
            plan = validate_plan(json.loads(content), slots)
            if silent and (any(t["dialogue"] for t in plan["segments"]) or plan["voice"] != "No speech or narration."):
                raise JobError("The master prompt explicitly forbids speech: all dialogue must be empty and voice must be 'No speech or narration.'.")
            if no_music and plan["music"] != "None.":
                raise JobError("The master prompt explicitly forbids music: music must be 'None.'.")
            report = speech_timing(plan, slots, c)
            record["speech_timing"] = report
            record["warnings"] = report["warnings"]
            for warning in report["warnings"]:
                log("Zeit-Hinweis: " + warning)
            record["status"] = "accepted"
            save_attempt()
            return plan
        except (ValueError, JobError) as e:
            record.update(status="rejected", validation_error=str(e))
            save_attempt()
            log(f"Ollama-Versuch {attempt + 1} abgelehnt: {e}")
            if attempt == 2:
                raise JobError(f"Ollama lieferte nach drei Versuchen keinen gültigen Plan: {e}") from e
            messages += [{"role": "assistant", "content": content},
                         {"role": "user", "content": "Correct ALL errors below and return the COMPLETE concise JSON plan again. "
                          "Keep the requested language, meaning and natural pacing. Word targets are guidance, "
                          "not rejection criteria. Check the JSON structure and explicit content constraints.\n" + str(e)}]


def render_prompt(plan, take, slot, c):
    subject = plan["subject"]
    for i in range(len(c["extra_images"])):
        subject += f" <Picture {i + 2}> shows the same subject as <Picture 1>, not an additional person."
    if c["reference_audio"]:
        subject += " <Audio 1> defines the voice of S1; preserve its timbre."
    if slot["index"] == 1:
        opening = "Begin from the first-frame guide with the composition of <Picture 1>."
    else:
        opening = ("The first 1.625 seconds are preserved audiovisual context from the preceding take. "
                   "Continue its ending smoothly into new action, without a cut or resetting the pose. "
                   "The description below concerns the NEW content after that context.")
    dialogue = take["dialogue"].strip()
    rate = c.get("speech_wpm", DEFAULTS["speech_wpm"])
    pause = c.get("speech_pause_seconds", DEFAULTS["speech_pause_seconds"])
    speech = (f"S1 ({plan['voice']}) says: <d>{dialogue}</d> Mouth motion follows the words. "
              f"Aim for approximately {rate:g} words per minute, adjusting naturally to the requested delivery. "
              f"Allow roughly {pause:g} seconds TOTAL for pauses within the NEW content and finish before its end. "
              "No additional speech." if dialogue else "No spoken dialogue or narration in this take.")
    return (f"subject_definitions:\n<Subject 1> is the subject in <Picture 1>. {subject}\n\n"
            f"summary:\n[reference generation + keyframe completion] {plan['setting']} {plan['look']}\n\n"
            "retention_analysis:\n<Subject 1> (appears in [Shot 1]): fully_preserved. "
            "Preserve recognizable identity, clothing and voice across continuations, except changes explicitly described.\n\n"
            f"detailed_description:\n[Shot 1] {opening} {take['action']} {take['camera']} "
            f"The new visible content lasts {slot['keep_frames'] / FPS:.3f} seconds. {speech} "
            "No subtitles, captions or watermarks unless explicitly described.\n\n"
            f"overall_soundscape:\n{take['ambience']} {plan['voice']}\n\n"
            f"non_diegetic_music:\n{plan['music']}\n")


def build_graph(c, prompt, slot, folder, seed, previous=None):
    g = {}
    def node(id_, kind, **inputs):
        g[str(id_)] = {"class_type": kind, "inputs": inputs}
    def wire(id_, slot_=0):
        return [str(id_), slot_]
    node(1, "UNETLoader", unet_name=c["unet"], weight_dtype="default")
    model = wire(1)
    for i, lora in enumerate(c["loras"]):
        node(50+i, "LoraLoaderModelOnly", model=model, lora_name=lora["name"], strength_model=float(lora["strength"]))
        model = wire(50+i)
    node(2, "MiniMaxH3SigmaShift", model=model, shift_video=12.0, shift_audio=3.0)
    node(3, "ModelAttentionBackend", model=wire(2), attention="pytorch attention")
    node(4, "H3VramHeadroom", model=wire(3), enabled=True, headroom_gb=c["headroom_gb"], count_guides=True)
    node(5, "CLIPLoader", clip_name=c["clip"], type="minimax", device="default")
    node(6, "VAELoader", vae_name=c["video_vae"])
    node(7, "VAELoader", vae_name=c["audio_vae"])
    node(8, "LoadImage", image=c["image"])
    node(9, "MiniMaxH3ReferenceToVideo", clip=wire(5), vae=wire(6), audio_vae=wire(7),
         prompt=prompt, width=c["width"], height=c["height"], length=slot["raw_frames"], ref_image_size="match")
    g["9"]["inputs"]["ref_images.ref_image_0"] = wire(8)
    for i, picture in enumerate(c["extra_images"]):
        node(30+i, "LoadImage", image=picture)
        g["9"]["inputs"][f"ref_images.ref_image_{i+1}"] = wire(30+i)
    if c["reference_audio"]:
        node(34, "LoadAudio", audio=c["reference_audio"])
        g["9"]["inputs"]["ref_audios.ref_audio_0"] = wire(34)
    cond = wire(9)
    if previous:
        node(10, "H3LoadMCtx", clip=relative_path(previous), create_pins="extend (pin tail)", pin_window="39")
    else:
        node(10, "MiniMaxH3AddGuide", positive=wire(9), vae=wire(6), latent=wire(9, 1), image=wire(8), frame_idx=0)
        cond = wire(10)
    node(11, "H3MCtxApplyPins", conditioning=cond, latent=wire(9, 1),
         snap_window_down_to_available=False, freeze_audio=False,
         video_vae=wire(6), audio_vae=wire(7), masked_audio_feather=8, audio_denoise=0.0)
    if previous:
        g["11"]["inputs"]["pin_specs"] = wire(10, 1)
    node(12, "RandomNoise", noise_seed=seed)
    node(13, "BasicGuider", model=wire(4), conditioning=wire(11))
    node(14, "KSamplerSelect", sampler_name="res_multistep")
    node(15, "BasicScheduler", model=wire(4), scheduler="simple", steps=c["steps"], denoise=1.0)
    node(16, "SamplerCustomAdvanced", noise=wire(12), guider=wire(13), sampler=wire(14),
         sigmas=wire(15), latent_image=wire(11, 1))
    node(17, "VAEDecode", samples=wire(16, 1), vae=wire(6))
    node(18, "VAEDecodeAudio", samples=wire(16, 1), vae=wire(7))
    node(19, "H3TrimAndSaveVideoWithMCtx", samples=wire(16, 1), images=wire(17), audio=wire(18),
         pins=wire(11, 2), conditioning=wire(11), base_folder=folder,
         filename_prefix=f"take_{slot['index']:02d}", crf=c["crf"], save_conditioning=True,
         metadata=json.dumps({"prompt": prompt, "seed": seed, "steps": c["steps"], "automatic_take": slot}))
    node(20, "H3ResultPreview", path=wire(19))
    return g


def input_specs(info):
    return {**info.get("input", {}).get("required", {}), **info.get("input", {}).get("optional", {})}


def choices(spec):
    return spec[0] if isinstance(spec[0], list) else (spec[1].get("options", []) if len(spec) > 1 else [])


def validate_graph(g, objects, skip_file_choices=False):
    """Check live node schema, mandatory inputs, enums, wire types and cycles."""
    for id_, n in g.items():
        kind = n["class_type"]
        if kind not in objects:
            raise JobError(f"ComfyUI-Node fehlt: {kind}")
        info = objects[kind]
        specs = input_specs(info)
        for key in info.get("input", {}).get("required", {}):
            if key not in n["inputs"]:
                raise JobError(f"{kind}: Pflichtfeld {key} fehlt.")
        for key, value in n["inputs"].items():
            spec = specs.get(key)
            if spec is None and "." in key:
                group, suffix = key.split(".", 1)
                parent = specs.get(group)
                if parent and parent[0] == "COMFY_AUTOGROW_V3":
                    opt = parent[1]["template"]
                    index = suffix.removeprefix(opt["prefix"])
                    if suffix.startswith(opt["prefix"]) and index.isdigit() and int(index) < opt["max"]:
                        spec = next(iter(opt["input"]["required"].values()))
            if spec is None:
                raise JobError(f"{kind}: unbekanntes Feld {key}; Node-Version prüfen.")
            typ = "COMBO" if isinstance(spec[0], list) else spec[0]
            if isinstance(value, list):
                if len(value) != 2 or str(value[0]) not in g or not isinstance(value[1], int):
                    raise JobError(f"{kind}.{key}: ungültige Verbindung.")
                source = objects[g[str(value[0])]["class_type"]]
                outs = source["output"]
                if not 0 <= value[1] < len(outs) or outs[value[1]] not in (typ, "*") and typ != "*":
                    raise JobError(f"{kind}.{key}: Verbindungstyp passt nicht.")
            else:
                opts = choices(spec)
                # Core LoadImage validates file existence itself. Its dropdown
                # only lists root files, so valid uploads in subfolders are absent.
                image_path = kind == "LoadImage" and key == "image"
                if image_path and not isinstance(value, str):
                    raise JobError(f"{kind}.{key}: Dateipfad als Text erwartet.")
                if opts and not image_path and not (skip_file_choices and (kind in ("LoadAudio", "H3LoadMCtx"))):
                    if value not in opts:
                        raise JobError(f"{kind}.{key}: nicht verfügbar: {value}")
                if typ == "INT" and (type(value) is not int):
                    raise JobError(f"{kind}.{key}: Ganzzahl erwartet.")
                if typ == "FLOAT" and type(value) not in (int, float):
                    raise JobError(f"{kind}.{key}: Zahl erwartet.")
                if typ == "BOOLEAN" and type(value) is not bool:
                    raise JobError(f"{kind}.{key}: Boolean erwartet.")
                if typ == "STRING" and not isinstance(value, str):
                    raise JobError(f"{kind}.{key}: Text erwartet.")
                if type(value) in (int, float) and len(spec) > 1:
                    if value < spec[1].get("min", -math.inf) or value > spec[1].get("max", math.inf):
                        raise JobError(f"{kind}.{key}: außerhalb des Wertebereichs.")
    active, visited = set(), set()
    def visit(k):
        if k in active:
            raise JobError("Workflow enthält einen Zyklus.")
        if k in visited:
            return
        active.add(k)
        for v in g[k]["inputs"].values():
            if isinstance(v, list):
                visit(str(v[0]))
        active.remove(k)
        visited.add(k)
    for k in g:
        visit(k)


def preflight(c, call=request, require_image=True):
    objects = call(c["comfy_url"], "/object_info")
    for kind in ('CacheBackendData //Inspire', 'RetrieveBackendData //Inspire'):
        if kind not in objects:
            raise JobError('Für die getrennte Prompt-Vorbereitung fehlt die bereits vorgesehene Inspire-Node: ' + kind)
    listed_keys(call(c['comfy_url'], '/inspire/cache/list', timeout=8))
    models = call(c["ollama_url"], "/api/tags").get("models", [])
    if c["ollama_model"] not in {m["name"] for m in models}:
        raise JobError("Ollama-Modell ist auf diesem Server nicht installiert: " + c["ollama_model"])
    for key, kind, field in [("unet", "UNETLoader", "unet_name"), ("clip", "CLIPLoader", "clip_name"),
                              ("video_vae", "VAELoader", "vae_name"), ("audio_vae", "VAELoader", "vae_name")]:
        if kind not in objects:
            raise JobError("Node fehlt: " + kind)
        available = choices(input_specs(objects[kind])[field])
        if c[key] not in available:
            matches = [a for a in available if a.replace("\\", "/").split("/")[-1] == c[key]]
            if len(matches) != 1:
                raise JobError("Modelldatei fehlt oder ist mehrdeutig: " + c[key])
            c[key] = matches[0]
    if require_image and not c["image"]:
        raise JobError("Bitte einmalig ein Startbild auswählen oder hochladen.")
    if c.get('hardware_profile'):
        c['hardware_profile'], budget = resolve_profile(c['hardware_profile'], c.get('_ram_budget_gib', c.get('_verified_ram_budget_gib', 24)))
        c['headroom_gb'] = c['hardware_profile']['headroom_gib']
        issues = selection_issues(c, budget, call(c['comfy_url'], '/system_stats', timeout=8))
        if issues:
            raise JobError('Hardware/Modelle: ' + ' '.join(issues))
    for previous in (None, "h3_automatik/check/take.mp4"):
        slot = schedule(c["seconds"])[1 if previous else 0]
        graph = build_graph(c, "Compatibility check", slot, "h3_automatik/check", 1, previous)
        validate_graph(graph, objects, skip_file_choices=True)
    if require_image:
        for image in [c["image"]] + c["extra_images"]:
            verify_input_image(c["comfy_url"], image)
    return objects


def ensure_idle(c, call=request):
    q = call(c["comfy_url"], "/queue")
    if q.get("queue_running") or q.get("queue_pending"):
        raise JobError("ComfyUI verarbeitet bereits einen Auftrag. Danach erneut starten/fortsetzen.")


def view_url(c, path):
    p = relative_path(path)
    sub, _, filename = p.rpartition("/")
    return c["comfy_url"] + "/view?" + urllib.parse.urlencode({"filename": filename, "subfolder": sub, "type": "output"})


class Runner:
    def __init__(self, directory, log=print, stopped=lambda: False, call=request, poll_seconds=3):
        self.directory = Path(directory)
        self._log, self.stopped, self.call, self.poll_seconds = log, stopped, call, poll_seconds
        self.state_path = self.directory / "job.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else None

    def log(self, message):
        self.directory.mkdir(parents=True, exist_ok=True)
        event = {"time": datetime.datetime.now(datetime.timezone.utc).isoformat(), "message": str(message)}
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._log(message)

    def save(self):
        atomic_json(self.state_path, self.state)

    def pause_point(self):
        if self.stopped() and not getattr(self, 'finish_current_take', False):
            raise Paused('Angehalten. Fertige Takes und der gespeicherte Plan bleiben erhalten.')

    def new(self, settings, master, review_prompts=False):
        requested = normalize_settings(settings)
        c = safe_new_settings(requested)
        master = master.strip()
        if not master or len(master) > 6000:
            raise JobError("Bitte einen Master-Prompt mit 1 bis 6000 Zeichen eingeben.")
        self.state = {"id": self.directory.name, "settings": c, "master_prompt": master,
                      "seed": secrets.randbits(50) if c["seed"] == -1 else c["seed"],
                      "slots": schedule(c["seconds"], c), "plan": None, "takes": [],
                      "pending": None, "status": "created", "final_path": None,
                      "review_prompts": review_prompts is True, "prompt_review_pending": False,
                      "prompt_review_confirmed": False,
                      "requested_resolution": [requested['width'], requested['height']]}
        self.save()

    def prompt_for_take(self, index):
        s = self.state
        path = self.directory / f"take_{index + 1:02d}_prompt.txt"
        if path.is_file():
            return path.read_text(encoding="utf-8")
        text = render_prompt(s["plan"], s["plan"]["segments"][index], s["slots"][index], s["settings"])
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        return text

    def prepare_prompt_review(self):
        s = self.state
        for index in range(len(s["slots"])):
            self.prompt_for_take(index)
        s["prompt_review_pending"] = True
        s["pause_reason"] = "prompt_review"
        self.save()

    def confirm_prompt_review(self):
        s = self.state
        if not s.get("prompt_review_pending"):
            return False
        if not s.get("plan") or any(
                not (self.directory / f"take_{index + 1:02d}_prompt.txt").is_file()
                for index in range(len(s.get("slots", [])))):
            raise JobError("Die Ollama-Prompts für diese Prüfung sind nicht vollständig gespeichert.")
        s["prompt_review_pending"] = False
        s["prompt_review_confirmed"] = True
        s.pop("pause_reason", None)
        self.save()
        return True

    def recover_submission(self):
        s, c = self.state, self.state["settings"]
        pending = s["pending"]
        if pending.get("prompt_id"):
            return
        q = self.call(c["comfy_url"], "/queue")
        history = self.call(c["comfy_url"], "/history?max_items=500")
        records = q.get("queue_running", []) + q.get("queue_pending", [])
        records += [h.get("prompt", []) for h in history.values()]
        matches = [r for r in records if len(r) > 3 and r[3].get("h3_automation_request") == pending["request_id"]]
        if not matches:
            raise JobError("Unklar, ob ComfyUI den letzten Auftrag angenommen hat. Kein automatischer Doppelstart. "
                           "Warteschlange/History prüfen; siehe README.md, Abschnitt Wiederaufnahme.")
        pending["prompt_id"] = matches[0][1]
        self.save()

    def wait_for_take(self):
        s, c = self.state, self.state["settings"]
        self.recover_submission()
        pending = s["pending"]
        pid = pending["prompt_id"]
        # Include elapsed time across controller restarts, not a fresh timeout.
        started = pending.setdefault('submitted_at', time.time())
        self.save()
        remaining = min(float(c["job_timeout_hours"]), 1) * 3600 - max(0, time.time() - started)
        deadline = time.monotonic() + max(0, remaining)
        while True:
            self.pause_point()
            h = self.call(c["comfy_url"], "/history/" + urllib.parse.quote(pid, safe=""), timeout=8)
            row = h.get(pid)
            if row:
                status = row.get("status", {})
                errors = [m for m in status.get("messages", []) if m[0] in ("execution_error", "execution_interrupted")]
                if errors or status.get("status_str") == "error":
                    s["failed_prompt"] = copy.deepcopy(pending)
                    atomic_json(self.directory / ('failed_' + pending['request_id'] + '.json'), row)
                    s["pending"] = None
                    if re.search(r'out.of.memory|\boom\b|cuda error', json.dumps(errors or status), re.I):
                        s['safety_hold'] = 'ComfyUI meldete einen Speicher-/CUDA-Fehler. Vor einem neuen Versuch Servergrenzen prüfen.'
                    self.save()
                    if (pending.get('retry_after_guard') and errors
                            and all(m[0] == 'execution_interrupted' for m in errors)):
                        self.log('Der frühere Schutzstopp ist in ComfyUI bestätigt. Dieser Take wird mit getrennter Prompt-Vorbereitung neu versucht.')
                        return
                    raise JobError("ComfyUI meldet einen Fehler: " + json.dumps(errors or status, ensure_ascii=False)[:2500])
                if status.get('completed') and pending.get('phase') in ('encoding', 'sampling'):
                    phase = pending['phase']
                    if not cache_present(self.call, c['comfy_url'], pending['cache_token'], phase):
                        # A server restart may have discarded the volatile cache.
                        self.log('Prompt-Zwischenspeicher fehlt; Vorbereitung wird erneut ausgeführt.')
                        s[phase] = None
                    else:
                        s[phase] = {k: pending[k] for k in ('cache_token', 'take_index', 'prompt_id')}
                        self.log('Prompt und Referenzbild vorbereitet. Text-Encoder wird vor der Videoberechnung freigegeben.' if phase == 'encoding'
                                 else 'Videoberechnung fertig. Videomodell wird vor der Bild- und Tonausgabe freigegeben.')
                    s['pending'] = None
                    self.save()
                    return
                result = row.get("outputs", {}).get("20", {}).get("h3_result", [])
                if status.get("completed") and result:
                    clip = relative_path(result[0]["clip"])
                    if not clip.startswith(s["folder"] + "/"):
                        raise JobError("Take-Pfad liegt außerhalb dieses Projekts.")
                    meta = self.call(c["comfy_url"], "/obvpm/h3/clip_meta", {"clip": clip})
                    slot = s["slots"][len(s["takes"])]
                    if meta.get("no_sidecar") or not meta.get("self_id"):
                        raise JobError("Take hat keinen verifizierten Bewegungskontext; Fortsetzung gestoppt.")
                    if int(meta.get("delivered_frames", -1)) != slot["delivered_frames"]:
                        raise JobError("Unerwartete Nutzlänge. Keine unsichere Fortsetzung; Details in job.json/History.")
                    if s["takes"] and meta.get("parent_id") != s["takes"][-1]["self_id"]:
                        raise JobError("Der neue Take setzt nicht den erwarteten Vorgänger fort.")
                    s["takes"].append({"clip": clip, "self_id": meta["self_id"],
                                       "frames": int(meta["delivered_frames"]), "prompt_id": pid,
                                       "seam": result[0].get("seam")})
                    s["pending"] = None
                    self.save()
                    return
                if status.get("completed"):
                    raise JobError("Auftrag abgeschlossen, aber H3ResultPreview fehlt in der History.")
            # Always reconcile a terminal history record, even after a long
            # pause. The deadline limits work still running, not saved results.
            if time.monotonic() >= deadline:
                raise SafetyError('Auftrag läuft länger als eine Stunde. Sicherheitsstopp; die Prompt-ID bleibt zur Prüfung erhalten.')
            if not row:
                q = self.call(c["comfy_url"], "/queue", timeout=8)
                if not any(str(r[1]) == pid for r in q.get("queue_running", []) + q.get("queue_pending", [])):
                    # History can become visible just after the queue changes.
                    again = self.call(c["comfy_url"], "/history/" + urllib.parse.quote(pid, safe=""))
                    if pid not in again:
                        raise JobError("Auftrag weder in Warteschlange noch History. Zustand bleibt erhalten; nicht doppelt gestartet.")
            if hasattr(self, 'guard'):
                self.guard.running(pending.get('phase', 'take') + '_running')
            time.sleep(self.poll_seconds)

    def submit_graph(self, graph, index, phase, cache_token):
        s, c = self.state, self.state['settings']
        self.guard.idle()
        self.guard.ollama_idle()
        self.guard.check(self.guard.measure('before_' + phase + '_submit'), admission=True)
        request_id = secrets.token_hex(16)
        s['pending'] = {'request_id': request_id, 'prompt_id': None, 'take_index': index,
                        'phase': phase, 'cache_token': cache_token, 'submitted_at': time.time()}
        s['status'] = {'encoding':'encoding', 'sampling':'generating', 'decoding':'decoding'}[phase]
        self.save()
        try:
            response = self.call(c['comfy_url'], '/prompt', {
                'prompt': graph, 'client_id': s['id'],
                'extra_data': {'h3_automation_request': request_id}})
        except JobError as exc:
            if 'HTTP 400:' in str(exc):
                s['pending'] = None
                self.save()
            raise
        if not response.get('prompt_id') or response.get('node_errors'):
            raise JobError('ComfyUI hat den Workflow nicht vollständig angenommen: ' + json.dumps(response))
        s['pending']['prompt_id'] = response['prompt_id']
        self.save()
        self.wait_for_take()

    def release_encoding(self):
        records = [self.state.get(phase) for phase in ('encoding', 'sampling')]
        for token in {record['cache_token'] for record in records if record}:
            self.guard.idle()
            clear_cache(self.call, self.state['settings']['comfy_url'], token)
        self.state['encoding'] = self.state['sampling'] = None
        self.save()

    def run(self):
        s, c = self.state, self.state["settings"]
        if s["final_path"]:
            return s["final_path"]
        try:
            c = normalize_settings(c)
            if s.get('safety_hold'):
                raise SafetyError(s['safety_hold'])
            validate_profile(c, s['slots'])
            # Upgrade resource settings without changing an existing take's shape.
            c = safe_new_settings(c)
            s["settings"] = c
            s["slots"] = with_speech_guidance(s["slots"], c)
            if s.get('pending') and s.get('status') == 'safety_stopped':
                s['pending']['retry_after_guard'] = True
            s.pop("error", None)
            s["diagnostics_version"] = 1
            self.log("Projekt gestartet / fortgesetzt.")
            self.guard = MemoryGuard(c, self.directory, self.call, self.log, self.pause_point)
            if s.get('requested_resolution') != [c['width'], c['height']]:
                self.log(f"Speicherprofil: {c['width']} × {c['height']} Pixel, höchstens 141 Rohframes je Take.")
            self.guard.measure('initial')
            self.log("Prüfe Nodes, Modelle und Referenzen …")
            preflight(c, self.call)
            s.setdefault("folder", c["output_prefix"] + "/" + s["id"])
            self.save()
            if not s["plan"]:
                self.pause_point()
                ensure_idle(c, self.call)
                s["status"] = "planning"
                self.save()
                with self.guard.observe('ollama_running'):
                    s["plan"] = create_plan(c, s["master_prompt"], s["slots"], self.log, self.call,
                                            attempt_dir=self.directory / "ollama",
                                            before_attempt=lambda: self.guard.prepare('before_ollama'))
                self.save()
                atomic_json(self.directory / "plan.json", s["plan"])
                self.log("Plan geprüft; Ollama entlädt das Planungsmodell (keep_alive=0).")
            for _ in range(30):
                loaded = self.call(c["ollama_url"], "/api/ps").get("models", [])
                if not any(m.get("name") == c["ollama_model"] or m.get("model") == c["ollama_model"] for m in loaded):
                    break
                self.pause_point()
                time.sleep(2)
            else:
                raise JobError("Ollama hat das Planungsmodell noch nicht entladen. Speicher vor Fortsetzung prüfen.")
            if (s.get("review_prompts") and not s.get("prompt_review_confirmed")
                    and not s.get("prompt_review_pending")):
                self.prepare_prompt_review()
            if s.get("prompt_review_pending"):
                raise Paused("Ollama hat die Prompts erstellt. Bitte prüfen und danach im Projekt fortsetzen.")
            self.finish_current_take = bool(s.get('pending') or s.get('encoding') or s.get('sampling'))
            if s["pending"]:
                self.log("Prüfe bereits gestarteten Auftrag …")
                count_before = len(s['takes'])
                self.wait_for_take()
                if len(s['takes']) > count_before:
                    self.release_encoding()
                    self.finish_current_take = False
            while len(s["takes"]) < len(s["slots"]):
                self.pause_point()
                self.finish_current_take = True
                i = len(s["takes"])
                slot = s["slots"][i]
                text = self.prompt_for_take(i)
                prev = s["takes"][-1]["clip"] if s["takes"] else None
                graph = build_graph(c, text, slot, s["folder"], (s["seed"] + i) % 2**63, prev)
                objects = self.call(c["comfy_url"], "/object_info")
                validate_graph(graph, objects)
                encoded = s.get('encoding')
                sampled = s.get('sampling')
                if sampled and (sampled['take_index'] != i or not cache_present(self.call, c['comfy_url'], sampled['cache_token'], 'sampling')):
                    self.release_encoding()
                    encoded = sampled = None
                if not sampled and encoded and (encoded['take_index'] != i or not cache_present(self.call, c['comfy_url'], encoded['cache_token'])):
                    self.release_encoding()
                    encoded = None
                ready = sampled or encoded
                token = ready['cache_token'] if ready else secrets.token_hex(16)
                encode, sample, decode = split_graph(graph, token)
                validate_graph(encode, objects)
                validate_graph(sample, objects)
                validate_graph(decode, objects)
                atomic_json(self.directory / f"take_{i+1:02d}_encode_api.json", encode)
                atomic_json(self.directory / f"take_{i+1:02d}_api.json", sample)
                atomic_json(self.directory / f"take_{i+1:02d}_decode_api.json", decode)
                if not ready:
                    self.guard.prepare('before_encoding')
                    self.log(f"Take {i+1}/{len(s['slots'])}: Prompt und Referenzbild vorbereiten …")
                    self.submit_graph(encode, i, 'encoding', token)
                    if not s.get('encoding'):
                        raise JobError('Prompt-Zwischenspeicher nach Vorbereitung nicht verfügbar. Bitte erneut fortsetzen.')
                # This is an actual execution-cache reset, not just GPU offload.
                if not sampled:
                    self.guard.prepare('before_take')
                    if not cache_present(self.call, c['comfy_url'], token):
                        raise JobError('Prompt-Zwischenspeicher nach Freigabe verloren. Kein Videoauftrag gestartet; bitte fortsetzen.')
                    self.log(f"Take {i+1}/{len(s['slots'])}: {slot['start_seconds']:.2f}–{slot['end_seconds']:.2f} s berechnen …")
                    self.submit_graph(sample, i, 'sampling', token)
                    if not s.get('sampling'):
                        raise JobError('Video-Zwischenspeicher nach Berechnung fehlt; bitte fortsetzen.')
                self.guard.prepare('before_decoding')
                if not cache_present(self.call, c['comfy_url'], token, 'sampling'):
                    raise JobError('Video-Zwischenspeicher nach Freigabe verloren. Bitte fortsetzen.')
                self.log(f'Take {i+1}: Bilder und Ton ausgeben …')
                self.submit_graph(decode, i, 'decoding', token)
                self.release_encoding()
                self.finish_current_take = False
            self.pause_point()
            self.release_encoding()
            self.guard.prepare('before_export')
            sequence = "\n".join(t["clip"] for t in s["takes"])
            args = {"sequence": sequence, "base_folder": s["folder"], "preview_filename": "assembled",
                    "export_filename_prefix": "final", "crf": c["crf"], "level_lock": True,
                    "level_lock_frames": 12, "level_lock_flicker": True, "level_lock_local": True,
                    "crossfade": True, "crossfade_frames": 0, "audio_declick": False}
            probe = self.call(c["comfy_url"], "/obvpm/h3/preview_cut", {**args, "probe": True}, timeout=180)
            target = c["seconds"] * FPS
            expected = sum(t["frames"] for t in s["takes"])
            if probe["total"] != expected or probe["total"] < target:
                raise JobError("Timeline-Nutzlänge weicht ab; Export zur Kontrolle gestoppt.")
            excess = probe["total"] - target
            if excess:
                keep = s["takes"][-1]["frames"] - excess
                if keep <= 0:
                    raise JobError("Letzter Take wäre vollständig abgeschnitten.")
                sequence += f" @ ..{keep}"
            args["sequence"] = sequence
            probe = self.call(c["comfy_url"], "/obvpm/h3/preview_cut", {**args, "probe": True}, timeout=180)
            if probe["total"] != target:
                raise JobError("Getrimmte Timeline hat nicht die Zieldauer.")
            s["sequence"] = sequence
            s["status"] = "exporting"
            self.save()
            (self.directory / "timeline.txt").write_text(sequence, encoding="utf-8")
            self.save_editor(sequence)
            self.log(f"Exportiere {c['seconds']} Sekunden mit Ton …")
            with self.guard.observe('export_running'):
                exported = self.call(c["comfy_url"], "/obvpm/h3/export", args, timeout=1800)
            s["final_path"] = relative_path(exported["path"])
            s["final_url"] = view_url(c, s["final_path"])
            s["status"] = "complete"
            self.save()
            self.log("Fertig: " + s["final_path"])
            return s["final_path"]
        except Paused as e:
            s["status"] = "paused"
            self.save()
            self.log(str(e))
            raise
        except Exception as e:
            if isinstance(e, SafetyError):
                s['status'] = 'safety_stopped'
                if hasattr(self, 'guard'):
                    self.guard.record('safety_stop', error=str(e))
                if hasattr(self, 'guard') and s.get('pending'):
                    s['pending']['retry_after_guard'] = True
                    self.guard.interrupt(s['pending'].get('prompt_id'))
            else:
                s["status"] = "error"
                # A lost connection must not leave an unmonitored known take
                # running silently. Keep pending even when interruption is sent.
                if hasattr(self, 'guard') and s.get('pending'):
                    self.guard.interrupt(s['pending'].get('prompt_id'))
            s["error"] = str(e)
            self.save()
            self.log("Fehler: " + str(e))
            raise

    def save_editor(self, sequence):
        template = Path(__file__).with_name("H3_NVFP4_Timeline_Editor.json")
        if not template.exists():
            return
        workflow = json.loads(template.read_text(encoding="utf-8"))
        c, s = self.state["settings"], self.state
        for n in workflow["nodes"]:
            if n["id"] == 761:
                n["widgets_values"][0] = render_prompt(s["plan"], s["plan"]["segments"][-1], s["slots"][-1], c)
                n["widgets_values"][3] = s["folder"]
            elif n["id"] == 1280:
                n["widgets_values"][0] = c["image"]
            elif n["id"] == 1221:
                n["widgets_values"][0] = sequence
                n["widgets_values_named"]["sequence"] = sequence
                n["widgets_values_named"]["pin_state"] = ""
                n["widgets_values"][6] = ""
            elif n["id"] == 1524:
                n["widgets_values"][0] = False
        atomic_json(self.directory / "Timeline_zur_Ansicht.json", workflow)
