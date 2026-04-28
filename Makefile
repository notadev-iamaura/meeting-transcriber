# Makefile — 개발 편의 명령어
#
# 사용법: make help

.DEFAULT_GOAL := help

# === 코드 품질 ===

.PHONY: lint
lint: ## 린트 검사 (ruff check)
	python3 -m ruff check .

.PHONY: format
format: ## 코드 포맷팅 (ruff format)
	python3 -m ruff format .

.PHONY: format-check
format-check: ## 포맷 검사 (변경 없이 확인만)
	python3 -m ruff format --check .

.PHONY: fix
fix: ## 린트 자동 수정 + 포맷팅
	python3 -m ruff check --fix .
	python3 -m ruff format .

# === 테스트 ===

.PHONY: test
test: ## 전체 테스트 실행
	python3 -m pytest tests/ -v --tb=short -x

.PHONY: test-cov
test-cov: ## 커버리지 포함 테스트
	python3 -m pytest tests/ -v --cov=. --cov-report=term-missing --cov-report=html

.PHONY: test-quick
test-quick: ## 빠른 테스트 (첫 실패 시 중단)
	python3 -m pytest tests/ -x -q --tb=short

# === 실행 ===

.PHONY: run
run: ## 애플리케이션 실행
	python3 main.py

# === 정리 ===

.PHONY: clean
clean: ## 캐시 및 임시 파일 정리
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name htmlcov -exec rm -rf {} + 2>/dev/null || true
	rm -f .coverage

# === 도움말 ===

.PHONY: help
help: ## 사용 가능한 명령어 목록
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

# === UI/UX Overhaul 하네스 ===

.PHONY: harness-test harness-board harness-clean

harness-test:
	pytest -m harness -v

harness-board:
	python -m harness board rebuild
	@echo "📋 보드: docs/superpowers/ui-ux-overhaul/00-overview.md"

harness-clean:
	@echo "⚠️  state/harness.db 와 모든 시각 회귀 임시 산출물을 삭제합니다."
	rm -f state/harness.db state/harness.db-journal
	rm -rf tests/ui/visual/diffs tests/ui/__snapshots__
	rm -rf state/gate-logs
