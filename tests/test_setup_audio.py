"""FFmpeg 장치 나열과 macOS 오디오 셋업의 연결을 검증한다."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("has_aggregate", [False, True])
def test_check_recognizes_audio_devices_despite_ffmpeg_list_exit(
    tmp_path: Path, has_aggregate: bool
) -> None:
    """입력 없는 FFmpeg가 목록을 출력한 뒤 실패해도 오디오 장치를 감지한다."""
    uname = tmp_path / "uname"
    uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
    uname.chmod(0o755)
    listing = [
        "[AVFoundation indev @ x] AVFoundation video devices:",
        "[AVFoundation indev @ x] [0] Meeting Transcriber Aggregate",
        "[AVFoundation indev @ x] AVFoundation audio devices:",
        "[AVFoundation indev @ x] [0] BlackHole 2ch",
    ]
    if has_aggregate:
        listing.append("[AVFoundation indev @ x] [1] Meeting Transcriber Aggregate")
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text(
        "#!/bin/sh\ncat >&2 <<'DEVICE_LIST'\n"
        + "\n".join(listing)
        + "\nError opening input file .\nDEVICE_LIST\nexit 1\n",
        encoding="utf-8",
    )
    ffmpeg.chmod(0o755)

    setup_script = Path(__file__).resolve().parents[1] / "scripts" / "setup_audio.sh"
    result = subprocess.run(
        ["bash", str(setup_script), "--check"],
        env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.defpath}"},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert "BlackHole 2ch 감지됨" in result.stdout
    assert result.returncode == (0 if has_aggregate else 2)
    assert ("Aggregate Device 감지됨" in result.stdout) is has_aggregate
