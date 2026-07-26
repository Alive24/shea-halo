from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import requests
from google.protobuf.json_format import MessageToJson
from inference_catalyst_tracing import CatalystTracing, setup
from opentelemetry import trace
from opentelemetry.context import attach, detach
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, set_span_in_context
from pydantic import BaseModel, ConfigDict, Field, model_validator

from shea_halo.config import (
    HaloConfig,
    effective_catalyst_sdk_base_endpoint,
    validate_trace_locality_environment,
)
from shea_halo.runtime_fs import RuntimeFilesystem, RuntimeFilesystemError

if TYPE_CHECKING:
    from shea_halo.models import InvestigationResult


class ResearchTraceError(RuntimeError):
    """Raised when canonical local trace evidence cannot be persisted safely."""


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
_BUNDLE_SCHEMA = "shea-halo/research-trace-bundle-v1"
_MAX_REFERENCES = 50
_HALO_ARTIFACT_NAMES = (
    "analysis.json",
    "events.jsonl",
    "report.md",
    "halo-telemetry.jsonl",
)


class TraceFileReceipt(BaseModel):
    """Bounded digest receipt for one file referenced by a research trace bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(min_length=1, max_length=300)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    span_count: int | None = Field(default=None, ge=1)
    trace_count: int | None = Field(default=None, ge=1)


class ResearchTraceManifest(BaseModel):
    """Safe, raw-payload-free index for one canonical local research capture."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["shea-halo/research-trace-bundle-v1"] = _BUNDLE_SCHEMA
    classification: Literal["complete", "incomplete"]
    trace_bundle_id: str
    halo_run_id: str
    target_repository: str
    issue_number: int = Field(gt=0)
    candidate_revision: str | None = None
    service_name: str
    service_version: str
    observation_classification: str | None = None
    control_plane: TraceFileReceipt | None = None
    target_native_traces: tuple[TraceFileReceipt, ...] = ()
    halo_engine_artifacts: tuple[TraceFileReceipt, ...] = ()
    failure_classification: str | None = None
    finalized_at: datetime | None = None

    @model_validator(mode="after")
    def _classification_matches_content(self) -> ResearchTraceManifest:
        if self.classification == "complete":
            if self.control_plane is None or self.finalized_at is None:
                raise ValueError("complete trace bundles require a finalized control-plane file")
            if self.failure_classification is not None:
                raise ValueError("complete trace bundles cannot carry an incomplete classification")
        elif self.finalized_at is not None:
            raise ValueError("incomplete trace bundles cannot claim finalization")
        if len(self.target_native_traces) > _MAX_REFERENCES:
            raise ValueError("trace bundle contains too many target-native references")
        if len(self.halo_engine_artifacts) > _MAX_REFERENCES:
            raise ValueError("trace bundle contains too many HALO Engine references")
        return self


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    return str(value)


def _timestamp(nanoseconds: int | None) -> str:
    if nanoseconds is None:
        return ""
    seconds, remainder = divmod(nanoseconds, 1_000_000_000)
    base = datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{remainder:09d}Z"


def canonical_span_record(span: ReadableSpan) -> dict[str, Any]:
    """Project one ended SDK span into HALO Engine's public canonical record."""

    from engine.traces.models.canonical_span import SpanRecord

    context = span.context
    if context is None:
        raise ResearchTraceError("ended span has no span context")
    parent = span.parent
    scope = span.instrumentation_scope
    status = span.status
    record: dict[str, Any] = {
        "trace_id": trace.format_trace_id(context.trace_id),
        "span_id": trace.format_span_id(context.span_id),
        "parent_span_id": trace.format_span_id(parent.span_id) if parent is not None else "",
        "trace_state": context.trace_state.to_header(),
        "name": span.name,
        "kind": f"SPAN_KIND_{span.kind.name}",
        "start_time": _timestamp(span.start_time),
        "end_time": _timestamp(span.end_time),
        "status": {
            "code": f"STATUS_CODE_{status.status_code.name}",
            "message": status.description or "",
        },
        "resource": {"attributes": _json_value(span.resource.attributes)},
        "scope": {
            "name": scope.name if scope is not None else "",
            "version": (scope.version or "") if scope is not None else "",
        },
        "attributes": _json_value(span.attributes or {}),
        "events": [
            {
                "name": event.name,
                "timestamp": _timestamp(event.timestamp),
                "attributes": _json_value(event.attributes or {}),
            }
            for event in span.events
        ],
        "links": [
            {
                "trace_id": trace.format_trace_id(link.context.trace_id),
                "span_id": trace.format_span_id(link.context.span_id),
                "trace_state": link.context.trace_state.to_header(),
                "attributes": _json_value(link.attributes or {}),
            }
            for link in span.links
        ],
    }
    return SpanRecord.model_validate(record).model_dump(mode="json")


class _RunCapture:
    def __init__(
        self,
        *,
        config: HaloConfig,
        trace_bundle_id: str,
        halo_run_id: str,
        issue_number: int,
        service_name: str,
        service_version: str,
    ) -> None:
        self.config = config
        self.trace_bundle_id = trace_bundle_id
        self.halo_run_id = halo_run_id
        self.issue_number = issue_number
        self.service_name = service_name
        self.service_version = service_version
        self.runtime = RuntimeFilesystem(config.root)
        self.directory = config.artifacts_dir / "research-traces" / trace_bundle_id
        self.spool_path = self.directory / "control-plane.jsonl.incomplete"
        self.final_path = self.directory / "control-plane.jsonl"
        self.incomplete_manifest_path = self.directory / "manifest.incomplete.json"
        self.manifest_path = self.directory / "manifest.json"
        self.runtime.ensure_directory(self.directory)
        self.handle = self.runtime.open_append(self.spool_path)
        self.digest = hashlib.sha256()
        self.span_count = 0
        self.trace_ids: set[str] = set()
        self.failure_classification: str | None = None
        self.target_native_traces: tuple[TraceFileReceipt, ...] = ()
        self.candidate_revision: str | None = None
        self.observation_classification: str | None = None
        try:
            self._write_incomplete_manifest("active")
        except ResearchTraceError:
            self.handle.close()
            raise

    def _manifest(
        self,
        *,
        classification: Literal["complete", "incomplete"],
        control_plane: TraceFileReceipt | None = None,
        failure_classification: str | None = None,
    ) -> ResearchTraceManifest:
        return ResearchTraceManifest(
            classification=classification,
            trace_bundle_id=self.trace_bundle_id,
            halo_run_id=self.halo_run_id,
            target_repository=self.config.repository,
            issue_number=self.issue_number,
            candidate_revision=self.candidate_revision,
            service_name=self.service_name,
            service_version=self.service_version,
            observation_classification=self.observation_classification,
            control_plane=control_plane,
            target_native_traces=self.target_native_traces,
            halo_engine_artifacts=self._halo_artifact_receipts(),
            failure_classification=failure_classification,
            finalized_at=datetime.now(UTC) if classification == "complete" else None,
        )

    def _write_manifest(self, path: Path, manifest: ResearchTraceManifest) -> None:
        rendered = manifest.model_dump_json(indent=2)
        self.runtime.atomic_write_text(path, f"{rendered}\n")

    def _write_incomplete_manifest(self, classification: str) -> None:
        try:
            self._write_manifest(
                self.incomplete_manifest_path,
                self._manifest(
                    classification="incomplete",
                    failure_classification=classification,
                ),
            )
        except (OSError, RuntimeFilesystemError, ValueError) as exc:
            self.failure_classification = type(exc).__name__
            raise ResearchTraceError("could not persist incomplete trace classification") from None

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self.failure_classification is not None:
            return SpanExportResult.FAILURE
        try:
            for span in spans:
                record = canonical_span_record(span)
                rendered = json.dumps(
                    record,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                payload = f"{rendered}\n".encode("utf-8")
                self.handle.write(payload.decode("utf-8"))
                self.handle.flush()
                os.fsync(self.handle.fileno())
                self.digest.update(payload)
                self.span_count += 1
                self.trace_ids.add(record["trace_id"])
        except Exception as exc:
            self.failure_classification = type(exc).__name__
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def attach_result(self, result: InvestigationResult) -> None:
        validation = result.trace_validation
        if validation is not None:
            self.candidate_revision = validation.candidate_revision
            self.target_native_traces = tuple(
                TraceFileReceipt(
                    label=trace_file.label,
                    sha256=trace_file.sha256,
                    size_bytes=trace_file.size_bytes,
                    span_count=trace_file.span_count,
                    trace_count=trace_file.trace_count,
                )
                for trace_file in validation.candidate_traces[:_MAX_REFERENCES]
            )
        receipt = result.desktop_preflight
        if receipt is not None:
            self.observation_classification = receipt.classification.value

    def _halo_artifact_receipts(self) -> tuple[TraceFileReceipt, ...]:
        receipts: list[TraceFileReceipt] = []
        for logical_run in (self.halo_run_id, f"{self.halo_run_id}-candidate"):
            directory = self.config.artifacts_dir / logical_run / "halo-engine"
            for name in _HALO_ARTIFACT_NAMES:
                path = directory / name
                try:
                    content = self.runtime.read_text(path).encode("utf-8")
                except FileNotFoundError:
                    continue
                except (OSError, RuntimeFilesystemError, UnicodeError):
                    continue
                receipts.append(
                    TraceFileReceipt(
                        label=f"halo-engine/{logical_run}/{name}",
                        sha256=hashlib.sha256(content).hexdigest(),
                        size_bytes=len(content),
                    )
                )
                if len(receipts) == _MAX_REFERENCES:
                    return tuple(receipts)
        return tuple(receipts)

    def _close(self) -> None:
        if not self.handle.closed:
            self.handle.flush()
            os.fsync(self.handle.fileno())
            self.handle.close()

    def finalize(self) -> Path:
        try:
            self._close()
        except OSError as exc:
            self.failure_classification = type(exc).__name__
        if self.failure_classification is not None:
            self.retain_incomplete(self.failure_classification)
            raise ResearchTraceError("canonical local trace export failed")
        if self.span_count == 0:
            self.retain_incomplete("empty_capture")
            raise ResearchTraceError("canonical local trace capture contains no spans")
        receipt = TraceFileReceipt(
            label="control-plane.jsonl",
            sha256=self.digest.hexdigest(),
            size_bytes=self.spool_path.stat().st_size,
            span_count=self.span_count,
            trace_count=len(self.trace_ids),
        )
        manifest = self._manifest(classification="complete", control_plane=receipt)
        try:
            self.runtime.finalize_file(self.spool_path, self.final_path)
            self._write_manifest(self.manifest_path, manifest)
            self.runtime.remove_file(self.incomplete_manifest_path)
        except (OSError, RuntimeFilesystemError) as exc:
            self.failure_classification = type(exc).__name__
            self.retain_incomplete(self.failure_classification)
            raise ResearchTraceError("canonical trace bundle finalization failed") from None
        return self.manifest_path

    def retain_incomplete(self, classification: str) -> None:
        try:
            self._close()
        except OSError:
            classification = "close_failure"
        try:
            self._write_incomplete_manifest(classification)
        except ResearchTraceError:
            pass


class CanonicalJsonlSpanExporter(SpanExporter):
    """Routes the same in-process ReadableSpan instances into one active run spool."""

    def __init__(self) -> None:
        self._active: _RunCapture | None = None
        self._lock = threading.RLock()

    @property
    def active(self) -> _RunCapture | None:
        with self._lock:
            return self._active

    def begin(self, capture: _RunCapture) -> None:
        with self._lock:
            if self._active is not None:
                raise ResearchTraceError("a research trace capture is already active")
            self._active = capture

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._lock:
            if self._active is None:
                return SpanExportResult.SUCCESS
            return self._active.export(spans)

    def release(self, capture: _RunCapture) -> None:
        with self._lock:
            if self._active is capture:
                self._active = None

    def shutdown(self) -> None:
        with self._lock:
            capture = self._active
            self._active = None
        if capture is not None:
            capture.retain_incomplete("worker_shutdown")

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        return self.active is None or self.active.failure_classification is None


class LoopbackOtlpJsonSpanExporter(SpanExporter):
    """Send OTLP/JSON to the validated loopback observer without following redirects."""

    def __init__(self, endpoint: str, *, timeout_seconds: float = 10.0) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        body = MessageToJson(encode_spans(spans)).encode("utf-8")
        try:
            response = requests.post(
                self.endpoint,
                data=body,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException:
            return SpanExportResult.FAILURE
        return (
            SpanExportResult.SUCCESS
            if 200 <= response.status_code < 300
            else SpanExportResult.FAILURE
        )

    def shutdown(self) -> None:
        return None


class ResearchTracing:
    """Worker-owned Catalyst provider plus run-scoped canonical local capture."""

    def __init__(
        self,
        *,
        handle: CatalystTracing | None,
        local_exporter: CanonicalJsonlSpanExporter,
        service_version: str,
    ) -> None:
        self._handle = handle
        self._local_exporter = local_exporter
        self.service_version = service_version
        self.tracer = (
            handle.tracer
            if handle is not None
            else trace.get_tracer("shea-halo.disabled", service_version)
        )
        self._root_span: Span | None = None
        self._root_context_token: Any | None = None
        self._capture: _RunCapture | None = None

    @classmethod
    def disabled(cls) -> ResearchTracing:
        return cls(
            handle=None,
            local_exporter=CanonicalJsonlSpanExporter(),
            service_version=version("shea-halo"),
        )

    @classmethod
    def initialize(cls, configs: Sequence[HaloConfig]) -> ResearchTracing:
        if not configs:
            raise ResearchTraceError("at least one target config is required for tracing")
        validate_trace_locality_environment()
        endpoints = {
            effective_catalyst_sdk_base_endpoint(config.observation.catalyst_sdk_base_endpoint)
            for config in configs
        }
        if len(endpoints) != 1:
            raise ResearchTraceError("one worker process requires one loopback OTLP endpoint")
        service_version = version("shea-halo")
        observer_endpoint = endpoints.pop()
        # setup() has no exporter-disable switch and its pinned HTTP exporter
        # follows redirects. Keep native provider/instrumentation initialization,
        # but contain that built-in transport at a closed literal-loopback sink.
        handle = setup(
            endpoint="http://127.0.0.1:0",
            service_name="shea-halo",
            service_version=service_version,
        )
        handle.provider.add_span_processor(
            BatchSpanProcessor(
                LoopbackOtlpJsonSpanExporter(f"{observer_endpoint.rstrip('/')}/v1/traces")
            )
        )
        local_exporter = CanonicalJsonlSpanExporter()
        handle.provider.add_span_processor(SimpleSpanProcessor(local_exporter))
        return cls(
            handle=handle,
            local_exporter=local_exporter,
            service_version=service_version,
        )

    @property
    def enabled(self) -> bool:
        return self._handle is not None

    @property
    def active(self) -> bool:
        return self._capture is not None

    def begin_run(
        self,
        config: HaloConfig,
        *,
        halo_run_id: str,
        issue_number: int,
    ) -> str | None:
        if not self.enabled:
            return None
        if not _SAFE_ID.fullmatch(halo_run_id):
            raise ResearchTraceError("Halo run id is unsafe for local trace correlation")
        trace_bundle_id = f"{halo_run_id}-{uuid.uuid4().hex[:12]}"
        capture = _RunCapture(
            config=config,
            trace_bundle_id=trace_bundle_id,
            halo_run_id=halo_run_id,
            issue_number=issue_number,
            service_name=self._handle.service_name if self._handle is not None else "shea-halo",
            service_version=self.service_version,
        )
        self._local_exporter.begin(capture)
        root = self.tracer.start_span(
            "shea-halo.research",
            kind=SpanKind.INTERNAL,
            attributes={
                "openinference.span.kind": "CHAIN",
                "shea.halo.run_id": halo_run_id,
                "shea.trace.bundle_id": trace_bundle_id,
                "shea.target.repository": config.repository,
                "shea.issue.number": issue_number,
                "shea.service.version": self.service_version,
            },
        )
        self._root_context_token = attach(set_span_in_context(root))
        self._root_span = root
        self._capture = capture
        return trace_bundle_id

    @contextmanager
    def stage(
        self,
        name: str,
        *,
        attributes: Mapping[str, str | bool | int | float] | None = None,
    ) -> Iterator[Span | None]:
        if not self.active:
            yield None
            return
        span = self.tracer.start_span(
            f"shea-halo.{name}",
            kind=SpanKind.INTERNAL,
            attributes={
                "shea.stage": name,
                **dict(attributes or {}),
            },
        )
        token = attach(set_span_in_context(span))
        try:
            yield span
        except BaseException as exc:
            self._mark_span_error(span, exc)
            raise
        finally:
            span.end()
            detach(token)

    @staticmethod
    def _mark_span_error(span: Span, exc: BaseException) -> None:
        classification = (
            "cancelled" if isinstance(exc, asyncio.CancelledError) else type(exc).__name__
        )
        span.set_attribute("shea.error.classification", classification)
        span.set_status(Status(StatusCode.ERROR, "stage failed"))

    def attach_result(self, result: InvestigationResult) -> None:
        if self._capture is not None:
            self._capture.attach_result(result)
        if self._root_span is not None:
            self._root_span.set_attribute("shea.outcome", result.outcome.value)
            validation = result.trace_validation
            if validation is not None and validation.candidate_revision is not None:
                self._root_span.set_attribute(
                    "shea.candidate.revision",
                    validation.candidate_revision,
                )

    def finalize_run(self, outcome: str = "recorded") -> Path | None:
        capture = self._capture
        if capture is None:
            return None
        root = self._root_span
        token = self._root_context_token
        try:
            if root is not None:
                root.set_attribute("shea.run.disposition", outcome)
                root.end()
            if token is not None:
                detach(token)
            return capture.finalize()
        finally:
            self._local_exporter.release(capture)
            self._capture = None
            self._root_span = None
            self._root_context_token = None

    def abort_run(self, exc: BaseException) -> None:
        capture = self._capture
        if capture is None:
            return
        root = self._root_span
        token = self._root_context_token
        try:
            if root is not None:
                self._mark_span_error(root, exc)
                root.end()
            if token is not None:
                detach(token)
            classification = (
                "cancelled" if isinstance(exc, asyncio.CancelledError) else type(exc).__name__
            )
            capture.retain_incomplete(classification)
        finally:
            self._local_exporter.release(capture)
            self._capture = None
            self._root_span = None
            self._root_context_token = None

    def discard_unchanged_scan(self) -> None:
        """Remove a polling scan that determined no research run was needed."""

        capture = self._capture
        if capture is None:
            return
        root = self._root_span
        token = self._root_context_token
        try:
            if root is not None:
                root.set_attribute("shea.run.disposition", "skipped_unchanged")
                root.end()
            if token is not None:
                detach(token)
            capture._close()
            capture.runtime.remove_tree(capture.directory)
        except (OSError, RuntimeFilesystemError):
            capture.retain_incomplete("unchanged_scan_cleanup_failure")
            raise ResearchTraceError("unchanged trace scan could not be discarded") from None
        finally:
            self._local_exporter.release(capture)
            self._capture = None
            self._root_span = None
            self._root_context_token = None

    def shutdown(self) -> None:
        if self.active:
            self.abort_run(RuntimeError("worker shutdown interrupted an active trace capture"))
        if self._handle is not None:
            self._handle.shutdown()
