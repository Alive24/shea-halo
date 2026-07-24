from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import write_target_config
from pydantic import ValidationError

import shea_halo.observation as observation
from shea_halo.config import HaloConfig
from shea_halo.observation import (
    ObservationCheckpoint,
    ObservationError,
    diff_observation_checkpoints,
    inventory_observations,
)
from shea_halo.workspace import HaloWorkspace


def canonical_span(
    *,
    trace_id: str,
    span_id: str,
    parent_span_id: str = "",
    span_kind: str = "AGENT",
) -> dict[str, object]:
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "trace_state": "",
        "name": f"{span_kind.casefold()}.run",
        "kind": "SPAN_KIND_INTERNAL",
        "start_time": "2026-07-24T00:00:00Z",
        "end_time": "2026-07-24T00:00:01Z",
        "status": {"code": "STATUS_CODE_OK", "message": ""},
        "resource": {"attributes": {"service.name": "fixture"}},
        "scope": {"name": "test", "version": "1"},
        "attributes": {"openinference.span.kind": span_kind},
    }


def write_trace(path: Path, *spans: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{json.dumps(span)}\n" for span in spans),
        encoding="utf-8",
    )


def workspace(path: Path) -> HaloWorkspace:
    path.mkdir(parents=True, exist_ok=True)
    return HaloWorkspace(
        path=path,
        branch="halo/issue-1-observe",
        base_revision="a" * 40,
    )


def configured_roots(tmp_path: Path) -> tuple[HaloConfig, HaloWorkspace]:
    target = tmp_path / "target"
    target.mkdir()
    write_target_config(target)
    return HaloConfig.load(target), workspace(tmp_path / "active-worktree")


def test_inventory_is_stable_path_free_and_covers_both_roots(tmp_path: Path) -> None:
    config, active = configured_roots(tmp_path)
    target_trace = config.root / ".shea/artifacts/halo/traces/target.jsonl"
    worktree_trace = active.path / ".shea/artifacts/halo/traces/worktree.jsonl"
    write_trace(
        target_trace,
        canonical_span(trace_id="a" * 32, span_id="1" * 16),
        canonical_span(
            trace_id="a" * 32,
            span_id="2" * 16,
            parent_span_id="1" * 16,
            span_kind="TOOL",
        ),
    )
    write_trace(
        worktree_trace,
        canonical_span(trace_id="b" * 32, span_id="3" * 16, span_kind="LLM"),
    )
    target_report = config.root / ".shea/artifacts/halo/desktop/run-a/report.md"
    worktree_report = active.path / ".shea/artifacts/halo/desktop/run-b/report.md"
    target_report.parent.mkdir(parents=True)
    worktree_report.parent.mkdir(parents=True)
    target_report.write_text("# Target report\n", encoding="utf-8")
    worktree_report.write_text("# Worktree report\n", encoding="utf-8")

    checkpoint = inventory_observations(config, active)

    assert [item.label for item in checkpoint.traces] == [
        "target/.shea/artifacts/halo/traces/target.jsonl",
        "worktree/.shea/artifacts/halo/traces/worktree.jsonl",
    ]
    target = checkpoint.traces[0]
    assert target.span_count == 2
    assert target.trace_count == 1
    assert target.parented_span_count == 1
    assert [(item.kind, item.count) for item in target.span_kinds] == [
        ("AGENT", 1),
        ("TOOL", 1),
    ]
    assert [item.label for item in checkpoint.desktop_reports] == [
        "target/.shea/artifacts/halo/desktop/run-a/report.md",
        "worktree/.shea/artifacts/halo/desktop/run-b/report.md",
    ]
    serialized = checkpoint.model_dump_json()
    assert str(config.root) not in serialized
    assert str(active.path) not in serialized


def test_delta_contains_only_new_and_changed_current_observations(tmp_path: Path) -> None:
    config, active = configured_roots(tmp_path)
    trace = config.root / ".shea/artifacts/halo/traces/existing.jsonl"
    report = active.path / ".shea/artifacts/halo/desktop/run/report.md"
    write_trace(trace, canonical_span(trace_id="a" * 32, span_id="1" * 16))
    report.parent.mkdir(parents=True)
    report.write_text("before\n", encoding="utf-8")
    previous = inventory_observations(config, active)

    write_trace(
        trace,
        canonical_span(trace_id="a" * 32, span_id="1" * 16),
        canonical_span(
            trace_id="a" * 32,
            span_id="2" * 16,
            parent_span_id="1" * 16,
            span_kind="TOOL",
        ),
    )
    report.write_text("after\n", encoding="utf-8")
    write_trace(
        active.path / ".shea/artifacts/halo/traces/new.jsonl",
        canonical_span(trace_id="b" * 32, span_id="3" * 16),
    )
    new_report = config.root / ".shea/artifacts/halo/desktop/new/report.md"
    new_report.parent.mkdir(parents=True)
    new_report.write_text("new\n", encoding="utf-8")
    current = inventory_observations(config, active)

    delta = diff_observation_checkpoints(previous, current)

    assert delta.has_changes
    assert [item.label for item in delta.new_traces] == [
        "worktree/.shea/artifacts/halo/traces/new.jsonl"
    ]
    assert [item.current.label for item in delta.changed_traces] == [
        "target/.shea/artifacts/halo/traces/existing.jsonl"
    ]
    assert delta.changed_traces[0].previous_sha256 == previous.traces[0].sha256
    assert [item.label for item in delta.new_desktop_reports] == [
        "target/.shea/artifacts/halo/desktop/new/report.md"
    ]
    assert [item.current.label for item in delta.changed_desktop_reports] == [
        "worktree/.shea/artifacts/halo/desktop/run/report.md"
    ]
    assert current.delta_from(previous) == delta
    assert not current.delta_from(current).has_changes


@pytest.mark.parametrize("kind", ["trace", "report"])
def test_inventory_rejects_symlink_escape(tmp_path: Path, kind: str) -> None:
    config, active = configured_roots(tmp_path)
    if kind == "trace":
        outside = tmp_path / "outside.jsonl"
        write_trace(outside, canonical_span(trace_id="a" * 32, span_id="1" * 16))
        link = config.root / ".shea/artifacts/halo/traces/escape.jsonl"
    else:
        outside = tmp_path / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        link = config.root / ".shea/artifacts/halo/desktop/run/report.md"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)

    with pytest.raises(ObservationError, match="escapes the controlled target root"):
        inventory_observations(config, active)


@pytest.mark.parametrize("kind", ["trace", "report"])
def test_inventory_rejects_oversized_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    config, active = configured_roots(tmp_path)
    monkeypatch.setattr(observation, "MAX_OBSERVATION_FILE_BYTES", 16)
    if kind == "trace":
        path = config.root / ".shea/artifacts/halo/traces/large.jsonl"
    else:
        path = config.root / ".shea/artifacts/halo/desktop/run/report.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x" * 17)

    with pytest.raises(ObservationError, match="exceeds 16 bytes"):
        inventory_observations(config, active)


@pytest.mark.parametrize(
    "record",
    [
        {"resourceSpans": []},
        canonical_span(trace_id="invalid", span_id="1" * 16),
        {
            key: value
            for key, value in canonical_span(trace_id="a" * 32, span_id="1" * 16).items()
            if key != "status"
        },
    ],
)
def test_trace_inventory_requires_strict_canonical_span_records(
    tmp_path: Path,
    record: dict[str, object],
) -> None:
    config, active = configured_roots(tmp_path)
    write_trace(
        config.root / ".shea/artifacts/halo/traces/invalid.jsonl",
        record,
    )

    with pytest.raises(ObservationError):
        inventory_observations(config, active)


def test_trace_inventory_rejects_duplicate_span_identity(tmp_path: Path) -> None:
    config, active = configured_roots(tmp_path)
    duplicate = canonical_span(trace_id="a" * 32, span_id="1" * 16)
    write_trace(
        config.root / ".shea/artifacts/halo/traces/duplicate.jsonl",
        duplicate,
        duplicate,
    )

    with pytest.raises(ObservationError, match="duplicate span identifier"):
        inventory_observations(config, active)


def test_trace_inventory_requires_real_acyclic_parent_relationships(tmp_path: Path) -> None:
    config, active = configured_roots(tmp_path)
    path = config.root / ".shea/artifacts/halo/traces/invalid-parent.jsonl"
    write_trace(
        path,
        canonical_span(
            trace_id="a" * 32,
            span_id="2" * 16,
            parent_span_id="1" * 16,
            span_kind="TOOL",
        ),
    )

    with pytest.raises(ObservationError, match="dangling parent"):
        inventory_observations(config, active)

    write_trace(
        path,
        canonical_span(
            trace_id="a" * 32,
            span_id="1" * 16,
            parent_span_id="2" * 16,
        ),
        canonical_span(
            trace_id="a" * 32,
            span_id="2" * 16,
            parent_span_id="1" * 16,
            span_kind="TOOL",
        ),
    )

    with pytest.raises(ObservationError, match="parent cycle"):
        inventory_observations(config, active)


def test_checkpoint_rejects_unsafe_or_unstable_persisted_labels() -> None:
    with pytest.raises(ValidationError, match="stable path"):
        ObservationCheckpoint.model_validate(
            {
                "traces": [],
                "desktop_reports": [
                    {
                        "label": "/private/tmp/report.md",
                        "sha256": "a" * 64,
                        "size_bytes": 1,
                    }
                ],
            }
        )

    with pytest.raises(ValidationError, match="unique, sorted"):
        ObservationCheckpoint.model_validate(
            {
                "traces": [],
                "desktop_reports": [
                    {
                        "label": "worktree/z.md",
                        "sha256": "a" * 64,
                        "size_bytes": 1,
                    },
                    {
                        "label": "target/a.md",
                        "sha256": "b" * 64,
                        "size_bytes": 1,
                    },
                ],
            }
        )


def test_duplicate_globs_do_not_duplicate_inventory_items(tmp_path: Path) -> None:
    config, active = configured_roots(tmp_path)
    write_trace(
        config.root / ".shea/artifacts/halo/traces/one.jsonl",
        canonical_span(trace_id="a" * 32, span_id="1" * 16),
    )
    config = replace(
        config,
        observation=replace(
            config.observation,
            trace_globs=(
                ".shea/artifacts/halo/traces/*.jsonl",
                ".shea/artifacts/halo/traces/one.jsonl",
            ),
        ),
    )

    checkpoint = inventory_observations(config, active)

    assert len(checkpoint.traces) == 1
