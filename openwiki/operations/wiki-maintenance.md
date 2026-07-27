---
type: Runbook
title: Maintaining the Shea Halo OpenWiki
description: Trigger, scope, review, and validation rules for manually updating the Shea Halo code wiki with OpenWiki.
tags: [shea-halo, openwiki, documentation, maintenance]
---

# Maintaining the Shea Halo OpenWiki

OpenWiki updates are manual and review-gated. Source code, tests, committed
configuration and workflow contracts, and the maintained README remain
authoritative. The wiki explains those sources; it does not create behavior,
backlog, or architectural authority.

## When to update

Run a focused update when a merged change alters one of these reader-visible
contracts:

- lifecycle state, re-entry, routing, or recovery;
- persisted evidence, publication, or revision identity;
- tracing transport, trace semantics, HALO Engine, or observation admission;
- security, credentials, runtime containment, or external side effects;
- target configuration or supported development commands;
- the Shea Halo / Shea Symphony ownership boundary.

Do not update merely because a PR opened or an Issue description changed.
Proposed work may be linked when it clarifies a current boundary, but should not
be described as shipped until its implementation and tests are on the
documented branch.

## Update strategy

1. Read `openwiki/INSTRUCTIONS.md` and identify the smallest affected pages.
2. Verify the current Git revision and inspect authoritative source/tests.
3. Run a page-focused OpenWiki update with an explicit scope and exclusions.
4. Review the diff for unsupported claims, duplicated README content, invented
   backlog, private data, and current-versus-proposed confusion.
5. Run one global reconciliation only after several related pages or navigation
   paths changed.
6. Optionally run one no-change update as an idempotence check. Repeated
   meaningful rewrites are a maintenance defect, not a reason to keep
   regenerating.

Prefer a small sequence of focused page updates over repeated full updates.
Generation may inspect relevant repository source and send it through the
configured ChatGPT/OpenAI model; exclude ignored runtime state, local config,
credentials, raw traces, prompts, model/tool payloads, endpoints, host paths,
and HALO Desktop private storage.

## Review gates

Every update should preserve:

- `openwiki/quickstart.md` as the human and coding-agent entrypoint;
- concise directory indexes and valid internal links;
- OKF front matter on substantive pages;
- Mermaid diagrams that pass OpenWiki's parser;
- real source/test anchors rather than inferred implementation;
- GitHub Issues and Projects as the only backlog authority;
- the distinction between committed synthetic trace fixtures and authorized
  external research evidence;
- the distinction between current `main`, experimental snapshots, open PRs,
  and proposed Issues.

Search the generated content for credentials, absolute host paths, raw trace or
prompt material, and private runtime locations before accepting a diff. Run
`git diff --check` plus the repository's normal verification sequence when the
wiki update accompanies code.

## Issue #9 synchronization

On the current documented base, Issue #9's complete research trace bundle is
not shipped. After its implementation is merged and UAT is complete:

1. update `workflows/observability.md`;
2. update `concepts/evidence-model.md` and the glossary;
3. update `architecture/overview.md` and configuration if their contracts
   changed;
4. add a focused trace-bundle page grounded in the merged model, serializer,
   lifecycle attachment, retention rules, and tests;
5. update Quickstart navigation;
6. run one global reconciliation and one no-change idempotence check.

Do not predeclare the bundle from the current observation checkpoint, workpad,
manifest, and HALO artifact structures.

## Automation boundary

No recurring documentation workflow is committed during the bootstrap. Keep
updates manual until focused generation, diff review, link/front-matter/Mermaid
validation, and no-change idempotence are reliable. If automation is introduced
later, it should open a reviewable change rather than publish generated content
directly.
