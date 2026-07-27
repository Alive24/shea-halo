---
type: Runbook
title: Shea Halo troubleshooting runbook
description: Symptom-led, evidence-preserving diagnosis for worker leases, re-entry, Project transitions, worktrees, observations, HALO Desktop, and exact-revision validation.
tags: [shea-halo, operations, troubleshooting, recovery]
---

# Shea Halo troubleshooting runbook

Begin with durable authority: the Issue identity, current Project status,
trusted workpad head, configured target identity, and persisted Git revisions.
Local runtime output is diagnostic material, not authority. Never repair a
failure by editing workpad comments, force-moving a branch, deleting a managed
worktree, or weakening an identity check.

## Fast triage

| Symptom | Likely meaning | Check first | Safe response |
|---|---|---|---|
| Worker reports `worker_lock_conflict` | Another local process owns the per-target advisory lease. | Identify the other worker process; file presence alone does not prove ownership. | Stop or wait for the owning process. Do not delete `worker.lock` or mutate GitHub status. |
| Item remains in `Halo Research` | It may be incomplete, unchanged, retryable, or waiting for evidence rather than failed. | Workpad outcome, residual risks, input fingerprint, observation delta, and last sanitized event type. | Supply new authoritative input/evidence or fix the owning capability. Do not force it to `Todo`. |
| Item does not re-enter after a change | The effective fingerprint may not have changed, or the item may no longer be in the configured research state. | Issue discussion, target/config revision, observation inventory, runtime source, environment-presence receipt, daily guide refresh, and Project status. | Change the actual authoritative input; do not edit a trusted workpad record merely to retrigger. |
| Project status changed but workpad says transition is pending | Status write succeeded and the final append failed, or a downstream workflow advanced the item. | Current Project status and pending transition target. | Run the worker so `_reconcile_terminal_transition` can record applied or superseded state without moving backward. |
| Managed worktree is rejected | Source identity, branch, base, remote, snapshot, cleanliness, or containment differs from persisted state. | Workpad `base_revision`, snapshot SHA, origin identity, current/remote heads, and unexpected tracked/untracked/ignored paths. | Let `WorkspaceManager` recover only states it can prove. Preserve the mismatch for diagnosis; do not reset or clean manually. |
| Publication stops before push | The intended snapshot manifest or repository authority failed validation. | Publication intent, changed paths, sensitive/opaque content, Git attributes, parent revision, origin and PR history. | Correct the source/configuration or rerun from a clean attempt. Never bypass the manifest or sensitive-file checks. |
| Canonical trace is not discovered | It is outside the controlled roots/globs, unstable, oversized, malformed, or has an unsafe label. | Committed `trace_globs`, target/worktree location, file stability, size/count bounds, and canonical JSONL shape. | Fix target instrumentation/output placement. Do not copy traces into a different authority boundary just to satisfy discovery. |
| Trace exists but validation remains incomplete | Candidate selection, topology, dataset revision, `service.version`, HALO analysis, or deterministic verification is incomplete. | `TraceValidation` flags, candidate-only delta, trace hierarchy, service-version coverage, and HALO report digest. | Rerun the post-publication experiment at the exact candidate revision; exclude setup and verification-only traces. |
| Semantic projection fails | Alias values, topology, attribution, token pairs, or rollups conflict or are ambiguous. | Sanitized `SemanticProjectionError` evidence and a minimal synthetic reproduction. | Correct instrumentation or normalization with tests. Do not choose one conflicting raw value heuristically. |
| HALO Desktop is unreachable | Only an explicitly configured external preflight failed. | Preflight classification and status class, then operator-managed Desktop/collector health outside Halo. | Restore the public endpoint or disable the optional preflight if Desktop is intentionally not required. Canonical JSONL remains the promotion evidence. |
| HALO analysis fails | The selected dataset, local engine environment, or safe persistence boundary failed. | Dataset manifest identities, engine version, sanitized exception, and owning `test_halo_engine.py` scenario. | Reproduce with synthetic traces; do not publish raw intermediate indexes/events or private traces. |
| `RuntimeFilesystemError` occurs | A managed path is ambiguous, escaping, linked, raced, or has an unsafe tree entry. | Target `.shea` layout, committed path configuration, symlink/hard-link state, and ownership. | Fix layout/configuration and use the guarded runtime API. Do not replace it with unrestricted file operations. |

## Diagnose an item that stays in research

1. Confirm the Project item still identifies the expected repository and Issue
   and remains in the configured research state (`src/shea_halo/github.py`).
2. Validate the trusted workpad chain. Ordinary comments can change the next
   input, but cannot replace durable state (`src/shea_halo/workpad.py`).
3. Determine whether the last result is `continue_research`, an unverified
   blocker, a validation failure, or an unchanged incomplete fingerprint
   (`src/shea_halo/service.py`).
4. If source changed, compare the persisted base and snapshot identities rather
   than the current filesystem alone.
5. If runtime evidence is required, follow the candidate-only chain:
   post-setup baseline → runtime experiment → frozen delta → HALO analysis →
   deterministic verification.
6. Reproduce the narrowest owning contract in `tests/test_service.py`,
   `tests/test_workspace.py`, `tests/test_observation.py`, or
   `tests/test_halo_engine.py`.

## Recovery invariants

- An unpublished attempt may be discarded only after its managed path, base,
  branch, remote, and persisted snapshot expectations are validated.
- Published snapshots are preserved as evidence. Validation recovery restores
  the exact snapshot rather than reconstructing it from a dirty worktree.
- A terminal status transition is monotonic: reconciliation may complete or
  record supersession, never pull a downstream item backward.
- HALO Desktop is not process-owned by Shea Halo. The worker does not start,
  stop, discover, enumerate, configure, or read its private storage.
- Runtime artifacts and ignored worktrees can carry recovery meaning. Use the
  owning cleanup surface; manual deletion is not normal troubleshooting.

## Safe evidence handling

Keep raw traces, prompts, model output, tool arguments/results, command output,
credentials, endpoints, host paths, and private workpad payloads local. Public
diagnosis should use logical labels, digests, bounded counts, status classes,
sanitized exception categories, and synthetic fixtures. See
[Security and trust boundaries](/openwiki/architecture/security.md) and
[Durable evidence and provenance model](/openwiki/concepts/evidence-model.md).

## Test ownership

| Failure boundary | Primary tests |
|---|---|
| Lease and process exit | `tests/test_locking.py`, `tests/test_main.py` |
| Workpad chain and persisted lifecycle model | `tests/test_workpad.py`, `tests/test_service.py` |
| Re-entry, routing, transition reconciliation, and candidate gates | `tests/test_service.py` |
| Worktree restoration, intent-first publication, and remote authority | `tests/test_workspace.py` |
| Observation discovery, stable reads, deltas, and cleanup | `tests/test_observation.py` |
| Trace semantics and attribution | `tests/test_trace_semantics.py` |
| HALO dataset and durable sanitized artifacts | `tests/test_halo_engine.py` |
| Runtime path containment and safe deletion | `tests/test_runtime_fs.py` |
