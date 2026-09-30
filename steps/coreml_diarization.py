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


_HEADER_SCAN_LIMIT = 65536  # fmt/LIST 등 선행 청크만 읽는다. 전체 PCM은 로드하지 않는다.


def _find_wav_data_chunk_size_offset(header: bytes) -> int | None:
    """헤더 바이트에서 data 청크 크기 필드 오프셋을 찾는다.

    RIFF/WAVE가 아니거나 data를 못 찾으면 None. 긴 PCM 본체는 스캔하지 않도록
    호출측이 앞부분만 넘긴다.
    """
    if len(header) < _RIFF_HEADER_SIZE or header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
        return None
    offset = _RIFF_HEADER_SIZE
    while offset + _CHUNK_HEADER_SIZE <= len(header):
        chunk_id = header[offset : offset + 4]
        chunk_size = int.from_bytes(header[offset + 4 : offset + 8], "little")
        if chunk_id == b"data":
            return offset + 4
        # data 이전 청크는 정상 경계로 건너뛴다. 크기 필드가 헤더 스캔 범위를
        # 벗어나면(손상/미완성) 더 이상 진행하지 않는다.
        next_offset = offset + _CHUNK_HEADER_SIZE + chunk_size + (chunk_size % 2)
        if next_offset <= offset or next_offset > len(header):
            return None
        offset = next_offset
    return None


def _wav_header_repair(
    *,
    file_size: int,
    header: bytes,
    data_size_offset: int,
) -> tuple[int, int] | None:
    """고쳐야 할 (riff_size, data_size)를 반환한다. 정상이면 None.

    - declared_data > 파일에 남은 바이트: force-kill/절단 → data를 EOF까지로 clamp.
    - declared_data < 남은 바이트이고 그 뒤에 다른 청크가 파싱되면: 유효한 trailing
      chunk(JUNK 등)이므로 data 크기는 유지. RIFF 크기만 틀렸으면 RIFF만 수정.
    - declared_data == 0 이고 payload가 있으며 trailing chunk가 없으면: 미완성으로
      보고 EOF까지 clamp (FFmpeg가 0 placeholder를 남긴 경우).
    - odd-sized data의 RIFF 패딩 바이트는 data 크기에 포함하지 않는다.
    """
    declared_data = int.from_bytes(header[data_size_offset : data_size_offset + 4], "little")
    declared_riff = int.from_bytes(header[4:8], "little")
    payload_start = data_size_offset + 4
    available = file_size - payload_start
    if available < 0:
        return None

    actual_riff = file_size - 8
    new_data = declared_data
    new_riff = declared_riff

    if declared_data > available:
        # 헤더가 약속한 길이보다 파일이 짧다 → 미완성. data를 실바이트로 clamp.
        new_data = available
    elif declared_data < available:
        # trailing 메타데이터 청크가 있는지 확인한다.
        pad = declared_data % 2
        next_off = payload_start + declared_data + pad
        has_trailing = False
        if next_off + _CHUNK_HEADER_SIZE <= file_size and next_off + _CHUNK_HEADER_SIZE <= len(
            header
        ):
            # 헤더 버퍼 안에 next chunk id가 있으면 검사
            chunk_id = header[next_off : next_off + 4]
            has_trailing = chunk_id.isalnum() or chunk_id in {
                b"JUNK",
                b"LIST",
                b"fact",
                b"PEAK",
                b"cue ",
            }
        elif next_off + _CHUNK_HEADER_SIZE <= file_size:
            # next chunk가 헤더 스캔 밖(드묾) — data를 확장하지 않고 통과
            has_trailing = True
        if has_trailing:
            new_data = declared_data  # 유지
        elif declared_data == 0:
            new_data = available
        else:
            # declared < available 이지만 trailing이 아니면 보수적으로 통과
            new_data = declared_data
    # declared_data == available: 유지

    # odd-sized final data의 패딩 1바이트는 RIFF 길이에만 영향. data 필드 값은 홀수 유지.
    if new_data != declared_data or new_riff != actual_riff:
        # trailing을 보존하는 경우에도 파일 길이 기준 RIFF 크기가 틀리면 고친다.
        new_riff = actual_riff
        if new_data == declared_data and new_riff == declared_riff:
            return None
        return (new_riff, new_data)
    return None


@contextmanager
def _senko_audio_path(audio_path: Path) -> Iterator[Path]:
    """Senko 입력 전용 오디오 경로를 제공한다. 원본 파일은 절대 덮어쓰지 않는다.

    녹음 정지 시 FFmpeg가 force-kill되면 RIFF data 청크 크기가 실제 바이트 수와
    어긋나 Senko가 "Invalid WAV data chunk size"로 거부하고 0개 세그먼트를
    반환한다. 헤더가 어긋난 경우에만 같은 PCM으로 헤더만 고친 임시 사본을
    만든다. 전체 파일을 Python 메모리에 올리지 않고, 앞부분 헤더만 파싱한 뒤
    필요할 때만 shutil.copyfile + in-place 패치한다.
    """
    file_size = audio_path.stat().st_size
    with audio_path.open("rb") as src:
        header = src.read(_HEADER_SCAN_LIMIT)
    data_size_offset = _find_wav_data_chunk_size_offset(header)
    if data_size_offset is None:
        yield audio_path
        return
    repair = _wav_header_repair(
        file_size=file_size, header=header, data_size_offset=data_size_offset
    )
    if repair is None:
        yield audio_path
        return

    new_riff, new_data = repair
    tmp_fd, tmp_name = tempfile.mkstemp(suffix=".wav", prefix="senko-normalized-")
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        shutil.copyfile(audio_path, tmp_path)
        with tmp_path.open("r+b") as out:
            out.seek(4)
            out.write(new_riff.to_bytes(4, "little"))
            out.seek(data_size_offset)
            out.write(new_data.to_bytes(4, "little"))
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
