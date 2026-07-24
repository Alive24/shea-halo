from __future__ import annotations

import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO


class RuntimeFilesystemError(RuntimeError):
    """Raised when a managed `.shea` filesystem boundary is unsafe."""


def _lexical_absolute(path: Path) -> Path:
    if ".." in path.parts:
        raise RuntimeFilesystemError("managed runtime paths cannot contain `..`")
    return Path(os.path.abspath(os.fspath(path)))


class RuntimeFilesystem:
    """Race-resistant filesystem access rooted at one target's `.shea` directory."""

    def __init__(self, target_root: Path) -> None:
        self.target_root = _lexical_absolute(Path(target_root))
        self.shea_root = self.target_root / ".shea"

    @classmethod
    def from_managed_path(cls, path: Path) -> RuntimeFilesystem:
        raw = Path(path)
        absolute = _lexical_absolute(raw)
        indexes = [index for index, part in enumerate(absolute.parts) if part == ".shea"]
        if not indexes:
            raise RuntimeFilesystemError("managed runtime path must be below target/.shea")
        if len(indexes) != 1:
            raise RuntimeFilesystemError("managed runtime path has an ambiguous .shea root")
        index = indexes[0]
        if index == 0:
            raise RuntimeFilesystemError("managed runtime path has no target root")
        return cls(Path(*absolute.parts[:index]))

    def path(self, candidate: Path, *, allow_shea_root: bool = False) -> Path:
        raw = Path(candidate)
        if ".." in raw.parts:
            raise RuntimeFilesystemError("managed runtime paths cannot contain `..`")
        absolute = (
            _lexical_absolute(raw) if raw.is_absolute() else _lexical_absolute(self.shea_root / raw)
        )
        if (absolute == self.shea_root and not allow_shea_root) or not absolute.is_relative_to(
            self.shea_root
        ):
            raise RuntimeFilesystemError("managed runtime path must stay below target/.shea")
        return absolute

    def logical_label(self, candidate: Path) -> str:
        return (Path(".shea") / self.path(candidate).relative_to(self.shea_root)).as_posix()

    @contextmanager
    def _directory_fd(self, directory: Path, *, create: bool) -> Iterator[int]:
        absolute = self.path(directory, allow_shea_root=True)
        relative = absolute.relative_to(self.shea_root)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            current = os.open(self.target_root, flags)
        except OSError:
            raise RuntimeFilesystemError("target root is not a safe directory") from None
        try:
            for component in (".shea", *relative.parts):
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                    except OSError:
                        raise RuntimeFilesystemError(
                            "managed runtime directory could not be created safely"
                        ) from None
                try:
                    child = os.open(component, flags, dir_fd=current)
                except FileNotFoundError:
                    if not create:
                        raise
                    raise RuntimeFilesystemError(
                        "managed runtime directory disappeared during creation"
                    ) from None
                except OSError:
                    raise RuntimeFilesystemError(
                        "managed runtime parent is a symlink or non-directory"
                    ) from None
                os.close(current)
                current = child
            yield current
        finally:
            os.close(current)

    @contextmanager
    def _parent_fd(self, candidate: Path, *, create: bool) -> Iterator[tuple[int, str]]:
        absolute = self.path(candidate)
        if not absolute.name:
            raise RuntimeFilesystemError("managed runtime file must have a name")
        with self._directory_fd(absolute.parent, create=create) as parent:
            yield parent, absolute.name

    @staticmethod
    def _reject_unsafe_leaf(parent_fd: int, name: str) -> None:
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError:
            raise RuntimeFilesystemError("managed runtime leaf could not be inspected") from None
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeFilesystemError("managed runtime leaf is not a regular file")

    def ensure_directory(self, directory: Path) -> Path:
        absolute = self.path(directory)
        with self._directory_fd(absolute, create=True):
            pass
        return absolute

    def validate_directory(self, directory: Path) -> Path:
        absolute = self.path(directory, allow_shea_root=True)
        with self._directory_fd(absolute, create=False):
            pass
        return absolute

    def require_absent(self, candidate: Path) -> Path:
        absolute = self.path(candidate)
        with self._parent_fd(absolute, create=False) as (parent, name):
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return absolute
            except OSError:
                raise RuntimeFilesystemError(
                    "managed runtime leaf could not be inspected"
                ) from None
        raise RuntimeFilesystemError("managed runtime leaf already exists")

    def atomic_write_text(self, candidate: Path, text: str) -> Path:
        absolute = self.path(candidate)
        payload = text.encode("utf-8")
        with self._parent_fd(absolute, create=True) as (parent, name):
            self._reject_unsafe_leaf(parent, name)
            temporary = f".{name}.{secrets.token_hex(12)}.tmp"
            descriptor: int | None = None
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent,
                )
                with os.fdopen(descriptor, "wb", closefd=True) as output:
                    descriptor = None
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(
                    temporary,
                    name,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                )
                os.fsync(parent)
            except OSError:
                raise RuntimeFilesystemError("managed runtime atomic write failed") from None
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
                except OSError:
                    raise RuntimeFilesystemError(
                        "managed runtime temporary file cleanup failed"
                    ) from None
        return absolute

    def open_append(self, candidate: Path) -> TextIO:
        absolute = self.path(candidate)
        with self._parent_fd(absolute, create=True) as (parent, name):
            self._reject_unsafe_leaf(parent, name)
            try:
                descriptor = os.open(
                    name,
                    os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent,
                )
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    os.close(descriptor)
                    raise RuntimeFilesystemError(
                        "managed runtime append leaf is not a regular file"
                    )
            except RuntimeFilesystemError:
                raise
            except OSError:
                raise RuntimeFilesystemError("managed runtime append open failed") from None
        return os.fdopen(descriptor, "a+", encoding="utf-8", closefd=True)

    def append_text(self, candidate: Path, text: str) -> Path:
        absolute = self.path(candidate)
        handle = self.open_append(absolute)
        try:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        return absolute

    def remove_file(self, candidate: Path, *, missing_ok: bool = True) -> None:
        absolute = self.path(candidate)
        try:
            with self._parent_fd(absolute, create=False) as (parent, name):
                try:
                    self._reject_unsafe_leaf(parent, name)
                    os.unlink(name, dir_fd=parent)
                except FileNotFoundError:
                    if not missing_ok:
                        raise
                except RuntimeFilesystemError:
                    raise
                except OSError:
                    raise RuntimeFilesystemError("managed runtime file removal failed") from None
        except FileNotFoundError:
            if not missing_ok:
                raise

    @staticmethod
    def _validate_tree(directory_fd: int) -> None:
        try:
            names = os.listdir(directory_fd)
        except OSError:
            raise RuntimeFilesystemError("managed runtime tree could not be listed") from None
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        for name in names:
            try:
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                raise RuntimeFilesystemError(
                    "managed runtime tree entry could not be inspected"
                ) from None
            if stat.S_ISDIR(metadata.st_mode):
                try:
                    child = os.open(name, flags, dir_fd=directory_fd)
                except OSError:
                    raise RuntimeFilesystemError(
                        "managed runtime tree directory is unsafe"
                    ) from None
                try:
                    RuntimeFilesystem._validate_tree(child)
                finally:
                    os.close(child)
            elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RuntimeFilesystemError(
                    "managed runtime tree contains a symlink or non-regular file"
                )

    @staticmethod
    def _delete_tree(directory_fd: int) -> None:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        for name in os.listdir(directory_fd):
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                child = os.open(name, flags, dir_fd=directory_fd)
                try:
                    RuntimeFilesystem._delete_tree(child)
                finally:
                    os.close(child)
                os.rmdir(name, dir_fd=directory_fd)
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                os.unlink(name, dir_fd=directory_fd)
            else:
                raise RuntimeFilesystemError("managed runtime tree changed to an unsafe entry")

    def remove_tree(self, candidate: Path, *, missing_ok: bool = True) -> None:
        absolute = self.path(candidate)
        try:
            with self._parent_fd(absolute, create=False) as (parent, name):
                try:
                    metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    if missing_ok:
                        return
                    raise
                except OSError:
                    raise RuntimeFilesystemError(
                        "managed runtime tree could not be inspected"
                    ) from None
                if not stat.S_ISDIR(metadata.st_mode):
                    raise RuntimeFilesystemError(
                        "managed runtime tree root is a symlink or non-directory"
                    )
                try:
                    directory = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=parent,
                    )
                except OSError:
                    raise RuntimeFilesystemError("managed runtime tree root is unsafe") from None
                try:
                    self._validate_tree(directory)
                    self._delete_tree(directory)
                except OSError:
                    raise RuntimeFilesystemError("managed runtime tree removal failed") from None
                finally:
                    os.close(directory)
                try:
                    os.rmdir(name, dir_fd=parent)
                except OSError:
                    raise RuntimeFilesystemError("managed runtime tree removal failed") from None
        except FileNotFoundError:
            if not missing_ok:
                raise

    def read_text(self, candidate: Path) -> str:
        absolute = self.path(candidate)
        with self._parent_fd(absolute, create=False) as (parent, name):
            try:
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                raise
            except OSError:
                raise RuntimeFilesystemError("managed runtime read open failed") from None
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise RuntimeFilesystemError("managed runtime read leaf is not a regular file")
                with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as source:
                    descriptor = -1
                    return source.read()
            finally:
                if descriptor >= 0:
                    os.close(descriptor)


def atomic_write_runtime_text(target_root: Path, path: Path, text: str) -> Path:
    """Write a target-owned `.shea` file atomically."""

    return RuntimeFilesystem(target_root).atomic_write_text(path, text)


def append_runtime_text(target_root: Path, path: Path, text: str) -> Path:
    """Append one service record through the target-owned `.shea` boundary."""

    return RuntimeFilesystem(target_root).append_text(path, text)
