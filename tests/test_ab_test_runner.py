"""core.ab_test_runner 및 core.ab_test_store 단위 테스트.

러너의 저장소 래퍼, 금지 패턴 카운터, 승자 산정, 러너 LLM/STT 해피 패스,
variant 부분 실패, 취소, diarize 체크포인트 분기를 monkeypatch 기반으로
검증한다. 실제 LLM/MLX 로드는 수행하지 않는다.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock


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


from pathlib import Path
from typing import Any

import pytest

from config import AppConfig, PathsConfig
from core import ab_test_runner, ab_test_store
from core.ab_test_runner import (
    LlmScope,
    ModelSpec,
    compute_winner_score,
    count_forbidden_patterns,
    determine_winner,
    inspect_llm_ab_source,
    new_test_id,
    reserve_llm_ab_test,
    reserve_stt_ab_test,
    run_llm_ab_test,
    run_stt_ab_test,
)
from steps.corrector import CorrectedResult, CorrectedUtterance
from steps.diarizer import DiarizationResult, DiarizationSegment
from steps.merger import MergedResult, MergedUtterance
from steps.summarizer import SummaryResult
from steps.transcriber import TranscriptResult, TranscriptSegment


def _denied_audio_admission(failure_kind_name: str) -> Any:
    """STT 러너 preflight용 비수락 결과를 생성한다."""
    from core.audio_quality import AudioFailureKind, AudioQualityResult, AudioQualityStatus

    media_invalid = failure_kind_name == "MEDIA_INVALID"
    return AudioQualityResult(
        status=AudioQualityStatus.REJECT if media_invalid else AudioQualityStatus.ERROR,
        mean_volume_db=None,
        duration_seconds=1.0 if media_invalid else None,
        reason=f"admission denied: {failure_kind_name}",
        failure_kind=getattr(AudioFailureKind, failure_kind_name),
    )


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def tmp_config(tmp_path: Path) -> AppConfig:
    """tmp_path 를 base_dir 로 하는 AppConfig."""
    cfg = AppConfig()
    cfg = cfg.model_copy(update={"paths": PathsConfig(base_dir=str(tmp_path))})
    # outputs 디렉터리 생성
    cfg.paths.resolved_outputs_dir.mkdir(parents=True, exist_ok=True)
    # 기존 러너 테스트는 실제 미디어가 아닌 최소 바이트 fixture를 사용한다.
    cfg.audio_quality.enabled = False
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
def meeting_with_merge(tmp_config: AppConfig, sample_merged: MergedResult) -> str:
    """merge.json 체크포인트가 준비된 가짜 회의 ID."""
    meeting_id = "meeting_20260409-000000"
    # 체크포인트 디렉터리 (merge.json, diarize.json 등)
    ckpt_dir = tmp_config.paths.resolved_checkpoints_dir / meeting_id
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    sample_merged.save_checkpoint(ckpt_dir / "merge.json")
    # WAV 는 audio_input/ 에 저장
    audio_dir = tmp_config.paths.resolved_audio_input_dir
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / f"{meeting_id}.wav").write_bytes(b"RIFF....WAVEfmt ")
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

    @pytest.mark.parametrize(
        ("active_status", "terminal_status"),
        [("pending", "failed"), ("running", "failed"), ("cancelling", "cancelled")],
    )
    def test_startup은_ghost_A_B_metadata를_terminal로_복구한다(
        self,
        tmp_config: AppConfig,
        active_status: str,
        terminal_status: str,
    ) -> None:
        """프로세스 재시작 뒤 소비할 task가 없는 active 상태를 남기지 않는다."""
        tid = new_test_id()
        ab_test_store.create_test_dir(tmp_config, tid)
        ab_test_store.write_metadata(
            tmp_config,
            tid,
            {
                "test_id": tid,
                "status": active_status,
                "current_variant": "variant_a",
                "current_step": "transcribe",
                "completed_at": None,
                "error": None,
            },
        )

        assert ab_test_runner.recover_orphaned_tests(tmp_config) == 1

        recovered = ab_test_store.read_metadata(tmp_config, tid)
        assert recovered["status"] == terminal_status
        assert recovered["current_variant"] is None
        assert recovered["current_step"] is None
        assert recovered["completed_at"]
        assert "종료" in recovered["error"]

    def test_startup_A_B_recovery는_terminal_metadata를_변경하지_않는다(
        self,
        tmp_config: AppConfig,
    ) -> None:
        tid = new_test_id()
        ab_test_store.create_test_dir(tmp_config, tid)
        original = {"test_id": tid, "status": "completed", "completed_at": "done"}
        ab_test_store.write_metadata(tmp_config, tid, original)

        assert ab_test_runner.recover_orphaned_tests(tmp_config) == 0
        assert ab_test_store.read_metadata(tmp_config, tid) == original

    @pytest.mark.asyncio
    async def test_취소요청은_crash전에_cancelling으로_durable하게_기록한다(
        self,
        tmp_config: AppConfig,
    ) -> None:
        """202 직후 종료돼도 startup은 failed가 아니라 cancelled로 복구한다."""
        tid = new_test_id()
        ab_test_store.create_test_dir(tmp_config, tid)
        ab_test_store.write_metadata(
            tmp_config,
            tid,
            {"test_id": tid, "status": "running", "error": None},
        )

        await ab_test_runner.cancel_test(tmp_config, tid)
        assert ab_test_store.read_metadata(tmp_config, tid)["status"] == "cancelling"

        ab_test_runner._cancel_requests.discard(tid)
        assert ab_test_runner.recover_orphaned_tests(tmp_config) == 1
        assert ab_test_store.read_metadata(tmp_config, tid)["status"] == "cancelled"

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

    def test_ab_tests_root_symlink를_따라_외부에_쓰지_않는다(
        self,
        tmp_config: AppConfig,
        tmp_path: Path,
    ) -> None:
        """저장소 root가 symlink면 외부 대상에 variant를 만들지 않고 거부한다."""
        external = tmp_path / "external-root"
        external.mkdir()
        (tmp_path / "ab_tests").symlink_to(external, target_is_directory=True)
        tid = "ab_20260822-120000_a1b2c3d4"

        with pytest.raises(ValueError, match="안전하지"):
            ab_test_store.create_test_dir(tmp_config, tid)

        assert list(external.iterdir()) == []

    def test_test와_variant_symlink를_저장_경계에서_거부한다(
        self,
        tmp_config: AppConfig,
        tmp_path: Path,
    ) -> None:
        """유효한 이름이어도 test/variant symlink는 no-follow 검증을 통과하지 못한다."""
        root = ab_test_store.get_ab_test_root(tmp_config)
        external = tmp_path / "external-test"
        external.mkdir()
        linked_tid = "ab_20260822-120001_a1b2c3d4"
        (root / linked_tid).symlink_to(external, target_is_directory=True)
        with pytest.raises(ValueError, match="안전"):
            ab_test_store.create_test_dir(tmp_config, linked_tid)

        safe_tid = "ab_20260822-120002_a1b2c3d4"
        safe_dir = ab_test_store.create_test_dir(tmp_config, safe_tid)
        (safe_dir / "variant_a").rmdir()
        (safe_dir / "variant_a").symlink_to(external, target_is_directory=True)
        with pytest.raises(ValueError, match="안전"):
            ab_test_store.resolve_variant_dir(tmp_config, safe_tid, "variant_a")
        assert list(external.iterdir()) == []

    def test_metadata_고정_tmp_symlink를_덮어쓰지_않는다(
        self,
        tmp_config: AppConfig,
        tmp_path: Path,
    ) -> None:
        """예측 가능한 legacy temp symlink가 있어도 원자 쓰기는 unique temp를 쓴다."""
        tid = "ab_20260822-120003_a1b2c3d4"
        test_dir = ab_test_store.create_test_dir(tmp_config, tid)
        marker = tmp_path / "external-marker.txt"
        marker.write_text("keep", encoding="utf-8")
        (test_dir / "metadata.json.tmp").symlink_to(marker)

        ab_test_store.write_metadata(tmp_config, tid, {"test_id": tid})

        assert marker.read_text(encoding="utf-8") == "keep"
        assert ab_test_store.read_metadata(tmp_config, tid) == {"test_id": tid}


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

    def test_stt_temp_config_비오염(
        self,
        tmp_config: AppConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hf-cache"))
        monkeypatch.setenv("HF_HOME", str(tmp_path / "hf-home"))
        original = tmp_config.stt.model_name
        spec = ModelSpec(label="T", model_id="seastar-medium-4bit")
        temp = ab_test_runner._build_stt_temp_config(tmp_config, spec)
        # 레지스트리가 짧은 ID 를 실제 HF repo ID 로 변환한다
        assert temp.stt.model_name == "youngouk/seastar-medium-ko-4bit-mlx"
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

    @pytest.mark.parametrize("configured_child", ["../outside", "/tmp/outside"])
    def test_checkpoint_config는_base_dir_밖을_가리킬수없다(
        self,
        tmp_config: AppConfig,
        dummy_manager: _DummyManager,
        configured_child: str,
    ) -> None:
        """상위 traversal·외부 absolute checkpoint root는 source 접근 전에 차단한다."""
        from core.audio_quality import AudioFailureKind
        from steps.transcriber import AudioAdmissionError

        tmp_config.paths.checkpoints_dir = configured_child

        with pytest.raises(AudioAdmissionError) as exc_info:
            _run(
                run_llm_ab_test(
                    config=tmp_config,
                    source_meeting_id="outside-meeting",
                    variant_a=ModelSpec(label="A", model_id="model-a"),
                    variant_b=ModelSpec(label="B", model_id="model-b"),
                    scope=LlmScope(),
                    model_manager=dummy_manager,
                )
            )

        assert exc_info.value.failure_kind is AudioFailureKind.SECURITY_BLOCKED
        assert not (tmp_config.paths.resolved_base_dir / "ab_tests").exists()

    def test_base_dir_안의_absolute_checkpoint_config는_호환된다(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
    ) -> None:
        """기존 absolute-inside-base 설정은 안전한 경로로 계속 허용한다."""
        inside_root = tmp_config.paths.resolved_checkpoints_dir
        tmp_config.paths.checkpoints_dir = str(inside_root)

        merge_path, _identity = inspect_llm_ab_source(
            tmp_config,
            meeting_with_merge,
        )

        assert merge_path == inside_root / meeting_with_merge / "merge.json"

    def test_merge_checkpoint가_읽는동안_변경되면_SOURCE_BUSY(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """fd로 pin한 JSON도 in-place write가 겹치면 snapshot으로 승인하지 않는다."""
        from core.audio_quality import AudioFailureKind
        from steps.transcriber import AudioAdmissionError

        merge_path = tmp_config.paths.resolved_checkpoints_dir / meeting_with_merge / "merge.json"
        original_load = ab_test_runner.json.load

        def mutate_after_load(handle: Any) -> Any:
            payload = original_load(handle)
            merge_path.write_text(
                merge_path.read_text(encoding="utf-8") + " ",
                encoding="utf-8",
            )
            return payload

        monkeypatch.setattr(ab_test_runner.json, "load", mutate_after_load)

        with pytest.raises(AudioAdmissionError) as exc_info:
            inspect_llm_ab_source(tmp_config, meeting_with_merge)

        assert exc_info.value.failure_kind is AudioFailureKind.SOURCE_BUSY


# ============================================================
# STT 러너
# ============================================================


class TestSttRunner:
    def test_legacy_pipeline_state의_audio_input_wav도_허용한다(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
    ) -> None:
        """구버전 state의 안전한 audio_input/{id}.wav 경로를 조기 차단하지 않는다."""
        wav_path = tmp_config.paths.resolved_audio_input_dir / f"{meeting_with_merge}.wav"
        state_path = (
            tmp_config.paths.resolved_checkpoints_dir / meeting_with_merge / "pipeline_state.json"
        )
        state_path.write_text(json.dumps({"wav_path": str(wav_path)}), encoding="utf-8")

        inspected, _identity = ab_test_runner._inspect_stt_audio_source(
            tmp_config,
            meeting_with_merge,
        )

        assert inspected == wav_path

    def test_pipeline_state는_다른_회의의_audio_input_wav를_가리킬_수_없다(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
    ) -> None:
        """변조된 state로 같은 input 디렉터리의 다른 회의 음성을 읽지 않는다."""
        from core.audio_quality import AudioFailureKind
        from steps.transcriber import AudioAdmissionError

        other = tmp_config.paths.resolved_audio_input_dir / "other-meeting.wav"
        other.write_bytes(b"RIFF....WAVEfmt ")
        state_path = (
            tmp_config.paths.resolved_checkpoints_dir / meeting_with_merge / "pipeline_state.json"
        )
        state_path.write_text(json.dumps({"wav_path": str(other)}), encoding="utf-8")

        with pytest.raises(AudioAdmissionError) as captured:
            ab_test_runner._inspect_stt_audio_source(tmp_config, meeting_with_merge)

        assert captured.value.failure_kind is AudioFailureKind.SECURITY_BLOCKED

    def test_metadata_reserved_STT_admission취소는_cancelled로_종결한다(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
        dummy_manager: _DummyManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """route 예약 뒤 full admission await 취소가 pending ghost를 남기지 않는다."""
        planned_id = "ab_20260409-143000_cafebabe"
        variant_a = ModelSpec(label="A", model_id="stt-a")
        variant_b = ModelSpec(label="B", model_id="stt-b")
        wav_path, _identity = ab_test_runner._inspect_stt_audio_source(
            tmp_config,
            meeting_with_merge,
        )
        reserve_stt_ab_test(
            tmp_config,
            test_id=planned_id,
            source_meeting_id=meeting_with_merge,
            wav_path=wav_path,
            variant_a=variant_a,
            variant_b=variant_b,
            allow_diarize_rerun=True,
        )

        async def scenario() -> None:
            entered = asyncio.Event()

            async def blocking_admission(*args: Any, **kwargs: Any) -> None:
                entered.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(
                ab_test_runner,
                "_require_stt_audio_admission",
                blocking_admission,
            )
            task = asyncio.create_task(
                run_stt_ab_test(
                    config=tmp_config,
                    source_meeting_id=meeting_with_merge,
                    variant_a=variant_a,
                    variant_b=variant_b,
                    model_manager=dummy_manager,
                    test_id=planned_id,
                    metadata_reserved=True,
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        _run(scenario())
        metadata = ab_test_store.read_metadata(tmp_config, planned_id)
        assert metadata["status"] == "cancelled"
        assert metadata["completed_at"] is not None

    @pytest.mark.parametrize("configured_child", ["../outside", "/tmp/outside"])
    def test_audio_input_config는_base_dir_밖을_가리킬수없다(
        self,
        tmp_config: AppConfig,
        dummy_manager: _DummyManager,
        configured_child: str,
    ) -> None:
        """STT A/B도 traversal·absolute-outside audio root를 preflight에서 차단한다."""
        from core.audio_quality import AudioFailureKind
        from steps.transcriber import AudioAdmissionError

        tmp_config.paths.audio_input_dir = configured_child

        with pytest.raises(AudioAdmissionError) as exc_info:
            _run(
                run_stt_ab_test(
                    config=tmp_config,
                    source_meeting_id="outside-meeting",
                    variant_a=ModelSpec(label="A", model_id="stt-a"),
                    variant_b=ModelSpec(label="B", model_id="stt-b"),
                    model_manager=dummy_manager,
                )
            )

        assert exc_info.value.failure_kind is AudioFailureKind.SECURITY_BLOCKED
        assert not (tmp_config.paths.resolved_base_dir / "ab_tests").exists()

    @pytest.mark.parametrize(
        "failure_kind_name",
        [
            "MEDIA_INVALID",
            "SOURCE_BUSY",
            "INFRA_UNAVAILABLE",
            "SECURITY_BLOCKED",
        ],
    )
    def test_run_stt_ab_test는_audio_ACCEPT_전에_산출물을_만들지_않는다(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
        dummy_manager: _DummyManager,
        monkeypatch: pytest.MonkeyPatch,
        failure_kind_name: str,
    ) -> None:
        """runner 직접 호출도 metadata·diarize·STT보다 먼저 공통 gate를 실행한다."""
        import core.audio_quality as audio_quality

        planned_id = "ab_20260409-143000_deadbeef"
        validate = MagicMock(return_value=_denied_audio_admission(failure_kind_name))
        ensure_diarize = AsyncMock(
            side_effect=AssertionError(f"preflight bypassed: {failure_kind_name}")
        )
        run_variant = AsyncMock(
            side_effect=AssertionError(f"STT started before admission: {failure_kind_name}")
        )
        monkeypatch.setattr(audio_quality, "validate_audio_quality", validate)
        monkeypatch.setattr(
            ab_test_runner,
            "validate_audio_quality",
            validate,
            raising=False,
        )
        monkeypatch.setattr(ab_test_runner, "_ensure_diarize", ensure_diarize)
        monkeypatch.setattr(ab_test_runner, "_run_stt_variant", run_variant)
        tmp_config.audio_quality.enabled = True

        with pytest.raises(Exception, match=failure_kind_name):
            _run(
                run_stt_ab_test(
                    config=tmp_config,
                    source_meeting_id=meeting_with_merge,
                    variant_a=ModelSpec(label="A", model_id="stt-a"),
                    variant_b=ModelSpec(label="B", model_id="stt-b"),
                    model_manager=dummy_manager,
                    test_id=planned_id,
                )
            )

        validate.assert_called_once()
        ensure_diarize.assert_not_called()
        run_variant.assert_not_called()
        assert not (tmp_config.paths.resolved_base_dir / "ab_tests" / planned_id).exists()

    def test_API_admission뒤_교체된_파일을_runner가_새_identity로_채택하지_않는다(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
        dummy_manager: _DummyManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """외부전송 동의가 route에서 검사한 동일 inode/content에만 결합된다."""
        from core.audio_quality import AudioFailureKind
        from steps.transcriber import AudioAdmissionError

        wav_path, approved_identity = ab_test_runner._inspect_stt_audio_source(
            tmp_config,
            meeting_with_merge,
        )
        wav_path.unlink()
        wav_path.write_bytes(b"replacement audio")
        ensure_diarize = AsyncMock(side_effect=AssertionError("교체 파일 diarize 금지"))
        run_variant = AsyncMock(side_effect=AssertionError("교체 파일 외부전송 금지"))
        monkeypatch.setattr(ab_test_runner, "_ensure_diarize", ensure_diarize)
        monkeypatch.setattr(ab_test_runner, "_run_stt_variant", run_variant)

        with pytest.raises(AudioAdmissionError) as exc_info:
            _run(
                run_stt_ab_test(
                    config=tmp_config,
                    source_meeting_id=meeting_with_merge,
                    variant_a=ModelSpec(label="A", model_id="stt-a"),
                    variant_b=ModelSpec(label="B", model_id="stt-b"),
                    model_manager=dummy_manager,
                    expected_source_identity=approved_identity,
                )
            )

        assert exc_info.value.failure_kind is AudioFailureKind.SOURCE_BUSY
        ensure_diarize.assert_not_called()
        run_variant.assert_not_called()
        assert not (tmp_config.paths.resolved_base_dir / "ab_tests").exists()

    def test_variant_완료직후_취소를_completed로_덮지_않는다(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
        dummy_manager: _DummyManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """마지막 외부 요청 중 취소된 테스트의 최종 상태는 cancelled다."""
        planned_id = "ab_20260822-143000_deadbeef"
        cached_diarize = DiarizationResult(
            segments=[],
            num_speakers=0,
            audio_path="/mock/audio.wav",
        )

        async def finish_after_cancel(**_kwargs: Any) -> dict[str, Any]:
            ab_test_runner._cancel_requests.add(planned_id)
            return {"char_count": {"correct": 1}}

        monkeypatch.setattr(
            ab_test_runner,
            "_ensure_diarize",
            AsyncMock(return_value=cached_diarize),
        )
        monkeypatch.setattr(ab_test_runner, "_run_stt_variant", finish_after_cancel)

        with pytest.raises(asyncio.CancelledError):
            _run(
                run_stt_ab_test(
                    config=tmp_config,
                    source_meeting_id=meeting_with_merge,
                    variant_a=ModelSpec(label="A", model_id="stt-a"),
                    variant_b=ModelSpec(label="B", model_id="stt-b"),
                    model_manager=dummy_manager,
                    test_id=planned_id,
                )
            )

        metadata = ab_test_store.read_metadata(tmp_config, planned_id)
        assert metadata["status"] == "cancelled"
        assert planned_id not in ab_test_runner._cancel_requests

    def test_run_stt_ab_test는_중간_symlink_target을_열지_않는다(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
        dummy_manager: _DummyManager,
    ) -> None:
        """audio_input 경로 중간 symlink는 gate disabled에서도 SECURITY_BLOCKED다."""
        from core.audio_quality import AudioFailureKind
        from steps.transcriber import AudioAdmissionError

        base_dir = tmp_config.paths.resolved_base_dir
        external_input = base_dir / "external" / "input"
        external_input.mkdir(parents=True)
        external_wav = external_input / f"{meeting_with_merge}.wav"
        external_wav.write_bytes(b"external audio")
        (base_dir / "jump").symlink_to(base_dir / "external", target_is_directory=True)
        tmp_config.paths.audio_input_dir = "jump/input"

        with pytest.raises(AudioAdmissionError) as exc_info:
            _run(
                run_stt_ab_test(
                    config=tmp_config,
                    source_meeting_id=meeting_with_merge,
                    variant_a=ModelSpec(label="A", model_id="stt-a"),
                    variant_b=ModelSpec(label="B", model_id="stt-b"),
                    model_manager=dummy_manager,
                )
            )

        assert exc_info.value.failure_kind is AudioFailureKind.SECURITY_BLOCKED
        assert external_wav.read_bytes() == b"external audio"
        assert not (base_dir / "ab_tests").exists()

    def test_base_dir_symlink도_resolve로_숨기지_않고_차단한다(
        self,
        tmp_path: Path,
        dummy_manager: _DummyManager,
    ) -> None:
        """raw base_dir가 symlink면 외부 audio_input target을 열지 않는다."""
        from core.audio_quality import AudioFailureKind
        from steps.transcriber import AudioAdmissionError

        external_base = tmp_path.resolve() / "external-base"
        external_input = external_base / "audio_input"
        external_input.mkdir(parents=True)
        meeting_id = "external-meeting"
        external_wav = external_input / f"{meeting_id}.wav"
        external_wav.write_bytes(b"EXTERNAL")
        linked_base = tmp_path.resolve() / "linked-base"
        linked_base.symlink_to(external_base, target_is_directory=True)
        config = AppConfig().model_copy(update={"paths": PathsConfig(base_dir=str(linked_base))})
        config.audio_quality.enabled = False

        with pytest.raises(AudioAdmissionError) as exc_info:
            _run(
                run_stt_ab_test(
                    config=config,
                    source_meeting_id=meeting_id,
                    variant_a=ModelSpec(label="A", model_id="stt-a"),
                    variant_b=ModelSpec(label="B", model_id="stt-b"),
                    model_manager=dummy_manager,
                )
            )

        assert exc_info.value.failure_kind is AudioFailureKind.SECURITY_BLOCKED
        assert external_wav.read_bytes() == b"EXTERNAL"

    def test_diarize예외도_metadata와_모듈상태를_final_cleanup한다(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
        dummy_manager: _DummyManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """variant 이전 diarize 실패도 pending/current/cancel 상태를 남기지 않는다."""
        planned_id = "ab_20260409-143000_deadbeef"
        ensure_diarize = AsyncMock(side_effect=RuntimeError("diarize boom"))
        monkeypatch.setattr(ab_test_runner, "_ensure_diarize", ensure_diarize)
        ab_test_runner._cancel_requests.add(planned_id)

        with pytest.raises(RuntimeError, match="diarize boom"):
            _run(
                run_stt_ab_test(
                    config=tmp_config,
                    source_meeting_id=meeting_with_merge,
                    variant_a=ModelSpec(label="A", model_id="stt-a"),
                    variant_b=ModelSpec(label="B", model_id="stt-b"),
                    model_manager=dummy_manager,
                    test_id=planned_id,
                )
            )

        meta = ab_test_store.read_metadata(tmp_config, planned_id)
        assert meta["status"] == "failed"
        assert meta["current_variant"] is None
        assert meta["current_step"] is None
        assert "diarize boom" in meta["error"]
        assert ab_test_runner._current_test_id is None
        assert planned_id not in ab_test_runner._cancel_requests

    def test_run_stt_ab_test_diarize_체크포인트_없으면_자동실행(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
        dummy_manager: _DummyManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """명시적으로 허용하면 diarize 체크포인트를 다시 생성한다."""
        # Diarizer/Transcriber/Merger stub
        stub_diarize = DiarizationResult(segments=[], num_speakers=1, audio_path="/fake")

        class StubDiarizer:
            def __init__(self, *a, **kw) -> None:
                pass

            async def diarize(self, wav_path) -> DiarizationResult:
                return stub_diarize

        class StubTranscriber:
            def __init__(self, *a, **kw) -> None:
                pass

            async def transcribe(self, wav_path, **kwargs) -> TranscriptResult:
                return TranscriptResult(segments=[], full_text="")

        class StubMerger:
            async def merge(self, *a, **kw) -> MergedResult:
                return MergedResult(utterances=[], num_speakers=1, audio_path="")

        monkeypatch.setattr(ab_test_runner, "Diarizer", StubDiarizer)
        monkeypatch.setattr(ab_test_runner, "Transcriber", StubTranscriber)
        monkeypatch.setattr(ab_test_runner, "Merger", StubMerger)

        # 에러 없이 완료되어야 함 (자동 diarize 실행)
        test_id = _run(
            run_stt_ab_test(
                config=tmp_config,
                source_meeting_id=meeting_with_merge,
                variant_a=ModelSpec(label="A", model_id="stt-a"),
                variant_b=ModelSpec(label="B", model_id="stt-b"),
                model_manager=dummy_manager,
                allow_diarize_rerun=True,
            )
        )
        assert test_id is not None

    def test_run_stt_ab_test_diarize_재실행_미동의면_모델로드전에_거부(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
        dummy_manager: _DummyManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """기본 OFF 계약에서는 pyannote를 암묵적으로 실행하지 않는다."""
        diarizer = MagicMock()
        monkeypatch.setattr(ab_test_runner, "Diarizer", diarizer)

        with pytest.raises(RuntimeError, match="화자분리 재실행에 동의"):
            _run(
                run_stt_ab_test(
                    config=tmp_config,
                    source_meeting_id=meeting_with_merge,
                    variant_a=ModelSpec(label="A", model_id="stt-a"),
                    variant_b=ModelSpec(label="B", model_id="stt-b"),
                    model_manager=dummy_manager,
                    allow_diarize_rerun=False,
                )
            )

        diarizer.assert_not_called()

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

            async def transcribe(self, wav_path: Path, **kwargs) -> TranscriptResult:
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
    @pytest.mark.parametrize("test_type", ["llm", "stt"])
    def test_reserved_lock_acquire취소는_cancelled로_종결한다(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
        dummy_manager: _DummyManager,
        monkeypatch: pytest.MonkeyPatch,
        test_type: str,
    ) -> None:
        """locked() 확인 뒤 acquire에서 취소되어도 metadata는 terminal 상태다."""
        planned_id = (
            "ab_20260409-143000_a11ce001" if test_type == "llm" else "ab_20260409-143000_a11ce002"
        )
        entered = asyncio.Event()

        class BlockingLock:
            def locked(self) -> bool:
                return False

            async def __aenter__(self) -> None:
                entered.set()
                await asyncio.Event().wait()

            async def __aexit__(self, *args: Any) -> None:
                return None

        fake_lock = BlockingLock()
        monkeypatch.setattr(ab_test_runner, "_get_ab_test_lock", lambda: fake_lock)

        async def scenario() -> None:
            if test_type == "llm":
                operation = run_llm_ab_test(
                    config=tmp_config,
                    source_meeting_id=meeting_with_merge,
                    variant_a=ModelSpec(label="A", model_id="llm-a"),
                    variant_b=ModelSpec(label="B", model_id="llm-b"),
                    scope=LlmScope(),
                    model_manager=dummy_manager,
                    test_id=planned_id,
                )
            else:
                operation = run_stt_ab_test(
                    config=tmp_config,
                    source_meeting_id=meeting_with_merge,
                    variant_a=ModelSpec(label="A", model_id="stt-a"),
                    variant_b=ModelSpec(label="B", model_id="stt-b"),
                    model_manager=dummy_manager,
                    test_id=planned_id,
                )
            task = asyncio.create_task(operation)
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        _run(scenario())
        metadata = ab_test_store.read_metadata(tmp_config, planned_id)
        assert metadata["status"] == "cancelled"
        assert metadata["completed_at"] is not None

    @pytest.mark.parametrize("test_type", ["llm", "stt"])
    def test_route예약후_lock_race는_pending을_failed로_종료한다(
        self,
        tmp_config: AppConfig,
        meeting_with_merge: str,
        dummy_manager: _DummyManager,
        test_type: str,
    ) -> None:
        """route busy check 뒤 생긴 경합도 반환된 ID를 pending으로 남기지 않는다."""
        planned_id = (
            "ab_20260409-143000_deadbeef" if test_type == "llm" else "ab_20260409-143000_feedface"
        )
        variant_a = ModelSpec(label="A", model_id=f"{test_type}-a")
        variant_b = ModelSpec(label="B", model_id=f"{test_type}-b")

        async def scenario() -> None:
            if test_type == "llm":
                merge_path, expected_identity = inspect_llm_ab_source(
                    tmp_config,
                    meeting_with_merge,
                )
                reserve_llm_ab_test(
                    tmp_config,
                    test_id=planned_id,
                    source_meeting_id=meeting_with_merge,
                    merge_path=merge_path,
                    variant_a=variant_a,
                    variant_b=variant_b,
                    scope=LlmScope(),
                )
            else:
                wav_path, expected_identity = ab_test_runner._inspect_stt_audio_source(
                    tmp_config,
                    meeting_with_merge,
                )
                reserve_stt_ab_test(
                    tmp_config,
                    test_id=planned_id,
                    source_meeting_id=meeting_with_merge,
                    wav_path=wav_path,
                    variant_a=variant_a,
                    variant_b=variant_b,
                    allow_diarize_rerun=True,
                )

            lock = ab_test_runner._get_ab_test_lock()
            await lock.acquire()
            try:
                with pytest.raises(RuntimeError, match="이미 진행 중"):
                    if test_type == "llm":
                        await run_llm_ab_test(
                            config=tmp_config,
                            source_meeting_id=meeting_with_merge,
                            variant_a=variant_a,
                            variant_b=variant_b,
                            scope=LlmScope(),
                            model_manager=dummy_manager,
                            test_id=planned_id,
                            expected_merge_identity=expected_identity,
                            metadata_reserved=True,
                        )
                    else:
                        await run_stt_ab_test(
                            config=tmp_config,
                            source_meeting_id=meeting_with_merge,
                            variant_a=variant_a,
                            variant_b=variant_b,
                            model_manager=dummy_manager,
                            test_id=planned_id,
                            metadata_reserved=True,
                        )
            finally:
                lock.release()

        _run(scenario())
        metadata = ab_test_store.read_metadata(tmp_config, planned_id)
        assert metadata["status"] == "failed"
        assert "이미 진행 중" in metadata["error"]

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
