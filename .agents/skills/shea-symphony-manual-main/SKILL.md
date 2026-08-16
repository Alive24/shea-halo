---
name: shea-symphony-manual-main
description: Execute one operator-selected Shea Halo issue in the current task through the Shea Symphony Main lane. Use for Todo implementation, explicitly authorized Backlog pickup, Main-lane Rework, or consistent resumable In Progress work that must reach a verified ready PR and Agent Review. Do not use merely to write a prompt, launch another agent task, review code, approve UAT, or merge.
---

# Shea Halo Manual Main

Implement one named Shea Halo issue in this task. Inspect the real tracker,
claim only after its gates pass, work in one isolated issue worktree, verify the
Python package, publish one ready PR, record durable evidence, and stop at
`Agent Review`.

Do the Main work directly. Do not substitute a configured backend, another
task, or a handoff prompt for execution.

## Authority

Own only:

- `Todo` implementation;
- Main-lane `Rework`;
- a consistent resumable `In Progress` issue owned by this worker;
- an operator-named Backlog issue explicitly authorized for direct execution
  and passing the Todo-grade gate.

Do not shape or promote an ordinary Backlog item, perform independent review,
approve Human-owned UAT, set `Human Review`, enter `Merging`, or merge a PR.
Use `$shea-symphony-issue-forge` for normal Backlog promotion and
`$shea-symphony-manual-review` after Main handoff.

## Bind the Repository

From the Shea Halo repository root:

1. Read `AGENTS.md`, `openwiki/quickstart.md`, and the issue-relevant canonical
   docs before editing.
2. If `SHEA_SYMPHONY_APP_PROFILE_PATH` is set, read that profile. Otherwise
   prefer `.shea/app-profile.local.json`, then `.shea/app-profile.json`.
3. Resolve the profile's `workflow_path` and `cli_path`; otherwise use
   `.shea/workflows/shea-symphony.md` and `.shea/bin/shea-symphony` when present.
4. Resolve absolute paths and verify the CLI with `--help`.
5. Confirm the workflow targets the current repository and Project, and read
   `git.base_branch`, workspace root, prompts, and verification commands. An
   explicit issue target branch overrides the workflow default only when the
   contract states it unambiguously.

Use short task-specific variables such as:

```bash
SHEA_CLI="<resolved-cli>"
SHEA_WORKFLOW="<resolved-workflow>"
ISSUE="#<number>"
```

Never patch `.shea/app/` or `.shea/bin/` unless the issue explicitly targets
the vendored runtime.

## Targeted Preflight

Use issue-scoped reads rather than whole-Project scans:

```bash
"$SHEA_CLI" project issue "$SHEA_WORKFLOW" "$ISSUE" --json
"$SHEA_CLI" project inspect "$SHEA_WORKFLOW" "$ISSUE" --lane main
"$SHEA_CLI" forge validate --workflow "$SHEA_WORKFLOW" --issue "$ISSUE"
"$SHEA_CLI" workspace show "$SHEA_WORKFLOW" "$ISSUE"
```

Use `gh issue view` and `gh pr view` only for the named issue or PR. Shea's
Project readback remains authoritative for status, claims, dependencies,
subissues, and linked PRs. Avoid routine `project state`, global `doctor`, and
all-lane loops. Stop instead of repeatedly scanning when GitHub quota is low.

Proceed only when:

- state is `Todo`, Main-lane `Rework`, matching resumable `In Progress`, or an
  explicitly authorized Backlog pickup;
- `Main Agent` is empty or belongs to this worker/run;
- native blockers are terminal and native subissues satisfy their Project gate;
- the quality gate is `Ready` or `ReadyWithAssumptions`;
- scope, target base, existing workspace, branch, and PR identity are clear;
- current `main`, open PRs, and recently merged related work have not solved or
  invalidated the contract.

For direct Backlog execution, validate the current body explicitly as Todo
without changing Project state:

```bash
"$SHEA_CLI" forge validate --workflow "$SHEA_WORKFLOW" --status todo \
  --title "<current-title>" --body-file "<current-body.md>"
```

Route an unexecutable contract to `Need to Clarify`. Route missing credentials,
operator authority, destructive approval, or unavailable external capability
to `Need Human Input`. Record the exact blocker before the final state change.

## Protect the Halo Boundary

Shea Halo research snapshots are evidence, not implementation branches.

- Never continue implementation in `halo/issue-*` worktrees or branches.
- Treat every Halo snapshot marked `experimental_snapshot_only` as read-only
  reference evidence.
- Start implementation from the confirmed current base in a separate
  issue-specific worktree and branch.
- Do not relabel target-repository changes as Shea Halo product work. A target
  such as FailureReport may be used only for issue-authorized disposable UAT.
- Preserve the GitHub Issue/Project product surface, append-only Halo workpad,
  managed workspace, exact-revision evidence, framework-native observation,
  HALO Engine, and credential-redaction boundaries.

## Claim and Select One Workspace

Before claiming, inspect `git status`, `git worktree list --porcelain`, linked
PRs, the Main Workpad, and recorded workspace/session evidence.

1. Reuse the one consistent issue worktree when it exists.
2. Evaluate the current task worktree for adoption before creating another.
   Adopt it only when it belongs to this repository, is not the canonical
   checkout, has no conflicting owner or git operation, and is clean for new
   work or consistently resumable for this issue.
3. Record the selected path and verify one canonical candidate:

   ```bash
   "$SHEA_CLI" workspace adopt "$SHEA_WORKFLOW" "$ISSUE" \
     "<issue-worktree>" --write
   "$SHEA_CLI" workspace show "$SHEA_WORKFLOW" "$ISSUE"
   ```

4. For normal `Todo`, `Rework`, or resumable `In Progress`, claim through Shea:

   ```bash
   "$SHEA_CLI" main claim "$SHEA_WORKFLOW" "$ISSUE" \
     --worker "<stable-worker-id>" --source manual --write
   ```

5. Write runtime ownership and readiness into the canonical Main Workpad, then
   make `In Progress` the phase's final mutation and read it back.
6. Create a new worktree and issue-numbered feature branch from the confirmed
   base only when no safe candidate exists. Never edit the canonical checkout
   or create a nested worktree inside another isolated worktree.

Stop for operator choice when multiple strong worktree candidates disagree or
existing changes belong to another issue. Maintain one issue, one branch, one
canonical worktree, and one PR.

### Explicit Backlog fast path

When the operator explicitly authorizes direct execution without promotion,
leave status `Backlog`, skip `main claim` and `In Progress`, record the skipped
states and Todo-grade validation in the Main Workpad, and otherwise follow all
normal isolation, verification, PR, and linkage gates. Move directly to
`Agent Review` only after every handoff invariant passes. If work cannot reach
a ready PR, leave it in Backlog with precise evidence.

## Execute the Main Work

1. Read the issue body, canonical Main Workpad, relevant timeline notes,
   `README.md`, `pyproject.toml`, `.shea/workflows/shea-symphony.md`, and the
   issue-relevant OpenWiki entry points and source files.
2. Write an issue-specific checkbox plan to the one Main Workpad before
   significant edits.
3. Implement only accepted scope in the issue worktree. Add concise comments at
   non-obvious runtime, schema, retry/idempotency, compatibility, trust, or
   external-service boundaries.
4. For exported Python APIs, Pydantic models, configuration, persisted schemas,
   or runtime behavior, audit intentional public visibility, compatibility, and
   documentation.
5. Do not hand-edit generated OpenWiki pages unless explicitly asked. Update
   source code or canonical docs and regenerate only through the reviewed wiki
   process when the issue owns that work.
6. Run issue-specific tests plus every workflow verification command:

   ```bash
   git diff --check
   uv sync --locked --all-groups
   uv run ruff format --check .
   uv run ruff check .
   uv run basedpyright
   uv run pytest
   uv build
   ```

7. Repair in-scope failures and rerun affected checks. Never claim an unrun
   command or certify operator-owned UAT.
8. Update the Main Workpad with changed files, decisions, risks, boundary
   comments, API/schema audit, exact commands/results, branch/worktree/commit,
   and UAT readiness.
9. Commit and push one issue branch. Open or update one ready, non-draft PR
   against the confirmed base. For the default base, include `Closes #<issue>`.
   For a non-default base, use `Refs #<issue>` and follow the workflow's
   accepted linkage procedure.
10. Read back the exact PR. For a default-base PR require
    `source=github_native`; repair the closing reference if absent. Treat
    `fallback_diagnostic` as diagnostic only and use it solely when the issue or
    operator explicitly accepts the non-default-base exception.
11. Complete the workpad with PR URL, base, ready status, linkage source,
    verification, residual risk, and the reason Main stops at `Agent Review`.
12. Make `Agent Review` the final mutation, then perform only targeted readback:

    ```bash
    "$SHEA_CLI" project set-state "$SHEA_WORKFLOW" "$ISSUE" agent_review --write
    "$SHEA_CLI" project issue "$SHEA_WORKFLOW" "$ISSUE" --json
    ```

Use the supported workpad surface for each Main Workpad update:

```bash
"$SHEA_CLI" project workpad "$SHEA_WORKFLOW" "$ISSUE" \
  "<workpad.md>" --write
```

Maintain exactly one `Shea Symphony Main Agent Workpad`; add Rework rounds to
it instead of creating competing workpads. Keep Review, Human Review, Merge,
Doctor, and Rework timeline notes separate and append-only.

## Hard Boundaries

- Never bypass issue quality, dependency, subissue, branch, or workspace gates.
- Never implement from a Halo experimental snapshot.
- Never edit in the canonical checkout or mix target-repository work into the
  Shea Halo product PR.
- Never treat a draft or unlinked PR as an Agent Review handoff.
- Never set `Human Review`, approve UAT, merge, force-push, or use the Merging
  Agent field.
- Never hide quota, provider, credential, permission, trust, or verification
  failures.
- After a Project status mutation, do only readback in the current session.
- Never finish by merely giving the operator a prompt for another agent.
