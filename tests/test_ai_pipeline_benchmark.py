"""AI pipeline benchmark harness 단위 테스트."""

from __future__ import annotations

import argparse

import pytest

from config import AppConfig
from scripts.benchmark_ai_pipeline import (
    _apply_overrides,
    _bool_arg,
    _find_latest_audio,
    _quality_for_merge,
)
from steps.merger import MergedResult, MergedUtterance


def test_bool_arg_parses_common_values() -> None:
    """CLI bool parser가 일반적인 true/false 표현을 처리한다."""
    assert _bool_arg("true") is True
    assert _bool_arg("off") is False
    with pytest.raises(argparse.ArgumentTypeError):
        _bool_arg("maybe")


def test_apply_overrides_updates_ai_variant_config() -> None:
    """CLI variant override가 AppConfig에 반영된다."""
    config = AppConfig()
    args = argparse.Namespace(
        stt_word_timestamps=False,
        vad_mode="auto",
        diarization_model="pyannote/speaker-diarization-community-1",
        diarization_output_mode="exclusive",
        correction_mode="changed_only",
        no_adaptive_correction_tokens=True,
    )

    _apply_overrides(config, args)

    assert config.stt.word_timestamps is False
    assert config.vad.mode == "auto"
    assert config.vad.enabled is True
    assert config.diarization.model_name == "pyannote/speaker-diarization-community-1"
    assert config.diarization.output_mode == "exclusive"
    assert config.llm.correction_mode == "changed_only"
    assert config.llm.correction_adaptive_max_tokens is False


def test_find_latest_audio_uses_config_input_dir(tmp_path) -> None:
    """audio 인자가 없을 때 입력 폴더의 최신 지원 오디오를 찾는다."""
    config = AppConfig(paths={"base_dir": str(tmp_path)})
    input_dir = config.paths.resolved_audio_input_dir
    input_dir.mkdir(parents=True)
    older = input_dir / "older.wav"
    newer = input_dir / "newer.m4a"
    older.write_bytes(b"old")
    newer.write_bytes(b"new")

    assert _find_latest_audio(config) == newer


def test_quality_for_merge_reports_unknown_ratio() -> None:
    """병합 품질 지표는 UNKNOWN 비율을 계산한다."""
    result = MergedResult(
        utterances=[
            MergedUtterance("a", "SPEAKER_00", 0.0, 1.0),
            MergedUtterance("b", "UNKNOWN", 1.0, 2.0),
        ],
        num_speakers=1,
        audio_path="/tmp/a.wav",
        unknown_count=1,
    )

    quality = _quality_for_merge(result)

    assert quality["utterance_count"] == 2
    assert quality["unknown_ratio"] == 0.5
