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
from core.wiki.extractors.person import ExtractedPerson, PersonExtractor
from core.wiki.extractors.project import ExtractedProject, ProjectExtractor
from core.wiki.extractors.topic import TopicExtractor
from core.wiki.guard import GuardVerdict, WikiGuard
from core.wiki.lint import WikiLinter
from core.wiki.llm_client import WikiLLMClient, WikiLLMError
from core.wiki.store import WikiStore, WikiStoreError

logger = logging.getLogger(__name__)


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
        # lint 트리거용 카운터 — 매 compile_meeting 직후 +1
        self._meeting_count: int = 0

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
            meeting_id: 8자리 hex.
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
        try:
            decisions = await self._decision_extractor.extract(
                meeting_id=meeting_id,
                meeting_date=meeting_date,
                summary=summary,
                utterances=utterances,
            )
        except WikiLLMError as exc:
            logger.warning(
                "DecisionExtractor 실패 — decisions 페이지 0건: %r", exc
            )
            decisions = []
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "DecisionExtractor 예상치 못한 오류: %r", exc, exc_info=True
            )
            decisions = []

        # ── 2. ActionItemExtractor.extract_new ────────────────────────
        new_actions: list = []
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
            logger.error(
                "ActionItemExtractor.extract_new 오류: %r", exc, exc_info=True
            )
            new_actions = []

        # ── 3. 기존 action_items.md 파싱 ──────────────────────────────
        try:
            existing_open, existing_closed = await self._parse_existing_action_items()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "기존 action_items.md 파싱 실패 — 빈 목록으로 진행: %r", exc
            )
            existing_open, existing_closed = [], []

        # ── 4. detect_closed ─────────────────────────────────────────
        newly_closed: list = []
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
            logger.error(
                "PersonExtractor 예상치 못한 오류: %r", exc, exc_info=True
            )
            person_pages = []

        # ── 6c. ProjectExtractor (Phase 3) — graceful degradation ────
        project_pages: list[tuple[str, str, int]] = []
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
            logger.error(
                "ProjectExtractor 예상치 못한 오류: %r", exc, exc_info=True
            )
            project_pages = []

        # ── 6d. TopicExtractor (Phase 4) — graceful degradation ──────
        topic_pages: list[tuple[str, str, int]] = []
        if self._topic_extractor is not None:
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
                logger.warning(
                    "TopicExtractor 실패 — topic 페이지 0건: %r", exc
                )
                topic_pages = []
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "TopicExtractor 예상치 못한 오류: %r", exc, exc_info=True
                )
                topic_pages = []

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

        for rel_path, content in all_pages:
            verdict = await self._guard.verify(
                page_path=rel_path,
                new_content=content,
                meeting_id=meeting_id,
            )
            # verdict.cleaned_content 가 있는 경우 D1 후처리 결과를 쓰기에 사용한다.
            final_content = verdict.cleaned_content if verdict.cleaned_content is not None else content
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
                    logger.warning(
                        "lint 실행 실패 — 다음 ingest 에서 재시도: %r", exc
                    )

        # ── 8. git commit ─────────────────────────────────────────────
        commit_sha = ""
        try:
            commit_sha = self._store.git_commit_atomic(
                f"wiki: {meeting_id} 컴파일 결과"
            )
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
                logger.error(
                    "store.write_page 실패: path=%s, reason=%s", rel_path, exc.reason
                )
                pages_rejected.append((rel_path, "write_failed"))
            except Exception as exc:  # noqa: BLE001
                logger.error("store.write_page 예상치 못한 오류: path=%s, %r", rel_path, exc)
                pages_rejected.append((rel_path, "write_failed"))

        elif verdict.reason == "low_confidence":
            # D3 미달 — pending/ 에 격리 저장 (사용자가 나중에 검토)
            pending_path = Path("pending") / rel_path
            try:
                store.write_page(pending_path, content)
                pages_pending.append(rel_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "pending 저장 실패 — rejected 처리: path=%s, %r", rel_path, exc
                )
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
