from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Collection
from dataclasses import dataclass

from shea_halo.github import IssueComment
from shea_halo.models import (
    ExecutedAction,
    ExternalBlocker,
    GateStatus,
    GuideSnapshot,
    HaloState,
    InvestigationResult,
    Outcome,
    Verification,
    WorkpadEntry,
)
from shea_halo.observation import (
    ObservationCheckpoint,
    TraceObservation,
    TraceValidation,
)
from shea_halo.security import (
    sanitize_persisted_data,
    sanitize_persisted_text,
    sanitize_visible_text,
)

MARKER = "<!-- shea-halo-workpad -->"
MAX_WORKPAD_BYTES = 60_000
_VISIBLE_SUMMARY_CHARS = 1_200
_VISIBLE_HANDOFF_CHARS = 1_600
_VISIBLE_BLOCKER_CHARS = 800
_VISIBLE_RISK_CHARS = 400
_VISIBLE_GATE_REASON_CHARS = 600
_VISIBLE_GATE_REASON_LIMIT = 8
_JSON_BLOCK = re.compile(
    r"<details>\s*<summary>Structured state</summary>\s*```json\s*(\{.*?\})\s*```"
    r"\s*</details>",
    re.DOTALL,
)


class WorkpadError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkpadHead:
    comment_id: int
    entry: WorkpadEntry


def _parse_comment(comment: IssueComment) -> WorkpadEntry | None:
    first_line = next(
        (line.strip() for line in comment.body.splitlines() if line.strip()),
        "",
    )
    if first_line != MARKER:
        return None
    match = _JSON_BLOCK.search(comment.body)
    if match is None:
        raise WorkpadError(f"malformed Halo workpad comment {comment.database_id}")
    try:
        return WorkpadEntry.model_validate_json(match.group(1))
    except ValueError as exc:
        raise WorkpadError(f"invalid Halo workpad comment {comment.database_id}") from exc


def _trusted_entries(
    comments: list[IssueComment],
    *,
    repository: str,
    issue_number: int,
    trusted_producers: Collection[str],
) -> dict[int, WorkpadEntry]:
    trusted = {producer.casefold() for producer in trusted_producers if producer.strip()}
    if not trusted:
        raise WorkpadError("Halo workpad requires at least one trusted producer")

    entries: dict[int, WorkpadEntry] = {}
    for comment in comments:
        author = comment.author.casefold()
        if author not in trusted:
            # Public Issue participants may quote or imitate the marker. Comments
            # outside the configured worker identity are ordinary discussion,
            # never state and never a reason to stop the trusted chain.
            continue
        entry = _parse_comment(comment)
        if entry is None:
            continue
        if comment.created_at and comment.updated_at and comment.created_at != comment.updated_at:
            raise WorkpadError(
                f"Halo workpad comment {comment.database_id} was edited after creation"
            )
        if entry.state.issue_repository != repository or entry.state.issue_number != issue_number:
            raise WorkpadError(f"Halo workpad comment {comment.database_id} targets another issue")
        producer = entry.producer.casefold()
        if author != producer:
            raise WorkpadError(
                f"Halo workpad comment {comment.database_id} has invalid producer ownership"
            )
        if producer not in trusted:
            raise WorkpadError(
                f"Halo workpad comment {comment.database_id} has an untrusted producer"
            )
        entries[comment.database_id] = entry
    return entries


def _validated_history(
    comments: list[IssueComment],
    *,
    repository: str,
    issue_number: int,
    trusted_producers: Collection[str],
) -> tuple[dict[int, WorkpadEntry], int | None]:
    entries = _trusted_entries(
        comments,
        repository=repository,
        issue_number=issue_number,
        trusted_producers=trusted_producers,
    )
    if not entries:
        return entries, None

    successors: dict[int, list[int]] = {}
    for comment_id, entry in entries.items():
        predecessor = entry.predecessor_comment_id
        if predecessor is None:
            continue
        if predecessor not in entries:
            raise WorkpadError(f"Halo workpad comment {comment_id} has a missing predecessor")
        successors.setdefault(predecessor, []).append(comment_id)

    forks = [comment_id for comment_id, children in successors.items() if len(children) > 1]
    if forks:
        raise WorkpadError(f"Halo workpad fork detected after comment {forks[0]}")

    roots = [
        comment_id for comment_id, entry in entries.items() if entry.predecessor_comment_id is None
    ]
    heads = [comment_id for comment_id in entries if comment_id not in successors]
    if len(roots) != 1 or len(heads) != 1:
        raise WorkpadError("Halo workpad must contain one complete linear history")

    cursor = heads[0]
    visited: set[int] = set()
    while cursor in entries:
        if cursor in visited:
            raise WorkpadError("Halo workpad cycle detected")
        visited.add(cursor)
        predecessor = entries[cursor].predecessor_comment_id
        if predecessor is None:
            break
        if entries[cursor].revision != entries[predecessor].revision + 1:
            raise WorkpadError("Halo workpad revisions are not consecutive")
        cursor = predecessor
    if len(visited) != len(entries):
        raise WorkpadError("Halo workpad contains disconnected histories")

    head_id = heads[0]
    head = entries[head_id]
    if head.revision != len(entries):
        raise WorkpadError("Halo workpad revision does not match its immutable history")
    return entries, head_id


def trusted_workpad_comment_ids(
    comments: list[IssueComment],
    *,
    repository: str,
    issue_number: int,
    trusted_producers: Collection[str],
) -> frozenset[int]:
    """Return IDs belonging to the one validated trusted workpad history.

    Marker-like comments from other authors are deliberately omitted rather
    than parsed, so callers can retain them as ordinary Issue discussion.
    """

    entries, _ = _validated_history(
        comments,
        repository=repository,
        issue_number=issue_number,
        trusted_producers=trusted_producers,
    )
    return frozenset(entries)


def find_head(
    comments: list[IssueComment],
    *,
    repository: str,
    issue_number: int,
    trusted_producers: Collection[str],
) -> WorkpadHead | None:
    entries, head_id = _validated_history(
        comments,
        repository=repository,
        issue_number=issue_number,
        trusted_producers=trusted_producers,
    )
    if head_id is None:
        return None
    head = entries[head_id]
    return WorkpadHead(comment_id=head_id, entry=head)


def next_entry(state: HaloState, head: WorkpadHead | None, *, producer: str) -> WorkpadEntry:
    return WorkpadEntry(
        revision=1 if head is None else head.entry.revision + 1,
        predecessor_comment_id=None if head is None else head.comment_id,
        producer=producer,
        state=state,
    )


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


def _visible_text(value: str, limit: int) -> str:
    """Render model-authored text as bounded inert prose, not Markdown structure."""

    sanitized = sanitize_visible_text(value)
    collapsed = " ".join(sanitized.split())
    clipped = collapsed if len(collapsed) <= limit else f"{collapsed[: limit - 1]}…"
    return html.escape(clipped, quote=False)


def _count_status(items: Collection[bool]) -> str:
    if not items:
        return "not recorded"
    return f"{sum(items)}/{len(items)} passed"


def _identity_lines(entry: WorkpadEntry) -> list[str]:
    state = entry.state
    result = state.result
    branch = state.branch
    candidate_revision = None
    if result is not None and result.trace_validation is not None:
        candidate_revision = result.trace_validation.candidate_revision
    if result is not None and candidate_revision is None:
        candidate_revision = branch.head_revision if branch is not None else state.base_revision

    lines = [
        f"- Run: `{sanitize_persisted_text(state.run_id)}`",
        f"- Workpad revision: `{entry.revision}`",
        (
            f"- Base revision: `{sanitize_persisted_text(state.base_revision)}`"
            if state.base_revision is not None
            else "- Base revision: not recorded"
        ),
    ]
    if candidate_revision is not None:
        lines.append(f"- Candidate revision: `{sanitize_persisted_text(candidate_revision)}`")
    if branch is None:
        lines.append("- Experimental snapshot: not published")
    else:
        lines.append(
            "- Experimental snapshot: "
            f"`{sanitize_persisted_text(branch.name)}` at "
            f"`{sanitize_persisted_text(branch.head_revision)}` "
            "(`experimental_snapshot_only`; do not open a PR from it or continue work on it)"
        )
    if entry.details_compacted and entry.details_sha256 is not None:
        lines.append(
            f"- Compacted-detail receipt: `sha256:{sanitize_persisted_text(entry.details_sha256)}`"
        )
    return lines


def _gate_lines(result: InvestigationResult) -> list[str]:
    disposition = result.gate_disposition
    if disposition is None:
        lines = ["- Deterministic gate: **NOT EVALUATED** — no gate receipt was recorded."]
    elif disposition.status == GateStatus.PASSED:
        lines = [
            "- Deterministic gate: **PASS** for proposed outcome "
            f"`{disposition.proposed_outcome.value}`."
        ]
    elif disposition.status == GateStatus.NOT_EVALUATED:
        lines = ["- Deterministic gate: **NOT EVALUATED** — the result was already non-terminal."]
    else:
        lines = [
            "- Deterministic gate: **FAIL** — proposed outcome "
            f"`{disposition.proposed_outcome.value}` was rejected.",
            f"- Missing requirements: `{len(disposition.reasons)}`",
        ]
        lines.extend(
            f"  - {_visible_text(reason, _VISIBLE_GATE_REASON_CHARS)}"
            for reason in disposition.reasons[:_VISIBLE_GATE_REASON_LIMIT]
        )
        omitted = len(disposition.reasons) - _VISIBLE_GATE_REASON_LIMIT
        if omitted > 0:
            lines.append(f"  - {omitted} additional gate reason(s) remain in structured state.")

    setup = [action.exit_code == 0 for action in result.executed_actions if action.stage == "setup"]
    runtime = [
        action.exit_code == 0
        for action in result.executed_actions
        if action.kind == "experiment" and action.stage == "candidate_validation"
    ]
    verification = [check.status == "passed" for check in result.verification]
    lines.extend(
        (
            f"- HALO analysis: {'present' if result.analysis_sha256 else 'missing'}",
            f"- Setup actions: {_count_status(setup)}",
            f"- Revision-bound runtime experiments: {_count_status(runtime)}",
            f"- Deterministic verification: {_count_status(verification)}",
        )
    )
    if not result.trace_validation_required:
        lines.append("- Trace validation: not required")
    elif result.trace_validation is None:
        lines.append("- Trace validation: **missing**")
    else:
        validation = result.trace_validation
        hierarchy_verified = bool(
            validation.candidate_traces
            and any(trace.parented_span_count > 0 for trace in validation.candidate_traces)
        )
        lines.append(
            "- Trace validation: "
            f"{'**PASS**' if validation.complete else '**FAIL**'} "
            f"(dataset={'pass' if validation.halo_dataset_verified else 'fail'}, "
            f"revision={'pass' if validation.halo_revision_verified else 'fail'}, "
            f"`service.version`={'pass' if validation.service_version_verified else 'fail'}, "
            f"hierarchy={'pass' if hierarchy_verified else 'fail'})"
        )
    return lines


def _result_sections(result: InvestigationResult, *, phase: str) -> str:
    outcome_text = {
        Outcome.READY_FOR_TODO: "Implementation handoff is dispatchable.",
        Outcome.CONTINUE_RESEARCH: "Research remains non-terminal.",
        Outcome.NO_CHANGE: "Research is complete with no implementation change.",
        Outcome.BLOCKED: "Research is blocked by a verified external dependency.",
    }[result.outcome]
    gate_failed = (
        result.gate_disposition is not None and result.gate_disposition.status == GateStatus.FAILED
    )
    visible_summary = _visible_text(result.summary, _VISIBLE_SUMMARY_CHARS)
    summary_line = (
        "**Model-authored context only — rejected terminal wording is non-authoritative.** "
        f"{visible_summary}"
        if gate_failed
        else f"**Result summary:** {visible_summary}"
    )

    if result.todo_handoff is None or not result.todo_handoff.strip():
        handoff = "No implementation handoff was retained."
    elif result.outcome == Outcome.READY_FOR_TODO:
        handoff = (
            "**Dispatchable:** this handoff may be implemented by Shea Symphony.\n\n"
            f"{_visible_text(result.todo_handoff, _VISIBLE_HANDOFF_CHARS)}"
        )
    else:
        handoff = (
            "**Provisional — not dispatchable:** the authoritative outcome is not "
            "`ready_for_todo`.\n\n"
            f"{_visible_text(result.todo_handoff, _VISIBLE_HANDOFF_CHARS)}"
        )

    blocker = "No verified external blocker."
    if result.outcome == Outcome.BLOCKED and result.blocker_verified and result.blocker is not None:
        blocker = (
            f"- Category: `{result.blocker.category}`\n"
            "- Verified blocker: "
            f"{_visible_text(result.blocker.description, _VISIBLE_BLOCKER_CHARS)}\n"
            "- Required operator action: "
            f"{_visible_text(result.blocker.required_action, _VISIBLE_BLOCKER_CHARS)}"
        )

    if result.residual_risks:
        risks = "\n".join(
            f"- {_visible_text(risk, _VISIBLE_RISK_CHARS)}" for risk in result.residual_risks[:4]
        )
        if len(result.residual_risks) > 4:
            risks += (
                f"\n- {len(result.residual_risks) - 4} additional model-authored risk(s) "
                "remain in structured state."
            )
    else:
        risks = "No model-authored residual risks were recorded."

    next_action = {
        Outcome.READY_FOR_TODO: (
            "Move the same Project item to `Todo`. Shea Symphony may implement the "
            "dispatchable handoff in a separate workspace and branch."
        ),
        Outcome.CONTINUE_RESEARCH: (
            "Keep the item in `Halo Research`; satisfy the failed or missing evidence "
            "requirements before attempting another terminal decision."
        ),
        Outcome.NO_CHANGE: (
            "Move the item to `Done`; no implementation should be dispatched from this result."
        ),
        Outcome.BLOCKED: (
            "Move the item to `Need Human Input`. Complete the required operator action, "
            "then return it to `Halo Research`."
        ),
    }[result.outcome]

    return f"""\
### Authoritative decision

- Outcome: `{result.outcome.value}`
- Lifecycle phase: `{phase}`
- Disposition: {outcome_text}

{summary_line}

### Evidence gates

{chr(10).join(_gate_lines(result))}

### Implementation handoff

{handoff}

### External blocker

{blocker}

### Model-authored residual risks

{risks}

### Next lifecycle action

{next_action}
"""


def _compact_trace(trace: TraceObservation) -> TraceObservation:
    return TraceObservation.model_validate(
        trace.model_copy(
            update={
                "semantic_receipt": None,
                "semantic_receipt_sha256": None,
            }
        ).model_dump()
    )


def _compact_checkpoint(
    checkpoint: ObservationCheckpoint | None,
    *,
    limit: int,
) -> ObservationCheckpoint | None:
    if checkpoint is None:
        return None
    return ObservationCheckpoint(
        traces=tuple(_compact_trace(trace) for trace in checkpoint.traces[:limit]),
        desktop_reports=checkpoint.desktop_reports[:limit],
    )


def _compact_trace_validation(
    validation: TraceValidation | None,
    *,
    limit: int,
) -> TraceValidation | None:
    if validation is None:
        return None
    checkpoint = _compact_checkpoint(validation.checkpoint, limit=limit)
    if checkpoint is None:
        raise WorkpadError("trace validation lost its required checkpoint")
    return TraceValidation.model_validate(
        validation.model_copy(
            update={
                "checkpoint": checkpoint,
                "candidate_traces": tuple(
                    _compact_trace(trace) for trace in validation.candidate_traces[:limit]
                ),
                "candidate_desktop_reports": validation.candidate_desktop_reports[:limit],
            }
        ).model_dump()
    )


def _compact_action(action: ExecutedAction, *, command_limit: int) -> ExecutedAction:
    return ExecutedAction.model_validate(
        action.model_copy(
            update={
                "command": [_clip(argument, 120) for argument in action.command[:command_limit]]
            }
        ).model_dump()
    )


def _compact_verification(
    verification: Verification,
    *,
    command_limit: int,
    evidence_limit: int,
) -> Verification:
    return Verification.model_validate(
        verification.model_copy(
            update={
                "command": [
                    _clip(argument, 120) for argument in verification.command[:command_limit]
                ],
                "evidence": _clip(verification.evidence, evidence_limit),
            }
        ).model_dump()
    )


def _compact_guide(guide: GuideSnapshot) -> GuideSnapshot:
    return GuideSnapshot.model_validate(
        guide.model_copy(
            update={
                "framework_evidence": [_clip(item, 200) for item in guide.framework_evidence[:5]],
                "selected_because": _clip(guide.selected_because, 500),
            }
        ).model_dump()
    )


def _compact_blocker(blocker: ExternalBlocker | None) -> ExternalBlocker | None:
    if blocker is None:
        return None
    return ExternalBlocker.model_validate(
        blocker.model_copy(
            update={
                "description": _clip(blocker.description, 700),
                "required_action": _clip(blocker.required_action, 700),
            }
        ).model_dump()
    )


def _compact_result(
    result: InvestigationResult | None,
    *,
    aggressive: bool,
) -> InvestigationResult | None:
    if result is None:
        return None
    observation_limit = 2 if aggressive else 5
    action_limit = 10 if aggressive else 20
    verification_limit = 5 if aggressive else 10
    return InvestigationResult.model_validate(
        result.model_copy(
            update={
                "evidence": [] if aggressive else result.evidence[:3],
                "competing_hypotheses": [
                    _clip(item, 500)
                    for item in result.competing_hypotheses[: 1 if aggressive else 3]
                ],
                "experiments": [] if aggressive else result.experiments[:3],
                "selected_guides": [
                    _compact_guide(guide)
                    for guide in result.selected_guides[: 1 if aggressive else 3]
                ],
                "changed_paths": [
                    _clip(path, 300) for path in result.changed_paths[: 10 if aggressive else 20]
                ],
                "trace_validation": _compact_trace_validation(
                    result.trace_validation,
                    limit=observation_limit,
                ),
                "executed_actions": [
                    _compact_action(action, command_limit=8 if aggressive else 12)
                    for action in result.executed_actions[-action_limit:]
                ],
                "verification": [
                    _compact_verification(
                        verification,
                        command_limit=8 if aggressive else 12,
                        evidence_limit=300 if aggressive else 500,
                    )
                    for verification in result.verification[-verification_limit:]
                ],
                "residual_risks": [
                    _clip(risk, 300 if aggressive else 700)
                    for risk in result.residual_risks[: 4 if aggressive else 8]
                ],
                "todo_handoff": (
                    None
                    if result.todo_handoff is None
                    else _clip(result.todo_handoff, 1_000 if aggressive else 2_000)
                ),
                "blocker": _compact_blocker(result.blocker),
            }
        ).model_dump()
    )


def _compact_entry(entry: WorkpadEntry, *, aggressive: bool) -> WorkpadEntry:
    safe_state = sanitize_persisted_data(entry.state.model_dump(mode="json"))
    details_sha256 = hashlib.sha256(
        json.dumps(
            safe_state,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    state = HaloState.model_validate(
        entry.state.model_copy(
            update={
                "result": _compact_result(entry.state.result, aggressive=aggressive),
                "validation_baseline": _compact_checkpoint(
                    entry.state.validation_baseline,
                    limit=2 if aggressive else 5,
                ),
            }
        ).model_dump()
    )
    return WorkpadEntry(
        revision=entry.revision,
        predecessor_comment_id=entry.predecessor_comment_id,
        producer=entry.producer,
        state=state,
        details_compacted=True,
        details_sha256=details_sha256,
    )


def prepare_entry(entry: WorkpadEntry, summary: str) -> tuple[WorkpadEntry, str]:
    """Render a recoverable bounded entry while retaining a full-state receipt."""

    try:
        return entry, render_entry(entry, summary)
    except WorkpadError:
        compact = _compact_entry(entry, aggressive=False)
    try:
        return compact, render_entry(compact, summary)
    except WorkpadError:
        compact = _compact_entry(entry, aggressive=True)
        return compact, render_entry(compact, summary)


def render_entry(entry: WorkpadEntry, summary: str) -> str:
    if len(summary.encode("utf-8")) > MAX_WORKPAD_BYTES:
        raise WorkpadError("Halo workpad entry exceeds the safe GitHub comment size")
    payload = json.dumps(
        sanitize_persisted_data(entry.model_dump(mode="json")),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    result_sections = (
        _result_sections(entry.state.result, phase=entry.state.phase)
        if entry.state.result is not None
        else (
            "### Lifecycle checkpoint\n\n"
            f"{_visible_text(summary, _VISIBLE_SUMMARY_CHARS)}\n\n"
            "### Next lifecycle action\n\n"
            "Continue the current Halo lifecycle phase; no material result has been recorded.\n"
        )
    )
    rendered = f"""\
{MARKER}
## Shea Halo — {entry.state.phase}

{result_sections}
### Lifecycle identity

{chr(10).join(_identity_lines(entry))}

<details>
<summary>Structured state</summary>

```json
{payload}
```

</details>
"""
    rendered = sanitize_persisted_text(rendered)
    if len(rendered.encode("utf-8")) > MAX_WORKPAD_BYTES:
        raise WorkpadError("Halo workpad entry exceeds the safe GitHub comment size")
    return rendered
