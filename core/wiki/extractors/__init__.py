"""Wiki extractors 패키지

Phase 2~4 의 LLM 기반 추출기 모음. 각 추출기는 회의 컨텍스트(요약 + 발화 목록)를
받아 결정사항·액션아이템·인물·프로젝트·개념(topic) 등의 구조화된 결과를 반환한다.

의존성 그래프:
    decision.py     →  llm_client, models, store, schema
    action_item.py  →  llm_client, models
    person.py       →  llm_client, models, decision (read-only), action_item (read-only)
    project.py      →  llm_client, models, decision (read-only), action_item (read-only)
    topic.py        →  llm_client, models, store

person ↔ project 상호 import 금지. topic 은 다른 extractor 와 독립.
"""

from __future__ import annotations

from core.wiki.extractors.action_item import (
    ActionItemExtractor,
    ClosedActionItem,
    NewActionItem,
    OpenActionItem,
)
from core.wiki.extractors.decision import (
    ActionItemRef,
    DecisionExtractor,
    ExtractedDecision,
)
from core.wiki.extractors.person import (
    ExistingPersonState,
    ExtractedPerson,
    PersonExtractor,
    TopicMention,
)
from core.wiki.extractors.project import (
    ExistingProject,
    ExtractedProject,
    ProjectExtractor,
    TimelineEntry,
)
from core.wiki.extractors.topic import (
    ConceptMention,
    ExtractedConcept,
    TopicExtractor,
)

__all__ = [
    # action_item
    "ActionItemExtractor",
    "ClosedActionItem",
    "NewActionItem",
    "OpenActionItem",
    # decision
    "ActionItemRef",
    "DecisionExtractor",
    "ExtractedDecision",
    # person
    "ExistingPersonState",
    "ExtractedPerson",
    "PersonExtractor",
    "TopicMention",
    # project
    "ExistingProject",
    "ExtractedProject",
    "ProjectExtractor",
    "TimelineEntry",
    # topic (Phase 4)
    "ConceptMention",
    "ExtractedConcept",
    "TopicExtractor",
]
