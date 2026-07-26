from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from shea_halo.agent import (
    Investigator,
    candidate_evidence_references,
    run_configured_stage,
    run_verification,
    validate_candidate_synthesis,
)
from shea_halo.config import HaloConfig, effective_catalyst_sdk_base_endpoint
from shea_halo.github import GitHubClient, IssueComment, ProjectIssue, ProjectMetadata
from shea_halo.halo_engine import HaloEngineAdapter
from shea_halo.integrations import preflight_halo_desktop
from shea_halo.locking import TargetLockUnavailable, target_worker_lock
from shea_halo.models import (
    BranchSnapshot,
    ExecutedAction,
    ExternalBlocker,
    HaloState,
    InvestigationResult,
    Outcome,
    Phase,
    StatusTransition,
)
from shea_halo.observation import (
    FileObservation,
    ObservationCheckpoint,
    TraceObservation,
    TraceValidation,
    discard_worktree_observation_changes,
    inventory_observations,
)
from shea_halo.provider import ApiMode, ProviderApiModeError
from shea_halo.runtime_fs import append_runtime_text
from shea_halo.security import sanitize_persisted_data, sanitize_persisted_text
from shea_halo.tracing import ResearchTracing
from shea_halo.workpad import (
    WorkpadHead,
    find_head,
    next_entry,
    render_entry,
    trusted_workpad_comment_ids,
)
from shea_halo.workspace import HaloWorkspace, WorkspaceManager, branch_name


class ServiceError(RuntimeError):
    pass


def _runtime_source_sha256(package_root: Path | None = None) -> str:
    """Identify the runtime code loaded by a newly started worker."""

    root = package_root or Path(__file__).resolve().parent
    digest = hashlib.sha256()
    sources = sorted(path for path in root.rglob("*.py") if path.is_file())
    if not sources:
        raise ServiceError("Shea Halo runtime contains no Python source files")
    for source in sources:
        relative = source.relative_to(root).as_posix().encode("utf-8")
        content = source.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


_RUNTIME_SOURCE_SHA256 = _runtime_source_sha256()
_MAX_RESIDUAL_RISKS = 12
_TURN_BUDGET_CHECKPOINT_RISK = (
    "The preliminary investigator exhausted its bounded turn budget. "
    "This exact-revision checkpoint must remain in Halo Research for a "
    "later investigation even if final synthesis recommends a terminal outcome."
)


def _bounded_residual_risks(
    risks: Iterable[str],
    *,
    retain: Iterable[str] = (),
) -> list[str]:
    """Deduplicate risks while retaining harness-owned checkpoint evidence."""

    retained = list(dict.fromkeys(retain))[:_MAX_RESIDUAL_RISKS]
    retained_set = set(retained)
    remaining = [risk for risk in dict.fromkeys(risks) if risk not in retained_set][
        : _MAX_RESIDUAL_RISKS - len(retained)
    ]
    return [*remaining, *retained]


def _checkpoint_invariant_risks(result: InvestigationResult) -> tuple[str, ...]:
    if not result.turn_budget_exhausted:
        return ()
    return (
        *result.residual_risks[:1],
        _TURN_BUDGET_CHECKPOINT_RISK,
    )


def _validated_result_with_residual_risks(
    result: InvestigationResult,
    *,
    add: Iterable[str] = (),
    retain: Iterable[str] = (),
) -> InvestigationResult:
    updated = result.model_copy(
        update={
            "residual_risks": _bounded_residual_risks(
                (*result.residual_risks, *add),
                retain=retain,
            )
        }
    )
    return InvestigationResult.model_validate(updated.model_dump())


def _provider_api_mode_blocker(
    api_mode: ApiMode,
    error: ProviderApiModeError,
    *,
    previous: InvestigationResult | None = None,
) -> InvestigationResult:
    risk = (
        f"The configured {api_mode!r} provider route returned HTTP {error.status_code}; "
        "Halo did not try the other API mode."
    )
    update = {
        "outcome": Outcome.BLOCKED,
        "summary": f"The configured provider does not expose the {api_mode!r} API mode.",
        "residual_risks": _bounded_residual_risks(
            (
                *(previous.residual_risks if previous is not None else ()),
                risk,
            ),
            retain=(risk,),
        ),
        "todo_handoff": None,
        "blocker": ExternalBlocker(
            category="service_unavailable",
            description=f"The configured provider rejected the {api_mode!r} API route.",
            required_action=(
                "Configure models.api_mode to a route the runtime provider supports, "
                "or select a compatible provider, then return the item to Halo Research."
            ),
        ),
        "blocker_verified": True,
    }
    if previous is None:
        return InvestigationResult(**update)
    return InvestigationResult.model_validate(previous.model_copy(update=update).model_dump())


class HaloService:
    def __init__(
        self,
        configs: list[HaloConfig],
        *,
        investigator: Investigator | None = None,
        halo_engine: HaloEngineAdapter | None = None,
        tracing: ResearchTracing | None = None,
    ) -> None:
        if not configs:
            raise ServiceError("at least one Shea Halo target is required")
        self.configs = configs
        self.tracing = tracing or ResearchTracing.disabled()
        self.investigator = investigator or Investigator(self.tracing.tracer)
        self.halo_engine = halo_engine or HaloEngineAdapter()
        self._terminal_items_seen: set[tuple[Path, str, str, str]] = set()

    async def run_once(self) -> None:
        for config in self.configs:
            try:
                with target_worker_lock(config.logs_dir):
                    github = GitHubClient(config.tracker)
                    metadata = github.project_metadata()
                    for issue in github.list_project_issues():
                        if issue.repository.casefold() != config.repository.casefold():
                            continue
                        try:
                            if issue.status == config.tracker.research_state:
                                self._terminal_items_seen = {
                                    key
                                    for key in self._terminal_items_seen
                                    if key[:2] != (config.root, issue.item_id)
                                }
                                try:
                                    await self._process(config, github, metadata, issue)
                                    if self.tracing.active:
                                        self.tracing.finalize_run("research_recorded")
                                except asyncio.CancelledError as exc:
                                    self.tracing.abort_run(exc)
                                    raise
                                except Exception as exc:
                                    self.tracing.abort_run(exc)
                                    raise
                            else:
                                terminal_key = (
                                    config.root,
                                    issue.item_id,
                                    issue.status,
                                    issue.updated_at,
                                )
                                if terminal_key not in self._terminal_items_seen:
                                    self._reconcile_terminal_transition(config, github, issue)
                                    self._terminal_items_seen.add(terminal_key)
                        except Exception as exc:
                            self._log(config, issue, "run_failed", {"error": str(exc)})
            except TargetLockUnavailable:
                # A lock conflict is normal worker coordination, not a Project
                # transition. The worker records it locally and touches no Issue.
                self._log_target(config, "worker_lock_conflict", {})

    async def _process(
        self,
        config: HaloConfig,
        github: GitHubClient,
        metadata: ProjectMetadata,
        issue: ProjectIssue,
    ) -> None:
        producer = github.current_user()
        comments = github.issue_comments(issue.repository, issue.number)
        trusted_producers = {producer, *config.tracker.trusted_producers}
        trusted_comment_ids = trusted_workpad_comment_ids(
            comments,
            repository=issue.repository,
            issue_number=issue.number,
            trusted_producers=trusted_producers,
        )
        discussion = self._discussion_context(
            comments,
            trusted_workpad_comment_ids=trusted_comment_ids,
        )
        head = find_head(
            comments,
            repository=issue.repository,
            issue_number=issue.number,
            trusted_producers=trusted_producers,
        )
        is_reentry = head is not None
        previous_result = head.entry.state.result if head is not None else None

        workspace_branch = (
            head.entry.state.workspace_branch
            if head is not None
            else branch_name(issue.number, issue.title)
        )
        run_id = (
            head.entry.state.run_id
            if head is not None
            else f"halo-{issue.number}-{uuid.uuid4().hex[:12]}"
        )
        if head is None:
            state = HaloState(
                issue_repository=issue.repository,
                issue_number=issue.number,
                run_id=run_id,
                phase="research",
                api_mode=config.api_mode,
                workspace_branch=workspace_branch,
            )
            head = self._append(
                github,
                issue,
                head,
                producer,
                state,
                "Claimed this `Halo Research` item and recorded its stable experiment branch.",
            )

        self.tracing.begin_run(
            config,
            halo_run_id=run_id,
            issue_number=issue.number,
        )

        github.assert_branch_has_no_pull_requests(issue.repository, workspace_branch)
        manager = WorkspaceManager(config)
        if (
            head.entry.state.phase == "experimenting"
            and head.entry.state.result is None
            and head.entry.state.publication_intent is None
        ):
            persisted_base = head.entry.state.base_revision
            if persisted_base is None:
                raise ServiceError("interrupted experiment has no persisted base revision")
            manager.recover_abandoned_attempt(
                issue_number=issue.number,
                persisted_branch=workspace_branch,
                persisted_base_revision=persisted_base,
                expected_snapshot=head.entry.state.branch,
            )
            recovered = HaloState(
                issue_repository=issue.repository,
                issue_number=issue.number,
                run_id=run_id,
                phase="research",
                api_mode=config.api_mode,
                workspace_branch=workspace_branch,
                base_revision=persisted_base,
                branch=head.entry.state.branch,
            )
            head = self._append(
                github,
                issue,
                head,
                producer,
                recovered,
                (
                    "Recovered a kill-interrupted attempt before publication. Halo "
                    "discarded its unrecorded source and ignored worktree state."
                ),
            )
            previous_result = None

        if head.entry.state.publication_intent is not None:
            pending = head.entry.state.publication_intent
            pending_result = head.entry.state.result
            if pending_result is None:
                raise ServiceError("pending publication has no persisted investigation result")
            with self.tracing.stage("workspace-preparation"):
                workspace = manager.prepare(
                    issue_number=issue.number,
                    title=issue.title,
                    persisted_branch=workspace_branch,
                    persisted_base_revision=head.entry.state.base_revision,
                    expected_snapshot=head.entry.state.branch,
                    publication_intent=pending,
                    allow_existing=True,
                )
            github.assert_branch_has_no_pull_requests(issue.repository, workspace_branch)
            github.assert_issue_remains_in_research(issue)
            with self.tracing.stage("candidate-publication"):
                snapshot = manager.publish_intent(
                    workspace,
                    pending,
                    issue_number=issue.number,
                    title=issue.title,
                    expected_snapshot=head.entry.state.branch,
                )
            workspace = manager.prepare_validation_workspace(
                workspace,
                issue_number=issue.number,
                persisted_base_revision=workspace.base_revision,
                expected_snapshot=snapshot,
            )
            validating = HaloState(
                issue_repository=issue.repository,
                issue_number=issue.number,
                run_id=run_id,
                phase="validating",
                api_mode=config.api_mode,
                workspace_branch=workspace_branch,
                base_revision=workspace.base_revision,
                branch=snapshot,
                validation_baseline=inventory_observations(config, workspace),
                input_fingerprint=head.entry.state.input_fingerprint,
                result=pending_result,
            )
            head = self._append(
                github,
                issue,
                head,
                producer,
                validating,
                (
                    f"Published `{snapshot.head_revision}` and started revision-bound "
                    "candidate validation."
                ),
            )
            await self._validate_candidate(
                config=config,
                github=github,
                metadata=metadata,
                issue=issue,
                producer=producer,
                head=head,
                manager=manager,
                workspace=workspace,
                workspace_branch=workspace_branch,
                run_id=run_id,
                snapshot=snapshot,
                preliminary_result=pending_result,
                discussion=discussion,
            )
            return

        if head.entry.state.phase == "validating" and head.entry.state.result is not None:
            preliminary_result = head.entry.state.result
            persisted_base = head.entry.state.base_revision
            if persisted_base is None:
                raise ServiceError("interrupted candidate validation has no persisted base")
            workspace = manager.recover_abandoned_attempt(
                issue_number=issue.number,
                persisted_branch=workspace_branch,
                persisted_base_revision=persisted_base,
                expected_snapshot=head.entry.state.branch,
            )
            recovered_validation = head.entry.state.model_copy(
                update={"updated_at": datetime.now(UTC)}
            )
            head = self._append(
                github,
                issue,
                head,
                producer,
                recovered_validation,
                (
                    "Recovered an interrupted revision-bound validation and discarded "
                    "all branch-external worktree state before rerunning it."
                ),
            )
            await self._validate_candidate(
                config=config,
                github=github,
                metadata=metadata,
                issue=issue,
                producer=producer,
                head=head,
                manager=manager,
                workspace=workspace,
                workspace_branch=workspace_branch,
                run_id=run_id,
                snapshot=head.entry.state.branch,
                preliminary_result=preliminary_result,
                discussion=discussion,
            )
            return

        if not os.getenv("OPENAI_API_KEY"):
            result = self._gate_outcome(
                InvestigationResult(
                    outcome=Outcome.BLOCKED,
                    summary="The local Halo worker has no OpenAI API credential.",
                    residual_risks=[
                        "Set OPENAI_API_KEY in the worker environment, then move the item "
                        "back to Halo Research."
                    ],
                    blocker=ExternalBlocker(
                        category="credential",
                        description="The local Halo worker has no OpenAI API credential.",
                        required_action="Set OPENAI_API_KEY in the worker environment.",
                    ),
                    blocker_verified=True,
                )
            )
            state = HaloState(
                issue_repository=issue.repository,
                issue_number=issue.number,
                run_id=run_id,
                phase="blocked",
                api_mode=config.api_mode,
                workspace_branch=workspace_branch,
                base_revision=head.entry.state.base_revision,
                branch=head.entry.state.branch,
                result=result,
            )
            self._finish(
                config=config,
                github=github,
                metadata=metadata,
                issue=issue,
                producer=producer,
                head=head,
                state=state,
            )
            return

        with self.tracing.stage("workspace-preparation"):
            workspace = manager.prepare(
                issue_number=issue.number,
                title=issue.title,
                persisted_branch=workspace_branch,
                persisted_base_revision=head.entry.state.base_revision,
                expected_snapshot=head.entry.state.branch,
                publication_intent=None,
                allow_existing=is_reentry,
            )
        current_base_revision = manager.current_base_revision()
        if current_base_revision != workspace.base_revision:
            result = self._base_revision_drift_result(
                InvestigationResult(
                    outcome=Outcome.CONTINUE_RESEARCH,
                    summary="The configured base branch advanced during Halo research.",
                ),
                persisted_revision=workspace.base_revision,
                current_revision=current_base_revision,
            )
            self._finish(
                config=config,
                github=github,
                metadata=metadata,
                issue=issue,
                producer=producer,
                head=head,
                state=HaloState(
                    issue_repository=issue.repository,
                    issue_number=issue.number,
                    run_id=run_id,
                    phase="blocked",
                    api_mode=config.api_mode,
                    workspace_branch=workspace_branch,
                    base_revision=workspace.base_revision,
                    branch=head.entry.state.branch,
                    result=result,
                ),
            )
            return
        starting_checkpoint = inventory_observations(config, workspace)
        input_fingerprint = self._input_fingerprint(
            config=config,
            issue=issue,
            discussion=discussion,
            checkpoint=starting_checkpoint,
            branch=head.entry.state.branch,
        )
        if (
            previous_result is not None
            and previous_result.outcome == Outcome.CONTINUE_RESEARCH
            and head.entry.state.input_fingerprint == input_fingerprint
        ):
            self.tracing.discard_unchanged_scan()
            return
        desktop_preflight = None
        if config.observation.desktop_preflight == "external":
            # External mode owns only this public-surface check. It never
            # discovers or manages the operator's Desktop process.
            effective_endpoint = effective_catalyst_sdk_base_endpoint(
                config.observation.catalyst_sdk_base_endpoint
            )
            with self.tracing.stage(
                "observation-preflight",
                attributes={"shea.observer.preflight.enabled": True},
            ):
                desktop_check = await preflight_halo_desktop(
                    effective_endpoint,
                    authorization_token=None,
                )
            desktop_preflight = desktop_check.receipt
            if not desktop_check.passed:
                self._log(
                    config,
                    issue,
                    "desktop_observer_unavailable",
                    {
                        "classification": desktop_check.receipt.classification,
                        "run_id": run_id,
                    },
                )
        else:
            with self.tracing.stage(
                "observation-preflight",
                attributes={"shea.observer.preflight.enabled": False},
            ):
                pass
        experimenting = HaloState(
            issue_repository=issue.repository,
            issue_number=issue.number,
            run_id=run_id,
            phase="experimenting",
            api_mode=config.api_mode,
            workspace_branch=workspace_branch,
            base_revision=workspace.base_revision,
            branch=head.entry.state.branch,
            input_fingerprint=input_fingerprint,
        )
        head = self._append(
            github,
            issue,
            head,
            producer,
            experimenting,
            (
                f"Prepared the managed worktree from `{workspace.base_revision}` "
                "and started trace-guided experiments."
            ),
        )

        try:
            with self.tracing.stage("halo-engine-analysis"):
                halo_analysis = await self.halo_engine.analyze(
                    config=config,
                    workspace=workspace,
                    run_id=run_id,
                    issue_title=issue.title,
                    issue_body=issue.body,
                )
            result = await self.investigator.run(
                config=config,
                workspace=workspace,
                run_id=run_id,
                issue_number=issue.number,
                issue_title=issue.title,
                issue_body=issue.body,
                issue_discussion=discussion,
                prior_result=previous_result,
                halo_analysis=halo_analysis,
            )
            result = result.model_copy(update={"desktop_preflight": desktop_preflight})
        except ProviderApiModeError as exc:
            manager.discard_unpublished_attempt(
                workspace,
                issue_number=issue.number,
                persisted_base_revision=workspace.base_revision,
                expected_snapshot=head.entry.state.branch,
            )
            result = _provider_api_mode_blocker(config.api_mode, exc).model_copy(
                update={"desktop_preflight": desktop_preflight}
            )
            self._finish(
                config=config,
                github=github,
                metadata=metadata,
                issue=issue,
                producer=producer,
                head=head,
                state=HaloState(
                    issue_repository=issue.repository,
                    issue_number=issue.number,
                    run_id=run_id,
                    phase="blocked",
                    api_mode=config.api_mode,
                    workspace_branch=workspace_branch,
                    base_revision=workspace.base_revision,
                    branch=head.entry.state.branch,
                    input_fingerprint=input_fingerprint,
                    result=result,
                ),
            )
            self._log(
                config,
                issue,
                "provider_api_mode_blocked",
                {
                    "api_mode": config.api_mode,
                    "status_code": exc.status_code,
                    "run_id": run_id,
                },
            )
            return
        except Exception as exc:
            manager.discard_unpublished_attempt(
                workspace,
                issue_number=issue.number,
                persisted_base_revision=workspace.base_revision,
                expected_snapshot=head.entry.state.branch,
            )
            result = InvestigationResult(
                outcome=Outcome.CONTINUE_RESEARCH,
                summary="The current research attempt stopped before publication.",
                residual_risks=[
                    (f"{type(exc).__name__}: {sanitize_persisted_text(str(exc))[:1_000]}")
                ],
                desktop_preflight=desktop_preflight,
            )
            failed = HaloState(
                issue_repository=issue.repository,
                issue_number=issue.number,
                run_id=run_id,
                phase="research",
                api_mode=config.api_mode,
                workspace_branch=workspace_branch,
                base_revision=workspace.base_revision,
                branch=head.entry.state.branch,
                input_fingerprint=input_fingerprint,
                result=result,
            )
            self._append(
                github,
                issue,
                head,
                producer,
                failed,
                (
                    "The attempt failed before a publication intent was recorded. "
                    "Halo discarded its uncommitted source changes and retained the "
                    "failure as shared evidence."
                ),
            )
            self._log(
                config,
                issue,
                "attempt_failed",
                {"error": str(exc), "run_id": run_id},
            )
            return

        current_changed_paths = tuple(result.changed_paths)
        if previous_result is not None and previous_result.trace_validation_required:
            result = result.model_copy(
                update={
                    "trace_validation_required": True,
                    "changed_paths": result.changed_paths or previous_result.changed_paths,
                }
            )

        snapshot = head.entry.state.branch
        if current_changed_paths:
            intent = manager.create_publication_intent(
                workspace,
                expected_snapshot=snapshot,
            )
            if intent is None:
                raise ServiceError("changed paths produced no publication intent")
            publishing = HaloState(
                issue_repository=issue.repository,
                issue_number=issue.number,
                run_id=run_id,
                phase="experimenting",
                api_mode=config.api_mode,
                workspace_branch=workspace_branch,
                base_revision=workspace.base_revision,
                branch=snapshot,
                publication_intent=intent,
                input_fingerprint=input_fingerprint,
                result=result,
            )
            head = self._append(
                github,
                issue,
                head,
                producer,
                publishing,
                "Recorded an immutable publication intent before publishing the experiment.",
            )
            github.assert_branch_has_no_pull_requests(issue.repository, workspace_branch)
            github.assert_issue_remains_in_research(issue)
            with self.tracing.stage("candidate-publication"):
                snapshot = manager.publish_intent(
                    workspace,
                    intent,
                    issue_number=issue.number,
                    title=issue.title,
                    expected_snapshot=head.entry.state.branch,
                )

        workspace = manager.prepare_validation_workspace(
            workspace,
            issue_number=issue.number,
            persisted_base_revision=workspace.base_revision,
            expected_snapshot=snapshot,
        )
        validating = HaloState(
            issue_repository=issue.repository,
            issue_number=issue.number,
            run_id=run_id,
            phase="validating",
            api_mode=config.api_mode,
            workspace_branch=workspace_branch,
            base_revision=workspace.base_revision,
            branch=snapshot,
            validation_baseline=inventory_observations(config, workspace),
            input_fingerprint=input_fingerprint,
            result=result,
        )
        github.assert_branch_has_no_pull_requests(issue.repository, workspace_branch)
        head = self._append(
            github,
            issue,
            head,
            producer,
            validating,
            (
                "The candidate revision is fixed. Halo is now rerunning the configured "
                "runtime experiment and verification against that exact revision."
            ),
        )
        await self._validate_candidate(
            config=config,
            github=github,
            metadata=metadata,
            issue=issue,
            producer=producer,
            head=head,
            manager=manager,
            workspace=workspace,
            workspace_branch=workspace_branch,
            run_id=run_id,
            snapshot=snapshot,
            preliminary_result=result,
            discussion=discussion,
        )

    def _reconcile_terminal_transition(
        self,
        config: HaloConfig,
        github: GitHubClient,
        issue: ProjectIssue,
    ) -> None:
        """Finish the workpad half of a status change that already reached GitHub."""

        producer = github.current_user()
        comments = github.issue_comments(issue.repository, issue.number)
        head = find_head(
            comments,
            repository=issue.repository,
            issue_number=issue.number,
            trusted_producers={producer, *config.tracker.trusted_producers},
        )
        if head is None:
            return
        transition = head.entry.state.status_transition
        if transition is None:
            return
        result = head.entry.state.result
        if result is None:
            raise ServiceError("pending status transition has no persisted result")
        expected_phase, expected_target, _ = self._route(config, result)
        if (
            expected_target is None
            or head.entry.state.phase != expected_phase
            or transition.target != expected_target
        ):
            raise ServiceError("status transition does not match its routed result")
        if transition.state == "applied":
            return
        if transition.target != issue.status:
            superseded = head.entry.state.model_copy(update={"status_transition": None})
            self._append(
                github,
                issue,
                head,
                producer,
                superseded,
                (
                    f"Project status `{issue.status}` superseded the pending Halo transition "
                    f"to `{transition.target}`; Halo made no automatic status change."
                ),
                require_research=False,
            )
            self._log(
                config,
                issue,
                "status_transition_mismatch",
                {
                    "current_status": issue.status,
                    "pending_target": transition.target,
                },
            )
            return
        applied = head.entry.state.model_copy(
            update={
                "status_transition": StatusTransition(
                    target=transition.target,
                    state="applied",
                    created_at=transition.created_at,
                    applied_at=datetime.now(UTC),
                )
            }
        )
        self._append(
            github,
            issue,
            head,
            producer,
            applied,
            (
                f"Confirmed the previously applied Project status "
                f"`{transition.target}` after worker recovery."
            ),
            require_research=False,
        )

    async def _validate_candidate(
        self,
        config: HaloConfig,
        *,
        github: GitHubClient,
        metadata: ProjectMetadata,
        issue: ProjectIssue,
        producer: str,
        head: WorkpadHead,
        manager: WorkspaceManager,
        workspace: HaloWorkspace,
        workspace_branch: str,
        run_id: str,
        snapshot: BranchSnapshot | None,
        preliminary_result: InvestigationResult,
        discussion: str,
    ) -> None:
        """Validate only evidence produced after the candidate revision is fixed."""

        workspace = manager.prepare_validation_workspace(
            workspace,
            issue_number=issue.number,
            persisted_base_revision=workspace.base_revision,
            expected_snapshot=snapshot,
        )
        candidate_revision = (
            snapshot.head_revision if snapshot is not None else workspace.base_revision
        )
        baseline = head.entry.state.validation_baseline
        if baseline is None:
            raise ServiceError("candidate validation has no persisted observation baseline")
        try:
            with self.tracing.stage("configured-runtime-setup"):
                setup_actions = run_configured_stage(
                    config,
                    workspace.path,
                    kind="setup",
                    stage="setup",
                    service_version=candidate_revision,
                    halo_run_id=run_id,
                    issue_number=issue.number,
                    candidate_revision=candidate_revision,
                )
            experiment_baseline = inventory_observations(config, workspace)
            with self.tracing.stage("configured-runtime-experiments"):
                experiment_actions = run_configured_stage(
                    config,
                    workspace.path,
                    kind="experiment",
                    stage="candidate_validation",
                    service_version=candidate_revision,
                    halo_run_id=run_id,
                    issue_number=issue.number,
                    candidate_revision=candidate_revision,
                )
            experiment_checkpoint = inventory_observations(config, workspace)
            with self.tracing.stage("exact-revision-verification"):
                verification, verification_actions = run_verification(
                    config,
                    workspace.path,
                    service_version=candidate_revision,
                    halo_run_id=run_id,
                    issue_number=issue.number,
                    candidate_revision=candidate_revision,
                )
            checkpoint = inventory_observations(config, workspace)
        except Exception as exc:
            manager.discard_unpublished_attempt(
                workspace,
                issue_number=issue.number,
                persisted_base_revision=workspace.base_revision,
                expected_snapshot=snapshot,
            )
            try:
                removed_observations = discard_worktree_observation_changes(
                    config,
                    workspace,
                    baseline,
                )
            except Exception as cleanup_exc:
                cleanup_failure_risk = (
                    "Revision-bound validation failed and its observation "
                    "artifact could not be discarded safely: "
                    f"{type(cleanup_exc).__name__}: "
                    f"{sanitize_persisted_text(str(cleanup_exc))[:1_000]}"
                )
                blocked = preliminary_result.model_copy(
                    update={
                        "outcome": Outcome.BLOCKED,
                        "blocker": ExternalBlocker(
                            category="permission",
                            description=(
                                "A failed validation left an observation artifact outside "
                                "Halo's safe cleanup boundary."
                            ),
                            required_action=(
                                "Inspect and remove or relocate the reported validation "
                                "artifact, then move the item back to Halo Research."
                            ),
                        ),
                        "blocker_verified": True,
                    }
                )
                blocked = _validated_result_with_residual_risks(
                    blocked,
                    add=(cleanup_failure_risk,),
                    retain=(
                        *_checkpoint_invariant_risks(preliminary_result),
                        cleanup_failure_risk,
                    ),
                )
                self._finish(
                    config=config,
                    github=github,
                    metadata=metadata,
                    issue=issue,
                    producer=producer,
                    head=head,
                    state=HaloState(
                        issue_repository=issue.repository,
                        issue_number=issue.number,
                        run_id=run_id,
                        phase="blocked",
                        api_mode=config.api_mode,
                        workspace_branch=workspace_branch,
                        base_revision=workspace.base_revision,
                        branch=snapshot,
                        result=blocked,
                    ),
                )
                self._log(
                    config,
                    issue,
                    "validation_cleanup_blocked",
                    {
                        "error": str(exc),
                        "cleanup_error": str(cleanup_exc),
                        "run_id": run_id,
                    },
                )
                return
            validation_failure_risk = (
                "Revision-bound validation failed: "
                f"{type(exc).__name__}: "
                f"{sanitize_persisted_text(str(exc))[:1_000]}"
            )
            failed = preliminary_result.model_copy(
                update={
                    "outcome": Outcome.CONTINUE_RESEARCH,
                }
            )
            failed = _validated_result_with_residual_risks(
                failed,
                add=(validation_failure_risk,),
                retain=(
                    *_checkpoint_invariant_risks(preliminary_result),
                    validation_failure_risk,
                ),
            )
            state = HaloState(
                issue_repository=issue.repository,
                issue_number=issue.number,
                run_id=run_id,
                phase="research",
                api_mode=config.api_mode,
                workspace_branch=workspace_branch,
                base_revision=workspace.base_revision,
                branch=snapshot,
                input_fingerprint=None,
                result=failed,
            )
            self._append(
                github,
                issue,
                head,
                producer,
                state,
                (
                    "Revision-bound validation stopped. Halo discarded non-ignored source "
                    f"changes and {len(removed_observations)} changed observation artifact(s), "
                    "then kept the item in `Halo Research` for a clean re-entry."
                ),
            )
            self._log(
                config,
                issue,
                "validation_failed",
                {"error": str(exc), "run_id": run_id},
            )
            return

        unexpected_intent = manager.create_publication_intent(
            workspace,
            expected_snapshot=snapshot,
        )
        source_clean = unexpected_intent is None
        if not source_clean:
            manager.discard_unpublished_attempt(
                workspace,
                issue_number=issue.number,
                persisted_base_revision=workspace.base_revision,
                expected_snapshot=snapshot,
            )

        candidate_traces, candidate_reports = self._stable_experiment_observations(
            before=experiment_baseline,
            after=experiment_checkpoint,
            final=checkpoint,
        )

        post_analysis = None
        if candidate_traces:
            try:
                with self.tracing.stage("halo-engine-analysis"):
                    post_analysis = await self.halo_engine.analyze(
                        config=config,
                        workspace=workspace,
                        run_id=f"{run_id}-candidate",
                        issue_title=issue.title,
                        issue_body=issue.body,
                        trace_sha256s=frozenset(trace.sha256 for trace in candidate_traces),
                    )
            except ProviderApiModeError as exc:
                manager.prepare_validation_workspace(
                    workspace,
                    issue_number=issue.number,
                    persisted_base_revision=workspace.base_revision,
                    expected_snapshot=snapshot,
                )
                blocked = _provider_api_mode_blocker(
                    config.api_mode,
                    exc,
                    previous=preliminary_result,
                )
                self._finish(
                    config=config,
                    github=github,
                    metadata=metadata,
                    issue=issue,
                    producer=producer,
                    head=head,
                    state=HaloState(
                        issue_repository=issue.repository,
                        issue_number=issue.number,
                        run_id=run_id,
                        phase="blocked",
                        api_mode=config.api_mode,
                        workspace_branch=workspace_branch,
                        base_revision=workspace.base_revision,
                        branch=snapshot,
                        result=blocked,
                    ),
                )
                self._log(
                    config,
                    issue,
                    "provider_api_mode_blocked",
                    {
                        "api_mode": config.api_mode,
                        "status_code": exc.status_code,
                        "run_id": run_id,
                    },
                )
                return

        analysis_sha256 = None
        trace_validation = TraceValidation(
            checkpoint=checkpoint,
            candidate_traces=candidate_traces,
            candidate_desktop_reports=candidate_reports,
            candidate_revision=candidate_revision,
        )
        validation_actions = [
            *setup_actions,
            *experiment_actions,
            *verification_actions,
        ]
        try:
            if post_analysis is not None and post_analysis.final_message:
                # This enforces the exact same bounded UTF-8 report contract used
                # by the prompt and evidence source before any digest is trusted.
                candidate_evidence_references(
                    candidate_revision=candidate_revision,
                    halo_analysis=post_analysis,
                    candidate_traces=candidate_traces,
                )
                analysis_sha256 = hashlib.sha256(
                    post_analysis.final_message.encode("utf-8")
                ).hexdigest()
            trace_validation = self._trace_validation(
                checkpoint=checkpoint,
                candidate_traces=candidate_traces,
                candidate_reports=candidate_reports,
                halo_analysis=post_analysis,
                candidate_revision=candidate_revision,
            )
            decision = await self.investigator.synthesize_candidate(
                config=config,
                issue_number=issue.number,
                issue_title=issue.title,
                issue_body=issue.body,
                issue_discussion=discussion,
                candidate_revision=candidate_revision,
                preliminary_result=preliminary_result,
                halo_analysis=post_analysis,
                candidate_traces=candidate_traces,
                trace_validation=trace_validation,
                validation_actions=validation_actions,
                verification=verification,
                source_clean=source_clean,
            )
            validate_candidate_synthesis(
                decision,
                candidate_revision=candidate_revision,
                halo_analysis=post_analysis,
                candidate_traces=candidate_traces,
                validation_actions=validation_actions,
            )
        except ProviderApiModeError as exc:
            manager.prepare_validation_workspace(
                workspace,
                issue_number=issue.number,
                persisted_base_revision=workspace.base_revision,
                expected_snapshot=snapshot,
            )
            blocked = _provider_api_mode_blocker(
                config.api_mode,
                exc,
                previous=preliminary_result,
            )
            self._finish(
                config=config,
                github=github,
                metadata=metadata,
                issue=issue,
                producer=producer,
                head=head,
                state=HaloState(
                    issue_repository=issue.repository,
                    issue_number=issue.number,
                    run_id=run_id,
                    phase="blocked",
                    api_mode=config.api_mode,
                    workspace_branch=workspace_branch,
                    base_revision=workspace.base_revision,
                    branch=snapshot,
                    result=blocked,
                ),
            )
            self._log(
                config,
                issue,
                "provider_api_mode_blocked",
                {
                    "api_mode": config.api_mode,
                    "status_code": exc.status_code,
                    "run_id": run_id,
                },
            )
            return
        except Exception as exc:
            manager.prepare_validation_workspace(
                workspace,
                issue_number=issue.number,
                persisted_base_revision=workspace.base_revision,
                expected_snapshot=snapshot,
            )
            synthesis_failure_risk = (
                "Final candidate synthesis failed closed: "
                f"{type(exc).__name__}: "
                f"{sanitize_persisted_text(str(exc))[:1_000]}"
            )
            failure_retained_risks: tuple[str, ...] = ()
            failure_risks = [synthesis_failure_risk]
            if preliminary_result.turn_budget_exhausted:
                failure_retained_risks = (
                    *_checkpoint_invariant_risks(preliminary_result),
                    synthesis_failure_risk,
                )
                failure_risks = [
                    *preliminary_result.residual_risks,
                    synthesis_failure_risk,
                    _TURN_BUDGET_CHECKPOINT_RISK,
                ]
            failed = InvestigationResult(
                outcome=Outcome.CONTINUE_RESEARCH,
                turn_budget_exhausted=preliminary_result.turn_budget_exhausted,
                summary=(
                    "The exact candidate evidence was collected, but its final "
                    "read-only synthesis did not complete."
                ),
                analysis_sha256=analysis_sha256,
                selected_guides=preliminary_result.selected_guides,
                changed_paths=preliminary_result.changed_paths,
                trace_validation_required=preliminary_result.trace_validation_required,
                trace_validation=trace_validation,
                executed_actions=[
                    *preliminary_result.executed_actions,
                    *validation_actions,
                ],
                verification=verification,
                residual_risks=_bounded_residual_risks(
                    failure_risks,
                    retain=failure_retained_risks,
                ),
                desktop_preflight=preliminary_result.desktop_preflight,
            )
            self._append(
                github,
                issue,
                head,
                producer,
                HaloState(
                    issue_repository=issue.repository,
                    issue_number=issue.number,
                    run_id=run_id,
                    phase="research",
                    api_mode=config.api_mode,
                    workspace_branch=workspace_branch,
                    base_revision=workspace.base_revision,
                    branch=snapshot,
                    input_fingerprint=None,
                    result=failed,
                ),
                (
                    "Final candidate synthesis failed closed. Halo kept the published "
                    "snapshot, discarded branch-external worktree state, and left the "
                    "item in `Halo Research` for a fresh re-entry."
                ),
            )
            self._log(
                config,
                issue,
                "candidate_synthesis_failed",
                {"error": str(exc), "run_id": run_id},
            )
            return
        result = InvestigationResult(
            outcome=decision.outcome,
            turn_budget_exhausted=preliminary_result.turn_budget_exhausted,
            summary=decision.summary,
            evidence=decision.evidence,
            analysis_sha256=analysis_sha256,
            competing_hypotheses=decision.competing_hypotheses,
            experiments=decision.experiments,
            selected_guides=preliminary_result.selected_guides,
            changed_paths=preliminary_result.changed_paths,
            trace_validation_required=preliminary_result.trace_validation_required,
            trace_validation=trace_validation,
            executed_actions=[
                *preliminary_result.executed_actions,
                *validation_actions,
            ],
            verification=verification,
            residual_risks=decision.residual_risks,
            todo_handoff=decision.todo_handoff,
            blocker=decision.blocker,
            blocker_verified=False,
            desktop_preflight=preliminary_result.desktop_preflight,
        )
        result = result.model_copy(
            update={
                "blocker_verified": self._blocker_verified_at_revision(
                    result,
                    validation_actions,
                    candidate_revision=candidate_revision,
                    allowed_exit_codes=config.external_blocker_exit_codes,
                )
            }
        )
        retained_result_risks: tuple[str, ...] = ()
        if preliminary_result.turn_budget_exhausted:
            retained_result_risks = (
                *_checkpoint_invariant_risks(preliminary_result),
                *result.residual_risks,
            )
            result = result.model_copy(
                update={
                    "outcome": Outcome.CONTINUE_RESEARCH,
                    "turn_budget_exhausted": True,
                    "summary": preliminary_result.summary,
                    "todo_handoff": None,
                    "blocker": None,
                    "blocker_verified": False,
                    "residual_risks": _bounded_residual_risks(
                        (
                            *preliminary_result.residual_risks,
                            *result.residual_risks,
                            _TURN_BUDGET_CHECKPOINT_RISK,
                        ),
                        retain=retained_result_risks,
                    ),
                }
            )
        if not source_clean:
            source_mutation_risk = (
                "A candidate-validation command changed repository source "
                "after publication; Halo discarded that unrecorded change."
            )
            retained_result_risks = (
                *retained_result_risks,
                source_mutation_risk,
            )
            result = result.model_copy(
                update={
                    "outcome": Outcome.CONTINUE_RESEARCH,
                    "residual_risks": [
                        *result.residual_risks,
                        source_mutation_risk,
                    ],
                }
            )
        current_base_revision = manager.current_base_revision()
        if current_base_revision != workspace.base_revision:
            prior_risks = set(result.residual_risks)
            result = self._base_revision_drift_result(
                result,
                persisted_revision=workspace.base_revision,
                current_revision=current_base_revision,
            )
            retained_result_risks = (
                *retained_result_risks,
                *(risk for risk in result.residual_risks if risk not in prior_risks),
            )
        prior_risks = set(result.residual_risks)
        result = self._gate_outcome(
            result,
            candidate_revision=candidate_revision,
        )
        retained_result_risks = (
            *retained_result_risks,
            *(risk for risk in result.residual_risks if risk not in prior_risks),
        )
        if result.outcome == Outcome.READY_FOR_TODO and result.changed_paths and snapshot is None:
            unpublished_snapshot_risk = (
                "A code-ready handoff requires a published experimental snapshot."
            )
            retained_result_risks = (
                *retained_result_risks,
                unpublished_snapshot_risk,
            )
            result = result.model_copy(
                update={
                    "outcome": Outcome.CONTINUE_RESEARCH,
                    "residual_risks": [
                        *result.residual_risks,
                        unpublished_snapshot_risk,
                    ],
                }
            )

        result = _validated_result_with_residual_risks(
            result,
            retain=retained_result_risks,
        )
        phase, _, _ = self._route(config, result)
        state = HaloState(
            issue_repository=issue.repository,
            issue_number=issue.number,
            run_id=run_id,
            phase=phase,
            api_mode=config.api_mode,
            workspace_branch=workspace_branch,
            base_revision=workspace.base_revision,
            branch=snapshot,
            input_fingerprint=self._input_fingerprint(
                config=config,
                issue=issue,
                discussion=discussion,
                checkpoint=checkpoint,
                branch=snapshot,
            ),
            result=result,
        )
        github.assert_branch_has_no_pull_requests(issue.repository, workspace_branch)
        self._finish(
            config=config,
            github=github,
            metadata=metadata,
            issue=issue,
            producer=producer,
            head=head,
            state=state,
        )
        self._log(
            config,
            issue,
            "run_completed",
            {
                "run_id": run_id,
                "outcome": result.outcome,
                "branch": snapshot.model_dump(mode="json") if snapshot else None,
            },
        )

    def _finish(
        self,
        *,
        config: HaloConfig,
        github: GitHubClient,
        metadata: ProjectMetadata,
        issue: ProjectIssue,
        producer: str,
        head: WorkpadHead,
        state: HaloState,
    ) -> WorkpadHead:
        if state.result is None:
            raise ServiceError("a routed Halo state requires an investigation result")
        phase, target_status, summary = self._route(config, state.result)
        pending_transition = (
            None
            if target_status is None
            else StatusTransition(target=target_status, state="pending")
        )
        routed = state.model_copy(
            update={
                "phase": phase,
                "status_transition": pending_transition,
            }
        )
        self.tracing.attach_result(state.result)
        with self.tracing.stage(
            "project-routing",
            attributes={
                "shea.project.routing.terminal": target_status is not None,
                "shea.project.routing.target": target_status or "Halo Research",
            },
        ):
            head = self._append(github, issue, head, producer, routed, summary)
        # Canonical evidence is authoritative and must be durable before a
        # terminal Project mutation can promote this research result.
        self.tracing.finalize_run(state.result.outcome.value)
        if target_status is None:
            return head

        github.assert_issue_remains_in_research(issue)
        github.set_status(metadata, issue.item_id, target_status)
        if pending_transition is None:
            raise ServiceError("terminal routing lost its pending status transition")
        applied = routed.model_copy(
            update={
                "status_transition": StatusTransition(
                    target=target_status,
                    state="applied",
                    created_at=pending_transition.created_at,
                    applied_at=datetime.now(UTC),
                )
            }
        )
        return self._append(
            github,
            issue,
            head,
            producer,
            applied,
            f"Applied the configured Project status `{target_status}`.",
            require_research=False,
        )

    @staticmethod
    def _append(
        github: GitHubClient,
        issue: ProjectIssue,
        head: WorkpadHead | None,
        producer: str,
        state: HaloState,
        summary: str,
        *,
        require_research: bool = True,
    ) -> WorkpadHead:
        if require_research:
            github.assert_issue_remains_in_research(issue)
        entry = next_entry(state, head, producer=producer)
        comment = github.add_comment(
            issue.repository,
            issue.number,
            render_entry(entry, summary),
        )
        if comment.author.casefold() != producer.casefold():
            raise ServiceError("GitHub persisted the Halo workpad under an unexpected author")
        return WorkpadHead(comment_id=comment.database_id, entry=entry)

    @staticmethod
    def _gate_outcome(
        result: InvestigationResult,
        *,
        candidate_revision: str | None = None,
    ) -> InvestigationResult:
        """Demote unsupported terminal claims to continued research."""

        missing: list[str] = []
        if candidate_revision is None and result.trace_validation is not None:
            candidate_revision = result.trace_validation.candidate_revision
        candidate_actions = [
            action
            for action in result.executed_actions
            if action.kind == "experiment" and action.stage == "candidate_validation"
        ]
        successful_candidate_indices = {
            action.index
            for action in candidate_actions
            if action.exit_code == 0
            and action.service_version
            and (candidate_revision is None or action.service_version == candidate_revision)
        }
        setup_actions = [action for action in result.executed_actions if action.stage == "setup"]
        experiments_are_bound = bool(result.experiments) and all(
            experiment.action_index in successful_candidate_indices
            and experiment.hypothesis.strip()
            and experiment.action.strip()
            and experiment.result.strip()
            for experiment in result.experiments
        )
        candidate_runtime_passed = bool(candidate_actions) and all(
            action.exit_code == 0
            and action.service_version
            and (candidate_revision is None or action.service_version == candidate_revision)
            for action in candidate_actions
        )
        setup_passed = all(action.exit_code == 0 for action in setup_actions)
        if result.outcome == Outcome.BLOCKED:
            if (
                result.blocker is None
                or not result.blocker_verified
                or not result.blocker.description.strip()
                or not result.blocker.required_action.strip()
            ):
                missing.append("A blocked outcome requires a structured external blocker.")
        elif result.outcome == Outcome.READY_FOR_TODO:
            if result.analysis_sha256 is None:
                missing.append("A Todo handoff requires runtime HALO analysis evidence.")
            if not result.evidence or any(
                not item.claim.strip() or not item.source.strip() for item in result.evidence
            ):
                missing.append("A Todo handoff requires evidence.")
            if not experiments_are_bound:
                missing.append(
                    "A Todo handoff requires completed experiments bound to successful "
                    "revision-bound runtime actions."
                )
            if not result.todo_handoff or not result.todo_handoff.strip():
                missing.append("A Todo handoff requires implementation guidance.")
            if not setup_passed:
                missing.append("A Todo handoff requires all configured setup actions to pass.")
            if not candidate_runtime_passed:
                missing.append(
                    "A Todo handoff requires every configured revision-bound runtime "
                    "experiment to pass."
                )
            if result.trace_validation_required and (
                not result.selected_guides or any(guide.stale for guide in result.selected_guides)
            ):
                missing.append(
                    "Tracing changes require at least one current framework guide "
                    "and no stale selected guide."
                )
            if result.trace_validation_required and (
                result.trace_validation is None or not result.trace_validation.complete
            ):
                missing.append(
                    "A Todo handoff requires a new candidate trace hierarchy, exact-dataset "
                    "post-experiment HALO Engine analysis, and the published candidate revision."
                )
            if not result.verification or any(
                check.status != "passed" for check in result.verification
            ):
                missing.append("A Todo handoff requires all recorded verification to pass.")
        elif result.outcome == Outcome.NO_CHANGE:
            if result.analysis_sha256 is None:
                missing.append("A no-change conclusion requires runtime HALO analysis evidence.")
            if not result.evidence or any(
                not item.claim.strip() or not item.source.strip() for item in result.evidence
            ):
                missing.append("A no-change conclusion requires evidence.")
            if not experiments_are_bound:
                missing.append(
                    "A no-change conclusion requires completed experiments bound to successful "
                    "revision-bound runtime actions."
                )
            if not setup_passed:
                missing.append("A no-change conclusion requires all setup actions to pass.")
            if not candidate_runtime_passed:
                missing.append(
                    "A no-change conclusion requires every configured revision-bound "
                    "runtime experiment to pass."
                )
            if result.trace_validation_required and (
                not result.selected_guides or any(guide.stale for guide in result.selected_guides)
            ):
                missing.append(
                    "Tracing experiments require at least one current framework guide "
                    "and no stale selected guide."
                )
            if result.trace_validation_required and (
                result.trace_validation is None or not result.trace_validation.complete
            ):
                missing.append(
                    "A tracing-based no-change conclusion requires a new candidate trace "
                    "hierarchy, exact-dataset HALO analysis, and the published revision."
                )
            if not result.verification or any(
                check.status != "passed" for check in result.verification
            ):
                missing.append(
                    "A no-change conclusion requires all deterministic verification to pass."
                )

        if not missing:
            return result
        residual_risks = list(result.residual_risks)
        for reason in missing:
            if reason not in residual_risks:
                residual_risks.append(reason)
        return result.model_copy(
            update={
                "outcome": Outcome.CONTINUE_RESEARCH,
                "residual_risks": residual_risks,
            }
        )

    @staticmethod
    def _base_revision_drift_result(
        result: InvestigationResult,
        *,
        persisted_revision: str,
        current_revision: str,
    ) -> InvestigationResult:
        return result.model_copy(
            update={
                "outcome": Outcome.BLOCKED,
                "summary": "The configured base branch advanced during Halo research.",
                "residual_risks": [
                    *result.residual_risks,
                    (
                        "The research lifecycle is pinned to "
                        f"`{persisted_revision}` while the configured base now resolves to "
                        f"`{current_revision}`. Do not promote evidence across that drift."
                    ),
                ],
                "blocker": ExternalBlocker(
                    category="missing_input",
                    description=(
                        "The target base branch advanced after this Halo lifecycle was pinned."
                    ),
                    required_action=(
                        "Start a fresh Halo Research lifecycle for the new base, or explicitly "
                        "review the pinned snapshot before deciding how to proceed."
                    ),
                ),
                "blocker_verified": True,
            }
        )

    @staticmethod
    def _blocker_verified_at_revision(
        result: InvestigationResult,
        actions: list[ExecutedAction],
        *,
        candidate_revision: str,
        allowed_exit_codes: frozenset[int],
    ) -> bool:
        blocker = result.blocker
        if result.outcome != Outcome.BLOCKED or blocker is None:
            return False
        failed_index = blocker.failed_action_index
        if failed_index is None:
            return False
        if any(action.stage == "setup" and action.exit_code != 0 for action in actions):
            return False
        matching = [
            action
            for action in actions
            if action.index == failed_index
            and action.kind == "experiment"
            and action.stage == "candidate_validation"
            and action.service_version == candidate_revision
            and action.exit_code in allowed_exit_codes
        ]
        return len(matching) == 1

    @staticmethod
    def _trace_validation(
        *,
        checkpoint: ObservationCheckpoint,
        candidate_traces: tuple[TraceObservation, ...],
        candidate_reports: tuple[FileObservation, ...],
        halo_analysis: object,
        candidate_revision: str | None,
    ) -> TraceValidation:
        report_digest: str | None = None
        dataset_digests: set[str] = set()
        dataset_revision: str | None = None
        if halo_analysis is not None:
            final_message = getattr(halo_analysis, "final_message", None)
            dataset = getattr(halo_analysis, "dataset", None)
            files = getattr(dataset, "files", ()) if dataset is not None else ()
            raw_revision = (
                getattr(dataset, "harness_revision", None) if dataset is not None else None
            )
            if isinstance(raw_revision, str):
                dataset_revision = raw_revision
            if isinstance(final_message, str) and final_message:
                report_digest = hashlib.sha256(final_message.encode()).hexdigest()
            dataset_digests = {
                digest
                for item in files
                if isinstance((digest := getattr(item, "sha256", None)), str)
            }
        candidate_digests = {item.sha256 for item in candidate_traces}
        dataset_verified = bool(candidate_digests and candidate_digests == dataset_digests)
        revision_verified = bool(candidate_revision and dataset_revision == candidate_revision)
        service_version_verified = bool(
            candidate_revision
            and candidate_traces
            and all(
                item.label.startswith("worktree/")
                and item.service_versions == (candidate_revision,)
                and item.service_version_span_count == item.span_count
                for item in candidate_traces
            )
        )
        complete = bool(
            candidate_traces
            and report_digest
            and dataset_verified
            and revision_verified
            and service_version_verified
            and any(item.parented_span_count > 0 for item in candidate_traces)
        )
        return TraceValidation(
            checkpoint=checkpoint,
            candidate_traces=candidate_traces,
            candidate_desktop_reports=candidate_reports,
            halo_report_sha256=report_digest,
            halo_dataset_verified=dataset_verified,
            halo_revision_verified=revision_verified,
            service_version_verified=service_version_verified,
            candidate_revision=candidate_revision,
            complete=complete,
        )

    @staticmethod
    def _stable_experiment_observations(
        *,
        before: ObservationCheckpoint,
        after: ObservationCheckpoint,
        final: ObservationCheckpoint,
    ) -> tuple[tuple[TraceObservation, ...], tuple[FileObservation, ...]]:
        """Select only worktree observations produced by the experiment stage.

        Setup output is already present in ``before``. Verification-only output
        appears only in ``final``. An experiment observation is accepted only
        when its complete inventory record is unchanged after verification.
        """

        delta = after.delta_from(before)
        final_traces = {item.label: item for item in final.traces}
        final_reports = {item.label: item for item in final.desktop_reports}
        traces = tuple(
            trace
            for trace in (
                *delta.new_traces,
                *(item.current for item in delta.changed_traces),
            )
            if trace.label.startswith("worktree/") and final_traces.get(trace.label) == trace
        )
        reports = tuple(
            report
            for report in (
                *delta.new_desktop_reports,
                *(item.current for item in delta.changed_desktop_reports),
            )
            if report.label.startswith("worktree/") and final_reports.get(report.label) == report
        )
        return traces, reports

    @staticmethod
    def _discussion_context(
        comments: list[IssueComment],
        *,
        trusted_workpad_comment_ids: frozenset[int],
    ) -> str:
        discussion: list[dict[str, object]] = []
        for comment in comments:
            if comment.database_id in trusted_workpad_comment_ids:
                continue
            discussion.append(
                {
                    "comment_id": comment.database_id,
                    "author": comment.author,
                    "updated_at": comment.updated_at or comment.created_at,
                    "body": sanitize_persisted_text(comment.body[:4_000]),
                }
            )
        return json.dumps(
            discussion[-20:],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _input_fingerprint(
        *,
        config: HaloConfig,
        issue: ProjectIssue,
        discussion: str,
        checkpoint: ObservationCheckpoint,
        branch: BranchSnapshot | None,
    ) -> str:
        branch_payload = branch.model_dump(mode="json") if branch is not None else None
        runtime_environment = {
            name: os.environ.get(name) is not None for name in config.target_environment_variables
        }
        effective_catalyst_endpoint = effective_catalyst_sdk_base_endpoint(
            config.observation.catalyst_sdk_base_endpoint
        )
        payload = {
            "daily_refresh": datetime.now(UTC).date().isoformat(),
            "issue": {
                "title": issue.title,
                "body": issue.body,
                "discussion": discussion,
            },
            "observation": checkpoint.model_dump(mode="json"),
            "branch": branch_payload,
            "runtime": {
                "shea_halo_source_sha256": _RUNTIME_SOURCE_SHA256,
            },
            "configuration": {
                "repository": config.repository,
                "base_branch": config.base_branch,
                "trace_globs": config.observation.trace_globs,
                "desktop_report_globs": config.observation.desktop_report_globs,
                "catalyst_sdk_base_endpoint": effective_catalyst_endpoint,
                "desktop_preflight": config.observation.desktop_preflight,
                "require_candidate_traces": config.observation.require_candidate_traces,
                "experiment_environment_presence": runtime_environment,
                "setup_commands": config.setup_commands,
                "experiment_commands": config.experiment_commands,
                "external_blocker_exit_codes": sorted(config.external_blocker_exit_codes),
                "verification_commands": config.verification_commands,
                "model": config.model,
                "api_mode": config.api_mode,
                "investigator_max_turns": config.investigator_max_turns,
                "halo_model": config.halo_model,
            },
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(serialized.encode()).hexdigest()

    @staticmethod
    def _route(config: HaloConfig, result: InvestigationResult) -> tuple[Phase, str | None, str]:
        if result.outcome == Outcome.READY_FOR_TODO:
            return (
                "ready",
                config.tracker.ready_state,
                (
                    "Research and experiments are complete. The same Project item is ready "
                    "for Shea Symphony Todo; any Halo branch is reference-only."
                ),
            )
        if result.outcome == Outcome.NO_CHANGE:
            return (
                "done",
                config.tracker.done_state,
                (
                    "The hypothesis was resolved and no implementation change is recommended. "
                    "Any Halo snapshot remains reference-only; the research item is done."
                ),
            )
        if result.outcome == Outcome.BLOCKED:
            return (
                "blocked",
                config.tracker.blocked_state,
                "A genuine external dependency requires human input before research can continue.",
            )
        return (
            "research",
            None,
            "Evidence or verification remains incomplete; the item stays in Halo Research.",
        )

    @staticmethod
    def _log(
        config: HaloConfig,
        issue: ProjectIssue,
        event: str,
        payload: dict[str, object],
    ) -> None:
        HaloService._log_target(
            config,
            event,
            {
                "repository": issue.repository,
                "issue_number": issue.number,
                **payload,
            },
        )

    @staticmethod
    def _log_target(
        config: HaloConfig,
        event: str,
        payload: dict[str, object],
    ) -> None:
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "repository": config.repository,
            **payload,
        }
        rendered = json.dumps(
            sanitize_persisted_data(
                record,
                path_labels={
                    config.root: "<target>",
                    config.logs_dir: "<logs>",
                    config.artifacts_dir: "<artifacts>",
                    config.worktrees_dir: "<worktrees>",
                },
            ),
            ensure_ascii=False,
            default=str,
        )
        append_runtime_text(
            config.root,
            config.logs_dir / "worker.jsonl",
            f"{rendered}\n",
        )


def targets_from_environment() -> list[HaloConfig]:
    raw = os.getenv("SHEA_HALO_TARGETS", "")
    roots = [Path(value) for value in raw.split(os.pathsep) if value]
    if not roots:
        raise ServiceError("SHEA_HALO_TARGETS must list local target checkout roots for the worker")
    return [HaloConfig.load(root) for root in roots]


async def run_worker() -> None:
    configs = targets_from_environment()
    poll_seconds = int(os.getenv("SHEA_HALO_POLL_SECONDS", "60"))
    if poll_seconds < 10:
        raise ServiceError("SHEA_HALO_POLL_SECONDS must be at least 10")
    tracing = ResearchTracing.initialize(configs)
    service = HaloService(configs, tracing=tracing)
    try:
        while True:
            await service.run_once()
            await asyncio.sleep(poll_seconds)
    finally:
        tracing.shutdown()
