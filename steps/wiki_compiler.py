"""WikiCompiler — LLM Wiki 9단계 컴파일러 (Phase 1 골격)

목적: 회의 요약 결과(`SummaryResult` + utterances)를 영구 위키 페이지로 컴파일
하는 9단계 파이프라인. Phase 1 에서는 실제 LLM 호출 없이 골격만 통합한다.

Phase 1 동작 (dry_run=True):
    1. WikiStore.init_repo() 멱등 호출 — 디렉토리 + git + 특수 파일 생성
    2. log.md 에 한 줄 append — `- [HH:MM:SS] ingest meeting:{id} → dry_run`
    3. WikiStore.git_commit_atomic() 으로 단일 커밋 생성

Phase 2 부터 (dry_run=False):
    - EXAONE compiler_model 로딩 → 발화 묶음을 wiki 페이지로 변환
    - 5중 방어(D1~D5) 검증 → 인용 강제, citation enforce, schema 검증
    - 페이지별 git commit + index.md 자동 갱신

이 모듈은 Phase 1 안전 기본값(`config.wiki.enabled=False`) 일 때 어떤 디스크
부작용도 일으키지 않는다. 실패 시 PipelineError 를 던지며, 호출 측(PipelineManager)
이 9단계를 non-fatal 로 처리하므로 RAG 파이프라인 결과는 정상 반환된다.

의존성: core.wiki.store, core.wiki.schema, core.pipeline.PipelineError, config.AppConfig.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from config import AppConfig
from core.pipeline import PipelineError
from core.wiki.store import WikiStore, WikiStoreError

logger = logging.getLogger(__name__)


class WikiCompiler:
    """LLM Wiki 9단계 컴파일러 (Phase 1 dry-run 전용).

    Args:
        config: AppConfig 인스턴스. config.wiki 만 참조한다.
        model_manager: ModelLoadManager (Phase 2 부터 사용). Phase 1 에서는 None.

    Threading: 인스턴스는 thread-safe 하지 않다. PipelineManager 가 순차 호출
    한다는 가정.
    """

    def __init__(
        self,
        config: AppConfig,
        model_manager: Any | None = None,
    ) -> None:
        """컴파일러를 초기화한다 (디스크 I/O 없음).

        Args:
            config: 애플리케이션 설정.
            model_manager: 모델 매니저 (Phase 1 에서는 사용 안 함, 호환성 위해 인자만 받음).
        """
        self._config = config
        self._model_manager = model_manager  # Phase 2 도입 예정
        # WikiStore 는 lazy 초기화 — disabled 일 때 디스크 접근 자체를 막기 위함
        self._store: WikiStore | None = None

    def _get_store(self) -> WikiStore:
        """WikiStore 인스턴스를 lazy 하게 만든다.

        Returns:
            WikiStore 인스턴스. wiki.root 의 ~ 확장은 여기서 1회만 처리.
        """
        if self._store is None:
            root = self._config.wiki.resolved_root
            self._store = WikiStore(root)
        return self._store

    async def run(
        self,
        meeting_id: str,
        summary: str | None = None,
        utterances: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Phase 1 dry-run: 빈 wiki 골격 + log.md 한 줄 append.

        Args:
            meeting_id: 8자리 hex 또는 PipelineManager 가 생성한 회의 ID.
                Phase 1 은 형식 검증을 하지 않는다 (Phase 2 D2 에서 검증).
            summary: 요약 텍스트. Phase 1 에서는 사용하지 않음.
            utterances: 발화 리스트. Phase 1 에서는 사용하지 않음.

        Returns:
            결과 딕셔너리:
                - {"status": "skipped", "reason": "disabled"}: enabled=False 일 때
                - {"status": "dry_run", "meeting_id": ..., "commit_sha": ...}:
                  Phase 1 dry_run 정상 완료
                - {"status": "compiled", ...}: Phase 2 부터 (현재 미구현)

        Raises:
            PipelineError: 디스크/git 작업 실패 시. 호출 측이 non-fatal 처리.
        """
        # ── 1. enabled 확인 — 즉시 no-op 반환 ─────────────────────────
        if not self._config.wiki.enabled:
            logger.debug("wiki.enabled=False, run() 스킵: meeting_id=%s", meeting_id)
            return {"status": "skipped", "reason": "disabled"}

        try:
            # ── 2. WikiStore 초기화 (멱등) ────────────────────────────
            store = self._get_store()
            store.init_repo()

            # ── 3. dry_run 분기 ──────────────────────────────────────
            if self._config.wiki.dry_run:
                return await self._run_dry_run(store, meeting_id)

            # ── 4. Phase 2 (실제 컴파일) ─────────────────────────────
            # Phase 1 에서는 미구현 — dry_run=False 일 때도 안전하게 dry_run
            # 동작으로 폴백한다. 향후 Phase 2 에서 실제 EXAONE 호출 추가.
            logger.warning(
                "wiki.dry_run=False 이지만 Phase 2 컴파일러가 아직 미구현. "
                "Phase 1 dry-run 으로 폴백."
            )
            return await self._run_dry_run(store, meeting_id)

        except WikiStoreError as exc:
            # Phase 1 에서 Wiki 실패는 non-fatal (PipelineManager 가 catch).
            # 일관된 PipelineError 로 escalate 하여 상위에서 분기 가능하게 한다.
            logger.error(
                "wiki 컴파일 실패 (meeting_id=%s, reason=%s): %s",
                meeting_id,
                exc.reason,
                exc.detail or exc,
            )
            raise PipelineError(
                f"wiki 컴파일 실패 ({exc.reason}): {exc.detail or exc}"
            ) from exc

    async def _run_dry_run(
        self,
        store: WikiStore,
        meeting_id: str,
    ) -> dict[str, Any]:
        """Phase 1 dry-run 실행 본체 — log.md 추가 + git commit.

        Args:
            store: 초기화된 WikiStore 인스턴스.
            meeting_id: 회의 ID.

        Returns:
            {"status": "dry_run", "meeting_id": ..., "commit_sha": "..."}.
            commit_sha 는 변경사항이 없을 때 빈 문자열.
        """
        # log.md 에 한 줄 append — 시간(HH:MM:SS) + meeting_id + dry_run 마커
        timestamp = datetime.now().strftime("%H:%M:%S")
        new_line = (
            f"- [{timestamp}] ingest meeting:{meeting_id} → dry_run "
            f"(Phase 1 골격, 실제 컴파일은 Phase 2 부터)\n"
        )
        # 기존 log.md 에 누적 — init_repo 가 헤더(`# Wiki 운영 로그`) 1줄을 보장
        log_path = store.log_path
        existing = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        # 기존 본문 + 신규 라인 한 번에 기록 (write_page 는 frontmatter 검증을 하지
        # 않으므로 raw text 그대로 저장 가능)
        new_text = existing + new_line if existing.endswith("\n") else existing + "\n" + new_line
        store.write_page(Path("log.md"), new_text)

        # git 커밋
        commit_sha = store.git_commit_atomic(
            f"기능: dry-run ingest {meeting_id}"
        )
        logger.info(
            "wiki dry-run 완료: meeting_id=%s, commit=%s",
            meeting_id,
            commit_sha[:8] if commit_sha else "(no-change)",
        )
        return {
            "status": "dry_run",
            "meeting_id": meeting_id,
            "commit_sha": commit_sha,
        }
