---
name: shea-symphony-human-review
description: Brief a Shea Symphony operator for Human Review after independent review evidence, guide operator-owned UAT and explicitly authorized narrow remediation, persist append-only evidence, and route only after explicit confirmation.
---

# Shea Symphony Human Review

Use this skill after an independent Review Agent pass when an operator must make final acceptance decisions before merge-lane work. Human Review is not a general implementation, independent review, or merging lane. It may perform a narrow UAT remediation in the linked issue workspace when the operator explicitly authorizes fixing a concrete UAT defect in place. An accepted ordinary issue routes to Merging, never directly to Done.

## Bind the review surface

Resolve the active repository, workflow, tracker project, canonical harness checkout, Human Review note template, parent-batch briefing template, linked PR workspace, and supported read/write actions. Never assume paths, repository, workflow file, template, or command.

Use the configured workflow surface for Project state, append-only timeline notes, and guarded routing. Use provider read-only views for ordinary issue and PR content. Human Review notes must never upsert or restructure the Main Agent Workpad.

## Authority and language

- Do not change implementation code except a narrow, mechanical PR freshness repair or an explicitly authorized narrow UAT remediation described below.
- Do not act as Review Agent or Merging Agent, and do not merge.
- Do not mutate Project state before the operator explicitly confirms the decision after the briefing and UAT discussion.
- Treat UAT as operator-owned unless the issue says otherwise.
- Treat any remediation commit as invalidating the prior Review Agent pass. Never approve the changed revision from Human Review without a fresh independent Agent Review.
- A routine native subissue does not enter direct Human Review. Without a recorded Subissue Human Review Exception, explain that a passing child routes from Agent Review to Merging and the parent owns final UAT.
- Match live conversation language to the operator. Durable tracker artifacts, issue bodies, workpads, and PR comments are English; preserve configured state names and decision labels exactly.

## Required reads

Before briefing, inspect:

- issue goal, scope, guardrails, dependencies, and all review checklists;
- Main Workpad and append-only evidence;
- Review Agent pass evidence and unchecked/missing items;
- linked PR identity, base/head, readiness, checks, and merge state; and
- missing evidence, stale assumptions, and blockers.

For a parent with native subissues, also inspect child Project state, child PR merge evidence into the parent integration branch, the parent final PR and Review Agent evidence, and remaining parent-owned UAT.

Summarize decision-relevant facts, not raw JSON.

## Required PR freshness preflight

Before PR-specific UAT, work only from the linked PR/issue worktree, never the canonical main checkout:

1. Refresh upstream references and verify that the PR branch contains latest origin/main.
2. If it is fresh, continue.
3. If behind, attempt only a safe, local, mechanical merge of origin/main.
4. If clean, run focused verification, push the branch, and record the repair in the running decision-note draft.
5. If conflict resolution is clearly mechanical, resolve in that worktree, verify, push, and record it.
6. If the conflict is broad, product-scope, ambiguous, or verification fails, stop before UAT and recommend Request Rework with the smallest finding.

If no safe linked worktree can be found or created from existing evidence, treat it as a UAT blocker and ask for the smallest workspace decision. Provider mergeability is corroborating evidence, not a substitute for the local ancestry check.

## Live review flow

Follow this order:

1. Give an operator-language orientation: issue/PR, purpose, intended outcome, change summary, Review Agent evidence, human-owned UAT, known risks, and possible decisions.
2. Run the automatic freshness preflight.
3. If any preflight work occurred, present a compact post-preflight packet with the updated verification result and a running note draft.
4. Ask for exactly one UAT result: pass, fail, deferred, or a stated blocker.
5. Wait for the operator's result.
6. Draft a Human Review Decision Note in English and show the routing result.
7. Obtain explicit confirmation before writing the note or changing state.

Do not end with a bare pass/fail question after a freshness repair. Keep the operator's decision separate from automatic preflight evidence.

The decision note is an append-only Shea Symphony Human Review Decision timeline comment. It records issue/PR, reviewed evidence, preflight outcome, UAT result, operator decision, residual risk, and proposed target state. Write the note before the guarded state route.

## Operator-authorized UAT remediation

When UAT exposes a concrete implementation defect and the operator explicitly asks to fix it in place, Human Review may repair it without first routing through `Rework` only when the change is narrow, local to the linked PR, and does not revise the issue contract.

1. State the exact defect, intended repair, and affected verification before editing.
2. Work only in the linked issue/PR worktree. Do not modify the canonical main checkout.
3. Keep the patch limited to the UAT defect and its focused regression coverage. If the repair becomes broad, ambiguous, or changes product scope, stop and recommend `Request Rework`.
4. Run focused tests and the issue's required verification.
5. Present the diff, verification, updated PR revision, and remaining UAT to the operator.
6. Mark the prior Review Agent evidence stale. Do not resume the acceptance decision on the changed revision.
7. Draft an append-only Human Review UAT Remediation note and obtain explicit confirmation before writing it or routing the issue to `Agent Review`.

The remediation note records the failing UAT evidence, operator authorization, changed files, verification, PR revision, residual risk, and proposed `Agent Review` target. The route is the final mutation. A fresh Review Agent pass is required before Human Review can continue.

## Routing

After explicit confirmation:

- Approve for Merging: route to Merging.
- Request Rework: route to Rework with actionable evidence.
- Need Human Input: route there with the unresolved question.
- Defer: retain Human Review and append the decision note; do not simulate approval.
- UAT remediated in place: route to Agent Review after the repair, verification, remediation-note draft, and explicit operator confirmation.

For an ordinary native child without an exception, do not create a direct Human Review result; preserve its parent-owned acceptance boundary.

The routing action is the final mutation. Afterwards, only read back the status and run Doctor verification.

## Quality bar

Do not approve when issue scope, review evidence, linked PR, freshness, UAT result, or operator confirmation is missing. Do not replace UAT with a technical test result. Do not treat self-verification after remediation as independent review. Do not overwrite comments or workpads; evidence is append-only.
