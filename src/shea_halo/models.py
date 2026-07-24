from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shea_halo.observation import ObservationCheckpoint, TraceValidation


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Outcome(StrEnum):
    READY_FOR_TODO = "ready_for_todo"
    NO_CHANGE = "no_change"
    CONTINUE_RESEARCH = "continue_research"
    BLOCKED = "blocked"


Phase: TypeAlias = Literal[
    "research",
    "experimenting",
    "validating",
    "ready",
    "done",
    "blocked",
]


class Evidence(StrictModel):
    claim: str = Field(max_length=1_000)
    source: str = Field(max_length=400)


class Experiment(StrictModel):
    action_index: int = Field(ge=0)
    hypothesis: str = Field(max_length=800)
    action: str = Field(max_length=800)
    result: str = Field(max_length=800)


class Verification(StrictModel):
    command: list[str] = Field(max_length=32)
    status: Literal["passed", "failed", "not_run"]
    evidence: str = Field(max_length=1_000)


class ExecutedAction(StrictModel):
    index: int = Field(ge=0)
    kind: Literal["setup", "experiment", "verification"]
    stage: Literal["setup", "exploration", "candidate_validation", "verification"]
    command: list[str] = Field(max_length=32)
    exit_code: int
    stdout_sha256: str = Field(min_length=64, max_length=64)
    stderr_sha256: str = Field(min_length=64, max_length=64)
    service_version: str | None = Field(default=None, min_length=1, max_length=128)


class GuideSnapshot(StrictModel):
    title: str = Field(max_length=300)
    url: str = Field(max_length=1_000)
    retrieved_at: datetime
    sha256: str = Field(min_length=64, max_length=64)
    stale: bool = False
    framework_evidence: list[str] = Field(max_length=20)
    selected_because: str = Field(max_length=1_000)


class GuideDecision(StrictModel):
    url: str = Field(max_length=1_000)
    selected_because: str = Field(max_length=1_000)


class ExternalBlocker(StrictModel):
    category: Literal[
        "credential",
        "network",
        "permission",
        "missing_input",
        "service_unavailable",
    ]
    description: str
    required_action: str
    failed_action_index: int | None = Field(default=None, ge=0)


class AgentDecision(StrictModel):
    outcome: Outcome
    summary: str = Field(max_length=2_000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    competing_hypotheses: list[str] = Field(default_factory=list, max_length=8)
    experiments: list[Experiment] = Field(default_factory=list, max_length=6)
    guide_decisions: list[GuideDecision] = Field(default_factory=list, max_length=6)
    residual_risks: list[str] = Field(default_factory=list, max_length=8)
    todo_handoff: str | None = Field(default=None, max_length=4_000)
    blocker: ExternalBlocker | None = None


class InvestigationResult(StrictModel):
    outcome: Outcome
    summary: str = Field(max_length=2_000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    analysis_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    competing_hypotheses: list[str] = Field(default_factory=list, max_length=8)
    experiments: list[Experiment] = Field(default_factory=list, max_length=6)
    selected_guides: list[GuideSnapshot] = Field(default_factory=list, max_length=6)
    changed_paths: list[str] = Field(default_factory=list, max_length=50)
    trace_validation_required: bool = False
    trace_validation: TraceValidation | None = None
    executed_actions: list[ExecutedAction] = Field(default_factory=list, max_length=30)
    verification: list[Verification] = Field(default_factory=list, max_length=20)
    residual_risks: list[str] = Field(default_factory=list, max_length=12)
    todo_handoff: str | None = Field(default=None, max_length=4_000)
    blocker: ExternalBlocker | None = None
    blocker_verified: bool = False


class BranchSnapshot(StrictModel):
    name: str
    head_revision: str
    reuse_policy: Literal["experimental_snapshot_only"] = "experimental_snapshot_only"


class SnapshotPath(StrictModel):
    path: str = Field(min_length=1, max_length=1_000)
    state: Literal["file", "deleted"]
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    git_blob_oid: str | None = Field(default=None, min_length=40, max_length=64)
    size: int = Field(default=0, ge=0)
    executable: bool = False


class PublicationIntent(StrictModel):
    branch: str
    parent_revision: str = Field(min_length=40, max_length=64)
    manifest_sha256: str = Field(min_length=64, max_length=64)
    paths: list[SnapshotPath] = Field(min_length=1, max_length=100)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StatusTransition(StrictModel):
    target: str = Field(min_length=1, max_length=200)
    state: Literal["pending", "applied"]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    applied_at: datetime | None = None

    @model_validator(mode="after")
    def _timestamps_match_state(self) -> StatusTransition:
        if self.state == "pending" and self.applied_at is not None:
            raise ValueError("a pending status transition cannot have applied_at")
        if self.state == "applied" and self.applied_at is None:
            raise ValueError("an applied status transition requires applied_at")
        return self


class HaloState(StrictModel):
    issue_repository: str
    issue_number: int
    run_id: str
    phase: Phase
    workspace_branch: str
    base_revision: str | None = None
    branch: BranchSnapshot | None = None
    publication_intent: PublicationIntent | None = None
    validation_baseline: ObservationCheckpoint | None = None
    status_transition: StatusTransition | None = None
    input_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    result: InvestigationResult | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _lifecycle_fields_match_phase(self) -> HaloState:
        if self.phase == "validating":
            if (
                self.base_revision is None
                or self.result is None
                or self.validation_baseline is None
            ):
                raise ValueError(
                    "validating state requires base_revision, result, and validation_baseline"
                )
            if self.publication_intent is not None or self.status_transition is not None:
                raise ValueError(
                    "validating state cannot contain publication or status-transition intent"
                )
        elif self.validation_baseline is not None:
            raise ValueError("validation_baseline is only valid during candidate validation")

        if self.publication_intent is not None and (
            self.phase != "experimenting"
            or self.base_revision is None
            or self.result is None
            or self.status_transition is not None
        ):
            raise ValueError(
                "publication intent requires an experimenting state with base and result"
            )

        if self.status_transition is not None and (
            self.phase not in {"ready", "done", "blocked"} or self.result is None
        ):
            raise ValueError("status transition requires a routed terminal state with result")
        if self.phase in {"ready", "done", "blocked"} and self.result is None:
            raise ValueError("a routed terminal state requires an investigation result")
        terminal_outcomes = {
            "ready": Outcome.READY_FOR_TODO,
            "done": Outcome.NO_CHANGE,
            "blocked": Outcome.BLOCKED,
        }
        expected_outcome = terminal_outcomes.get(self.phase)
        if (
            expected_outcome is not None
            and self.result is not None
            and self.result.outcome != expected_outcome
        ):
            raise ValueError("terminal Halo phase does not match its investigation outcome")
        return self


class WorkpadEntry(StrictModel):
    schema_version: Literal["shea-halo/workpad-v1"] = "shea-halo/workpad-v1"
    revision: int
    predecessor_comment_id: int | None
    producer: str
    state: HaloState
