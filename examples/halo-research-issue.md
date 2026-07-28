# Halo Research: <observable improvement question>

## Research objective

Describe the agent-loop or harness outcome to improve. State the user-visible or operator-visible problem without prescribing an implementation.

## Questions to resolve

- What does current trace and source evidence prove?
- Which competing hypotheses could explain the behavior?
- Is trace coverage sufficient to distinguish them?
- Which smallest experiment can falsify the leading hypothesis?
- If tracing changes are needed, which current framework-native Inference guide applies?

## Completion contract

- Record facts, hypotheses, experiments, and residual risks in append-only Halo workpad comments.
- Resolve tracing changes against the current official Inference catalog rather than remembered setup code; record dependency evidence, guide URL, retrieval time, digest, staleness, and selection reason.
- Publish the experimental snapshot before final validation, then run setup, runtime experiments, and deterministic verification against that exact SHA.
- Make each tracing experiment use framework-native tracing, offer standard OTLP to the configured loopback observer, and atomically write canonical one-span-per-line JSONL; it must fail if canonical JSONL flush or finalization fails, while an absent optional viewer is supplemental status.
- Attribute candidate traces only to the post-setup runtime-experiment window, and reject candidates changed by later verification.
- Prove experimental changes with a new candidate trace hierarchy, exact-dataset HALO Engine analysis, a matching dataset revision and trace `service.version`, and deterministic verification.
- Record HALO Desktop reports as supplemental evidence when they are available; do not require Desktop to promote an otherwise complete investigation.
- Put experimental changes only on `halo/issue-<number>-<stable-semantic-slug>`.
- Treat that branch as `experimental_snapshot_only`; do not create a PR or continue development from it.
- Move the same Project item to `Todo` only after the investigation is complete and a separate implementation agent has a precise handoff.
- Use `Done` when the hypothesis is disproved or no code change is justified.
- Use `Need Human Input` only for a structured external blocker.
