"""Local advisory locks are released by the OS even after a process crash."""
from contextlib import contextmanager
import os


def writer_active(path):
    if not path.exists():
        return False
    try:
        with writer_lock(path):
            return False
    except ValueError:
        return True


@contextmanager
def writer_lock(path, *, wait=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError("该记录目录正由另一实例写入，请使用原服务或其他目录") from None
        else:
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("该记录目录正由另一实例写入，请使用原服务或其他目录") from None
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)
