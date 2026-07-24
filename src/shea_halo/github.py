from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any

from shea_halo.config import TrackerConfig
from shea_halo.security import redact_text


class GitHubError(RuntimeError):
    pass


@dataclass(frozen=True)
class IssueComment:
    database_id: int
    author: str
    body: str
    created_at: str
    updated_at: str = ""


@dataclass(frozen=True)
class ProjectIssue:
    project_id: str
    item_id: str
    repository: str
    number: int
    title: str
    body: str
    url: str
    status: str
    updated_at: str = ""


@dataclass(frozen=True)
class ProjectMetadata:
    project_id: str
    status_field_id: str
    status_options: dict[str, str]


@dataclass(frozen=True)
class BranchPullRequest:
    number: int
    url: str
    state: str
    merged: bool


@dataclass(frozen=True)
class BranchPullRequestCheck:
    repository: str
    head_branch: str
    pull_requests: tuple[BranchPullRequest, ...]


_GH_ENVIRONMENT_NAMES = {
    "ALL_PROXY",
    "GH_CONFIG_DIR",
    "GH_ENTERPRISE_TOKEN",
    "GH_HOST",
    "GH_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GITHUB_TOKEN",
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "NO_PROXY",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "XDG_CONFIG_HOME",
}
_GITHUB_SEARCH_RESULT_LIMIT = 1_000


def _github_environment() -> dict[str, str]:
    environment = {
        name: value for name, value in os.environ.items() if name.upper() in _GH_ENVIRONMENT_NAMES
    }
    environment["GH_PROMPT_DISABLED"] = "1"
    return environment


class GitHubClient:
    def __init__(self, tracker: TrackerConfig) -> None:
        self.tracker = tracker

    def _run(self, args: list[str], *, input_text: str | None = None) -> str:
        try:
            process = subprocess.run(
                ["gh", *args],
                input=input_text,
                text=True,
                capture_output=True,
                env=_github_environment(),
                check=False,
            )
        except OSError as exc:
            detail = redact_text(str(exc))
            raise GitHubError(f"failed to start gh: {detail}") from None
        if process.returncode:
            detail = process.stderr.strip() or process.stdout.strip()
            raise GitHubError(f"gh {' '.join(args[:2])} failed: {redact_text(detail)[-30_000:]}")
        return process.stdout

    def _json(self, args: list[str]) -> Any:
        output = self._run(args)
        try:
            return json.loads(output)
        except json.JSONDecodeError as exc:
            raise GitHubError("GitHub returned invalid JSON") from exc

    def current_user(self) -> str:
        data = self._json(["api", "user"])
        login = data.get("login") if isinstance(data, dict) else None
        if not isinstance(login, str):
            raise GitHubError("GitHub user response did not contain login")
        return login

    def project_metadata(self) -> ProjectMetadata:
        owner_field = "user" if self.tracker.owner_type == "user" else "organization"
        query = f"""
        query($owner:String!,$number:Int!){{
          {owner_field}(login:$owner){{
            projectV2(number:$number){{
              id
              fields(first:100){{
                nodes{{
                  ... on ProjectV2SingleSelectField{{
                    id
                    name
                    options{{id name}}
                  }}
                }}
              }}
            }}
          }}
        }}
        """
        data = self._json(
            [
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-f",
                f"owner={self.tracker.owner}",
                "-F",
                f"number={self.tracker.project_number}",
            ]
        )
        project = data.get("data", {}).get(owner_field, {}).get("projectV2")
        if not isinstance(project, dict):
            raise GitHubError("configured GitHub Project was not found")
        for field in project.get("fields", {}).get("nodes", []):
            if isinstance(field, dict) and field.get("name") == self.tracker.status_field:
                options = field.get("options", [])
                option_map = {
                    option["name"]: option["id"]
                    for option in options
                    if isinstance(option, dict)
                    and isinstance(option.get("name"), str)
                    and isinstance(option.get("id"), str)
                }
                required = {
                    self.tracker.research_state,
                    self.tracker.ready_state,
                    self.tracker.done_state,
                    self.tracker.blocked_state,
                }
                missing = required - option_map.keys()
                if missing:
                    raise GitHubError(
                        f"Project status field is missing options: {', '.join(sorted(missing))}"
                    )
                return ProjectMetadata(
                    project_id=project["id"],
                    status_field_id=field["id"],
                    status_options=option_map,
                )
        raise GitHubError(f"Project has no {self.tracker.status_field} single-select field")

    def list_project_issues(self) -> list[ProjectIssue]:
        owner_field = "user" if self.tracker.owner_type == "user" else "organization"
        query = f"""
        query($owner:String!,$number:Int!,$after:String,$statusField:String!){{
          {owner_field}(login:$owner){{
            projectV2(number:$number){{
              id
              items(first:100,after:$after){{
                pageInfo{{hasNextPage endCursor}}
                nodes{{
                  id
                  fieldValueByName(name:$statusField){{
                    ... on ProjectV2ItemFieldSingleSelectValue{{name}}
                  }}
                  content{{
                    ... on Issue{{
                      number
                      title
                      body
                      url
                      updatedAt
                      repository{{nameWithOwner}}
                    }}
                  }}
                }}
              }}
            }}
          }}
        }}
        """
        issues: list[ProjectIssue] = []
        cursor: str | None = None
        while True:
            args = [
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-f",
                f"owner={self.tracker.owner}",
                "-F",
                f"number={self.tracker.project_number}",
                "-f",
                f"statusField={self.tracker.status_field}",
            ]
            if cursor is not None:
                args.extend(["-f", f"after={cursor}"])
            data = self._json(args)
            project = data.get("data", {}).get(owner_field, {}).get("projectV2")
            if not isinstance(project, dict):
                raise GitHubError("configured GitHub Project was not found")
            items = project.get("items", {})
            for node in items.get("nodes", []):
                if not isinstance(node, dict):
                    continue
                status = node.get("fieldValueByName", {})
                content = node.get("content")
                if not isinstance(status, dict) or not isinstance(content, dict):
                    continue
                status_name = status.get("name")
                if not isinstance(status_name, str) or not status_name:
                    continue
                repository = content.get("repository", {}).get("nameWithOwner")
                if not isinstance(repository, str):
                    continue
                issues.append(
                    ProjectIssue(
                        project_id=project["id"],
                        item_id=node["id"],
                        repository=repository,
                        number=content["number"],
                        title=content["title"],
                        body=content.get("body") or "",
                        url=content["url"],
                        status=status_name,
                        updated_at=content.get("updatedAt") or "",
                    )
                )
            page_info = items.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                return issues
            cursor = page_info.get("endCursor")
            if not isinstance(cursor, str):
                raise GitHubError("Project pagination cursor was missing")

    def list_research_issues(self) -> list[ProjectIssue]:
        return [
            issue
            for issue in self.list_project_issues()
            if issue.status == self.tracker.research_state
        ]

    def issue_comments(self, repository: str, number: int) -> list[IssueComment]:
        pages = self._json(
            [
                "api",
                "--paginate",
                "--slurp",
                "--method",
                "GET",
                f"repos/{repository}/issues/{number}/comments",
                "-f",
                "per_page=100",
            ]
        )
        if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
            raise GitHubError("GitHub issue comments response was not a list")
        data = [comment for page in pages for comment in page]
        return [
            IssueComment(
                database_id=comment["id"],
                author=comment.get("user", {}).get("login", ""),
                body=comment.get("body") or "",
                created_at=comment.get("created_at") or "",
                updated_at=comment.get("updated_at") or "",
            )
            for comment in data
            if isinstance(comment, dict) and isinstance(comment.get("id"), int)
        ]

    def assert_issue_remains_in_research(self, expected: ProjectIssue) -> ProjectIssue:
        current = next(
            (issue for issue in self.list_research_issues() if issue.item_id == expected.item_id),
            None,
        )
        if (
            current is None
            or current.repository.casefold() != expected.repository.casefold()
            or current.number != expected.number
        ):
            raise GitHubError("Project item left Halo Research or changed identity during the run")
        return current

    def branch_pull_requests(
        self,
        repository: str,
        head_branch: str,
    ) -> BranchPullRequestCheck:
        parts = repository.split("/")
        if len(parts) != 2 or not all(part.strip() == part and part for part in parts):
            raise GitHubError("repository must use the owner/name form")
        if not head_branch or head_branch.strip() != head_branch:
            raise GitHubError("head branch must be a non-empty Git ref")
        query = """
        query($searchQuery:String!,$after:String){
          search(type:ISSUE,query:$searchQuery,first:100,after:$after){
            issueCount
            pageInfo{hasNextPage endCursor}
            nodes{
              ... on PullRequest{
                number
                url
                state
                mergedAt
                headRefName
                headRepository{nameWithOwner}
              }
            }
          }
        }
        """
        pull_requests: list[BranchPullRequest] = []
        seen: set[tuple[int, str]] = set()
        cursor: str | None = None
        expected_issue_count: int | None = None
        raw_result_count = 0
        while True:
            args = [
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-f",
                f"searchQuery=is:pr head:{head_branch}",
            ]
            if cursor is not None:
                args.extend(["-f", f"after={cursor}"])
            data = self._json(args)
            connection = data.get("data", {}).get("search")
            if not isinstance(connection, dict):
                raise GitHubError("GitHub pull request history search was unavailable")
            issue_count = connection.get("issueCount")
            if not isinstance(issue_count, int) or isinstance(issue_count, bool) or issue_count < 0:
                raise GitHubError("GitHub pull request history count was unavailable")
            if issue_count > _GITHUB_SEARCH_RESULT_LIMIT:
                raise GitHubError("GitHub pull request history exceeds the complete search window")
            if expected_issue_count is None:
                expected_issue_count = issue_count
            elif issue_count != expected_issue_count:
                raise GitHubError("GitHub pull request history count changed during pagination")
            nodes = connection.get("nodes")
            if not isinstance(nodes, list):
                raise GitHubError("GitHub pull request response was not a list")
            raw_result_count += len(nodes)
            for raw_pull_request in nodes:
                if not isinstance(raw_pull_request, dict):
                    raise GitHubError("GitHub pull request response contained an invalid item")
                head_repository_data = raw_pull_request.get("headRepository")
                head_repository = (
                    head_repository_data.get("nameWithOwner")
                    if isinstance(head_repository_data, dict)
                    else None
                )
                head_ref = raw_pull_request.get("headRefName")
                if (
                    not isinstance(head_repository, str)
                    or head_repository.casefold() != repository.casefold()
                    or head_ref != head_branch
                ):
                    continue
                number = raw_pull_request.get("number")
                url = raw_pull_request.get("url")
                state = raw_pull_request.get("state")
                merged_at = raw_pull_request.get("mergedAt")
                if (
                    not isinstance(number, int)
                    or isinstance(number, bool)
                    or not isinstance(url, str)
                    or not url
                    or state not in {"OPEN", "CLOSED", "MERGED"}
                    or not (merged_at is None or isinstance(merged_at, str))
                ):
                    raise GitHubError("GitHub pull request response was incomplete")
                identity = (number, url)
                if identity in seen:
                    raise GitHubError("GitHub pull request response contained a duplicate")
                seen.add(identity)
                pull_requests.append(
                    BranchPullRequest(
                        number=number,
                        url=url,
                        state="open" if state == "OPEN" else "closed",
                        merged=state == "MERGED" or merged_at is not None,
                    )
                )
            page_info = connection.get("pageInfo")
            if not isinstance(page_info, dict):
                raise GitHubError("GitHub pull request pagination was missing")
            if not page_info.get("hasNextPage"):
                if raw_result_count != expected_issue_count:
                    raise GitHubError("GitHub pull request history search was incomplete")
                break
            cursor = page_info.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                raise GitHubError("GitHub pull request pagination cursor was missing")
        return BranchPullRequestCheck(
            repository=repository,
            head_branch=head_branch,
            pull_requests=tuple(pull_requests),
        )

    def assert_branch_has_no_pull_requests(
        self,
        repository: str,
        head_branch: str,
    ) -> BranchPullRequestCheck:
        check = self.branch_pull_requests(repository, head_branch)
        if check.pull_requests:
            details = ", ".join(
                f"#{pull_request.number} "
                f"({'merged' if pull_request.merged else pull_request.state})"
                for pull_request in check.pull_requests
            )
            raise GitHubError(
                redact_text(
                    f"{repository} branch {head_branch} already has pull requests: {details}"
                )
            )
        return check

    def add_comment(self, repository: str, number: int, body: str) -> IssueComment:
        data = self._json(
            [
                "api",
                "-X",
                "POST",
                f"repos/{repository}/issues/{number}/comments",
                "-f",
                f"body={body}",
            ]
        )
        return IssueComment(
            database_id=data["id"],
            author=data.get("user", {}).get("login", ""),
            body=data.get("body") or "",
            created_at=data.get("created_at") or "",
            updated_at=data.get("updated_at") or "",
        )

    def set_status(self, metadata: ProjectMetadata, item_id: str, status_name: str) -> None:
        try:
            option_id = metadata.status_options[status_name]
        except KeyError as exc:
            raise GitHubError(f"unknown Project status: {status_name}") from exc
        mutation = """
        mutation($project:ID!,$item:ID!,$field:ID!,$option:String!){
          updateProjectV2ItemFieldValue(input:{
            projectId:$project
            itemId:$item
            fieldId:$field
            value:{singleSelectOptionId:$option}
          }){projectV2Item{id}}
        }
        """
        self._json(
            [
                "api",
                "graphql",
                "-f",
                f"query={mutation}",
                "-f",
                f"project={metadata.project_id}",
                "-f",
                f"item={item_id}",
                "-f",
                f"field={metadata.status_field_id}",
                "-f",
                f"option={option_id}",
            ]
        )
