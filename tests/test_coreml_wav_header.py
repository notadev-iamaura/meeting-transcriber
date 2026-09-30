"""force-kill된 FFmpeg가 남긴 불완전한 WAV 헤더를 Senko 입력 전용으로
정규화하는 로직(steps/coreml_diarization._senko_audio_path)을 검증한다.

원본 오디오 파일은 어떤 경우에도 덮어써서는 안 되며, 헤더가 이미 정상이면
불필요한 임시 복사를 만들지 않아야 한다.
"""

from __future__ import annotations

import struct
import sys
import wave
from pathlib import Path
from unittest.mock import MagicMock

from steps.coreml_diarization import _senko_audio_path, run_coreml
from steps.transcriber import inspect_audio_path_no_symlinks

_NUM_FRAMES = 1600
_PCM_FRAME = b"\x12\x34"


def _write_valid_wav(path: Path, num_frames: int = _NUM_FRAMES) -> bytes:
    """16kHz mono 16bit PCM WAV을 생성하고 원본 바이트를 반환한다."""
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(_PCM_FRAME * num_frames)
    return path.read_bytes()


def _corrupt_data_chunk_size(
    raw: bytes,
    *,
    declared_data_size: int,
    declared_riff_size: int | None = None,
) -> bytes:
    """RIFF data 청크 크기 필드를 실제 값과 다르게 만들어 force-kill 시나리오를 재현한다.

    Python wave 모듈이 만드는 최소 헤더(RIFF/fmt /data, 확장 청크 없음)는
    data 청크 ID가 offset 36, 크기 필드가 offset 40에 위치한다.
    """
    buf = bytearray(raw)
    assert buf[36:40] == b"data", "wave 모듈 헤더 레이아웃 가정이 깨졌습니다."
    buf[40:44] = struct.pack("<I", declared_data_size)
    if declared_riff_size is not None:
        buf[4:8] = struct.pack("<I", declared_riff_size)
    return bytes(buf)


def _payload(tmp_path: Path, audio_path: Path) -> dict:
    return {
        "engine": "senko",
        "model_name": "senko-coreml",
        "audio_path": str(audio_path),
        "audio_identity": list(inspect_audio_path_no_symlinks(audio_path)),
        "output_path": str(tmp_path / "output.json"),
        "speakrs_binary": "recap-speakrs",
    }


# === _senko_audio_path 단위 테스트 ===


def test_broken_header_produces_normalized_temp_copy_and_preserves_original(
    tmp_path: Path,
) -> None:
    """data 청크 크기가 실제 바이트 수보다 큰(미완성 녹음 전형) WAV를 정규화한다."""
    audio_path = tmp_path / "broken.wav"
    valid_bytes = _write_valid_wav(audio_path)
    # force-kill로 인해 갱신되지 못한 큰 placeholder 크기를 시뮬레이션한다.
    corrupted = _corrupt_data_chunk_size(valid_bytes, declared_data_size=0xFFFFFFFF)
    audio_path.write_bytes(corrupted)

    with _senko_audio_path(audio_path) as senko_path:
        assert senko_path != audio_path
        assert senko_path.exists()

        normalized = senko_path.read_bytes()
        data_size = struct.unpack_from("<I", normalized, 40)[0]
        riff_size = struct.unpack_from("<I", normalized, 4)[0]
        assert data_size == len(normalized) - 44
        assert riff_size == len(normalized) - 8

        with wave.open(str(senko_path), "rb") as wav_file:
            assert wav_file.getnframes() == _NUM_FRAMES
            assert wav_file.readframes(_NUM_FRAMES) == _PCM_FRAME * _NUM_FRAMES

        # 원본 파일은 손상된 상태 그대로 변경되지 않아야 한다.
        assert audio_path.read_bytes() == corrupted

    # 컨텍스트 종료 후 임시 파일은 정리된다.
    assert not senko_path.exists()


def test_truncated_riff_size_is_also_normalized(tmp_path: Path) -> None:
    """RIFF 전체 크기 필드도 실제 바이트 수와 어긋나면 함께 바로잡는다."""
    audio_path = tmp_path / "truncated.wav"
    valid_bytes = _write_valid_wav(audio_path)
    corrupted = _corrupt_data_chunk_size(
        valid_bytes, declared_data_size=0, declared_riff_size=0
    )
    audio_path.write_bytes(corrupted)

    with _senko_audio_path(audio_path) as senko_path:
        normalized = senko_path.read_bytes()
        assert struct.unpack_from("<I", normalized, 40)[0] == len(normalized) - 44
        assert struct.unpack_from("<I", normalized, 4)[0] == len(normalized) - 8
        assert audio_path.read_bytes() == corrupted


def test_valid_header_yields_original_path_without_temp_copy(tmp_path: Path) -> None:
    """헤더가 이미 정상이면 원본 경로를 그대로 사용하고 임시 파일을 만들지 않는다."""
    audio_path = tmp_path / "ok.wav"
    valid_bytes = _write_valid_wav(audio_path)
    before = set(tmp_path.iterdir())

    with _senko_audio_path(audio_path) as senko_path:
        assert senko_path == audio_path
        assert audio_path.read_bytes() == valid_bytes
        assert set(tmp_path.iterdir()) == before

    assert audio_path.read_bytes() == valid_bytes


def test_non_wav_content_passes_through_unchanged(tmp_path: Path) -> None:
    """RIFF/WAVE 매직이 아닌 내용은 정규화 대상이 아니므로 원본 경로를 그대로 쓴다."""
    audio_path = tmp_path / "notwav.bin"
    audio_path.write_bytes(b"not a wav file at all")

    with _senko_audio_path(audio_path) as senko_path:
        assert senko_path == audio_path


# === run_coreml 통합 테스트 (senko 모킹) ===


def test_run_coreml_feeds_normalized_temp_path_to_senko(
    tmp_path: Path, monkeypatch
) -> None:
    """run_coreml은 헤더가 깨진 오디오일 때 정규화된 임시 경로를 Senko에 넘긴다."""
    audio_path = tmp_path / "input.wav"
    valid_bytes = _write_valid_wav(audio_path)
    corrupted = _corrupt_data_chunk_size(valid_bytes, declared_data_size=0xFFFFFFFF)
    audio_path.write_bytes(corrupted)
    payload = _payload(tmp_path, audio_path)

    captured_paths: list[str] = []

    def fake_diarize(path: str, generate_colors: bool = False) -> dict:
        captured_paths.append(path)
        # 호출 시점에 임시 파일이 존재하고 헤더가 정상인지 확인한다.
        data = Path(path).read_bytes()
        assert struct.unpack_from("<I", data, 40)[0] == len(data) - 44
        return {
            "raw_segments": [{"speaker": "SPEAKER_00", "start": 0.0, "end": 0.5}],
            "merged_segments": [],
        }

    senko = MagicMock()
    senko.Diarizer.return_value.diarize.side_effect = fake_diarize
    monkeypatch.setitem(sys.modules, "senko", senko)
    monkeypatch.setattr("steps.coreml_diarization.coreml_install_issue", lambda *a: None)

    result = run_coreml(payload)

    assert len(captured_paths) == 1
    used_path = Path(captured_paths[0])
    assert used_path != audio_path
    assert not used_path.exists()  # run_coreml 반환 시점에 임시 파일은 정리됨
    assert audio_path.read_bytes() == corrupted  # 원본 불변
    assert result["audio_path"] == str(audio_path)  # 계약: 결과의 audio_path는 원본
    assert result["segments"] == [{"speaker": "SPEAKER_00", "start": 0.0, "end": 0.5}]


def test_run_coreml_uses_original_path_when_header_already_valid(
    tmp_path: Path, monkeypatch
) -> None:
    """헤더가 정상인 오디오는 불필요한 복사 없이 원본 경로를 그대로 Senko에 넘긴다."""
    audio_path = tmp_path / "input.wav"
    valid_bytes = _write_valid_wav(audio_path)
    payload = _payload(tmp_path, audio_path)

    captured_paths: list[str] = []

    def fake_diarize(path: str, generate_colors: bool = False) -> dict:
        captured_paths.append(path)
        return {"raw_segments": [], "merged_segments": []}

    senko = MagicMock()
    senko.Diarizer.return_value.diarize.side_effect = fake_diarize
    monkeypatch.setitem(sys.modules, "senko", senko)
    monkeypatch.setattr("steps.coreml_diarization.coreml_install_issue", lambda *a: None)

    run_coreml(payload)

    assert captured_paths == [str(audio_path)]
    assert audio_path.read_bytes() == valid_bytes


def test_run_coreml_cleans_up_temp_file_even_if_senko_raises(
    tmp_path: Path, monkeypatch
) -> None:
    """Senko diarize가 예외를 던져도 임시 파일은 finally에서 정리된다."""
    audio_path = tmp_path / "input.wav"
    valid_bytes = _write_valid_wav(audio_path)
    corrupted = _corrupt_data_chunk_size(valid_bytes, declared_data_size=0xFFFFFFFF)
    audio_path.write_bytes(corrupted)
    payload = _payload(tmp_path, audio_path)

    captured_paths: list[str] = []

    def fake_diarize(path: str, generate_colors: bool = False) -> dict:
        captured_paths.append(path)
        raise RuntimeError("senko 추론 실패")

    senko = MagicMock()
    senko.Diarizer.return_value.diarize.side_effect = fake_diarize
    monkeypatch.setitem(sys.modules, "senko", senko)
    monkeypatch.setattr("steps.coreml_diarization.coreml_install_issue", lambda *a: None)

    try:
        run_coreml(payload)
    except RuntimeError:
        pass

    assert len(captured_paths) == 1
    assert not Path(captured_paths[0]).exists()
    assert audio_path.read_bytes() == corrupted

def test_trailing_junk_chunk_is_not_absorbed_into_data(tmp_path: Path) -> None:
    """data 뒤에 JUNK 청크가 있는 정상 WAV는 헤더를 바꾸지 않고 원본 경로를 쓴다."""
    audio_path = tmp_path / "trailing.wav"
    valid = bytearray(_write_valid_wav(audio_path, num_frames=16))
    junk = b"JUNK" + struct.pack("<I", 4) + b"pad!"
    valid.extend(junk)
    valid[4:8] = struct.pack("<I", len(valid) - 8)
    audio_path.write_bytes(valid)
    original = audio_path.read_bytes()
    assert struct.unpack_from("<I", original, 40)[0] == 32

    with _senko_audio_path(audio_path) as senko_path:
        assert senko_path == audio_path
        assert audio_path.read_bytes() == original


def test_odd_sized_data_with_pad_byte_is_left_alone(tmp_path: Path) -> None:
    """홀수 data + RIFF 패딩 1바이트는 data 크기를 늘리지 않는다."""
    audio_path = tmp_path / "odd.wav"
    fmt = struct.pack("<HHIIHH", 1, 1, 16000, 16000, 1, 8)
    data_payload = b"\x7f"
    chunks = (
        b"fmt "
        + struct.pack("<I", 16)
        + fmt
        + b"data"
        + struct.pack("<I", 1)
        + data_payload
        + b"\x00"
    )
    raw = bytearray(b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks)
    audio_path.write_bytes(raw)
    original = bytes(raw)

    with _senko_audio_path(audio_path) as senko_path:
        assert senko_path == audio_path
        assert audio_path.read_bytes() == original
        assert struct.unpack_from("<I", original, 40)[0] == 1


def test_header_repair_does_not_load_entire_file_into_memory(
    tmp_path: Path, monkeypatch
) -> None:
    """정규화 경로는 read_bytes로 전체 PCM을 올리지 않고 헤더만 스캔한다."""
    audio_path = tmp_path / "bigish.wav"
    valid_bytes = _write_valid_wav(audio_path)
    corrupted = _corrupt_data_chunk_size(valid_bytes, declared_data_size=0xFFFFFFFF)
    audio_path.write_bytes(corrupted)

    def forbid_read_bytes(self):  # noqa: ANN001
        raise AssertionError("read_bytes must not be used for Senko WAV normalize")

    monkeypatch.setattr(Path, "read_bytes", forbid_read_bytes)

    with _senko_audio_path(audio_path) as senko_path:
        assert senko_path != audio_path
        # 검증용으로만 임시 파일을 직접 연다 (Path.read_bytes 패치 우회)
        with open(senko_path, "rb") as f:
            data = f.read()
        assert struct.unpack_from("<I", data, 40)[0] == len(data) - 44
