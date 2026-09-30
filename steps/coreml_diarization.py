"""CoreML 화자분리 어댑터. 무거운 라이브러리는 worker에서만 import한다."""

from __future__ import annotations

import importlib.util
import math
import os
import platform
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_RIFF_HEADER_SIZE = 12  # "RIFF" + 크기(4) + "WAVE"
_CHUNK_HEADER_SIZE = 8  # 청크 ID(4) + 청크 크기(4)


def coreml_install_issue(engine: str, binary: str = "recap-speakrs") -> str | None:
    """네트워크/모델 로드 없이 설치 전제 조건을 확인한다."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return f"{engine}는 Apple Silicon Mac이 필요합니다. 설정에서 community-1을 선택하세요."
    if engine == "senko":
        version = platform.mac_ver()[0]
        if version and int(version.split(".")[0]) < 14:
            return "Senko CoreML은 macOS 14 이상이 필요합니다."
        if any(importlib.util.find_spec(name) is None for name in ("senko", "coremltools")):
            return "Senko/CoreML 의존성이 없습니다. README의 Senko venv 설치 절차를 실행하세요."
    elif engine == "speakrs":
        if shutil.which(binary) is None:
            return "recap-speakrs가 없습니다. README의 binary 설치 후 diarization.speakrs_binary를 설정하세요."
    else:
        return f"지원하지 않는 화자분리 엔진: {engine}"
    return None


def normalize_segments(rows: Any) -> list[dict[str, Any]]:
    """어댑터 결과의 시간/화자 필드를 검증하고 공통 형식으로 변환한다."""
    if not isinstance(rows, list):
        raise RuntimeError("화자분리 segments는 목록이어야 합니다.")
    segments = []
    for row in rows:
        start, end = float(row["start"]), float(row["end"])
        speaker = str(row["speaker"])
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise RuntimeError("화자분리 구간 시간이 유효하지 않습니다.")
        if not speaker:
            raise RuntimeError("화자분리 화자 ID가 비어 있습니다.")
        segments.append({"start": start, "end": end, "speaker": speaker})
    return sorted(segments, key=lambda segment: segment["start"])


def _find_wav_data_chunk_size_offset(raw: bytes) -> int | None:
    """RIFF/WAVE 청크를 순회해 data 청크의 크기 필드 오프셋을 찾는다.

    RIFF/WAVE 매직이 아니거나 data 청크를 찾지 못하면(다른 포맷이거나 헤더가
    파싱 불가능할 정도로 손상됨) None을 반환해 정규화를 건너뛰게 한다.
    """
    if len(raw) < _RIFF_HEADER_SIZE or raw[0:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return None
    offset = _RIFF_HEADER_SIZE
    while offset + _CHUNK_HEADER_SIZE <= len(raw):
        chunk_id = raw[offset : offset + 4]
        chunk_size = int.from_bytes(raw[offset + 4 : offset + 8], "little")
        if chunk_id == b"data":
            return offset + 4
        next_offset = offset + _CHUNK_HEADER_SIZE + chunk_size + (chunk_size % 2)
        if next_offset <= offset:
            return None
        offset = next_offset
    return None


def _normalize_wav_header(raw: bytearray, data_size_offset: int) -> bool:
    """실제 payload 바이트 수 기준으로 RIFF/data 청크 크기를 바로잡는다.

    force-kill된 FFmpeg는 RIFF 헤더의 크기 필드를 최종 패치하지 못해 실제
    바이트 수와 어긋난 값(0, placeholder, 잘린 값 등)을 남긴다. PCM 샘플은
    그대로 두고 크기 필드만 실제 파일 길이에 맞게 재계산한다.

    Returns:
        헤더를 실제로 고쳤으면 True, 이미 정상이면 False.
    """
    declared_data_size = int.from_bytes(raw[data_size_offset : data_size_offset + 4], "little")
    actual_data_size = len(raw) - (data_size_offset + 4)
    declared_riff_size = int.from_bytes(raw[4:8], "little")
    actual_riff_size = len(raw) - 8
    if declared_data_size == actual_data_size and declared_riff_size == actual_riff_size:
        return False
    raw[data_size_offset : data_size_offset + 4] = actual_data_size.to_bytes(4, "little")
    raw[4:8] = actual_riff_size.to_bytes(4, "little")
    return True


@contextmanager
def _senko_audio_path(audio_path: Path) -> Iterator[Path]:
    """Senko 입력 전용 오디오 경로를 제공한다. 원본 파일은 절대 덮어쓰지 않는다.

    녹음 정지 시 FFmpeg가 force-kill되면 RIFF data 청크 크기가 실제 바이트 수와
    어긋나 Senko가 "Invalid WAV data chunk size"로 거부하고 0개 세그먼트를
    반환한다. 헤더가 어긋난 경우에만 같은 PCM 샘플로 헤더를 바로잡은 임시
    사본을 만들어 그 경로를 넘긴다. 헤더가 이미 정상이면 원본 경로를 그대로
    사용해 불필요한 복사를 피한다.
    """
    raw = bytearray(audio_path.read_bytes())
    data_size_offset = _find_wav_data_chunk_size_offset(bytes(raw))
    if data_size_offset is None or not _normalize_wav_header(raw, data_size_offset):
        yield audio_path
        return

    tmp_fd, tmp_name = tempfile.mkstemp(suffix=".wav", prefix="senko-normalized-")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(raw)
        yield tmp_path
    finally:
        tmp_path.unlink(missing_ok=True)


def run_coreml(payload: dict[str, Any]) -> dict[str, Any]:
    """Senko를 실행하거나 같은 PID를 speakrs sidecar로 교체한다."""
    from steps.transcriber import inspect_audio_path_no_symlinks

    engine = str(payload["engine"])
    binary = str(payload.get("speakrs_binary", "recap-speakrs"))
    issue = coreml_install_issue(engine, binary)
    if issue:
        raise RuntimeError(issue)
    audio_path = Path(payload["audio_path"])
    expected_identity = tuple(payload["audio_identity"])
    if engine == "speakrs":
        # exec로 PID를 보존해 부모의 pause/timeout/cancel이 Rust 추론에도 적용된다.
        os.execvp(binary, [binary, str(audio_path), str(payload["output_path"])])
        raise RuntimeError("speakrs exec가 반환되었습니다.")

    try:
        import senko

        diarizer = senko.Diarizer(device="coreml", warmup=False, quiet=True)
    except ImportError as exc:
        raise RuntimeError("Senko 로드 실패. README의 Senko venv 설치 절차를 확인하세요.") from exc
    if inspect_audio_path_no_symlinks(audio_path) != expected_identity:
        raise RuntimeError("Senko 모델 로드 중 오디오가 변경되었습니다.")
    with _senko_audio_path(audio_path) as senko_input_path:
        result = diarizer.diarize(str(senko_input_path), generate_colors=False)
    # merged_segments는 짧은 발화를 제거하고 최대 4초 무음까지 합친다.
    # 전사와 시간 겹침으로 병합하므로 raw_segments를 보존한다.
    segments = normalize_segments([] if result is None else result["raw_segments"])
    return {
        "segments": segments,
        "num_speakers": len({segment["speaker"] for segment in segments}),
        "audio_path": str(audio_path),
        "model_name": "senko-coreml",
        "output_mode": "exclusive",
    }
