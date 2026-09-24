# Continuation Goal

This prompt keeps the agentic harness moving until the current quality phase is
done. At each turn, inspect the repository state and decide whether to continue,
pause, or escalate.

## Baseline

- Date: 2026-09-24 (goals refresh — Phase G verified; Current → Phase H)
- Branch: `main`
- Verified HEAD at refresh: `75496c8` (#73); Phase G code-verified at
  `0f6f8b9` (#72)
- Historical baseline before Phase G: `2fec123f1d630eea1c3b24460e2cd126d2bd49df`
- Completed merge wave (docs/STATUS.md 기준): #41, #38, #39, #40, #42, #43, #44,
  #45, #46, #47, #48, #52, #53, #65 … #73
- Note: 로컬 git 이력은 squash 되어 있을 수 있어 단계 완료 판정은
  **코드 증거** 기준이다.

## Completed Workstreams

- Route-specific SPA views were extracted from `ui/web/spa.js`:
  `settings-view.js`, `viewer-view.js`, `chat-view.js`, `wiki-view.js`,
  `ab-test-view.js`, `search-view.js`, and `empty-view.js`.
- Shared frontend boundaries were extracted:
  `api-client.js`, `list-panel.js`, `global-resource-bar.js`,
  `bulk-action-bar.js`, `theme-controller.js`, `mobile-drawer.js`, and
  `shortcut-controller.js`.
- Runtime gates, docs, model/pipeline safety settings, and CI checks were
  hardened. Consensus harness workflow/scope/artifact/assignment/gate/ticket/
  board support merged.
- Phase A status/retry UX alignment (#42).
- Phase B STT model API router extraction (#43) → `api/routers/stt_models.py`.
- Viewer missing-transcript UX (#44).
- Phase C wiki/reindex API router extraction (#45) → `api/routers/wiki.py`,
  `api/routers/reindex.py`.
- Phase D settings/user-settings API router extraction (#46).
- Phase E search/chat API router extraction (#47) → `api/routers/search_chat.py`.
- Phase F meeting detail API router extraction (#48) →
  `api/routers/meeting_detail.py`.
- **Phase G system/recording/upload router boundary — DONE (code-verified)**:
  - G1 `api/routers/system.py`: `GET /status`, `GET /system/resources`,
    `GET /dashboard/stats`, `POST /system/open-audio-folder` + audio-input
    recovery endpoint (`recover_unregistered_audio`).
  - G2 `api/routers/uploads.py`: `POST /uploads`, `_sanitize_upload_filename`,
    `_resolve_unique_upload_path`, `_UPLOAD_MAX_BYTES`,
    `_FILENAME_FORBIDDEN_PATTERN`.
  - G3 `api/routers/recording.py`: `GET /recording/status`,
    `POST /recording/start`, `POST /recording/stop`, `GET /recording/devices`.
  - G4 `api/routes.py` keeps compatibility re-exports (response models,
    `_ACTIVE/_PENDING/_UNTRANSCRIBED_JOB_STATUSES`, `shutil`/`subprocess`/`sys`
    aliases, upload helpers, recording schemas) and `router.include_router(...)`.
    `docs/STATUS.md` records the split.
- Additional routers already split beyond the original Phase G plan (**DONE**):
  `ab_tests.py`, `auto_processing.py`, `setup_readiness.py`,
  `transcription_models.py`, `meetings_batch.py`, `meeting_titles.py`
  (former "A/B test routes" / post-G candidates are done).
- Partial CSS component split already shipped: `bulk-actions.css`,
  `ab-test.css`, `wiki.css`, `recording.css`, `settings.css`, `tokens.css`
  (see `docs/STATUS.md` 알려진 우선 과제).

## Current Phase: Phase H — Residual `api/routes.py` Ownership + CSS Split

Goal: finish the `api/routes.py` monolith reduction so it becomes a pure
aggregator + compatibility re-export module, and continue component CSS
extraction without changing public HTTP contracts.

Residual implementation still owned by `api/routes.py` (verified at goals
refresh):

- `GET /api/meetings` (`get_meetings`, `MeetingsResponse`)
- `POST /api/meetings/summarize-batch` (`summarize_batch`,
  `SummarizeBatchRequest`) — legacy endpoint, no `ui/web` caller found.
- `_validate_meeting_id` (duplicated in `api/routers/meeting_detail.py` and
  `api/routers/reindex.py`)
- `_log_task_exception`

Recommended execution order:

1. **H1**: extract `get_meetings`/`MeetingsResponse` into a meetings list router
   (e.g. `api/routers/meetings.py` or `meetings_list.py`) with re-exports in
   `api.routes`.
2. **H2**: decide `summarize-batch` ownership: move next to `meetings_batch.py`
   and align it with the mutation-coordinator contract
   (`_ensure_completed_meeting_mutation_allowed` like `summarize_meeting`), or
   deprecate it in favor of `POST /api/meetings/batch`. This is a behavior
   decision — escalate if it changes the contract. Pin
   `tests/test_routes.py` legacy summarize-batch validators either way.
3. **H3**: consolidate `_validate_meeting_id` into one shared helper
   (e.g. `api/dependencies.py` or `api/validation.py`) and keep re-exports;
   continue `ui/web/style.css` (~5.9k LOC) split — next candidates per STATUS:
   **viewer**, **command palette**, **layout shell**. Keep visual/a11y gates
   (`pytest -m ui …`).
4. **H4**: update `docs/STATUS.md` and this file; leave unrelated git/harness
   churn to separate PRs.

Completion criteria:

- `api/routes.py` contains no `@router.<method>` endpoint implementations
  (re-exports + `include_router` only), or H2 records an explicit keep-decision.
- `tests/test_routes.py` legacy validator tests
  (`test_legacy_summarize_batch_validator*`) and meeting list tests stay green.
- Endpoint paths, response models, and error mapping unchanged unless H2
  explicitly records a migration; monkeypatch paths preserved.
- CSS split PRs include visual or a11y coverage for touched surfaces.
- PR CI green before merge.

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
- Public API contracts would change without a migration plan (H2 risk).
- The backend PR cannot stay isolated from unrelated frontend or harness work.

## Next Workstream Candidates (after Phase H)

1. Memorable Wiki follow-ups (see `goals/memorable-wiki.md`): G1 hybrid filter
   bypass fix, `/api/wiki/search` per-request `rebuild()` removal, C4 golden set.
2. Native marker tests remain manual/scheduled diagnostic gates
   (`.github/workflows/ci.yml` `native-gate` on `workflow_dispatch` /
   `schedule`) — document operator runbook gaps rather than re-litigating.
3. Debt markers (`type: ignore`, broad `BLE001`, empty `pass`) per STATUS
   "알려진 우선 과제".
4. STT metric harness with real reference-interval fixtures → `docs/BENCHMARK.md`
   (`core/stt_quality_metrics.py`, `scripts/evaluate_stt_quality.py`).
