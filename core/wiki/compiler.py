"""WikiCompilerV2 — Phase 2 의 실제 LLM 호출 기반 컴파일러.

설계 의도: Phase 1 의 `steps/wiki_compiler.WikiCompiler` 는 dry_run 골격이며 그대로
유지된다. 본 모듈의 `WikiCompilerV2` 는 실제 페이지 생성 책임을 담당하고,
Phase 1 의 WikiCompiler 가 wiki.dry_run=False 일 때 본 클래스를 위임 호출한다.

흐름:
    1. DecisionExtractor.extract() → ExtractedDecision[].
    2. ActionItemExtractor.extract_new() + detect_closed().
    3. DecisionExtractor.render_pages() → [(rel_path, content)].
    4. ActionItemExtractor.render_unified_page() → action_items.md 본문.
    5. 페이지마다 WikiGuard.verify() → D1+D2+D3.
    6. passed → store.write_page(); low_confidence → pending/; reject → log.
    7. log.md append + git_commit_atomic.

자동 운영: 모든 분기에서 사용자 prompt 없음.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from core.wiki.extractors.action_item import (
    ActionItemExtractor,
    ClosedActionItem,
    OpenActionItem,
)
from core.wiki.extractors.decision import DecisionExtractor
from core.wiki.extractors.person import PersonExtractor
from core.wiki.extractors.project import ProjectExtractor
from core.wiki.extractors.topic import TopicExtractor
from core.wiki.guard import GuardVerdict, WikiGuard
from core.wiki.lint import WikiLinter
from core.wiki.llm_client import WikiLLMClient, WikiLLMError
from core.wiki.search_index import WikiSearchIndex
from core.wiki.store import WikiStore, WikiStoreError

logger = logging.getLogger(__name__)


def _first_utterance_timestamp(utterances: list[Any]) -> str | None:
    """첫 정상 발화의 시작 시각을 HH:MM:SS 로 반환한다."""
    best_seconds: float | None = None
    for utt in utterances:
        try:
            if isinstance(utt, dict):
                raw_start = utt.get("start")
                raw_end = utt.get("end")
            else:
                raw_start = utt.start
                raw_end = utt.end
            start = float(raw_start)  # type: ignore[arg-type]  # None/이상치는 아래 except 에서 skip
            end = float(raw_end)  # type: ignore[arg-type]
        except (TypeError, ValueError, AttributeError):
            continue
        if start < 0 or end < start:
            continue
        if best_seconds is None or start < best_seconds:
            best_seconds = start
    if best_seconds is None:
        return None
    total = int(best_seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _normalize_zero_timestamp_citations(
    content: str,
    *,
    meeting_id: str,
    replacement_ts: str | None,
) -> str:
    """현재 회의의 00:00:00 citation 을 첫 실제 발화 시각으로 정규화한다."""
    if not replacement_ts or replacement_ts == "00:00:00":
        return content
    old_marker = f"[meeting:{meeting_id}@00:00:00]"
    if old_marker not in content:
        return content
    new_marker = f"[meeting:{meeting_id}@{replacement_ts}]"
    return content.replace(old_marker, new_marker)


# ─────────────────────────────────────────────────────────────────────────
# 5.1 결과 dataclass
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CompileResult:
    """단일 회의 ingest 의 wiki 컴파일 결과 요약.

    Attributes:
        meeting_id: 입력된 회의 ID.
        pages_created: 신규 생성된 페이지의 rel_path 목록.
        pages_updated: 기존 페이지 갱신된 rel_path 목록.
        pages_pending: D3 미달로 pending/ 에 격리된 rel_path 목록.
        pages_rejected: D1 overflow 또는 D2 phantom 으로 거부된 (rel_path, reason).
        commit_sha: D5 git_commit_atomic 결과. 변경 없으면 빈 문자열.
        duration_seconds: 경과 시간.
        llm_call_count: LLM generate() 누적 호출 수.
    """

    meeting_id: str
    pages_created: list[str] = field(default_factory=list)
    pages_updated: list[str] = field(default_factory=list)
    pages_pending: list[str] = field(default_factory=list)
    pages_rejected: list[tuple[str, str]] = field(default_factory=list)
    commit_sha: str = ""
    duration_seconds: float = 0.0
    llm_call_count: int = 0


# ─────────────────────────────────────────────────────────────────────────
# 5.2 CompileTargets — 4종 페이지 분기 결과
# ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CompileTargets:
    """이번 회의 ingest 가 갱신할 페이지 목록 (LLM 의 영향 페이지 결정 결과).

    Phase 2 까지는 _decide_pages 가 별도 dataclass 없이 (decisions, action_items_flag)
    만 받았으나 Phase 3 에서 4종 분기로 확장하므로 명시적 타입을 도입한다.

    Phase 3 에서는 라우터(_decide_pages) 를 도입하지 않고 모든 extractor 를 직접
    호출하는 패턴을 유지하므로, CompileTargets 는 후속 단계용 결과 컨테이너로
    사용된다.

    Attributes:
        decisions: ExtractedDecision 후보들.
        action_items: 항상 True (action_items.md 는 단일 통합 파일).
        people: ExtractedPerson 후보들 (PersonExtractor 결과).
        projects: ExtractedProject 후보들 (ProjectExtractor 결과).
        topics: Phase 4 placeholder. 현재는 항상 빈 리스트.
    """

    decisions: list = field(default_factory=list)
    action_items: bool = True
    people: list = field(default_factory=list)
    projects: list = field(default_factory=list)
    topics: list = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────
# 5.3 컴파일러
# ─────────────────────────────────────────────────────────────────────────


class WikiCompilerV2:
    """Phase 2 실제 컴파일러. 4종 추출기 + 5중 방어 통과.

    Phase 3 변경:
        - PersonExtractor / ProjectExtractor 를 추가 의존성으로 주입.
        - compile_meeting 에 단계 5b (people) + 5c (projects) 추가.
        - speaker_name_map 인자로 화자 정규화를 person/action_item 에 전달.

    Threading: 단일 코루틴에서 직렬 호출 가정.
    """

    def __init__(
        self,
        *,
        config: Any,
        store: WikiStore,
        llm: WikiLLMClient,
        guard: WikiGuard,
        decision_extractor: DecisionExtractor,
        action_item_extractor: ActionItemExtractor,
        person_extractor: PersonExtractor,
        project_extractor: ProjectExtractor,
        topic_extractor: TopicExtractor | None = None,
        linter: WikiLinter | None = None,
        semantic_doc_embedder: Callable[[list[str]], list[list[float]]] | None = None,
    ) -> None:
        """모든 의존성을 주입받는다 — DI 로 테스트 격리.

        Args:
            config: WikiConfig.
            store: 초기화된 WikiStore.
            llm: WikiLLMClient.
            guard: WikiGuard 인스턴스.
            decision_extractor: DecisionExtractor 인스턴스.
            action_item_extractor: ActionItemExtractor 인스턴스.
            person_extractor: PersonExtractor 인스턴스 (Phase 3).
            project_extractor: ProjectExtractor 인스턴스 (Phase 3).
            topic_extractor: TopicExtractor 인스턴스 (Phase 4, 선택).
            linter: WikiLinter 인스턴스 (Phase 4, 선택). N회의마다 lint 실행.
        """
        self._config = config
        self._store: WikiStore = store
        self._llm: WikiLLMClient = llm
        self._guard: WikiGuard = guard
        self._decision_extractor: DecisionExtractor = decision_extractor
        self._action_item_extractor: ActionItemExtractor = action_item_extractor
        self._person_extractor: PersonExtractor = person_extractor
        self._project_extractor: ProjectExtractor = project_extractor
        # Phase 4 신규
        self._topic_extractor: TopicExtractor | None = topic_extractor
        self._linter: WikiLinter | None = linter
        # G1 — 페이지 쓰기 시 벡터 색인용 문서 임베더. 주입 시에만 시맨틱 색인 활성
        # (미주입=색인 skip → 테스트는 e5 미로드). prod 팩토리가 e5 임베더 주입.
        self._semantic_doc_embedder = semantic_doc_embedder
        # lint 트리거용 카운터 — 매 compile_meeting 직후 +1
        self._meeting_count: int = 0

    async def _reindex_semantic(self) -> None:
        """G1 — 페이지 벡터 색인 갱신(graceful). 임베더 미주입/비활성/실패 시 무영향.

        문서 임베딩(e5)은 별도 스레드에서 수행하며, 실패해도 ingest 는 유지되고
        검색은 BM25 로 폴백한다. config 가 AppConfig 가 아니면(paths 부재) skip.
        """
        if self._semantic_doc_embedder is None:
            return
        wiki_cfg = getattr(self._config, "wiki", self._config)
        sem_cfg = getattr(wiki_cfg, "semantic", None)
        paths_cfg = getattr(self._config, "paths", None)
        if sem_cfg is None or not getattr(sem_cfg, "enabled", False) or paths_cfg is None:
            return
        try:
            import asyncio

            from core.wiki.semantic_index import WikiSemanticIndex, rebuild_semantic_index

            semantic_index = WikiSemanticIndex(
                paths_cfg.resolved_chroma_db_dir,
                collection_name=sem_cfg.collection_name,
            )
            count = await asyncio.to_thread(
                rebuild_semantic_index,
                self._store,
                semantic_index=semantic_index,
                embed_documents=self._semantic_doc_embedder,
            )
            logger.info("wiki 벡터 색인 갱신: %d 페이지", count)
        except Exception as exc:  # noqa: BLE001 — 색인 실패는 ingest/검색에 무영향
            logger.warning("wiki 벡터 색인 rebuild 실패 — ingest 유지: %r", exc)

    def _regenerate_digest(self) -> None:
        """C2 — 현황 다이제스트(digest.md) 재생성(graceful, LLM/모델 0).

        결정/액션을 순수 집계(`core.wiki.digest`)해 digest.md 를 갱신한다. 모델 로드
        0(불변식 #4)이며, 비활성(`wiki.digest.enabled=false`)/실패 시 ingest 무영향.
        """
        wiki_cfg = getattr(self._config, "wiki", self._config)
        digest_cfg = getattr(wiki_cfg, "digest", None)
        if digest_cfg is None or not getattr(digest_cfg, "enabled", False):
            return
        try:
            from core.wiki.digest import build_digest, render_digest_markdown

            digest = build_digest(self._store, digest_config=digest_cfg, now=date.today())
            self._store.write_page(Path("digest.md"), render_digest_markdown(digest))
            logger.info(
                "wiki 현황 다이제스트 갱신: 미해결 %d · 최근결정 %d · 프로젝트 %d",
                digest.total_open_actions,
                len(digest.recent_decisions),
                len(digest.project_status),
            )
        except Exception as exc:  # noqa: BLE001 — 다이제스트 실패는 ingest 무영향(graceful)
            logger.warning("wiki 현황 다이제스트 생성 실패 — ingest 유지: %r", exc)

    async def compile_meeting(
        self,
        *,
        meeting_id: str,
        meeting_date: date,
        summary: str,
        utterances: list,
        speaker_name_map: dict[str, str] | None = None,
    ) -> CompileResult:
        """단일 회의를 컴파일하여 wiki 를 갱신한다.

        Phase 3 흐름:
            1. DecisionExtractor.extract → ExtractedDecision[].
            2. ActionItemExtractor.extract_new + detect_closed.
            3. 기존 action_items.md 파싱.
            4. DecisionExtractor.render_pages → decision pages.
            5. ActionItemExtractor.render_unified_page → action_items.md.
            5b. PersonExtractor.extract_speakers + render_or_update_pages → people pages.
            5c. ProjectExtractor.extract_projects + detect_status_transitions
                + render_or_update_pages → project pages.
            6. WikiGuard.verify 적용 후 store.write_page.
            7. log.md append + git_commit_atomic.

        실패 격리: person/project 어느 한쪽 실패해도 decisions/action_items 는 살아남음.

        Args:
            meeting_id: 실제 회의 ID 또는 하위 호환 8자리 hex.
            meeting_date: 회의 날짜.
            summary: 8단계 요약 마크다운.
            utterances: 5단계 corrector 결과.
            speaker_name_map: corrector 가 제공한 {SPEAKER_XX: 한국어이름}.
                None 이면 person/action_item 의 fuzzy matching 비활성화.

        Returns:
            CompileResult.
        """
        start_ts = time.time()
        pages_created: list[str] = []
        pages_updated: list[str] = []
        pages_pending: list[str] = []
        pages_rejected: list[tuple[str, str]] = []

        # ── 1. DecisionExtractor — graceful degradation ────────────────
        decisions: list = []
        extractor_start = time.perf_counter()
        try:
            decisions = await self._decision_extractor.extract(
                meeting_id=meeting_id,
                meeting_date=meeting_date,
                summary=summary,
                utterances=utterances,
            )
        except WikiLLMError as exc:
            logger.warning("DecisionExtractor 실패 — decisions 페이지 0건: %r", exc)
            decisions = []
        except Exception as exc:  # noqa: BLE001
            logger.error("DecisionExtractor 예상치 못한 오류: %r", exc, exc_info=True)
            decisions = []
        logger.info(
            "Wiki extractor timing: meeting_id=%s extractor=decision.extract "
            "elapsed_seconds=%.3f items=%d",
            meeting_id,
            time.perf_counter() - extractor_start,
            len(decisions),
        )

        # ── 2. ActionItemExtractor.extract_new ────────────────────────
        new_actions: list = []
        extractor_start = time.perf_counter()
        try:
            new_actions = await self._action_item_extractor.extract_new(
                meeting_id=meeting_id,
                meeting_date=meeting_date,
                utterances=utterances,
                speaker_name_map=speaker_name_map,
            )
        except WikiLLMError as exc:
            logger.warning("ActionItemExtractor.extract_new 실패: %r", exc)
            new_actions = []
        except Exception as exc:  # noqa: BLE001
            logger.error("ActionItemExtractor.extract_new 오류: %r", exc, exc_info=True)
            new_actions = []
        logger.info(
            "Wiki extractor timing: meeting_id=%s extractor=action_item.extract_new "
            "elapsed_seconds=%.3f items=%d",
            meeting_id,
            time.perf_counter() - extractor_start,
            len(new_actions),
        )

        # ── 3. 기존 action_items.md 파싱 ──────────────────────────────
        try:
            existing_open, existing_closed = await self._parse_existing_action_items()
        except Exception as exc:  # noqa: BLE001
            logger.warning("기존 action_items.md 파싱 실패 — 빈 목록으로 진행: %r", exc)
            existing_open, existing_closed = [], []

        # ── 4. detect_closed ─────────────────────────────────────────
        newly_closed: list = []
        extractor_start = time.perf_counter()
        try:
            newly_closed = await self._action_item_extractor.detect_closed(
                existing_open=existing_open,
                meeting_id=meeting_id,
                utterances=utterances,
            )
        except WikiLLMError as exc:
            logger.warning("detect_closed 실패: %r", exc)
            newly_closed = []
        except Exception as exc:  # noqa: BLE001
            logger.error("detect_closed 오류: %r", exc, exc_info=True)
            newly_closed = []
        logger.info(
            "Wiki extractor timing: meeting_id=%s extractor=action_item.detect_closed "
            "elapsed_seconds=%.3f items=%d",
            meeting_id,
            time.perf_counter() - extractor_start,
            len(newly_closed),
        )

        # ── 5. DecisionExtractor.render_pages ────────────────────────
        # PRD R3 리스크 대응: 회의당 갱신 페이지를 상한 8개로 제한.
        # TODO(Phase 2.E): decisions 정렬 기준(confidence 내림차순) 추가 후
        #   decisions = decisions[:8] 로 slice. 현재는 추출된 전체를 처리.
        decision_pages: list[tuple[str, str]] = []
        if decisions:
            try:
                decision_pages = await self._decision_extractor.render_pages(
                    decisions=decisions,
                    meeting_id=meeting_id,
                    meeting_date=meeting_date,
                    existing_store=self._store,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("render_pages 전체 실패: %r", exc)
                decision_pages = []

        # ── 6. action_items.md 렌더링 ────────────────────────────────
        action_pages: list[tuple[str, str]] = []
        try:
            action_content = await self._action_item_extractor.render_unified_page(
                new_open=new_actions,
                newly_closed=newly_closed,
                existing_open=existing_open,
                existing_closed=existing_closed,
                last_compiled_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            )
            action_pages.append(("action_items.md", action_content))
        except Exception as exc:  # noqa: BLE001
            logger.warning("render_unified_page 실패: %r", exc)
            action_pages = []

        # ── 6b. PersonExtractor (Phase 3) — graceful degradation ─────
        person_pages: list[tuple[str, str, int]] = []
        extractor_start = time.perf_counter()
        try:
            persons = await self._person_extractor.extract_speakers(
                meeting_id=meeting_id,
                meeting_date=meeting_date,
                utterances=utterances,
                speaker_name_map=speaker_name_map,
            )
            if persons:
                person_pages = await self._person_extractor.render_or_update_pages(
                    persons=persons,
                    meeting_id=meeting_id,
                    meeting_date=meeting_date,
                    existing_store=self._store,
                    meeting_decisions=decisions,
                    meeting_new_actions=new_actions,
                    existing_open_actions=existing_open,
                )
        except WikiLLMError as exc:
            logger.warning("PersonExtractor 실패 — people 페이지 0건: %r", exc)
            person_pages = []
        except Exception as exc:  # noqa: BLE001
            logger.error("PersonExtractor 예상치 못한 오류: %r", exc, exc_info=True)
            person_pages = []
        logger.info(
            "Wiki extractor timing: meeting_id=%s extractor=person elapsed_seconds=%.3f pages=%d",
            meeting_id,
            time.perf_counter() - extractor_start,
            len(person_pages),
        )

        # ── 6c. ProjectExtractor (Phase 3) — graceful degradation ────
        project_pages: list[tuple[str, str, int]] = []
        extractor_start = time.perf_counter()
        try:
            projects = await self._project_extractor.extract_projects(
                meeting_id=meeting_id,
                meeting_date=meeting_date,
                utterances=utterances,
                summary=summary,
            )
            # 기존 ExistingProject 목록은 Phase 3 에서 별도 인덱스 도입 전까지
            # 빈 리스트로 둔다 — detect_status_transitions 는 빈 리스트에서
            # LLM 호출 0회로 즉시 반환하므로 안전.
            existing_projects: list = []
            status_transitions = await self._project_extractor.detect_status_transitions(
                existing_projects=existing_projects,
                meeting_id=meeting_id,
                utterances=utterances,
            )
            if projects:
                project_pages = await self._project_extractor.render_or_update_pages(
                    projects=projects,
                    status_transitions=status_transitions,
                    meeting_id=meeting_id,
                    meeting_date=meeting_date,
                    existing_store=self._store,
                    meeting_decisions=decisions,
                    meeting_new_actions=new_actions,
                    existing_open_actions=existing_open,
                )
        except WikiLLMError as exc:
            logger.warning("ProjectExtractor 실패 — project 페이지 0건: %r", exc)
            project_pages = []
        except Exception as exc:  # noqa: BLE001
            logger.error("ProjectExtractor 예상치 못한 오류: %r", exc, exc_info=True)
            project_pages = []
        logger.info(
            "Wiki extractor timing: meeting_id=%s extractor=project elapsed_seconds=%.3f pages=%d",
            meeting_id,
            time.perf_counter() - extractor_start,
            len(project_pages),
        )

        # ── 6d. TopicExtractor (Phase 4) — graceful degradation ──────
        topic_pages: list[tuple[str, str, int]] = []
        if self._topic_extractor is not None:
            extractor_start = time.perf_counter()
            try:
                new_concepts = await self._topic_extractor.extract_concepts(
                    meeting_id=meeting_id,
                    meeting_date=meeting_date,
                    utterances=utterances,
                    summary=summary,
                )
                if new_concepts:
                    topic_pages = await self._topic_extractor.aggregate_and_render(
                        new_concepts=new_concepts,
                        meeting_id=meeting_id,
                        meeting_date=meeting_date,
                        existing_store=self._store,
                    )
            except WikiLLMError as exc:
                logger.warning("TopicExtractor 실패 — topic 페이지 0건: %r", exc)
                topic_pages = []
            except Exception as exc:  # noqa: BLE001
                logger.error("TopicExtractor 예상치 못한 오류: %r", exc, exc_info=True)
                topic_pages = []
            logger.info(
                "Wiki extractor timing: meeting_id=%s extractor=topic "
                "elapsed_seconds=%.3f pages=%d",
                meeting_id,
                time.perf_counter() - extractor_start,
                len(topic_pages),
            )

        # ── 7. 페이지별 WikiGuard.verify + write ─────────────────────
        # Phase 3: people / projects 는 (path, content, confidence) 튜플 형식이므로
        #   2-tuple 로 정규화. confidence 는 D3 검증에서 사용되며, guard 가 본문의
        #   <!-- confidence: N --> 마커를 직접 읽으므로 추가 인자 전달은 불요.
        people_pages_pairs = [(p, c) for p, c, _ in person_pages]
        project_pages_pairs = [(p, c) for p, c, _ in project_pages]
        # Phase 4: topics 도 동일 형식
        topic_pages_pairs = [(p, c) for p, c, _ in topic_pages]
        all_pages: list[tuple[str, str]] = (
            list(decision_pages)
            + list(action_pages)
            + people_pages_pairs
            + project_pages_pairs
            + topic_pages_pairs
        )
        zero_ts_replacement = _first_utterance_timestamp(utterances)

        for rel_path, content in all_pages:
            normalized_content = _normalize_zero_timestamp_citations(
                content,
                meeting_id=meeting_id,
                replacement_ts=zero_ts_replacement,
            )
            if normalized_content != content:
                logger.warning(
                    "00:00:00 citation 정규화: page=%s, replacement_ts=%s",
                    rel_path,
                    zero_ts_replacement,
                )
                content = normalized_content
            verdict = await self._guard.verify(
                page_path=rel_path,
                new_content=content,
                meeting_id=meeting_id,
            )
            # verdict.cleaned_content 가 있는 경우 D1 후처리 결과를 쓰기에 사용한다.
            final_content = (
                verdict.cleaned_content if verdict.cleaned_content is not None else content
            )
            self._dispatch_page_by_verdict(
                rel_path=rel_path,
                content=final_content,
                verdict=verdict,
                store=self._store,
                pages_created=pages_created,
                pages_updated=pages_updated,
                pages_pending=pages_pending,
                pages_rejected=pages_rejected,
            )

        # ── 7b. 검색 색인 갱신 ──────────────────────────────────────
        # Wiki 페이지는 파일 시스템이 원장이고 FTS5는 파생 색인이다. 단일 ingest 후
        # 전체 rebuild 비용은 MVP 규모(<1000 pages)에서 작고, pending/rejected 이동까지
        # 한 번에 반영되므로 stale index 위험을 줄인다.
        if pages_created or pages_updated or pages_pending:
            try:
                WikiSearchIndex(self._store.root).rebuild(self._store)
            except Exception as exc:  # noqa: BLE001
                logger.warning("wiki 검색 색인 rebuild 실패 — ingest 는 유지: %r", exc)
            # G1 — 시맨틱(벡터) 색인 갱신(임베더 주입 시에만 활성, graceful).
            await self._reindex_semantic()

        # ── 7c. 현황 다이제스트 재생성 (C2) ──────────────────────────────
        # 변경 가드 밖에서 매 compile 재생성한다 — 집계는 모델 0(저렴)이고, 이렇게
        # 해야 action 렌더 실패 등으로 페이지 변경이 0건인 회의에서도 digest 가
        # stale 되지 않는다(현황판은 항상 최신이 핵심). 실패는 graceful(ingest 유지).
        self._regenerate_digest()

        # ── 9. lint 트리거 (Phase 4 신규) ────────────────────────────
        # 매 compile_meeting 후 카운터 +1. lint_interval 도달 시 lint_all 실행.
        # lint 자체 실패는 ingest 를 막지 않는다 (graceful).
        if self._linter is not None:
            self._meeting_count += 1
            lint_interval = int(
                getattr(getattr(self._config, "wiki", self._config), "lint_interval", 5)
            )
            if self._meeting_count >= lint_interval:
                try:
                    report = await self._linter.lint_all(
                        meetings_since_last_lint=self._meeting_count,
                    )
                    self._store.write_page(
                        Path("HEALTH.md"),
                        report.to_health_md(),
                    )
                    logger.info(
                        "D4 lint 완료 — citation_pass_rate=%.2f, orphans=%d, "
                        "cyclic=%d, contradictions=%d",
                        report.citation_pass_rate,
                        len(report.orphans),
                        len(report.cyclic_citations),
                        len(report.contradictions),
                    )
                    self._meeting_count = 0
                except Exception as exc:  # noqa: BLE001
                    logger.warning("lint 실행 실패 — 다음 ingest 에서 재시도: %r", exc)

        # ── 8. git commit ─────────────────────────────────────────────
        commit_sha = ""
        try:
            commit_sha = self._store.git_commit_atomic(f"wiki: {meeting_id} 컴파일 결과")
        except WikiStoreError as exc:
            logger.warning("git_commit_atomic 실패: %r", exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("git_commit_atomic 예상치 못한 오류: %r", exc, exc_info=True)

        duration = time.time() - start_ts

        # llm_call_count — mock client 의 calls 길이를 사용 (실제 클라이언트는 자체 카운터)
        llm_call_count = getattr(self._llm, "calls", None)
        if llm_call_count is None:
            llm_count = 0
        else:
            llm_count = len(llm_call_count)

        return CompileResult(
            meeting_id=meeting_id,
            pages_created=pages_created,
            pages_updated=pages_updated,
            pages_pending=pages_pending,
            pages_rejected=pages_rejected,
            commit_sha=commit_sha,
            duration_seconds=duration,
            llm_call_count=llm_count,
        )

    async def _parse_existing_action_items(
        self,
    ) -> tuple[list[OpenActionItem], list[ClosedActionItem]]:
        """기존 action_items.md 의 Open / Closed 섹션을 파싱한다.

        Phase 2.C 단계의 단순 구현: 파일이 없거나 파싱이 실패하면 빈 리스트 반환.
        실제 파싱 로직은 Phase 2.E 에서 강화.

        Returns:
            (existing_open, existing_closed). 비어있으면 ([], []).
        """
        try:
            page = self._store.read_page(Path("action_items.md"))
        except WikiStoreError:
            return ([], [])
        except Exception as exc:  # noqa: BLE001
            logger.debug("action_items.md 읽기 실패: %r", exc)
            return ([], [])

        # Phase 2.C 골격 — 기존 항목 파싱은 Phase 2.E 에서 정밀화.
        # 현재는 빈 리스트 반환하여 후속 호출이 정상 동작하도록 한다.
        _ = page
        return ([], [])

    @staticmethod
    def _dispatch_page_by_verdict(
        *,
        rel_path: str,
        content: str,
        verdict: GuardVerdict,
        store: WikiStore,
        pages_created: list[str],
        pages_updated: list[str],
        pages_pending: list[str],
        pages_rejected: list[tuple[str, str]],
    ) -> None:
        """verdict 결과에 따라 페이지를 분류하고 store 에 기록한다.

        Args:
            rel_path: 위키 루트 기준 상대 경로.
            content: D1 후처리 완료된 최종 페이지 본문.
            verdict: WikiGuard.verify() 결과.
            store: WikiStore 인스턴스 (디스크 쓰기 담당).
            pages_created / pages_updated / pages_pending / pages_rejected:
                컴파일 결과 누적 리스트.

        Note:
            D3 low_confidence 페이지는 `pending/` 접두사를 붙여 격리 저장한다.
            D1 overflow 또는 D2 phantom 은 디스크에 쓰지 않고 rejected 만 기록.
        """
        if verdict.passed:
            # 5중 방어 통과 — 디스크에 기록
            try:
                # 기존 페이지 존재 여부로 created/updated 분기
                try:
                    store.read_page(Path(rel_path))
                    is_existing = True
                except WikiStoreError:
                    is_existing = False
                store.write_page(Path(rel_path), content)
                if is_existing:
                    pages_updated.append(rel_path)
                else:
                    pages_created.append(rel_path)
            except WikiStoreError as exc:
                logger.error("store.write_page 실패: path=%s, reason=%s", rel_path, exc.reason)
                pages_rejected.append((rel_path, "write_failed"))
            except Exception as exc:  # noqa: BLE001
                logger.error("store.write_page 예상치 못한 오류: path=%s, %r", rel_path, exc)
                pages_rejected.append((rel_path, "write_failed"))

        elif verdict.reason == "low_confidence":
            # D3 미달 — pending/ 에 격리 저장 (사용자가 나중에 검토)
            pending_path = Path("pending") / rel_path
            try:
                store.write_page(pending_path, _set_frontmatter_status(content, "pending"))
                pages_pending.append(rel_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("pending 저장 실패 — rejected 처리: path=%s, %r", rel_path, exc)
                pages_pending.append(rel_path)  # 기록은 유지 (write 실패여도 pending 분류)

        else:
            # D1 overflow, D2 phantom, malformed_confidence — 디스크 쓰기 없이 거부 기록
            logger.warning(
                "페이지 거부: path=%s, reason=%s, phantom_count=%d",
                rel_path,
                verdict.reason,
                len(verdict.rejected_citations),
            )
            pages_rejected.append((rel_path, verdict.reason))


def _set_frontmatter_status(content: str, status: str) -> str:
    """Markdown frontmatter 의 status 값을 교체하거나 추가한다."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return content
    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return content
    for idx in range(1, end_idx):
        if lines[idx].startswith("status:"):
            lines[idx] = f"status: {status}"
            return "\n".join(lines) + ("\n" if content.endswith("\n") else "")
    lines.insert(end_idx, f"status: {status}")
    return "\n".join(lines) + ("\n" if content.endswith("\n") else "")
