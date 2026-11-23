"""명사구 주제를 실제 의문·후속 질문·명시 회의 범위와 구분한다."""

import pytest

from core.chat_scope import meeting_lookup_topic


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "STT에서 누가 먼저 끊었는지 보는 통화 종료 사유 코드",
            "STT에서 누가 먼저 끊었는지 보는 통화 종료 사유 코드",
        ),
        ("어디서 반품됐는지 추적하는 라벨", "어디서 반품됐는지 추적하는 라벨"),
        ("배송이 왜 지연됐는지 확인하는 절차", "배송이 왜 지연됐는지 확인하는 절차"),
        ("마감이 언제인지 표시하는 캘린더", "마감이 언제인지 표시하는 캘린더"),
        ("QR 인쇄 품질.", "QR 인쇄 품질"),
        ("2~3일 배송 보장?", "2~3일 배송 보장"),
        ("2-3일 배송 보장 관련 회의 찾아줘", "2-3일 배송 보장"),
        ("11월 22일 출시 프로모션 관련 회의 찾아줘", "11월 22일 출시 프로모션"),
        ("회의 찾아줘: 3/4 사이즈 포장 규격", "3/4 사이즈 포장 규격"),
        ("회의를 찾아 주세요：오전 입고 절차", "오전 입고 절차"),
        ("클라우드 정산", "클라우드 정산"),
        ("QR refund policy?", "QR refund policy"),
        ("WHO safety guidance", "WHO safety guidance"),
        ("다음 배송일 표시 관련 회의 찾아줘", "다음 배송일 표시"),
        ("find meetings about refund policy", "refund policy"),
        ("Which meetings discussed CAN bus diagnostics?", "CAN bus diagnostics"),
    ],
)
def test_nominal_topic_with_subordinate_question_or_numbers(query: str, expected: str) -> None:
    """주제 안의 의문사·기간·제품 제원과 명사구 끝의 문장부호를 허용한다."""
    assert meeting_lookup_topic(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        "누가 먼저 끊었어?",
        "누가 먼저 끊었는지 알려줘.",
        "어디서 반품됐는지 설명",
        "이 마감이 언제인지 아는 사람?",
        "위키에서 누가 정했는지 보는 코드",
        "그 회의의 종료 사유 코드",
        "회의 찾아줘: 11월 22일 회의",
        "2026-11-22 오전 9시 30분 회의 요약해줘",
        "8/19에 열린 회의에서 일정 찾아줘",
        "프로젝트 일정?",
        "지난번 예산은?",
        "예산이 확정됐어?",
        "다음 회의는 언제야?",
        "다음 주 회의 일정 알려줘",
        "지난번 회의 결정사항 알려줘",
        "영수증 마스킹 여부를 확인해 주실래요?",
        "영수증 마스킹 일정을 정리 부탁해",
        "What did we decide last week?",
        "WHAT DID WE DECIDE?",
        "Who is responsible for billing",
        "Was shipping approved?",
        "Show me the previous action items",
        "Find meetings",
        "Find meetings about the next meeting",
        "주제 관련 회의 찾아줘",
    ],
)
def test_ordinary_questions_and_explicit_scopes_stay_in_existing_route(query: str) -> None:
    """실제 질문과 특정 날짜·위키·기존 대화를 가리키는 범위를 유지한다."""
    assert meeting_lookup_topic(query) is None
