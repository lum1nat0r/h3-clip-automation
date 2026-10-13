"""Operator-owned shared LoRA folder; no weight parsing or dependencies."""
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path

from engine import JobError, normalize_settings, ensure_idle
from runtime_lock import exclusive

MAX_BYTES = 2 * 1024**3
CHUNK_SIZE = 1024**2


def upload_options(comfy_url):
    result = {'available': False, 'reason': '', 'max_bytes': MAX_BYTES}
    directory = os.environ.get('H3_LORA_DIR', '')
    server = os.environ.get('H3_LORA_COMFY_URL', '')
    if not directory or not server:
        result['reason'] = 'LoRA-Upload nicht eingerichtet: Betreiber muss H3_LORA_DIR und H3_LORA_COMFY_URL setzen und den Ordner in ComfyUI als loras registrieren.'
        return result
    try:
        configured = normalize_settings({'comfy_url': server})['comfy_url']
        requested = normalize_settings({'comfy_url': comfy_url})['comfy_url']
    except (JobError, ValueError):
        result['reason'] = 'LoRA-Upload: H3_LORA_COMFY_URL oder die angefragte HTTP(S)-Serveradresse ist ungültig.'
        return result
    if configured != requested:
        result['reason'] = 'LoRA-Upload ist einem anderen ComfyUI-Server zugeordnet. H3_LORA_COMFY_URL prüfen.'
        return result
    try:
        folder = Path(directory)
        if not folder.is_absolute() or not folder.is_dir() or not os.access(folder, os.W_OK | os.X_OK):
            raise OSError('folder unavailable')
        folder.resolve(strict=True)
    except (OSError, ValueError):
        result['reason'] = 'LoRA-Uploadordner ist ungültig, nicht vorhanden oder nicht beschreibbar. H3_LORA_DIR prüfen.'
        return result
    result.update(available=True, reason='')
    return result


def store_lora(stream, length, name, comfy_url, base):
    """Stream into a private same-filesystem part, then publish under the lease."""
    if type(length) is not int or not 0 < length <= MAX_BYTES:
        raise JobError("LoRA-Datei muss zwischen 1 Byte und 2 GiB groß sein.")
    if not isinstance(name, str) or any(ch in name for ch in "\x00\r\n"):
        raise JobError("Ungültiger LoRA-Dateiname.")
    filename = re.sub(r"[^a-zA-Z0-9_.-]", "_", name.replace("\\", "/").split("/")[-1])
    if Path(filename).suffix.lower() != ".safetensors":
        raise JobError("LoRA-Upload unterstützt nur .safetensors.")
    with exclusive(base):
        options = upload_options(comfy_url)
        if not options["available"]:
            raise JobError(options["reason"])
        folder = Path(os.environ["H3_LORA_DIR"]).resolve(strict=True)
        ensure_idle({"comfy_url": comfy_url})
        free = shutil.disk_usage(folder).free
        required = length + 2 * 1024**3
        if free < required:
            raise JobError(f"Zu wenig freier Speicher: {free / 1024**3:.1f} GiB; benötigt {required / 1024**3:.1f} GiB einschließlich Reserve.")
        while True:
            target = folder / (secrets.token_hex(16) + "_" + filename)
            if not target.resolve().is_relative_to(folder):
                raise JobError("LoRA-Dateiname liegt außerhalb des Uploadordners.")
            if not target.exists():
                break
        part = None
        try:
            fd, path = tempfile.mkstemp(prefix=".h3-lora-", suffix=".part", dir=folder)
            part = Path(path)
            with os.fdopen(fd, "wb") as output:
                remaining = length
                while remaining:
                    chunk = stream.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        raise JobError("LoRA-Upload unvollständig: Verbindung vorzeitig beendet.")
                    output.write(chunk)
                    remaining -= len(chunk)
            # Completed weights must be readable by an external ComfyUI UID.
            # Directory permissions remain the operator's access boundary.
            part.chmod(0o644)
            os.replace(part, target)
            return target.name
        finally:
            if part is not None:
                part.unlink(missing_ok=True)
