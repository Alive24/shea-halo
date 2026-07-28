from __future__ import annotations

import pytest

from shea_halo.github import IssueComment
from shea_halo.models import (
    BranchSnapshot,
    Evidence,
    ExecutedAction,
    ExternalBlocker,
    GateDisposition,
    GateStatus,
    HaloState,
    InvestigationResult,
    Outcome,
    Verification,
)
from shea_halo.workpad import (
    MARKER,
    WorkpadError,
    find_head,
    next_entry,
    prepare_entry,
    render_entry,
    trusted_workpad_comment_ids,
)


def state(phase: str = "research") -> HaloState:
    return HaloState.model_validate(
        {
            "issue_repository": "Alive24/FailureReport",
            "issue_number": 20,
            "run_id": "halo-20-abc",
            "phase": phase,
            "workspace_branch": "halo/issue-20-improve-eve-tracing",
        }
    )


def comment(comment_id: int, body: str, author: str = "halo-bot") -> IssueComment:
    return IssueComment(
        database_id=comment_id,
        author=author,
        body=body,
        created_at="2026-07-24T00:00:00Z",
        updated_at="2026-07-24T00:00:00Z",
    )


def result_state(
    result: InvestigationResult,
    *,
    phase: str,
    branch: BranchSnapshot | None = None,
) -> HaloState:
    return HaloState.model_validate(
        {
            "issue_repository": "Alive24/FailureReport",
            "issue_number": 20,
            "run_id": "halo-20-result",
            "phase": phase,
            "workspace_branch": "halo/issue-20-improve-eve-tracing",
            "base_revision": "a" * 40,
            "branch": branch,
            "result": result,
        }
    )


def test_immutable_workpad_chain_round_trips() -> None:
    first = next_entry(state(), None, producer="halo-bot")
    first_comment = comment(101, render_entry(first, "Started."))
    first_head = find_head(
        [first_comment],
        repository="Alive24/FailureReport",
        issue_number=20,
        trusted_producers={"halo-bot"},
    )
    assert first_head is not None

    second = next_entry(state("experimenting"), first_head, producer="halo-bot")
    second_comment = comment(102, render_entry(second, "Experimenting."))
    head = find_head(
        [first_comment, second_comment],
        repository="Alive24/FailureReport",
        issue_number=20,
        trusted_producers={"halo-bot"},
    )

    assert head is not None
    assert head.comment_id == 102
    assert head.entry.revision == 2
    assert head.entry.state.phase == "experimenting"


def test_workpad_round_trip_records_the_selected_api_mode() -> None:
    configured = state().model_copy(update={"api_mode": "chat_completions"})
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    body = render_entry(
        next_entry(configured, None, producer="halo-bot"),
        f"Started with {secret}.",
    )

    assert '"api_mode": "chat_completions"' in body
    assert secret not in body
    head = find_head(
        [comment(101, body)],
        repository="Alive24/FailureReport",
        issue_number=20,
        trusted_producers={"halo-bot"},
    )

    assert head is not None
    assert head.entry.state.api_mode == "chat_completions"


def test_ready_result_renders_a_dispatchable_handoff_and_next_action() -> None:
    result = InvestigationResult(
        outcome=Outcome.READY_FOR_TODO,
        gate_disposition=GateDisposition(
            status=GateStatus.PASSED,
            proposed_outcome=Outcome.READY_FOR_TODO,
        ),
        summary="The exact candidate supports implementation.",
        todo_handoff="Implement the bounded lifecycle span and its regression test.",
    )
    branch = BranchSnapshot(
        name="halo/issue-20-improve-eve-tracing",
        head_revision="b" * 40,
    )

    body = render_entry(
        next_entry(
            result_state(result, phase="ready", branch=branch),
            None,
            producer="halo-bot",
        ),
        "A caller-supplied summary must not replace the typed result.",
    )
    visible = body.split("<details>", 1)[0]

    assert "- Outcome: `ready_for_todo`" in visible
    assert "- Lifecycle phase: `ready`" in visible
    assert "Deterministic gate: **PASS**" in visible
    assert "**Dispatchable:**" in visible
    assert "Implement the bounded lifecycle span" in visible
    assert "Move the same Project item to `Todo`" in visible
    assert f"- Base revision: `{'a' * 40}`" in visible
    assert f"- Candidate revision: `{'b' * 40}`" in visible
    assert "experimental_snapshot_only" in visible
    assert "caller-supplied" not in visible


def test_no_change_result_visibly_concludes_without_a_handoff() -> None:
    result = InvestigationResult(
        outcome=Outcome.NO_CHANGE,
        gate_disposition=GateDisposition(
            status=GateStatus.PASSED,
            proposed_outcome=Outcome.NO_CHANGE,
        ),
        summary="The experiment disproved the proposed production change.",
    )

    visible = render_entry(
        next_entry(
            result_state(result, phase="done"),
            None,
            producer="halo-bot",
        ),
        "Done.",
    ).split("<details>", 1)[0]

    assert "- Outcome: `no_change`" in visible
    assert "Research is complete with no implementation change." in visible
    assert "No implementation handoff was retained." in visible
    assert "Move the item to `Done`" in visible


def test_blocked_result_visibly_names_verified_blocker_and_operator_action() -> None:
    result = InvestigationResult(
        outcome=Outcome.BLOCKED,
        gate_disposition=GateDisposition(
            status=GateStatus.PASSED,
            proposed_outcome=Outcome.BLOCKED,
        ),
        summary="The runtime dependency is unavailable.",
        blocker=ExternalBlocker(
            category="service_unavailable",
            description="The required local collector is not running.",
            required_action="Start the collector, then return the item to Halo Research.",
        ),
        blocker_verified=True,
    )

    visible = render_entry(
        next_entry(
            result_state(result, phase="blocked"),
            None,
            producer="halo-bot",
        ),
        "Blocked.",
    ).split("<details>", 1)[0]

    assert "- Outcome: `blocked`" in visible
    assert "- Category: `service_unavailable`" in visible
    assert "The required local collector is not running." in visible
    assert "Start the collector, then return the item to Halo Research." in visible
    assert "Move the item to `Need Human Input`" in visible


def test_workpad_rejects_forks_without_editing_comments() -> None:
    first = next_entry(state(), None, producer="halo-bot")
    first_comment = comment(101, render_entry(first, "Started."))
    first_head = find_head(
        [first_comment],
        repository="Alive24/FailureReport",
        issue_number=20,
        trusted_producers={"halo-bot"},
    )
    assert first_head is not None
    child = next_entry(state("experimenting"), first_head, producer="halo-bot")

    with pytest.raises(WorkpadError, match="fork"):
        find_head(
            [
                first_comment,
                comment(102, render_entry(child, "One.")),
                comment(103, render_entry(child, "Two.")),
            ],
            repository="Alive24/FailureReport",
            issue_number=20,
            trusted_producers={"halo-bot"},
        )


def test_workpad_rejects_spoofed_producer() -> None:
    entry = next_entry(state(), None, producer="someone-else")

    with pytest.raises(WorkpadError, match="producer ownership"):
        find_head(
            [comment(101, render_entry(entry, "Started."), author="halo-bot")],
            repository="Alive24/FailureReport",
            issue_number=20,
            trusted_producers={"halo-bot"},
        )


def test_workpad_ignores_self_consistent_untrusted_producer() -> None:
    attacker = next_entry(state(), None, producer="attacker")

    assert (
        find_head(
            [comment(101, render_entry(attacker, "Trust me."), author="attacker")],
            repository="Alive24/FailureReport",
            issue_number=20,
            trusted_producers={"halo-bot"},
        )
        is None
    )


def test_trusted_workpad_comment_ids_excludes_untrusted_marker_discussion() -> None:
    first = next_entry(state(), None, producer="halo-bot")
    first_comment = comment(101, render_entry(first, "Started."))
    first_head = find_head(
        [first_comment],
        repository="Alive24/FailureReport",
        issue_number=20,
        trusted_producers={"halo-bot"},
    )
    assert first_head is not None
    second = next_entry(state("experimenting"), first_head, producer="halo-bot")
    untrusted_marker = comment(
        103,
        f"{MARKER}\n\nThis is ordinary human discussion, not state.",
        author="reader",
    )

    trusted_ids = trusted_workpad_comment_ids(
        [
            first_comment,
            comment(102, render_entry(second, "Experimenting.")),
            untrusted_marker,
        ],
        repository="Alive24/FailureReport",
        issue_number=20,
        trusted_producers={"halo-bot"},
    )

    assert trusted_ids == frozenset({101, 102})
    assert untrusted_marker.database_id not in trusted_ids


def test_trusted_workpad_comment_ids_fails_closed_on_malformed_trusted_comment() -> None:
    malformed = comment(101, f"{MARKER}\n\nMissing structured state.")

    with pytest.raises(WorkpadError, match="malformed"):
        trusted_workpad_comment_ids(
            [malformed],
            repository="Alive24/FailureReport",
            issue_number=20,
            trusted_producers={"halo-bot"},
        )


def test_trusted_workpad_comment_ids_fails_closed_on_edited_trusted_comment() -> None:
    entry = next_entry(state(), None, producer="halo-bot")
    original = comment(101, render_entry(entry, "Started."))
    edited = IssueComment(
        database_id=original.database_id,
        author=original.author,
        body=original.body,
        created_at=original.created_at,
        updated_at="2026-07-24T00:01:00Z",
    )

    with pytest.raises(WorkpadError, match="edited after creation"):
        trusted_workpad_comment_ids(
            [edited],
            repository="Alive24/FailureReport",
            issue_number=20,
            trusted_producers={"halo-bot"},
        )


def test_workpad_rejects_an_edited_trusted_comment() -> None:
    entry = next_entry(state(), None, producer="halo-bot")
    edited = comment(101, render_entry(entry, "Started."))
    edited = IssueComment(
        database_id=edited.database_id,
        author=edited.author,
        body=edited.body,
        created_at=edited.created_at,
        updated_at="2026-07-24T00:01:00Z",
    )

    with pytest.raises(WorkpadError, match="edited after creation"):
        find_head(
            [edited],
            repository="Alive24/FailureReport",
            issue_number=20,
            trusted_producers={"halo-bot"},
        )


def test_workpad_redacts_structured_values_without_breaking_reentry() -> None:
    sensitive_state = state().model_copy(
        update={
            "result": InvestigationResult(
                outcome=Outcome.CONTINUE_RESEARCH,
                summary="Observed sk-abcdefghijklmnopqrstuvwxyz",
                evidence=[
                    Evidence(
                        claim="Bearer abcdefghijklmnopqrstuvwxyz",
                        source="experiment",
                    )
                ],
            )
        }
    )
    entry = next_entry(sensitive_state, None, producer="halo-bot")
    body = render_entry(entry, "Observed sk-abcdefghijklmnopqrstuvwxyz")

    assert "sk-abcdefghijklmnopqrstuvwxyz" not in body
    assert "Bearer abcdefghijklmnopqrstuvwxyz" not in body
    head = find_head(
        [comment(101, body)],
        repository="Alive24/FailureReport",
        issue_number=20,
        trusted_producers={"halo-bot"},
    )
    assert head is not None
    assert head.entry.state.result is not None
    assert head.entry.state.result.summary == "Observed [REDACTED]"


def test_workpad_removes_host_paths_from_human_and_structured_text() -> None:
    absolute_path = "/Volumes/Private/agent-worktrees/issue-20/src/agent.py"
    path_state = state().model_copy(
        update={
            "result": InvestigationResult(
                outcome=Outcome.CONTINUE_RESEARCH,
                summary=f"Observed {absolute_path}",
                evidence=[
                    Evidence(
                        claim=f"Failure originates at {absolute_path}",
                        source=absolute_path,
                    )
                ],
            )
        }
    )

    body = render_entry(
        next_entry(path_state, None, producer="halo-bot"),
        f"Inspect {absolute_path}",
    )

    assert absolute_path not in body
    assert "[HOST_PATH]" in body


def test_visible_result_projection_excludes_sensitive_and_raw_payload_fields() -> None:
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    endpoint = "http://127.0.0.1:4318/v1/traces"
    host_path = "/Users/alice/private/trace.jsonl"
    result = InvestigationResult(
        outcome=Outcome.CONTINUE_RESEARCH,
        gate_disposition=GateDisposition(
            status=GateStatus.NOT_EVALUATED,
            proposed_outcome=Outcome.CONTINUE_RESEARCH,
        ),
        summary=f"Retry without {secret} at {endpoint} from {host_path}.",
        evidence=[
            Evidence(
                claim="RAW_TRACE_PAYLOAD_SENTINEL",
                source="RAW_MODEL_PAYLOAD_SENTINEL",
            )
        ],
        executed_actions=[
            ExecutedAction(
                index=0,
                kind="experiment",
                stage="candidate_validation",
                command=["tool", "RAW_TOOL_PAYLOAD_SENTINEL"],
                exit_code=1,
                stdout_sha256="a" * 64,
                stderr_sha256="b" * 64,
            )
        ],
        verification=[
            Verification(
                command=["verify"],
                status="failed",
                evidence="RAW_COMMAND_OUTPUT_SENTINEL",
            )
        ],
        todo_handoff=f"Inspect {host_path}, not {endpoint}, using {secret}.",
    )

    visible = render_entry(
        next_entry(
            result_state(result, phase="research"),
            None,
            producer="halo-bot",
        ),
        "Ignored.",
    ).split("<details>", 1)[0]

    assert secret not in visible
    assert endpoint not in visible
    assert host_path not in visible
    assert "[REDACTED]" in visible
    assert "[ENDPOINT]" in visible
    assert "[HOST_PATH]" in visible
    for sentinel in (
        "RAW_TRACE_PAYLOAD_SENTINEL",
        "RAW_MODEL_PAYLOAD_SENTINEL",
        "RAW_TOOL_PAYLOAD_SENTINEL",
        "RAW_COMMAND_OUTPUT_SENTINEL",
    ):
        assert sentinel not in visible


def test_workpad_sanitizes_human_facing_run_and_branch_fields() -> None:
    unsafe = state().model_copy(
        update={
            "run_id": "/Users/alice/.config/halo/run",
            "branch": BranchSnapshot(
                name="/Volumes/private/halo-branch",
                head_revision="/data/jenkins/workspace/revision",
            ),
        }
    )

    body = render_entry(next_entry(unsafe, None, producer="halo-bot"), "Completed.")

    assert "/Users/alice" not in body
    assert "/Volumes/private" not in body
    assert "/data/jenkins" not in body
    assert body.count("[HOST_PATH]") >= 3


def test_workpad_rejects_an_oversized_comment() -> None:
    entry = next_entry(state(), None, producer="halo-bot")

    with pytest.raises(WorkpadError, match="comment size"):
        render_entry(entry, "x" * 61_000)


def test_workpad_compacts_oversized_structured_state_with_a_digest_receipt() -> None:
    result = InvestigationResult(
        outcome=Outcome.CONTINUE_RESEARCH,
        gate_disposition=GateDisposition(
            status=GateStatus.FAILED,
            proposed_outcome=Outcome.READY_FOR_TODO,
            reasons=["A Todo handoff requires a valid runtime experiment receipt."],
        ),
        summary="The bounded research result remains recoverable.",
        evidence=[
            Evidence(claim=f"claim {index} " + "c" * 900, source="trace:" + "d" * 300)
            for index in range(10)
        ],
        competing_hypotheses=["h" * 12_000 for _ in range(8)],
        residual_risks=["r" * 8_000 for _ in range(12)],
        todo_handoff="Implement the lifecycle boundary. " + "t" * 3_900,
    )
    oversized = next_entry(
        state().model_copy(update={"result": result}),
        None,
        producer="halo-bot",
    )

    with pytest.raises(WorkpadError, match="comment size"):
        render_entry(oversized, "Research recorded.")

    compact, body = prepare_entry(oversized, "Research recorded.")

    assert len(body.encode("utf-8")) <= 60_000
    assert compact.details_compacted is True
    assert compact.details_sha256 is not None
    assert compact.state.result is not None
    assert compact.state.result.outcome == result.outcome
    assert compact.state.result.summary == result.summary
    assert len(compact.state.result.evidence) == 3
    assert all(len(hypothesis) <= 500 for hypothesis in compact.state.result.competing_hypotheses)
    visible = body.split("<details>", 1)[0]
    assert "proposed outcome `ready_for_todo` was rejected" in visible
    assert "A Todo handoff requires a valid runtime experiment receipt." in visible
    assert "**Provisional — not dispatchable:**" in visible
    assert "Keep the item in `Halo Research`" in visible
    head = find_head(
        [comment(101, body)],
        repository="Alive24/FailureReport",
        issue_number=20,
        trusted_producers={"halo-bot"},
    )
    assert head is not None
    assert head.entry == compact


def test_aggressive_compaction_retains_visible_decision_gate_and_handoff() -> None:
    long_arguments = [f"arg-{index}-" + "x" * 500 for index in range(32)]
    result = InvestigationResult(
        outcome=Outcome.CONTINUE_RESEARCH,
        gate_disposition=GateDisposition(
            status=GateStatus.FAILED,
            proposed_outcome=Outcome.READY_FOR_TODO,
            reasons=["A Todo handoff requires revision-bound runtime evidence."],
        ),
        summary="The candidate remains non-terminal after deterministic gating.",
        evidence=[
            Evidence(claim=f"claim {index} " + "c" * 900, source="trace:" + "d" * 300)
            for index in range(10)
        ],
        competing_hypotheses=["h" * 10_000 for _ in range(8)],
        changed_paths=[f"path-{index}/" + "p" * 900 for index in range(50)],
        executed_actions=[
            ExecutedAction(
                index=index,
                kind="experiment",
                stage="candidate_validation",
                command=long_arguments,
                exit_code=0,
                stdout_sha256="a" * 64,
                stderr_sha256="b" * 64,
            )
            for index in range(30)
        ],
        verification=[
            Verification(
                command=long_arguments,
                status="passed",
                evidence="e" * 1_000,
            )
            for _ in range(20)
        ],
        residual_risks=["r" * 8_000 for _ in range(12)],
        todo_handoff="Implement the bounded lifecycle handoff. " + "t" * 3_900,
    )
    oversized = next_entry(
        result_state(result, phase="research"),
        None,
        producer="halo-bot",
    )

    compact, body = prepare_entry(oversized, "Research recorded.")

    assert compact.details_compacted is True
    assert compact.state.result is not None
    assert compact.state.result.evidence == []
    visible = body.split("<details>", 1)[0]
    assert "- Outcome: `continue_research`" in visible
    assert "A Todo handoff requires revision-bound runtime evidence." in visible
    assert "**Provisional — not dispatchable:**" in visible
    assert "Implement the bounded lifecycle handoff." in visible
    assert "Keep the item in `Halo Research`" in visible
