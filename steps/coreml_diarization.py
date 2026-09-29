"""CoreML 화자분리 어댑터. 무거운 라이브러리는 worker에서만 import한다."""

from __future__ import annotations

import importlib.util
import math
import os
import platform
import shutil
from pathlib import Path
from typing import Any


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
    result = diarizer.diarize(str(audio_path), generate_colors=False)
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
