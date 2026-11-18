"""CoreAudio 장치를 열지 않고 실제 Swift 오류 신호와 부분 WAV 마감을 검증한다."""

from __future__ import annotations

import shutil
import subprocess
import sys
import wave
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swiftc") is None,
    reason="macOS Swift 컴파일러가 필요한 장치 없는 네이티브 검사",
)


@pytest.fixture(scope="module")
def capture_failure_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """제품의 버퍼 쓰기·실패 신호·마감 함수를 그대로 컴파일한다."""
    root = Path(__file__).resolve().parents[1]
    source = (root / "scripts" / "zoom_audio_activity.swift").read_text()
    entrypoint = (
        'if CommandLine.arguments.count == 5 && CommandLine.arguments[1] == "--record-split",'
    )
    assert entrypoint in source
    declarations = source.split(entrypoint)[0]
    harness = r"""
let target = CommandLine.arguments[1]
let descriptor = open(target, O_RDWR | O_CREAT | O_EXCL, S_IRUSR | S_IWUSR)
guard descriptor >= 0 else { fatalError("temporary file creation failed") }
let output = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
try output.write(contentsOf: wavHeader(rate: 16000, frames: 0))
private let state = try SplitRecording(output: output, rate: 16000, mic: 0)
let samples = UnsafeMutablePointer<Int16>.allocate(capacity: 192)
samples.initialize(repeating: 1500, count: 192)
let buffer = UnsafeMutablePointer<AudioQueueBuffer>.allocate(capacity: 1)
buffer.initialize(to: AudioQueueBuffer(mAudioDataBytesCapacity: 384,
    mAudioData: UnsafeMutableRawPointer(samples), mAudioDataByteSize: 384,
    mUserData: nil, mPacketDescriptionCapacity: 0, mPacketDescriptions: nil,
    mPacketDescriptionCount: 0))
try state.append(buffer)

// 실제 write 오류: 같은 파일의 읽기 전용 FD에 쓰기를 시도한다.
// 첫 성공 버퍼의 파일은 원래 쓰기 FD로 마감할 수 있게 그대로 보존한다.
let readOnly = open(target, O_RDONLY)
private let failing = try SplitRecording(output: FileHandle(fileDescriptor: readOnly, closeOnDealloc: true), rate: 16000, mic: 0)
let callbackFinished = DispatchSemaphore(value: 0)
DispatchQueue.global().async {
    usleep(100_000)
    do {
        try failing.append(buffer)
        fatalError("expected write failure")
    } catch {
        state.recordFailure(error)
    }
    callbackFinished.signal()
}
try waitForRecordingStop(failureDescriptor: state.failureDescriptor)
callbackFinished.wait()
try state.finish()
guard state.failure != nil else { fatalError("missing write error") }
samples.deinitialize(count: 192)
samples.deallocate()
buffer.deallocate()
exit(2)
"""
    directory = tmp_path_factory.mktemp("native-capture-failure")
    test_source = directory / "capture_failure.swift"
    test_source.write_text(declarations + harness)
    binary = directory / "capture_failure"
    result = subprocess.run(
        [
            "swiftc",
            "-module-cache-path",
            str(directory / "cache"),
            str(test_source),
            "-o",
            str(binary),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return binary


def test_native_write_failure_exits_without_q_and_finalizes_partial_wav(
    capture_failure_binary: Path, tmp_path: Path
) -> None:
    """stdin을 열어 둬도 쓰기 오류가 대기를 깨우며 성공한 프레임을 WAV로 보존한다."""
    partial = tmp_path / "partial.wav"
    with subprocess.Popen(
        [str(capture_failure_binary), str(partial)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as proc:
        try:
            assert proc.wait(timeout=5) == 2
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
    with wave.open(str(partial)) as audio:
        assert audio.getnchannels() == 2
        assert audio.getframerate() == 16000
        assert audio.getnframes() == 64
        assert audio.readframes(64) == b"\xdc\x05\xdc\x05" * 64
