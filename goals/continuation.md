# Continuation Goal

This prompt keeps the agentic harness moving until the current quality phase is
done. At each turn, inspect the repository state and decide whether to continue,
pause, or escalate.

## Baseline

- Date: 2026-05-06
- Branch: `main`
- Baseline commit before Phase A: `8243dbfaafaf60620c271ba42094005def710276`
- Open PR count after the cleanup wave: 0
- Completed merge wave: #41, #38, #39, #40

## Completed Workstreams

- Route-specific SPA views were extracted from `ui/web/spa.js`:
  `settings-view.js`, `viewer-view.js`, `chat-view.js`, `wiki-view.js`,
  `ab-test-view.js`, `search-view.js`, and `empty-view.js`.
- Shared frontend boundaries were extracted:
  `api-client.js`, `list-panel.js`, `global-resource-bar.js`,
  `bulk-action-bar.js`, `theme-controller.js`, `mobile-drawer.js`, and
  `shortcut-controller.js`.
- API runtime cleanup reached the first domain-router milestone:
  app-state dependency helpers and the meetings batch router are split from
  `api/routes.py`.
- Runtime gates, docs, model/pipeline safety settings, and CI checks were
  hardened.
- Consensus harness workflow, scope, artifact, assignment, gate, ticket, and
  board support were merged.

## Current Phase: Phase A, Status And Retry UX Alignment

Goal: align project status documents with the merged main branch and preserve the
small viewer UX improvement that distinguishes two materially different recovery
actions:

- `실패한 단계부터 다시 시도`: keeps existing results/progress and requeues from the
  failed point.
- `처음부터 다시 전사`: deletes existing transcript/summary/progress and restarts
  from audio conversion.

Completion criteria:

- `docs/STATUS.md` reflects the #38-#41 merged state and no longer presents
  completed global shell work as a future candidate.
- `goals/continuation.md` names the current phase and next decision points.
- Viewer retry/re-transcribe microcopy has regression coverage.
- Touched frontend JavaScript passes syntax checks.
- Targeted harness/UI checks pass.

## Continue When

- A change can be scoped to docs drift, small UX copy/state clarification, or a
  focused follow-up with direct regression coverage.
- A proposed workstream has agreement from at least two independent auditors, or
  the lead records an evidence-backed tie-break for a low-risk change.
- The verification surface is clear and can run locally without native model
  downloads or user secrets.

## Stop Or Pause When

- The next task requires a broad redesign, product policy decision, or model
  quality experiment.
- Required tests need unavailable local dependencies or gated external assets.
- Public API contracts would change without a migration plan.

## Next Workstream Candidates

Recommended order after Phase A:

1. Continue `api/routes.py` domain router separation by low-coupling domains
   such as STT models, settings, search/chat, wiki, and meeting detail routes.
2. Split `ui/web/style.css` into component-level CSS files with visual/a11y
   gates.
3. Decide how native marker tests should run in CI: required, manual, or
   scheduled.
4. Convert the STT coverage and hallucination plans into an experiment harness
   with reproducible fixtures and metrics.
