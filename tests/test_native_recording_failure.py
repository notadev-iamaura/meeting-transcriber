"""CoreAudio 장치를 열지 않고 실제 Swift 오류 신호와 부분 WAV 마감을 검증한다."""

from __future__ import annotations

import json
import select
import shutil
import subprocess
import sys
import time
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
if CommandLine.arguments[1] == "--route-test" {
    let speaker = ListeningDevice(id: 4, headphones: false, builtIn: true)
    let usb = ListeningDevice(id: 9, headphones: true, builtIn: false)
    precondition(listeningDevice([speaker], preferred: 88) == speaker.id)
    precondition(listeningDevice([speaker, usb], preferred: speaker.id) == usb.id)
    precondition(listeningDevice([speaker], preferred: usb.id) == speaker.id)
    precondition(isZoomProcess(bundleID: "us.zoom.xos", path: ""))
    precondition(!isZoomProcess(bundleID: "org.example.notzoom", path: "/tmp/zoom-test"))
    // 실제 채널 복사 함수가 마이크와 좌우 상대 신호를 순서대로 전달하는지 확인한다.
    let layout = AVAudioChannelLayout(layoutTag: kAudioChannelLayoutTag_DiscreteInOrder | 3)!
    let format = AVAudioFormat(standardFormatWithSampleRate: 48000, channelLayout: layout)
    var samples: [Float] = [1.0, 0.25, -0.25, 0.5, 0.125, -0.125]
    try samples.withUnsafeMutableBytes { bytes in
        var input = AudioBufferList(mNumberBuffers: 1, mBuffers: AudioBuffer(
            mNumberChannels: 3, mDataByteSize: UInt32(bytes.count), mData: bytes.baseAddress))
        let frame = try copyTapFrame(&input, format: format)!
        precondition(frame.frameLength == 2)
        let values = frame.floatChannelData!
        precondition(values[0][0] == 1 && values[1][0] == 0.25 && values[2][1] == -0.125)
    }
    exit(0)
}
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
        state.recordFailure(error, stage: .wavWrite)
    }
    // 마감 자체도 실패하는 경우 먼저 기록한 CoreAudio 단계가 남아야 한다.
    failing.recordStatus(-50, stage: .inputLayout)
    do {
        try failing.finish()
        fatalError("expected finalize failure")
    } catch let failure as CaptureFailure {
        precondition(failure.stage == .inputLayout && failure.errorCode == -50)
    } catch {
        fatalError("expected the original CoreAudio failure")
    }
    callbackFinished.signal()
}
try waitForRecordingStop(state)
callbackFinished.wait()
if CommandLine.arguments.contains("--hold-finalize") { _ = readLine() }
do {
    try state.finish()
    fatalError("missing write error")
} catch {
    reportCaptureFailure(error)
}
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


def test_native_usb_route_and_tap_channels(capture_failure_binary: Path) -> None:
    """가상 출력이 기본일 때도 이어폰의 연결·분리와 세 입력의 순서를 확인한다."""
    subprocess.run([str(capture_failure_binary), "--route-test"], check=True, timeout=5)


@pytest.mark.parametrize(
    "hold_finalize", [False, True], ids=["automatic-exit", "delayed-finalize"]
)
def test_native_write_failure_exits_without_q_and_finalizes_partial_wav(
    capture_failure_binary: Path, tmp_path: Path, hold_finalize: bool
) -> None:
    """stdin을 열어 둬도 쓰기 오류가 대기를 깨우며 성공한 프레임을 WAV로 보존한다."""
    partial = tmp_path / "partial.wav"
    started_ms = time.time_ns() // 1_000_000
    command = [str(capture_failure_binary), str(partial)]
    if hold_finalize:
        command.append("--hold-finalize")
    early_diagnostic = b""
    with subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as proc:
        try:
            if hold_finalize:
                assert proc.stdin is not None and proc.stderr is not None
                readable, _, _ = select.select([proc.stderr], [], [], 5)
                assert readable, "부분 WAV를 마감하기 전에 최초 진단을 받아야 한다."
                early_diagnostic = proc.stderr.readline()
                assert early_diagnostic.startswith(b"capture-failure=")
                assert proc.poll() is None
                proc.stdin.write(b"\n")
                proc.stdin.flush()
            assert proc.wait(timeout=5) == 2
            stdout, stderr = proc.communicate(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
    prefix = b"capture-failure="
    stderr = early_diagnostic + stderr
    assert stdout == b"capture-error=coreaudio\n"
    assert stderr.startswith(prefix) and len(stderr.splitlines()) == 1
    diagnostic = json.loads(stderr[len(prefix) :])
    assert diagnostic["stage"] == "wav_write"
    assert diagnostic["error_domain"] in {"NSCocoaErrorDomain", "NSPOSIXErrorDomain"}
    assert isinstance(diagnostic["error_code"], int) and diagnostic["error_code"] != 0
    assert started_ms <= diagnostic["last_input_frame_ms"] <= time.time_ns() // 1_000_000
    assert str(partial).encode() not in stderr
    with wave.open(str(partial)) as audio:
        assert audio.getnchannels() == 2
        assert audio.getframerate() == 16000
        assert audio.getnframes() == 64
        assert audio.readframes(64) == b"\xdc\x05\xdc\x05" * 64
