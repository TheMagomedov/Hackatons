"""OS-released single-instance lock: recovery must not race a live sender."""

import os
from pathlib import Path

from app.shared.errors import Conflict


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise Conflict("Приложение уже запущено с этой папкой DATA_DIR.") from None
        self._handle = handle
        return self

    def __exit__(self, *_):
        if self._handle:
            self._handle.close()
            self._handle = None
