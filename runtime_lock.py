"""One cross-process lease for generation and model maintenance."""
from contextlib import contextmanager
from pathlib import Path
import os


class BusyError(RuntimeError):
    pass


@contextmanager
def exclusive(directory):
    path = Path(directory) / '.activity.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise BusyError('Generierung oder Modellinstallation läuft bereits.') from exc
    try:
        yield
    finally:
        handle.close()  # The kernel releases the lease even after a crash.
