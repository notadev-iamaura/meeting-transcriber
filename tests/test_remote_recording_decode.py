"""상대 트랙 전사의 실제 입력·디지털 무음·긴 연속 발화를 검사한다."""

from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from core.remote_recording_decode import transcribe_remote_recording


@pytest.mark.parametrize("rate", [8000, 16000, 44100])
def test_remote_keeps_quiet_greeting_only_where_spoken(tmp_path: Path, rate: int) -> None:
    """발화의 표기나 크기를 기준으로 삭제하지 않고 오직 디지털 무음의 단어만 제외한다."""
    path = tmp_path / "remote.wav"
    data = np.zeros(rate * 8, dtype=np.float32)
    data[2 * rate : 3 * rate] = 2 / 32768
    sf.write(path, data, rate, subtype="PCM_16")
    calls = []

    def recognize(samples: np.ndarray, **kwargs: Any) -> dict[str, Any]:
        calls.append((len(samples), kwargs["temperature"]))
        return {
            "segments": [
                {
                    "text": " 감사합니다. 감사합니다.",
                    "start": 1.2,
                    "end": 3.8,
                    "words": [
                        {"start": 1.2, "end": 1.7, "word": " 감사합니다."},
                        {"start": 3.6, "end": 3.8, "word": " 감사합니다."},
                    ],
                },
                {"text": "감사합니다.", "start": 3.9, "end": 3.92},
            ]
        }

    result = transcribe_remote_recording(recognize, str(path), [1.0, 5.0, 6.0, 7.0])
    assert calls == [(64000, 0.0)]
    assert result["text"] == "감사합니다."
    assert [(s["start"], s["end"]) for s in result["segments"]] == [(2.2, 2.7)]


def test_long_continuous_clip_keeps_boundary_words_once_and_late_short_reply(
    tmp_path: Path,
) -> None:
    """30초 창을 넘는 연속 신호 중 겹쳐 읽은 경계 단어와 마지막 100ms 답변을 보존한다."""
    rate = 16000
    path = tmp_path / "continuous.wav"
    duration = 60.1
    data = np.full(round(rate * duration), 1 / 32768, dtype=np.float32)
    # 경계 양쪽 문맥에서 읽힐 단어를 포함해 고유 진폭으로 시간 순서를 추적한다.
    times = [2.0, 20.02, 39.87, 60.0]
    for i, time in enumerate(times, start=1):
        data[round(time * rate) : round((time + 0.10) * rate)] = i / 10
    sf.write(path, data, rate)
    sizes = []

    def recognize(samples: np.ndarray, **_kwargs: Any) -> dict[str, Any]:
        sizes.append(len(samples))
        segments = []
        for i in range(1, len(times) + 1):
            hits = np.flatnonzero(abs(samples - i / 10) < 0.001)
            if hits.size:
                segments.append(
                    {"start": hits[0] / rate, "end": (hits[-1] + 1) / rate, "text": str(i)}
                )
        return {"segments": segments}

    result = transcribe_remote_recording(recognize, str(path), [0.0, duration])
    assert len(sizes) > 1 and max(sizes) <= 30 * rate
    assert [s["text"] for s in result["segments"]] == ["1", "2", "3", "4"]
    assert [s["start"] for s in result["segments"]] == pytest.approx(times, abs=0.01)
