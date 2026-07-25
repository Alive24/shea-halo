from __future__ import annotations

import json
import os
import sys
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
    _prebuild_trace_indexes_when_process_pool_unavailable,
    _selected_trace_files,
    _trace_file,
    _trace_files,
    _trace_index_process_pool_available,
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


def test_trace_index_process_pool_is_unavailable_when_semaphore_query_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(_name: str) -> int:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "sysconf", denied)

    assert not _trace_index_process_pool_available()


def test_trace_index_keeps_parallel_path_when_semaphores_are_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "sysconf", lambda _name: 256)

    assert _trace_index_process_pool_available()


@pytest.mark.parametrize(("limit", "available"), [(-1, True), (255, False), (256, True)])
def test_trace_index_process_pool_respects_reported_semaphore_limit(
    limit: int,
    available: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "sysconf", lambda _name: limit)

    assert _trace_index_process_pool_available() is available


def test_trace_index_process_pool_propagates_unexpected_sysconf_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(_name: str) -> int:
        raise OSError(5, "I/O error")

    monkeypatch.setattr(os, "sysconf", failed)

    with pytest.raises(OSError, match="I/O error"):
        _trace_index_process_pool_available()


@pytest.mark.parametrize("error", [AttributeError("missing"), ValueError("unsupported")])
def test_trace_index_process_pool_accepts_unavailable_sysconf_query(
    error: Exception,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(_name: str) -> int:
        raise error

    monkeypatch.setattr(os, "sysconf", unavailable)

    assert _trace_index_process_pool_available()


@pytest.mark.asyncio
async def test_trace_index_serial_fallback_does_not_mutate_halo_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from engine.traces.models.trace_index_config import TraceIndexConfig
    from engine.traces.trace_index_builder import TraceIndexBuilder

    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps(canonical_span()) + "\n", encoding="utf-8")
    seen_thresholds: list[int] = []

    async def capture_threshold(
        cls: type[TraceIndexBuilder],
        trace_path: Path,
        config: TraceIndexConfig,
    ) -> Path:
        del trace_path, config
        seen_thresholds.append(cls.SMALL_FILE_THRESHOLD)
        return tmp_path / "index.jsonl"

    monkeypatch.setattr(
        "shea_halo.halo_engine._trace_index_process_pool_available",
        lambda: False,
    )
    monkeypatch.setattr(
        TraceIndexBuilder,
        "ensure_index_exists",
        classmethod(capture_threshold),
    )
    original_threshold = TraceIndexBuilder.SMALL_FILE_THRESHOLD

    used_fallback = await _prebuild_trace_indexes_when_process_pool_unavailable(
        [trace],
        TraceIndexConfig(index_dir=tmp_path / "indexes"),
    )

    assert used_fallback
    assert seen_thresholds == [sys.maxsize]
    assert TraceIndexBuilder.SMALL_FILE_THRESHOLD == original_threshold


@pytest.mark.asyncio
async def test_serial_fallback_sidecar_is_reused_by_halo_engine_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from engine.traces.models.trace_index_config import TraceIndexConfig
    from engine.traces.trace_index_builder import TraceIndexBuilder

    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        "".join(
            json.dumps(canonical_span(span_id=f"{index:016x}")) + "\n" for index in range(1_001)
        ),
        encoding="utf-8",
    )
    config = TraceIndexConfig(index_dir=tmp_path / "indexes")
    monkeypatch.setattr(
        "shea_halo.halo_engine._trace_index_process_pool_available",
        lambda: False,
    )

    assert await _prebuild_trace_indexes_when_process_pool_unavailable([trace], config)

    async def forbidden_rebuild(
        cls: type[TraceIndexBuilder],
        trace_path: Path,
    ) -> list[object]:
        del cls, trace_path
        raise AssertionError("base HALO builder attempted to rebuild the serial sidecar")

    monkeypatch.setattr(TraceIndexBuilder, "_run_build", classmethod(forbidden_rebuild))

    index_path = await TraceIndexBuilder.ensure_index_exists(trace, config)

    assert index_path.is_file()
    assert len(index_path.read_text(encoding="utf-8").splitlines()) == 1


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
                catalyst_sdk_base_endpoint="http://127.0.0.1:8799",
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
    configured_api_modes: list[str] = []
    monkeypatch.setattr(
        "shea_halo.provider.set_default_openai_api",
        configured_api_modes.append,
    )
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
            api_mode="chat_completions",
            artifacts_dir=artifacts_dir,
            observation=SimpleNamespace(
                trace_globs=(".shea/artifacts/halo/traces/*.jsonl",),
                catalyst_sdk_base_endpoint="http://127.0.0.1:8799",
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
    assert artifact.api_mode == "chat_completions"
    assert configured_api_modes == ["chat_completions"]
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
    assert manifest["api_mode"] == "chat_completions"
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
