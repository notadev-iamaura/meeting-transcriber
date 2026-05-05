# Continuation Goal

This prompt keeps the agentic harness moving until the current quality phase is
done. At each turn, inspect the current repository state and decide whether to
continue, pause, or escalate.

## Current Phase

Continue reducing `ui/web/spa.js` global shell ownership by extracting
`BulkActionBar` behind a factory boundary while preserving selected-meeting
behavior, public browser globals, accessibility contracts, and existing
bulk-action tests.

Status: route-specific view extraction, `GlobalResourceBar` extraction, and
`BulkActionBar` extraction are reached. `spa.js` now keeps the SPA shell,
router, shortcuts, mobile drawer, and theme toggle.

## Continue When

- `ui/web/spa.js` still owns global shell modules that can be extracted behind a
  `window.Meeting* create(deps)` factory without changing public behavior.
- A proposed workstream has agreement from at least two independent auditors for
  the same role, or the lead records an evidence-backed tie-break.
- The change can be kept narrow, with implementation and regression tests in
  the same loop.
- P1 lifecycle or compatibility risks found by reviewers do not yet have tests
  and implementation criteria.

## Stop Or Pause When

- The next candidate requires a broad redesign rather than a narrow extraction.
- Required tests cannot run because of missing local dependencies or environment
  constraints that need user action.
- Public contracts would change without an explicit migration plan.
- The current workstream is complete and the next step requires a new product or
  architecture decision from the user.

## Completed Workstream Gates

Completed extraction gates:

- `WikiView`, wiki constants, and wiki helper functions live in
  `ui/web/wiki-view.js`.
- A/B list/new/result views live in `ui/web/ab-test-view.js`.
- `SearchView` lives in `ui/web/search-view.js`.
- Extracted views preserve their `window.SPA.*View` public constructors.
- Extracted async views guard stale or destroyed continuations.
- Harness boundary tests and targeted SPA integration tests pass.

Final route-view extraction gate:

- `EmptyView` and home-only action dropdown helpers live in
  `ui/web/empty-view.js`.
- `ui/web/spa.js` consumes `window.MeetingEmptyView.create({ ... })` and keeps
  `window.SPA.EmptyView` compatible.
- Home dashboard stats, audio-folder action, recording/start dropdowns, and
  status/toast behavior remain compatible.
- Any async stats or folder-open callbacks are guarded against stale DOM writes
  after `destroy()`.
- Harness boundary tests and SPA home/empty-state integration tests pass.

## Completion Decision

Stop the current loop unless the user explicitly starts a new phase. The next
possible extraction work is not a route-specific view; it would be global shell
architecture (`GlobalResourceBar`, mobile drawer, theme toggle, or shortcut
controllers), which needs a fresh workstream decision and risk review.

## Next Phase: Global Shell

Started after user confirmation. First accepted workstream:

- `GlobalResourceBar` lives in `ui/web/global-resource-bar.js`.
- `ui/web/spa.js` consumes `window.MeetingGlobalResourceBar.create({ ... })`.
- `#globalResourceBar` remains a singleton with `role="status"` and
  `aria-live="polite"`.
- `/api/system/resources` updates RAM/CPU/model display and preserves
  warning/danger thresholds.
- `start()` is idempotent and `stop()` prevents stale in-flight resource
  responses from mutating DOM.
- Harness boundary tests, SPA integration tests, system resource route tests,
  lint, and format checks pass.

Completion decision for this workstream: reached.

## Completed Workstream Gate: BulkActionBar

- `BulkActionBar` moves from `ui/web/spa.js` to a focused module such as
  `ui/web/bulk-action-bar.js`.
- The module exposes a factory boundary, for example
  `window.MeetingBulkActionBar.create({ App, ListPanel })`.
- `ui/web/spa.js` keeps `window.SPA.BulkActionBar` compatibility and calls
  `BulkActionBar.init()` exactly as before.
- Selection count, action button enablement, dropdown behavior, clear selection,
  batch API payloads, toast/status feedback, and keyboard/a11y semantics remain
  unchanged.
- Existing bulk action behavior/a11y/visual tests pass. Because some UI tests
  share fixed ports, run fixed-port suites sequentially unless their fixtures
  are made port-isolated first.

Completion decision for this workstream: reached.

## Next Workstream Candidates

Pause for the next decision unless the user explicitly continues. Remaining
global shell candidates are mobile drawer, theme toggle, shortcut controller,
or broader API/domain router cleanup. Each candidate has a different
verification surface and should receive a fresh paired review before edits.
