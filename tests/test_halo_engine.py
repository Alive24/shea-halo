from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from shea_halo.config import HaloConfig
from shea_halo.halo_engine import (
    HaloEngineAdapter,
    HaloEngineError,
    _clear_previous_outputs,
    _halo_engine_environment,
    _managed_run_dir,
    _persist_safe_jsonl,
    _selected_trace_files,
    _trace_file,
    _trace_files,
    _write_safe_text,
)
from shea_halo.runtime_fs import RuntimeFilesystem
from shea_halo.workspace import HaloWorkspace


def canonical_span(**updates: object) -> dict[str, object]:
    span: dict[str, object] = {
        "trace_id": "a" * 32,
        "span_id": "b" * 16,
        "parent_span_id": "",
        "trace_state": "",
        "name": "agent.run",
        "kind": "SPAN_KIND_INTERNAL",
        "start_time": "2026-07-24T00:00:00Z",
        "end_time": "2026-07-24T00:00:01Z",
        "status": {"code": "STATUS_CODE_OK", "message": ""},
        "resource": {"attributes": {"service.name": "test-agent"}},
        "scope": {"name": "test", "version": "1"},
        "attributes": {"openinference.span.kind": "AGENT"},
    }
    span.update(updates)
    return span


def test_canonical_flat_jsonl_manifest(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    path.write_text(json.dumps(canonical_span()) + "\n", encoding="utf-8")

    trace = _trace_file(path)

    assert trace.span_count == 1
    assert len(trace.sha256) == 64


@pytest.mark.parametrize(
    "logical_path",
    [
        "/private/tmp/traces.jsonl",
        "../traces.jsonl",
        "workspace:../traces.jsonl",
        "host:C:\\Users\\alice\\traces.jsonl",
        "workspace:",
    ],
)
def test_trace_manifest_rejects_non_logical_paths(
    tmp_path: Path,
    logical_path: str,
) -> None:
    path = tmp_path / "traces.jsonl"
    path.write_text(json.dumps(canonical_span()) + "\n", encoding="utf-8")

    with pytest.raises(HaloEngineError, match="relative or logically labelled"):
        _trace_file(path, logical_path=logical_path)


def test_otlp_envelope_is_not_misrepresented_as_engine_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "otlp.jsonl"
    path.write_text(json.dumps({"resourceSpans": []}) + "\n", encoding="utf-8")

    with pytest.raises(HaloEngineError, match="one-span"):
        _trace_file(path)


def test_canonical_jsonl_requires_halo_span_record_structure(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"
    invalid = canonical_span()
    invalid.pop("status")
    path.write_text(json.dumps(invalid) + "\n", encoding="utf-8")

    with pytest.raises(HaloEngineError, match="one-span"):
        _trace_file(path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("trace_id", "not-hex", "trace_id or span_id"),
        ("span_id", "z" * 16, "trace_id or span_id"),
        ("parent_span_id", "f" * 15, "parent_span_id"),
    ],
)
def test_canonical_jsonl_rejects_invalid_otel_ids(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    path = tmp_path / "traces.jsonl"
    path.write_text(json.dumps(canonical_span(**{field: value})) + "\n", encoding="utf-8")

    with pytest.raises(HaloEngineError, match=message):
        _trace_file(path)


def test_trace_glob_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "target"
    trace_dir = root / "traces"
    trace_dir.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps(canonical_span()) + "\n", encoding="utf-8")
    (trace_dir / "escaped.jsonl").symlink_to(outside)
    config = cast(
        HaloConfig,
        SimpleNamespace(
            root=root,
            observation=SimpleNamespace(trace_globs=("traces/*.jsonl",)),
        ),
    )

    with pytest.raises(HaloEngineError, match="outside the target repository"):
        _trace_files(config)


def test_halo_engine_environment_sets_and_restores_runtime_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CATALYST_OTLP_ENDPOINT", "https://previous.invalid")
    monkeypatch.setenv("CATALYST_SERVICE_NAME", "previous-service")
    monkeypatch.delenv("HALO_TELEMETRY_PATH", raising=False)
    config = cast(
        HaloConfig,
        SimpleNamespace(
            observation=SimpleNamespace(
                desktop_otlp_base_endpoint="http://127.0.0.1:8799",
            )
        ),
    )

    with _halo_engine_environment(config, tmp_path):
        assert os.environ["CATALYST_OTLP_ENDPOINT"] == "https://previous.invalid"
        assert os.environ["CATALYST_SERVICE_NAME"] == "shea-halo"
        assert os.environ["HALO_TELEMETRY_PATH"] == str(tmp_path / "halo-telemetry.jsonl")

    assert os.environ["CATALYST_OTLP_ENDPOINT"] == "https://previous.invalid"
    assert os.environ["CATALYST_SERVICE_NAME"] == "previous-service"
    assert "HALO_TELEMETRY_PATH" not in os.environ


def test_trace_discovery_includes_the_managed_experiment_worktree(
    tmp_path: Path,
) -> None:
    root = tmp_path / "target"
    worktree = tmp_path / "worktree"
    root.mkdir()
    worktree.mkdir()
    relative = Path(".shea/artifacts/halo/traces/trace.jsonl")
    trace = worktree / relative
    trace.parent.mkdir(parents=True)
    trace.write_text(json.dumps(canonical_span()) + "\n", encoding="utf-8")
    config = cast(
        HaloConfig,
        SimpleNamespace(
            root=root,
            observation=SimpleNamespace(trace_globs=(".shea/artifacts/halo/traces/*.jsonl",)),
        ),
    )

    assert _trace_files(config, worktree) == [trace.resolve()]


def test_candidate_analysis_selects_exact_trace_digests(tmp_path: Path) -> None:
    root = tmp_path / "target"
    worktree_path = tmp_path / "worktree"
    root.mkdir()
    worktree_path.mkdir()
    first = root / ".shea/artifacts/halo/traces/first.jsonl"
    second = worktree_path / ".shea/artifacts/halo/traces/second.jsonl"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text(json.dumps(canonical_span(span_id="1" * 16)) + "\n", encoding="utf-8")
    second.write_text(json.dumps(canonical_span(span_id="2" * 16)) + "\n", encoding="utf-8")
    config = cast(
        HaloConfig,
        SimpleNamespace(
            root=root,
            observation=SimpleNamespace(
                trace_globs=(".shea/artifacts/halo/traces/*.jsonl",),
            ),
        ),
    )
    workspace = HaloWorkspace(
        path=worktree_path,
        branch="halo/issue-26-tracing",
        base_revision="a" * 40,
    )
    second_digest = _trace_file(second).sha256

    assert _selected_trace_files(config, workspace, frozenset({second_digest})) == [
        second.resolve()
    ]
    with pytest.raises(HaloEngineError, match="unavailable"):
        _selected_trace_files(config, workspace, frozenset({"f" * 64}))


@pytest.mark.asyncio
async def test_analysis_persists_only_sanitized_logical_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from engine.models.engine_output import AgentOutputItem
    from engine.models.messages import AgentMessage

    root = tmp_path / "target"
    workspace_path = root / ".shea" / "worktrees" / "halo" / "issue-26"
    artifacts_dir = root / ".shea" / "artifacts" / "halo"
    trace_path = workspace_path / ".shea" / "artifacts" / "halo" / "traces" / "trace.jsonl"
    trace_path.parent.mkdir(parents=True)
    trace_path.write_text(json.dumps(canonical_span()) + "\n", encoding="utf-8")
    secret = "sk-halo-artifact-secret-value"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    stale_run_dir = artifacts_dir / "run-26" / "halo-engine"
    (stale_run_dir / "indexes").mkdir(parents=True)
    (stale_run_dir / "indexes" / "legacy.json").write_text(secret, encoding="utf-8")
    for name in ("analysis.json", "events.jsonl", "report.md", "halo-telemetry.jsonl"):
        (stale_run_dir / name).write_text(secret, encoding="utf-8")

    raw_paths: dict[str, str] = {}

    async def fake_stream(
        messages: list[AgentMessage],
        engine_config: object,
        trace_paths: list[Path],
        *,
        telemetry: bool,
    ) -> Any:
        del messages, engine_config, trace_paths
        assert telemetry
        telemetry_path = Path(os.environ["HALO_TELEMETRY_PATH"])
        raw_paths["telemetry"] = str(telemetry_path)
        telemetry_path.write_text(
            json.dumps(
                {
                    "api_key": secret,
                    "source": str(workspace_path / "src/agent.py"),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        yield AgentOutputItem(
            sequence=1,
            agent_id="halo",
            parent_agent_id=None,
            parent_tool_call_id=None,
            agent_name="Halo",
            depth=0,
            item=AgentMessage(
                role="assistant",
                content=(
                    f"Evidence in {workspace_path / 'src/agent.py'} used {secret}; "
                    f"temporary telemetry was {telemetry_path}"
                ),
            ),
            final=True,
        )

    monkeypatch.setattr("engine.main.stream_engine_output_async", fake_stream)
    monkeypatch.setattr(
        "shea_halo.halo_engine.workspace_manager_head",
        lambda workspace: "a" * 40,
    )
    config = cast(
        HaloConfig,
        SimpleNamespace(
            root=root,
            repository="Alive24/FailureReport",
            halo_model="gpt-5.6",
            artifacts_dir=artifacts_dir,
            observation=SimpleNamespace(
                trace_globs=(".shea/artifacts/halo/traces/*.jsonl",),
                desktop_otlp_base_endpoint="http://127.0.0.1:8799",
            ),
        ),
    )
    workspace = HaloWorkspace(
        path=workspace_path,
        branch="halo/issue-26-improve-tracing",
        base_revision="a" * 40,
    )

    artifact = await HaloEngineAdapter().analyze(
        config=config,
        workspace=workspace,
        run_id="run-26",
        issue_title=f"Inspect {root / 'agent-loop.py'}",
        issue_body=f"Do not persist {secret} from {workspace_path / 'debug.log'}",
    )

    assert artifact is not None
    run_dir = artifacts_dir / "run-26" / "halo-engine"
    manifest = json.loads((run_dir / "analysis.json").read_text(encoding="utf-8"))
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
    telemetry = (run_dir / "halo-telemetry.jsonl").read_text(encoding="utf-8")
    persisted = "\n".join([json.dumps(manifest), report, events, telemetry, artifact.final_message])

    assert secret not in persisted
    assert str(root) not in persisted
    assert str(workspace_path) not in persisted
    assert raw_paths["telemetry"] not in persisted
    assert "<workspace>/src/agent.py" in report
    assert "<engine-temporary>/halo-telemetry.jsonl" in report
    assert manifest["dataset"]["files"][0]["path"].startswith("workspace:")
    assert manifest["report_path"] == "report.md"
    assert manifest["events_path"] == "events.jsonl"
    assert "Sanitized issue input SHA-256:" in manifest["prompt"]
    assert "Do not persist" not in manifest["prompt"]
    event = json.loads(events)
    assert set(event) == {
        "agent_name",
        "content_sha256",
        "content_summary",
        "content_truncated",
        "depth",
        "final",
        "role",
        "sequence",
    }
    assert event["content_summary"] == report
    assert json.loads(telemetry)["api_key"] == "[REDACTED]"
    assert not (run_dir / "indexes").exists()


def test_missing_transient_telemetry_removes_stale_destination(tmp_path: Path) -> None:
    source = tmp_path / "missing.jsonl"
    destination = (
        tmp_path / ".shea" / "artifacts" / "halo" / "run-1" / "halo-engine" / "halo-telemetry.jsonl"
    )
    destination.parent.mkdir(parents=True)
    destination.write_text("stale secret", encoding="utf-8")

    _persist_safe_jsonl(source, destination, path_labels={tmp_path: "<artifacts>"})

    assert not destination.exists()


def test_halo_run_directory_rejects_symlink_component(tmp_path: Path) -> None:
    root = tmp_path / "target"
    artifacts = root / ".shea" / "artifacts" / "halo"
    artifacts.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (artifacts / "run-unsafe").symlink_to(outside, target_is_directory=True)
    config = cast(
        HaloConfig,
        SimpleNamespace(root=root, artifacts_dir=artifacts),
    )

    with pytest.raises(HaloEngineError, match="directory is unsafe"):
        _managed_run_dir(config, "run-unsafe", RuntimeFilesystem(root))

    assert list(outside.iterdir()) == []


def test_halo_stale_output_rejects_hardlink(tmp_path: Path) -> None:
    root = tmp_path / "target"
    root.mkdir()
    artifacts = root / ".shea" / "artifacts" / "halo"
    config = cast(
        HaloConfig,
        SimpleNamespace(root=root, artifacts_dir=artifacts),
    )
    runtime = RuntimeFilesystem(root)
    run_dir = _managed_run_dir(config, "run-hardlink", runtime)
    outside = tmp_path / "outside-report.md"
    outside.write_text("protected", encoding="utf-8")
    (run_dir / "report.md").hardlink_to(outside)

    with pytest.raises(HaloEngineError, match="unsafe stale artifact"):
        _clear_previous_outputs(runtime, run_dir)

    assert outside.read_text(encoding="utf-8") == "protected"


def test_halo_artifact_write_rejects_symlink_leaf(tmp_path: Path) -> None:
    root = tmp_path / "target"
    root.mkdir()
    artifacts = root / ".shea" / "artifacts" / "halo"
    config = cast(
        HaloConfig,
        SimpleNamespace(root=root, artifacts_dir=artifacts),
    )
    runtime = RuntimeFilesystem(root)
    run_dir = _managed_run_dir(config, "run-symlink", runtime)
    outside = tmp_path / "outside-report.md"
    outside.write_text("protected", encoding="utf-8")
    report = run_dir / "report.md"
    report.symlink_to(outside)

    with pytest.raises(HaloEngineError, match="path is unsafe"):
        _write_safe_text(
            report,
            "safe report",
            path_labels={root: "<target>"},
            runtime=runtime,
        )

    assert outside.read_text(encoding="utf-8") == "protected"
