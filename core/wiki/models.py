"""Wiki 도메인 데이터 모델 모듈

목적: WikiCompiler 9단계와 5중 방어(WikiGuard) 가 공유하는 불변 데이터 구조를
정의한다. 이 모듈은 다른 wiki 모듈의 import 시작점이며, 외부 의존성을 갖지 않아
순환 import 위험을 원천 차단한다.

주요 기능:
    - PageType: Wiki 페이지 종류 Enum (decisions / people / projects / topics / ...)
    - Citation: 단일 인용 마커의 파싱 결과 (meeting_id + timestamp)
    - WikiPage: 한 페이지의 완전한 표현 (frontmatter + content + 추출된 인용 목록)
    - HealthReport: D4 lint 결과 보고서 (모순/고아/통과율)

의존성: 표준 라이브러리만 사용 (dataclasses, enum, datetime, pathlib).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class PageType(StrEnum):
    """Wiki 페이지의 정적 카테고리.

    PRD §4.1 디렉토리 레이아웃과 1:1 매핑. StrEnum 이므로 JSON 직렬화 시
    문자열 값이 그대로 사용된다.
    """

    # 회의별 결정사항 페이지
    DECISION = "decision"
    # 화자 1명에 대한 누적 페이지
    PERSON = "person"
    # 프로젝트별 진행 페이지
    PROJECT = "project"
    # 3회 이상 반복 등장한 개념 페이지
    TOPIC = "topic"
    # 단일 파일 — open/closed 통합
    ACTION_ITEMS = "action_items"
    # 단일 파일 — 카탈로그
    INDEX = "index"
    # append-only 시간순 운영 로그
    LOG = "log"
    # 최근 lint 결과 스냅샷
    HEALTH = "health"


@dataclass(frozen=True)
class Citation:
    """단일 인용 마커 `[meeting:{id}@{HH:MM:SS}]` 의 파싱 결과.

    PRD §4.3 인용 형식 표준에 정의된 정규식과 1:1 매핑된다. 모든 wiki 페이지의
    사실 진술 문장은 최소 하나 이상의 Citation 을 가져야 하며 (D1 인용 강제),
    각 Citation 의 timestamp 가 실제 발화와 일치해야 한다 (D2 실재성 검증).

    Attributes:
        meeting_id: 8자리 hex 문자열 (예: "abc12345"). DB 의 meeting.id 와 일치.
        timestamp_str: 원문 그대로의 "HH:MM:SS" 문자열 (예: "00:23:45").
        timestamp_seconds: HH:MM:SS 를 초 단위 정수로 변환 (예: 1425).
            D2 검증 시 utterance.start (float seconds) 와 비교용.
    """

    meeting_id: str
    timestamp_str: str
    timestamp_seconds: int


@dataclass
class WikiPage:
    """단일 Wiki 페이지의 메모리 내 표현.

    `WikiStore.read_page()` 가 디스크에서 읽어 반환하는 형태이며, WikiCompiler
    가 이를 갱신해 `WikiStore.write_page()` 로 다시 저장한다.

    Attributes:
        path: wiki 루트 기준 상대 경로. 절대 경로가 아니라는 점 주의 —
            root 결합은 WikiStore 가 담당.
        page_type: 카테고리 Enum.
        frontmatter: YAML 헤더(--- ~ ---) 를 dict 로 파싱한 결과.
            파싱 실패 시 빈 dict.
        content: frontmatter 를 제외한 마크다운 본문 (UTF-8 문자열).
        citations: content 에서 추출된 모든 Citation 목록 (등장 순서, 중복 허용).
    """

    path: Path
    page_type: PageType
    # 인스턴스마다 독립 dict 가 되도록 default_factory 사용
    frontmatter: dict[str, Any] = field(default_factory=dict)
    content: str = ""
    # 인스턴스마다 독립 list 가 되도록 default_factory 사용
    citations: list[Citation] = field(default_factory=list)


@dataclass(frozen=True)
class HealthReport:
    """D4 자동 lint 의 결과 스냅샷 (PRD §6 D4, §4 HEALTH.md).

    5회의마다 1회 생성되어 `wiki/HEALTH.md` 에 마크다운으로 직렬화된다.

    Attributes:
        last_lint_at: lint 실행 완료 시각 (ISO8601 문자열, 한국 시간대 권장).
        contradictions: 모순 페이지 경로 목록.
        orphans: 인커밍 링크가 0인 페이지 경로 목록.
        cyclic_links: 순환 인용을 형성하는 페이지 페어 또는 사이클 목록.
        citation_pass_rate: D2 재검증 통과율 (0.0 ~ 1.0).
        total_pages: 위키 전체 페이지 수.
        total_citations: 전체 인용 수.
    """

    last_lint_at: str
    contradictions: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    cyclic_links: list[tuple[str, ...]] = field(default_factory=list)
    citation_pass_rate: float = 1.0
    total_pages: int = 0
    total_citations: int = 0
