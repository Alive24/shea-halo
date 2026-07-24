from __future__ import annotations

import pytest

from shea_halo.github import IssueComment
from shea_halo.models import BranchSnapshot, Evidence, HaloState, InvestigationResult, Outcome
from shea_halo.workpad import (
    MARKER,
    WorkpadError,
    find_head,
    next_entry,
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
