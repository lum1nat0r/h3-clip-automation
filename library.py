"""Input-picture catalog and saved job diagnostics; standard library only."""
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
            'has_resources': (root / 'resources.jsonl').is_file()}
