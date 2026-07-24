from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import dataclass

from shea_halo.github import IssueComment
from shea_halo.models import HaloState, WorkpadEntry
from shea_halo.security import sanitize_persisted_data, sanitize_persisted_text

MARKER = "<!-- shea-halo-workpad -->"
MAX_WORKPAD_BYTES = 60_000
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


def find_head(
    comments: list[IssueComment],
    *,
    repository: str,
    issue_number: int,
    trusted_producers: Collection[str],
) -> WorkpadHead | None:
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

    if not entries:
        return None

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
    return WorkpadHead(comment_id=head_id, entry=head)


def next_entry(state: HaloState, head: WorkpadHead | None, *, producer: str) -> WorkpadEntry:
    return WorkpadEntry(
        revision=1 if head is None else head.entry.revision + 1,
        predecessor_comment_id=None if head is None else head.comment_id,
        producer=producer,
        state=state,
    )


def render_entry(entry: WorkpadEntry, summary: str) -> str:
    if len(summary.encode("utf-8")) > MAX_WORKPAD_BYTES:
        raise WorkpadError("Halo workpad entry exceeds the safe GitHub comment size")
    payload = json.dumps(
        sanitize_persisted_data(entry.model_dump(mode="json")),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    branch = entry.state.branch
    branch_text = (
        "No experimental branch has been published."
        if branch is None
        else (
            f"`{sanitize_persisted_text(branch.name)}` at "
            f"`{sanitize_persisted_text(branch.head_revision)}` is an experimental snapshot only. "
            "Do not open a PR from it or continue development on it."
        )
    )
    rendered = f"""\
{MARKER}
## Shea Halo — {entry.state.phase}

{sanitize_persisted_text(summary)}

- Run: `{sanitize_persisted_text(entry.state.run_id)}`
- Workpad revision: `{entry.revision}`
- Branch: {branch_text}

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
