"""실제로 선택한 답변 출처의 범위를 생성 문구와 분리해 전달한다."""

from enum import StrEnum


class ChatSourceScope(StrEnum):
    """공개 문서의 부재·업무 상태가 아닌 이번 답변의 실제 분기 기록."""

    WIKI_FALLBACK = "wiki_fallback"


def source_scope_note(scope: ChatSourceScope | None) -> str:
    """라우터가 실제 기록한 범위만 안내하고 전체 자료의 부재를 추론하지 않는다."""
    if scope is ChatSourceScope.WIKI_FALLBACK:
        return (
            "이번 검색에서 답변에 사용할 공개 Wiki 근거를 선택하지 못해 회의 원문으로 답합니다. "
            "아래 원문을 공개 문서에서 확정된 결정으로 확인한 결과는 아닙니다."
        )
    return ""
