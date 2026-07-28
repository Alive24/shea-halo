from __future__ import annotations

import asyncio
import json
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from conftest import write_target_config
from engine.traces.models.canonical_span import SpanRecord
from inference_catalyst_tracing import CatalystTracing
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

from shea_halo.config import HaloConfig
from shea_halo.models import InvestigationResult, Outcome
from shea_halo.observation import (
    ObservationCheckpoint,
    SpanKindCount,
    TraceObservation,
    TraceValidation,
)
from shea_halo.tracing import (
    LoopbackOtlpJsonSpanExporter,
    ResearchTraceError,
    ResearchTracing,
)


class _ObserverExporter(SpanExporter):
    def __init__(self, result: SpanExportResult = SpanExportResult.SUCCESS) -> None:
        self.spans: list[ReadableSpan] = []
        self.result = result

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
        return self.result

    def shutdown(self) -> None:
        return None


def _initialize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    observer_result: SpanExportResult = SpanExportResult.SUCCESS,
) -> tuple[HaloConfig, ResearchTracing, _ObserverExporter, list[dict[str, object]]]:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)
    observer = _ObserverExporter(observer_result)
    calls: list[dict[str, object]] = []

    def fake_setup(**kwargs: object) -> CatalystTracing:
        calls.append(kwargs)
        service_name = cast(str, kwargs["service_name"])
        service_version = cast(str, kwargs["service_version"])
        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": service_name,
                    "service.version": service_version,
                }
            )
        )
        provider.add_span_processor(SimpleSpanProcessor(observer))
        return CatalystTracing(
            provider=provider,
            tracer=provider.get_tracer("catalyst"),
            service_name=service_name,
            endpoint=cast(str, kwargs["endpoint"]),
            installed=[],
        )

    monkeypatch.setattr("shea_halo.tracing.setup", fake_setup)
    tracing = ResearchTracing.initialize([config])
    return config, tracing, observer, calls


def _records(config: HaloConfig) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    [bundle] = (config.artifacts_dir / "research-traces").iterdir()
    records = [
        json.loads(line)
        for line in (bundle / "control-plane.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    return bundle, records, manifest


def test_one_worker_setup_fans_out_identical_span_identities_to_otlp_and_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, tracing, observer, calls = _initialize(tmp_path, monkeypatch)

    tracing.begin_run(config, halo_run_id="halo-26-one", issue_number=26)
    with tracing.stage("workspace-preparation") as span:
        assert span is not None
        span.set_attribute("test.safe", "value")
        span.add_event("prepared", {"count": 1})
    tracing.finalize_run("continue_research")

    _, records, manifest = _records(config)
    by_name = {cast(str, record["name"]): record for record in records}
    observer_by_name = {span.name: span for span in observer.spans}
    assert len(calls) == 1
    assert set(by_name) == {"shea-halo.research", "shea-halo.workspace-preparation"}
    for name, record in by_name.items():
        observed = observer_by_name[name]
        assert observed.context is not None
        assert record["trace_id"] == format(observed.context.trace_id, "032x")
        assert record["span_id"] == format(observed.context.span_id, "016x")
        assert record["parent_span_id"] == (
            format(observed.parent.span_id, "016x") if observed.parent is not None else ""
        )
        assert record["attributes"] == dict(observed.attributes or {})
        assert record["resource"]["attributes"]["service.name"] == "shea-halo"
        SpanRecord.model_validate(record)
    assert by_name["shea-halo.workspace-preparation"]["events"][0]["name"] == "prepared"
    assert manifest["classification"] == "complete"
    assert manifest["control_plane"]["span_count"] == 2
    assert manifest["service_name"] == "shea-halo"
    bundle = config.artifacts_dir / "research-traces" / manifest["trace_bundle_id"]
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o700
    assert stat.S_IMODE((bundle / "control-plane.jsonl").stat().st_mode) == 0o600
    assert stat.S_IMODE((bundle / "manifest.json").stat().st_mode) == 0o600
    assert (
        not (config.artifacts_dir / "research-traces")
        .joinpath(manifest["trace_bundle_id"], "manifest.incomplete.json")
        .exists()
    )
    tracing.shutdown()


def test_multiple_runs_share_provider_but_get_distinct_roots_and_bundles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, tracing, observer, calls = _initialize(tmp_path, monkeypatch)

    for issue_number in (26, 27):
        tracing.begin_run(
            config,
            halo_run_id=f"halo-{issue_number}-run",
            issue_number=issue_number,
        )
        tracing.finalize_run("recorded")

    bundles = sorted((config.artifacts_dir / "research-traces").iterdir())
    roots = [span for span in observer.spans if span.name == "shea-halo.research"]
    assert all(span.context is not None for span in roots)
    assert len(calls) == 1
    assert len(bundles) == 2
    assert len({span.context.trace_id for span in roots if span.context is not None}) == 2
    assert {
        json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))["issue_number"]
        for bundle in bundles
    } == {26, 27}
    tracing.shutdown()


def test_unchanged_polling_scan_does_not_leave_a_research_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, tracing, observer, _ = _initialize(tmp_path, monkeypatch)
    tracing.begin_run(config, halo_run_id="halo-26-unchanged", issue_number=26)

    tracing.discard_unchanged_scan()

    bundles_root = config.artifacts_dir / "research-traces"
    assert list(bundles_root.iterdir()) == []
    assert any(span.name == "shea-halo.research" for span in observer.spans)
    tracing.shutdown()


def test_manifest_correlates_target_and_engine_artifacts_without_raw_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, tracing, _, _ = _initialize(tmp_path, monkeypatch)
    run_id = "halo-26-correlated"
    target_trace = TraceObservation(
        label="worktree/.shea/artifacts/halo/traces/candidate.jsonl",
        sha256="a" * 64,
        size_bytes=321,
        span_count=2,
        trace_count=1,
        parented_span_count=1,
        span_kinds=(SpanKindCount(kind="AGENT", count=2),),
        service_version_span_count=2,
        service_versions=("b" * 40,),
    )
    result = InvestigationResult(
        outcome=Outcome.CONTINUE_RESEARCH,
        summary="Raw prompt must stay out of the manifest.",
        trace_validation=TraceValidation(
            checkpoint=ObservationCheckpoint(traces=(target_trace,)),
            candidate_traces=(target_trace,),
            candidate_revision="b" * 40,
        ),
    )
    engine_dir = config.artifacts_dir / run_id / "halo-engine"
    engine_dir.mkdir(parents=True)
    (engine_dir / "report.md").write_text("raw prompt and response", encoding="utf-8")

    tracing.begin_run(config, halo_run_id=run_id, issue_number=26)
    with tracing.stage("investigation") as span:
        assert span is not None
        span.set_attribute("input.value", "raw prompt and response")
    tracing.attach_result(result)
    tracing.finalize_run("continue_research")

    _, records, manifest = _records(config)
    rendered_manifest = json.dumps(manifest)
    assert manifest["candidate_revision"] == "b" * 40
    assert manifest["target_native_traces"][0]["sha256"] == "a" * 64
    assert manifest["halo_engine_artifacts"][0]["label"].endswith("/report.md")
    assert "raw prompt and response" not in rendered_manifest
    assert str(tmp_path) not in rendered_manifest
    assert any(
        record["attributes"].get("input.value") == "raw prompt and response" for record in records
    )
    tracing.shutdown()


def test_missing_optional_observer_does_not_invalidate_local_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, tracing, observer, _ = _initialize(
        tmp_path,
        monkeypatch,
        observer_result=SpanExportResult.FAILURE,
    )

    tracing.begin_run(config, halo_run_id="halo-26-offline", issue_number=26)
    tracing.finalize_run("recorded")

    _, records, manifest = _records(config)
    assert observer.spans
    assert records
    assert manifest["classification"] == "complete"
    tracing.shutdown()


def test_interruption_retains_only_explicit_incomplete_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, tracing, _, _ = _initialize(tmp_path, monkeypatch)

    tracing.begin_run(config, halo_run_id="halo-26-interrupted", issue_number=26)
    with tracing.stage("configured-runtime-experiments"):
        pass
    tracing.abort_run(RuntimeError("interrupted"))

    [bundle] = (config.artifacts_dir / "research-traces").iterdir()
    manifest = json.loads((bundle / "manifest.incomplete.json").read_text(encoding="utf-8"))
    assert manifest["classification"] == "incomplete"
    assert manifest["failure_classification"] == "RuntimeError"
    assert not (bundle / "manifest.json").exists()
    assert not (bundle / "control-plane.jsonl").exists()
    tracing.shutdown()


@pytest.mark.parametrize("error", [RuntimeError("tool failed"), asyncio.CancelledError()])
def test_stage_errors_and_cancellation_are_classified_without_raw_error_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    config, tracing, _, _ = _initialize(tmp_path, monkeypatch)
    tracing.begin_run(config, halo_run_id="halo-26-stage-failure", issue_number=26)

    with pytest.raises(type(error)):
        with tracing.stage("configured-runtime-experiments"):
            raise error
    tracing.abort_run(error)

    [bundle] = (config.artifacts_dir / "research-traces").iterdir()
    records = [
        json.loads(line)
        for line in (bundle / "control-plane.jsonl.incomplete")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    stage = next(record for record in records if record["name"].endswith("experiments"))
    expected = "cancelled" if isinstance(error, asyncio.CancelledError) else "RuntimeError"
    assert stage["attributes"]["shea.error.classification"] == expected
    assert stage["status"]["code"] == "STATUS_CODE_ERROR"
    if str(error):
        assert str(error) not in json.dumps(stage)
    tracing.shutdown()


def test_local_exporter_failure_cannot_finalize_canonical_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, tracing, _, _ = _initialize(tmp_path, monkeypatch)

    tracing.begin_run(config, halo_run_id="halo-26-export-failure", issue_number=26)
    capture = tracing._local_exporter.active
    assert capture is not None
    capture.handle.close()
    with tracing.stage("workspace-preparation"):
        pass

    with pytest.raises(ResearchTraceError, match="export failed"):
        tracing.finalize_run("recorded")

    [bundle] = (config.artifacts_dir / "research-traces").iterdir()
    manifest = json.loads((bundle / "manifest.incomplete.json").read_text(encoding="utf-8"))
    assert manifest["classification"] == "incomplete"
    assert not (bundle / "manifest.json").exists()
    tracing.shutdown()


def test_loopback_observer_exporter_never_follows_redirects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, tracing, observer, _ = _initialize(tmp_path, monkeypatch)
    tracing.begin_run(config, halo_run_id="halo-26-redirect", issue_number=26)
    tracing.finalize_run("recorded")
    assert observer.spans
    request: dict[str, object] = {}

    def post(endpoint: str, **kwargs: object) -> object:
        request["endpoint"] = endpoint
        request.update(kwargs)
        return type("Response", (), {"status_code": 307})()

    monkeypatch.setattr("shea_halo.tracing.requests.post", post)
    exporter = LoopbackOtlpJsonSpanExporter("http://127.0.0.1:8799/v1/traces")

    assert exporter.export([observer.spans[0]]) == SpanExportResult.FAILURE
    assert request["allow_redirects"] is False
    assert request["endpoint"] == "http://127.0.0.1:8799/v1/traces"
    tracing.shutdown()
