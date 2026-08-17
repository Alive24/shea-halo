# Shea Halo

Shea Halo is a GitHub-native research agent for improving agent loops and their harnesses. It observes a repository, analyzes OpenInference-shaped OpenTelemetry traces with the open-source HALO Engine, runs focused experiments in a managed Git worktree, and hands completed research to Shea Symphony through the same GitHub Project item.

The product surface is the Issue and Project—not an action CLI:

1. Create a research Issue in the observed repository.
2. Add it to the configured GitHub Project with status `Halo Research`.
3. Shea Halo records an append-only workpad as new Issue comments.
4. When research is complete and code work is justified, the same item moves to `Todo`.

There are no `analyze`, `instrument`, or `promote` product commands. The no-argument worker process only watches the Project queue and executes that lifecycle.

The first live study is [FailureReport #26](https://github.com/Alive24/FailureReport/issues/26).

## Lifecycle

```text
Halo Research
  ├─ more evidence needed ────────────────┐
  ├─ external blocker → Need Human Input │
  ├─ hypothesis disproved → Done         │
  └─ research complete → Todo            │
                    ▲                    │
                    └────────────────────┘
```

`Need Human Input` is reserved for a structured external blocker such as a missing credential, network failure, permission boundary, missing input, or unavailable service. Incomplete evidence stays in `Halo Research`.

Every workpad entry links to its predecessor, shows a bounded human-readable result first, and puts canonical JSON in a folded block. Shea Halo never edits an Issue body or existing comment. Marker-like comments from untrusted authors are ordinary discussion, while an edited trusted workpad comment fails closed.

For a material result, the visible part of the comment is the operator decision surface. It identifies the authoritative post-gate outcome and lifecycle phase, base and candidate revisions, experimental snapshot, deterministic evidence-gate disposition, implementation handoff, verified external blocker, model-authored residual risks, and next lifecycle action. Deterministic gate failures are stored separately from residual risks, so a model summary that says research is complete cannot override a final `continue_research` outcome. Runtime endpoints, credentials, host paths, raw traces, prompts, model/tool payloads, and command output are excluded from this projection.

Interpret the four rendered result states as follows:

| Authoritative outcome | Operator interpretation |
|---|---|
| `ready_for_todo` | The evidence gate passed. The visible handoff is dispatchable to Shea Symphony in a separate implementation workspace and branch. |
| `continue_research` | The result is non-terminal. Any retained handoff is explicitly provisional and non-dispatchable; satisfy the listed deterministic gate reasons before another terminal decision. |
| `blocked` | A verified external blocker names its category and required operator action. Resolve it, then return the item to `Halo Research`. |
| `no_change` | The evidence gate passed and research concluded without an implementation change. Move the item to `Done`; there is no dispatchable handoff. |

Normal and aggressive workpad compaction retain this visible decision boundary and a digest receipt for omitted structured detail. Structured JSON remains the recoverable machine authority.

On re-entry, Halo reads the prior trusted workpad result plus ordinary Issue discussion. It fingerprints the Issue, discussion, observation artifacts, branch snapshot, effective experiment contract, and the Shea Halo runtime source loaded at worker startup, so an unchanged incomplete item is not rerun every polling interval. The environment part records only allowlisted environment-variable names and whether each is present, plus the normalized effective OTLP endpoint—never credential values. New comments, new trace/Desktop artifacts, target code or configuration changes, a Shea Halo runtime update, runtime capability becoming available, and a daily documentation refresh reopen the loop.

Terminal routing is also append-only. Halo records a pending status transition, changes the Project field, then records the applied transition. If the status write succeeds but the second comment fails, the worker scans every status for Issues in the configured target repository on its next pass. It either completes that workpad record or records that a later downstream status such as `In Progress` superseded the pending Halo transition; it never moves the item backward.

Durable workpad checkpoints are reserved for typed lifecycle changes. If a proposed `HaloState` differs from the trusted head only in its non-authoritative `updated_at` value, Halo keeps the existing comment and revision and writes a bounded `workpad_checkpoint_suppressed` receipt to the target-local `.shea/logs/halo/worker.jsonl`. The receipt contains issue/run/phase correlation, a fixed reason, and a digest—not state, summaries, prompts, payloads, endpoints, credentials, exceptions, or host paths. Every base, workspace, snapshot, publication intent, observation baseline, result, blocker, and pending/applied transition change remains append-only durable state. If typed comparison or local receipt persistence is unavailable, Halo fails safe by retaining the GitHub checkpoint.

## Experimental branches

Halo owns `.shea/worktrees/halo/` and may publish `halo/issue-<number>-<stable-semantic-slug>`.

Every published Halo branch has `reuse_policy: experimental_snapshot_only`. It is evidence for selective inspection or cherry-picking; nobody should open a PR from it or continue development on it. Shea Symphony must use a separate implementation worktree and branch.

The runtime checks the complete open/closed/merged PR history for the exact branch before restoration, publication, and handoff; any PR use fails closed. Before a commit or push, Halo appends an immutable publication intent containing the parent revision and a content manifest. A crash before or after push can therefore resume only the exact recorded snapshot.

Candidate validation deliberately happens after publication:

```text
publication intent
→ commit and push the exact experimental snapshot
→ persist the pre-validation observation baseline
→ setup
→ establish the experiment-only observation baseline
→ runtime experiments
→ freeze the candidate observation delta
→ deterministic verification
→ confirm the candidate observations are unchanged
→ exact-dataset HALO Engine analysis
→ zero-tool final evidence synthesis
→ Project routing
```

Every post-publication action receives `CATALYST_SERVICE_VERSION=<snapshot SHA>`. Candidate trace evidence is the worktree delta created strictly between the post-setup baseline and the end of runtime experiments; setup output, verification-only output, and traces changed by verification are excluded. A tracing result is complete only when the HALO dataset revision equals that SHA and every selected candidate trace contains the same value in `resource.attributes["service.version"]`. HALO Engine is then given exactly those candidate trace digests, not the rest of the observation history.

The preliminary investigator does not own the terminal conclusion. After deterministic validation, a separate read-only Agents SDK turn with zero tools re-decides the semantic outcome from the exact candidate SHA, trace summaries and digests, exact-dataset HALO report, action receipts, verification, source-clean result, and current guide snapshots. `Todo` and `no_change` evidence must mechanically cite the exact report digest and candidate dataset at that revision. The report passed to that turn and the report that is hashed are the same bounded UTF-8 text; an oversized or invalid report fails closed instead of being truncated. An invalid or unavailable synthesis appends a `continue_research` result with no input fingerprint so a later worker pass can retry; Halo never silently falls back to the preliminary claim.

The persisted recovery baseline lets an interrupted validation safely recognize the same attempt artifacts on re-entry.

The base revision, branch, snapshot HEAD, and workpad lineage are persisted and revalidated on re-entry. One Halo lifecycle is pinned to the base SHA resolved when its workspace is first prepared. If the remote base branch advances before that lifecycle reaches a terminal conclusion, Halo fails closed to `Need Human Input`; it never promotes evidence across revisions. To investigate the new base, create a new Halo research Issue and place that new item in `Halo Research`. The original Issue remains the reviewable record for its pinned snapshot. A renamed Issue does not rename its established branch. Unrecorded files, commits, local HEAD changes, remote branch changes, and publication-manifest mismatches stop restoration.

## Framework-native tracing

Shea Halo has no hardcoded framework setup table. For each investigation it:

1. detects dependency and version evidence from bounded manifests throughout the target checkout;
2. retrieves the current [Inference documentation index](https://docs.inference.net/llms.txt);
3. dynamically ranks the current `/integrations/traces/*.md` guides against that evidence;
4. fetches and snapshots the selected official guide;
5. applies framework-native instrumentation first;
6. adds manual OpenInference spans only for lifecycle gaps proven to remain outside native coverage.

The guide URL, retrieval time, SHA-256 digest, dependency evidence, staleness, and selection reason are persisted in the workpad. New framework guides therefore become available without a Shea Halo release.

The catalog address itself is a protocol constant: `https://docs.inference.net/llms.txt`. A target cannot substitute an operator-authored framework table. If the network is unavailable, cached catalog and guide content may support continued research only as explicitly stale evidence; stale guidance cannot promote tracing changes to `Todo`.

For FailureReport, the live dependency scan finds `eve ^0.24.4` and selects the current [Vercel Eve tracing guide](https://docs.inference.net/integrations/traces/eve). The guide—not a Shea Halo framework case—defines Eve's native integration and coverage.

## HALO Desktop and Engine

The interoperability boundary is OpenInference-shaped OpenTelemetry:

- live local observation uses an optional standard OTLP-compatible collector on literal loopback; HALO Desktop is the default richer observation surface;
- offline analysis uses canonical JSONL with one span per line;
- `halo-engine` consumes that canonical dataset through its public async Python API;
- exported Desktop `report.md` files can be supplied as optional, bounded plain-text evidence;
- Shea Halo never reads Desktop SQLite, calls private Desktop APIs, or invents another span schema.

`catalyst_sdk_base_endpoint` identifies HALO Desktop or another operator-managed
HTTP(S) OTLP collector. It accepts either the SDK base URL or a full signal URL
ending in `/v1/traces`; Halo normalizes the latter to the base. The configured
URL and `CATALYST_OTLP_ENDPOINT` override must use the literal host `127.0.0.1`
or `::1`. DNS names, credentials in URLs, LAN/public addresses, redirects, and
hosted Catalyst destinations fail closed before trace payload egress.

Generic collectors remain the default: when `observation.desktop_preflight` is absent or set to `disabled`, Halo does not probe the endpoint. Operators who explicitly set `desktop_preflight = "external"` ask Halo to make one bounded pre-research check against only the derived `GET /health` and OTLP/JSON `POST /v1/traces` paths. The check follows no redirects and sends a deterministic synthetic span with no model I/O, tool data, credentials, endpoint data, or host paths. Its workpad receipt contains only the mode, allowlisted Desktop service identity, HTTP status classes, timestamps, payload digest, and pass/fail classification.

External mode never starts, stops, discovers, enumerates, signals, configures,
or otherwise owns the Desktop process. A successful POST is only an
ingest-acceptance receipt: Halo does not read Desktop SQLite, tRPC, WebSockets,
application-data paths, or trace lists, and cannot verify durable storage or UI
visibility. A failed or unavailable optional viewer is recorded as a bounded
observation classification and does not block canonical local evidence.

Trace discovery covers both the observed checkout and the active experiment worktree. Each canonical record is validated against HALO Engine's public `SpanRecord` contract, including full trace/span identifiers and required span structure, before analysis begins.

The investigator reads that JSON schema directly from the installed public `halo-engine` model before it designs a capture path. Shea Halo therefore does not duplicate or freeze a private canonical schema in framework-specific instructions.

Every real tracing experiment has a native-first capture contract. It offers
the same instrumented run to the configured loopback OTLP observer and
atomically writes canonical one-span-per-line JSONL under one of `trace_globs`.
The target-owned experiment must await exporter flush/shutdown and exit nonzero
when canonical JSONL cannot be persisted or finalized. Viewer absence does not
invalidate a completed target-native dataset. There is intentionally no
Desktop-to-JSONL bridge and no Shea-specific receiver.

An experimental code change cannot move to `Todo` from source diff and model claims alone. Halo requires a successful post-publication runtime experiment, a new or changed canonical candidate trace with an actual parent/child hierarchy, post-experiment HALO Engine analysis of exactly that candidate dataset, passing deterministic verification, and the exact published candidate revision. The same trace contract applies when a tracing experiment concludes `no_change`. A HALO Desktop report remains useful supplemental evidence when available, but is not a promotion requirement. Missing required evidence leaves the item in `Halo Research` for the next re-entry.

Shea Halo pins HALO Engine 0.3.5 and keeps both configured OpenAI API modes
explicit. HALO retains completed tool history in its typed chat-shaped context
and renders it as paired Responses `function_call` / `function_call_output`
items for replay. In `chat_completions` mode, the pinned Agents SDK converts the
same items to ordered assistant/tool messages with stable call identifiers and
an explicit `content: null` on tool-call-only assistant rows. Shea does not drop
completed tool turns or silently change API mode when a provider rejects that
shape; the existing structured provider-mode blocker remains authoritative.
HALO's `final_answer` call/result pair stays in replay history while its answer
is emitted once as the sanitized final report. No local replay monkey patch or
checkpoint persistence is enabled.

HALO Engine artifacts are local and ignored under `.shea/artifacts/halo/<run-id>/halo-engine/`:

- `analysis.json` — a versioned manifest with logical trace labels, digests, and run provenance;
- `events.jsonl` — bounded, sanitized event summaries;
- `report.md` — the sanitized HALO report;
- `halo-telemetry.jsonl` — a sanitized local copy of HALO's own telemetry.

## Research trace bundles

The worker initializes `inference-catalyst-tracing` once for its process
lifetime and retains its native OpenAI Agents instrumentation. Each claimed
research attempt creates one `shea-halo.research` root span and bounded Shea
stage spans around control-plane work that native instrumentation does not
cover. The same ended OpenTelemetry span objects fan out to the optional
loopback OTLP observer and a local canonical exporter.

Bundles live under the ignored, permission-restricted directory
`.shea/artifacts/halo/research-traces/<trace-bundle-id>/`:

- `control-plane.jsonl` contains canonical HALO Engine `SpanRecord` rows,
  including original trace/span/parent IDs, timestamps, status, resource,
  scope, attributes, events, and links;
- `manifest.json` contains only bounded counts, digests, service identities,
  revisions, safe run/issue/repository correlation, and logical references to
  target-native trace and HALO Engine artifacts;
- `*.incomplete` and `manifest.incomplete.json` identify interrupted or failed
  captures and are never promotion evidence.

The JSONL may contain prompts, responses, and tool payloads. Keep the entire
bundle inside ignored runtime storage with its default restrictive permissions;
do not paste it into Issues, workpads, logs, PRs, or other public evidence.
Choose a local retention period appropriate to the target's data and remove
expired bundle directories only while the worker is stopped. After a crash,
inspect the safe incomplete manifest for classification, preserve the spool
only for local diagnosis, and rerun the research item to produce a new bundle;
never rename an incomplete spool into canonical evidence by hand.

HALO Desktop can inspect a bundle either live through the configured loopback
OTLP endpoint or by importing `control-plane.jsonl` through its public file
import UI. HALO Engine consumes the same file directly through its public API.
This is Catalyst-native local telemetry, not a self-hosted Catalyst backend.

HALO Engine indexes and raw intermediate events live only in a per-run temporary directory. Host paths and credential-shaped values are removed before durable artifacts or public workpads are written. Live OTLP sent by the observed target to the configured Desktop or collector remains a trusted observation boundary and may contain application data; operators must secure that endpoint and choose target instrumentation deliberately.

Before HALO analysis, Shea Halo projects canonical spans through a bounded,
provenance-preserving semantic view. It recognizes documented OpenInference,
OpenTelemetry `gen_ai.*`, Vercel AI SDK `ai.*`, and Eve `$eve.*` aliases for
span kind, agent identity, model identity, and token usage. Identical aliases
retain every source key; conflicting populated aliases fail closed. Token
rollups accept complete input/output usage from attributable LLM spans only.
Enclosing Eve workflow aggregates are never added to their child usage, while
per-call token fields on non-LLM spans, missing parents, repeated span
identifiers, and tokenized LLMs without an identified AGENT ancestor stop
analysis.

The resulting `analysis.json` manifest contains only bounded semantic receipts:
safe identities and counts, source trace/span identifiers, source attribute
keys, contribution links, and receipt digests. It does not copy raw span
attributes, prompts, outputs, tool payloads, or endpoints.
Consumers that already hold validated HALO Engine `SpanRecord` objects can use
`shea_halo.trace_semantics.project_trace_semantics`; Shea Halo intentionally
does not expose raw attribute maps through that public result.

The committed fixture under `tests/fixtures/eve-semantics/` is explicitly
synthetic. Its LLM spans sum to the accepted public FailureReport totals of
498161 input tokens and 22392 output tokens, which proves normalization and
arithmetic against those published aggregates. It is not evidence that the
unavailable original FailureReport trace was parsed. Confirming that source is
a separate Human Review/UAT action: an authorized operator must select it
through a runtime-local path, keep it out of the repository and durable
artifacts, and retain only safe receipt counts, identities, digests, and
pass/fail evidence.

## Target configuration

Each observed checkout commits `.shea/halo.toml`:

```toml
schema_version = "shea-halo/v1"

[target]
repository = "Alive24/FailureReport"
base_branch = "main"

[tracker]
owner = "Alive24"
owner_type = "user"
project_number = 10
status_field = "Status"
research_state = "Halo Research"
ready_state = "Todo"
done_state = "Done"
blocked_state = "Need Human Input"
trusted_producers = ["Alive24"]

[observation]
trace_globs = [".shea/artifacts/halo/traces/*.jsonl"]
desktop_report_globs = [".shea/artifacts/halo/desktop/**/report.md"]
catalyst_sdk_base_endpoint = "http://127.0.0.1:8799"
# Optional: explicitly verify an already operator-managed HALO Desktop.
# desktop_preflight = "external"
require_candidate_traces = true

[models]
api_mode = "responses"

[setup]
commands = [
  ["pnpm", "install", "--lockfile-only"],
  ["pnpm", "install", "--frozen-lockfile"],
]

[experiments]
external_blocker_exit_codes = [69]
commands = [
  ["pnpm", "run", "halo:trace-smoke"],
]

[verification]
commands = [
  ["pnpm", "build"],
  ["pnpm", "check"],
  ["pnpm", "test"],
  ["pnpm", "format:check"],
]
```

The complete FailureReport example is at [`examples/failure-report/.shea/halo.toml`](examples/failure-report/.shea/halo.toml).

A framework-neutral Issue starting point is at [`examples/halo-research-issue.md`](examples/halo-research-issue.md).

Optional `[runtime]` paths default to `.shea/logs/halo`, `.shea/artifacts/halo`, and `.shea/worktrees/halo`. Runtime paths must remain below the target's `.shea/` directory and cannot escape through symlinks.

Optional `[models]` keys `investigator` and `halo` default to `OPENAI_MODEL`, `HALO_MODEL`, then `gpt-5.6`. `api_mode` accepts `responses` (the default) or `chat_completions`; the selected mode is applied explicitly to investigator, synthesis, correction, and HALO Engine calls without fallback. The non-secret mode is recorded in re-entry fingerprints, workpad state, and HALO analysis manifests, while provider endpoints and credentials remain runtime-only. `investigator_max_turns` defaults to `30` and may be set from `1` through `100`; increase it only when a repository-scale experiment genuinely needs more tool iterations. If that bound is reached after files changed, Halo uses the Agents SDK max-turn handler to checkpoint the changes as an experimental snapshot, runs exact-revision validation, and keeps the item in research instead of discarding useful work or claiming completion. The budget participates in re-entry fingerprinting, so changing it makes a failed research attempt immediately eligible for another pass.

For machine-local onboarding, a complete ignored `.shea/halo.local.toml` takes precedence over the shared file. It is a full replacement, so the effective contract always has one unambiguous source.

`tracker.trusted_producers` is the stable allowlist for the append-only workpad chain. Keep the previous worker identity in this list while rotating a GitHub App or bot token; the currently authenticated worker is also accepted for the comment it writes.

The command tables have distinct meanings:

- `[setup].commands` only prepares dependencies.
- `[experiments].commands` contains bounded target-owned runtime actions that test research hypotheses and, for tracing work, satisfy the OTLP plus canonical JSONL contract.
- `[verification].commands` contains deterministic build, type, test, and format checks.

The investigator can run any exact allowlisted argv by its stable index but cannot invent a shell command or append arguments. Install, build, typecheck, and formatting do not count as research experiments. Before terminal routing, Halo automatically reruns every configured setup, experiment, and verification action against the published snapshot; exploratory runs never substitute for this pass.

`[experiments].external_blocker_exit_codes` reserves target-owned statuses for failures that specifically mean an external capability or dependency is unavailable. A generic nonzero status remains research evidence and cannot move an Issue to `Need Human Input`; the example uses `69` for this explicit contract.

The FailureReport example names `pnpm run halo:trace-smoke` as the target-owned runtime action. The first research candidate may add that script and its native Eve instrumentation on the experimental branch. Until it exists and satisfies the dual-output contract, the action fails and the item remains in `Halo Research`.

When a trusted target must call a real provider during an experiment, the ignored local config may add environment names—not values—with `pass_env = ["OPENAI_API_KEY"]` under `[experiments]`. Shared committed config is forbidden from requesting host credentials. Only explicitly named runtime values are restored to experiment processes; setup and verification retain the credential-scrubbed environment. The snapshot/workpad redaction boundary still applies.

## Runtime and security boundary

Shea Halo is a long-running local worker because experiments need the target's real Git toolchain, package managers, credentials, and optionally a local HALO Desktop or another compatible collector. A deployment supplies:

- `SHEA_HALO_TARGETS`: local target checkout roots separated by the platform path separator;
- `OPENAI_API_KEY`: used by OpenAI Agents SDK and HALO Engine;
- GitHub authentication through `gh` or `GH_TOKEN`, with repository and Project access;
- optional loopback-only `CATALYST_OTLP_ENDPOINT`;
- optional `DENO_DIR`: a persistent writable Deno cache for restricted launchers that cannot write Deno's normal user cache.

If `CATALYST_OTLP_ENDPOINT` is absent, the configured Catalyst SDK base endpoint
is used. `CATALYST_OTLP_TOKEN` and the legacy OTLP ingest token are rejected in
local-only mode; a loopback observer requires no trace credential.

Repository experiment and verification processes receive a credential-scrubbed environment. Their raw output is shown to the active investigator only after redaction; GitHub receives exit status and output digests, not stdout or stderr. Before a branch is staged, Halo rejects sensitive paths, known credential material, common token forms, path escapes, and oversized files.

Deterministic Git lifecycle operations use the host's safe credential-provider handles but never expose those handles or raw credentials to target commands. The target repository and configured experiment commands still execute real host code, so the operator must trust the repositories they choose to observe.

Run exactly one worker per target checkout. On macOS and Linux, a non-blocking local lease under `.shea/logs/halo/worker.lock` prevents concurrent workers from forking the append-only workpad. Separate clones of the same target are not coordinated in this first version.

The service-manager entry point is:

```text
python -m shea_halo
```

It accepts no action arguments. Investigation, re-entry, promotion, and completion happen only through GitHub Issues and Project status.

## Development

Use Python 3.11–3.14 and `uv`:

```text
uv sync --locked --all-groups
uv run ruff format --check .
uv run ruff check .
uv run basedpyright
uv run pytest
uv build
```

Live model and GitHub mutation tests are deliberately opt-in. The ordinary suite uses local bare repositories, recorded catalog fragments, and fake tracker data.
