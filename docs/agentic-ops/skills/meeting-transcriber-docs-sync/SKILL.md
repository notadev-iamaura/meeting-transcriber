---
name: meeting-transcriber-docs-sync
description: Use after code, config, model, setup, API, UI, benchmark, or release-process changes in the meeting-transcriber repository to update only the relevant project documentation.
---

# Meeting Transcriber Docs Sync

Use this skill when implementation changes require documentation to stay aligned.

## Workflow

1. Inventory the change:
   - changed files
   - changed behavior
   - changed commands, config keys, API routes, model defaults, or manual steps.
2. Find the smallest documentation surface:
   - setup/runtime: `AGENTS.md`, `README.md`, `docs/STATUS.md`
   - audio capture: `docs/AGGREGATE_DEVICE_SETUP.md`
   - UI/design: `docs/design.md`, relevant `docs/plans/*`
   - STT/LLM quality: `docs/BENCHMARK.md`, `docs/plan-stt-improvements.md`
   - agent workflow: `docs/agentic-ops/*`, `harness/README.md`.
3. Preserve public/private boundaries:
   - do not add secrets, local tokens, private customer context, or hidden environment details
   - do not document unsafe network or SSL bypasses.
4. Edit only stale or missing documentation.
5. Verify examples and commands:
   - run the narrowest command if practical
   - otherwise state that the command was documented but not executed.

## Output Contract

Final response should include:

- docs updated
- behavior or command now documented
- verification command and result, or skipped reason.
