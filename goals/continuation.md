# Continuation Goal

This prompt keeps the agentic harness moving until the current quality phase is
done. At each turn, inspect the repository state and decide whether to continue,
pause, or escalate.

## Baseline

- Date: 2026-05-07
- Branch: `main`
- Baseline commit before Phase G: `2fec123f1d630eea1c3b24460e2cd126d2bd49df`
- Open PR count after Phase F: 0
- Completed merge wave: #41, #38, #39, #40, #42, #43, #44, #45, #46, #47, #48

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
- Phase C wiki/reindex API router extraction was merged in #45 with green CI.
- Phase D settings/user-settings API router extraction was merged in #46 with
  green CI.
- Phase E search/chat API router extraction was merged in #47 with green CI.
- Phase F meeting detail API router extraction was merged in #48 with green CI.

## Current Phase: Phase G, System/Recording/Upload Router Boundary

Goal: continue reducing the `api/routes.py` monolith by extracting the next
well-tested API domains into dedicated routers while preserving endpoint
contracts, monkeypatch-compatible helpers, and lazy imports.

Recommended execution order:

1. Phase G1: extract `/api/status`, `/api/system/resources`,
   `/api/dashboard/stats`, and `/api/system/open-audio-folder` into
   `api/routers/system.py`.
2. Phase G2: extract `/api/uploads` and upload filename/path helpers into
   `api/routers/uploads.py`.
3. Phase G3: extract `/api/recording/*` schemas and endpoints into
   `api/routers/recording.py`.
4. Phase G4: preserve compatibility re-exports and existing monkeypatch paths,
   then update docs.

Completion criteria:

- `api/routes.py` no longer owns system, dashboard, upload, or recording
  endpoint implementations.
- `api.routes` keeps compatibility re-exports for tests and external imports
  that still patch helper symbols such as response models, upload helpers, and
  system `sys`/`shutil`/`subprocess` aliases.
- Endpoint paths, response models, dashboard aggregation, upload streaming,
  filename validation, and recording error mapping remain unchanged.
- Targeted route, home dashboard/upload, server, security, recording, and lint
  gates pass locally.
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

Recommended order after Phase G:

1. Continue `api/routes.py` domain router separation with A/B test routes and
   remaining meeting list/summarize-batch ownership decisions.
2. Split `ui/web/style.css` into component-level CSS files with visual/a11y
   gates.
3. Decide how native marker tests should run in CI: required, manual, or
   scheduled.
4. Convert the STT coverage and hallucination plans into an experiment harness
   with reproducible fixtures and metrics.
