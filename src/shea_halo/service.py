from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from shea_halo.agent import Investigator
from shea_halo.config import HaloConfig
from shea_halo.github import GitHubClient, IssueComment, ProjectIssue, ProjectMetadata
from shea_halo.halo_engine import HaloEngineAdapter
from shea_halo.locking import TargetLockUnavailable, target_worker_lock
from shea_halo.models import (
    BranchSnapshot,
    ExternalBlocker,
    HaloState,
    InvestigationResult,
    Outcome,
    Phase,
)
from shea_halo.observation import (
    FileObservation,
    ObservationCheckpoint,
    TraceObservation,
    TraceValidation,
    inventory_observations,
)
from shea_halo.runtime_fs import append_runtime_text
from shea_halo.security import sanitize_persisted_data, sanitize_persisted_text
from shea_halo.workpad import MARKER, WorkpadHead, find_head, next_entry, render_entry
from shea_halo.workspace import WorkspaceManager, branch_name


class ServiceError(RuntimeError):
    pass


class HaloService:
    def __init__(
        self,
        configs: list[HaloConfig],
        *,
        investigator: Investigator | None = None,
        halo_engine: HaloEngineAdapter | None = None,
    ) -> None:
        if not configs:
            raise ServiceError("at least one Shea Halo target is required")
        self.configs = configs
        self.investigator = investigator or Investigator()
        self.halo_engine = halo_engine or HaloEngineAdapter()

    async def run_once(self) -> None:
        for config in self.configs:
            try:
                with target_worker_lock(config.logs_dir):
                    github = GitHubClient(config.tracker)
                    metadata = github.project_metadata()
                    for issue in github.list_research_issues():
                        if issue.repository.casefold() != config.repository.casefold():
                            continue
                        try:
                            await self._process(config, github, metadata, issue)
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
        discussion = self._discussion_context(comments)
        head = find_head(
            comments,
            repository=issue.repository,
            issue_number=issue.number,
            trusted_producers={producer, *config.tracker.trusted_producers},
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

        github.assert_branch_has_no_pull_requests(issue.repository, workspace_branch)
        manager = WorkspaceManager(config)
        if head.entry.state.publication_intent is not None:
            pending = head.entry.state.publication_intent
            pending_result = head.entry.state.result
            if pending_result is None:
                raise ServiceError("pending publication has no persisted investigation result")
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
            snapshot = manager.publish_intent(
                workspace,
                pending,
                issue_number=issue.number,
                title=issue.title,
                expected_snapshot=head.entry.state.branch,
            )
            pending_result = pending_result.model_copy(
                update={
                    "trace_validation": self._with_candidate_revision(
                        pending_result.trace_validation,
                        snapshot.head_revision,
                    )
                }
            )
            result = self._gate_outcome(pending_result)
            phase, target_status, summary = self._route(config, result)
            final_state = HaloState(
                issue_repository=issue.repository,
                issue_number=issue.number,
                run_id=run_id,
                phase=phase,
                workspace_branch=workspace_branch,
                base_revision=workspace.base_revision,
                branch=snapshot,
                input_fingerprint=self._input_fingerprint(
                    config=config,
                    issue=issue,
                    discussion=discussion,
                    checkpoint=inventory_observations(config, workspace),
                    branch=snapshot,
                ),
                result=result,
            )
            github.assert_branch_has_no_pull_requests(issue.repository, workspace_branch)
            self._append(github, issue, head, producer, final_state, summary)
            if target_status is not None:
                github.assert_issue_remains_in_research(issue)
                github.set_status(metadata, issue.item_id, target_status)
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
            phase, target_status, summary = self._route(config, result)
            state = HaloState(
                issue_repository=issue.repository,
                issue_number=issue.number,
                run_id=run_id,
                phase=phase,
                workspace_branch=workspace_branch,
                base_revision=head.entry.state.base_revision,
                branch=head.entry.state.branch,
                result=result,
            )
            self._append(
                github,
                issue,
                head,
                producer,
                state,
                summary,
            )
            if target_status is not None:
                github.assert_issue_remains_in_research(issue)
                github.set_status(metadata, issue.item_id, target_status)
            return

        workspace = manager.prepare(
            issue_number=issue.number,
            title=issue.title,
            persisted_branch=workspace_branch,
            persisted_base_revision=head.entry.state.base_revision,
            expected_snapshot=head.entry.state.branch,
            publication_intent=None,
            allow_existing=is_reentry,
        )
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
            return
        experimenting = HaloState(
            issue_repository=issue.repository,
            issue_number=issue.number,
            run_id=run_id,
            phase="experimenting",
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
        current_changed_paths = tuple(result.changed_paths)
        if previous_result is not None and previous_result.trace_validation_required:
            result = result.model_copy(
                update={
                    "trace_validation_required": True,
                    "changed_paths": result.changed_paths or previous_result.changed_paths,
                }
            )

        checkpoint = inventory_observations(config, workspace)
        previous_validation = (
            previous_result.trace_validation if previous_result is not None else None
        )
        if current_changed_paths or previous_validation is None:
            reference_checkpoint = starting_checkpoint
            prior_traces: tuple[TraceObservation, ...] = ()
            prior_reports: tuple[FileObservation, ...] = ()
        else:
            reference_checkpoint = previous_validation.checkpoint
            current_traces = {item.label: item for item in checkpoint.traces}
            current_reports = {item.label: item for item in checkpoint.desktop_reports}
            prior_traces = tuple(
                item
                for item in previous_validation.candidate_traces
                if current_traces.get(item.label) == item
            )
            prior_reports = tuple(
                item
                for item in previous_validation.candidate_desktop_reports
                if current_reports.get(item.label) == item
            )
        delta = checkpoint.delta_from(reference_checkpoint)
        candidate_traces = self._merge_trace_observations(
            prior_traces,
            (
                *delta.new_traces,
                *(item.current for item in delta.changed_traces),
            ),
        )
        candidate_reports = self._merge_file_observations(
            prior_reports,
            (
                *delta.new_desktop_reports,
                *(item.current for item in delta.changed_desktop_reports),
            ),
        )
        post_analysis = None
        if result.trace_validation_required and candidate_traces:
            post_analysis = await self.halo_engine.analyze(
                config=config,
                workspace=workspace,
                run_id=f"{run_id}-candidate",
                issue_title=issue.title,
                issue_body=issue.body,
                trace_sha256s=frozenset(trace.sha256 for trace in candidate_traces),
            )
            if post_analysis is not None and post_analysis.final_message:
                result = result.model_copy(
                    update={
                        "analysis_sha256": hashlib.sha256(
                            post_analysis.final_message.encode()
                        ).hexdigest()
                    }
                )
        candidate_revision = (
            head.entry.state.branch.head_revision
            if not current_changed_paths and head.entry.state.branch is not None
            else None
        )
        trace_validation = self._trace_validation(
            checkpoint=checkpoint,
            candidate_traces=candidate_traces,
            candidate_reports=candidate_reports,
            halo_analysis=post_analysis,
            candidate_revision=candidate_revision,
        )
        publication_result = result.model_copy(update={"trace_validation": trace_validation})
        result = self._gate_outcome(publication_result)

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
                workspace_branch=workspace_branch,
                base_revision=workspace.base_revision,
                branch=snapshot,
                publication_intent=intent,
                input_fingerprint=input_fingerprint,
                result=publication_result,
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
            snapshot = manager.publish_intent(
                workspace,
                intent,
                issue_number=issue.number,
                title=issue.title,
                expected_snapshot=head.entry.state.branch,
            )
            publication_result = publication_result.model_copy(
                update={
                    "trace_validation": self._trace_validation(
                        checkpoint=checkpoint,
                        candidate_traces=candidate_traces,
                        candidate_reports=candidate_reports,
                        halo_analysis=post_analysis,
                        candidate_revision=snapshot.head_revision,
                    )
                }
            )
            result = self._gate_outcome(publication_result)
        if result.outcome == Outcome.READY_FOR_TODO and result.changed_paths and snapshot is None:
            result = result.model_copy(
                update={
                    "outcome": Outcome.CONTINUE_RESEARCH,
                    "residual_risks": [
                        *result.residual_risks,
                        "A code-ready handoff requires a published experimental snapshot.",
                    ],
                }
            )

        phase, target_status, summary = self._route(config, result)
        final_state = HaloState(
            issue_repository=issue.repository,
            issue_number=issue.number,
            run_id=run_id,
            phase=phase,
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
        self._append(github, issue, head, producer, final_state, summary)
        if target_status is not None:
            github.assert_issue_remains_in_research(issue)
            github.set_status(metadata, issue.item_id, target_status)
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

    @staticmethod
    def _append(
        github: GitHubClient,
        issue: ProjectIssue,
        head: WorkpadHead | None,
        producer: str,
        state: HaloState,
        summary: str,
    ) -> WorkpadHead:
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
    def _gate_outcome(result: InvestigationResult) -> InvestigationResult:
        """Demote unsupported terminal claims to continued research."""

        missing: list[str] = []
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
            if not result.experiments or any(
                not item.hypothesis.strip() or not item.action.strip() or not item.result.strip()
                for item in result.experiments
            ):
                missing.append("A Todo handoff requires completed experiments.")
            if not result.todo_handoff or not result.todo_handoff.strip():
                missing.append("A Todo handoff requires implementation guidance.")
            if not result.executed_actions or not any(
                action.kind == "experiment" and action.exit_code == 0
                for action in result.executed_actions
            ):
                missing.append("A Todo handoff requires a successful runtime-recorded experiment.")
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
                    "A Todo handoff requires a new candidate trace hierarchy, a new HALO "
                    "Desktop report, post-experiment HALO Engine analysis, and the published "
                    "candidate revision."
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
            if not result.experiments or any(
                not item.hypothesis.strip() or not item.action.strip() or not item.result.strip()
                for item in result.experiments
            ):
                missing.append("A no-change conclusion requires completed experiments.")
            if result.changed_paths:
                missing.append("A no-change conclusion conflicts with experiment file changes.")
            if not result.executed_actions or not any(
                action.kind == "experiment" and action.exit_code == 0
                for action in result.executed_actions
            ):
                missing.append(
                    "A no-change conclusion requires a successful runtime-recorded experiment."
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
    def _merge_trace_observations(
        previous: tuple[TraceObservation, ...],
        current: tuple[TraceObservation, ...],
    ) -> tuple[TraceObservation, ...]:
        merged = {item.label: item for item in previous}
        merged.update({item.label: item for item in current})
        return tuple(merged[label] for label in sorted(merged))

    @staticmethod
    def _merge_file_observations(
        previous: tuple[FileObservation, ...],
        current: tuple[FileObservation, ...],
    ) -> tuple[FileObservation, ...]:
        merged = {item.label: item for item in previous}
        merged.update({item.label: item for item in current})
        return tuple(merged[label] for label in sorted(merged))

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
        if halo_analysis is not None:
            final_message = getattr(halo_analysis, "final_message", None)
            dataset = getattr(halo_analysis, "dataset", None)
            files = getattr(dataset, "files", ()) if dataset is not None else ()
            if isinstance(final_message, str) and final_message:
                report_digest = hashlib.sha256(final_message.encode()).hexdigest()
            dataset_digests = {
                digest
                for item in files
                if isinstance((digest := getattr(item, "sha256", None)), str)
            }
        candidate_digests = {item.sha256 for item in candidate_traces}
        complete = bool(
            candidate_traces
            and candidate_reports
            and report_digest
            and candidate_digests == dataset_digests
            and candidate_revision
            and any(item.parented_span_count > 0 for item in candidate_traces)
        )
        return TraceValidation(
            checkpoint=checkpoint,
            candidate_traces=candidate_traces,
            candidate_desktop_reports=candidate_reports,
            halo_report_sha256=report_digest,
            halo_dataset_verified=bool(candidate_digests and candidate_digests == dataset_digests),
            candidate_revision=candidate_revision,
            complete=complete,
        )

    @staticmethod
    def _with_candidate_revision(
        validation: TraceValidation | None,
        revision: str,
    ) -> TraceValidation | None:
        if validation is None:
            return None
        complete = bool(
            validation.candidate_traces
            and validation.candidate_desktop_reports
            and validation.halo_report_sha256
            and validation.halo_dataset_verified
            and any(trace.parented_span_count > 0 for trace in validation.candidate_traces)
        )
        return validation.model_copy(
            update={
                "candidate_revision": revision,
                "complete": complete,
            }
        )

    @staticmethod
    def _discussion_context(comments: list[IssueComment]) -> str:
        discussion: list[dict[str, object]] = []
        for comment in comments:
            if comment.body.lstrip().startswith(MARKER):
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
        payload = {
            "daily_refresh": datetime.now(UTC).date().isoformat(),
            "issue": {
                "title": issue.title,
                "body": issue.body,
                "discussion": discussion,
            },
            "observation": checkpoint.model_dump(mode="json"),
            "branch": branch_payload,
            "configuration": {
                "repository": config.repository,
                "base_branch": config.base_branch,
                "trace_globs": config.observation.trace_globs,
                "desktop_report_globs": config.observation.desktop_report_globs,
                "experiment_commands": config.experiment_commands,
                "verification_commands": config.verification_commands,
                "model": config.model,
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
                "The hypothesis was resolved without a code change; the research item is done.",
            )
        if result.outcome == Outcome.BLOCKED:
            return (
                "blocked",
                config.tracker.blocked_state,
                "A genuine external dependency requires human input before research can continue.",
            )
        return (
            "experimenting",
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
    service = HaloService(targets_from_environment())
    poll_seconds = int(os.getenv("SHEA_HALO_POLL_SECONDS", "60"))
    if poll_seconds < 10:
        raise ServiceError("SHEA_HALO_POLL_SECONDS must be at least 10")
    while True:
        await service.run_once()
        await asyncio.sleep(poll_seconds)
