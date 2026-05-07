# Phase E Search/Chat Router Boundary Plan

Date: 2026-05-07
Baseline: `main` at `dbdf1e1f1926f7c37cb09c3a0e578f0e0c912595`

## Objective

Continue reducing `api/routes.py` by moving the search and chat API surface into
a focused router without changing RAG, wiki routing, or response contracts.

Phase E focuses on:

1. Search API: `POST /api/search`
2. Chat API: `POST /api/chat`, including optional wiki-router metadata

## Current State

- `api/routes.py`: 3,901 lines after Phase D.
- Already split:
  - `api/dependencies.py`
  - `api/config_yaml.py`
  - `api/routers/meetings_batch.py`
  - `api/routers/stt_models.py`
  - `api/routers/wiki.py`
  - `api/routers/reindex.py`
  - `api/routers/settings.py`
  - `api/routers/user_settings.py`
- Open PR count: 0.
- Phase D PR #46 was merged with green CI.

## Decision

Proceed with one backend-only Phase E PR:

1. Extract search/chat schemas and endpoints into `api/routers/search_chat.py`.
2. Preserve all dependency access through `api.dependencies`.
3. Keep `api.routes` compatibility aliases for schemas and helper functions.

## Work Breakdown

### E1: Search Router Extraction

Move from `api/routes.py`:

- `SearchRequest`
- `SearchResultItem`
- `SearchResponse`
- `POST /api/search`

Compatibility:

- Preserve `api.routes.SearchResponse` and related aliases.
- Preserve `EmptyQueryError` -> 400 and `ModelLoadError` -> 503 mappings.
- Preserve filter forwarding to `HybridSearchEngine.search()`.

### E2: Chat Router Extraction

Move from `api/routes.py`:

- `ChatRequest`
- `ChatReferenceItem`
- `ChatResponse`
- `_ChatEngineAdapter`
- `_build_chat_references`
- `_serialize_router_verdict`
- `_serialize_wiki_sources`
- `_build_hybrid_chat_service`
- `POST /api/chat`

Compatibility:

- Preserve router-disabled behavior: new metadata fields remain `None`.
- Preserve wiki/rag/both routing response shapes.
- Move active monkeypatch targets to `api.routers.search_chat` where router
  internals now read them.

### E3: Verification And PR

Final local gates:

```bash
ruff check api/routes.py api/routers/search_chat.py tests/wiki/test_routes_chat_router.py
ruff format --check api/routes.py api/routers/search_chat.py tests/wiki/test_routes_chat_router.py
.venv/bin/python -m pytest tests/test_routes.py tests/wiki/test_routes_chat_router.py tests/wiki/test_rag_unchanged.py tests/test_chat.py tests/test_server.py -q
```

Observed local outcome before PR:

- `api/routes.py`: reduced from 3,901 lines after Phase D to 3,394 lines after
  extracting search/chat ownership.
- `api/routers/search_chat.py`: 538 lines.

PR expectations:

- One backend-only PR.
- Do not include unrelated local STT/model-manager changes already present in
  the working tree.
- PR body lists moved symbols, compatibility aliases, and exact verification.
- Merge only after CI green.

## Risks

- Chat tests patch `_build_hybrid_chat_service`; those patches must target the
  new router module to exercise the active implementation.
- `api.routes.ChatResponse` remains a public compatibility import used by wiki
  chat-router tests.
- Wiki router metadata is optional but user-visible when enabled, so response
  defaults must stay stable.

## Stop Conditions

Pause before implementation if:

- Any endpoint path, response shape, or status code must change.
- The backend router PR picks up unrelated STT, model-manager, frontend,
  harness, or CI changes.
- Targeted tests show RAG/wiki routing behavior changed.
