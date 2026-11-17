"""복합 질문의 로컬 모델 호출 상한을 설정 경계에서 고정한다."""

import pytest
from pydantic import ValidationError

from config import ChatConfig


def test_focus_selection_has_bounded_default() -> None:
    """기존 설정 파일에도 제한된 기본 예산을 적용한다."""
    assert ChatConfig().source_focus_max_calls == 4
    assert ChatConfig(source_focus_max_calls=1).source_focus_max_calls == 1
    assert ChatConfig().source_focus_max_items == 3


@pytest.mark.parametrize("invalid", [0, 5, -1, True, "4", 2.5])
def test_focus_selection_rejects_invalid_call_budget(invalid: object) -> None:
    """상한 초과와 암묵적 형변환을 허용하지 않는다."""
    with pytest.raises(ValidationError):
        ChatConfig(source_focus_max_calls=invalid)


@pytest.mark.parametrize("invalid", [0, 17, True, "3", 2.5])
def test_focus_selection_rejects_invalid_item_budget(invalid: object) -> None:
    """각 초점의 직접 선택 수에도 정수 상한을 적용한다."""
    with pytest.raises(ValidationError):
        ChatConfig(source_focus_max_items=invalid)
