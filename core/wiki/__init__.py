"""Wiki 패키지

목적: Phase 1 의 wiki 도메인 4개 모듈(models / citations / schema / store) 을
모은 패키지. 인터페이스 정의에 따라 의도적으로 re-export 하지 않으며,
사용처가 명시적으로 `from core.wiki.models import ...` 하도록 강제한다.
이는 의존성을 가시화하고 순환 import 위험을 줄이기 위함이다.

의존성 그래프:
    models     →  (의존 없음)
    citations  →  models
    schema     →  models
    store      →  (git subprocess 만, 다른 wiki 모듈 import 금지)
"""

from __future__ import annotations
