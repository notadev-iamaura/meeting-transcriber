---
name: meeting-transcriber-release-harness
description: Use for release readiness, PR preparation, risky refactors, CI hardening, regression triage, and verification planning in the meeting-transcriber repository.
---

# Meeting Transcriber Release Harness

Use this skill when a task needs evidence before editing or when the change can affect runtime, model loading, storage, search, UI routing, setup, or release confidence.

## Workflow

1. Inspect repository state first:
   - `git status --short`
   - identify user changes and do not revert them.
2. Define the fix boundary:
   - touched modules
   - behavior that must stay unchanged
   - narrowest test command that proves the change.
3. Gather local evidence before editing:
   - read `docs/STATUS.md`
   - read the directly affected source and tests
   - for UI/CSS, read `docs/design.md`.
4. Implement the smallest scoped change.
5. Verify with targeted gates:
   - Python logic: `pytest <touched tests> -q`
   - API/router: use the API/router commands in `docs/STATUS.md`
   - UI shell/view: `node --check ui/web/<file>.js` and relevant `pytest -m ui ...`
   - release confidence: `pytest -m harness -q`
   - eval-sensitive AI behavior: `pytest tests/test_quality_evals.py -q`.
6. Report:
   - commands run and results
   - checks skipped and why
   - residual risk.

## Guardrails

- Do not read `.env`, `.env.local`, or production env files.
- Do not bypass SSL, HuggingFace gating, package index security, or corporate network policy.
- Ask before production dependency changes, broad network access, auth default changes, destructive commands, or publishing remote changes.
- Keep model lifecycle rules intact: one large model loaded at a time, pyannote on CPU, MLX in process unless configured otherwise.

## Useful References

- `AGENTS.md` for project-wide constraints.
- `docs/STATUS.md` for current gates.
- `harness/README.md` for consensus and UI gate mechanics.
- `docs/BENCHMARK.md` for STT model quality context.
