"""Wiki extractors 패키지

Phase 2 의 LLM 기반 추출기 모음. 각 추출기는 회의 컨텍스트(요약 + 발화 목록)를
받아 결정사항·액션아이템 등의 구조화된 결과를 반환한다.

의존성 그래프:
    decision.py     →  llm_client, models, store, schema
    action_item.py  →  llm_client, models
"""

from __future__ import annotations
