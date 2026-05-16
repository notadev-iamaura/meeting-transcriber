"""화자분리 worker 프로토콜 테스트."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

from steps.diarization_worker import _run


class FakeTurn:
    """pyannote Segment 대체 객체."""

    def __init__(self, start: float, end: float) -> None:
        self.start = start
        self.end = end


class FakeAnnotation:
    """pyannote Annotation 대체 객체."""

    def itertracks(self, yield_label: bool = False) -> list[tuple[FakeTurn, None, str]]:
        return [
            (FakeTurn(0.0, 1.23456), None, "SPEAKER_00"),
            (FakeTurn(1.5, 3.0), None, "SPEAKER_01"),
        ]


class FakePipeline:
    """pyannote Pipeline 대체 객체."""

    loaded_token: str | None = None
    received_params: dict[str, Any] | None = None
    received_device: Any = None

    @classmethod
    def from_pretrained(cls, model_name: str, token: str) -> FakePipeline:
        cls.loaded_token = token
        return cls()

    def to(self, device: Any) -> None:
        type(self).received_device = device

    def __call__(self, audio_path: str, **params: Any) -> FakeAnnotation:
        type(self).received_params = params
        return FakeAnnotation()


def test_worker_writes_diarization_json(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """worker가 stdin payload 상당 설정으로 결과 JSON을 생성한다."""
    audio_path = tmp_path / "audio.wav"
    output_path = tmp_path / "result.json"
    audio_path.write_bytes(b"RIFF" + b"\x00" * 100)

    pyannote_module = types.ModuleType("pyannote")
    pyannote_audio_module = types.ModuleType("pyannote.audio")
    pyannote_audio_module.Pipeline = FakePipeline
    torch_module = types.ModuleType("torch")
    torch_module.device = lambda name: name

    monkeypatch.setitem(sys.modules, "pyannote", pyannote_module)
    monkeypatch.setitem(sys.modules, "pyannote.audio", pyannote_audio_module)
    monkeypatch.setitem(sys.modules, "torch", torch_module)

    _run(
        {
            "model_name": "pyannote/speaker-diarization-3.1",
            "audio_path": str(audio_path),
            "output_path": str(output_path),
            "huggingface_token": "hf_test",
            "min_speakers": 1,
            "max_speakers": 2,
        }
    )

    data = json.loads(output_path.read_text(encoding="utf-8"))
    assert data["num_speakers"] == 2
    assert data["audio_path"] == str(audio_path)
    assert data["segments"] == [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 1.235},
        {"speaker": "SPEAKER_01", "start": 1.5, "end": 3.0},
    ]
    assert FakePipeline.loaded_token == "hf_test"
    assert FakePipeline.received_device == "cpu"
    assert FakePipeline.received_params == {"min_speakers": 1, "max_speakers": 2}
