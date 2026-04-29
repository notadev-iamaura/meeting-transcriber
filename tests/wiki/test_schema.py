"""Wiki 스키마 모듈 테스트 (test_schema.py)

목적: core/wiki/schema.py 의 모든 퍼블릭 함수가 PRD §4.2 명세를 정확히
     준수하는지 검증한다. TDD Red 단계 — schema.py 가 아직 없으므로 모든
     테스트는 ImportError 로 실패해야 한다.

주요 기능:
    - generate_schema_md(): 인용 형식·병기 금지 규칙 포함 여부
    - render_decision_template(): frontmatter 6필드 + 본문 4섹션
    - render_person_template(): frontmatter 필드 + 본문 4섹션
    - render_project_template(): status 열거값 제약
    - render_topic_template(): 빈 concept 에러 처리
    - render_action_items_template(): 빈 입력 시 헤더 항상 출력
    - render_index_md(): 카테고리 순서 보장

의존성: pytest, core.wiki.schema (TDD Red 단계에서 미구현)
"""

from __future__ import annotations

import pytest

from core.wiki.models import PageType

# TDD Red: core/wiki/schema.py 가 존재하지 않으므로 아래 import 가
# ImportError 를 발생시켜 모든 테스트가 실패해야 한다.
from core.wiki.schema import (
    generate_schema_md,
    render_action_items_template,
    render_decision_template,
    render_index_md,
    render_person_template,
    render_project_template,
    render_topic_template,
)

# ──────────────────────────────────────────────
# 1. generate_schema_md() 테스트 (2건)
# ──────────────────────────────────────────────


class TestGenerateSchemaMd:
    """generate_schema_md() 가 wiki/CLAUDE.md 에 필요한 규칙을 포함하는지 검증한다."""

    def test_인용_형식_표준이_출력에_포함된다(self) -> None:
        """출력 마크다운에 PRD §4.3 인용 형식 표준 `[meeting:{id}@HH:MM:SS]` 가 명시되어야 한다.

        Arrange: 별도 입력 없음
        Act: generate_schema_md() 호출
        Assert: 결과 문자열에 "[meeting:" 과 "HH:MM:SS" 가 모두 포함됨
        """
        # Arrange (별도 setup 없음)

        # Act
        result = generate_schema_md()

        # Assert
        assert "[meeting:" in result, "LLM 시스템 프롬프트에 인용 마커 접두사 '[meeting:' 이 없음"
        assert "HH:MM:SS" in result, "인용 형식 표준에 타임스탬프 형식 'HH:MM:SS' 가 없음"

    def test_한국어_고유명사_병기_금지_규칙이_포함된다(self) -> None:
        """출력에 한국어 고유명사 영어·중국어 병기 금지 규칙이 명시되어야 한다.

        PRD §4.3 LLM 작성 규칙: "배미령(Baimilong)" 같은 병기를 EXAONE 이
        출력하지 못하도록 CLAUDE.md 에 명시적 금지 규칙을 포함해야 한다.

        Arrange: 별도 입력 없음
        Act: generate_schema_md() 호출
        Assert: "병기 금지" 또는 "병기" + "금지" 를 포함하는 규칙이 존재
        """
        # Arrange (별도 setup 없음)

        # Act
        result = generate_schema_md()

        # Assert — "병기 금지" 구문 또는 "배미령(Baimilong)" 같은 예시로 규칙 존재 확인
        has_rule = ("병기 금지" in result) or ("병기" in result and "금지" in result)
        assert has_rule, (
            "LLM 시스템 프롬프트에 한국어 고유명사 외국어 병기 금지 규칙이 없음. "
            "'병기 금지' 또는 이에 상응하는 구문이 포함되어야 한다."
        )


# ──────────────────────────────────────────────
# 2. render_decision_template() 테스트 (2건)
# ──────────────────────────────────────────────


class TestRenderDecisionTemplate:
    """render_decision_template() 이 PRD §4.2 decisions 템플릿을 준수하는지 검증한다."""

    REQUIRED_FRONTMATTER_FIELDS = [
        "type",
        "date",
        "meeting_id",
        "status",
        "participants",
        "projects",
    ]

    @pytest.mark.parametrize("field", REQUIRED_FRONTMATTER_FIELDS)
    def test_frontmatter_에_필수_6필드가_모두_포함된다(self, field: str) -> None:
        """PRD §4.2 decisions frontmatter 의 6개 필드가 출력에 존재해야 한다.

        Args:
            field: type / date / meeting_id / status / participants / projects

        Arrange: 최소 인자로 render_decision_template() 호출
        Act: 결과 문자열 검사
        Assert: 각 필드 키가 frontmatter 영역에 포함됨
        """
        # Arrange
        result = render_decision_template(
            meeting_id="abc12345",
            date="2026-04-28",
            title="테스트 결정 제목",
            participants=["철수", "영희"],
            projects=["new-onboarding"],
            confidence=8,
        )

        # Act + Assert
        assert f"{field}:" in result, f"decisions frontmatter 에 필수 필드 '{field}' 가 없음"

    def test_본문_4개_섹션_헤더가_모두_포함된다(self) -> None:
        """PRD §4.2 decisions 본문의 4섹션 헤더가 출력에 존재해야 한다.

        Arrange: 표준 인자로 render_decision_template() 호출
        Act: 결과 문자열에서 ## 헤더 검사
        Assert: '## 결정 내용', '## 배경', '## 후속 액션', '## 참고 회의' 모두 포함
        """
        # Arrange
        result = render_decision_template(
            meeting_id="abc12345",
            date="2026-04-28",
            title="5월 출시일 확정",
        )

        # Act + Assert
        expected_sections = ["## 결정 내용", "## 배경", "## 후속 액션", "## 참고 회의"]
        for section in expected_sections:
            assert section in result, f"decisions 템플릿 본문에 섹션 '{section}' 이 없음"


# ──────────────────────────────────────────────
# 3. render_person_template() 테스트 (2건)
# ──────────────────────────────────────────────


class TestRenderPersonTemplate:
    """render_person_template() 이 PRD §4.2 people 템플릿을 준수하는지 검증한다."""

    REQUIRED_FRONTMATTER_FIELDS = [
        "type",
        "name",
        "role",
        "first_seen",
        "last_seen",
        "meetings_count",
    ]

    @pytest.mark.parametrize("field", REQUIRED_FRONTMATTER_FIELDS)
    def test_frontmatter_에_필수_필드가_모두_포함된다(self, field: str) -> None:
        """PRD §4.2 people frontmatter 필드 6개가 출력에 존재해야 한다.

        Args:
            field: type / name / role / first_seen / last_seen / meetings_count

        Arrange: role 포함 전체 인자로 호출
        Act: 결과 문자열 검사
        Assert: 각 필드 키가 frontmatter 에 포함됨
        """
        # Arrange — role 을 명시적으로 제공해야 해당 필드가 렌더링됨
        result = render_person_template(
            name="철수",
            role="PM",
            first_seen="2026-01-10",
            last_seen="2026-04-28",
            meetings_count=12,
        )

        # Act + Assert
        assert f"{field}:" in result, f"people frontmatter 에 필수 필드 '{field}' 가 없음"

    def test_본문_4개_섹션_헤더가_모두_포함된다(self) -> None:
        """PRD §4.2 people 본문의 4섹션 헤더가 출력에 존재해야 한다.

        Arrange: 최소 인자(name 만)로 render_person_template() 호출
        Act: 결과 문자열에서 ## 헤더 검사
        Assert: 4개 섹션 헤더 모두 포함
        """
        # Arrange
        result = render_person_template(name="영희")

        # Act + Assert
        expected_sections = [
            "## 최근 결정",
            "## 담당 프로젝트",
            "## 자주 언급하는 주제",
            "## 미해결 액션아이템",
        ]
        for section in expected_sections:
            assert section in result, f"people 템플릿 본문에 섹션 '{section}' 이 없음"


# ──────────────────────────────────────────────
# 4. render_project_template() 테스트 (1건)
# ──────────────────────────────────────────────


class TestRenderProjectTemplate:
    """render_project_template() 의 status 열거값 제약을 검증한다."""

    VALID_STATUSES = ["in-progress", "blocked", "shipped", "cancelled"]
    INVALID_STATUSES = ["active", "done", "wip", "", "IN-PROGRESS", "진행중"]

    @pytest.mark.parametrize("valid_status", VALID_STATUSES)
    def test_유효한_status_값은_정상_렌더링된다(self, valid_status: str) -> None:
        """허용된 4가지 status 값으로 호출 시 예외 없이 결과를 반환해야 한다.

        Args:
            valid_status: in-progress | blocked | shipped | cancelled

        Arrange: 해당 status 로 render_project_template() 호출
        Act: 렌더링 수행
        Assert: ValueError 없이 결과 반환, 결과에 status 값 포함
        """
        # Arrange + Act
        result = render_project_template(slug="new-onboarding", status=valid_status)

        # Assert
        assert valid_status in result, f"status '{valid_status}' 가 출력 frontmatter 에 없음"

    @pytest.mark.parametrize("invalid_status", INVALID_STATUSES)
    def test_유효하지_않은_status_값은_ValueError_를_발생시킨다(self, invalid_status: str) -> None:
        """허용 목록 외 status 값으로 호출 시 ValueError 가 발생해야 한다.

        PRD §4.2 projects frontmatter: status 는 in-progress|blocked|shipped|cancelled
        4개 중 하나만 허용한다. 오탈자·대소문자 불일치도 즉시 거부.

        Args:
            invalid_status: 허용 목록 외 임의 문자열

        Arrange: 잘못된 status 로 render_project_template() 호출
        Act: 호출 시 예외 발생 여부 확인
        Assert: ValueError 발생
        """
        # Arrange + Act + Assert
        with pytest.raises(ValueError, match=r"status"):
            render_project_template(slug="test-proj", status=invalid_status)


# ──────────────────────────────────────────────
# 5. render_topic_template() 테스트 (1건)
# ──────────────────────────────────────────────


class TestRenderTopicTemplate:
    """render_topic_template() 의 입력 유효성 검증을 테스트한다."""

    def test_빈_concept_인자는_ValueError_를_발생시킨다(self) -> None:
        """concept 인자가 빈 문자열이면 ValueError 가 발생해야 한다.

        topics 페이지의 식별자(concept) 는 파일명으로 사용되므로 빈 문자열을
        허용하면 안 된다. 공백만 있는 문자열도 거부 대상이다.

        Arrange: concept="" 로 render_topic_template() 호출
        Act: 호출 시 예외 발생 여부 확인
        Assert: ValueError 발생
        """
        # Arrange + Act + Assert
        with pytest.raises(ValueError):
            render_topic_template(concept="")

    def test_공백만_있는_concept_도_ValueError_를_발생시킨다(self) -> None:
        """concept 인자가 공백만 있는 문자열이면 ValueError 가 발생해야 한다.

        Arrange: concept="   " 로 render_topic_template() 호출
        Act: 호출 시 예외 발생 여부 확인
        Assert: ValueError 발생
        """
        # Arrange + Act + Assert
        with pytest.raises(ValueError):
            render_topic_template(concept="   ")


# ──────────────────────────────────────────────
# 6. render_action_items_template() 테스트 (1건)
# ──────────────────────────────────────────────


class TestRenderActionItemsTemplate:
    """render_action_items_template() 이 빈 입력에서도 헤더를 출력하는지 검증한다."""

    def test_빈_입력_시_Open_과_Closed_헤더가_항상_출력된다(self) -> None:
        """open/closed 항목이 없어도 섹션 헤더가 항상 렌더링되어야 한다.

        PRD §4.2 action_items 템플릿: "## Open (0)" 과 "## Closed (0)" 헤더는
        항목 유무에 관계없이 항상 출력되어야 한다.

        Arrange: 인자 없이 render_action_items_template() 호출 (기본값 사용)
        Act: 결과 문자열 검사
        Assert: "## Open" 과 "## Closed" 헤더가 모두 포함됨, 괄호 안 숫자 0 포함
        """
        # Arrange + Act
        result = render_action_items_template()

        # Assert — 헤더 존재 여부 (숫자 포함 형식 "## Open (0)")
        assert "## Open" in result, "비어있는 action_items 에도 '## Open' 섹션 헤더가 있어야 함"
        assert "## Closed" in result, (
            "비어있는 action_items 에도 '## Closed' 섹션 헤더가 있어야 함"
        )
        # 빈 상태에서 카운트가 0 임을 표시해야 함
        assert "(0)" in result, "빈 action_items 의 헤더에 아이템 수 '(0)' 이 표시되어야 함"


# ──────────────────────────────────────────────
# 7. render_index_md() 테스트 (1건)
# ──────────────────────────────────────────────


class TestRenderIndexMd:
    """render_index_md() 가 PRD §4.2 카테고리 순서를 준수하는지 검증한다."""

    def test_카테고리가_PRD_명세_순서대로_출력된다(self) -> None:
        """index.md 출력의 카테고리 순서가 PRD 명세와 일치해야 한다.

        PRD §4.2 index.md 카테고리 순서:
            1. Decisions (결정)
            2. People (사람)
            3. Projects (프로젝트)
            4. Topics (주제)
            5. Action Items (액션아이템)

        Arrange: 5개 카테고리 각 1건씩 pages_metadata 구성
        Act: render_index_md() 호출
        Assert: 출력 문자열에서 각 카테고리 헤더의 등장 순서가 PRD 명세와 동일
        """
        # Arrange — 5개 카테고리 각 1건
        pages_metadata = {
            PageType.DECISION: [
                {
                    "path": "decisions/2026-04-28-launch.md",
                    "type": PageType.DECISION,
                    "title": "출시일 확정",
                    "last_updated": "2026-04-28",
                }
            ],
            PageType.PERSON: [
                {
                    "path": "people/철수.md",
                    "type": PageType.PERSON,
                    "title": "철수",
                    "last_updated": "2026-04-28",
                    "meetings_count": 5,
                }
            ],
            PageType.PROJECT: [
                {
                    "path": "projects/new-onboarding.md",
                    "type": PageType.PROJECT,
                    "title": "신규 온보딩",
                    "last_updated": "2026-04-27",
                }
            ],
            PageType.TOPIC: [
                {
                    "path": "topics/pricing-strategy.md",
                    "type": PageType.TOPIC,
                    "title": "pricing-strategy",
                    "last_updated": "2026-04-26",
                }
            ],
            PageType.ACTION_ITEMS: [
                {
                    "path": "action_items.md",
                    "type": PageType.ACTION_ITEMS,
                    "title": "Action Items",
                    "last_updated": "2026-04-28",
                }
            ],
        }

        # Act
        result = render_index_md(pages_metadata)

        # Assert — 순서 검증: 각 카테고리 헤더의 첫 등장 위치 비교
        # 헤더 키워드는 구현에 따라 영문/한글 혼용 가능 — 여기서는 최소 식별자 사용
        category_keywords = [
            "decision",  # Decisions 카테고리
            "people",  # People 카테고리 (또는 "person")
            "project",  # Projects 카테고리
            "topic",  # Topics 카테고리
            "action",  # Action Items 카테고리
        ]

        result_lower = result.lower()
        positions = []
        for keyword in category_keywords:
            pos = result_lower.find(keyword)
            assert pos != -1, f"index.md 출력에 카테고리 키워드 '{keyword}' 가 없음"
            positions.append(pos)

        # 순서 검증 — 앞 위치가 뒤 위치보다 항상 작아야 함
        for i in range(len(positions) - 1):
            assert positions[i] < positions[i + 1], (
                f"index.md 카테고리 순서 오류: '{category_keywords[i]}' (pos={positions[i]}) "
                f"이 '{category_keywords[i + 1]}' (pos={positions[i + 1]}) 보다 뒤에 있음. "
                f"PRD 순서: Decisions → People → Projects → Topics → Action Items"
            )
