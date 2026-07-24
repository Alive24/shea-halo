from __future__ import annotations

import subprocess
from typing import Any

import pytest

from shea_halo.config import TrackerConfig
from shea_halo.github import GitHubClient, GitHubError, ProjectIssue


def _client() -> GitHubClient:
    return GitHubClient(
        TrackerConfig(
            owner="Alive24",
            owner_type="user",
            project_number=10,
            status_field="Status",
            research_state="Halo Research",
            ready_state="Todo",
            done_state="Done",
            blocked_state="Need Human Input",
            trusted_producers=(),
        )
    )


def test_gh_process_receives_minimal_environment_and_redacts_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-live-secret-value"
    captured_environment: dict[str, str] = {}
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/Users/tester")
    monkeypatch.setenv("GH_TOKEN", "ghp_githubtoken123")
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("UNRELATED_HOST_VALUE", "do-not-pass")

    def fake_run(*_args: object, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_environment.update(kwargs["env"])
        return subprocess.CompletedProcess(
            args=["gh", "api", "user"],
            returncode=1,
            stdout="",
            stderr=f"provider rejected token {secret}",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(GitHubError) as error:
        _client().current_user()

    assert secret not in str(error.value)
    assert "[REDACTED]" in str(error.value)
    assert captured_environment["PATH"] == "/usr/bin"
    assert captured_environment["HOME"] == "/Users/tester"
    assert captured_environment["GH_TOKEN"] == "ghp_githubtoken123"
    assert captured_environment["GH_PROMPT_DISABLED"] == "1"
    assert "OPENAI_API_KEY" not in captured_environment
    assert "UNRELATED_HOST_VALUE" not in captured_environment


def test_gh_start_failure_is_a_redacted_github_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-start-failure-secret"
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    def fail_to_start(*_args: object, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise OSError(f"cannot start helper with {secret}")

    monkeypatch.setattr(subprocess, "run", fail_to_start)

    with pytest.raises(GitHubError) as error:
        _client().current_user()

    assert secret not in str(error.value)
    assert "[REDACTED]" in str(error.value)
    assert error.value.__cause__ is None


def test_issue_research_lease_accepts_only_the_same_current_project_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    issue = ProjectIssue(
        project_id="project",
        item_id="item",
        repository="Alive24/FailureReport",
        number=26,
        title="Improve tracing",
        body="",
        url="https://github.com/Alive24/FailureReport/issues/26",
        status="Halo Research",
    )
    monkeypatch.setattr(client, "list_research_issues", lambda: [issue])

    assert client.assert_issue_remains_in_research(issue) == issue

    monkeypatch.setattr(client, "list_research_issues", lambda: [])
    with pytest.raises(GitHubError, match="left Halo Research"):
        client.assert_issue_remains_in_research(issue)


def test_branch_pull_requests_returns_all_same_repository_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    captured_calls: list[list[str]] = []

    def fake_json(args: list[str]) -> object:
        captured_calls.append(args)
        after = next(
            (value.removeprefix("after=") for value in args if value.startswith("after=")),
            None,
        )
        nodes = (
            [
                {
                    "number": 41,
                    "url": "https://github.com/upstream/project/pull/41",
                    "state": "OPEN",
                    "mergedAt": None,
                    "headRefName": "halo/issue-26-tracing",
                    "headRepository": {"nameWithOwner": "Alive24/FailureReport"},
                }
            ]
            if after is None
            else [
                {
                    "number": 39,
                    "url": "https://github.com/upstream/project/pull/39",
                    "state": "MERGED",
                    "mergedAt": "2026-07-24T10:00:00Z",
                    "headRefName": "halo/issue-26-tracing",
                    "headRepository": {"nameWithOwner": "Alive24/FailureReport"},
                }
            ]
        )
        return {
            "data": {
                "repository": {
                    "ref": {
                        "name": "halo/issue-26-tracing",
                        "associatedPullRequests": {
                            "nodes": nodes,
                            "pageInfo": {
                                "hasNextPage": after is None,
                                "endCursor": "cursor-2" if after is None else None,
                            },
                        },
                    }
                }
            }
        }

    monkeypatch.setattr(client, "_json", fake_json)

    check = client.branch_pull_requests(
        "Alive24/FailureReport",
        "halo/issue-26-tracing",
    )

    assert check.repository == "Alive24/FailureReport"
    assert check.head_branch == "halo/issue-26-tracing"
    assert [(item.number, item.state, item.merged) for item in check.pull_requests] == [
        (41, "open", False),
        (39, "closed", True),
    ]
    assert len(captured_calls) == 2
    assert "qualifiedName=refs/heads/halo/issue-26-tracing" in captured_calls[0]
    assert "after=cursor-2" in captured_calls[1]


def test_assert_branch_has_no_pull_requests_returns_empty_structured_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    monkeypatch.setattr(
        client,
        "_json",
        lambda _args: {
            "data": {
                "repository": {
                    "ref": {
                        "name": "halo/issue-26-tracing",
                        "associatedPullRequests": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    }
                }
            }
        },
    )

    check = client.assert_branch_has_no_pull_requests(
        "Alive24/FailureReport",
        "halo/issue-26-tracing",
    )

    assert check.pull_requests == ()


def test_assert_branch_has_no_pull_requests_rejects_closed_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    monkeypatch.setattr(
        client,
        "_json",
        lambda _args: {
            "data": {
                "repository": {
                    "ref": {
                        "name": "halo/issue-26-tracing",
                        "associatedPullRequests": {
                            "nodes": [
                                {
                                    "number": 39,
                                    "url": "https://github.com/upstream/project/pull/39",
                                    "state": "CLOSED",
                                    "mergedAt": None,
                                    "headRefName": "halo/issue-26-tracing",
                                    "headRepository": {"nameWithOwner": "Alive24/FailureReport"},
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    }
                }
            }
        },
    )

    with pytest.raises(GitHubError, match=r"#39 \(closed\)"):
        client.assert_branch_has_no_pull_requests(
            "Alive24/FailureReport",
            "halo/issue-26-tracing",
        )


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"data": {"repository": {"ref": {"name": "halo/issue-26-tracing"}}}},
        {
            "data": {
                "repository": {
                    "ref": {
                        "name": "halo/issue-26-tracing",
                        "associatedPullRequests": {
                            "nodes": [
                                {
                                    "number": 1,
                                    "url": "https://github.com/upstream/project/pull/1",
                                    "state": "OPEN",
                                    "mergedAt": None,
                                    "headRefName": "halo/issue-26-tracing",
                                    "headRepository": {"nameWithOwner": "attacker/fork"},
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    },
                }
            }
        },
    ],
)
def test_branch_pull_request_lookup_fails_closed_on_untrusted_response(
    monkeypatch: pytest.MonkeyPatch,
    response: object,
) -> None:
    client = _client()
    monkeypatch.setattr(client, "_json", lambda _args: response)

    with pytest.raises(GitHubError):
        client.branch_pull_requests(
            "Alive24/FailureReport",
            "halo/issue-26-tracing",
        )
