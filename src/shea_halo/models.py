from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from shea_halo.observation import TraceValidation


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Outcome(StrEnum):
    READY_FOR_TODO = "ready_for_todo"
    NO_CHANGE = "no_change"
    CONTINUE_RESEARCH = "continue_research"
    BLOCKED = "blocked"


Phase: TypeAlias = Literal["research", "experimenting", "ready", "done", "blocked"]


class Evidence(StrictModel):
    claim: str = Field(max_length=1_000)
    source: str = Field(max_length=400)


class Experiment(StrictModel):
    hypothesis: str = Field(max_length=800)
    action: str = Field(max_length=800)
    result: str = Field(max_length=800)


class Verification(StrictModel):
    command: list[str] = Field(max_length=32)
    status: Literal["passed", "failed", "not_run"]
    evidence: str = Field(max_length=1_000)


class ExecutedAction(StrictModel):
    index: int = Field(ge=0)
    kind: Literal["experiment", "verification"]
    command: list[str] = Field(max_length=32)
    exit_code: int
    stdout_sha256: str = Field(min_length=64, max_length=64)
    stderr_sha256: str = Field(min_length=64, max_length=64)


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


class HaloState(StrictModel):
    issue_repository: str
    issue_number: int
    run_id: str
    phase: Phase
    workspace_branch: str
    base_revision: str | None = None
    branch: BranchSnapshot | None = None
    publication_intent: PublicationIntent | None = None
    input_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    result: InvestigationResult | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WorkpadEntry(StrictModel):
    schema_version: Literal["shea-halo/workpad-v1"] = "shea-halo/workpad-v1"
    revision: int
    predecessor_comment_id: int | None
    producer: str
    state: HaloState
