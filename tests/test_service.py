from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from conftest import write_target_config

from shea_halo.agent import Investigator
from shea_halo.config import HaloConfig
from shea_halo.github import GitHubClient, IssueComment, ProjectIssue, ProjectMetadata
from shea_halo.halo_engine import HaloEngineAdapter
from shea_halo.locking import target_worker_lock
from shea_halo.models import (
    BranchSnapshot,
    Evidence,
    ExecutedAction,
    Experiment,
    ExternalBlocker,
    GuideSnapshot,
    HaloState,
    InvestigationResult,
    Outcome,
    PublicationIntent,
    SnapshotPath,
    Verification,
)
from shea_halo.observation import (
    FileObservation,
    ObservationCheckpoint,
    SpanKindCount,
    TraceObservation,
    TraceValidation,
)
from shea_halo.runtime_fs import RuntimeFilesystemError
from shea_halo.service import HaloService
from shea_halo.workpad import find_head, next_entry, render_entry
from shea_halo.workspace import HaloWorkspace


def _successful_action() -> ExecutedAction:
    return ExecutedAction(
        index=0,
        kind="experiment",
        command=["pnpm", "test"],
        exit_code=0,
        stdout_sha256="b" * 64,
        stderr_sha256="c" * 64,
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
    assert continuing[:2] == ("experimenting", None)


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
            dataset=SimpleNamespace(files=[SimpleNamespace(sha256="0" * 64)]),
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
                ]
            ),
        ),
        candidate_revision="1" * 40,
    )

    assert complete.complete
    assert complete.halo_dataset_verified
    assert not wrong_dataset.complete
    assert not wrong_dataset.halo_dataset_verified
    assert not mixed_dataset.complete
    assert not mixed_dataset.halo_dataset_verified


def test_reentry_input_fingerprint_is_stable_and_tracks_new_discussion(
    tmp_path: Path,
) -> None:
    write_target_config(tmp_path)
    config = HaloConfig.load(tmp_path)
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

    assert first == same
    assert first != changed


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

    rendered = HaloService._discussion_context([workpad_comment, discussion])

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


class _FakeEngine:
    async def analyze(self, **_kwargs: object) -> str:
        return "HALO analysis"


class _FakeInvestigator:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, **_kwargs: object) -> InvestigationResult:
        self.calls += 1
        return InvestigationResult(
            outcome=Outcome.CONTINUE_RESEARCH,
            summary="More evidence is needed.",
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
    def __init__(self, root: Path) -> None:
        self.root = root

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
        raise AssertionError("continued research without changes must not publish a branch")


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
    head = find_head(
        github.comments,
        repository=config.repository,
        issue_number=20,
        trusted_producers={"halo-bot"},
    )
    assert head is not None
    assert head.entry.revision == 3
    assert head.entry.state.run_id == "halo-20-original"
    assert head.entry.state.phase == "experimenting"


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
    assert github.statuses == ["Todo"]
    head = find_head(
        github.comments,
        repository=config.repository,
        issue_number=20,
        trusted_producers={"halo-bot"},
    )
    assert head is not None
    assert head.entry.state.phase == "ready"
    assert head.entry.state.branch is not None
    assert head.entry.state.branch.head_revision == "1" * 40


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
