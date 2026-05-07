# Phase C API Router Boundary Plan

Date: 2026-05-07
Baseline: `main` at `7c234cecf80064ae6cffc131aded735c044ad8a6`

## Objective

Reduce the remaining `api/routes.py` monolith by moving the next well-covered
domains into dedicated routers without changing public API behavior.

Phase C focuses on two adjacent backend domains:

1. Wiki page/search/backfill API
2. Reindex status/single/all API

## Current State

- `api/routes.py`: 5,965 lines after Phase B.
- Already split:
  - `api/dependencies.py`
  - `api/config_yaml.py`
  - `api/routers/meetings_batch.py`
  - `api/routers/stt_models.py`
- Open PR count: 0.
- Phase B PR #43 was merged with green CI.
- Viewer missing-transcript UX cleanup was completed separately in #44 before
  Phase C started.

## Decision

Proceed with Phase C from clean `main` after the viewer UX cleanup is merged.
The backend router PR must remain isolated from frontend-boundary work.

Recommended action:

1. Create `codex/phase-c-wiki-reindex-routers` from clean `main`.
2. Extract wiki routes first, then reindex routes.
3. Update only router-boundary tests and status/goal documentation.

## Work Breakdown

### C0: Preflight

Scope:

- Confirm no open PRs.
- Confirm `main` equals `origin/main`.
- Confirm no unrelated frontend or harness changes remain in scope.
- Record baseline commit.

Gate:

```bash
git status --short --branch
gh pr list --state open --json number,title,url
```

### C1: Wiki Router Extraction

Target files:

- Add `api/routers/wiki.py`
- Update `api/routes.py`
- Update targeted wiki route tests only where monkeypatch paths move

Move from `api/routes.py`:

- Wiki response models
- Wiki path/page/search helpers
- `/api/wiki/pages`
- `/api/wiki/health`
- `/api/wiki/pages/{page_type}/{slug:path}`
- `/api/wiki/search`
- `/api/wiki/backfill`
- `/api/wiki/backfill/{job_id}`
- `/api/wiki/backfill/{job_id}/cancel`
- `_wiki_backfill_jobs` and `_wiki_backfill_lock`

Compatibility:

- Keep `api.routes` re-exports for moved schemas/helpers/functions.
- Preserve lazy imports of `core.wiki.*` and `scripts.backfill_wiki`.
- Preserve disabled-wiki behavior exactly.

Targeted gates:

```bash
ruff check api/routes.py api/routers/wiki.py tests/wiki/test_routes.py tests/wiki/test_routes_phase2.py tests/wiki/test_routes_backfill.py
ruff format --check api/routes.py api/routers/wiki.py tests/wiki/test_routes.py tests/wiki/test_routes_phase2.py tests/wiki/test_routes_backfill.py
.venv/bin/python -m pytest tests/wiki/test_routes.py tests/wiki/test_routes_phase2.py tests/wiki/test_routes_backfill.py -q
.venv/bin/python -m pytest tests/wiki/test_rag_unchanged.py -q
```

### C2: Reindex Router Extraction

Target files:

- Add `api/routers/reindex.py`
- Update `api/routes.py`
- Update `tests/test_routes_reindex.py` monkeypatch paths

Move from `api/routes.py`:

- Reindex response models
- Chroma collection/status helpers
- `_reindex_meeting`
- `_start_reindex_all`
- `/api/reindex/status`
- `/api/meetings/{meeting_id}/reindex`
- `/api/reindex/all`

Compatibility:

- Keep `api.routes._reindex_meeting` and `api.routes._start_reindex_all` aliases
  until tests and external callers no longer rely on the old module path.
- Preserve app-state `reindex_lock_busy` behavior.
- Preserve WebSocket `reindex_progress` broadcast payloads.

Targeted gates:

```bash
ruff check api/routes.py api/routers/reindex.py tests/test_routes_reindex.py
ruff format --check api/routes.py api/routers/reindex.py tests/test_routes_reindex.py
.venv/bin/python -m pytest tests/test_routes_reindex.py -q
.venv/bin/python -m pytest tests/test_routes.py -q
```

### C3: Integration And PR

Final local gates:

```bash
ruff check api/routes.py api/routers/wiki.py api/routers/reindex.py
ruff format --check api/routes.py api/routers/wiki.py api/routers/reindex.py
.venv/bin/python -m pytest tests/wiki/test_routes.py tests/wiki/test_routes_phase2.py tests/wiki/test_routes_backfill.py tests/wiki/test_rag_unchanged.py tests/test_routes_reindex.py tests/test_routes.py -q
```

Observed outcome:

- `api/routes.py`: reduced from 5,965 lines after Phase B to 4,629 lines after
  extracting wiki and reindex ownership.
- `api/routers/wiki.py`: 943 lines.
- `api/routers/reindex.py`: 510 lines.
- Local combined gate also included `tests/test_server.py` to verify router
  registration through the FastAPI app factory.

PR expectations:

- One backend-only PR.
- No frontend-boundary/viewer change in the PR.
- PR body lists moved symbols, compatibility aliases, and exact verification.
- Merge only after CI green.

## Risks

- Wiki route block is larger than STT and includes background job state.
- Reindex helpers are monkeypatched by tests, so alias compatibility matters.
- Chat route uses wiki integration but should not move in Phase C unless tests
  show a necessary coupling.

## Stop Conditions

Pause before implementation if:

- The backend router PR picks up unrelated frontend, harness, or model changes.
- Moving wiki and reindex together creates unclear diff review. In that case,
  split Phase C into C1 and C2 PRs.
- Any endpoint behavior changes beyond import/module ownership.
