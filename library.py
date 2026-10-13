"""Live model/input-picture catalogs and saved job diagnostics; standard library only."""
import datetime
import json
import re
import threading
import urllib.request
from pathlib import Path

from engine import JobError, atomic_json, choices, input_image_url, input_specs, request

LIBRARY_LOCK = threading.RLock()
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}


def image_preview(server, image):
    # The browser only contacts the local controller. This also works when the
    # ComfyUI host disallows cross-origin images or is reachable only locally.
    url = input_image_url(server, image) + "&preview=webp;70"
    with urllib.request.urlopen(url, timeout=30) as response:
        content_type = response.headers.get_content_type()
        if not content_type.startswith("image/"):
            raise JobError("ComfyUI lieferte keine Bildvorschau.")
        data = response.read(32 * 1024 * 1024 + 1)
    if not data or len(data) > 32 * 1024 * 1024:
        raise JobError("Bildvorschau ist leer oder größer als 32 MB.")
    return data, content_type


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def remember_images(base, server, paths, source="used"):
    server = server.rstrip("/")
    with LIBRARY_LOCK:
        file = Path(base) / "image_library.json"
        library = read_json(file) if file.exists() else {}
        entries = library.setdefault(server, {})
        changed = False
        for image in paths:
            if not image:
                continue
            try:
                input_image_url(server, image)
            except JobError:
                continue
            image = image.replace("\\", "/")
            if image not in entries or (source == "uploaded" and entries[image]["source"] != "uploaded"):
                entries[image] = {"source": source, "added_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
                changed = True
        if changed:
            atomic_json(file, library)
        return dict(entries)


def image_catalog(base, settings, call=request):
    base = Path(base)
    server = settings["comfy_url"].rstrip("/")
    remembered = [settings.get("image", ""), *settings.get("extra_images", [])]
    # Import pre-picker selections. These remain available even if jobs are later removed.
    files = [base / "settings.json", *sorted((base / "runs").glob("*/job.json"))]
    for file in files:
        if not file.exists():
            continue
        try:
            data = read_json(file)
            c = data.get("settings", data)
            if c.get("comfy_url", "").rstrip("/") == server:
                remembered += [c.get("image", ""), *c.get("extra_images", [])]
        except (ValueError, OSError, TypeError):
            continue
    entries = remember_images(base, server, remembered)
    warning = ""
    try:
        objects = call(server, "/object_info/LoadImage")
        for image in choices(input_specs(objects["LoadImage"])["image"]):
            entries.setdefault(image, {"source": "server"})
    except (JobError, KeyError, TypeError) as error:
        warning = "ComfyUI-Bildliste konnte nicht geladen werden. Gespeicherte Bilder werden weiterhin angezeigt. " + str(error)
    result = []
    for path, item in entries.items():
        if Path(path).suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        try:
            url = input_image_url(server, path)
        except JobError:
            continue
        result.append({"path": path, "name": path.rsplit("/", 1)[-1], "source": item["source"], "url": url})
    result.sort(key=lambda row: (row["source"] == "server", row["name"].casefold(), row["path"]))
    return {"images": result, "warning": warning}


def lora_catalog(settings, call=request):
    strength = {"default": 1.0, "min": -100.0, "max": 100.0, "step": 0.01}
    try:
        objects = call(settings["comfy_url"], "/object_info/LoraLoaderModelOnly", timeout=8)
        if "LoraLoaderModelOnly" not in objects:
            raise JobError("ComfyUI-Node fehlt: LoraLoaderModelOnly")
        specs = input_specs(objects["LoraLoaderModelOnly"])
        names = choices(specs["lora_name"])
        metadata = specs["strength_model"][1]
        strength.update({key: metadata[key] for key in strength if key in metadata})
        return {"loras": sorted(names, key=str.casefold), "strength": strength, "warning": ""}
    except (JobError, KeyError, TypeError, IndexError) as error:
        return {"loras": [], "strength": strength,
                "warning": "ComfyUI-LoRA-Liste konnte nicht geladen werden. " + str(error)}


def job_file(directory, filename):
    if filename not in ("plan.json", "events.jsonl", "resources.jsonl") and not re.fullmatch(
            r"ollama/[0-9]{8}T[0-9]{6}_[0-9a-f]{8}_attempt_[0-9]{2}\.json", str(filename)):
        raise JobError("Diese Protokolldatei ist nicht freigegeben.")
    root = Path(directory).resolve()
    file = (root / filename).resolve()
    if not file.is_relative_to(root) or not file.is_file():
        raise JobError("Protokolldatei wurde nicht gefunden.")
    return {"filename": filename, "content": file.read_text(encoding="utf-8")}


def job_details(directory):
    root = Path(directory)
    if not (root / "job.json").is_file():
        raise JobError("Projekt wurde nicht gefunden.")
    state = read_json(root / "job.json")
    prompt_review_prompts = []
    if state.get("review_prompts"):
        for index in range(len(state.get("slots", []))):
            file = root / f"take_{index + 1:02d}_prompt.txt"
            if file.is_file():
                prompt_review_prompts.append({"take": index + 1, "text": file.read_text(encoding="utf-8")})
    attempts = []
    for file in sorted((root / "ollama").glob("*_attempt_*.json"), reverse=True):
        record = read_json(file)
        attempts.append({"file": "ollama/" + file.name, "attempt": record["attempt"],
                         "started_at": record["started_at"], "status": record["status"],
                         "error": record.get("validation_error") or record.get("error"),
                         "model": record.get("request", {}).get("model"),
                         "elapsed_seconds": record.get("elapsed_seconds")})
    log = ""
    file = root / "events.jsonl"
    if file.exists():
        # Preview the tail; the separate download always contains the complete log.
        with file.open("rb") as stream:
            stream.seek(0, 2)
            offset = max(0, stream.tell() - 128 * 1024)
            stream.seek(offset)
            if offset:
                stream.readline()
            lines = stream.read().decode("utf-8").splitlines()
        events = []
        for line in lines:
            try:
                item = json.loads(line)
                events.append(item["time"] + "  " + item["message"])
            except (ValueError, KeyError):
                continue  # A running job may still be appending the last line.
        log = "\n".join(events)
    return {"id": state["id"], "status": state["status"], "error": state.get("error"),
            "log": log, "has_log": file.exists(), "has_plan": (root / "plan.json").exists(),
            "attempts": attempts, "legacy": not state.get("diagnostics_version"),
            'has_resources': (root / 'resources.jsonl').is_file(),
            "prompt_review_enabled": bool(state.get("review_prompts")),
            "prompt_review_pending": bool(state.get("prompt_review_pending")),
            "prompt_review_prompts": prompt_review_prompts}
