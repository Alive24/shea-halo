from __future__ import annotations

import errno
import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO

from shea_halo.runtime_fs import RuntimeFilesystem


class TargetLockUnavailable(RuntimeError):
    """Raised when another local Shea Halo worker owns the target checkout."""


@contextmanager
def target_worker_lock(logs_dir: Path) -> Iterator[Path]:
    """Hold one non-blocking worker lock for a target checkout.

    The persistent file is only a rendezvous point. Ownership is the live
    advisory ``flock`` attached to this open descriptor, so a crashed worker
    releases the lock without requiring stale-lock cleanup.
    """

    runtime = RuntimeFilesystem.from_managed_path(logs_dir)
    lock_path = runtime.path(logs_dir / "worker.lock")
    handle: TextIO = runtime.open_append(lock_path)
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            raise TargetLockUnavailable(
                f"another Shea Halo worker owns {runtime.logical_label(lock_path)}"
            ) from exc
        yield lock_path
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
