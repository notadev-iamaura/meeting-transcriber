"""실제로 선택한 답변 출처의 범위를 생성 문구와 분리해 전달한다."""

import re
import unicodedata
from enum import StrEnum


class ChatSourceScope(StrEnum):
    """공개 문서의 부재·업무 상태가 아닌 이번 답변의 실제 분기 기록."""

    WIKI_FALLBACK = "wiki_fallback"


_MEETING_DATE = re.compile(
    r"(?<![a-zA-Z0-9])(?:(?:\d{4}년\s*)?\d{1,2}월\s*\d{1,2}일|"
    r"(?:\d{4}[./-])?(?:0?[1-9]|1[0-2])[./-](?:0?[1-9]|[12]\d|3[01])일?)"
    r"\s*(?:(?:오전|오후)?\s*\d{1,2}(?:시|:)\s*(?:\d{1,2}분?)?\s*)?"
    r"(?:의\s*|에\s*(?:열린|했던|진행한)\s*)?회의"
)
_WH_WORD = re.compile(r"어떻|어떡|어디|언제|얼마|누구|누가|무엇|어떤|어느\s|왜\b|\b뭐")
_EMBEDDED_WH = re.compile(r"(?:는지|은지|할지|될지|갈지|낼지|볼지|을지|인지)(?:를|에|의)?\s+\S")
_ENGLISH_MEETING_LOOKUP = re.compile(
    r"^(?:(?:please\s+)?(?:find|search(?:\s+for)?|list|show(?:\s+me)?)\s+(?:the\s+)?"
    r"meetings?\s+(?:about|on|regarding|where\s+we\s+discussed)|"
    r"which\s+meetings?\s+(?:covered|discussed|mentioned))\s+(.+?)[?.!]*$",
    re.I,
)
_ENGLISH_QUESTION = re.compile(
    r"^(?:(?:who|what|when|where|why|how|which|whose|whom)\b|"
    r"(?:am|is|are|was|were|can|could|will|would|should|have|has|did|does|do)\s+"
    r"(?:i|we|you|he|she|it|they|there|the|a|an|any|anyone|this|that|our|my|your)\b|"
    r"(?:please\s+|(?:show|tell|give)\s+me\b|(?:summari[sz]e|explain)\s+)|"
    r"(?:find|search(?:\s+for)?|list|show)\s+(?:the\s+)?meetings?\b)",
    re.I,
)
_ENGLISH_AUXILIARY = re.compile(
    r"^(?:am|is|are|was|were|can|could|will|would|should|have|has|had|did|does|do)\s",
    re.I,
)


def meeting_lookup_topic(query: str) -> str | None:
    """회의 탐색 요청이나 독립 명사구에서 주제만 가져오고 일반 질문은 보존한다."""
    text = unicodedata.normalize("NFC", query).strip()
    text = re.sub(
        r"^(?:(?:주제|안건)|(?:관련\s*)?회의(?:를)?\s*(?:찾아\s*(?:줘|주세요)|검색))\s*[:：]\s*",
        "",
        text,
    )
    reversed_topic = re.fullmatch(
        r"(?:어느|어떤)\s*회의(?:들)?에서\s*(.+?)(?:을|를)?\s*"
        r"(?:다뤘|논의했|얘기했|이야기했|언급했)(?:었)?(?:어|지|나|나요|는지)?[?？.]*",
        text,
    )
    if reversed_topic:
        text = f"{reversed_topic[1]} 관련 회의 찾아줘"
    if not text or re.search(r"meeting_\d", text) or _MEETING_DATE.search(text):
        return None
    if re.search(r"^(?:그(?:거|건|럼|\s)|이(?:거|건|번\s회의)|방금|위에서|이어서|다시\s)", text):
        return None
    if re.search(
        r"(?:위키|wiki)(?:에서|의|만|에\s)|(?:위키|wiki)\s*(?:문서|페이지)에서", text, re.I
    ):
        return None
    english_lookup = _ENGLISH_MEETING_LOOKUP.fullmatch(text)
    explicit = re.match(
        r"^(?P<topic>.+?)\s*"
        r"(?:(?:에\s*(?:관해|대해)|(?:과|와)\s*관련해|을|를)?\s*"
        r"(?:(?:논의|이야기|얘기|언급|검토|다룬|다뤘|다루|말했|나왔|나온|정리)"
        r"(?:했(?:던|었(?:던)?)?|하던|했던|한|된|했던|었던|던|는)?|관련(?:된)?)\s*)?"
        r"회의(?:들)?(?:를|가|는|에서|였는지|인지|라도|랑|와|도)?\s*"
        r"(?:(?:좀\s*)?(?:찾|검색|보여|알려|정리|요약|뽑)|결정|후속|일정|어디|언제|있(?:어|었|나|는지)|뭐|무엇|[?？]|$)",
        text,
    )
    if english_lookup:
        topic = english_lookup[1]
    elif explicit:
        topic = explicit["topic"]
    else:
        if re.search(r"(?:은|는|을|를)[?？]+$", text):
            return None
        ends_in_question = text.endswith(("?", "？"))
        text = re.sub(r"[.,!。！？?]+$", "", text).rstrip()
        if len(text.split()) > 16 or len(text) > 110 or re.search(r"[?？!。\n]", text):
            return None
        # WHO와 CAN 등은 질문 첫 단어이면서 전문 분야의 약어이기도 하다.
        if (
            _ENGLISH_QUESTION.match(text) or (ends_in_question and _ENGLISH_AUXILIARY.match(text))
        ) and (ends_in_question or text.split()[0] not in {"WHO", "CAN", "IS", "DO"}):
            return None
        question = _WH_WORD.search(text)
        if question and (
            not _EMBEDDED_WH.search(text, question.end())
            or re.search(r"\s(?:설명|답변|확인|궁금|아는\s*사람|기억나)$", text)
        ):
            return None
        if re.search(
            r"(?:알려|줘|주세요|주세|해봐|찾아봐|해볼래|"
            r"할까|일까|을까|됐나|했나|했지|됐지|인지|였지|나요|지요|인가|냐|"
            r"예요|에요|해요|어요|세요|니다|했어|됐어|였어|왔어|났어|거야|이야|있어|없어|"
            r"맞아|했니|할래|어때|해라|해줄래|주실래요?|줄래요?|"
            r"부탁해(?:요)?|부탁드려(?:요)?|"
            r"설명해|정리해|알려줄래|궁금해)$|"
            r"^(?:안녕|고마워|감사|질문(?:입니다|[\dA-Z])?$|테스트$)",
            text,
        ):
            return None
        topic = text
    topic = topic.strip(" \t'\"‘’“”,.?!")
    if not re.search(r"[가-힣a-zA-Z0-9]", topic) or re.fullmatch(
        r"(?:(?:최근|지난|오늘|어제|내일|모레|모든|전체|어떤|마지막|"
        r"(?:저번|지난|이번|다음)(?:\s*(?:번|주|달|분기))?|그|정리|요약|"
        r"녹음|전사|회의|내용|일정|결정사항|후속\s*작업|액션\s*아이템|결정|"
        r"프로젝트|주제|안건|답변|없는|긴|짧은|검색|테스트|질문|알려줘|"
        r"the|all|last|next|this|recent|previous|meetings?|weeks?|months?|"
        r"agendas?|topics?|summar(?:y|ies)|decisions?|action\s+items?|schedule)(?:\s+|$))+",
        topic,
        re.I,
    ):
        return None
    return topic


def source_scope_note(scope: ChatSourceScope | None) -> str:
    """라우터가 실제 기록한 범위만 안내하고 전체 자료의 부재를 추론하지 않는다."""
    if scope is ChatSourceScope.WIKI_FALLBACK:
        return (
            "이번 검색에서 답변에 사용할 공개 Wiki 근거를 선택하지 못해 회의 원문으로 답합니다. "
            "아래 원문을 공개 문서에서 확정된 결정으로 확인한 결과는 아닙니다."
        )
    return ""
