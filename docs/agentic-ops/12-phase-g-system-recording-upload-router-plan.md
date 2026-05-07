# Phase G System/Recording/Upload Router Boundary Plan

## Goal

Extract system status, dashboard, upload, and manual recording APIs from the
`api/routes.py` monolith into focused routers without changing public endpoint
behavior.

Phase G focuses on:

1. System/dashboard:
   `GET /api/status`, `GET /api/system/resources`,
   `GET /api/dashboard/stats`, and `POST /api/system/open-audio-folder`.
2. Upload:
   `POST /api/uploads`, filename validation, collision-safe target resolution,
   and raw body streaming.
3. Recording:
   `GET /api/recording/status`, `POST /api/recording/start`,
   `POST /api/recording/stop`, and `GET /api/recording/devices`.

## Starting State

- Baseline commit: `2fec123f1d630eea1c3b24460e2cd126d2bd49df`.
- Phase F PR #48 was merged with green CI.
- `api/routes.py`: 1,782 lines after Phase F.
- Existing domain routers:
  - `api/routers/meetings_batch.py`
  - `api/routers/stt_models.py`
  - `api/routers/wiki.py`
  - `api/routers/reindex.py`
  - `api/routers/settings.py`
  - `api/routers/user_settings.py`
  - `api/routers/search_chat.py`
  - `api/routers/meeting_detail.py`

## Scope

Proceed with one backend-only Phase G PR:

1. Add `api/routers/system.py`.
2. Add `api/routers/uploads.py`.
3. Add `api/routers/recording.py`.
4. Keep meeting list, meeting summarize-batch, and A/B test APIs in
   `api/routes.py`.
5. Preserve `api.routes` compatibility re-exports for existing tests and
   external monkeypatch/import paths.
6. Update status/goal docs after local gates pass.

## Compatibility Contracts

- Endpoint paths, HTTP methods, response models, and error mappings remain
  unchanged.
- `api.routes.StatusResponse`, `SystemResourcesResponse`,
  `DashboardStatsResponse`, `OpenFolderResponse`, `UploadResponse`,
  `RecordingStatusResponse`, `RecordingStartRequest`, and `AudioDeviceItem`
  remain importable.
- Upload helpers such as `_sanitize_upload_filename`,
  `_resolve_unique_upload_path`, `_UPLOAD_MAX_BYTES`, and
  `_FILENAME_FORBIDDEN_PATTERN` remain importable from `api.routes`.
- Existing `api.routes.sys`, `api.routes.shutil`, and `api.routes.subprocess`
  monkeypatch paths remain available for open-folder tests. The active system
  router reads the legacy `sys` alias when present so platform patching remains
  behavior-preserving.

## Verification Gates

Run the smallest sufficient backend gates:

```bash
.venv/bin/python -m ruff check api/routes.py api/routers/system.py api/routers/uploads.py api/routers/recording.py
.venv/bin/python -m ruff format --check api/routes.py api/routers/system.py api/routers/uploads.py api/routers/recording.py
.venv/bin/python -m py_compile api/routes.py api/routers/system.py api/routers/uploads.py api/routers/recording.py
.venv/bin/python -m pytest tests/test_routes.py tests/test_routes_home_dashboard.py tests/test_server.py tests/test_security_fixes.py -q
.venv/bin/python -m pytest tests/test_meeting_edit.py tests/test_routes_meetings_batch.py tests/wiki/test_routes_chat_router.py tests/wiki/test_rag_unchanged.py tests/test_chat.py -q
```

## Result

- `api/routes.py`: reduced from 1,782 lines after Phase F to 1,126 lines after
  Phase G.
- `api/routers/system.py`: 351 lines.
- `api/routers/uploads.py`: 231 lines.
- `api/routers/recording.py`: 195 lines.
- Local targeted gates passed:
  - `ruff check api/routes.py api/routers/system.py api/routers/uploads.py api/routers/recording.py`
  - `ruff format --check api/routes.py api/routers/system.py api/routers/uploads.py api/routers/recording.py`
  - `py_compile api/routes.py api/routers/system.py api/routers/uploads.py api/routers/recording.py`
  - `pytest tests/test_routes.py tests/test_routes_home_dashboard.py tests/test_server.py tests/test_security_fixes.py -q`
  - `pytest tests/test_meeting_edit.py tests/test_routes_meetings_batch.py tests/wiki/test_routes_chat_router.py tests/wiki/test_rag_unchanged.py tests/test_chat.py -q`

## Residual Risk

- The system router intentionally includes a tiny compatibility shim for legacy
  `api.routes.sys` monkeypatching. A later test cleanup can move those patches
  directly to `api.routers.system`.
- A/B test endpoints still live in `api/routes.py` and are the next large
  router candidate.
