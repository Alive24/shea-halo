from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from conftest import write_target_config

from shea_halo.agent import Investigator, candidate_evidence_references
from shea_halo.config import HaloConfig
from shea_halo.github import GitHubClient, IssueComment, ProjectIssue, ProjectMetadata
from shea_halo.halo_engine import HaloAnalysisArtifact, HaloEngineAdapter
from shea_halo.locking import target_worker_lock
from shea_halo.models import (
    AgentDecision,
    BranchSnapshot,
    Evidence,
    ExecutedAction,
    Experiment,
    ExternalBlocker,
    GuideSnapshot,
    HaloDesktopPreflightClassification,
    HaloDesktopPreflightReceipt,
    HaloDesktopPreflightResult,
    HaloState,
    InvestigationResult,
    Outcome,
    PublicationIntent,
    SnapshotPath,
    StatusTransition,
    Verification,
)
from shea_halo.observation import (
    FileObservation,
    ObservationCheckpoint,
    SpanKindCount,
    TraceObservation,
    TraceValidation,
)
from shea_halo.provider import ProviderApiModeError
from shea_halo.runtime_fs import RuntimeFilesystemError
from shea_halo.service import (
    HaloService,
    ServiceError,
    _provider_api_mode_blocker,
    _runtime_source_sha256,
)
from shea_halo.workpad import WorkpadHead, find_head, next_entry, render_entry
from shea_halo.workspace import HaloWorkspace, WorkspaceManager


def _successful_action() -> ExecutedAction:
    return ExecutedAction(
        index=1,
        kind="experiment",
        stage="candidate_validation",
        command=["pnpm", "test"],
        exit_code=0,
        stdout_sha256="b" * 64,
        stderr_sha256="c" * 64,
        service_version="1" * 40,
    )


def _ready_result(*, stale_guide: bool = False) -> InvestigationResult:
    trace = TraceObservation(
        label="worktree/.shea/artifacts/halo/traces/candidate.jsonl",
        sha256="d" * 64,
        size_bytes=100,
        span_count=2,
        trace_count=1,
        parented_span_count=1,
        span_kinds=(
            SpanKindCount(kind="AGENT", count=1),
            SpanKindCount(kind="TOOL", count=1),
        ),
        service_version_span_count=2,
        service_versions=("1" * 40,),
    )
    report = FileObservation(
        label="target/.shea/artifacts/halo/desktop/run/report.md",
        sha256="e" * 64,
        size_bytes=100,
    )
    return InvestigationResult(
        outcome=Outcome.READY_FOR_TODO,
        summary="ready",
        evidence=[Evidence(claim="The native integration closes the gap.", source="trace:1")],
        analysis_sha256="9" * 64,
        experiments=[
            Experiment(
                action_index=1,
                hypothesis="Native tracing preserves Eve hierarchy.",
                action="Run the instrumented fixture.",
                result="The expected spans were present.",
            )
        ],
        selected_guides=[
            GuideSnapshot(
                title="Eve",
                url="https://docs.inference.net/integrations/traces/eve",
                retrieved_at=datetime.now(UTC),
                sha256="a" * 64,
                stale=stale_guide,
                framework_evidence=["eve@0.24.4"],
                selected_because="The target is an Eve agent.",
            )
        ],
        changed_paths=["eve/agent/instrumentation.ts"],
        trace_validation_required=True,
        trace_validation=TraceValidation(
            checkpoint=ObservationCheckpoint(
                traces=(trace,),
                desktop_reports=(report,),
            ),
            candidate_traces=(trace,),
            candidate_desktop_reports=(report,),
            halo_report_sha256="f" * 64,
            halo_dataset_verified=True,
            halo_revision_verified=True,
            service_version_verified=True,
            candidate_revision="1" * 40,
            complete=True,
        ),
        executed_actions=[_successful_action()],
        verification=[
            Verification(
                command=["pnpm", "test"],
                status="passed",
                evidence="All tests passed.",
            )
        ],
        todo_handoff="Selectively adopt the verified instrumentation.",
    )


def test_routes_only_completed_research_to_symphony_todo(tmp_path: Path) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)

    ready_result = HaloService._gate_outcome(_ready_result())
    ready = HaloService._route(config, ready_result)
    continuing = HaloService._route(
        config,
        InvestigationResult(
            outcome=Outcome.CONTINUE_RESEARCH,
            summary="more evidence needed",
        ),
    )

    assert ready[:2] == ("ready", "Todo")
    assert continuing[:2] == ("research", None)


def test_runtime_source_digest_is_stable_and_tracks_code_changes(tmp_path: Path) -> None:
    package = tmp_path / "shea_halo"
    nested = package / "nested"
    nested.mkdir(parents=True)
    first = package / "first.py"
    second = nested / "second.py"
    first.write_text("FIRST = 1\n", encoding="utf-8")
    second.write_text("SECOND = 2\n", encoding="utf-8")

    initial = _runtime_source_sha256(package)
    assert initial == _runtime_source_sha256(package)

    second.write_text("SECOND = 3\n", encoding="utf-8")

    assert initial != _runtime_source_sha256(package)


def test_halo_state_rejects_incoherent_lifecycle_payloads() -> None:
    common = {
        "issue_repository": "Alive24/FailureReport",
        "issue_number": 26,
        "run_id": "halo-26-state",
        "workspace_branch": "halo/issue-26-tracing",
    }
    with pytest.raises(ValueError, match="validation_baseline"):
        HaloState(
            **common,
            phase="validating",
            base_revision="a" * 40,
            result=InvestigationResult(
                outcome=Outcome.CONTINUE_RESEARCH,
                summary="Validating.",
            ),
        )
    with pytest.raises(ValueError, match="publication intent"):
        HaloState(
            **common,
            phase="research",
            base_revision="a" * 40,
            publication_intent=PublicationIntent(
                branch=common["workspace_branch"],
                parent_revision="a" * 40,
                manifest_sha256="b" * 64,
                paths=[
                    SnapshotPath(
                        path="instrumentation.ts",
                        state="file",
                        sha256="c" * 64,
                        size=1,
                    )
                ],
            ),
            result=InvestigationResult(
                outcome=Outcome.CONTINUE_RESEARCH,
                summary="Publishing.",
            ),
        )
    with pytest.raises(ValueError, match="status transition"):
        HaloState(
            **common,
            phase="experimenting",
            result=InvestigationResult(
                outcome=Outcome.READY_FOR_TODO,
                summary="Ready.",
            ),
            status_transition=StatusTransition(target="Todo", state="pending"),
        )
    with pytest.raises(ValueError, match="terminal Halo phase"):
        HaloState(
            **common,
            phase="ready",
            result=InvestigationResult(
                outcome=Outcome.NO_CHANGE,
                summary="No change.",
            ),
            status_transition=StatusTransition(
                target="Todo",
                state="applied",
                applied_at=datetime.now(UTC),
            ),
        )


def test_ready_gate_rejects_incomplete_or_stale_evidence() -> None:
    incomplete = HaloService._gate_outcome(
        InvestigationResult(
            outcome=Outcome.READY_FOR_TODO,
            summary="ready",
        )
    )
    stale = HaloService._gate_outcome(_ready_result(stale_guide=True))
    no_runtime_trace = HaloService._gate_outcome(
        _ready_result().model_copy(update={"trace_validation": None})
    )

    assert incomplete.outcome == Outcome.CONTINUE_RESEARCH
    assert stale.outcome == Outcome.CONTINUE_RESEARCH
    assert no_runtime_trace.outcome == Outcome.CONTINUE_RESEARCH
    assert any("current framework guide" in risk for risk in stale.residual_risks)
    assert any("candidate trace hierarchy" in risk for risk in no_runtime_trace.residual_risks)


def test_ready_gate_rejects_verification_receipts_reclassified_as_experiments() -> None:
    ready = _ready_result()
    verification_action = ExecutedAction(
        index=2,
        kind="verification",
        stage="verification",
        command=["pnpm", "build"],
        exit_code=0,
        stdout_sha256="a" * 64,
        stderr_sha256="b" * 64,
        service_version="1" * 40,
    )
    invalid = ready.model_copy(
        update={
            "experiments": [
                *ready.experiments,
                Experiment(
                    action_index=2,
                    hypothesis="The candidate builds.",
                    action="Run deterministic verification.",
                    result="The build passed.",
                ),
            ],
            "executed_actions": [*ready.executed_actions, verification_action],
        }
    )

    gated = HaloService._gate_outcome(invalid)

    assert gated.outcome == Outcome.CONTINUE_RESEARCH
    assert any("runtime actions" in risk for risk in gated.residual_risks)


def test_ready_gate_requires_every_runtime_experiment_at_exact_candidate_revision() -> None:
    ready = _ready_result()
    wrong_revision = ready.model_copy(
        update={
            "executed_actions": [
                ready.executed_actions[0].model_copy(update={"service_version": "2" * 40})
            ]
        }
    )

    gated = HaloService._gate_outcome(
        wrong_revision,
        candidate_revision="1" * 40,
    )

    assert gated.outcome == Outcome.CONTINUE_RESEARCH
    assert any("revision-bound runtime actions" in risk for risk in gated.residual_risks)


def test_trace_validation_is_bound_to_post_experiment_engine_dataset() -> None:
    ready = _ready_result()
    assert ready.trace_validation is not None
    checkpoint = ready.trace_validation.checkpoint
    traces = ready.trace_validation.candidate_traces
    reports = ready.trace_validation.candidate_desktop_reports
    artifact = SimpleNamespace(
        final_message="Candidate trace analysis completed.",
        dataset=SimpleNamespace(
            files=[SimpleNamespace(sha256=traces[0].sha256)],
            harness_revision="1" * 40,
        ),
    )

    complete = HaloService._trace_validation(
        checkpoint=checkpoint,
        candidate_traces=traces,
        candidate_reports=reports,
        halo_analysis=artifact,
        candidate_revision="1" * 40,
    )
    wrong_dataset = HaloService._trace_validation(
        checkpoint=checkpoint,
        candidate_traces=traces,
        candidate_reports=reports,
        halo_analysis=SimpleNamespace(
            final_message="Analyzed another dataset.",
            dataset=SimpleNamespace(
                files=[SimpleNamespace(sha256="0" * 64)],
                harness_revision="1" * 40,
            ),
        ),
        candidate_revision="1" * 40,
    )
    mixed_dataset = HaloService._trace_validation(
        checkpoint=checkpoint,
        candidate_traces=traces,
        candidate_reports=reports,
        halo_analysis=SimpleNamespace(
            final_message="Candidate mixed with an old trace.",
            dataset=SimpleNamespace(
                files=[
                    SimpleNamespace(sha256=traces[0].sha256),
                    SimpleNamespace(sha256="0" * 64),
                ],
                harness_revision="1" * 40,
            ),
        ),
        candidate_revision="1" * 40,
    )
    wrong_revision = HaloService._trace_validation(
        checkpoint=checkpoint,
        candidate_traces=traces,
        candidate_reports=(),
        halo_analysis=SimpleNamespace(
            final_message="Candidate trace analysis completed.",
            dataset=SimpleNamespace(
                files=[SimpleNamespace(sha256=traces[0].sha256)],
                harness_revision="2" * 40,
            ),
        ),
        candidate_revision="1" * 40,
    )
    missing_service_version = HaloService._trace_validation(
        checkpoint=checkpoint,
        candidate_traces=(
            traces[0].model_copy(
                update={
                    "service_versions": (),
                    "service_version_span_count": 0,
                }
            ),
        ),
        candidate_reports=(),
        halo_analysis=artifact,
        candidate_revision="1" * 40,
    )
    mixed_service_versions = HaloService._trace_validation(
        checkpoint=checkpoint,
        candidate_traces=(
            traces[0].model_copy(
                update={
                    "service_versions": ("0" * 40, "1" * 40),
                }
            ),
        ),
        candidate_reports=(),
        halo_analysis=artifact,
        candidate_revision="1" * 40,
    )
    partially_versioned = HaloService._trace_validation(
        checkpoint=checkpoint,
        candidate_traces=(traces[0].model_copy(update={"service_version_span_count": 1}),),
        candidate_reports=(),
        halo_analysis=artifact,
        candidate_revision="1" * 40,
    )

    assert complete.complete
    assert complete.candidate_desktop_reports == reports
    assert complete.halo_dataset_verified
    assert not wrong_dataset.complete
    assert not wrong_dataset.halo_dataset_verified
    assert not mixed_dataset.complete
    assert not mixed_dataset.halo_dataset_verified
    assert not wrong_revision.complete
    assert not wrong_revision.halo_revision_verified
    assert not missing_service_version.complete
    assert not missing_service_version.service_version_verified
    assert not mixed_service_versions.complete
    assert not mixed_service_versions.service_version_verified
    assert not partially_versioned.complete
    assert not partially_versioned.service_version_verified


def test_desktop_report_is_optional_for_exact_revision_trace_validation() -> None:
    ready = _ready_result()
    assert ready.trace_validation is not None
    traces = ready.trace_validation.candidate_traces
    validation = HaloService._trace_validation(
        checkpoint=ready.trace_validation.checkpoint,
        candidate_traces=traces,
        candidate_reports=(),
        halo_analysis=SimpleNamespace(
            final_message="Candidate trace analysis completed.",
            dataset=SimpleNamespace(
                files=[SimpleNamespace(sha256=traces[0].sha256)],
                harness_revision="1" * 40,
            ),
        ),
        candidate_revision="1" * 40,
    )

    assert validation.complete
    assert validation.candidate_desktop_reports == ()


def test_candidate_observations_are_created_only_by_experiments_and_survive_verification() -> None:
    ready = _ready_result()
    assert ready.trace_validation is not None
    candidate = ready.trace_validation.candidate_traces[0]
    setup_trace = candidate.model_copy(
        update={
            "label": "worktree/.shea/artifacts/halo/traces/setup.jsonl",
            "sha256": "1" * 64,
        }
    )
    verification_trace = candidate.model_copy(
        update={
            "label": "worktree/.shea/artifacts/halo/traces/verification.jsonl",
            "sha256": "2" * 64,
        }
    )
    target_trace = candidate.model_copy(
        update={
            "label": "target/.shea/artifacts/halo/traces/target.jsonl",
            "sha256": "3" * 64,
        }
    )

    traces, reports = HaloService._stable_experiment_observations(
        before=ObservationCheckpoint(traces=(setup_trace,)),
        after=ObservationCheckpoint(
            traces=tuple(
                sorted(
                    (candidate, setup_trace, target_trace),
                    key=lambda item: item.label,
                )
            )
        ),
        final=ObservationCheckpoint(
            traces=tuple(
                sorted(
                    (candidate, setup_trace, target_trace, verification_trace),
                    key=lambda item: item.label,
                )
            )
        ),
    )

    assert traces == (candidate,)
    assert reports == ()

    mutated, _ = HaloService._stable_experiment_observations(
        before=ObservationCheckpoint(traces=(setup_trace,)),
        after=ObservationCheckpoint(
            traces=tuple(sorted((candidate, setup_trace), key=lambda item: item.label))
        ),
        final=ObservationCheckpoint(
            traces=tuple(
                sorted(
                    (
                        candidate.model_copy(update={"sha256": "4" * 64}),
                        setup_trace,
                    ),
                    key=lambda item: item.label,
                )
            )
        ),
    )

    assert mutated == ()


def test_reentry_input_fingerprint_is_stable_and_tracks_new_discussion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shea_halo.service._RUNTIME_SOURCE_SHA256", "a" * 64)
    monkeypatch.delenv("PROVIDER_RUNTIME_HANDLE", raising=False)
    monkeypatch.delenv("CATALYST_OTLP_ENDPOINT", raising=False)
    write_target_config(tmp_path)
    config = replace(
        HaloConfig.load(tmp_path),
        target_environment_variables=("PROVIDER_RUNTIME_HANDLE",),
    )
    issue = ProjectIssue(
        project_id="project",
        item_id="item",
        repository=config.repository,
        number=20,
        title="Improve tracing",
        body="Investigate.",
        url="https://github.com/Alive24/FailureReport/issues/20",
        status=config.tracker.research_state,
    )
    checkpoint = ObservationCheckpoint()
    branch = BranchSnapshot(name="halo/issue-20-improve-tracing", head_revision="1" * 40)
    first = HaloService._input_fingerprint(
        config=config,
        issue=issue,
        discussion="[]",
        checkpoint=checkpoint,
        branch=branch,
    )
    same = HaloService._input_fingerprint(
        config=config,
        issue=issue,
        discussion="[]",
        checkpoint=checkpoint,
        branch=branch,
    )
    changed = HaloService._input_fingerprint(
        config=config,
        issue=issue,
        discussion='[{"comment_id":12,"body":"new evidence"}]',
        checkpoint=checkpoint,
        branch=branch,
    )
    monkeypatch.setenv("PROVIDER_RUNTIME_HANDLE", "secret-value-never-persisted")
    with_runtime_handle = HaloService._input_fingerprint(
        config=config,
        issue=issue,
        discussion="[]",
        checkpoint=checkpoint,
        branch=branch,
    )
    monkeypatch.setenv("CATALYST_OTLP_ENDPOINT", "http://127.0.0.1:4318/v1/traces")
    with_endpoint_override = HaloService._input_fingerprint(
        config=config,
        issue=issue,
        discussion="[]",
        checkpoint=checkpoint,
        branch=branch,
    )
    with_larger_turn_budget = HaloService._input_fingerprint(
        config=replace(config, investigator_max_turns=60),
        issue=issue,
        discussion="[]",
        checkpoint=checkpoint,
        branch=branch,
    )
    with_chat_completions = HaloService._input_fingerprint(
        config=replace(config, api_mode="chat_completions"),
        issue=issue,
        discussion="[]",
        checkpoint=checkpoint,
        branch=branch,
    )
    monkeypatch.setattr("shea_halo.service._RUNTIME_SOURCE_SHA256", "b" * 64)
    with_updated_runtime = HaloService._input_fingerprint(
        config=config,
        issue=issue,
        discussion="[]",
        checkpoint=checkpoint,
        branch=branch,
    )

    assert first == same
    assert first != changed
    assert first != with_runtime_handle
    assert with_runtime_handle != with_endpoint_override
    assert with_endpoint_override != with_larger_turn_budget
    assert first != with_chat_completions
    assert with_endpoint_override != with_updated_runtime


def test_provider_api_mode_blocker_is_structured_and_never_falls_back() -> None:
    result = _provider_api_mode_blocker(
        "chat_completions",
        ProviderApiModeError("chat_completions", 404),
    )

    assert result.outcome == Outcome.BLOCKED
    assert result.blocker is not None
    assert result.blocker.category == "service_unavailable"
    assert result.blocker_verified is True
    assert "chat_completions" in result.blocker.description
    assert "other API mode" in result.residual_risks[0]


def test_discussion_context_ignores_workpad_and_sanitizes_public_input() -> None:
    workpad_comment = IssueComment(
        database_id=1,
        author="halo-bot",
        body="<!-- shea-halo-workpad -->\ninternal",
        created_at="2026-07-24T00:00:00Z",
    )
    discussion = IssueComment(
        database_id=2,
        author="reader",
        body="Evidence is at /Users/alice/private/trace.jsonl",
        created_at="2026-07-24T01:00:00Z",
    )

    rendered = HaloService._discussion_context(
        [workpad_comment, discussion],
        trusted_workpad_comment_ids=frozenset({1}),
    )

    assert "internal" not in rendered
    assert "/Users/alice" not in rendered
    assert "[HOST_PATH]" in rendered
    assert '"comment_id":2' in rendered


def test_no_change_gate_requires_evidence_experiments_and_no_changed_paths() -> None:
    incomplete = HaloService._gate_outcome(
        InvestigationResult(outcome=Outcome.NO_CHANGE, summary="nothing to do")
    )
    contradictory = HaloService._gate_outcome(
        InvestigationResult(
            outcome=Outcome.NO_CHANGE,
            summary="nothing to do",
            evidence=[Evidence(claim="No production gap remains.", source="experiment:1")],
            analysis_sha256="9" * 64,
            experiments=[
                Experiment(
                    action_index=1,
                    hypothesis="The current harness is sufficient.",
                    action="Replay the regression fixture.",
                    result="The fixture passed.",
                )
            ],
            changed_paths=["eve/agent/instrumentation.ts"],
            executed_actions=[_successful_action()],
        )
    )
    complete = HaloService._gate_outcome(
        InvestigationResult(
            outcome=Outcome.NO_CHANGE,
            summary="nothing to do",
            evidence=[Evidence(claim="No production gap remains.", source="experiment:1")],
            analysis_sha256="9" * 64,
            experiments=[
                Experiment(
                    action_index=1,
                    hypothesis="The current harness is sufficient.",
                    action="Replay the regression fixture.",
                    result="The fixture passed.",
                )
            ],
            executed_actions=[_successful_action()],
            verification=[
                Verification(
                    command=["pnpm", "test"],
                    status="passed",
                    evidence="passed",
                )
            ],
        )
    )

    assert incomplete.outcome == Outcome.CONTINUE_RESEARCH
    assert contradictory.outcome == Outcome.CONTINUE_RESEARCH
    assert complete.outcome == Outcome.NO_CHANGE


def test_no_change_tracing_snapshot_requires_the_same_trace_contract_as_todo() -> None:
    complete = _ready_result().model_copy(
        update={
            "outcome": Outcome.NO_CHANGE,
            "summary": "The candidate disproved the need for an implementation change.",
            "todo_handoff": None,
        }
    )
    missing_trace = complete.model_copy(update={"trace_validation": None})
    stale_guide = complete.model_copy(
        update={"selected_guides": [complete.selected_guides[0].model_copy(update={"stale": True})]}
    )

    assert HaloService._gate_outcome(complete).outcome == Outcome.NO_CHANGE
    assert HaloService._gate_outcome(missing_trace).outcome == Outcome.CONTINUE_RESEARCH
    assert HaloService._gate_outcome(stale_guide).outcome == Outcome.CONTINUE_RESEARCH


def test_blocked_gate_requires_structured_external_blocker() -> None:
    unsupported = HaloService._gate_outcome(
        InvestigationResult(outcome=Outcome.BLOCKED, summary="blocked")
    )
    empty = HaloService._gate_outcome(
        InvestigationResult(
            outcome=Outcome.BLOCKED,
            summary="blocked",
            blocker=ExternalBlocker(
                category="missing_input",
                description="",
                required_action="",
            ),
        )
    )
    supported = HaloService._gate_outcome(
        InvestigationResult(
            outcome=Outcome.BLOCKED,
            summary="blocked",
            blocker=ExternalBlocker(
                category="credential",
                description="The worker lacks an API credential.",
                required_action="Set OPENAI_API_KEY.",
            ),
            blocker_verified=True,
        )
    )

    assert unsupported.outcome == Outcome.CONTINUE_RESEARCH
    assert empty.outcome == Outcome.CONTINUE_RESEARCH
    assert supported.outcome == Outcome.BLOCKED


def test_blocker_is_reverified_only_from_the_exact_revision_action() -> None:
    revision = "1" * 40
    result = InvestigationResult(
        outcome=Outcome.BLOCKED,
        summary="The configured runtime action cannot reach its dependency.",
        blocker=ExternalBlocker(
            category="network",
            description="The dependency rejected the request.",
            required_action="Restore access to the dependency.",
            failed_action_index=1,
        ),
        blocker_verified=True,
    )
    stale_failure = ExecutedAction(
        index=1,
        kind="experiment",
        stage="exploration",
        command=["pnpm", "run", "halo:trace-smoke"],
        exit_code=1,
        stdout_sha256="a" * 64,
        stderr_sha256="b" * 64,
        service_version="halo-26-old-attempt",
    )
    current_success = stale_failure.model_copy(
        update={
            "stage": "candidate_validation",
            "exit_code": 0,
            "service_version": revision,
        }
    )
    current_failure = current_success.model_copy(update={"exit_code": 69})
    failed_setup = current_failure.model_copy(
        update={
            "index": 0,
            "kind": "setup",
            "stage": "setup",
            "exit_code": 1,
        }
    )

    assert not HaloService._blocker_verified_at_revision(
        result,
        [stale_failure, current_success],
        candidate_revision=revision,
        allowed_exit_codes=frozenset({69}),
    )
    assert HaloService._blocker_verified_at_revision(
        result,
        [current_failure],
        candidate_revision=revision,
        allowed_exit_codes=frozenset({69}),
    )
    assert not HaloService._blocker_verified_at_revision(
        result,
        [current_failure.model_copy(update={"exit_code": 1})],
        candidate_revision=revision,
        allowed_exit_codes=frozenset({69}),
    )
    assert not HaloService._blocker_verified_at_revision(
        result,
        [failed_setup, current_failure],
        candidate_revision=revision,
        allowed_exit_codes=frozenset({69}),
    )
    assert not HaloService._blocker_verified_at_revision(
        result,
        [current_failure.model_copy(update={"index": 2})],
        candidate_revision=revision,
        allowed_exit_codes=frozenset({69}),
    )
    assert not HaloService._blocker_verified_at_revision(
        result,
        [
            current_failure.model_copy(
                update={
                    "kind": "setup",
                    "stage": "setup",
                }
            )
        ],
        candidate_revision=revision,
        allowed_exit_codes=frozenset({69}),
    )
    assert not HaloService._blocker_verified_at_revision(
        result,
        [
            current_failure.model_copy(
                update={
                    "kind": "verification",
                    "stage": "verification",
                }
            )
        ],
        candidate_revision=revision,
        allowed_exit_codes=frozenset({69}),
    )


class _FakeEngine:
    async def analyze(self, **_kwargs: object) -> str:
        return "HALO analysis"


class _FakeInvestigator:
    def __init__(self) -> None:
        self.calls = 0
        self.synthesis_calls = 0
        self.synthesis_error: Exception | None = None
        self.synthesis_outcome: Outcome | None = None
        self.synthesis_residual_risks: list[str] | None = None
        self.synthesis_experiments: list[Experiment] | None = None

    async def run(self, **_kwargs: object) -> InvestigationResult:
        self.calls += 1
        return InvestigationResult(
            outcome=Outcome.CONTINUE_RESEARCH,
            summary="More evidence is needed.",
        )

    async def synthesize_candidate(self, **kwargs: object) -> AgentDecision:
        self.synthesis_calls += 1
        if self.synthesis_error is not None:
            raise self.synthesis_error
        preliminary = cast(InvestigationResult, kwargs["preliminary_result"])
        traces = cast(tuple[TraceObservation, ...], kwargs["candidate_traces"])
        revision = cast(str, kwargs["candidate_revision"])
        outcome = self.synthesis_outcome or preliminary.outcome
        report_reference, trace_references = candidate_evidence_references(
            candidate_revision=revision,
            halo_analysis=cast(
                HaloAnalysisArtifact | None,
                kwargs["halo_analysis"],
            ),
            candidate_traces=traces,
        )
        evidence = list(preliminary.evidence)
        if outcome in {Outcome.READY_FOR_TODO, Outcome.NO_CHANGE}:
            assert report_reference is not None
            assert trace_references
            evidence = [
                Evidence(
                    claim="The exact HALO report supports the final candidate decision.",
                    source=report_reference,
                ),
                Evidence(
                    claim="The candidate trace dataset is bound to the published revision.",
                    source=trace_references[0],
                ),
            ]
        return AgentDecision(
            outcome=outcome,
            summary=f"Final synthesis: {preliminary.summary}",
            evidence=evidence,
            competing_hypotheses=preliminary.competing_hypotheses,
            experiments=(
                self.synthesis_experiments
                if self.synthesis_experiments is not None
                else preliminary.experiments
            ),
            residual_risks=(
                self.synthesis_residual_risks
                if self.synthesis_residual_risks is not None
                else preliminary.residual_risks
            ),
            todo_handoff=(preliminary.todo_handoff if outcome == Outcome.READY_FOR_TODO else None),
            blocker=preliminary.blocker,
        )


class _FakeGitHub:
    def __init__(self, comments: list[IssueComment]) -> None:
        self.comments = comments
        self.events: list[str] = []
        self.statuses: list[str] = []

    def current_user(self) -> str:
        self.events.append("current_user")
        return "halo-bot"

    def issue_comments(self, _repository: str, _number: int) -> list[IssueComment]:
        self.events.append("issue_comments")
        return list(self.comments)

    def add_comment(self, _repository: str, _number: int, body: str) -> IssueComment:
        self.events.append("add_comment")
        comment = IssueComment(
            database_id=100 + len(self.comments),
            author="halo-bot",
            body=body,
            created_at="2026-07-24T00:00:00Z",
        )
        self.comments.append(comment)
        return comment

    def set_status(self, _metadata: ProjectMetadata, _item_id: str, status_name: str) -> None:
        self.statuses.append(status_name)

    def assert_branch_has_no_pull_requests(self, _repository: str, _head_branch: str) -> None:
        self.events.append("assert_no_pr")

    def assert_issue_remains_in_research(self, issue: ProjectIssue) -> ProjectIssue:
        self.events.append("assert_research")
        return issue


class _FakeWorkspaceManager:
    def __init__(self, root: Path, *, current_base_revision: str = "a" * 40) -> None:
        self.root = root
        self.current_base_revision_value = current_base_revision
        self.recovery_calls = 0

    def prepare(
        self,
        *,
        issue_number: int,
        title: str,
        persisted_branch: str | None = None,
        persisted_base_revision: str | None = None,
        expected_snapshot: object | None = None,
        publication_intent: object | None = None,
        allow_existing: bool = False,
    ) -> HaloWorkspace:
        del (
            issue_number,
            title,
            persisted_base_revision,
            expected_snapshot,
            publication_intent,
        )
        assert allow_existing
        assert persisted_branch is not None
        (self.root / "managed-worktree").mkdir(exist_ok=True)
        return HaloWorkspace(
            path=self.root / "managed-worktree",
            branch=persisted_branch,
            base_revision="a" * 40,
        )

    def create_publication_intent(self, *_args: object, **_kwargs: object) -> None:
        return None

    def current_base_revision(self) -> str:
        return self.current_base_revision_value

    @staticmethod
    def prepare_validation_workspace(
        workspace: HaloWorkspace,
        **_kwargs: object,
    ) -> HaloWorkspace:
        return workspace

    def recover_abandoned_attempt(self, **kwargs: object) -> HaloWorkspace:
        self.recovery_calls += 1
        return self.prepare(
            issue_number=cast(int, kwargs["issue_number"]),
            title="Recovered attempt",
            persisted_branch=cast(str, kwargs["persisted_branch"]),
            persisted_base_revision=cast(str, kwargs["persisted_base_revision"]),
            expected_snapshot=kwargs.get("expected_snapshot"),
            allow_existing=True,
        )


class _PendingWorkspaceManager:
    def __init__(self, root: Path, branch: str) -> None:
        self.root = root
        self.branch = branch
        self.publish_calls = 0

    def prepare(self, **_kwargs: object) -> HaloWorkspace:
        path = self.root / "managed-worktree"
        path.mkdir(exist_ok=True)
        return HaloWorkspace(
            path=path,
            branch=self.branch,
            base_revision="a" * 40,
        )

    def publish_intent(self, *_args: object, **_kwargs: object) -> BranchSnapshot:
        self.publish_calls += 1
        return BranchSnapshot(name=self.branch, head_revision="1" * 40)

    @staticmethod
    def prepare_validation_workspace(
        workspace: HaloWorkspace,
        **_kwargs: object,
    ) -> HaloWorkspace:
        return workspace


def _issue(config: HaloConfig, *, status: str | None = None) -> ProjectIssue:
    return ProjectIssue(
        project_id="project",
        item_id="item",
        repository=config.repository,
        number=20,
        title="Improve Eve tracing",
        body="Investigate.",
        url="https://github.com/Alive24/FailureReport/issues/20",
        status=status or config.tracker.research_state,
    )


def _metadata() -> ProjectMetadata:
    return ProjectMetadata(
        project_id="project",
        status_field_id="status",
        status_options={},
    )


def _desktop_receipt(
    classification: HaloDesktopPreflightClassification,
) -> HaloDesktopPreflightReceipt:
    return HaloDesktopPreflightReceipt(
        service=("halo-canvas-telemetry" if classification == "passed" else None),
        health_status_class="2xx",
        ingest_status_class="2xx",
        started_at=datetime(2026, 7, 25, tzinfo=UTC),
        completed_at=datetime(2026, 7, 25, 0, 0, 1, tzinfo=UTC),
        payload_sha256="d" * 64,
        classification=classification,
    )


@pytest.mark.parametrize(
    ("mode", "expected_calls"),
    [("disabled", 0), ("external", 1)],
)
async def test_service_runs_desktop_preflight_only_when_explicitly_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["disabled", "external"],
    expected_calls: int,
) -> None:
    write_target_config(tmp_path)
    loaded = HaloConfig.load(tmp_path)
    config = replace(
        loaded,
        observation=replace(loaded.observation, desktop_preflight=mode),
    )
    issue = _issue(config)
    initial = HaloState(
        issue_repository=config.repository,
        issue_number=issue.number,
        run_id="halo-20-desktop-preflight",
        phase="research",
        workspace_branch="halo/issue-20-improve-eve-tracing",
        base_revision="a" * 40,
    )
    entry = next_entry(initial, None, producer="halo-bot")
    github = _FakeGitHub(
        [
            IssueComment(
                database_id=100,
                author="halo-bot",
                body=render_entry(entry, "Claimed."),
                created_at="2026-07-25T00:00:00Z",
            )
        ]
    )
    manager = _FakeWorkspaceManager(tmp_path)
    investigator = _FakeInvestigator()
    preflight_calls = 0
    validation_receipts: list[HaloDesktopPreflightReceipt | None] = []

    async def preflight(*_args: object, **_kwargs: object) -> HaloDesktopPreflightResult:
        nonlocal preflight_calls
        preflight_calls += 1
        receipt = _desktop_receipt(HaloDesktopPreflightClassification.PASSED)
        return HaloDesktopPreflightResult(passed=True, receipt=receipt)

    async def validate_candidate(
        _service: HaloService,
        **kwargs: object,
    ) -> None:
        preliminary = cast(InvestigationResult, kwargs["preliminary_result"])
        validation_receipts.append(preliminary.desktop_preflight)

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr("shea_halo.service.WorkspaceManager", lambda _config: manager)
    monkeypatch.setattr("shea_halo.service.preflight_halo_desktop", preflight)
    monkeypatch.setattr(HaloService, "_validate_candidate", validate_candidate)

    await HaloService(
        [config],
        investigator=cast(Investigator, investigator),
        halo_engine=cast(HaloEngineAdapter, _FakeEngine()),
    )._process(
        config,
        cast(GitHubClient, github),
        _metadata(),
        issue,
    )

    assert preflight_calls == expected_calls
    assert investigator.calls == 1
    assert len(validation_receipts) == 1
    if mode == "external":
        assert validation_receipts[0] is not None
        assert validation_receipts[0].classification == "passed"
    else:
        assert validation_receipts == [None]


async def test_failed_desktop_preflight_is_supplemental_and_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_target_config(tmp_path)
    loaded = HaloConfig.load(tmp_path)
    config = replace(
        loaded,
        observation=replace(loaded.observation, desktop_preflight="external"),
    )
    issue = _issue(config)
    initial = HaloState(
        issue_repository=config.repository,
        issue_number=issue.number,
        run_id="halo-20-desktop-preflight-failure",
        phase="research",
        workspace_branch="halo/issue-20-improve-eve-tracing",
        base_revision="a" * 40,
    )
    entry = next_entry(initial, None, producer="halo-bot")
    github = _FakeGitHub(
        [
            IssueComment(
                database_id=100,
                author="halo-bot",
                body=render_entry(entry, "Claimed."),
                created_at="2026-07-25T00:00:00Z",
            )
        ]
    )
    manager = _FakeWorkspaceManager(tmp_path)
    investigator = _FakeInvestigator()
    endpoint = "http://127.0.0.1:48799/private-endpoint"
    validation_receipts: list[object] = []

    async def rejected_preflight(
        *_args: object,
        **_kwargs: object,
    ) -> HaloDesktopPreflightResult:
        receipt = _desktop_receipt(HaloDesktopPreflightClassification.REJECTED_INGEST)
        return HaloDesktopPreflightResult(
            passed=False,
            receipt=receipt,
            blocker=ExternalBlocker(
                category="service_unavailable",
                description="The external Desktop rejected the synthetic OTLP/JSON trace.",
                required_action="Repair the operator-managed public ingest surface.",
            ),
        )

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("CATALYST_OTLP_ENDPOINT", endpoint)
    monkeypatch.setattr("shea_halo.service.WorkspaceManager", lambda _config: manager)
    monkeypatch.setattr("shea_halo.service.preflight_halo_desktop", rejected_preflight)

    async def validate_candidate(_service: HaloService, **kwargs: object) -> None:
        preliminary = cast(InvestigationResult, kwargs["preliminary_result"])
        validation_receipts.append(preliminary.desktop_preflight)

    monkeypatch.setattr(HaloService, "_validate_candidate", validate_candidate)

    await HaloService(
        [config],
        investigator=cast(Investigator, investigator),
        halo_engine=cast(HaloEngineAdapter, _FakeEngine()),
    )._process(
        config,
        cast(GitHubClient, github),
        _metadata(),
        issue,
    )

    assert investigator.calls == 1
    assert github.statuses == []
    assert len(validation_receipts) == 1
    receipt = cast(HaloDesktopPreflightReceipt, validation_receipts[0])
    assert receipt.classification == "rejected_ingest"
    durable_workpad = "\n".join(comment.body for comment in github.comments)
    assert endpoint not in durable_workpad


def test_finish_persists_pending_then_applied_status_transition(tmp_path: Path) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)
    issue = _issue(config)
    initial_state = HaloState(
        issue_repository=config.repository,
        issue_number=issue.number,
        run_id="halo-20-transition",
        phase="research",
        workspace_branch="halo/issue-20-improve-eve-tracing",
    )
    initial_entry = next_entry(initial_state, None, producer="halo-bot")
    github = _FakeGitHub(
        [
            IssueComment(
                database_id=100,
                author="halo-bot",
                body=render_entry(initial_entry, "Claimed."),
                created_at="2026-07-24T00:00:00Z",
            )
        ]
    )
    terminal = HaloState(
        issue_repository=config.repository,
        issue_number=issue.number,
        run_id="halo-20-transition",
        phase="ready",
        workspace_branch=initial_state.workspace_branch,
        base_revision="a" * 40,
        result=_ready_result(),
    )

    head = HaloService([config])._finish(
        config=config,
        github=cast(GitHubClient, github),
        metadata=_metadata(),
        issue=issue,
        producer="halo-bot",
        head=WorkpadHead(comment_id=100, entry=initial_entry),
        state=terminal,
    )

    assert github.statuses == ["Todo"]
    assert head.entry.revision == 3
    transition = head.entry.state.status_transition
    assert transition is not None
    assert transition.target == "Todo"
    assert transition.state == "applied"
    assert transition.applied_at is not None
    pending = find_head(
        github.comments[:-1],
        repository=config.repository,
        issue_number=issue.number,
        trusted_producers={"halo-bot"},
    )
    assert pending is not None
    assert pending.entry.state.status_transition is not None
    assert pending.entry.state.status_transition.state == "pending"
    assert pending.entry.state.status_transition.created_at == transition.created_at


def test_terminal_item_reconciles_applied_comment_after_status_write(tmp_path: Path) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)
    issue = _issue(config, status="Todo")
    pending_state = HaloState(
        issue_repository=config.repository,
        issue_number=issue.number,
        run_id="halo-20-transition",
        phase="ready",
        workspace_branch="halo/issue-20-improve-eve-tracing",
        base_revision="a" * 40,
        result=_ready_result(),
        status_transition=StatusTransition(target="Todo", state="pending"),
    )
    pending_entry = next_entry(pending_state, None, producer="halo-bot")
    github = _FakeGitHub(
        [
            IssueComment(
                database_id=100,
                author="halo-bot",
                body=render_entry(pending_entry, "Ready."),
                created_at="2026-07-24T00:00:00Z",
            )
        ]
    )

    HaloService([config])._reconcile_terminal_transition(
        config,
        cast(GitHubClient, github),
        issue,
    )

    assert github.statuses == []
    head = find_head(
        github.comments,
        repository=config.repository,
        issue_number=issue.number,
        trusted_producers={"halo-bot"},
    )
    assert head is not None
    assert head.entry.revision == 2
    assert head.entry.state.status_transition is not None
    assert head.entry.state.status_transition.state == "applied"


def test_downstream_project_status_supersedes_a_pending_halo_target(
    tmp_path: Path,
) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)
    issue = _issue(config, status="In Progress")
    pending_state = HaloState(
        issue_repository=config.repository,
        issue_number=issue.number,
        run_id="halo-20-transition",
        phase="ready",
        workspace_branch="halo/issue-20-improve-eve-tracing",
        base_revision="a" * 40,
        result=_ready_result(),
        status_transition=StatusTransition(target="Todo", state="pending"),
    )
    pending_entry = next_entry(pending_state, None, producer="halo-bot")
    github = _FakeGitHub(
        [
            IssueComment(
                database_id=100,
                author="halo-bot",
                body=render_entry(pending_entry, "Ready."),
                created_at="2026-07-24T00:00:00Z",
            )
        ]
    )

    HaloService([config])._reconcile_terminal_transition(
        config,
        cast(GitHubClient, github),
        issue,
    )

    assert github.statuses == []
    head = find_head(
        github.comments,
        repository=config.repository,
        issue_number=issue.number,
        trusted_producers={"halo-bot"},
    )
    assert head is not None
    assert head.entry.revision == 2
    assert head.entry.state.status_transition is None


def test_terminal_reconciliation_rejects_a_transition_inconsistent_with_result(
    tmp_path: Path,
) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)
    issue = _issue(config, status="Done")
    invalid_state = HaloState(
        issue_repository=config.repository,
        issue_number=issue.number,
        run_id="halo-20-transition",
        phase="ready",
        workspace_branch="halo/issue-20-improve-eve-tracing",
        base_revision="a" * 40,
        result=_ready_result(),
        status_transition=StatusTransition(target="Done", state="pending"),
    )
    entry = next_entry(invalid_state, None, producer="halo-bot")
    github = _FakeGitHub(
        [
            IssueComment(
                database_id=100,
                author="halo-bot",
                body=render_entry(entry, "Done."),
                created_at="2026-07-24T00:00:00Z",
            )
        ]
    )

    with pytest.raises(ServiceError, match="does not match"):
        HaloService([config])._reconcile_terminal_transition(
            config,
            cast(GitHubClient, github),
            issue,
        )

    assert len(github.comments) == 1


async def test_terminal_recovery_cache_tracks_new_issue_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)
    first_issue = replace(
        _issue(config, status="Todo"),
        updated_at="2026-07-24T00:00:00Z",
    )
    first_state = HaloState(
        issue_repository=config.repository,
        issue_number=first_issue.number,
        run_id="halo-20-transition",
        phase="ready",
        workspace_branch="halo/issue-20-improve-eve-tracing",
        base_revision="a" * 40,
        result=_ready_result(),
        status_transition=StatusTransition(target="Todo", state="pending"),
    )
    first_entry = next_entry(first_state, None, producer="halo-bot")

    class TerminalGitHub(_FakeGitHub):
        def __init__(self) -> None:
            super().__init__(
                [
                    IssueComment(
                        database_id=100,
                        author="halo-bot",
                        body=render_entry(first_entry, "Ready."),
                        created_at="2026-07-24T00:00:00Z",
                    )
                ]
            )
            self.issue = first_issue

        def project_metadata(self) -> ProjectMetadata:
            return _metadata()

        def list_project_issues(self) -> list[ProjectIssue]:
            return [self.issue]

    github = TerminalGitHub()
    monkeypatch.setattr(
        "shea_halo.service.GitHubClient",
        lambda _tracker: github,
    )
    service = HaloService([config])

    await service.run_once()
    first_head = find_head(
        github.comments,
        repository=config.repository,
        issue_number=first_issue.number,
        trusted_producers={"halo-bot"},
    )
    assert first_head is not None
    assert first_head.entry.state.status_transition is not None
    assert first_head.entry.state.status_transition.state == "applied"

    second_state = first_head.entry.state.model_copy(
        update={
            "status_transition": StatusTransition(target="Todo", state="pending"),
        }
    )
    second_entry = next_entry(second_state, first_head, producer="halo-bot")
    github.comments.append(
        IssueComment(
            database_id=102,
            author="halo-bot",
            body=render_entry(second_entry, "Ready again."),
            created_at="2026-07-24T00:01:00Z",
        )
    )
    github.issue = replace(
        first_issue,
        updated_at="2026-07-24T00:01:00Z",
    )

    await service.run_once()

    second_head = find_head(
        github.comments,
        repository=config.repository,
        issue_number=first_issue.number,
        trusted_producers={"halo-bot"},
    )
    assert second_head is not None
    assert second_head.entry.revision == 4
    assert second_head.entry.state.status_transition is not None
    assert second_head.entry.state.status_transition.state == "applied"


async def test_research_status_supersedes_a_stale_pending_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)
    issue = _issue(config)
    pending_state = HaloState(
        issue_repository=config.repository,
        issue_number=issue.number,
        run_id="halo-20-transition",
        phase="ready",
        workspace_branch="halo/issue-20-improve-eve-tracing",
        base_revision="a" * 40,
        result=_ready_result(),
        status_transition=StatusTransition(target="Todo", state="pending"),
    )
    pending_entry = next_entry(pending_state, None, producer="halo-bot")
    github = _FakeGitHub(
        [
            IssueComment(
                database_id=100,
                author="halo-bot",
                body=render_entry(pending_entry, "Ready."),
                created_at="2026-07-24T00:00:00Z",
            )
        ]
    )
    investigator = _FakeInvestigator()
    manager = _FakeWorkspaceManager(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(
        "shea_halo.service.WorkspaceManager",
        lambda _config: manager,
    )
    validation_calls: list[dict[str, object]] = []

    async def validate_candidate(
        _service: HaloService,
        **kwargs: object,
    ) -> None:
        validation_calls.append(kwargs)

    monkeypatch.setattr(HaloService, "_validate_candidate", validate_candidate)

    await HaloService(
        [config],
        investigator=cast(Investigator, investigator),
        halo_engine=cast(HaloEngineAdapter, _FakeEngine()),
    )._process(
        config,
        cast(GitHubClient, github),
        _metadata(),
        issue,
    )

    assert github.statuses == []
    assert investigator.calls == 1
    assert len(validation_calls) == 1
    assert "assert_no_pr" in github.events
    head = find_head(
        github.comments,
        repository=config.repository,
        issue_number=issue.number,
        trusted_producers={"halo-bot"},
    )
    assert head is not None
    assert head.entry.state.phase == "validating"
    assert head.entry.state.status_transition is None


async def test_explicit_move_back_to_halo_research_reenters_finalized_workpad(
    tmp_path: Path, monkeypatch: object
) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)
    prior_state = HaloState(
        issue_repository=config.repository,
        issue_number=20,
        run_id="halo-20-original",
        phase="ready",
        workspace_branch="halo/issue-20-improve-eve-tracing",
        base_revision="a" * 40,
        result=InvestigationResult(
            outcome=Outcome.READY_FOR_TODO,
            summary="Previously ready.",
        ),
    )
    prior_entry = next_entry(prior_state, None, producer="halo-bot")
    github = _FakeGitHub(
        [
            IssueComment(
                database_id=100,
                author="halo-bot",
                body=render_entry(prior_entry, "Previously ready."),
                created_at="2026-07-24T00:00:00Z",
            )
        ]
    )
    investigator = _FakeInvestigator()
    manager = _FakeWorkspaceManager(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test")  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "shea_halo.service.WorkspaceManager", lambda _config: manager
    )
    validation_calls: list[dict[str, object]] = []

    async def validate_candidate(
        _service: HaloService,
        *,
        config: HaloConfig,
        **kwargs: object,
    ) -> None:
        validation_calls.append({"config": config, **kwargs})

    monkeypatch.setattr(  # type: ignore[attr-defined]
        HaloService,
        "_validate_candidate",
        validate_candidate,
    )
    service = HaloService(
        [config],
        investigator=cast(Investigator, investigator),
        halo_engine=cast(HaloEngineAdapter, _FakeEngine()),
    )
    issue = ProjectIssue(
        project_id="project",
        item_id="item",
        repository=config.repository,
        number=20,
        title="Improve Eve tracing",
        body="Investigate.",
        url="https://github.com/Alive24/FailureReport/issues/20",
        status=config.tracker.research_state,
    )
    metadata = ProjectMetadata(
        project_id="project",
        status_field_id="status",
        status_options={},
    )

    await service._process(
        config,
        cast(GitHubClient, github),
        metadata,
        issue,
    )

    assert github.events[:3] == ["current_user", "issue_comments", "assert_no_pr"]
    assert investigator.calls == 1
    assert github.statuses == []
    assert len(validation_calls) == 1
    head = find_head(
        github.comments,
        repository=config.repository,
        issue_number=20,
        trusted_producers={"halo-bot"},
    )
    assert head is not None
    assert head.entry.revision == 3
    assert head.entry.state.run_id == "halo-20-original"
    assert head.entry.state.phase == "validating"


async def test_base_branch_drift_fails_closed_without_running_investigator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)
    issue = _issue(config)
    persisted = HaloState(
        issue_repository=config.repository,
        issue_number=issue.number,
        run_id="halo-20-pinned",
        phase="research",
        workspace_branch="halo/issue-20-improve-eve-tracing",
        base_revision="a" * 40,
    )
    entry = next_entry(persisted, None, producer="halo-bot")
    github = _FakeGitHub(
        [
            IssueComment(
                database_id=100,
                author="halo-bot",
                body=render_entry(entry, "Pinned research."),
                created_at="2026-07-24T00:00:00Z",
            )
        ]
    )
    investigator = _FakeInvestigator()
    manager = _FakeWorkspaceManager(tmp_path, current_base_revision="b" * 40)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr("shea_halo.service.WorkspaceManager", lambda _config: manager)

    await HaloService(
        [config],
        investigator=cast(Investigator, investigator),
        halo_engine=cast(HaloEngineAdapter, _FakeEngine()),
    )._process(
        config,
        cast(GitHubClient, github),
        _metadata(),
        issue,
    )

    assert investigator.calls == 0
    assert github.statuses == ["Need Human Input"]
    head = find_head(
        github.comments,
        repository=config.repository,
        issue_number=issue.number,
        trusted_producers={"halo-bot"},
    )
    assert head is not None
    assert head.entry.state.phase == "blocked"
    assert head.entry.state.result is not None
    assert head.entry.state.result.blocker is not None
    assert head.entry.state.result.blocker.category == "missing_input"
    assert "a" * 40 in head.entry.state.result.residual_risks[-1]
    assert "b" * 40 in head.entry.state.result.residual_risks[-1]


async def test_reentry_recovers_attempt_interrupted_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)
    issue = _issue(config)
    interrupted = HaloState(
        issue_repository=config.repository,
        issue_number=issue.number,
        run_id="halo-20-interrupted",
        phase="experimenting",
        workspace_branch="halo/issue-20-improve-eve-tracing",
        base_revision="a" * 40,
    )
    entry = next_entry(interrupted, None, producer="halo-bot")
    github = _FakeGitHub(
        [
            IssueComment(
                database_id=100,
                author="halo-bot",
                body=render_entry(entry, "Running experiments."),
                created_at="2026-07-24T00:00:00Z",
            )
        ]
    )
    investigator = _FakeInvestigator()
    manager = _FakeWorkspaceManager(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr("shea_halo.service.WorkspaceManager", lambda _config: manager)
    validation_calls: list[dict[str, object]] = []

    async def validate_candidate(
        _service: HaloService,
        **kwargs: object,
    ) -> None:
        validation_calls.append(kwargs)

    monkeypatch.setattr(HaloService, "_validate_candidate", validate_candidate)

    await HaloService(
        [config],
        investigator=cast(Investigator, investigator),
        halo_engine=cast(HaloEngineAdapter, _FakeEngine()),
    )._process(
        config,
        cast(GitHubClient, github),
        _metadata(),
        issue,
    )

    assert manager.recovery_calls == 1
    assert investigator.calls == 1
    assert len(validation_calls) == 1
    assert any(
        "Recovered a kill-interrupted attempt" in comment.body for comment in github.comments
    )


async def test_pending_publication_recovers_and_routes_without_rerunning_model(
    tmp_path: Path, monkeypatch: object
) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)
    branch = "halo/issue-20-improve-eve-tracing"
    pending_result = _ready_result()
    assert pending_result.trace_validation is not None
    pending_result = pending_result.model_copy(
        update={
            "trace_validation": pending_result.trace_validation.model_copy(
                update={"candidate_revision": None, "complete": False}
            )
        }
    )
    state = HaloState(
        issue_repository=config.repository,
        issue_number=20,
        run_id="halo-20-original",
        phase="experimenting",
        workspace_branch=branch,
        base_revision="a" * 40,
        publication_intent=PublicationIntent(
            branch=branch,
            parent_revision="a" * 40,
            manifest_sha256="f" * 64,
            paths=[
                SnapshotPath(
                    path="instrumentation.ts",
                    state="file",
                    sha256="e" * 64,
                    size=10,
                )
            ],
        ),
        result=pending_result,
    )
    entry = next_entry(state, None, producer="halo-bot")
    github = _FakeGitHub(
        [
            IssueComment(
                database_id=100,
                author="halo-bot",
                body=render_entry(entry, "Publishing."),
                created_at="2026-07-24T00:00:00Z",
            )
        ]
    )
    manager = _PendingWorkspaceManager(tmp_path, branch)
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "shea_halo.service.WorkspaceManager", lambda _config: manager
    )
    validation_calls: list[dict[str, object]] = []

    async def validate_candidate(
        _service: HaloService,
        *,
        config: HaloConfig,
        **kwargs: object,
    ) -> None:
        validation_calls.append({"config": config, **kwargs})

    monkeypatch.setattr(  # type: ignore[attr-defined]
        HaloService,
        "_validate_candidate",
        validate_candidate,
    )
    service = HaloService(
        [config],
        investigator=cast(Investigator, _FakeInvestigator()),
        halo_engine=cast(HaloEngineAdapter, _FakeEngine()),
    )
    issue = ProjectIssue(
        project_id="project",
        item_id="item",
        repository=config.repository,
        number=20,
        title="Improve Eve tracing",
        body="Investigate.",
        url="https://github.com/Alive24/FailureReport/issues/20",
        status=config.tracker.research_state,
    )
    metadata = ProjectMetadata(
        project_id="project",
        status_field_id="status",
        status_options={},
    )

    await service._process(
        config,
        cast(GitHubClient, github),
        metadata,
        issue,
    )

    assert manager.publish_calls == 1
    assert github.statuses == []
    assert len(validation_calls) == 1
    head = find_head(
        github.comments,
        repository=config.repository,
        issue_number=20,
        trusted_producers={"halo-bot"},
    )
    assert head is not None
    assert head.entry.state.phase == "validating"
    assert head.entry.state.branch is not None
    assert head.entry.state.branch.head_revision == "1" * 40


async def test_validating_reentry_uses_the_original_persisted_observation_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)
    issue = _issue(config)
    workspace_path = tmp_path / "managed-worktree"
    workspace_path.mkdir()
    workspace = HaloWorkspace(
        path=workspace_path,
        branch="halo/issue-20-improve-eve-tracing",
        base_revision="a" * 40,
    )
    revision = "1" * 40
    candidate_trace = _ready_result().trace_validation
    assert candidate_trace is not None
    trace = candidate_trace.candidate_traces[0]
    checkpoint = ObservationCheckpoint(traces=(trace,))
    preliminary = _ready_result().model_copy(
        update={
            "trace_validation": None,
            "executed_actions": [],
            "verification": [],
        }
    )
    validating_state = HaloState(
        issue_repository=config.repository,
        issue_number=issue.number,
        run_id="halo-20-retry",
        phase="validating",
        workspace_branch=workspace.branch,
        base_revision=workspace.base_revision,
        branch=BranchSnapshot(name=workspace.branch, head_revision=revision),
        validation_baseline=ObservationCheckpoint(),
        result=preliminary,
    )
    validating_entry = next_entry(validating_state, None, producer="halo-bot")
    head = WorkpadHead(comment_id=100, entry=validating_entry)
    inventory_calls = 0
    checkpoints = iter(
        (
            ObservationCheckpoint(),
            checkpoint,
            checkpoint,
        )
    )

    def inventory(_config: HaloConfig, _workspace: HaloWorkspace) -> ObservationCheckpoint:
        nonlocal inventory_calls
        inventory_calls += 1
        return next(checkpoints)

    def stage(
        _config: HaloConfig,
        _workspace: Path,
        *,
        kind: str,
        stage: str,
        service_version: str,
        **_kwargs: object,
    ) -> list[ExecutedAction]:
        index = 0 if kind == "setup" else 1
        return [
            ExecutedAction(
                index=index,
                kind=cast(Literal["setup", "experiment", "verification"], kind),
                stage=cast(
                    Literal["setup", "exploration", "candidate_validation", "verification"],
                    stage,
                ),
                command=["configured", kind],
                exit_code=0,
                stdout_sha256="b" * 64,
                stderr_sha256="c" * 64,
                service_version=service_version,
            )
        ]

    def verify(
        _config: HaloConfig,
        _workspace: Path,
        *,
        service_version: str,
        **_kwargs: object,
    ) -> tuple[list[Verification], list[ExecutedAction]]:
        action = ExecutedAction(
            index=2,
            kind="verification",
            stage="verification",
            command=["configured", "verification"],
            exit_code=0,
            stdout_sha256="d" * 64,
            stderr_sha256="e" * 64,
            service_version=service_version,
        )
        return (
            [
                Verification(
                    command=action.command,
                    status="passed",
                    evidence="The configured verification passed.",
                )
            ],
            [action],
        )

    class Manager:
        @staticmethod
        def prepare_validation_workspace(
            prepared_workspace: HaloWorkspace,
            **_kwargs: object,
        ) -> HaloWorkspace:
            return prepared_workspace

        @staticmethod
        def current_base_revision() -> str:
            return workspace.base_revision

        @staticmethod
        def create_publication_intent(
            _workspace: HaloWorkspace,
            *,
            expected_snapshot: BranchSnapshot | None,
        ) -> PublicationIntent | None:
            assert expected_snapshot is not None
            return None

        @staticmethod
        def discard_unpublished_attempt(
            _workspace: HaloWorkspace,
            **_kwargs: object,
        ) -> None:
            return None

    class Engine:
        async def analyze(self, **_kwargs: object) -> object:
            return SimpleNamespace(
                final_message="Candidate trace analysis completed.",
                dataset=SimpleNamespace(
                    files=[SimpleNamespace(sha256=trace.sha256)],
                    harness_revision=revision,
                ),
            )

    finished: list[HaloState] = []

    def finish(_service: HaloService, **kwargs: object) -> WorkpadHead:
        finished.append(cast(HaloState, kwargs["state"]))
        return cast(WorkpadHead, kwargs["head"])

    monkeypatch.setattr("shea_halo.service.inventory_observations", inventory)
    monkeypatch.setattr("shea_halo.service.run_configured_stage", stage)
    monkeypatch.setattr("shea_halo.service.run_verification", verify)
    monkeypatch.setattr(HaloService, "_finish", finish)
    investigator = _FakeInvestigator()
    investigator.synthesis_outcome = Outcome.NO_CHANGE
    service = HaloService(
        [config],
        investigator=cast(Investigator, investigator),
        halo_engine=cast(HaloEngineAdapter, Engine()),
    )

    await service._validate_candidate(
        config=config,
        github=cast(GitHubClient, _FakeGitHub([])),
        metadata=_metadata(),
        issue=issue,
        producer="halo-bot",
        head=head,
        manager=cast(WorkspaceManager, Manager()),
        workspace=workspace,
        workspace_branch=workspace.branch,
        run_id=validating_state.run_id,
        snapshot=validating_state.branch,
        preliminary_result=preliminary,
        discussion="[]",
    )

    assert inventory_calls == 3
    assert len(finished) == 1
    assert investigator.synthesis_calls == 1
    result = finished[0].result
    assert result is not None
    assert result.outcome == Outcome.NO_CHANGE
    assert result.summary == "Final synthesis: ready"
    assert result.evidence[0].source.startswith("halo-report:sha256:")
    assert result.todo_handoff is None
    assert result.trace_validation is not None
    assert result.trace_validation.complete
    assert result.trace_validation.checkpoint == checkpoint

    checkpoints = iter(
        (
            ObservationCheckpoint(),
            checkpoint,
            checkpoint,
        )
    )
    finished.clear()
    investigator.synthesis_outcome = Outcome.READY_FOR_TODO
    investigator.synthesis_residual_risks = [f"synthesis risk {index}" for index in range(8)]
    turn_limit_risk = (
        "The investigator reached its configured 100-turn limit. "
        "The experimental checkpoint may be incomplete."
    )
    exhausted_preliminary = preliminary.model_copy(
        update={
            "outcome": Outcome.CONTINUE_RESEARCH,
            "turn_budget_exhausted": True,
            "summary": ("The bounded investigator turn budget ended after local exploration."),
            "residual_risks": [
                turn_limit_risk,
                *[f"preliminary risk {index}" for index in range(11)],
            ],
            "todo_handoff": None,
        }
    )

    await service._validate_candidate(
        config=config,
        github=cast(GitHubClient, _FakeGitHub([])),
        metadata=_metadata(),
        issue=issue,
        producer="halo-bot",
        head=head,
        manager=cast(WorkspaceManager, Manager()),
        workspace=workspace,
        workspace_branch=workspace.branch,
        run_id=validating_state.run_id,
        snapshot=validating_state.branch,
        preliminary_result=exhausted_preliminary,
        discussion="[]",
    )

    assert len(finished) == 1
    checkpoint_result = finished[0].result
    assert checkpoint_result is not None
    assert checkpoint_result.outcome == Outcome.CONTINUE_RESEARCH
    assert checkpoint_result.turn_budget_exhausted is True
    assert checkpoint_result.summary == exhausted_preliminary.summary
    assert checkpoint_result.todo_handoff is None
    assert checkpoint_result.blocker is None
    assert checkpoint_result.blocker_verified is False
    assert turn_limit_risk in checkpoint_result.residual_risks
    assert "synthesis risk 0" in checkpoint_result.residual_risks
    assert len(checkpoint_result.residual_risks) == 12
    assert any(
        risk.startswith("The preliminary investigator exhausted")
        for risk in checkpoint_result.residual_risks
    )
    assert InvestigationResult.model_validate(checkpoint_result.model_dump()) == checkpoint_result

    checkpoints = iter(
        (
            ObservationCheckpoint(),
            checkpoint,
            checkpoint,
        )
    )
    finished.clear()
    investigator.synthesis_residual_risks = None
    investigator.synthesis_error = RuntimeError("structured synthesis unavailable")
    failed_github = _FakeGitHub(
        [
            IssueComment(
                database_id=head.comment_id,
                author="halo-bot",
                body=render_entry(head.entry, "Validating candidate."),
                created_at="2026-07-24T00:00:00Z",
            )
        ]
    )

    await service._validate_candidate(
        config=config,
        github=cast(GitHubClient, failed_github),
        metadata=_metadata(),
        issue=issue,
        producer="halo-bot",
        head=head,
        manager=cast(WorkspaceManager, Manager()),
        workspace=workspace,
        workspace_branch=workspace.branch,
        run_id=validating_state.run_id,
        snapshot=validating_state.branch,
        preliminary_result=exhausted_preliminary,
        discussion="[]",
    )

    assert finished == []
    failed_head = find_head(
        failed_github.comments,
        repository=config.repository,
        issue_number=issue.number,
        trusted_producers={"halo-bot"},
    )
    assert failed_head is not None
    assert failed_head.entry.state.phase == "research"
    assert failed_head.entry.state.input_fingerprint is None
    assert failed_head.entry.state.result is not None
    assert failed_head.entry.state.result.outcome == Outcome.CONTINUE_RESEARCH
    assert failed_head.entry.state.result.turn_budget_exhausted is True
    assert failed_head.entry.state.result.summary.startswith(
        "The exact candidate evidence was collected"
    )
    assert failed_head.entry.state.result.todo_handoff is None
    assert turn_limit_risk in failed_head.entry.state.result.residual_risks
    assert any(
        "structured synthesis unavailable" in risk
        for risk in failed_head.entry.state.result.residual_risks
    )
    assert len(failed_head.entry.state.result.residual_risks) == 12
    assert (
        InvestigationResult.model_validate(failed_head.entry.state.result.model_dump())
        == failed_head.entry.state.result
    )

    checkpoints = iter(
        (
            ObservationCheckpoint(),
            checkpoint,
            checkpoint,
        )
    )
    investigator.synthesis_error = None
    investigator.synthesis_outcome = Outcome.READY_FOR_TODO
    investigator.synthesis_experiments = [
        Experiment(
            action_index=2,
            hypothesis="The candidate builds.",
            action="Run deterministic verification.",
            result="The build passed.",
        )
    ]
    binding_failure_github = _FakeGitHub(
        [
            IssueComment(
                database_id=head.comment_id,
                author="halo-bot",
                body=render_entry(head.entry, "Validating candidate."),
                created_at="2026-07-24T00:00:00Z",
            )
        ]
    )

    await service._validate_candidate(
        config=config,
        github=cast(GitHubClient, binding_failure_github),
        metadata=_metadata(),
        issue=issue,
        producer="halo-bot",
        head=head,
        manager=cast(WorkspaceManager, Manager()),
        workspace=workspace,
        workspace_branch=workspace.branch,
        run_id=validating_state.run_id,
        snapshot=validating_state.branch,
        preliminary_result=preliminary,
        discussion="[]",
    )

    binding_failure_head = find_head(
        binding_failure_github.comments,
        repository=config.repository,
        issue_number=issue.number,
        trusted_producers={"halo-bot"},
    )
    assert binding_failure_head is not None
    assert binding_failure_head.entry.state.phase == "research"
    assert binding_failure_head.entry.state.result is not None
    assert binding_failure_head.entry.state.result.outcome == Outcome.CONTINUE_RESEARCH
    assert any(
        "reclassified non-experiment receipt" in risk
        for risk in binding_failure_head.entry.state.result.residual_risks
    )
    investigator.synthesis_experiments = None

    def fail_validation_stage(
        _config: HaloConfig,
        _workspace: Path,
        **_kwargs: object,
    ) -> list[ExecutedAction]:
        raise RuntimeError("candidate validation exploded")

    monkeypatch.setattr(
        "shea_halo.service.run_configured_stage",
        fail_validation_stage,
    )
    monkeypatch.setattr(
        "shea_halo.service.discard_worktree_observation_changes",
        lambda *_args, **_kwargs: (),
    )
    validation_failure_github = _FakeGitHub(
        [
            IssueComment(
                database_id=head.comment_id,
                author="halo-bot",
                body=render_entry(head.entry, "Validating candidate."),
                created_at="2026-07-24T00:00:00Z",
            )
        ]
    )

    await service._validate_candidate(
        config=config,
        github=cast(GitHubClient, validation_failure_github),
        metadata=_metadata(),
        issue=issue,
        producer="halo-bot",
        head=head,
        manager=cast(WorkspaceManager, Manager()),
        workspace=workspace,
        workspace_branch=workspace.branch,
        run_id=validating_state.run_id,
        snapshot=validating_state.branch,
        preliminary_result=exhausted_preliminary,
        discussion="[]",
    )

    validation_failure_head = find_head(
        validation_failure_github.comments,
        repository=config.repository,
        issue_number=issue.number,
        trusted_producers={"halo-bot"},
    )
    assert validation_failure_head is not None
    validation_failure_result = validation_failure_head.entry.state.result
    assert validation_failure_result is not None
    assert validation_failure_result.turn_budget_exhausted is True
    assert turn_limit_risk in validation_failure_result.residual_risks
    assert any(
        "candidate validation exploded" in risk for risk in validation_failure_result.residual_risks
    )
    assert len(validation_failure_result.residual_risks) == 12
    assert (
        InvestigationResult.model_validate(validation_failure_result.model_dump())
        == validation_failure_result
    )

    def fail_observation_cleanup(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise PermissionError("observation cleanup denied")

    monkeypatch.setattr(
        "shea_halo.service.discard_worktree_observation_changes",
        fail_observation_cleanup,
    )
    finished.clear()

    await service._validate_candidate(
        config=config,
        github=cast(GitHubClient, _FakeGitHub([])),
        metadata=_metadata(),
        issue=issue,
        producer="halo-bot",
        head=head,
        manager=cast(WorkspaceManager, Manager()),
        workspace=workspace,
        workspace_branch=workspace.branch,
        run_id=validating_state.run_id,
        snapshot=validating_state.branch,
        preliminary_result=exhausted_preliminary,
        discussion="[]",
    )

    assert len(finished) == 1
    cleanup_failure_result = finished[0].result
    assert cleanup_failure_result is not None
    assert cleanup_failure_result.outcome == Outcome.BLOCKED
    assert cleanup_failure_result.turn_budget_exhausted is True
    assert turn_limit_risk in cleanup_failure_result.residual_risks
    assert any(
        "observation cleanup denied" in risk for risk in cleanup_failure_result.residual_risks
    )
    assert len(cleanup_failure_result.residual_risks) == 12
    assert (
        InvestigationResult.model_validate(cleanup_failure_result.model_dump())
        == cleanup_failure_result
    )

    class SourceMutationAndDriftManager(Manager):
        @staticmethod
        def create_publication_intent(
            _workspace: HaloWorkspace,
            *,
            expected_snapshot: BranchSnapshot | None,
        ) -> PublicationIntent:
            assert expected_snapshot is not None
            return PublicationIntent(
                branch=expected_snapshot.name,
                parent_revision=expected_snapshot.head_revision,
                manifest_sha256="f" * 64,
                paths=[
                    SnapshotPath(
                        path="unexpected-source-change.txt",
                        state="deleted",
                    )
                ],
            )

        @staticmethod
        def current_base_revision() -> str:
            return "f" * 40

    checkpoints = iter(
        (
            ObservationCheckpoint(),
            checkpoint,
            checkpoint,
        )
    )
    monkeypatch.setattr("shea_halo.service.run_configured_stage", stage)
    monkeypatch.setattr(
        "shea_halo.service.discard_worktree_observation_changes",
        lambda *_args, **_kwargs: (),
    )
    investigator.synthesis_error = None
    investigator.synthesis_outcome = Outcome.READY_FOR_TODO
    investigator.synthesis_residual_risks = [f"synthesis risk {index}" for index in range(8)]
    finished.clear()

    await service._validate_candidate(
        config=config,
        github=cast(GitHubClient, _FakeGitHub([])),
        metadata=_metadata(),
        issue=issue,
        producer="halo-bot",
        head=head,
        manager=cast(WorkspaceManager, SourceMutationAndDriftManager()),
        workspace=workspace,
        workspace_branch=workspace.branch,
        run_id=validating_state.run_id,
        snapshot=validating_state.branch,
        preliminary_result=exhausted_preliminary,
        discussion="[]",
    )

    assert len(finished) == 1
    drift_result = finished[0].result
    assert drift_result is not None
    assert drift_result.outcome == Outcome.BLOCKED
    assert drift_result.turn_budget_exhausted is True
    assert turn_limit_risk in drift_result.residual_risks
    assert any(
        "changed repository source after publication" in risk
        for risk in drift_result.residual_risks
    )
    assert any("f" * 40 in risk for risk in drift_result.residual_risks)
    assert len(drift_result.residual_risks) == 12
    assert InvestigationResult.model_validate(drift_result.model_dump()) == drift_result


async def test_lock_conflict_skips_target_and_leaves_project_untouched(
    tmp_path: Path, monkeypatch: object
) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)

    def unexpected_client(_tracker: object) -> None:
        raise AssertionError("a lock conflict must not reach GitHub")

    monkeypatch.setattr(  # type: ignore[attr-defined]
        "shea_halo.service.GitHubClient", unexpected_client
    )
    service = HaloService([config])

    with target_worker_lock(config.logs_dir):
        await service.run_once()

    records = [
        json.loads(line)
        for line in (config.logs_dir / "worker.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["event"] == "worker_lock_conflict"


def test_service_log_rejects_symlinked_logs_directory(tmp_path: Path) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    config.logs_dir.parent.mkdir(parents=True)
    config.logs_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeFilesystemError, match="symlink or non-directory"):
        HaloService._log_target(config, "unsafe", {})

    assert list(outside.iterdir()) == []


def test_service_log_rejects_hardlinked_leaf(tmp_path: Path) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)
    config.logs_dir.mkdir(parents=True)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("protected\n", encoding="utf-8")
    (config.logs_dir / "worker.jsonl").hardlink_to(outside)

    with pytest.raises(RuntimeFilesystemError, match="regular file"):
        HaloService._log_target(config, "unsafe", {})

    assert outside.read_text(encoding="utf-8") == "protected\n"
