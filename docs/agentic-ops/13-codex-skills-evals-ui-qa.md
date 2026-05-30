# Codex Skills, Evals, UI QA

## 프로젝트 로컬 스킬

Source of truth는 `docs/agentic-ops/skills/` 아래에 두고, 현재 프로젝트에는
동일한 내용을 `.codex/skills/` 아래에도 복사해 Codex 로컬 스킬로 바로 쓸 수
있게 둔다.

- `docs/agentic-ops/skills/meeting-transcriber-release-harness/SKILL.md`
- `docs/agentic-ops/skills/meeting-transcriber-docs-sync/SKILL.md`

## 오프라인 품질 Evals

모델 다운로드나 외부 API 없이 실행되는 회귀 eval이다.

```bash
.venv/bin/python -m pytest tests/test_quality_evals.py -q
```

골든 케이스는 `evals/quality_golden_cases.json`에 둔다. 현재 범위:

- STT 텍스트 정규화와 고유명사 보존
- VAD/세그먼트 시간 누락 및 환각 예산
- 반복 환각 필터링
- LLM 실패 시 폴백 회의록 계약
- RAG 프롬프트의 출처 메타데이터 보존

## UI QA Playwright 루프

서버를 먼저 실행한다.

```bash
.venv/bin/python main.py --no-menubar
```

다른 터미널에서 핵심 SPA 경로를 데스크톱/모바일 뷰포트로 점검한다.

```bash
.venv/bin/python scripts/ui_qa_playwright.py --url http://127.0.0.1:8765
```

결과는 `artifacts/ui-qa/report.json`과 PNG 스크린샷으로 남는다. 콘솔 에러나
페이지 에러가 있으면 스크립트는 non-zero로 종료한다.
