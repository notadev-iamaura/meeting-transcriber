"""core.ab_test_runner 및 core.ab_test_store 단위 테스트.

러너의 저장소 래퍼, 금지 패턴 카운터, 승자 산정, 러너 LLM/STT 해피 패스,
variant 부분 실패, 취소, diarize 체크포인트 분기를 monkeypatch 기반으로
검증한다. 실제 LLM/MLX 로드는 수행하지 않는다.
"""

from __future__ import annotations

import asyncio
import json


def _run(coro: Any) -> Any:
    """asyncio.run 대체 — 실행 후 현재 스레드에 새 event loop 를 설정해둔다.

    `asyncio.run` 은 종료 시 현재 스레드의 이벤트 루프를 None 으로 만들어,
    같은 세션에서 레거시 `asyncio.get_event_loop()` 를 사용하는 다른 테스트가
    실패할 수 있다. 이를 피하기 위해 수동으로 루프를 만들고, 실행 후에도 루프를
    살려서 set_event_loop 해 둔다.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        asyncio.set_event_loop(loop)
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from config import AppConfig, PathsConfig
from core import ab_test_runner, ab_test_store
from core.ab_test_runner import (
    LlmScope,
    ModelSpec,
    compute_metrics,
    compute_winner_score,
    count_forbidden_patterns,
    determine_winner,
    new_test_id,
    run_llm_ab_test,
    run_stt_ab_test,
)
from steps.corrector import CorrectedResult, CorrectedUtterance
from steps.diarizer import DiarizationResult, DiarizationSegment
from steps.merger import MergedResult, MergedUtterance
from steps.summarizer import SummaryResult
from steps.transcriber import TranscriptResult, TranscriptSegment


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def tmp_config(tmp_path: Path) -> AppConfig:
    """tmp_path 를 base_dir 로 하는 AppConfig."""
    cfg = AppConfig()
    cfg = cfg.model_copy(
        update={"paths": PathsConfig(base_dir=str(tmp_path))}
    )
    # outputs 디렉터리 생성
    cfg.paths.resolved_outputs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def sample_merged() -> MergedResult:
    """최소 2개 발화를 포함하는 MergedResult."""
    return MergedResult(
        utterances=[
            MergedUtterance(text="안녕하세요", speaker="SPEAKER_00", start=0.0, end=1.0),
            MergedUtterance(text="반갑습니다", speaker="SPEAKER_01", start=1.0, end=2.0),
        ],
        num_speakers=2,
        audio_path="/fake/input.wav",
    )


@pytest.fixture
def meeting_with_merge(
    tmp_config: AppConfig, sample_merged: MergedResult
) -> str:
    """merge.json 체크포인트가 준비된 가짜 회의 ID."""
    meeting_id = "meeting_20260409-000000"
    meeting_dir = tmp_config.paths.resolved_outputs_dir / meeting_id
    meeting_dir.mkdir(parents=True, exist_ok=True)
    sample_merged.save_checkpoint(meeting_dir / "merge.json")
    # input.wav placeholder
    (meeting_dir / "input.wav").write_bytes(b"RIFF....WAVEfmt ")
    return meeting_id


class _DummyManager:
    """ModelLoadManager 대체 — unload 만 no-op."""

    async def unload_model(self) -> None:
        return None


@pytest.fixture
def dummy_manager() -> _DummyManager:
    return _DummyManager()


# ============================================================
# new_test_id / is_valid_test_id
# ============================================================


class TestTestId:
    def test_new_test_id_형식_검증(self) -> None:
        tid = new_test_id()
        assert ab_test_store.is_valid_test_id(tid)
        assert tid.startswith("ab_")

    def test_is_valid_test_id_허용값(self) -> None:
        assert ab_test_store.is_valid_test_id("ab_20260409-143000_a1b2c3d4")

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "../etc/passwd",
            "/etc/passwd",
            "ab_20260409-143000_A1B2C3D4",  # 대문자
            "ab_20260409_143000_a1b2c3d4",  # 언더스코어
            "ab_2026-04-09_a1b2c3d4",
            "한글",
            "ab_20260409-143000_xyz",
            "..ab_20260409-143000_a1b2c3d4",
        ],
    )
    def test_is_valid_test_id_path_traversal_거부(self, bad: str) -> None:
        assert not ab_test_store.is_valid_test_id(bad)


# ============================================================
# 저장소
# ============================================================


class TestStore:
    def test_metadata_라운드트립(self, tmp_config: AppConfig) -> None:
        tid = new_test_id()
        ab_test_store.create_test_dir(tmp_config, tid)
        data = {"test_id": tid, "status": "pending", "value": 42}
        ab_test_store.write_metadata(tmp_config, tid, data)
        loaded = ab_test_store.read_metadata(tmp_config, tid)
        assert loaded == data

    def test_update_metadata_병합(self, tmp_config: AppConfig) -> None:
        tid = new_test_id()
        ab_test_store.create_test_dir(tmp_config, tid)
        ab_test_store.write_metadata(tmp_config, tid, {"a": 1, "b": 2})
        merged = ab_test_store.update_metadata(tmp_config, tid, b=3, c=4)
        assert merged == {"a": 1, "b": 3, "c": 4}

    def test_list_tests_빈_상태(self, tmp_config: AppConfig) -> None:
        assert ab_test_store.list_test_ids(tmp_config) == []

    def test_list_tests_정렬(self, tmp_config: AppConfig) -> None:
        """타임스탬프 내장 ID 이므로 역순 정렬이 최신순."""
        ids = [
            "ab_20260101-000000_aaaaaaaa",
            "ab_20260501-000000_bbbbbbbb",
            "ab_20260301-000000_cccccccc",
        ]
        for tid in ids:
            ab_test_store.create_test_dir(tmp_config, tid)
            ab_test_store.write_metadata(tmp_config, tid, {"test_id": tid})
        result = ab_test_store.list_test_ids(tmp_config)
        assert result == sorted(ids, reverse=True)

    def test_delete_test_디렉터리_제거(self, tmp_config: AppConfig) -> None:
        tid = new_test_id()
        ab_test_store.create_test_dir(tmp_config, tid)
        ab_test_store.write_metadata(tmp_config, tid, {"test_id": tid})
        path = ab_test_store.resolve_test_dir(tmp_config, tid)
        assert path.exists()
        ab_test_store.delete_test_dir(tmp_config, tid)
        assert not path.exists()

    def test_resolve_test_dir_부적합_id_거부(self, tmp_config: AppConfig) -> None:
        with pytest.raises(ValueError):
            ab_test_store.resolve_test_dir(tmp_config, "../evil")


# ============================================================
# 금지 패턴 / 메트릭
# ============================================================


class TestForbiddenPatterns:
    def test_speaker_placeholder(self) -> None:
        out = count_forbidden_patterns("회의에서 SPEAKER_00 이 말했다. SPEAKER_12 도.")
        assert out["speaker_placeholder"] == 2
        assert out["unknown_label"] == 0
        assert out["total"] == 2

    def test_unknown_label(self) -> None:
        out = count_forbidden_patterns("어떤 UNKNOWN 이 그랬어요. UNKNOWN! 다시.")
        assert out["unknown_label"] == 2

    def test_english_gloss(self) -> None:
        out = count_forbidden_patterns("이것은 컴퓨터(Computer)와 인공지능(Ai) 를 다룬다.")
        # "인공지능(Ai)" 는 대소문자 규칙상 매칭되지 않음 (첫 글자만 대문자 + 추가 영문)
        # 정규식 [A-Z][a-zA-Z]+ 요구 → 최소 2자 영문 필요
        assert out["english_gloss"] == 2

    def test_혼합(self) -> None:
        text = "SPEAKER_00 말씀: 디비(Database) 는 UNKNOWN 상태."
        out = count_forbidden_patterns(text)
        assert out["speaker_placeholder"] == 1
        assert out["unknown_label"] == 1
        assert out["english_gloss"] == 1
        assert out["total"] == 3

    def test_빈_문자열(self) -> None:
        out = count_forbidden_patterns("")
        assert out["total"] == 0


class TestWinner:
    def _metrics(self, forbidden: int, elapsed: float, chars: int) -> dict[str, Any]:
        return {
            "forbidden_patterns": {"total": forbidden},
            "elapsed_seconds": {"total": elapsed},
            "char_count": {"correct": chars, "summary": 0},
        }

    def test_compute_winner_score_공식(self) -> None:
        m = self._metrics(forbidden=1, elapsed=100.0, chars=0)
        # -2*1 - 0.01*100 + 0.5*log1p(0) = -3.0
        assert abs(compute_winner_score(m) - (-3.0)) < 1e-9

    def test_determine_winner_A_우세(self) -> None:
        a = self._metrics(forbidden=0, elapsed=10.0, chars=1000)
        b = self._metrics(forbidden=5, elapsed=10.0, chars=1000)
        assert determine_winner(a, b) == "A"

    def test_determine_winner_B_우세(self) -> None:
        a = self._metrics(forbidden=10, elapsed=10.0, chars=1000)
        b = self._metrics(forbidden=0, elapsed=10.0, chars=1000)
        assert determine_winner(a, b) == "B"

    def test_determine_winner_무승부(self) -> None:
        a = self._metrics(forbidden=0, elapsed=10.0, chars=1000)
        b = self._metrics(forbidden=0, elapsed=10.0, chars=1000)
        assert determine_winner(a, b) == "무승부"


# ============================================================
# config.model_copy 비오염
# ============================================================


class TestConfigModelCopy:
    def test_app_config_model_copy_비오염(self, tmp_config: AppConfig) -> None:
        original_llm_model = tmp_config.llm.mlx_model_name
        spec = ModelSpec(label="T", model_id="mlx-community/test", backend="mlx")
        temp = ab_test_runner._build_llm_temp_config(tmp_config, spec)
        assert temp.llm.mlx_model_name == "mlx-community/test"
        # 원본은 변경되지 않아야 함
        assert tmp_config.llm.mlx_model_name == original_llm_model

    def test_stt_temp_config_비오염(self, tmp_config: AppConfig) -> None:
        original = tmp_config.stt.model_name
        spec = ModelSpec(label="T", model_id="seastar-medium-4bit")
        temp = ab_test_runner._build_stt_temp_config(tmp_config, spec)
        assert temp.stt.model_name == "seastar-medium-4bit"
        assert tmp_config.stt.model_name == original


# ============================================================
# LLM 러너 해피 패스 / 실패 / 취소
# ============================================================


def _make_corrected(merged: MergedResult) -> CorrectedResult:
    """stub 용 CorrectedResult 생성."""
    return CorrectedResult(
        utterances=[
            CorrectedUtterance(
                text=u.text + "(수정)",
                original_text=u.text,
                speaker=u.speaker,
                start=u.start,
                end=u.end,
                was_corrected=True,
            )
            for u in merged.utterances
        ],
        num_speakers=merged.num_speakers,
        audio_path=merged.audio_path,
        total_corrected=len(merged.utterances),
    )


def _make_summary(markdown: str = "## 요약\n\n테스트") -> SummaryResult:
    return SummaryResult(
        markdown=markdown,
        audio_path="/fake/input.wav",
        num_speakers=2,
        speakers=["SPEAKER_00", "SPEAKER_01"],
        num_utterances=2,
    )


@pytest.fixture
def patch_llm_steps(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Corrector/Summarizer 를 stub 으로 교체한다."""
    counts = {"corrector_init": 0, "summarizer_init": 0, "correct": 0, "summarize": 0}

    class StubCorrector:
        def __init__(self, config: Any, manager: Any) -> None:
            counts["corrector_init"] += 1
            self._config = config

        async def correct(self, merged: MergedResult) -> CorrectedResult:
            counts["correct"] += 1
            return _make_corrected(merged)

    class StubSummarizer:
        def __init__(self, config: Any, manager: Any) -> None:
            counts["summarizer_init"] += 1
            self._config = config

        async def summarize(self, corrected: CorrectedResult) -> SummaryResult:
            counts["summarize"] += 1
            return _make_summary()

    monkeypatch.setattr(ab_test_runner, "Corrector", StubCorrector)
    monkeypatch.setattr(ab_test_runner, "Summarizer", StubSummarizer)
    return counts


class TestLlmRunner:
    def test_run_llm_ab_test_해피_패스(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
        dummy_manager: _DummyManager,
        patch_llm_steps: dict[str, int],
    ) -> None:
        test_id = _run(
            run_llm_ab_test(
                config=tmp_config,
                source_meeting_id=meeting_with_merge,
                variant_a=ModelSpec(label="A", model_id="model-a"),
                variant_b=ModelSpec(label="B", model_id="model-b"),
                scope=LlmScope(correct=True, summarize=True),
                model_manager=dummy_manager,
            )
        )
        assert ab_test_store.is_valid_test_id(test_id)

        meta = ab_test_store.read_metadata(tmp_config, test_id)
        assert meta["status"] == "completed"
        assert meta["variant_errors"] == {}

        test_dir = ab_test_store.resolve_test_dir(tmp_config, test_id)
        for variant in ("variant_a", "variant_b"):
            assert (test_dir / variant / "correct.json").exists()
            assert (test_dir / variant / "summary.md").exists()
            assert (test_dir / variant / "metrics.json").exists()

        # Corrector/Summarizer 각각 2회씩 생성되어야 함
        assert patch_llm_steps["corrector_init"] == 2
        assert patch_llm_steps["summarizer_init"] == 2

    def test_run_llm_ab_test_variant_A_실패시_B_계속(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
        dummy_manager: _DummyManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        call = {"n": 0}

        class FailingCorrector:
            def __init__(self, config: Any, manager: Any) -> None:
                pass

            async def correct(self, merged: MergedResult) -> CorrectedResult:
                call["n"] += 1
                if call["n"] == 1:
                    raise RuntimeError("A 실패 시뮬레이션")
                return _make_corrected(merged)

        class StubSummarizer:
            def __init__(self, config: Any, manager: Any) -> None:
                pass

            async def summarize(self, corrected: CorrectedResult) -> SummaryResult:
                return _make_summary()

        monkeypatch.setattr(ab_test_runner, "Corrector", FailingCorrector)
        monkeypatch.setattr(ab_test_runner, "Summarizer", StubSummarizer)

        test_id = _run(
            run_llm_ab_test(
                config=tmp_config,
                source_meeting_id=meeting_with_merge,
                variant_a=ModelSpec(label="A", model_id="model-a"),
                variant_b=ModelSpec(label="B", model_id="model-b"),
                scope=LlmScope(correct=True, summarize=True),
                model_manager=dummy_manager,
            )
        )

        meta = ab_test_store.read_metadata(tmp_config, test_id)
        assert meta["status"] == "partial_failed"
        assert "A" in meta["variant_errors"]
        assert "B" not in meta["variant_errors"]

    def test_run_llm_ab_test_취소(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
        dummy_manager: _DummyManager,
        patch_llm_steps: dict[str, int],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """lock 획득 직후 취소 플래그가 세팅되어 있으면 cancelled 로 종료."""
        original_new_id = ab_test_runner.new_test_id
        planned_id = original_new_id()

        def _fixed_id() -> str:
            return planned_id

        monkeypatch.setattr(ab_test_runner, "new_test_id", _fixed_id)
        # 취소 플래그 미리 등록
        ab_test_runner._cancel_requests.add(planned_id)

        test_id = _run(
            run_llm_ab_test(
                config=tmp_config,
                source_meeting_id=meeting_with_merge,
                variant_a=ModelSpec(label="A", model_id="model-a"),
                variant_b=ModelSpec(label="B", model_id="model-b"),
                scope=LlmScope(correct=True, summarize=True),
                model_manager=dummy_manager,
            )
        )
        assert test_id == planned_id
        meta = ab_test_store.read_metadata(tmp_config, test_id)
        assert meta["status"] == "cancelled"

    def test_run_llm_ab_test_same_models_거부(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
        dummy_manager: _DummyManager,
    ) -> None:
        with pytest.raises(ValueError):
            _run(
                run_llm_ab_test(
                    config=tmp_config,
                    source_meeting_id=meeting_with_merge,
                    variant_a=ModelSpec(label="A", model_id="same"),
                    variant_b=ModelSpec(label="B", model_id="same"),
                    scope=LlmScope(),
                    model_manager=dummy_manager,
                )
            )


# ============================================================
# STT 러너
# ============================================================


class TestSttRunner:
    def test_run_stt_ab_test_diarize_체크포인트_없음_에러(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
        dummy_manager: _DummyManager,
    ) -> None:
        with pytest.raises(ValueError, match="diarize"):
            _run(
                run_stt_ab_test(
                    config=tmp_config,
                    source_meeting_id=meeting_with_merge,
                    variant_a=ModelSpec(label="A", model_id="stt-a"),
                    variant_b=ModelSpec(label="B", model_id="stt-b"),
                    allow_diarize_rerun=False,
                    model_manager=dummy_manager,
                )
            )

    def test_run_stt_ab_test_diarize_재실행_허용(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
        dummy_manager: _DummyManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_diarize = DiarizationResult(
            segments=[
                DiarizationSegment(speaker="SPEAKER_00", start=0.0, end=1.0),
                DiarizationSegment(speaker="SPEAKER_01", start=1.0, end=2.0),
            ],
            num_speakers=2,
            audio_path="/fake/input.wav",
        )

        class StubDiarizer:
            def __init__(self, config: Any, manager: Any) -> None:
                pass

            async def diarize(self, wav_path: Path) -> DiarizationResult:
                return fake_diarize

        class StubTranscriber:
            def __init__(self, config: Any, manager: Any) -> None:
                self._config = config

            async def transcribe(self, wav_path: Path) -> TranscriptResult:
                return TranscriptResult(
                    segments=[
                        TranscriptSegment(text="안녕하세요", start=0.0, end=1.0),
                        TranscriptSegment(text="반갑습니다", start=1.0, end=2.0),
                    ],
                    full_text="안녕하세요 반갑습니다",
                    language="ko",
                    audio_path=str(wav_path),
                )

        monkeypatch.setattr(ab_test_runner, "Diarizer", StubDiarizer)
        monkeypatch.setattr(ab_test_runner, "Transcriber", StubTranscriber)

        test_id = _run(
            run_stt_ab_test(
                config=tmp_config,
                source_meeting_id=meeting_with_merge,
                variant_a=ModelSpec(label="A", model_id="stt-a"),
                variant_b=ModelSpec(label="B", model_id="stt-b"),
                allow_diarize_rerun=True,
                model_manager=dummy_manager,
            )
        )

        meta = ab_test_store.read_metadata(tmp_config, test_id)
        assert meta["status"] == "completed"
        test_dir = ab_test_store.resolve_test_dir(tmp_config, test_id)
        for variant in ("variant_a", "variant_b"):
            assert (test_dir / variant / "transcribe.json").exists()
            assert (test_dir / variant / "merge.json").exists()
            assert (test_dir / variant / "metrics.json").exists()


# ============================================================
# 동시성 — _ab_test_lock 직렬화
# ============================================================


class TestLock:
    def test_ab_test_lock_직렬화(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
        dummy_manager: _DummyManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """첫 번째 테스트가 락을 점유하고 있을 때 두 번째 호출은 RuntimeError."""

        gate = asyncio.Event()

        class BlockingCorrector:
            def __init__(self, config: Any, manager: Any) -> None:
                pass

            async def correct(self, merged: MergedResult) -> CorrectedResult:
                await gate.wait()
                return _make_corrected(merged)

        class StubSummarizer:
            def __init__(self, config: Any, manager: Any) -> None:
                pass

            async def summarize(self, corrected: CorrectedResult) -> SummaryResult:
                return _make_summary()

        monkeypatch.setattr(ab_test_runner, "Corrector", BlockingCorrector)
        monkeypatch.setattr(ab_test_runner, "Summarizer", StubSummarizer)

        async def scenario() -> None:
            first = asyncio.create_task(
                run_llm_ab_test(
                    config=tmp_config,
                    source_meeting_id=meeting_with_merge,
                    variant_a=ModelSpec(label="A", model_id="model-a"),
                    variant_b=ModelSpec(label="B", model_id="model-b"),
                    scope=LlmScope(correct=True, summarize=True),
                    model_manager=dummy_manager,
                )
            )
            # 첫 번째가 lock 에 진입할 때까지 대기
            for _ in range(50):
                await asyncio.sleep(0.01)
                if ab_test_runner._get_ab_test_lock().locked():
                    break
            assert ab_test_runner._get_ab_test_lock().locked()

            with pytest.raises(RuntimeError):
                await run_llm_ab_test(
                    config=tmp_config,
                    source_meeting_id=meeting_with_merge,
                    variant_a=ModelSpec(label="A", model_id="model-a"),
                    variant_b=ModelSpec(label="B", model_id="model-b"),
                    scope=LlmScope(correct=True, summarize=True),
                    model_manager=dummy_manager,
                )

            # 첫 번째 태스크 해제
            gate.set()
            await first

        _run(scenario())
