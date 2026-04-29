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
from datetime import date, datetime
from pathlib import Path
from typing import Any

from config import AppConfig
from core.pipeline import PipelineError
from core.wiki.store import WikiStore, WikiStoreError

logger = logging.getLogger(__name__)


def _create_wiki_compiler_v2(
    *,
    config: AppConfig,
    store: WikiStore,
    model_manager: Any | None,
) -> Any:
    """Phase 2 V2 컴파일러 팩토리. 테스트 monkeypatch 진입점.

    실제 의존성 (MlxWikiClient, WikiGuard, DecisionExtractor, ActionItemExtractor)
    을 lazy import 하여 wiki 비활성 시 import 비용을 0 으로 둔다. 테스트는 이
    함수를 monkeypatch 하여 mock V2 인스턴스를 주입할 수 있다.

    Args:
        config: AppConfig — wiki 컴파일러 설정.
        store: 초기화된 WikiStore.
        model_manager: ModelLoadManager (실제 LLM 호출 시 필요).

    Returns:
        WikiCompilerV2 인스턴스 (또는 mock).
    """
    # Lazy import — 실제 LLM 호출 경로에서만 로드
    from core.wiki.compiler import WikiCompilerV2
    from core.wiki.extractors.action_item import ActionItemExtractor
    from core.wiki.extractors.decision import DecisionExtractor
    from core.wiki.guard import WikiGuard
    from core.wiki.llm_client import MlxWikiClient

    llm = MlxWikiClient(config=config, model_manager=model_manager)
    # Phase 2.E: CitationVerifier 는 wiki_compiler.py 가 자체 회의 단위 verifier
    # 를 빌드해서 주입 (utterances 기반). 여기서는 placeholder.
    from core.wiki.guard import CitationVerifier

    class _NullVerifier:
        """Phase 2.E placeholder. Phase 3 에서 RAG 기반 verifier 로 교체."""

        async def verify_exists(self, meeting_id: str, timestamp_seconds: int) -> bool:
            return True

        async def fetch_utterance(
            self, meeting_id: str, timestamp_seconds: int
        ) -> str | None:
            return None

    guard = WikiGuard(
        verifier=_NullVerifier(),
        confidence_threshold=config.wiki.confidence_threshold,
    )
    decision_extractor = DecisionExtractor(llm)
    action_item_extractor = ActionItemExtractor(llm)
    return WikiCompilerV2(
        config=config,
        store=store,
        llm=llm,
        guard=guard,
        decision_extractor=decision_extractor,
        action_item_extractor=action_item_extractor,
    )


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

            # ── 4. Phase 2 (실제 컴파일) — V2 호출 ───────────────────
            # summary 가 비어있으면 안전하게 dry_run 폴백 (8단계 실패 시).
            if not summary:
                logger.warning(
                    "wiki.dry_run=False 이지만 summary 가 비어있음 → "
                    "dry_run 폴백 (meeting_id=%s)",
                    meeting_id,
                )
                return await self._run_dry_run(store, meeting_id)

            return await self._run_v2(
                store=store,
                meeting_id=meeting_id,
                summary=summary,
                utterances=utterances or [],
            )

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

    async def _run_v2(
        self,
        *,
        store: WikiStore,
        meeting_id: str,
        summary: str,
        utterances: list[Any],
    ) -> dict[str, Any]:
        """Phase 2 실제 컴파일 — WikiCompilerV2.compile_meeting 위임.

        V2 가 예외를 던지면 PipelineError 로 wrap 하여 PipelineManager 의
        non-fatal 처리에 일관되게 노출한다.

        Args:
            store: 초기화된 WikiStore.
            meeting_id: 회의 ID.
            summary: 8단계 요약 결과.
            utterances: 5단계 보정 발화 리스트.

        Returns:
            {"status": "compiled", "meeting_id": ..., "pages_created": [...],
             "pages_updated": [...], "pages_pending": [...], "pages_rejected": [...],
             "commit_sha": "...", "duration_seconds": float}.

        Raises:
            PipelineError: V2.compile_meeting 이 예외를 던졌을 때.
        """
        try:
            v2 = _create_wiki_compiler_v2(
                config=self._config,
                store=store,
                model_manager=self._model_manager,
            )
            result = await v2.compile_meeting(
                meeting_id=meeting_id,
                meeting_date=date.today(),
                summary=summary,
                utterances=utterances,
            )
            logger.info(
                "wiki V2 컴파일 완료: meeting_id=%s, created=%d, updated=%d, "
                "pending=%d, rejected=%d",
                meeting_id,
                len(getattr(result, "pages_created", []) or []),
                len(getattr(result, "pages_updated", []) or []),
                len(getattr(result, "pages_pending", []) or []),
                len(getattr(result, "pages_rejected", []) or []),
            )
            return {
                "status": "compiled",
                "meeting_id": meeting_id,
                "pages_created": list(getattr(result, "pages_created", []) or []),
                "pages_updated": list(getattr(result, "pages_updated", []) or []),
                "pages_pending": list(getattr(result, "pages_pending", []) or []),
                "pages_rejected": list(getattr(result, "pages_rejected", []) or []),
                "commit_sha": getattr(result, "commit_sha", "") or "",
                "duration_seconds": float(
                    getattr(result, "duration_seconds", 0.0) or 0.0
                ),
            }
        except Exception as exc:
            logger.error(
                "wiki V2 컴파일 실패 (meeting_id=%s): %s",
                meeting_id,
                exc,
            )
            raise PipelineError(f"wiki V2 컴파일 실패: {exc}") from exc

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
