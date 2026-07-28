# OpenSourceIRL Demo Runbook — 31 July 2026

## Demo objective

Show a credible, evidence-bound Shea Halo research handoff using FailureReport.
The audience should see that Shea Halo is a complete GitHub-native research
agent: it reads a research problem, analyzes real local trace evidence, avoids
duplicating framework-native instrumentation, identifies a concrete semantic
coverage gap, produces an implementation-ready handoff, and routes the same
GitHub Project item from `Halo Research` to `Todo`.

The demo is deliberately controlled. It is not a claim that an arbitrary new
repository can complete a long, unsupervised study live on stage.

## Audience takeaway

By the end of the demo, the audience should understand:

1. Shea Halo studies the behavior of agent loops from traces, not only source
   code or model opinion.
2. It prefers framework-native Catalyst/OpenTelemetry instrumentation and
   rejects redundant manual spans.
3. Every terminal recommendation is tied to an exact Git revision, a selected
   trace dataset, HALO Engine analysis, runtime action receipts, and
   deterministic verification.
4. The result is a human-readable engineering handoff in GitHub.
5. `Todo` means research is complete and Shea Symphony may begin implementation;
   a Halo experimental snapshot is never the implementation PR branch.

## Demo boundary

### In scope

- One fresh FailureReport research Issue pinned to the then-current
  FailureReport `main`.
- One freshly generated, finalized, sanitized real FailureReport research trace
  bundle produced by the golden run through the completed Issue #9 pipeline,
  then frozen for rehearsal and presentation.
- Framework-native Eve/Catalyst coverage analysis.
- A concrete handoff for an uncovered FailureReport semantic boundary.
- A visible `Halo Research` → `Todo` transition.
- Local or self-hosted trace storage and analysis.

### Out of scope

- Selecting an arbitrary repository from the audience.
- Waiting through the entire research lifecycle on stage.
- Demonstrating every retry, recovery, or workpad checkpoint.
- Implementing or merging the FailureReport change during the presentation.
- Treating HALO Desktop as required infrastructure.
- Claiming production stability beyond the current Alpha status.

## Target demo story

This is the target story, not a pre-authorized conclusion for the research
agent. The fresh Issue must ask Halo to verify native coverage and identify any
remaining semantic gap. If exact-revision evidence does not support the expected
Codex App Server lifecycle finding, preserve the evidence-bound result and use
the conditional demo rather than forcing this narrative.

FailureReport asks Shea Halo to improve trace coverage for its Root/Codex
diagnostic loop.

Halo determines that Eve's native instrumentation already covers Root/Eve
turns, workflow lineage, delegated agent invocations, model calls, and Eve tool
execution. It therefore refuses to add duplicate manual spans around those
operations.

Halo then identifies a real semantic gap: the Codex App Server process lifecycle
is not represented clearly enough. It hands off a narrowly scoped change around
process creation, startup, readiness, stop, disposal, and failure, with
before/after trace acceptance criteria proving that native Eve spans and token
rollups are not duplicated.

## Definition of demo-ready

The demo is ready only when all of the following are true:

- [ ] The machine used for the demo is running a Shea Halo build containing the
      merged Issue #9 trace-bundle implementation and merged Issue #13
      human-readable handoff implementation.
- [ ] The human-readable Research Summary and Proposed Handoff are visible
      without opening the structured workpad JSON.
- [ ] The demo uses a new FailureReport research Issue; Issue #26 is linked as
      historical evidence and is not resumed.
- [ ] The new Issue is pinned to the current FailureReport base revision.
- [ ] The selected trace bundle has a bounded manifest, exact source revision,
      canonical trace identities, and no raw secrets or host paths in public
      evidence.
- [ ] The selected target-native traces were generated after the golden
      candidate revision was fixed; historical FailureReport #26 traces are
      context only and are not presented as promotion evidence for the fresh
      Issue.
- [ ] Candidate trace validation, HALO dataset validation, revision validation,
      `service.version` validation, and repository verification all pass.
- [ ] The Project item reaches `Todo`.
- [ ] The handoff names a bounded implementation surface and explicitly excludes
      duplicate Root, Eve turn, model, delegated-agent, and Eve-tool spans.
- [ ] A successful real run has been rehearsed from the same build and
      configuration used for the presentation.
- [ ] A screen recording and static screenshots of that successful run are
      available offline.

## Critical path before Friday

Only two tracker items are on the critical path:

1. Complete Shea Halo
   [#13](https://github.com/Alive24/shea-halo/issues/13), merge it, and freeze
   the resulting Shea Halo `main` revision.
2. Create one fresh FailureReport `Halo Research` Issue after #13 is merged and
   run it until the same Project item reaches `Todo`.

FailureReport #26 remains historical evidence. Shea Halo #7 workpad coalescing
remains outside the critical path unless the golden run demonstrates a concrete
revision storm that prevents completion.

### Golden-run experiment preflight

Do not create the fresh Research Issue until the ignored FailureReport
`.shea/halo.local.toml` runs a real target-owned candidate experiment.

The pre-demo configuration still stages the retained #26 analysis JSONL with a
`cp` action. That was valid for re-entry against the old exact candidate, but it
cannot prove a fresh Issue or current candidate revision. Remove that action
from the frozen demo configuration.

The replacement experiment must:

- run from the managed candidate worktree after its revision is fixed;
- receive the candidate SHA through `CATALYST_SERVICE_VERSION`;
- use an operator-approved fixture Issue and immutable target revision;
- exercise the real Eve Root → diagnostic session → Codex path;
- use Eve/Catalyst-native tracing and a loopback-only OTLP path;
- atomically finalize new canonical JSONL while preserving trace IDs, span IDs,
  parent IDs, timestamps, resource identity, and exact `service.version`;
- exit nonzero if the real flow, JSONL finalization, or required trace
  invariants fail;
- leave raw traces in ignored local storage and emit only bounded receipts;
- prove the selected observation is new or changed after the candidate
  validation baseline.

Run this experiment once against a disposable fixture before creating the
golden Issue. If it cannot produce fresh exact-revision JSONL, the end-to-end
demo is not ready even when historical #26 traces remain available.

### Post-#13 execution sequence

After #13 is merged:

1. Read back #13 as `Done`, record the merge SHA, and update the demo worker to
   that exact clean `main`.
2. Confirm the worker build contains both the #9 research-bundle lifecycle and
   #13 visible decision/handoff projection.
3. Replace the historical-trace `cp` action in the ignored FailureReport Halo
   configuration with the real candidate experiment defined above.
4. Run that experiment against a disposable operator-approved fixture. Stop if
   its JSONL is absent, incomplete, stale, mixed-revision, or not accepted by
   HALO Engine.
5. Refresh the prepared golden Research Issue contract against current
   FailureReport `main`, then create it and place it in `Halo Research`.
6. Run Shea Halo to a deterministic terminal disposition. Do not manually move
   the Issue to make the demo story succeed.
7. For `ready_for_todo`, read back the visible Research Summary, Evidence Gates,
   Proposed Handoff, final Project `Todo` status, and complete local bundle.
   For `no_change`, incomplete evidence, or a verified blocker, preserve that
   result and switch the presentation plan to Conditional Go.
8. Freeze all identities and presentation artifacts before the final
   rehearsal; do not reuse the golden Issue for another technical run.

### Tuesday, 28 July — freeze the scope

- Confirm Issue #9 is merged into the `main` revision used by the worker.
- Complete Issue #13 through review, UAT, merge, and `Done`.
- Define and verify the human-readable Research Summary / Proposed Handoff
  presentation.
- Keep workpad coalescing, provider retry policy, Chat Completions compatibility,
  and broader automation outside the demo critical path unless they directly
  break the selected run.
- Select the exact FailureReport flow and trace bundle used in the demo.
- Verify that the bundle is safe to retain locally and safe to summarize
  publicly.

### Wednesday, 29 July — produce the golden run

- Update the demo machine to the frozen Shea Halo revision containing #9 and
  #13.
- Create a new FailureReport Issue with the focused tracing-coverage objective.
- Add it to the configured FailureReport Project in `Halo Research`.
- Run Shea Halo using the pinned model/API mode. Let the exact-revision
  candidate experiment produce the target-native traces and let the completed
  #9 pipeline finalize this run's new research bundle.
- Resolve only defects that block:
  - exact trace validation;
  - human-readable handoff rendering;
  - deterministic routing to `Todo`.
- Record the successful Issue URL, base revision, candidate revision, bundle
  identity, HALO report digest, and final Project status.
- Record the demo-relevant #9 UAT evidence from the same run: finalized local
  bundle, accepted HALO Engine dataset, identity preservation, and absence of
  raw trace content from public evidence. HALO Desktop remains optional.

### Thursday, 30 July — rehearse and freeze

- Rehearse the presentation twice.
- Use a disposable Issue or controlled fixture for the second technical
  rehearsal; do not mutate the successful golden Issue.
- Confirm the talk fits within the allocated time without waiting for a model
  response.
- Capture:
  - a full screen recording;
  - screenshots of the Issue objective, Research Summary, evidence gates,
    Proposed Handoff, and `Todo` status;
  - a sanitized trace-bundle manifest;
  - the experimental snapshot comparison.
- Freeze the worker revision, FailureReport revision, configuration, model/API
  mode, and presentation tabs.
- Do not accept non-critical dependency upgrades after the final rehearsal.

### Friday, 31 July — preflight

Complete this at least 30 minutes before presenting:

- [ ] Network and GitHub access work.
- [ ] Required credentials are loaded without being visible in shell history,
      slides, screenshots, or logs.
- [ ] The local/self-hosted collector is available if it is part of the visual
      presentation.
- [ ] The successful golden Issue and backup recording open correctly.
- [ ] Browser tabs are ordered and notifications are disabled.
- [ ] Terminal history and filesystem paths are not visible.
- [ ] The demo branch, bundle, and report identities match the rehearsal notes.
- [ ] The fallback recording is available without network access.

## Recommended presentation format

Use the completed golden run as the primary demonstration. If desired, start a
separate live run before the talk, but do not make the talk depend on it
finishing.

### 0:00–0:45 — The problem

Open the fresh FailureReport Issue.

Suggested narration:

> FailureReport is itself an agent system. We want to know where its Root/Codex
> diagnostic loop is observable and where it is not. This is a research
> question, not a pre-approved request to add spans everywhere.

Show:

- the research objective;
- the current FailureReport revision;
- the `Halo Research` Project status.

### 0:45–2:00 — The local evidence pipeline

Show the bounded trace-bundle manifest, not raw trace payloads.

Explain:

- FailureReport exports standard OTLP for live observation;
- the same run writes canonical JSONL locally;
- Shea Halo selects exact trace identities;
- HALO Engine analyzes that exact local dataset;
- the durable public evidence contains digests, counts, revisions, and safe
  semantic receipts rather than private trace contents.

Suggested narration:

> Everything needed for the research can remain local or self-hosted. GitHub is
> the collaboration and state surface, not the trace database.

### 2:00–3:30 — What Halo discovered

Open the human-readable Research Summary.

Highlight:

- native Eve tracing already covers Root/Eve turns;
- delegated agents, LLM calls, tools, and workflow lineage are already visible;
- adding manual spans around them would create duplication and unreliable token
  rollups;
- lifecycle semantics remain partially invisible.

Suggested narration:

> The useful result is not “add more telemetry.” Halo first proves where adding
> telemetry would be wrong.

### 3:30–5:30 — The handoff

Open Proposed Handoff.

Show the recommended Codex App Server lifecycle span:

- creation/start/readiness/stop/disposal;
- parented under the active Eve context;
- bounded, non-secret attributes;
- exception recording and OpenTelemetry error status;
- no extra `invoke_agent`, `chat`, or `execute_tool` spans.

Show its acceptance criteria:

- exactly one new lifecycle span for the relevant lifecycle;
- no disconnected span;
- no change to native Eve span counts or token rollups;
- canonical JSONL still valid;
- build, check, test, format, and real Root→Codex smoke verification pass.

### 5:30–6:45 — Why the result is trustworthy

Show the compact deterministic gate summary:

- candidate source revision;
- selected trace count and digest;
- HALO report digest;
- dataset/revision/`service.version` verification;
- repository verification results;
- source-clean result.

Do not open the full structured JSON unless answering a technical question.

### 6:45–7:30 — Research becomes implementation work

Show the same Project item in `Todo`.

Suggested narration:

> Halo has finished research. It does not turn its experimental branch into a
> production PR. The same Issue now moves to Shea Symphony, which implements the
> handoff in a separate worktree and branch.

If time permits, show the experimental snapshot as selectively reviewable
evidence, while explicitly stating that it is not a PR branch.

### 7:30–8:00 — Close

Suggested closing:

> Shea Halo is a research agent for agent systems. It combines source, real
> traces, local experiments, and exact-revision validation to decide what should
> change—and just as importantly, what should not.

## Required demo artifacts

Fill these before the final rehearsal:

| Artifact | Value |
|---|---|
| Shea Halo revision | `<fill before demo>` |
| FailureReport base revision | `<fill before demo>` |
| FailureReport research Issue | `<fill before demo>` |
| Experimental snapshot revision | `<fill before demo>` |
| Trace-bundle identity | `<fill before demo>` |
| HALO report digest | `<fill before demo>` |
| Final Project status | `Todo` |
| Backup recording | `<local offline location>` |

Do not put credentials, endpoint tokens, raw prompts, raw model/tool payloads,
private trace attributes, or host-specific absolute paths in this table.

## Failure and fallback plan

### Live worker or model stalls

Stop waiting after the prepared time budget. Switch immediately to the
completed golden Issue and explain that the full run was performed from the
same frozen build and evidence bundle.

Do not debug model retries on stage.

### GitHub or network is unavailable

Use the offline screen recording and static screenshots. Preserve the same
story order: objective → trace manifest → research finding → handoff → evidence
gates → `Todo`.

### HALO Desktop is unavailable

Continue with the local canonical JSONL bundle and HALO Engine evidence.
Desktop is an optional observation surface and must not become a hidden
requirement.

### The live run remains in `Halo Research`

Do not manually move it to `Todo` during the presentation. Use the completed
golden Issue and state that the live run has not yet satisfied its deterministic
gates.

### Sensitive material appears

Immediately leave the screen and switch to the recording. Do not attempt to
redact or explain the material while it remains visible.

## Go / no-go decision

Make the final decision after Thursday's second rehearsal.

### Go

Proceed with the end-to-end story when:

- the golden Issue reached `Todo` without manual status intervention;
- the handoff is readable without opening structured JSON;
- all identities and gate summaries agree;
- no sensitive material is exposed;
- the offline fallback is ready.

### Conditional go

Present a product and research walkthrough without claiming a completed
end-to-end handoff if the analysis is sound but routing or presentation remains
unreliable.

### No-go for the end-to-end claim

Do not claim autonomous Research → Todo completion if:

- a human manually changed the status;
- evidence belongs to a different source revision;
- the trace bundle or HALO report cannot be identified;
- the handoff exists only as an inferred interpretation of raw workpad state;
- the result depends on private HALO Desktop storage or unavailable remote
  services.

## Deferred work after the demo

- Coalesce no-op workpad revisions and keep transient worker events local.
- Add explicit convergence limits and a fresh-research lifecycle for base
  revision drift.
- Coordinate provider cooldown handling across investigator, synthesis, and
  HALO Engine calls.
- Validate Chat Completions tool-turn replay where that API mode is required.
- Automate reviewed OpenWiki refreshes after the workflow is stable.
