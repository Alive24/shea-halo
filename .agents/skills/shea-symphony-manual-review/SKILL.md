---
name: shea-symphony-manual-review
description: Trigger one independent Shea Symphony Review run for a named Shea Halo issue through the external backend configured by this repository. Use when an issue has a ready linked PR in Agent Review, or when the operator explicitly authorizes preparation of one unambiguous standalone implementation for review. Resolve the repo-local CLI and workflow, validate the handoff, invoke exactly one `review once`, and read back its result. Do not inspect or judge the diff in the current agent.
---

# Shea Halo Manual Review

Launch one operator-selected Shea Halo review through the repository's
configured independent Review backend. The current task is the operator-side
launcher, not the reviewer. The backend owns code inspection, verification,
findings, evidence, and routing.

Never replace a failed external launch with current-session review, fabricated
evidence, or a direct Project mutation.

## Bind the Repository and Backend

From the Shea Halo repository root:

1. Read `AGENTS.md`, `openwiki/quickstart.md`, and the issue-relevant canonical
   docs.
2. Resolve `workflow_path` and `cli_path` from
   `SHEA_SYMPHONY_APP_PROFILE_PATH`, otherwise
   `.shea/app-profile.local.json`, then `.shea/app-profile.json`; fall back to
   `.shea/workflows/shea-symphony.md` and `.shea/bin/shea-symphony` only when
   necessary.
3. Resolve absolute paths, verify the CLI with `--help`, and confirm the
   workflow targets the current repository and Project.
4. Read the base branch, workspace root, Review prompt, verification commands,
   and `review_lane` configuration.

Use task-specific variables:

```bash
SHEA_CLI="<resolved-cli>"
SHEA_WORKFLOW="<resolved-workflow>"
ISSUE="#<number>"
```

Require a supported real external backend. For this repository the committed
workflow currently selects `agy-cli`; treat the live workflow as authoritative.
Reject fake, fixture-only, empty, or unknown backends. Do not call AGY, Gemini,
Codex, Claude, or any reviewer command directly; Shea must launch it.

If executable, authentication, model, quota, policy, sandbox, or transport is
unavailable, report the exact backend failure and stop. Never fall back to
local review.

## Targeted Preflight

Use issue-scoped reads:

```bash
"$SHEA_CLI" project issue "$SHEA_WORKFLOW" "$ISSUE" --json
"$SHEA_CLI" project inspect "$SHEA_WORKFLOW" "$ISSUE" --lane review
"$SHEA_CLI" workspace show "$SHEA_WORKFLOW" "$ISSUE"
```

Use `gh issue view` and `gh pr view` only for the named issue and linked PR. Do
not use whole-Project scans or an all-lane loop for routine review preflight.

Confirm:

- status is `Agent Review`, unless the operator explicitly requests supported
  re-review or standalone handoff preparation;
- exactly one intended PR is Project-visible, open, ready, and not draft;
- PR base/head/commit, Main Workpad, and canonical workspace are consistent;
- no active Review claim or queued job already owns the issue;
- dependencies and native subissues satisfy their gates;
- the external backend passes configuration validation.

For a native subissue, preserve independent Agent Review but route a routine
PASS to `Merging`; the parent owns final Human Review and UAT unless an explicit
`Subissue Human Review Exception` is recorded.

## Preserve Shea Halo Review Boundaries

The external reviewer must evaluate the issue contract and the current PR,
including these repo-specific boundaries when relevant:

- `halo/issue-*` snapshots remain reference-only and are not implementation
  branches;
- the append-only Halo workpad, trusted lineage, exact revision, and two-phase
  publication/Project transition boundaries remain recoverable;
- target repositories such as FailureReport are used only for contracted UAT,
  not silently changed as Shea Halo product scope;
- persisted errors, receipts, traces, prompts, endpoints, credentials, and host
  paths remain within their redaction and managed-filesystem boundaries;
- framework-native observation and HALO Engine evidence are not duplicated or
  overstated;
- exported Python APIs, Pydantic models, configuration fields, persisted
  schemas, and runtime behavior have intentional visibility, compatibility,
  documentation, and focused tests;
- generated OpenWiki content is not hand-edited outside an issue-owned reviewed
  regeneration.

Human-owned UAT that has not yet run is not by itself an implementation defect.
It is a Human Review follow-up unless the issue required Main to implement the
UAT harness or fixture and that deliverable is missing.

## Standalone Review Fast Path

Use only when the operator explicitly asks to review a named implementation
that did not pass through Main. Before any mutation require exactly one ready
PR, one matching clean pushed worktree/head, the expected base, no terminal
issue or active lane owner, terminal dependencies, and a valid external backend.

Prepare only through Shea, in this order:

```bash
"$SHEA_CLI" project link-pr "$SHEA_WORKFLOW" "$ISSUE" "#<pr>" --write
"$SHEA_CLI" workspace adopt "$SHEA_WORKFLOW" "$ISSUE" \
  "<worktree>" --write
"$SHEA_CLI" project issue "$SHEA_WORKFLOW" "$ISSUE" --json
"$SHEA_CLI" workspace show "$SHEA_WORKFLOW" "$ISSUE"
"$SHEA_CLI" project set-state "$SHEA_WORKFLOW" "$ISSUE" agent_review --write
"$SHEA_CLI" project issue "$SHEA_WORKFLOW" "$ISSUE" --json
```

Preserve assignee and do not manufacture a Main claim. Make `Agent Review` the
final preparation mutation. For an explicitly accepted non-default-base PR,
allow exactly one `fallback_diagnostic` linkage attempt; never retry it or call
it GitHub-native. Any ambiguous, additional, draft, closed, or mismatched PR is
a hard stop.

## Launch Exactly One Review

Run:

```bash
"$SHEA_CLI" review once "$SHEA_WORKFLOW" "$ISSUE" --write
```

`review once` owns prompt rendering, external backend launch, output parsing,
review evidence, checklist handling, and state routing. Do not separately run
`review claim`, `review pass`, `review reject`, or claim-clearing commands; they
belong to a different manual-evidence path.

Do not inspect the PR diff, run reviewer verification, classify findings, edit
the issue checklist, or override the backend conclusion in the current agent.
Reading metadata and generated evidence for launch verification is allowed.

## Read Back

After the command returns, perform only targeted readback:

```bash
"$SHEA_CLI" review status "$SHEA_WORKFLOW" \
  --issue "$ISSUE" --recent 3 --verbose
"$SHEA_CLI" project issue "$SHEA_WORKFLOW" "$ISSUE" --json
```

Report the backend identity, terminal job result, evidence location, linked PR,
and resulting Project state. A PASS normally routes an ordinary Shea Halo issue
to `Human Review`; confirmed implementation defects route to `Rework`;
inconclusive or backend failures must not be described as review success.

## Hard Boundaries

- Do not edit implementation code, review the diff, merge, force-push, or
  approve Human-owned UAT.
- Do not run fake review, direct reviewer commands, raw Project GraphQL, or
  manual review-evidence commands alongside `review once`.
- Do not create a manual Review claim or evidence file.
- Do not convert backend configuration, quota, timeout, authentication, policy,
  or transport failures into a code-review result.
- Do not overwrite the Main Workpad; Review evidence remains standalone and
  append-only.
- Do not present targeted `review once` as an autopilot or all-lane loop.
- After routing, perform readback only.
