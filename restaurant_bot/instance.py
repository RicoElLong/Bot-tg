"""Cross-platform process lock: never run two pollers on the same local DB."""
from contextlib import contextmanager
import os
from pathlib import Path


@contextmanager
def instance_lock(database: Path):
    lock_path = Path(str(database) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = lock_path.open("a+b")
    try:
        handle.seek(0)
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
            raise RuntimeError("Бот уже запущен с этой базой данных") from None
        yield
    finally:
        handle.close()
