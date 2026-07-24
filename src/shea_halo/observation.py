from __future__ import annotations

import hashlib
import os
import re
import stat
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from shea_halo.config import HaloConfig
from shea_halo.runtime_fs import RuntimeFilesystem, RuntimeFilesystemError

if TYPE_CHECKING:
    from shea_halo.workspace import HaloWorkspace

MAX_OBSERVATION_FILE_BYTES = 64 * 1024 * 1024
MAX_OBSERVATION_FILES = 50

_TRACE_ID = re.compile(r"^[0-9a-fA-F]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-fA-F]{16}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ObservationError(RuntimeError):
    pass


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FileObservation(_FrozenModel):
    label: str
    sha256: str
    size_bytes: int = Field(ge=0)

    @field_validator("label")
    @classmethod
    def _logical_label(cls, value: str) -> str:
        logical = PurePosixPath(value)
        if (
            not value
            or "\\" in value
            or logical.is_absolute()
            or ".." in logical.parts
            or logical.parts[0] not in {"target", "worktree"}
            or logical.as_posix() != value
            or len(logical.parts) < 2
        ):
            raise ValueError("observation label must be a stable path below target/ or worktree/")
        return value

    @field_validator("sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("sha256 must be a lowercase hexadecimal digest")
        return value


class SpanKindCount(_FrozenModel):
    kind: str = Field(min_length=1)
    count: int = Field(gt=0)


class TraceObservation(FileObservation):
    span_count: int = Field(gt=0)
    trace_count: int = Field(gt=0)
    parented_span_count: int = Field(ge=0)
    span_kinds: tuple[SpanKindCount, ...]
    service_version_span_count: int = Field(default=0, ge=0)
    service_versions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _consistent_summary(self) -> TraceObservation:
        if self.trace_count > self.span_count:
            raise ValueError("trace_count cannot exceed span_count")
        if self.parented_span_count > self.span_count:
            raise ValueError("parented_span_count cannot exceed span_count")
        if self.service_version_span_count > self.span_count:
            raise ValueError("service_version_span_count cannot exceed span_count")
        if not self.span_kinds or sum(item.count for item in self.span_kinds) != self.span_count:
            raise ValueError("span_kinds must account for every span")
        kinds = tuple(item.kind for item in self.span_kinds)
        if kinds != tuple(sorted(set(kinds))):
            raise ValueError("span_kinds must be unique and sorted")
        if self.service_versions != tuple(sorted(set(self.service_versions))):
            raise ValueError("service_versions must be unique and sorted")
        if bool(self.service_version_span_count) != bool(self.service_versions):
            raise ValueError(
                "service_version_span_count and service_versions must both be present or absent"
            )
        return self


class ObservationCheckpoint(_FrozenModel):
    schema_version: Literal["shea-halo/observation-checkpoint-v1"] = (
        "shea-halo/observation-checkpoint-v1"
    )
    traces: tuple[TraceObservation, ...] = ()
    desktop_reports: tuple[FileObservation, ...] = ()

    @model_validator(mode="after")
    def _stable_inventory(self) -> ObservationCheckpoint:
        for observations in (self.traces, self.desktop_reports):
            labels = tuple(item.label for item in observations)
            if labels != tuple(sorted(set(labels))):
                raise ValueError("checkpoint observations must have unique, sorted labels")
        return self

    def delta_from(self, previous: ObservationCheckpoint | None) -> ObservationDelta:
        return diff_observation_checkpoints(previous, self)


class ChangedFileObservation(_FrozenModel):
    previous_sha256: str
    current: FileObservation

    @field_validator("previous_sha256")
    @classmethod
    def _previous_digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("previous_sha256 must be a lowercase hexadecimal digest")
        return value


class ChangedTraceObservation(_FrozenModel):
    previous_sha256: str
    current: TraceObservation

    @field_validator("previous_sha256")
    @classmethod
    def _previous_digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("previous_sha256 must be a lowercase hexadecimal digest")
        return value


class ObservationDelta(_FrozenModel):
    schema_version: Literal["shea-halo/observation-delta-v1"] = "shea-halo/observation-delta-v1"
    new_traces: tuple[TraceObservation, ...] = ()
    changed_traces: tuple[ChangedTraceObservation, ...] = ()
    new_desktop_reports: tuple[FileObservation, ...] = ()
    changed_desktop_reports: tuple[ChangedFileObservation, ...] = ()

    @property
    def has_changes(self) -> bool:
        return any(
            (
                self.new_traces,
                self.changed_traces,
                self.new_desktop_reports,
                self.changed_desktop_reports,
            )
        )


class TraceValidation(_FrozenModel):
    schema_version: Literal["shea-halo/trace-validation-v1"] = "shea-halo/trace-validation-v1"
    checkpoint: ObservationCheckpoint
    candidate_traces: tuple[TraceObservation, ...] = ()
    candidate_desktop_reports: tuple[FileObservation, ...] = ()
    halo_report_sha256: str | None = None
    halo_dataset_verified: bool = False
    halo_revision_verified: bool = False
    service_version_verified: bool = False
    candidate_revision: str | None = None
    complete: bool = False
    validated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("halo_report_sha256")
    @classmethod
    def _optional_report_digest(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("halo_report_sha256 must be a lowercase hexadecimal digest")
        return value

    @model_validator(mode="after")
    def _complete_validation_has_runtime_evidence(self) -> TraceValidation:
        if not self.complete:
            return self
        if (
            not self.candidate_traces
            or self.halo_report_sha256 is None
            or not self.halo_dataset_verified
            or not self.halo_revision_verified
            or not self.service_version_verified
            or self.candidate_revision is None
            or not any(trace.parented_span_count > 0 for trace in self.candidate_traces)
            or any(
                trace.service_version_span_count != trace.span_count
                for trace in self.candidate_traces
            )
        ):
            raise ValueError(
                "complete trace validation requires a candidate hierarchy, HALO analysis, "
                "candidate revision, and matching trace service.version"
            )
        return self


@dataclass(frozen=True)
class _ObservationRoot:
    scope: Literal["target", "worktree"]
    path: Path


def inventory_observations(
    config: HaloConfig,
    workspace: HaloWorkspace,
) -> ObservationCheckpoint:
    """Build a deterministic, path-free inventory from the two controlled roots."""

    roots = _controlled_roots(config.root, workspace.path)
    trace_candidates = _discover(
        roots,
        config.observation.trace_globs,
        category="trace",
    )
    report_candidates = _discover(
        roots,
        config.observation.desktop_report_globs,
        category="desktop report",
    )
    return ObservationCheckpoint(
        traces=tuple(
            _inventory_trace(path, label) for label, path in sorted(trace_candidates.items())
        ),
        desktop_reports=tuple(
            _inventory_file(path, label) for label, path in sorted(report_candidates.items())
        ),
    )


def discard_worktree_observation_changes(
    config: HaloConfig,
    workspace: HaloWorkspace,
    baseline: ObservationCheckpoint,
) -> tuple[str, ...]:
    """Remove only changed validation artifacts below the worktree's managed `.shea`.

    This recovery path deliberately hashes files without parsing them so a partial
    or malformed trace cannot strand the next research re-entry.
    """

    root = workspace.path.resolve(strict=True)
    roots = (_ObservationRoot(scope="worktree", path=root),)
    candidates = {
        **_discover(roots, config.observation.trace_globs, category="trace"),
        **_discover(
            roots,
            config.observation.desktop_report_globs,
            category="desktop report",
        ),
    }
    baseline_digests = {
        item.label: item.sha256 for item in (*baseline.traces, *baseline.desktop_reports)
    }
    changed: list[tuple[str, Path]] = []
    for label, path in sorted(candidates.items()):
        current = _inventory_file(path, label)
        if baseline_digests.get(label) == current.sha256:
            continue
        relative = path.relative_to(root)
        if not relative.parts or relative.parts[0] != ".shea":
            raise ObservationError(
                "changed validation observation outside worktree/.shea requires manual cleanup"
            )
        changed.append((label, path))

    try:
        runtime = RuntimeFilesystem(root)
        for _, path in changed:
            runtime.remove_file(path)
    except RuntimeFilesystemError:
        raise ObservationError(
            "changed validation observation could not be discarded safely"
        ) from None
    return tuple(label for label, _ in changed)


def diff_observation_checkpoints(
    previous: ObservationCheckpoint | None,
    current: ObservationCheckpoint,
) -> ObservationDelta:
    """Return only observations that are new or changed in the current checkpoint."""

    previous_traces = {item.label: item for item in previous.traces} if previous else {}
    previous_reports = {item.label: item for item in previous.desktop_reports} if previous else {}

    new_traces: list[TraceObservation] = []
    changed_traces: list[ChangedTraceObservation] = []
    for item in current.traces:
        prior = previous_traces.get(item.label)
        if prior is None:
            new_traces.append(item)
        elif prior != item:
            changed_traces.append(
                ChangedTraceObservation(previous_sha256=prior.sha256, current=item)
            )

    new_reports: list[FileObservation] = []
    changed_reports: list[ChangedFileObservation] = []
    for item in current.desktop_reports:
        prior = previous_reports.get(item.label)
        if prior is None:
            new_reports.append(item)
        elif prior != item:
            changed_reports.append(
                ChangedFileObservation(previous_sha256=prior.sha256, current=item)
            )

    return ObservationDelta(
        new_traces=tuple(new_traces),
        changed_traces=tuple(changed_traces),
        new_desktop_reports=tuple(new_reports),
        changed_desktop_reports=tuple(changed_reports),
    )


def _controlled_roots(target: Path, worktree: Path) -> tuple[_ObservationRoot, ...]:
    roots: list[_ObservationRoot] = []
    configured: tuple[tuple[Literal["target", "worktree"], Path], ...] = (
        ("target", target),
        ("worktree", worktree),
    )
    for scope, raw_root in configured:
        try:
            root = raw_root.resolve(strict=True)
        except OSError as exc:
            raise ObservationError(f"{scope} observation root is unavailable") from exc
        if not root.is_dir():
            raise ObservationError(f"{scope} observation root is not a directory")
        if any(item.path == root for item in roots):
            continue
        roots.append(_ObservationRoot(scope=scope, path=root))
    return tuple(roots)


def _discover(
    roots: tuple[_ObservationRoot, ...],
    patterns: tuple[str, ...],
    *,
    category: str,
) -> dict[str, Path]:
    discovered: dict[str, Path] = {}
    for root in roots:
        for pattern in patterns:
            for candidate in root.path.glob(pattern):
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError as exc:
                    raise ObservationError(
                        f"{category} candidate below {root.scope}/ could not be resolved"
                    ) from exc
                if not resolved.is_relative_to(root.path):
                    raise ObservationError(
                        f"{category} candidate escapes the controlled {root.scope} root"
                    )
                if not resolved.is_file():
                    continue
                relative = candidate.relative_to(root.path).as_posix()
                label = f"{root.scope}/{relative}"
                try:
                    FileObservation(
                        label=label,
                        sha256="0" * 64,
                        size_bytes=0,
                    )
                except ValidationError as exc:
                    raise ObservationError(
                        f"{category} candidate has an unsafe logical label"
                    ) from exc
                discovered[label] = resolved
                if len(discovered) > MAX_OBSERVATION_FILES:
                    raise ObservationError(
                        f"{category} inventory exceeds {MAX_OBSERVATION_FILES} files"
                    )
    return discovered


def _open_stable(path: Path, label: str):
    try:
        source = path.open("rb")
    except OSError as exc:
        raise ObservationError(f"observation file is unreadable: {label}") from exc
    before = os.fstat(source.fileno())
    if not stat.S_ISREG(before.st_mode):
        source.close()
        raise ObservationError(f"observation is not a regular file: {label}")
    if before.st_size > MAX_OBSERVATION_FILE_BYTES:
        source.close()
        raise ObservationError(f"observation exceeds {MAX_OBSERVATION_FILE_BYTES} bytes: {label}")
    return source, before


def _assert_stable(source, before: os.stat_result, bytes_read: int, label: str) -> None:
    after = os.fstat(source.fileno())
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_after != identity_before or bytes_read != before.st_size:
        raise ObservationError(f"observation changed while it was inventoried: {label}")


def _inventory_file(path: Path, label: str) -> FileObservation:
    digest = hashlib.sha256()
    bytes_read = 0
    source, before = _open_stable(path, label)
    try:
        while chunk := source.read(1024 * 1024):
            bytes_read += len(chunk)
            if bytes_read > MAX_OBSERVATION_FILE_BYTES:
                raise ObservationError(
                    f"observation exceeds {MAX_OBSERVATION_FILE_BYTES} bytes: {label}"
                )
            digest.update(chunk)
        _assert_stable(source, before, bytes_read, label)
    finally:
        source.close()
    return FileObservation(
        label=label,
        sha256=digest.hexdigest(),
        size_bytes=bytes_read,
    )


def _inventory_trace(path: Path, label: str) -> TraceObservation:
    from engine.traces.models.canonical_span import SpanRecord

    digest = hashlib.sha256()
    bytes_read = 0
    span_count = 0
    parented_span_count = 0
    trace_ids: set[str] = set()
    span_ids: set[tuple[str, str]] = set()
    parent_links: dict[tuple[str, str], tuple[str, str]] = {}
    span_kinds: Counter[str] = Counter()
    service_versions: set[str] = set()
    service_version_span_count = 0

    source, before = _open_stable(path, label)
    try:
        for line_number, raw_line in enumerate(source, start=1):
            bytes_read += len(raw_line)
            if bytes_read > MAX_OBSERVATION_FILE_BYTES:
                raise ObservationError(
                    f"observation exceeds {MAX_OBSERVATION_FILE_BYTES} bytes: {label}"
                )
            digest.update(raw_line)
            if not raw_line.strip():
                continue
            try:
                span = SpanRecord.model_validate_json(raw_line, strict=True)
            except ValidationError as exc:
                raise ObservationError(
                    f"trace is not canonical one-span JSONL at {label}:{line_number}"
                ) from exc
            if (
                not _TRACE_ID.fullmatch(span.trace_id)
                or not _SPAN_ID.fullmatch(span.span_id)
                or span.parent_span_id
                and not _SPAN_ID.fullmatch(span.parent_span_id)
            ):
                raise ObservationError(
                    f"trace has an invalid trace/span identifier at {label}:{line_number}"
                )
            if not span.name.strip() or not span.kind.strip():
                raise ObservationError(
                    f"trace has an empty span name or kind at {label}:{line_number}"
                )
            identity = (span.trace_id.lower(), span.span_id.lower())
            if identity in span_ids:
                raise ObservationError(
                    f"trace has a duplicate span identifier at {label}:{line_number}"
                )
            span_ids.add(identity)
            trace_ids.add(span.trace_id.lower())
            span_count += 1
            if span.parent_span_id:
                parent_identity = (
                    span.trace_id.lower(),
                    span.parent_span_id.lower(),
                )
                if parent_identity == identity:
                    raise ObservationError(
                        f"trace span cannot parent itself at {label}:{line_number}"
                    )
                parent_links[identity] = parent_identity
                parented_span_count += 1
            openinference_kind = span.attributes.get("openinference.span.kind")
            kind = (
                openinference_kind.strip().upper()
                if isinstance(openinference_kind, str) and openinference_kind.strip()
                else span.kind.strip().upper()
            )
            span_kinds[kind] += 1
            service_version = span.resource.attributes.get("service.version")
            if isinstance(service_version, str) and service_version.strip():
                service_versions.add(service_version.strip())
                service_version_span_count += 1
        _assert_stable(source, before, bytes_read, label)
    finally:
        source.close()

    if span_count == 0:
        raise ObservationError(f"trace contains no spans: {label}")
    missing_parents = set(parent_links.values()) - span_ids
    if missing_parents:
        raise ObservationError(f"trace contains a dangling parent span: {label}")
    for identity in parent_links:
        visited: set[tuple[str, str]] = set()
        cursor = identity
        while cursor in parent_links:
            if cursor in visited:
                raise ObservationError(f"trace contains a parent cycle: {label}")
            visited.add(cursor)
            cursor = parent_links[cursor]
    return TraceObservation(
        label=label,
        sha256=digest.hexdigest(),
        size_bytes=bytes_read,
        span_count=span_count,
        trace_count=len(trace_ids),
        parented_span_count=parented_span_count,
        span_kinds=tuple(
            SpanKindCount(kind=kind, count=count) for kind, count in sorted(span_kinds.items())
        ),
        service_version_span_count=service_version_span_count,
        service_versions=tuple(sorted(service_versions)),
    )
