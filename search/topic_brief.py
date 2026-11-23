"""한 주제에 관한 회의별 요약과 검증된 원문 인용을 구성한다."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass

from search.source_answers import SourceCandidate, selected_with_replies
from search.source_query import topic_content_terms

logger = logging.getLogger(__name__)

FACETS = {"decision": "결정사항", "action": "후속 작업", "schedule": "일정"}
_TIME_SUFFIX = r"(?=$|[\s.,?!~～/·]|부터|까지|이?후|안에|내에|이내|정도|가량|쯤|경에|동안|간\b|마다|에|로|은|는|이\s|을\s|입니다)"
_SPAN = r"(?<![a-zA-Z0-9])(?:\d+(?:[.]\d+)?(?:\s*[~～,-]\s*\d+)?|한두|두세|이삼|한|두|세|네)\s*(?:영업일|개월|주일|시간|분기|일|주|분)"
_CALENDAR = re.compile(
    r"(?:[월화수목금토일]요일|(?:이번|다음|다다음)\s*(?:주말|주|달|분기)|내일|모레|오늘|"
    r"월말|월초|연말|상반기|하반기|금주|차주|일주일|이틀|사흘|"
    r"\d{1,2}월\s*\d{1,2}일|\d{1,2}시(?:\s*\d{1,2}분)?|" + _SPAN + r")" + _TIME_SUFFIX
)
_DECIDED = re.compile(
    r"하기로|기로\s*(?:했|하|정|결)|결정(?:했|하였|됐|되었|한|된)|"
    r"확정(?:했|하였|됐|되었|한|된)|합의(?:했|하였|한|된)|정했|정해졌"
)
_ACTION = re.compile(
    r"겠습니|할게|해\s*볼게|드릴게|보낼게|해줄게|드리겠|"
    r"해\s*주시(?:죠|기\s*바랍니다|면\s*(?:감사|좋겠))|해\s*주세|부탁\s*드|하기로|예정입|하려고\s*합니|도록\s*하겠"
)
_HANDOFF = re.compile(
    r"(?:담당자(?:분)?|[가-힣A-Za-z]{2,}님)(?:에게|께|한테).{0,24}"
    r"(?:문의하|확인하|전달하|말씀드리|여쭤보)(?:고|겠|기로|려고|도록)"
)
_DELIVERABLE = re.compile(
    r"(?:전달|공유|제출|확인|배포|발송|요청|보고|회신|게시|수정|검토|초대|준비|작성)(?:[하해드했할]|해\s*드)"
)
_ASSESSMENT = re.compile(
    r"(?:알|구별할|구분할|확인할|판단할|파악할|조회할|추적할)\s*수(?:는|가)?\s*없|"
    r"(?:구분|확인|조회|판단|추적)(?:해\s*주기|하기)(?:는|가)?\s*어렵|"
    r"(?:기존|종전|원래).{0,30}(?:대신|에서).{0,25}(?:전환|바꾸|변경)"
)
_NONFINAL = re.compile(r"(?:할까요|어떨까요|건가요|인가요|나을까요|하시겠어요)[.。?!？]*$|[?？]$")
_PROPOSAL = re.compile(
    r"필요(?:할|하다고|합니다|해요)|해야\s*(?:할|됩니|됩니|해요)|하는\s*(?:방향|게\s*좋)"
)
_TRANSITION = re.compile(
    r"다음\s*(?:안건|주제|아이템|이슈|건은)|별개(?:의\s*안건|\s*주제)|다른\s*(?:안건|주제)|주제를\s*바꿔"
)
_DURATION_QUESTION = re.compile(
    r"얼마나\s*(?:걸|소요)|(?:며칠|몇\s*(?:일|주|시간)).{0,10}(?:걸|필요|소요)"
)
_DURATION_REPLY = re.compile(
    r"(?:주시면|받으면|받고|자료|완료되면|조건|준비되면).{0,42}(?:걸릴|소요|정도)|\d+\s*(?:일|주|시간).{0,14}(?:걸릴|소요)"
)

_SCHEDULED_ACTION = (
    r"설치|공유|제출|진행|추진|출시|검토|전달|공개|오픈|배포|발송|방문|예약|제공|신청|"
    r"완료|시작|재개|해결|점검|발표|회의|미팅|교육|면담|회신|착수|정리"
)
_FUTURE = re.compile(
    r"겠습|겠어|할게|드릴게|보낼게|(?:하|받|넘기|드리|보내)기로|"
    r"(?:예정|계획|목표|기한|마감)(?:입니|이에|예요|으로|으로써|로\s|입니다|[.。]?$)|"
    r"(?:할|될|갈|받을|있을|끝날|걸릴|드릴|보낼|나올|볼|줄|마칠|날)\s*(?:것|거|예정|계획)|"
    r"(?:" + _SCHEDULED_ACTION + r")(?:합니다|해요|하죠|할|하자|하기로|합시다|[.。]?$)|"
    r"까지.{0,30}(?:해야|해주세요|해\s*주세|부탁|해\s*주시|주세|바랍니|필요)|"
    r"(?:요일|내일|모레|주)(?:에|부터|까지)?\s*(?:합니다|됩니다)"
)
_AFTER_EVENT = re.compile(
    r"(?:승인|리뷰|검토|검수|배포|공유|전달|완료|종료|도착|수신|오픈)\s*"
    r"(?:(?:이|가|을|를)\s*)?(?:이후|직후|후(?:에|부터|\s|$)|뒤|되면|나면|끝나면|마치면|(?:되|나)는\s*대로)|"
    r"받(?:으면|는\s*대로|은\s*(?:뒤|후))|(?:완료|종료|도착)하면|끝나는\s*대로"
)
_RECURRING = re.compile(
    r"(?:매주|매월|매달|매일|매년|(?:주|월)\s*\d+\s*회|정기적으로)" + _TIME_SUFFIX
)
_BOOKED = re.compile(
    r"잡(?:았|혔|혀\s*있(?:습|어|죠|는데))|(?:예약|배정|확정)(?:했|됐|되어\s*있(?:습|어|죠|는데))|"
    r"(?:회의|미팅|면담)(?:을|를)?\s*(?:하나\s*)?(?:세팅|설정|예약)(?:을|를)?\s*했"
)
_REPORTED_PROPOSAL = re.compile(
    r"(?:[가-힣]+자고|(?:만나|논의|검토|공유|방문|시작|제출|진행|보내|참석).{0,8}(?:제안|요청))(?:\s*(?:말씀)?(?:하셨|했|해|하|드렸)|했|드렸)"
)
_PAST = re.compile(r"어제|지난\s*(?:[월화수목금토일]요일|주|달|분기)|전날|당시")
_MEASUREMENT = re.compile(
    r"(?:시간|주일|주|일|개월|분)\s*(?:기준|동안의|이내의)|"
    r"(?:시간|주일|주|일|개월|분)(?:간|\s*동안)?\s*(?:통계|데이터|수치|평균|누적)|"
    r"(?:기준|동안)\s*(?:집계|관측|측정)한|(?:집계|관측|측정)\s*기간"
)
_WITHDRAWN = re.compile(
    r"(?:취소|철회|무산)(?:됐|되었|했)|(?:계획|예정)(?:은|이)?\s*없|"
    r"(?:확정|결정|합의)(?:된?\s*(?:게|것이)|되지)\s*(?:없|않)"
)
_SETTLED = re.compile(
    r"하는\s*(?:걸로|것으로)|(?:그렇게|그걸로|그대로).{0,10}(?:진행|하기|하죠|가죠|합시다)|(?:유지|제외|포함|적용|변경)(?:하죠|합니다|할게요|하겠습니다)"
)
_RULE_END = r"(?:합니다|해요|한다|됩니다|돼요|된다|입니다|이에요|이다|한다는\s*뜻입니다)[.。!\s]*$"
_OPERATING_RULE = re.compile(
    r"(?:기준(?:으로|에\s*따라)|에\s*따라).{0,32}?"
    r"(?:보|판단하|판정하|평가하|분류하|처리하|선택하|정하)면\s*(?:된다|돼요|됩니다)[.。!\s]*$|"
    r"(?:기존|기왕|현재|원래).{0,36}(?:그대로\s*)?유지(?:를\s*)?" + _RULE_END + r"|"
    r"(?:에만|에게만|에서만|에\s*한해|만을\s*대상으로).{0,28}?"
    r"(?:적용|운영|사용|포함|허용|허가|제공)(?:(?:을|를)\s*)?" + _RULE_END
)
_TRIAL_PLAN = re.compile(
    r"(?:시범|시험|한시)\s*(?:운영|도입|적용|실시)|파일럿|파일롯|\bpilot\b", re.I
)
_TRIAL_START = re.compile(
    r"(?:시작|진행|운영|실시)(?:합니다|해요|한다|하겠습니다|하기로\s*했(?:습니다|어요))[.。!\s]*$"
)
_NON_BINDING = re.compile(
    r"예를\s*들|예시로|가령|만약|해\s*보면\s*어떨|(?:면|으면)\s*좋|제안|검토\s*중|"
    r"미확정|미정|잠정|(?:아직|확정된|결정된).{0,18}(?:않|없|아니)|"
    r"하기로\s*(?:한\s*(?:건|것은)\s*아니|하지\s*않)|(?:할|될|한|인)\s*것\s*같"
)
_SCOPE_NOTE = re.compile(r"(?:기존.{1,32}유지|에만\s*적용)[.。\s]*$")


def _stated_rule(text: str) -> bool:
    """시행 주체가 현재 기준 또는 한정된 운영을 서술한 완전한 원문인지 확인한다."""
    if _NON_BINDING.search(text) or _NONFINAL.search(text) or _WITHDRAWN.search(text):
        return False
    if _OPERATING_RULE.search(text):
        return True
    if not _TRIAL_PLAN.search(text):
        return False
    if _TRIAL_START.search(text):
        return True
    # 명사형 회의 메모는 기간과 대상 규모를 둘 다 명시한 운영 방침만 채택한다.
    return bool(
        _CALENDAR.search(text)
        and re.search(r"(?:\d+|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*(?:명|개소|곳)", text)
        and re.search(r"(?:시작|진행)[.。\s]*$", text)
    )


def _planned_clause(clause: str) -> bool:
    """같은 절 안에서 시간 표현이 예약·목표·예상 소요 기간과 결합하는지 확인한다."""
    if _MEASUREMENT.search(clause):
        return False
    calendar = _CALENDAR.search(clause)
    after_event = _AFTER_EVENT.search(clause)
    if not (calendar or after_event or _RECURRING.search(clause)):
        return False
    booked = bool(calendar and _BOOKED.search(clause))
    if _PAST.search(clause) and not (booked and re.search(r"내일|모레|다음\s*(?:주|달)", clause)):
        return False
    if calendar and (
        re.search(r"(?:걸릴|소요될)\s*(?:것|거|예정)", clause)
        or (after_event or re.search(r"주시면", clause))
        and re.search(r"걸려요|걸립니다|소요됩니다", clause)
    ):
        return True
    return booked or bool(_FUTURE.search(clause) or calendar and _REPORTED_PROPOSAL.search(clause))


def _planned_time(text: str) -> bool:
    """질문·철회된 계획·이미 지난 사건에서 날짜만 떼어 일정으로 표시하지 않는다."""
    for sentence in re.split(r"(?<=[.!?。？！])\s+", text):
        if _NONFINAL.search(sentence) or _WITHDRAWN.search(sentence):
            continue
        missed = re.search(
            r"(?:안|못)\s*(?:오셨|오셔|왔|와서)|불참했|참석하지\s*(?:못했|않았)", sentence
        )
        if missed:
            sentence = sentence[missed.end() :]
        clauses = re.split(r"[,，]\s*|(?<=[했었였았봤]는데)\s+|(?<=[했었였았봤]고)\s+", sentence)
        if any(_planned_clause(clause) for clause in clauses):
            return True
    return False


_IMPLICIT_PROMISE = re.compile(
    r"^(?:(?:네|예|아|그럼|그러면|제가|저희가|그걸|이걸|(?:이거|그거|이것|그것)(?:를|을|는)?|"
    r"한\s*번|1\s*번|다시|일단|먼저|좀|[가-힣]{1,10}(?:해서|하고))[,，\s]+)*"
    r"[가-힣]{1,12}(?:겠습니다|겠어요|할게요|릴게요|낼게요)[.!\s]*$"
)
_REQUESTED = re.compile(
    r"싶다고|원한다고|궁금하다(?:고|는데)|필요하다(?:고|는데)|달라고|달라(?:는|고)|"
    r"해\s*주(?:세요|실\s*수|시겠)|주시겠|부탁|물어보|여쭤보"
)
_DISCOURSE_SHIFT = re.compile(
    r"다른\s*(?:이야기|얘기|팀|건은)|(?:그건|그거는)\s*(?:됐|별개)|"
    r"(?:별도로|한편|반면|예전에|지난번에는|저번에는|이전에|당시에는)"
)


def _request_before_promise(
    promise: SourceCandidate, turns: list[SourceCandidate], topic: str
) -> tuple[SourceCandidate, ...]:
    """대상이 생략된 약속에 바로 앞 실제 요청과 문법상 이어진 앞 절만 붙인다."""
    if not _IMPLICIT_PROMISE.fullmatch(promise.quote.text.strip()):
        return ()
    nearby: dict[int, list[SourceCandidate]] = {}
    for turn in turns:
        if (
            turn.meeting_id == promise.meeting_id
            and 0 < promise.source_index - turn.source_index <= 2
        ):
            nearby.setdefault(turn.source_index, []).append(turn)
    prior = nearby.get(promise.source_index - 1, [])
    if len(prior) != 1 or promise.quote.speaker in {"", "UNKNOWN"}:
        return ()
    request = prior[0]
    if (
        request.quote.speaker in {"", "UNKNOWN"}
        or _TRANSITION.search(request.quote.text)
        or _DISCOURSE_SHIFT.search(request.quote.text)
        or not (_NONFINAL.search(request.quote.text) or _REQUESTED.search(request.quote.text))
    ):
        return ()
    leading = nearby.get(promise.source_index - 2, [])
    if len(leading) == 1:
        intro = leading[0]
        terms = [
            w.casefold() for w in topic_content_terms(topic) if len(w) >= 2 and w not in _GENERIC
        ]
        grounded_pair = all(
            any(word in turn.quote.text.casefold() for word in terms) for turn in (intro, request)
        )
        if (
            intro.quote.speaker == request.quote.speaker
            and (
                grounded_pair
                or re.match(r"(?:그|이)(?:걸|거|건|것|쪽|때|중|걸로|부분)", request.quote.text)
            )
            and not _TRANSITION.search(intro.quote.text)
            and not _DISCOURSE_SHIFT.search(intro.quote.text)
            and re.search(
                r"(?:는데요|인데요|관련해서|대해서|에게서|에서는|면서|하려고|하려면)[,，.\s]*$",
                intro.quote.text,
            )
            and not re.match(r"(?:그런데|하지만|한편|대신|방금|지금부터)", request.quote.text)
        ):
            return intro, request
    return (request,)


def _fact_context(
    key: str,
    selected: tuple[SourceCandidate, ...],
    turns: list[SourceCandidate],
    *,
    topic: str = "",
) -> tuple[SourceCandidate, ...]:
    """선택한 약속의 생략된 대상·미완결 다음 절을 실제 인접 원문에서만 복원한다."""
    if not selected or key == "schedule" or len(selected) >= 3:
        return selected
    by_index = {(t.meeting_id, t.source_index): t for t in turns if not t.context_only}
    actual = {t.id: t for t in turns}
    result = {t.id: t for t in selected}
    for t in selected:
        if key == "action" and t.quote.speaker not in {"", "UNKNOWN"}:
            # 한 사람이 여러 전사 구간에 걸쳐 '담당자가 판단하셔서 … 경우에는
            # … 공유해 주세요'라고 말하면 조건·주체가 남은 앞 절도 함께 인용한다.
            for offset in (1, 2):
                prev = by_index.get((t.meeting_id, t.source_index - offset))
                if (
                    not prev
                    or prev.quote.speaker != t.quote.speaker
                    or len(result) >= 3
                    or _TRANSITION.search(prev.quote.text)
                    or not re.search(
                        r"(?:셔서|해서|한\s*뒤에|을\s*경우[에는]*|것들(?:은|을)|대상(?:은|을))[,，…\s]*$",
                        prev.quote.text,
                    )
                ):
                    break
                result[prev.id] = prev
        if key == "decision" and _ASSESSMENT.search(t.quote.text) and len(result) < 3:
            clarification = by_index.get((t.meeting_id, t.source_index + 1))
            if (
                clarification
                and re.match(r"^(?:그게|이게|그건|그거|그렇|그러니까)", clarification.quote.text)
                and re.search(r"없으면|있으면|경우", clarification.quote.text)
                and _ASSESSMENT.search(clarification.quote.text)
                and not _TRANSITION.search(clarification.quote.text)
            ):
                result[clarification.id] = clarification
        if (
            key == "action"
            and len(result) < 3
            and re.search(
                r"(?:드리|보내|넘기|받|시키|건네|전달하|검토하|정리하)고[,，…\s]*$", t.quote.text
            )
        ):
            following = by_index.get((t.meeting_id, t.source_index + 1))
            if (
                following
                and following.quote.speaker == t.quote.speaker
                and t.quote.speaker not in {"", "UNKNOWN"}
                and not _TRANSITION.search(following.quote.text)
            ):
                result[following.id] = following
        for context in selected_with_replies([t.id], turns, 3):
            if abs(context.source_index - t.source_index) <= 1 and not _TRANSITION.search(
                context.quote.text
            ):
                if len(result) < 3:
                    result[context.id] = actual[context.id]
        if key == "action":
            for context in reversed(_request_before_promise(t, turns, topic)):
                if len(result) < 3:
                    result[context.id] = context
    return tuple(sorted(result.values(), key=lambda t: t.source_index))


_GENERIC = {
    "작업",
    "관련",
    "회의",
    "안건",
    "방식",
    "방법",
    "상태",
    "진행",
    "처리",
    "공유",
    "이슈",
    "논의",
    "현황",
    "일정",
    "기준",
    "결정사항",
    "후속",
    "내용",
}


@dataclass(frozen=True)
class TopicFact:
    """표시할 한 문장과 그 문장에 배정된 실제 원문 발언."""

    text: str
    evidence: tuple[SourceCandidate, ...]


@dataclass(frozen=True)
class MeetingBrief:
    """주제를 특정한 발언 하나와 항목별로 확인된 사실."""

    anchor: SourceCandidate
    facts: dict[str, TopicFact]
    used_model: bool
    clarification: SourceCandidate | None = None


def _extractive(turns: tuple[SourceCandidate, ...]) -> str:
    """모델 문장을 확인할 수 없으면 선택 근거의 원래 조건과 어미를 보존한다."""
    return " ".join(turn.quote.text for turn in turns)


def _grounded_options(
    topic: str, turns: list[SourceCandidate]
) -> tuple[SourceCandidate | None, dict[str, list[SourceCandidate]]]:
    """주제 발언과 바로 인접한 약속을 연결하고, 기간 질문에 대한 조건부 답을 별도로 찾는다."""
    words = [w.casefold() for w in topic_content_terms(topic) if len(w) >= 2]
    terms = [w for w in words if w not in _GENERIC] or words
    patterns = [
        re.compile(
            rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])" if w.isascii() else re.escape(w), re.I
        )
        for w in terms
    ]
    matches = {t.id: {w.pattern for w in patterns if w.search(t.quote.text)} for t in turns}
    direct = [t for t in turns if matches[t.id] and not t.context_only]
    options: dict[str, list[SourceCandidate]] = {k: [] for k in FACETS}
    if not direct or len(set.union(*(matches[t.id] for t in direct))) * 2 < len(terms):
        return None, options
    cut = [t.source_index for t in turns if _TRANSITION.search(t.quote.text)]

    def connected(a: SourceCandidate, b: SourceCandidate) -> bool:
        lo, hi = sorted([a.source_index, b.source_index])
        return a.meeting_id == b.meeting_id and not any(lo < i <= hi for i in cut)

    def closest(t: SourceCandidate) -> int:
        return min(
            (abs(t.source_index - d.source_index) for d in direct if connected(t, d)),
            default=10**6,
        )

    distances = {t.id: closest(t) for t in turns}
    anchor = max(
        direct,
        key=lambda t: (
            len(matches[t.id]),
            not bool(_NONFINAL.search(t.quote.text)),
            bool(
                _DECIDED.search(t.quote.text)
                or _SETTLED.search(t.quote.text)
                or _PROPOSAL.search(t.quote.text)
            ),
            len(t.quote.text) > 25,
        ),
    )

    def rank(t: SourceCandidate) -> tuple[int, int, int]:
        return len(matches[t.id]), -distances[t.id], t.source_index

    near = [
        t
        for t in turns
        if distances[t.id] <= 2 and not t.context_only and not _NONFINAL.search(t.quote.text)
    ]
    operating = {t.id for t in near if matches[t.id] and _stated_rule(t.quote.text)}
    options["action"] = sorted(
        (
            t
            for t in near
            if _ACTION.search(t.quote.text)
            or _HANDOFF.search(t.quote.text)
            or t.id in operating
            and _TRIAL_PLAN.search(t.quote.text)
        ),
        key=lambda t: (bool(_DELIVERABLE.search(t.quote.text)), rank(t)),
        reverse=True,
    )
    options["decision"] = sorted(
        (
            t
            for t in near
            if _DECIDED.search(t.quote.text)
            or _SETTLED.search(t.quote.text)
            or t.id in operating
            or matches[t.id]
            and (
                _PROPOSAL.search(t.quote.text)
                or _ASSESSMENT.search(t.quote.text)
                or _SCOPE_NOTE.search(t.quote.text)
            )
        ),
        key=lambda t: (
            t.id in operating,
            bool(_DECIDED.search(t.quote.text) or _SETTLED.search(t.quote.text)),
            rank(t),
        ),
        reverse=True,
    )
    duration_questions = [
        q
        for q in turns
        if _DURATION_QUESTION.search(q.quote.text)
        and any(
            0 <= q.source_index - a.source_index <= 8 and connected(a, q)
            for a in options["action"]
        )
    ]
    duration_answers = [
        t
        for t in turns
        if not _NONFINAL.search(t.quote.text)
        and _DURATION_REPLY.search(t.quote.text)
        and any(
            0 < t.source_index - q.source_index <= 3 and connected(q, t)
            for q in duration_questions
        )
    ]

    def continuation(t: SourceCandidate) -> bool:
        if matches[t.id]:
            return True
        if re.match(r"^(?:이거|그거)(?:를|는)\s", t.quote.text):
            return not _DISCOURSE_SHIFT.search(t.quote.text) and not _TRANSITION.search(
                t.quote.text
            )
        # 새 주어가 생긴 인접 발언의 날짜를 앞 주제에 붙이지 않는다.
        subject = re.search(r"(?:^|\s)([가-힣A-Za-z]{2,})(?:은|는)\s", t.quote.text)
        if subject and subject[1] not in {
            "저희",
            "이번",
            "이거",
            "그거",
            "일정",
            "기한",
            "이것",
            "그것",
        }:
            return any(
                abs(d.source_index - t.source_index) <= 2 and subject[1] in d.quote.text
                for d in direct
            )
        return True

    options["action"] = [t for t in options["action"] if continuation(t)]
    # 담당자가 바로 이어서 잡은 확인 미팅·기한도 같은 업무의 후속 시점이다.
    # 이웃이라는 이유만으로 타 화자나 다른 안건의 일정을 승격하지 않는다.
    after_action = [
        t
        for t in turns
        if t not in near
        and any(
            a.quote.speaker == t.quote.speaker
            and a.quote.speaker not in {"", "UNKNOWN"}
            and t.source_index == a.source_index + 1
            and connected(a, t)
            for a in options["action"]
        )
        and re.match(
            r"^(?:(?:네|일단|그래서|그러면)[,\s]+)*(?:제가|저희가|이거|그거|이를|그걸)",
            t.quote.text,
        )
        and not _DISCOURSE_SHIFT.search(t.quote.text)
    ]
    options["schedule"] = duration_answers + sorted(
        (
            t
            for t in [*near, *after_action]
            if t not in duration_answers and _planned_time(t.quote.text) and continuation(t)
        ),
        key=rank,
        reverse=True,
    )
    return anchor, options


def _grounded_fact(key: str, selected: tuple[SourceCandidate, ...]) -> TopicFact:
    """조건·화자·숫자를 바꾸지 않고 발언을 보여주며 합의가 불분명하면 표시한다."""
    evidence = _extractive(selected)
    if key == "decision" and (
        _NON_BINDING.search(evidence)
        or not (_DECIDED.search(evidence) or _SETTLED.search(evidence) or _stated_rule(evidence))
    ):
        evidence = f"확정 여부를 확인할 수 없는 논의입니다. {evidence}"
    if (
        key == "schedule"
        and _REPORTED_PROPOSAL.search(evidence)
        and not (_DECIDED.search(evidence) or _BOOKED.search(evidence))
    ):
        evidence = f"제안된 일정 · {evidence}"
    return TopicFact(evidence, selected)


def _output_ids(value: object) -> list[int]:
    """번호·번호 목록·명시적인 ids 필드만 받아 JSON 표현의 차이를 흡수한다."""
    if isinstance(value, dict):
        value = value.get("ids", value.get("id"))
    values = value if isinstance(value, list) else [value]
    if len(values) > 3:
        return []
    ids = []
    for item in values:
        if type(item) is int:
            ids.append(item)
        elif isinstance(item, str) and re.fullmatch(r"[0-9]{1,6}", item):
            ids.append(int(item))
        else:
            return []
    return ids


def parse_topic_brief(topic: str, raw: str, turns: list[SourceCandidate]) -> MeetingBrief | None:
    """다른 회의·숨겨진 번호를 배제하고 각 항목의 원문과 표시 문장을 결합한다."""
    by_id = {t.id: t for t in turns}
    body = raw.strip() if isinstance(raw, str) else ""
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", body, re.S | re.I)
    try:
        value = json.loads(fence[1] if fence else body)
    except (TypeError, ValueError):
        value = None
    parsed = value if isinstance(value, dict) else {}
    # 정해진 키의 타입과 번호만 기록한다. 모델 문구·원문·질문은 남기지 않는다.
    shape = {
        k: (type(parsed[k]).__name__, _output_ids(parsed[k])) if k in parsed else ("missing", [])
        for k in ("anchor", *FACETS)
    }
    group = hashlib.sha256(turns[0].meeting_id.encode()).hexdigest()[:12] if turns else ""
    logger.info(f"주제 요약 선택 결과: group={group}, shape={shape}, rows={len(turns)}")
    if "anchor" in parsed and parsed["anchor"] is None:
        return None
    literal, options = _grounded_options(topic, turns)
    anchor_ids = _output_ids(parsed.get("anchor"))
    model_anchor = by_id.get(anchor_ids[0]) if len(anchor_ids) == 1 else None
    if model_anchor and model_anchor.context_only:
        model_anchor = None
    anchor = literal or model_anchor
    if anchor is None:
        return None
    facts: dict[str, TopicFact] = {}
    used_model = model_anchor == anchor
    for key in FACETS:
        ids = _output_ids(parsed.get(key)) if model_anchor else []
        if model_anchor and not ids and parsed.get(key) not in (None, []):
            continue
        if any(i not in by_id or by_id[i].meeting_id != anchor.meeting_id for i in ids):
            continue
        selected = tuple(
            sorted((by_id[i] for i in dict.fromkeys(ids)), key=lambda t: t.source_index)
        )
        substantive = [t for t in selected if not t.context_only]
        usable = bool(substantive)
        if key == "decision" and all(_NONFINAL.search(t.quote.text) for t in substantive):
            usable = False
        if key == "schedule" and not _planned_time(_extractive(selected)):
            usable = False
        if usable:
            used_model = True
        if not usable and options[key]:
            first = options[key][0]
            selected = (first,)
            if key == "action":
                adjacent = next(
                    (
                        t
                        for t in options[key][1:]
                        if t.quote.speaker == first.quote.speaker
                        and abs(t.source_index - first.source_index) == 1
                    ),
                    None,
                )
                if adjacent:
                    selected = tuple(sorted((first, adjacent), key=lambda t: t.source_index))
            usable = True
        if usable:
            facts[key] = _grounded_fact(key, _fact_context(key, selected, turns, topic=topic))
    return MeetingBrief(anchor, facts, used_model)
