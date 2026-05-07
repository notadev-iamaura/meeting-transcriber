# Continuation Goal

This prompt keeps the agentic harness moving until the current quality phase is
done. At each turn, inspect the repository state and decide whether to continue,
pause, or escalate.

## Baseline

- Date: 2026-05-07
- Branch: `main`
- Baseline commit before Phase C: `7c234cecf80064ae6cffc131aded735c044ad8a6`
- Open PR count after Phase B: 0
- Completed merge wave: #41, #38, #39, #40, #42, #43, #44

## Completed Workstreams

- Route-specific SPA views were extracted from `ui/web/spa.js`:
  `settings-view.js`, `viewer-view.js`, `chat-view.js`, `wiki-view.js`,
  `ab-test-view.js`, `search-view.js`, and `empty-view.js`.
- Shared frontend boundaries were extracted:
  `api-client.js`, `list-panel.js`, `global-resource-bar.js`,
  `bulk-action-bar.js`, `theme-controller.js`, `mobile-drawer.js`, and
  `shortcut-controller.js`.
- API runtime cleanup reached the first domain-router milestone:
  app-state dependency helpers, the meetings batch router, shared config.yaml
  helpers, and the STT model router are split from `api/routes.py`.
- Runtime gates, docs, model/pipeline safety settings, and CI checks were
  hardened.
- Consensus harness workflow, scope, artifact, assignment, gate, ticket, and
  board support were merged.
- Phase A status/retry UX alignment was merged in #42.
- Phase B STT model API router extraction was merged in #43 with green CI.
- The pre-existing viewer missing-transcript UX change was completed, verified,
  and merged separately in #44 before backend Phase C work began.

## Current Phase: Phase C, Wiki/Reindex Router Boundary

Goal: continue reducing the `api/routes.py` monolith by extracting the next
well-tested API domains into dedicated routers while preserving endpoint
contracts, monkeypatch-compatible helpers, and lazy imports.

Recommended execution order:

1. Preflight: complete and merge the existing viewer missing-transcript UX
   change separately before touching backend router files.
2. Phase C1: extract wiki page/search/backfill endpoints from `api/routes.py`
   into `api/routers/wiki.py`.
3. Phase C2: extract reindex endpoints and helpers into `api/routers/reindex.py`.
4. Phase C3: update status docs and route-boundary tests only after the code
   extraction gates pass.

Completion criteria:

- `api/routes.py` no longer owns wiki and reindex endpoint implementations.
- `api.routes` keeps compatibility re-exports for tests and external imports
  that still patch helper symbols.
- Wiki/reindex endpoint paths, response models, lazy import behavior, and
  app-state access semantics remain unchanged.
- Targeted wiki, reindex, route, and lint gates pass locally.
- PR CI is green before merge.

## Continue When

- A change can be scoped to a single route domain or a directly coupled router
  pair with clear test coverage.
- A proposed workstream has agreement from at least two independent auditors, or
  the lead records an evidence-backed tie-break for a low-risk change.
- The verification surface is clear and can run locally without native model
  downloads or user secrets.

## Stop Or Pause When

- The next task requires a broad redesign, product policy decision, or model
  quality experiment.
- Required tests need unavailable local dependencies or gated external assets.
- Public API contracts would change without a migration plan.
- The backend PR cannot stay isolated from unrelated frontend or harness work.

## Next Workstream Candidates

Recommended order after Phase C:

1. Continue `api/routes.py` domain router separation with settings/prompts/
   vocabulary, then search/chat, then meeting detail routes.
2. Split `ui/web/style.css` into component-level CSS files with visual/a11y
   gates.
3. Decide how native marker tests should run in CI: required, manual, or
   scheduled.
4. Convert the STT coverage and hallucination plans into an experiment harness
   with reproducible fixtures and metrics.
