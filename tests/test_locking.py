from __future__ import annotations

from pathlib import Path

import pytest

from shea_halo.locking import TargetLockUnavailable, target_worker_lock
from shea_halo.runtime_fs import RuntimeFilesystemError


def test_target_worker_lock_is_non_blocking_and_process_scoped(tmp_path: Path) -> None:
    logs_dir = tmp_path / ".shea" / "logs" / "halo"

    with target_worker_lock(logs_dir) as lock_path:
        assert lock_path == logs_dir / "worker.lock"
        with pytest.raises(TargetLockUnavailable):
            with target_worker_lock(logs_dir):
                pytest.fail("a second worker must not enter the target")

    with target_worker_lock(logs_dir):
        pass


def test_target_worker_lock_rejects_symlinked_logs_directory(tmp_path: Path) -> None:
    shea = tmp_path / ".shea"
    shea.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (shea / "logs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeFilesystemError, match="symlink or non-directory"):
        with target_worker_lock(shea / "logs" / "halo"):
            pytest.fail("an unsafe lock path must not be opened")

    assert list(outside.iterdir()) == []


def test_target_worker_lock_rejects_symlink_leaf(tmp_path: Path) -> None:
    logs_dir = tmp_path / ".shea" / "logs" / "halo"
    logs_dir.mkdir(parents=True)
    outside = tmp_path / "outside.lock"
    outside.write_text("protected", encoding="utf-8")
    (logs_dir / "worker.lock").symlink_to(outside)

    with pytest.raises(RuntimeFilesystemError, match="regular file"):
        with target_worker_lock(logs_dir):
            pytest.fail("a symlink lock leaf must not be opened")

    assert outside.read_text(encoding="utf-8") == "protected"
