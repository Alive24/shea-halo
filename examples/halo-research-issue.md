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
- Resolve tracing changes against the current official Inference catalog rather than remembered setup code.
- Prove experimental changes with a new candidate trace hierarchy, HALO Desktop report, post-experiment HALO Engine analysis, and deterministic verification.
- Put experimental changes only on `halo/issue-<number>-<stable-semantic-slug>`.
- Treat that branch as `experimental_snapshot_only`; do not create a PR or continue development from it.
- Move the same Project item to `Todo` only after the investigation is complete and a separate implementation agent has a precise handoff.
- Use `Done` when the hypothesis is disproved or no code change is justified.
- Use `Need Human Input` only for a structured external blocker.
