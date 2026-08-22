"""OpenAI 전사 어댑터의 네트워크 경계와 응답 변환 계약을 검증한다.

HTTPS 연결, ffmpeg, Keychain은 모두 fake/mock으로 대체하며 실제 외부 호출은 하지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import AppConfig, PathsConfig
from core.retry_policy import NonRetryableError, should_retry
from steps import openai_transcriber
from steps.openai_transcriber import (
    OpenAITranscriber,
    OpenAITranscriptionError,
    _default_transport,
    _multipart_body,
    cleanup_stale_openai_temp_dirs,
)
from steps.transcriber import (
    AudioAdmissionError,
    AudioFileIdentity,
    inspect_audio_path_no_symlinks,
)


class FakeHTTPResponse:
    """http.client 응답의 status/read 계약만 모사한다."""

    def __init__(self, status: int, payload: bytes) -> None:
        self.status = status
        self.payload = payload
        self.read_limit: int | None = None

    def read(self, limit: int) -> bytes:
        self.read_limit = limit
        return self.payload


def _config(tmp_path: Path) -> AppConfig:
    """임시 base_dir와 비활성화된 품질 gate를 가진 설정을 만든다."""
    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    config.audio_quality.enabled = False
    return config


def _prepared_chunk(path: Path, offset: float) -> tuple[Path, float, AudioFileIdentity]:
    """mock chunk도 production과 같은 inode identity 계약으로 만든다."""
    return path, offset, inspect_audio_path_no_symlinks(path)


def test_multipart는_generic_파일명만_외부에_노출한다(tmp_path: Path) -> None:
    """원본 회의명 대신 고정 audio.mp3만 multipart filename에 사용한다."""
    audio_path = tmp_path / "고객사-비공개-전략회의.mp3"
    audio_path.write_bytes(b"mock-audio-bytes")

    body, boundary = _multipart_body(
        audio_path.read_bytes(),
        model="gpt-4o-transcribe-diarize",
    )

    assert boundary.startswith("recap-")
    assert b'filename="audio.mp3"' in body
    assert audio_path.name.encode("utf-8") not in body
    assert b'name="model"' in body
    assert b"gpt-4o-transcribe-diarize" in body
    assert b'name="response_format"' in body
    assert b"diarized_json" in body
    assert b'name="language"' not in body
    assert b"mock-audio-bytes" in body


def test_startup은_종료된_process의_OpenAI_임시음성을_정리한다(tmp_path: Path) -> None:
    """SIGKILL 뒤 남은 generic MP3는 다음 startup에서 로컬로 제거한다."""
    config = _config(tmp_path)
    stale = tmp_path / ".openai-stt-999999999-deadbeef"
    stale.mkdir(mode=0o700)
    (stale / "audio_0000.mp3").write_bytes(b"private meeting audio")
    (stale / "audio_0000.mp3").chmod(0o600)

    removed = cleanup_stale_openai_temp_dirs(config)

    assert removed == 1
    assert not stale.exists()


def test_startup은_살아있는_process나_예상밖_entry를_보존한다(tmp_path: Path) -> None:
    """동시 실행과 symlink 공격 가능성이 있는 임시 경로는 삭제하지 않는다."""
    config = _config(tmp_path)
    active = tmp_path / f".openai-stt-{os.getpid()}-active"
    active.mkdir(mode=0o700)
    (active / "audio_0000.mp3").write_bytes(b"active")
    unsafe = tmp_path / ".openai-stt-999999998-unsafe"
    unsafe.mkdir(mode=0o700)
    target = tmp_path / "outside.txt"
    target.write_text("keep", encoding="utf-8")
    (unsafe / "audio_0000.mp3").symlink_to(target)

    removed = cleanup_stale_openai_temp_dirs(config)

    assert removed == 0
    assert active.exists()
    assert unsafe.exists()
    assert target.read_text(encoding="utf-8") == "keep"


def test_default_transport는_고정_HTTPS_목적지와_generic_파일명을_사용한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """transport가 실제 네트워크 없이 고정 OpenAI endpoint만 호출하는지 검증한다."""
    audio_path = tmp_path / "private-original-name.mp3"
    audio_path.write_bytes(b"audio")
    response = FakeHTTPResponse(200, json.dumps({"segments": []}).encode("utf-8"))
    connection = MagicMock()
    connection.getresponse.return_value = response
    constructor = MagicMock(return_value=connection)
    monkeypatch.setattr(openai_transcriber.http.client, "HTTPSConnection", constructor)

    result = _default_transport(
        audio_path.read_bytes(),
        "sk-mock-secret-value-1234567890",
        "gpt-4o-transcribe-diarize",
        123,
    )

    assert result == {"segments": []}
    constructor.assert_called_once_with("api.openai.com", 443, timeout=123)
    request_args, request_kwargs = connection.request.call_args
    assert request_args[:2] == ("POST", "/v1/audio/transcriptions")
    assert b'filename="audio.mp3"' in request_kwargs["body"]
    assert b"private-original-name.mp3" not in request_kwargs["body"]
    assert request_kwargs["headers"]["Content-Length"] == str(len(request_kwargs["body"]))
    connection.close.assert_called_once_with()


def test_응답_파서는_NFC_정규화와_chunk_offset을_적용한다() -> None:
    """청크별 상대 시각을 전체 오디오 절대 시각으로 변환한다."""
    payload = {
        "segments": [
            {"text": "  cafe\u0301 회의  ", "start": 0.125, "end": 1.5},
            {"text": "   ", "start": 1.5, "end": 2.0},
        ]
    }

    segments = OpenAITranscriber._parse_response(payload, offset=20.0)

    assert len(segments) == 1
    assert segments[0].text == "café 회의"
    assert segments[0].start == 20.125
    assert segments[0].end == 21.5


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"segments": "not-a-list"},
        {"segments": ["not-an-object"]},
        {"segments": [{"text": "회의", "start": 2.0, "end": 1.0}]},
        {"segments": [{"text": "회의", "start": float("inf"), "end": 3.0}]},
    ],
)
def test_잘못된_응답은_안전한_전사_오류로_거부한다(payload: dict[str, Any]) -> None:
    """잘못된 JSON schema나 비정상 시간값을 결과로 저장하지 않는다."""
    with pytest.raises(OpenAITranscriptionError, match="OpenAI 전사"):
        OpenAITranscriber._parse_response(payload, offset=0.0)


@pytest.mark.asyncio
async def test_여러_chunk_결과의_offset과_provenance를_보존한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """주입된 mock transport 결과를 전체 시간축의 TranscriptResult로 결합한다."""
    config = _config(tmp_path)
    source = tmp_path / "input.wav"
    source.write_bytes(b"mock-source")
    first_chunk = tmp_path / "first.mp3"
    second_chunk = tmp_path / "second.mp3"
    first_chunk.write_bytes(b"first")
    second_chunk.write_bytes(b"second")
    calls: list[tuple[bytes, str, str, int]] = []

    def transport(
        audio_bytes: bytes,
        api_key: str,
        model: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        calls.append((audio_bytes, api_key, model, timeout_seconds))
        return {"segments": [{"text": audio_bytes.decode("ascii"), "start": 0.25, "end": 1.0}]}

    transcriber = OpenAITranscriber(
        config,
        transport=transport,
        credential_resolver=lambda: "sk-injected-mock-secret-123456",
    )

    @contextmanager
    def prepared_chunks(
        *_args: Any,
    ) -> Iterator[list[tuple[Path, float, AudioFileIdentity]]]:
        yield [_prepared_chunk(first_chunk, 0.0), _prepared_chunk(second_chunk, 8.5)]

    monkeypatch.setattr(transcriber, "_prepared_chunks", prepared_chunks)
    quality_check = AsyncMock(return_value=None)
    identity_check = MagicMock()
    monkeypatch.setattr(transcriber, "_require_audio_quality", quality_check)
    monkeypatch.setattr(transcriber, "_assert_identity", identity_check)

    result = await transcriber.transcribe(source)

    assert [segment.start for segment in result.segments] == [0.25, 8.75]
    assert [segment.end for segment in result.segments] == [1.0, 9.5]
    assert result.full_text == "first second"
    assert result.language == "auto"
    assert result.provider == "openai"
    assert result.model == "gpt-4o-transcribe-diarize"
    assert len(calls) == 2
    assert all(call[1] == "sk-injected-mock-secret-123456" for call in calls)
    assert all(call[3] == config.stt.openai_timeout_seconds for call in calls)


@pytest.mark.asyncio
async def test_준비후_교체된_chunk는_외부전송하지_않는다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prep identity와 다른 inode/bytes가 path에 놓이면 transport 전에 거부한다."""
    config = _config(tmp_path)
    source = tmp_path / "input.wav"
    source.write_bytes(b"mock-source")
    chunk = tmp_path / "audio_0000.mp3"
    chunk.write_bytes(b"approved-chunk")
    transport = MagicMock()
    transcriber = OpenAITranscriber(
        config,
        transport=transport,
        credential_resolver=lambda: "sk-injected-mock-secret-123456",
    )

    @contextmanager
    def swapped_chunks(
        *_args: Any,
    ) -> Iterator[list[tuple[Path, float, AudioFileIdentity]]]:
        prepared = _prepared_chunk(chunk, 0.0)
        chunk.unlink()
        chunk.write_bytes(b"replacement-chunk")
        yield [prepared]

    monkeypatch.setattr(transcriber, "_prepared_chunks", swapped_chunks)
    monkeypatch.setattr(transcriber, "_require_audio_quality", AsyncMock(return_value=None))
    monkeypatch.setattr(transcriber, "_assert_identity", MagicMock())

    with pytest.raises(OpenAITranscriptionError, match="준비 후 변경"):
        await transcriber.transcribe(source)

    transport.assert_not_called()


@pytest.mark.asyncio
async def test_후반_chunk_실패후_재시도는_성공한_chunk를_재전송하지_않는다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """성공 응답의 SHA 기반 캐시가 뒤 청크 실패 시 중복 업로드를 막는다."""
    config = _config(tmp_path)
    source = tmp_path / "input.wav"
    source.write_bytes(b"mock-source")
    first_chunk = tmp_path / "first.mp3"
    second_chunk = tmp_path / "second.mp3"
    first_chunk.write_bytes(b"first")
    second_chunk.write_bytes(b"second")
    resume_dir = tmp_path / "checkpoints" / "meeting" / ".openai-transcribe-parts"
    calls: list[str] = []
    second_attempt = False

    def transport(
        audio_bytes: bytes,
        _api_key: str,
        _model: str,
        _timeout_seconds: int,
    ) -> dict[str, Any]:
        nonlocal second_attempt
        label = audio_bytes.decode("ascii")
        calls.append(label)
        if label == "second" and not second_attempt:
            second_attempt = True
            raise OpenAITranscriptionError("두 번째 청크 실패")
        return {"segments": [{"text": label, "start": 0.0, "end": 1.0}]}

    transcriber = OpenAITranscriber(
        config,
        transport=transport,
        credential_resolver=lambda: "sk-injected-mock-secret-123456",
    )

    @contextmanager
    def prepared_chunks(
        *_args: Any,
    ) -> Iterator[list[tuple[Path, float, AudioFileIdentity]]]:
        yield [_prepared_chunk(first_chunk, 0.0), _prepared_chunk(second_chunk, 8.0)]

    monkeypatch.setattr(transcriber, "_prepared_chunks", prepared_chunks)
    monkeypatch.setattr(transcriber, "_require_audio_quality", AsyncMock(return_value=None))
    monkeypatch.setattr(transcriber, "_assert_identity", MagicMock())

    with pytest.raises(OpenAITranscriptionError, match="두 번째 청크 실패"):
        await transcriber.transcribe(source, resume_dir=resume_dir)

    result = await transcriber.transcribe(source, resume_dir=resume_dir)

    assert calls == ["first", "second", "second"]
    assert result.full_text == "first second"
    assert len(list(resume_dir.glob("chunk_*.json"))) == 2

    transcriber.cleanup_resume_cache(resume_dir)
    assert list(resume_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_요청중_취소는_응답을_캐시하고_다음_chunk_전송을_막는다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """마지막 응답 직후에도 취소를 확인하며 다음 외부 요청은 시작하지 않는다."""
    config = _config(tmp_path)
    source = tmp_path / "input.wav"
    source.write_bytes(b"mock-source")
    first_chunk = tmp_path / "first.mp3"
    second_chunk = tmp_path / "second.mp3"
    first_chunk.write_bytes(b"first")
    second_chunk.write_bytes(b"second")
    resume_dir = tmp_path / "checkpoints" / "meeting" / ".openai-transcribe-parts"
    calls: list[str] = []
    cancelled = False

    def transport(
        audio_bytes: bytes,
        _api_key: str,
        _model: str,
        _timeout_seconds: int,
    ) -> dict[str, Any]:
        nonlocal cancelled
        label = audio_bytes.decode("ascii")
        calls.append(label)
        cancelled = True
        return {"segments": [{"text": label, "start": 0.0, "end": 1.0}]}

    transcriber = OpenAITranscriber(
        config,
        transport=transport,
        credential_resolver=lambda: "sk-injected-mock-secret-123456",
    )

    @contextmanager
    def prepared_chunks(
        *_args: Any,
    ) -> Iterator[list[tuple[Path, float, AudioFileIdentity]]]:
        yield [_prepared_chunk(first_chunk, 0.0), _prepared_chunk(second_chunk, 8.0)]

    monkeypatch.setattr(transcriber, "_prepared_chunks", prepared_chunks)
    monkeypatch.setattr(transcriber, "_require_audio_quality", AsyncMock(return_value=None))
    monkeypatch.setattr(transcriber, "_assert_identity", MagicMock())

    with pytest.raises(asyncio.CancelledError):
        await transcriber.transcribe(
            source,
            resume_dir=resume_dir,
            should_cancel=lambda: cancelled,
        )

    assert calls == ["first"]
    assert len(list(resume_dir.glob("chunk_*.json"))) == 1


@pytest.mark.asyncio
async def test_ffmpeg_준비중_취소도_첫_upload_전에_반영한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """blocking 준비 작업을 thread로 격리해 API 취소 처리가 선행될 수 있다."""
    config = _config(tmp_path)
    source = tmp_path / "input.wav"
    source.write_bytes(b"mock-source")
    chunk = tmp_path / "first.mp3"
    chunk.write_bytes(b"first")
    prep_started = threading.Event()
    prep_release = threading.Event()
    cancelled = False
    transport = MagicMock()
    transcriber = OpenAITranscriber(
        config,
        transport=transport,
        credential_resolver=lambda: "sk-injected-mock-secret-123456",
    )

    @contextmanager
    def blocking_prepared_chunks(
        *_args: Any,
    ) -> Iterator[list[tuple[Path, float, AudioFileIdentity]]]:
        prep_started.set()
        if not prep_release.wait(timeout=2.0):
            raise AssertionError("테스트 준비 barrier timeout")
        yield [_prepared_chunk(chunk, 0.0)]

    monkeypatch.setattr(transcriber, "_prepared_chunks", blocking_prepared_chunks)
    monkeypatch.setattr(transcriber, "_require_audio_quality", AsyncMock(return_value=None))
    monkeypatch.setattr(transcriber, "_assert_identity", MagicMock())

    task = asyncio.create_task(transcriber.transcribe(source, should_cancel=lambda: cancelled))
    assert await asyncio.to_thread(prep_started.wait, 1.0)
    cancelled = True
    prep_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    transport.assert_not_called()


@pytest.mark.asyncio
async def test_task_cancel중_완료된_HTTPS_응답도_캐시해_재업로드를_막는다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shutdown 취소가 to_thread 결과를 버려 같은 청크를 이중 과금하지 않는다."""
    config = _config(tmp_path)
    source = tmp_path / "input.wav"
    source.write_bytes(b"mock-source")
    chunk = tmp_path / "first.mp3"
    chunk.write_bytes(b"first")
    resume_dir = tmp_path / "checkpoints" / "meeting" / ".openai-transcribe-parts"
    request_started = threading.Event()
    request_release = threading.Event()
    calls = 0

    def transport(
        _audio_bytes: bytes,
        _api_key: str,
        _model: str,
        _timeout_seconds: int,
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        request_started.set()
        if not request_release.wait(timeout=2.0):
            raise AssertionError("테스트 transport barrier timeout")
        return {"segments": [{"text": "cached", "start": 0.0, "end": 1.0}]}

    transcriber = OpenAITranscriber(
        config,
        transport=transport,
        credential_resolver=lambda: "sk-injected-mock-secret-123456",
    )

    @contextmanager
    def prepared_chunks(
        *_args: Any,
    ) -> Iterator[list[tuple[Path, float, AudioFileIdentity]]]:
        yield [_prepared_chunk(chunk, 0.0)]

    monkeypatch.setattr(transcriber, "_prepared_chunks", prepared_chunks)
    monkeypatch.setattr(transcriber, "_require_audio_quality", AsyncMock(return_value=None))
    monkeypatch.setattr(transcriber, "_assert_identity", MagicMock())

    first_run = asyncio.create_task(transcriber.transcribe(source, resume_dir=resume_dir))
    assert await asyncio.to_thread(request_started.wait, 1.0)
    first_run.cancel()
    request_release.set()
    with pytest.raises(asyncio.CancelledError):
        await first_run

    resumed = await transcriber.transcribe(source, resume_dir=resume_dir)

    assert resumed.full_text == "cached"
    assert calls == 1


def test_업로드_chunk_크기_초과는_transport_전에_거부한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mock ffmpeg가 만든 청크가 제한보다 크면 context 진입 전에 실패한다."""
    config = _config(tmp_path)
    config.stt.openai_max_upload_bytes = 4
    source = tmp_path / "input.wav"
    source.write_bytes(b"mock-source")
    identity = inspect_audio_path_no_symlinks(source)
    transcriber = OpenAITranscriber(
        config,
        transport=MagicMock(),
        credential_resolver=lambda: "sk-injected-mock-secret-123456",
    )
    monkeypatch.setattr(openai_transcriber.shutil, "which", lambda _name: "/mock/ffmpeg")

    def fake_run(command: list[str], **_kwargs: Any) -> None:
        output = Path(command[-1].replace("%04d", "0000"))
        output.write_bytes(b"12345")

    monkeypatch.setattr(openai_transcriber.subprocess, "run", fake_run)

    with pytest.raises(OpenAITranscriptionError, match="크기가 허용 범위를 벗어났습니다"):
        with transcriber._prepared_chunks(source, identity):
            pytest.fail("크기 초과 청크를 외부 전송 대상으로 반환하면 안 됩니다")


def test_업로드_변환은_원본_metadata를_제거하고_기본_한시간_청크를_사용한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """파일명뿐 아니라 ID3/chapter metadata도 외부 전송본에서 제거한다."""
    config = _config(tmp_path)
    source = tmp_path / "private-source.wav"
    source.write_bytes(b"mock-source")
    identity = inspect_audio_path_no_symlinks(source)
    commands: list[list[str]] = []
    transcriber = OpenAITranscriber(
        config,
        transport=MagicMock(),
        credential_resolver=lambda: "sk-injected-mock-secret-123456",
    )
    monkeypatch.setattr(openai_transcriber.shutil, "which", lambda _name: "/mock/ffmpeg")
    monkeypatch.setattr(openai_transcriber, "measure_audio_duration", lambda _path: 1.0)

    def fake_run(command: list[str], **_kwargs: Any) -> None:
        commands.append(command)
        output = Path(command[-1].replace("%04d", "0000"))
        output.write_bytes(b"encoded-audio")

    monkeypatch.setattr(openai_transcriber.subprocess, "run", fake_run)

    with transcriber._prepared_chunks(source, identity) as chunks:
        assert len(chunks) == 1

    command = commands[0]
    assert command[command.index("-map_metadata") + 1] == "-1"
    assert command[command.index("-map_chapters") + 1] == "-1"
    assert command[command.index("-segment_time") + 1] == "3600"


def test_응답_크기_제한을_초과하면_JSON을_파싱하지_않는다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """상한보다 큰 응답은 작은 mock payload로 재현해 즉시 거부한다."""
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"audio")
    monkeypatch.setattr(openai_transcriber, "_MAX_RESPONSE_BYTES", 8)
    response = FakeHTTPResponse(200, b"123456789")
    connection = MagicMock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(
        openai_transcriber.http.client,
        "HTTPSConnection",
        MagicMock(return_value=connection),
    )

    with pytest.raises(OpenAITranscriptionError, match="응답이 허용 크기를 초과"):
        _default_transport(
            audio_path.read_bytes(),
            "sk-injected-mock-secret-123456",
            "gpt-4o-transcribe-diarize",
            60,
        )


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_HTTP_오류는_upstream_body와_API_키를_노출하지_않는다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """외부 응답 본문의 민감 문자열은 사용자용 오류에 포함하지 않는다."""
    secret = "sk-do-not-reflect-this-secret-123456"
    upstream_marker = "upstream-private-debug-body"
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"audio")
    response = FakeHTTPResponse(status, upstream_marker.encode("utf-8"))
    connection = MagicMock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(
        openai_transcriber.http.client,
        "HTTPSConnection",
        MagicMock(return_value=connection),
    )

    with pytest.raises(OpenAITranscriptionError) as captured:
        _default_transport(
            audio_path.read_bytes(),
            secret,
            "gpt-4o-transcribe-diarize",
            60,
        )

    message = str(captured.value)
    assert secret not in message
    assert upstream_marker not in message
    assert isinstance(captured.value, NonRetryableError)
    assert should_retry(captured.value, attempt=1, max_attempts=3) is False


def test_transport_예외의_민감_메시지를_외부_오류에_반사하지_않는다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """socket/TLS 예외는 원문 대신 고정된 안전 메시지로 감싼다."""
    secret = "sk-do-not-reflect-this-secret-123456"
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"audio")
    connection = MagicMock()
    connection.request.side_effect = OSError(f"socket failed with {secret}")
    monkeypatch.setattr(
        openai_transcriber.http.client,
        "HTTPSConnection",
        MagicMock(return_value=connection),
    )

    with pytest.raises(OpenAITranscriptionError) as captured:
        _default_transport(
            audio_path.read_bytes(),
            secret,
            "gpt-4o-transcribe-diarize",
            60,
        )

    assert secret not in str(captured.value)
    assert "자동 재시도하지 않습니다" in str(captured.value)
    assert captured.value.__cause__ is None
    connection.close.assert_called_once_with()


def test_transport는_제어문자가_있는_키를_HTTP_호출_전에_거부한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """수동 Keychain 변조가 있어도 header 예외에 키 원문이 노출되지 않는다."""
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"audio")
    unsafe = "sk-secret-value\nInjected: marker-123456"
    constructor = MagicMock()
    monkeypatch.setattr(openai_transcriber.http.client, "HTTPSConnection", constructor)

    with pytest.raises(OpenAITranscriptionError) as captured:
        _default_transport(
            audio_path.read_bytes(),
            unsafe,
            "gpt-4o-transcribe-diarize",
            60,
        )

    assert unsafe not in str(captured.value)
    constructor.assert_not_called()


def test_transport는_non_ascii_키를_HTTP_호출_전에_거부한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authorization header 인코딩 실패가 traceback/로그로 번지지 않는다."""
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"audio")
    unsafe = "가" * 20
    constructor = MagicMock()
    monkeypatch.setattr(openai_transcriber.http.client, "HTTPSConnection", constructor)

    with pytest.raises(OpenAITranscriptionError) as captured:
        _default_transport(
            audio_path.read_bytes(),
            unsafe,
            "gpt-4o-transcribe-diarize",
            60,
        )

    assert unsafe not in str(captured.value)
    constructor.assert_not_called()


@pytest.mark.asyncio
async def test_자격증명_누락은_파일_검사_전에_거부한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API 키가 없으면 원본 파일이나 외부 transport를 건드리지 않는다."""
    config = _config(tmp_path)
    source_inspector = MagicMock()
    transport = MagicMock()
    monkeypatch.setattr(openai_transcriber, "inspect_audio_path_no_symlinks", source_inspector)
    transcriber = OpenAITranscriber(
        config,
        transport=transport,
        credential_resolver=lambda: None,
    )

    with pytest.raises(OpenAITranscriptionError, match="API 키가 등록되어 있지 않습니다"):
        await transcriber.transcribe(tmp_path / "does-not-need-to-exist.wav")

    source_inspector.assert_not_called()
    transport.assert_not_called()


@pytest.mark.asyncio
async def test_동의후_교체된_inode는_chunk_준비와_transport_전에_거부한다(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """route admission identity를 adapter까지 전달해 재검사 gap을 닫는다."""
    config = _config(tmp_path)
    source = tmp_path / "input.wav"
    source.write_bytes(b"approved audio")
    approved_identity = inspect_audio_path_no_symlinks(source)
    source.unlink()
    source.write_bytes(b"replacement audio")
    transport = MagicMock()
    prepare = MagicMock()
    transcriber = OpenAITranscriber(
        config,
        transport=transport,
        credential_resolver=lambda: "sk-injected-mock-secret-123456",
    )
    monkeypatch.setattr(transcriber, "_prepared_chunks", prepare)

    with pytest.raises(AudioAdmissionError, match="동의 후 오디오 파일이 변경"):
        await transcriber.transcribe(
            source,
            expected_audio_identity=approved_identity,
        )

    prepare.assert_not_called()
    transport.assert_not_called()
