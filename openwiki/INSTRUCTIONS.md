# Shea Halo Code Wiki

A code wiki for this local repository. Prioritize a concise quickstart,
architecture overview, source map, key workflows, domain concepts,
operations/runbook notes, testing guidance, and integration points. Inspect git
history to understand reasoning behind code changes and the progression of the
repository. Keep pages grounded in the repository structure and recent code
changes. Prefer practical navigation for engineers and coding agents over
generic summaries.

## Project-specific priorities

- Explain the GitHub Issue and Project-driven product surface and the boundary
  between Shea Halo research and Shea Symphony implementation.
- Document the complete Halo Research lifecycle, append-only workpad,
  experimental snapshot, exact-revision validation, re-entry, failure, and
  recovery semantics.
- Explain framework-native Catalyst/OpenTelemetry/OpenInference tracing, the
  roles of HALO Desktop and HALO Engine, canonical JSONL evidence, trace
  semantics, and local-only observability boundaries.
- Make security and trust boundaries easy to find: untrusted Issue input,
  credential handling, contained runtime paths, branch authority, artifact
  publication, sanitization, and fail-closed behavior.
- Document configuration from the committed examples and current models,
  including what belongs in shared `.shea/halo.toml` versus ignored
  `.shea/halo.local.toml`.
- Include contributor workflows, verification commands, important tests,
  operational diagnostics, and the FailureReport research case where they are
  supported by current repository evidence.

## Authority and maintenance

- Treat source code, tests, committed workflow contracts, and current
  configuration models as authoritative. Treat `README.md` as the maintained
  product overview. The generated wiki explains and links to those sources; it
  does not replace or silently redefine them.
- Distinguish current behavior from proposed work, experimental evidence, and
  historical decisions. Do not describe an open Issue or experimental branch as
  shipped behavior.
- GitHub Issues and Projects are the only backlog authority. Do not invent,
  infer, or maintain a separate backlog in the wiki. Link to the relevant live
  Issue when proposed work materially clarifies current boundaries.
- Prefer several focused, linked concept pages over duplicating the long README.
  Link back to relevant source paths and canonical external documentation.
- Preserve useful hand-written wiki content and update only what current source
  or history justifies.

## Safety and exclusions

- Do not inspect or document ignored runtime state under `.shea/artifacts/`,
  `.shea/logs/`, or `.shea/worktrees/`, virtual environments, caches, local
  configuration, credentials, or host-specific files.
- Do not include raw traces, prompts, model outputs, tool arguments/results,
  tokens, endpoint values, host paths, private workpad payloads, or secret-like
  values.
- Do not treat HALO Desktop private storage or APIs as an information source.
- Treat append-only research evidence in the linked FailureReport #26 Issue as
  evidence distinct from committed synthetic fixtures. Never imply that raw
  source traces should be copied into this repository.
- Do not generate or modify CI automation as part of the initial wiki
  bootstrap. Recurring updates will be introduced separately after the local
  workflow is reviewed.
