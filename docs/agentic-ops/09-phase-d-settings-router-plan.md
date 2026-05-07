# Phase D Settings Router Boundary Plan

Date: 2026-05-07
Baseline: `main` at `69af82335c9f3bb694bb8684effd81ce4f7cb61c`

## Objective

Continue reducing `api/routes.py` by moving the settings screen backend surface
into focused routers without changing public API behavior.

Phase D focuses on two related but storage-distinct domains:

1. System settings: `GET /api/settings`, `PUT /api/settings`
2. User-editable AI settings: `/api/prompts*`, `/api/vocabulary*`

## Current State

- `api/routes.py`: 4,629 lines after Phase C.
- Already split:
  - `api/dependencies.py`
  - `api/config_yaml.py`
  - `api/routers/meetings_batch.py`
  - `api/routers/stt_models.py`
  - `api/routers/wiki.py`
  - `api/routers/reindex.py`
- Open PR count: 0.
- Phase C PR #45 was merged with green CI.

## Decision

Proceed with one backend-only Phase D PR:

1. Extract config.yaml-backed settings into `api/routers/settings.py`.
2. Extract JSON-backed prompts and vocabulary into `api/routers/user_settings.py`.
3. Keep `api.routes` compatibility aliases for response models, helper
   functions, and legacy imports.

## Work Breakdown

### D1: Settings Router Extraction

Target files:

- Add `api/routers/settings.py`
- Update `api/routes.py`
- Update settings/security tests only where monkeypatch paths move

Move from `api/routes.py`:

- `_ALLOWED_MLX_MODELS`
- `_STT_LANGUAGE_PATTERN`
- `_AVAILABLE_MODELS`
- `SettingsResponse`
- `SettingsUpdateRequest`
- `SettingsUpdateResponse`
- `get_settings`
- `update_settings`

Compatibility:

- Keep `api.routes.SettingsResponse` and related aliases.
- Keep `api.routes._STT_LANGUAGE_PATTERN` available for tests and external
  imports.
- Move active monkeypatch targets to `api.routers.settings` where router
  internals actually read them.

### D2: User Settings Router Extraction

Target files:

- Add `api/routers/user_settings.py`
- Update `api/routes.py`

Move from `api/routes.py`:

- Prompt and vocabulary payload models
- `_prompts_to_payload`
- `_term_to_payload`
- `_map_user_settings_error`
- `/api/prompts`
- `/api/prompts/reset`
- `/api/vocabulary`
- `/api/vocabulary/terms`
- `/api/vocabulary/terms/{term_id}`
- `/api/vocabulary/reset`

Compatibility:

- Preserve JSON persistence through `core.user_settings`.
- Preserve response schemas, status codes, validation behavior, and reset
  semantics.
- Keep `api.routes` aliases for schemas/helpers while external imports catch up.

### D3: Verification And PR

Final local gates:

```bash
ruff check api/routes.py api/routers/settings.py api/routers/user_settings.py tests/test_security_fixes.py
ruff format --check api/routes.py api/routers/settings.py api/routers/user_settings.py tests/test_security_fixes.py
.venv/bin/python -m pytest tests/test_user_settings_api.py tests/test_user_settings_e2e.py tests/test_security_fixes.py tests/test_routes.py tests/test_server.py -q
```

Observed local outcome before PR:

- `api/routes.py`: reduced from 4,629 lines after Phase C to 3,901 lines after
  extracting settings and user settings ownership.
- `api/routers/settings.py`: 460 lines.
- `api/routers/user_settings.py`: 339 lines.

PR expectations:

- One backend-only PR.
- No frontend, model, CI, or harness changes in the PR.
- PR body lists moved symbols, compatibility aliases, and exact verification.
- Merge only after CI green.

## Risks

- `PUT /api/settings` writes `config.yaml` and historically exposed
  `_get_config_path` for monkeypatching. Tests must patch the router module that
  now owns the function.
- `/api/prompts` and `/api/vocabulary` share `core.user_settings` cache state;
  extraction must not change cache invalidation or file isolation behavior.
- SettingsView consumes all moved endpoints, so path stability matters more than
  module ownership.

## Stop Conditions

Pause before implementation if:

- Any endpoint path, response shape, or status code must change.
- The backend router PR picks up unrelated frontend, harness, or model changes.
- Targeted tests show persistence or config write behavior changed.
