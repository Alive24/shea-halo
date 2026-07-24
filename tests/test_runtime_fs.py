from __future__ import annotations

from pathlib import Path

import pytest

from shea_halo.runtime_fs import (
    RuntimeFilesystem,
    RuntimeFilesystemError,
    append_runtime_text,
    atomic_write_runtime_text,
)


def test_atomic_write_and_append_stay_below_target_shea(tmp_path: Path) -> None:
    runtime = RuntimeFilesystem(tmp_path)
    path = tmp_path / ".shea" / "logs" / "halo" / "worker.jsonl"

    runtime.atomic_write_text(path, "first\n")
    runtime.append_text(path, "second\n")

    assert path.read_text(encoding="utf-8") == "first\nsecond\n"
    assert list(path.parent.glob(".*.tmp")) == []
    assert runtime.logical_label(path) == ".shea/logs/halo/worker.jsonl"


def test_service_helpers_require_explicit_target_containment(tmp_path: Path) -> None:
    path = tmp_path / ".shea" / "logs" / "halo" / "worker.jsonl"

    atomic_write_runtime_text(tmp_path, path, "one\n")
    append_runtime_text(tmp_path, path, "two\n")

    assert path.read_text(encoding="utf-8") == "one\ntwo\n"
    with pytest.raises(RuntimeFilesystemError, match="stay below"):
        append_runtime_text(tmp_path, tmp_path / "outside.log", "unsafe\n")


def test_runtime_path_rejects_lexical_parent_segments(tmp_path: Path) -> None:
    runtime = RuntimeFilesystem(tmp_path)
    escaped = tmp_path / ".shea" / "logs" / ".." / ".." / "outside.log"

    with pytest.raises(RuntimeFilesystemError, match="cannot contain"):
        runtime.atomic_write_text(escaped, "unsafe")

    assert not (tmp_path / "outside.log").exists()


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_runtime_rejects_unsafe_parent_components(tmp_path: Path, kind: str) -> None:
    shea = tmp_path / ".shea"
    shea.mkdir()
    parent = shea / "artifacts"
    outside = tmp_path / "outside"
    outside.mkdir()
    if kind == "symlink":
        parent.symlink_to(outside, target_is_directory=True)
    else:
        parent.write_text("not a directory", encoding="utf-8")
    runtime = RuntimeFilesystem(tmp_path)

    with pytest.raises(RuntimeFilesystemError, match="symlink or non-directory"):
        runtime.atomic_write_text(parent / "halo" / "cache.json", "unsafe")

    assert not (outside / "halo" / "cache.json").exists()


@pytest.mark.parametrize("operation", ["write", "append", "read"])
def test_runtime_rejects_symlink_leaf(
    tmp_path: Path,
    operation: str,
) -> None:
    runtime = RuntimeFilesystem(tmp_path)
    leaf = tmp_path / ".shea" / "logs" / "halo" / "worker.jsonl"
    leaf.parent.mkdir(parents=True)
    outside = tmp_path / "outside.log"
    outside.write_text("protected", encoding="utf-8")
    leaf.symlink_to(outside)

    with pytest.raises(RuntimeFilesystemError):
        if operation == "write":
            runtime.atomic_write_text(leaf, "unsafe")
        elif operation == "append":
            runtime.append_text(leaf, "unsafe")
        else:
            runtime.read_text(leaf)

    assert outside.read_text(encoding="utf-8") == "protected"


def test_runtime_factory_requires_a_shea_component(tmp_path: Path) -> None:
    with pytest.raises(RuntimeFilesystemError, match="target/.shea"):
        RuntimeFilesystem.from_managed_path(tmp_path / "cache")

    with pytest.raises(RuntimeFilesystemError, match="ambiguous"):
        RuntimeFilesystem.from_managed_path(tmp_path / ".shea" / "nested" / ".shea" / "cache")


def test_runtime_rejects_hardlinked_append_leaf(tmp_path: Path) -> None:
    runtime = RuntimeFilesystem(tmp_path)
    outside = tmp_path / "outside.log"
    outside.write_text("protected", encoding="utf-8")
    leaf = tmp_path / ".shea" / "logs" / "worker.jsonl"
    leaf.parent.mkdir(parents=True)
    leaf.hardlink_to(outside)

    with pytest.raises(RuntimeFilesystemError, match="regular file"):
        runtime.append_text(leaf, "unsafe")

    assert outside.read_text(encoding="utf-8") == "protected"


def test_runtime_removes_only_safe_managed_files_and_trees(tmp_path: Path) -> None:
    runtime = RuntimeFilesystem(tmp_path)
    file_path = tmp_path / ".shea" / "artifacts" / "run" / "report.md"
    tree_path = tmp_path / ".shea" / "artifacts" / "run" / "indexes"
    runtime.atomic_write_text(file_path, "report")
    runtime.atomic_write_text(tree_path / "nested" / "index.json", "{}")

    runtime.remove_file(file_path)
    runtime.remove_tree(tree_path)

    assert not file_path.exists()
    assert not tree_path.exists()


def test_runtime_tree_removal_rejects_symlink_entries(tmp_path: Path) -> None:
    runtime = RuntimeFilesystem(tmp_path)
    tree_path = tmp_path / ".shea" / "artifacts" / "run" / "indexes"
    tree_path.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("protected", encoding="utf-8")
    (tree_path / "escape").symlink_to(outside)

    with pytest.raises(RuntimeFilesystemError, match="symlink or non-regular"):
        runtime.remove_tree(tree_path)

    assert outside.read_text(encoding="utf-8") == "protected"
