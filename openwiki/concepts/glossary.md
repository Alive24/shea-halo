---
type: Reference
title: Shea Halo glossary
description: Definitions for the lifecycle, evidence, observability, and Shea Symphony terms used in the Shea Halo codebase.
tags: [shea-halo, glossary, lifecycle, observability]
---

# Shea Halo glossary

These definitions describe current repository behavior. Follow the linked
concept pages for the complete contracts.

| Term | Meaning |
|---|---|
| **Halo Research** | The configured GitHub Project status in which Shea Halo may start or resume investigation. |
| **Shea Halo** | The research worker. It observes, experiments, validates evidence, and routes the same Project item; it does not own production implementation. |
| **Shea Symphony** | The separate implementation, independent review, human-review, and merge workflow that receives a sufficiently proven item through `Todo`. |
| **workpad** | The trusted, append-only chain of structured GitHub Issue comments that carries recoverable Halo state. |
| **workpad head** | The one latest entry in a validated linear workpad history. |
| **ordinary discussion** | Issue comments that may inform research but are not trusted state, even if they imitate the workpad marker. |
| **base revision** | The persisted source commit from which a Halo experiment attempt is derived. |
| **publication intent** | A pre-side-effect receipt containing the intended parent revision and bounded source path manifest. |
| **experimental snapshot** | An exact published `halo/issue-*` Git revision used as research evidence. Its reuse policy forbids continuing development or opening a PR from it. |
| **candidate revision** | The exact experimental snapshot SHA to which candidate runtime evidence and validation must be bound. |
| **observation checkpoint** | A deterministic, path-free inventory of bounded trace and Desktop-report observations under controlled roots. |
| **observation delta** | New or changed observations between two checkpoints. Candidate evidence uses the experiment-only delta, not all historical observations. |
| **canonical JSONL** | The required one-span-per-line local trace representation validated against HALO Engine's public span contract. |
| **OTLP** | The standard live telemetry transport used to send the same instrumented run to HALO Desktop or another compatible collector. |
| **Catalyst** | The preferred framework-native tracing SDK/integration when supported. It exports standard OpenTelemetry/OpenInference-shaped data rather than defining Shea-specific trace storage. |
| **HALO Desktop** | An optional operator-managed local observation surface. Shea Halo uses only its documented public health/ingest boundary and optional exported reports. |
| **HALO Engine** | The open-source local analysis engine that receives an exact selected trace dataset and produces bounded analysis artifacts. |
| **semantic receipt** | A deterministic projection of trace provenance, agent attribution, and token contributions; conflicting or ambiguous semantics fail closed. |
| **trace dataset manifest** | The exact trace file digests, harness revision, semantic receipts, and receipt-derived totals supplied to HALO Engine. |
| **trace validation** | The mechanical receipt proving candidate hierarchy, selected HALO dataset, report digest, `service.version`, and candidate revision alignment. |
| **dual-output contract** | A real tracing experiment must both deliver live OTLP and atomically write canonical JSONL for promotion evidence. |
| **external blocker** | A verified credential, network, permission, missing-input, or service-availability condition that needs action outside the research loop. |
| **pending transition** | Durable intent to change the Project status, written before the external mutation so partial failure can be reconciled. |
| **re-entry** | Resuming an existing research item from its validated workpad state after new inputs, evidence, runtime capability, or refresh conditions appear. |
| **fail closed** | Stop, retain research state, or route through a verified blocker when identity, completeness, containment, or provenance cannot be proven. |
| **complete research trace bundle** | The proposed Issue #9 aggregate for a whole research run. It is not part of the documented current-main behavior. |

See [Durable evidence and provenance model](/openwiki/concepts/evidence-model.md),
[Halo Research lifecycle](/openwiki/workflows/research-lifecycle.md), and
[Observability and trace evidence](/openwiki/workflows/observability.md).
