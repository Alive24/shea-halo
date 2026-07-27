---
type: Configuration Guide
title: Shea Halo target configuration
description: Source-grounded reference for shared and machine-local Shea Halo target configuration, model API routing, allowlisted actions and environment names, observation settings, and contained runtime paths.
tags: [shea-halo, configuration, operations, security]
---

# Shea Halo target configuration

Shea Halo loads one versioned TOML contract from each observed target checkout. That contract connects a repository and GitHub Project to the [research lifecycle](/openwiki/workflows/research-lifecycle.md), selects bounded observation and action surfaces, and supplies contained runtime locations to the [worker architecture](/openwiki/architecture/overview.md). The authoritative parser is `src/shea_halo/config.py`; `examples/failure-report/.shea/halo.toml` is the checked-in complete example, and `tests/test_config.py` locks down defaults and rejection cases.

## Shared contract versus local replacement

Commit `.shea/halo.toml` as the reviewable team contract. It must declare `schema_version = "shea-halo/v1"` and normally contains repository/Project identity, observation policy, non-secret model choices, and exact setup, experiment, and verification actions.

An ignored `.shea/halo.local.toml`, when present, takes precedence. It is a **complete replacement**, not an overlay: `HaloConfig.load()` parses only the local file and does not merge missing tables or keys from the committed file. Start by copying the shared contract, then make only the machine-local changes needed for that operator. This single-source rule was present in the initial Issue-driven worker and is tested by `test_complete_local_config_takes_precedence` (`src/shea_halo/config.py`, `README.md`, `tests/test_config.py`).

Do not put credential values in either file. The local replacement may name environment variables that an experiment is allowed to receive, but the values stay in the worker environment. This is part of the broader [security and trust boundary](/openwiki/architecture/security.md).

## Contract by table

### Target and tracker

`[target]` requires `repository`; `base_branch` defaults to `main`. `[tracker]` requires `owner` and a positive `project_number`. It also configures Project ownership and lifecycle field names:

| Key | Default or constraint |
|---|---|
| `owner_type` | `user`; the other accepted value is `organization`. |
| `status_field` | `Status`. |
| `research_state` | `Halo Research`. |
| `ready_state` | `Todo`. |
| `done_state` | `Done`. |
| `blocked_state` | `Need Human Input`. |
| `trusted_producers` | Empty list; entries must be non-empty GitHub logins and are normalized into a case-insensitively sorted, duplicate-free tuple. |

The four lifecycle states must be distinct ignoring case. `trusted_producers` is the stable author allowlist for the append-only workpad chain; retain the previous worker identity during an authentication rotation so prior trusted entries remain readable (`src/shea_halo/config.py`, `README.md`, `tests/test_config.py`).

### Observation

`[observation]` configures evidence discovery used by [observability and trace analysis](/openwiki/workflows/observability.md):

- `trace_globs` defaults to canonical JSONL under `.shea/artifacts/halo/traces/`.
- `desktop_report_globs` defaults to optional Desktop reports under `.shea/artifacts/halo/desktop/`.
- `catalyst_sdk_base_endpoint` defaults to the documented local HTTP collector. It must be an HTTP(S) URL with a host and no embedded user information, query, or fragment. A full signal URL ending in `/v1/traces` is normalized to its SDK base; content after that signal path is rejected. The obsolete `desktop_otlp_base_endpoint` key is rejected.
- `desktop_preflight` defaults to `disabled`. The only opt-in value is `external`, which requests the bounded check of an already operator-managed Desktop described on the observability page; it does not transfer process ownership to Halo.
- `require_candidate_traces` is a strict boolean and defaults to `true`.

Observation globs must be relative and cannot contain `..`. A target cannot set `observation.catalog_url`; the framework-integration catalog is fixed by the Shea Halo protocol (`src/shea_halo/config.py`, `README.md`, `tests/test_config.py`).

### Models and API route

`[models]` separates model selection from OpenAI-compatible API routing:

| Key | Resolution |
|---|---|
| `investigator` | Explicit value, otherwise the runtime `OPENAI_MODEL` value, otherwise `gpt-5.6`. |
| `halo` | Explicit value, otherwise the runtime `HALO_MODEL` value, otherwise `gpt-5.6`. |
| `api_mode` | `responses` by default; the only alternative is `chat_completions`. |
| `investigator_max_turns` | `30` by default; accepted range is 1 through 100 inclusive. |

`OpenAIModelBootstrap` applies the selected `api_mode` to the Agents SDK provider, run configuration, and public SDK default. It does not auto-negotiate or fall back to another route. Provider route rejections with HTTP 404, 405, or 501 become a structured `ProviderApiModeError` containing only the selected mode and status code; tests verify that provider request details are not copied into that error (`src/shea_halo/provider.py`, `tests/test_provider.py`).

The explicit route was introduced to make one validated API mode apply across investigator, synthesis, correction, and HALO Engine calls. `README.md` records the non-secret mode as lifecycle evidence while keeping provider endpoints and credentials runtime-only. Changing the investigator turn budget changes the effective experiment contract; reaching the bound does not authorize a completion claim.

### Setup, experiments, and verification

Each action is a non-empty argv array. The parser preserves the configured order and does not accept shell strings, empty commands, or empty arguments. The three tables have different authority (`src/shea_halo/config.py`, `README.md`):

- `[setup].commands` prepares dependencies only.
- `[experiments].commands` contains bounded, target-owned runtime actions that test a research hypothesis. The investigator may select an exact entry by stable index but cannot invent a command or append arguments.
- `[verification].commands` contains deterministic build, type, test, and formatting checks.

Before terminal routing, Halo reruns all configured setup, experiment, and verification actions against the published snapshot; exploratory runs are not a substitute. Setup and verification remain credential-scrubbed. Only experiments can receive locally allowlisted environment values.

`[experiments].external_blocker_exit_codes` is an optional set of integer statuses from 1 through 255. These statuses mean that a target-owned action could not access a specifically external capability; an ordinary nonzero result remains research evidence rather than automatically routing the Issue to human input.

The committed FailureReport contract makes the stages concrete (`examples/failure-report/.shea/halo.toml`, `tests/test_config.py`):

1. setup performs lockfile-only and frozen-lockfile dependency installation;
2. the sole experiment invokes `pnpm run halo:trace-smoke` and reserves exit status `69` for its external-blocker contract;
3. verification runs build, check, test, and format-check actions.

The experiment script is a contract for the research candidate, not proof that the target already implements it. Until the action exists and satisfies the dual OTLP/canonical-JSONL evidence requirement, research remains incomplete (`README.md`).

## Environment-name allowlist

`[experiments].pass_env` contains **names only**. Every name must match a conventional environment identifier: it starts with a letter or underscore and continues with letters, digits, or underscores. Names are deduplicated and sorted when loaded.

This key is rejected in committed `.shea/halo.toml`; it is accepted only from the ignored complete local replacement. At execution time, only those named values are restored to experiment processes. Setup and verification do not receive them. For example, a trusted target that must call a provider can allowlist the documented runtime name `OPENAI_API_KEY` locally without storing its value in TOML (`src/shea_halo/config.py`, `README.md`, `tests/test_config.py`).

Treat the allowlist as capability granting, not convenience configuration: include only names required by the exact experiment commands, and never record their values in config, artifacts, workpads, or branches.

## Runtime path containment

Optional `[runtime]` keys select managed storage:

| Key | Default |
|---|---|
| `logs` | `.shea/logs/halo` |
| `artifacts` | `.shea/artifacts/halo` |
| `worktrees` | `.shea/worktrees/halo` |

All three must be relative, must not contain `..`, and must resolve **below** the target repository's `.shea/` directory—not to `.shea/` itself. The loader resolves both the repository and candidate path, so absolute paths, paths elsewhere in the checkout, and symlink escapes fail closed (`src/shea_halo/config.py`, `tests/test_config.py`). Observation globs have a separate but related repository-relative containment check.

These paths identify ignored runtime state, not documentation or source evidence. Do not commit or inspect their contents as part of configuration maintenance.

## Safe editing and validation

1. Update the committed FailureReport-style contract when the change is shared, reviewable, and non-secret. Use the ignored complete replacement only for machine-local selections or experiment environment-name grants.
2. Keep action arrays exact and preserve the semantic split among setup, experiments, and verification.
3. Check observation and runtime paths for containment before running the worker.
4. Run focused tests from the Shea Halo repository:

```text
uv run pytest tests/test_config.py tests/test_provider.py
```

For changes that affect action execution, observation, or lifecycle fingerprints, continue with the related service, agent-boundary, observation, and integration suites under `tests/`. The focused config tests also load the checked-in FailureReport example, preventing that operational contract from drifting away from the parser.

## Source anchors

- `src/shea_halo/config.py` — parsing, defaults, precedence, normalization, and fail-closed validation.
- `src/shea_halo/provider.py` — API-mode bootstrap and sanitized unsupported-route errors.
- `examples/failure-report/.shea/halo.toml` — committed three-stage target contract.
- `tests/test_config.py`, `tests/test_provider.py` — executable configuration and provider contracts.
- `README.md` — maintained operational meaning and lifecycle consequences of configuration.
