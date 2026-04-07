"""
사용자 설정 ↔ 파이프라인 통합 테스트.

저장소에 사용자 편집 프롬프트와 용어집을 쓰고, 실제 Corrector/Summarizer가
그 값을 LLM 백엔드에 전달하는지 목(mock) 백엔드로 검증한다.

검증 대상:
    1. Corrector가 잡 시작 시 build_corrector_snapshot()을 호출한다.
    2. 용어집 항목이 시스템 프롬프트에 주입되어 백엔드에 전달된다.
    3. 회의 처리 중에 저장소가 변경되어도 진행 중 회의는 동일 스냅샷을 유지한다
       (잡 단위 일관성).
    4. Summarizer의 단일 요약 경로가 사용자 편집 프롬프트를 사용한다.
    5. Summarizer의 청크 요약 경로의 최종 머지 단계가 사용자 편집 프롬프트를 사용한다.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core import user_settings as us
from steps.corrector import Corrector
from steps.merger import MergedResult, MergedUtterance
from steps.summarizer import Summarizer
from steps.corrector import CorrectedResult, CorrectedUtterance


@pytest.fixture(autouse=True)
def isolated_user_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """임시 user_data 디렉토리로 격리."""
    data_dir = tmp_path / "user_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(us, "_user_data_dir", lambda: data_dir)
    us.invalidate_cache()
    yield data_dir
    us.invalidate_cache()


def _make_merged(texts: list[str]) -> MergedResult:
    """간단한 MergedResult 생성."""
    utterances = [
        MergedUtterance(text=t, speaker="SPEAKER_00", start=float(i), end=float(i + 1))
        for i, t in enumerate(texts)
    ]
    return MergedResult(
        utterances=utterances,
        num_speakers=1,
        audio_path="/tmp/test.wav",
    )


def _fake_correction_response(n: int) -> str:
    """[번호] 형식의 정상 응답을 생성."""
    lines = [f"[{i + 1}] 보정된 텍스트 {i + 1}" for i in range(n)]
    return "\n".join(lines)


class _RecordingBackend:
    """backend.chat() 호출 시 messages를 기록하는 목 백엔드."""

    def __init__(self, response_factory=None):
        self.calls: list[list[dict[str, str]]] = []
        self._response_factory = response_factory or (lambda n: _fake_correction_response(n))

    def chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        # user 프롬프트에서 배치 크기 추정
        user_msg = next((m for m in messages if m.get("role") == "user"), None)
        if user_msg is None:
            return ""
        n = user_msg["content"].count("[")
        return self._response_factory(n)

    def close(self):
        pass


# === Corrector 통합 ===


@pytest.mark.asyncio
async def test_corrector_uses_user_edited_prompt(isolated_user_data: Path) -> None:
    """사용자가 편집한 보정 프롬프트가 실제로 백엔드에 전달된다."""
    # 사용자 편집 프롬프트 저장
    custom_prompt = (
        "커스텀 보정 프롬프트입니다. [번호] 텍스트 포맷으로 출력하세요. "
        "존댓말로 통일하세요. 테스트용 고유 마커: XYZ-CUSTOM-777"
    )
    initial = us.load_prompts()
    updated = initial.model_copy(
        update={"corrector": us.PromptEntry(system_prompt=custom_prompt)}
    )
    us.save_prompts(updated)

    # 목 백엔드로 Corrector 실행
    recording = _RecordingBackend()
    corrector = Corrector()

    # _create_backend와 ModelLoadManager.acquire를 목으로 교체
    with patch.object(corrector, "_create_backend", return_value=recording):
        # ModelLoadManager를 우회: acquire를 async context manager 모킹
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_acquire(name, loader, **kwargs):
            yield loader()

        with patch.object(corrector._manager, "acquire", side_effect=fake_acquire):
            merged = _make_merged(["안녕하세요", "테스트 발화입니다"])
            result = await corrector.correct(merged)

    assert len(recording.calls) >= 1
    system_msg = recording.calls[0][0]
    assert system_msg["role"] == "system"
    assert "XYZ-CUSTOM-777" in system_msg["content"]
    assert len(result.utterances) == 2


@pytest.mark.asyncio
async def test_corrector_injects_vocabulary_into_system_prompt(
    isolated_user_data: Path,
) -> None:
    """용어집 항목이 corrector의 시스템 프롬프트에 주입되어 백엔드에 전달된다."""
    us.add_vocabulary_term(
        term="FastAPI", aliases=["패스트api"], note="웹 프레임워크"
    )
    us.add_vocabulary_term(term="Pyannote", aliases=["파이아노트"])

    recording = _RecordingBackend()
    corrector = Corrector()

    with patch.object(corrector, "_create_backend", return_value=recording):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_acquire(name, loader, **kwargs):
            yield loader()

        with patch.object(corrector._manager, "acquire", side_effect=fake_acquire):
            merged = _make_merged(["테스트"])
            await corrector.correct(merged)

    system_content = recording.calls[0][0]["content"]
    assert "고유명사 사전" in system_content
    assert "FastAPI" in system_content
    assert "패스트api" in system_content
    assert "Pyannote" in system_content
    assert "파이아노트" in system_content
    assert "웹 프레임워크" in system_content


@pytest.mark.asyncio
async def test_corrector_job_snapshot_immutable_during_processing(
    isolated_user_data: Path,
) -> None:
    """회의 처리 도중 사용자가 프롬프트를 바꿔도 진행 중 회의는 원래 스냅샷을 유지한다."""
    # 초기 프롬프트
    initial_marker = "INITIAL-MARKER-111"
    initial = us.load_prompts()
    us.save_prompts(
        initial.model_copy(
            update={
                "corrector": us.PromptEntry(
                    system_prompt=(
                        f"초기 프롬프트 {initial_marker}. [번호] 텍스트 포맷으로 출력하세요."
                    )
                )
            }
        )
    )

    recording = _RecordingBackend()
    # 첫 배치 후 사용자가 프롬프트를 변경하도록 시뮬레이션
    call_count = {"n": 0}
    original_chat = recording.chat

    def chat_with_midway_edit(messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # 첫 배치 이후 프롬프트 변경
            current = us.load_prompts(force_reload=True)
            us.save_prompts(
                current.model_copy(
                    update={
                        "corrector": us.PromptEntry(
                            system_prompt=(
                                "변경된 프롬프트 CHANGED-MARKER-222. [번호] 텍스트 포맷으로 출력하세요."
                            )
                        )
                    }
                )
            )
        return original_chat(messages, **kwargs)

    recording.chat = chat_with_midway_edit

    corrector = Corrector()
    corrector._batch_size = 1  # 배치 여러 번 돌도록

    with patch.object(corrector, "_create_backend", return_value=recording):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_acquire(name, loader, **kwargs):
            yield loader()

        with patch.object(corrector._manager, "acquire", side_effect=fake_acquire):
            merged = _make_merged(["발화1", "발화2", "발화3"])
            await corrector.correct(merged)

    # 모든 배치에서 initial_marker가 유지되어야 함 (잡 단위 스냅샷)
    assert len(recording.calls) >= 3
    for call in recording.calls:
        system_content = call[0]["content"]
        assert initial_marker in system_content, (
            f"잡 중간에 스냅샷이 바뀜: {system_content[:100]}"
        )
        assert "CHANGED-MARKER-222" not in system_content


# === Summarizer 통합 ===


def _make_corrected(texts: list[str]) -> CorrectedResult:
    utterances = [
        CorrectedUtterance(
            text=t,
            original_text=t,
            speaker="SPEAKER_00",
            start=float(i),
            end=float(i + 1),
            was_corrected=False,
        )
        for i, t in enumerate(texts)
    ]
    return CorrectedResult(
        utterances=utterances, num_speakers=1, audio_path="/tmp/test.wav"
    )


@pytest.mark.asyncio
async def test_summarizer_single_uses_user_edited_prompt(
    isolated_user_data: Path,
) -> None:
    """단일 요약 경로가 사용자 편집 요약 프롬프트를 사용한다."""
    custom_summary_prompt = (
        "커스텀 요약 프롬프트 SUMMARY-MARKER-333. "
        "회의록을 한 문장으로 요약하세요. 마크다운 형식."
    )
    initial = us.load_prompts()
    us.save_prompts(
        initial.model_copy(
            update={"summarizer": us.PromptEntry(system_prompt=custom_summary_prompt)}
        )
    )

    # 단일 요약 경로를 강제하기 위해 summarizer._max_input_tokens를 큰 값으로
    recording = _RecordingBackend(response_factory=lambda n: "## 회의 개요\n- 요약 결과")
    summarizer = Summarizer()
    summarizer._max_input_tokens = 100000  # 분할 안 일어나게

    with patch.object(summarizer, "_create_backend", return_value=recording):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_acquire(name, loader, **kwargs):
            yield loader()

        with patch.object(summarizer._manager, "acquire", side_effect=fake_acquire):
            corrected = _make_corrected(["짧은 회의 발화"])
            await summarizer.summarize(corrected)

    assert len(recording.calls) >= 1
    system_content = recording.calls[0][0]["content"]
    assert "SUMMARY-MARKER-333" in system_content


@pytest.mark.asyncio
async def test_summarizer_chunked_merge_uses_user_edited_prompt(
    isolated_user_data: Path,
) -> None:
    """청크 요약의 최종 머지 단계가 사용자 편집 요약 프롬프트를 사용한다.

    회귀 방지: 이전에는 _MERGE_SUMMARY_PROMPT 상수를 사용해 사용자 편집이
    긴 회의에 반영되지 않는 불일치가 있었다.
    """
    custom_summary_prompt = (
        "커스텀 요약 프롬프트 MERGE-MARKER-444. 회의록을 마크다운으로 작성하세요."
    )
    initial = us.load_prompts()
    us.save_prompts(
        initial.model_copy(
            update={"summarizer": us.PromptEntry(system_prompt=custom_summary_prompt)}
        )
    )

    recording = _RecordingBackend(response_factory=lambda n: "## 파트 요약")
    summarizer = Summarizer()
    # 분할을 강제 (토큰 한도를 아주 작게)
    summarizer._max_input_tokens = 10

    with patch.object(summarizer, "_create_backend", return_value=recording):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_acquire(name, loader, **kwargs):
            yield loader()

        with patch.object(summarizer._manager, "acquire", side_effect=fake_acquire):
            corrected = _make_corrected(
                [
                    "아주 긴 발화 " + "가" * 50,
                    "두 번째 긴 발화 " + "나" * 50,
                    "세 번째 긴 발화 " + "다" * 50,
                ]
            )
            await summarizer.summarize(corrected)

    # 여러 번 호출: 청크 N개 + 머지 1개
    assert len(recording.calls) >= 2

    # 마지막 호출(머지)의 시스템 프롬프트에 사용자 마커가 있어야 함
    last_system = recording.calls[-1][0]["content"]
    assert "MERGE-MARKER-444" in last_system, (
        f"청크 머지 단계가 사용자 프롬프트를 쓰지 않음: {last_system[:200]}"
    )

    # 중간 청크 호출들은 하드코딩된 _CHUNK_SUMMARY_PROMPT 사용 (내부 구현 상세)
    if len(recording.calls) >= 3:
        chunk_system = recording.calls[0][0]["content"]
        assert "핵심 내용을 요약" in chunk_system or "불릿 포인트" in chunk_system


# === Chat 통합 ===


def test_chat_uses_user_edited_prompt(isolated_user_data: Path) -> None:
    """ChatEngine의 _get_system_prompt가 사용자 편집값을 반환한다."""
    custom_chat = (
        "커스텀 채팅 프롬프트 CHAT-MARKER-555. 회의 내용을 기반으로만 답변하세요."
    )
    initial = us.load_prompts()
    us.save_prompts(
        initial.model_copy(
            update={"chat": us.PromptEntry(system_prompt=custom_chat)}
        )
    )

    from search.chat import ChatEngine

    # 검색 엔진은 목으로 교체 (초기화 시 인덱스 필요)
    with patch("search.chat.HybridSearchEngine") as MockSearch:
        MockSearch.return_value = MagicMock()
        engine = ChatEngine(search_engine=MagicMock())

    loaded = engine._get_system_prompt()
    assert "CHAT-MARKER-555" in loaded
