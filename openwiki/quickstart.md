---
type: Project Guide
title: Shea Halo quickstart
description: Entry point to the Shea Halo codebase, its GitHub-native research product, architecture, safety boundaries, workflows, configuration, and contributor guidance.
resource: https://github.com/Alive24/shea-halo
tags: [shea-halo, quickstart, github, research-agent]
---

# Shea Halo quickstart

Shea Halo is an alpha-stage GitHub-native research agent for improving agent
loops and their harnesses. It runs as a long-running local Python worker whose
product surface is a GitHub Issue in a configured GitHub Project: moving an
item into `Halo Research` starts or resumes research, while a sufficiently
proven implementation handoff moves the same item to `Todo`. It is **not** an
action CLI; `python -m shea_halo` rejects arguments and continuously polls
configured targets (`README.md`, `src/shea_halo/__main__.py`,
`src/shea_halo/service.py`).

The maintained product overview is `README.md`. This wiki is an engineer-oriented map of the current source, tests, and committed workflow contracts; it does not replace that README or turn proposed research into shipped behavior.

## Product and authority boundary

Halo owns observation, research, bounded experiments, and reference-only `halo/issue-*` snapshots. The [research lifecycle](/openwiki/workflows/research-lifecycle.md) routes a completed implementation recommendation to `Todo`, where Shea Symphony owns implementation in a **separate** worktree and branch. Halo snapshots have `reuse_policy: experimental_snapshot_only`: do not open a PR from them or continue development on them (`src/shea_halo/models.py`, `src/shea_halo/workpad.py`, `.shea/workflows/shea-symphony.md`).

The two GitHub Projects are also distinct contracts:

- a target repository's `.shea/halo.toml` names the Project and statuses Halo observes; the committed FailureReport example uses its own target Project;
- `.shea/workflows/shea-symphony.md` configures Shea Halo repository implementation work for Symphony, with implementation, review, and merge lanes.

## Start here

1. **Understand execution:** [Architecture and source map](/openwiki/architecture/overview.md) explains the worker, GitHub adapter, investigator, managed Git workspace, observations, HALO Engine, and handoff boundary.
2. **Follow a research item:** [Halo Research lifecycle](/openwiki/workflows/research-lifecycle.md) covers append-only workpads, exact snapshots, validation, routing, re-entry, failures, and recovery.
3. **Trace evidence identities:** [Durable evidence and provenance model](/openwiki/concepts/evidence-model.md) connects workpad state, Git revisions, observation checkpoints, semantic receipts, HALO datasets, and promotion.
4. **Understand trace transport:** [Observability and trace evidence](/openwiki/workflows/observability.md) covers framework-native guidance, Catalyst and OTLP, HALO Desktop, canonical JSONL, HALO Engine, semantic receipts, and the FailureReport case.
5. **Review trust zones before changing side effects:** [Security and trust boundaries](/openwiki/architecture/security.md) maps untrusted inputs, credential handling, path containment, branch authority, sanitization, and fail-closed checks.
6. **Configure a target:** [Target configuration](/openwiki/operations/configuration.md) distinguishes shared `.shea/halo.toml` from the ignored complete replacement `.shea/halo.local.toml`.
7. **Develop or extend:** [Contributing and operations](/openwiki/operations/contributing.md) lists verification commands and test ownership; [Extending Shea Halo safely](/openwiki/architecture/extending.md) maps cross-layer changes.
8. **Diagnose by symptom:** [Troubleshooting runbook](/openwiki/operations/troubleshooting.md) preserves evidence while narrowing leases, re-entry, worktrees, transitions, and trace failures.

Use the [glossary](/openwiki/concepts/glossary.md) when lifecycle or observability
terms are unfamiliar. Maintainers should follow the
[OpenWiki maintenance runbook](/openwiki/operations/wiki-maintenance.md) instead
of repeating full updates until the prose changes.

## Repository orientation

| Area | Purpose |
|---|---|
| `src/shea_halo/service.py` | Top-level polling, lifecycle orchestration, exact-revision validation, outcome gates, Project routing, and recovery. |
| `src/shea_halo/models.py` | Strict persisted lifecycle, evidence, snapshot, transition, and preflight models. |
| `src/shea_halo/agent.py` | Investigator tools, configured action execution, framework-native tracing contract, and final zero-tool synthesis. |
| `src/shea_halo/github.py` | GitHub Project inventory/status updates, Issue comments, research lease, and branch PR-history checks. |
| `src/shea_halo/workpad.py`, `workspace.py` | Trusted append-only journal and managed Git worktree/publication protocol. |
| `src/shea_halo/observation.py`, `halo_engine.py`, `trace_semantics.py` | Canonical evidence inventory, exact HALO analysis, and bounded semantic projection. |
| `src/shea_halo/config.py`, `provider.py`, `integrations.py` | Target contract, explicit model API route, official guide discovery, and bounded Desktop preflight. |
| `src/shea_halo/security.py`, `runtime_fs.py`, `locking.py` | Redaction, environment scrubbing, contained runtime I/O, and one-worker lease. |
| `tests/` | Hermetic behavioral contracts, including local bare Git repositories, fake tracker data, and synthetic trace fixtures. |
| `examples/` | Target configuration and Issue templates; the FailureReport case is research evidence, not a bundled implementation. |
| `.shea/workflows/`, `.shea/prompts/`, `.shea/template/` | Tracked Shea Symphony integration contracts. Runtime output directories are ignored and outside wiki evidence. |

## Development fast path

Use Python 3.11–3.14 and `uv` (`pyproject.toml`):

```text
uv sync --locked --all-groups
uv run ruff format --check .
uv run ruff check .
uv run basedpyright
uv run pytest
uv build
```

The same checks are encoded in `.github/workflows/ci.yml` and the Symphony workflow. Ordinary tests do not require live GitHub mutations or model calls.

## Historical shape

The code began as an Issue-driven worker, then was hardened around revision-bound lifecycle semantics. Recent changes added deterministic incomplete-research re-entry, preservation at investigator turn limits, intent-first snapshot publication, runtime-receipt-bound evidence, explicit OpenAI API mode, fail-closed Eve trace attribution/token rollups, and an opt-in bounded external HALO Desktop preflight. The latest commits update vendored Symphony runtime assets; they do not move Halo's research/implementation authority boundary.

## Proposed work

GitHub Issues and Projects—not this generated wiki—are the backlog authority.
For example, [Shea Halo #9](https://github.com/Alive24/shea-halo/issues/9)
tracks the not-yet-shipped complete local research trace bundle. Regenerate the
wiki after related implementation lands instead of maintaining inferred backlog
items here.
