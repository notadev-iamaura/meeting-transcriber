# Phase F Meeting Detail Router Boundary Plan

## Goal

Extract single-meeting detail APIs from the `api/routes.py` monolith into a
focused router without changing public endpoint behavior.

Phase F focuses on:

1. Meeting detail and state transitions:
   `GET/PATCH/DELETE /api/meetings/{meeting_id}`,
   retry/transcribe/cancel/re-transcribe, and pipeline-state.
2. Meeting artifacts:
   audio streaming with HTTP Range support, transcript/summary reads, summary
   edits, transcript edits, and transcript pattern replacement.
3. Single-meeting LLM summarization:
   `POST /api/meetings/{meeting_id}/summarize`.

## Starting State

- Baseline commit: `23774267d85135c7da5765e6e803465a976b337f`.
- Phase E PR #47 was merged with green CI.
- `api/routes.py`: 3,394 lines after Phase E.
- Existing domain routers:
  - `api/routers/meetings_batch.py`
  - `api/routers/stt_models.py`
  - `api/routers/wiki.py`
  - `api/routers/reindex.py`
  - `api/routers/settings.py`
  - `api/routers/user_settings.py`
  - `api/routers/search_chat.py`

## Scope

Proceed with one backend-only Phase F PR:

1. Add `api/routers/meeting_detail.py`.
2. Move the active endpoint implementations for `/api/meetings/{meeting_id}` and
   its nested routes into that module.
3. Keep `/api/meetings` list and `/api/meetings/summarize-batch` in
   `api/routes.py`.
4. Preserve `api.routes` compatibility re-exports for existing tests and
   external monkeypatch/import paths.
5. Update status/goal docs after local gates pass.

## Compatibility Contracts

- Endpoint paths, HTTP methods, response models, and error mappings remain
  unchanged.
- `api.routes.MeetingItem`, `TranscriptResponse`, `SummaryResponse`,
  transcript edit request/response schemas, audio helpers, and atomic write
  helper aliases remain importable.
- `api.routes._validate_meeting_id` remains available for A/B test and batch
  routes that still live in the main router.
- Single-meeting summarize moves with the meeting detail router; batch summarize
  stays in `api/routes.py`.

## Verification Gates

Run the smallest sufficient backend gates:

```bash
.venv/bin/python -m ruff check api/routes.py api/routers/meeting_detail.py
.venv/bin/python -m py_compile api/routes.py api/routers/meeting_detail.py
.venv/bin/python -m pytest tests/test_routes.py tests/test_meeting_edit.py -q
.venv/bin/python -m pytest tests/test_routes_meetings_batch.py tests/test_server.py tests/test_security_fixes.py -q
```

Before PR:

```bash
.venv/bin/python -m pytest tests/test_routes.py tests/test_meeting_edit.py tests/test_routes_meetings_batch.py tests/test_server.py tests/test_security_fixes.py -q
```

## Result

- `api/routes.py`: reduced from 3,394 lines after Phase E to 1,782 lines after
  Phase F.
- `api/routers/meeting_detail.py`: 1,677 lines.
- Local targeted gates passed:
  - `ruff check api/routes.py api/routers/meeting_detail.py`
  - `py_compile api/routes.py api/routers/meeting_detail.py`
  - `pytest tests/test_routes.py tests/test_meeting_edit.py tests/test_routes_meetings_batch.py tests/test_server.py tests/test_security_fixes.py -q`

## Residual Risk

- The new router intentionally keeps several route-level helpers local rather
  than introducing a service layer. That keeps Phase F behavior-preserving; a
  future service extraction can split storage/artifact concerns behind tests.
- System, recording, upload, dashboard, and A/B test APIs still live in
  `api/routes.py` and are the next backend router candidates.
