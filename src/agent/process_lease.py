"""Host-local exclusive process lease for one task database.

The OS releases the advisory lock when a process exits, so recovery needs no
invented timeout and cannot mistake a slow live process for a stale worker.
This deliberately supports multiple asyncio workers in one bot process, not
distributed workers on separate hosts or filesystems with weak lock semantics.
"""

from __future__ import annotations

import os
from pathlib import Path


class ProcessLeaseUnavailable(RuntimeError):
    """Another live Delilah process already owns this task database."""


class DatabaseProcessLease:
    def __init__(self, database_path: str | os.PathLike[str]) -> None:
        path = str(database_path)
        if not path or path == ":memory:":
            raise ValueError("a file-backed database path is required for a process lease")
        resolved_path = Path(path).resolve(strict=False)
        self.path = Path(f"{resolved_path}.worker.lock")
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b" ")
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise ProcessLeaseUnavailable(
                        f"another process holds {self.path}"
                    ) from exc
            else:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ProcessLeaseUnavailable(
                        f"another process holds {self.path}"
                    ) from exc
            os.ftruncate(fd, 0)
            os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
            os.fsync(fd)
            self._fd = fd
        except Exception:
            os.close(fd)
            raise

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "DatabaseProcessLease":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()
