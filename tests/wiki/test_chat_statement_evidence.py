"""답변 문장의 기존 인용과 실제 직접 원문을 연결하는 회귀."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from config import AppConfig, PathsConfig, WikiConfig, WikiSemanticConfig
from core.wiki.chat_integration import HybridChatService
from core.wiki.router import RouteDecision, RouterVerdict
from core.wiki.store import WikiStore

FILLER = "[meeting:sample@00:03:02]"
QUESTION = "[meeting:sample@00:04:00]"
DIRECT = "[meeting:sample@00:04:30]"
PROMISE = "[meeting:sample@00:09:15]"


class _Router:
    async def classify(self, query: str) -> RouterVerdict:
        """공개 Wiki 답변의 최종 출처 선별만 검사한다."""
        return RouterVerdict(RouteDecision.WIKI, 9, "fixture", [], False)


class _Rag:
    async def respond(self, query: str, **kwargs: Any) -> str:
        """근거를 읽을 수 없으면 원문 경로로 돌려보낸다."""
        return "원문에서 확인합니다."


class _LLM:
    def __init__(self, answer: str) -> None:
        """실제 추론 없이 고정된 답변과 입력 원장만 사용한다."""
        self.answer = answer
        self.prompts: list[str] = []

    async def generate(self, *, system_prompt: str, user_prompt: str) -> str:
        """실제 원문 밖의 마커가 입력되는지 검사한다."""
        self.prompts.append(user_prompt)
        return self.answer


def _world(tmp_path: Path, answer: str, *, question_only: bool = False) -> tuple[Any, ...]:
    """정상·군말·확인 질문을 구분할 수 있는 작은 실제 체크포인트를 만든다."""
    config = AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        wiki=WikiConfig(semantic=WikiSemanticConfig(enabled=False)),
    )
    store = WikiStore(tmp_path / "wiki")
    store.init_repo()
    body = (
        "---\ntype: decision\nstatus: decided\ntitle: 오류 제보 캡처\n---\n\n"
        f"# 오류 제보 캡처\n\n콘솔과 네트워크 화면을 함께 캡처해 제보한다. {FILLER} 그 뭐지? {QUESTION} 질문 {DIRECT}\n"
        f"가이드 업데이트 후 링크를 공유하기로 했다. {PROMISE}\n"
    )
    store.write_page(Path("decisions/live.md"), body)
    utterances = [
        {"speaker": "SPEAKER_01", "text": "그 뭐지?", "start": 182, "end": 183},
        {
            "speaker": "SPEAKER_01",
            "text": "콘솔과 네트워크 화면이 필요할까요?",
            "start": 240,
            "end": 244,
        },
        {
            "speaker": "SPEAKER_02",
            "text": "콘솔과 네트워크 화면을 함께 캡처해 제보해 주세요.",
            "start": 270,
            "end": 277,
        },
        {
            "speaker": "SPEAKER_01",
            "text": "가이드 업데이트 후 링크를 공유하겠습니다.",
            "start": 555,
            "end": 562,
        },
    ]
    if question_only:
        for utterance in utterances:
            utterance["text"] = "이 작업을 진행할까요?"
    path = config.paths.resolved_checkpoints_dir / "sample" / "correct.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"utterances": utterances}), encoding="utf-8")
    llm = _LLM(answer)
    service = HybridChatService(_Rag(), _Router(), store, llm, config)
    return service, llm, store, path, body


async def test_군말_인용을_빼고_문장의_실제_직접발화를_주인용으로_준다(tmp_path: Path) -> None:
    """첫 마커를 임의 상속하지 않고 직접 발화가 모델 입력과 출처에 올라간다."""
    service, llm, store, path, body = _world(
        tmp_path, f"콘솔·네트워크 화면을 함께 제보합니다. {DIRECT}"
    )
    original = path.read_bytes()
    result = await service.respond("오류 제보 캡처 결정은?")
    assert result.source_type == "wiki"
    assert result.wiki_sources[0].citations[0] == DIRECT
    assert FILLER not in result.wiki_sources[0].citations
    assert FILLER not in llm.prompts[0]
    assert "그 뭐지" not in llm.prompts[0]
    assert f"주 인용: {DIRECT}" in llm.prompts[0]
    assert (
        f"확인 질문 문맥: [SPEAKER_01] 콘솔과 네트워크 화면이 필요할까요? {QUESTION}"
        in llm.prompts[0]
    )
    assert (
        "직접 원문: [SPEAKER_02] 콘솔과 네트워크 화면을 함께 캡처해 제보해 주세요."
        in llm.prompts[0]
    )
    assert store.read_raw_page(Path("decisions/live.md")) == body
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "answer",
    [
        f"화면을 함께 제보하기로 했습니다. {FILLER}",
        "화면을 함께 제보하기로 했습니다. [meeting:unknown@00:00:00]",
        "화면을 함께 제보하기로 했습니다.",
        f"화면을 함께 제보합니다. {DIRECT}\n링크 공유까지 이미 완료했습니다.",
        f"화면을 함께 제보합니다. 링크 공유까지 이미 완료했습니다. {DIRECT}",
        f"화면을 함께 제보합니다.링크 공유까지 이미 완료했습니다. {DIRECT}",
        f"화면을 함께 제보합니다. {DIRECT} 링크 공유까지 이미 완료했습니다.",
        f"화면을 함께 제보하기로 했습니다. {QUESTION}",
        f"# 모든 작업 완료\n화면을 함께 제보합니다. {DIRECT}",
        f"<!-- 모든 작업 완료 -->\n화면을 함께 제보합니다. {DIRECT}",
        f"화면을 함께 제보하기로 했나요? {DIRECT}",
        f"예산은 500만 원으로 확정됐습니다. {DIRECT}",
        f"다른 담당자가 모든 작업을 이미 완료했습니다. {DIRECT}",
        f"콘솔과 네트워크 화면을 함께 캡처해 제보한다. {PROMISE}",
        f"콘솔과 네트워크 화면을 함께 캡처해 제보한다. {DIRECT} {PROMISE}",
        f"콘솔과 네트워크 화면을 함께 캡처해 제보한다. {DIRECT}\n[모든 작업 완료](../완료.md)",
    ],
)
async def test_임의인용_누락문장_질문단독인용은_게시문장으로_돌아간다(
    tmp_path: Path, answer: str
) -> None:
    """모델이 주 인용을 잃으면 새 사실 없이 저장된 문장과 원래 마커만 반환한다."""
    service, _llm, _store, _path, _body = _world(tmp_path, answer)
    result = await service.respond("오류 제보 캡처 결정은?")
    assert result.source_type == "wiki"
    assert result.wiki_answer.startswith("공개 Wiki에 기록된 내용입니다.")
    assert "이미 완료" not in result.wiki_answer
    assert FILLER not in result.wiki_answer
    assert DIRECT in result.wiki_answer
    assert PROMISE in result.wiki_answer


async def test_같은게시문장의_직접인용과_내용을_그대로쓴답변은_유지한다(tmp_path: Path) -> None:
    """공백·목록·문장 끝 표기를 제외한 내용을 그대로 보존한 답변만 통과시킨다."""
    answer = f"- 콘솔과 네트워크 화면을 함께 캡처해 제보한다 {DIRECT}"
    service, _llm, _store, _path, _body = _world(tmp_path, answer)
    result = await service.respond("오류 제보 캡처 결정은?")
    assert result.wiki_answer == answer


@pytest.mark.parametrize("replacement", ["50", "5.0", "-50"])
async def test_음수와_소수점은_문장부호로_지워_다른값을_허용하지_않는다(
    tmp_path: Path, replacement: str
) -> None:
    """기호를 제거하면 같은 문자열이 되는 숫자도 실제 값이 다르면 대체 답변으로 돌린다."""
    source_text = "예산 조정치는 -5.0만 원이다."
    answer = f"예산 조정치는 {replacement}만 원이다. {DIRECT}"
    service, _llm, store, path, body = _world(tmp_path, answer)
    store.write_page(
        Path("decisions/live.md"),
        body.replace("콘솔과 네트워크 화면을 함께 캡처해 제보한다.", source_text),
    )
    payload = json.loads(path.read_text())
    payload["utterances"][2]["text"] = source_text
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = await service.respond("예산 조정치 결정은?")
    assert result.wiki_answer.startswith("공개 Wiki에 기록된 내용입니다.")
    assert source_text in result.wiki_answer
    assert answer not in result.wiki_answer


@pytest.mark.parametrize("acknowledgement", [None, "네, 네.", "맞습니다."])
async def test_확인질문만_있으면_게시확정으로_합성하지_않는다(
    tmp_path: Path, acknowledgement: str | None
) -> None:
    """문자상 일치해도 질문뿐인 원문을 확정 근거로 승격하지 않는다."""
    service, llm, _store, path, _body = _world(tmp_path, "호출되면 안 됩니다.", question_only=True)
    if acknowledgement is not None:
        payload = json.loads(path.read_text())
        payload["utterances"][-1]["text"] = acknowledgement
        path.write_text(json.dumps(payload), encoding="utf-8")
    result = await service.respond("오류 제보 캡처 결정은?")
    assert result.source_type == "rag"
    assert result.wiki_sources == []
    assert llm.prompts == []


async def test_게시문서만_있고_인용원문이_없으면_합성하지_않는다(tmp_path: Path) -> None:
    """원문 checkpoint의 부재를 정상적인 사실 검증으로 숨기지 않는다."""
    service, llm, _store, path, _body = _world(tmp_path, "호출되면 안 됩니다.")
    path.unlink()
    result = await service.respond("오류 제보 캡처 결정은?")
    assert result.source_type == "rag"
    assert llm.prompts == []
