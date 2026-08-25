"""
폴더 감시기 테스트 모듈 (Folder Watcher Test Module)

목적: FolderWatcher의 파일 감지, 큐 등록, debounce, 콜백 호출,
     에러 처리 등 전체 기능을 검증한다.
주요 테스트:
    - 오디오 파일 확장자 필터링
    - debounce (파일 크기 안정화 대기)
    - 작업 큐 자동 등록
    - 중복 등록 방지
    - 콜백 호출 (동기/비동기)
    - start/stop 생명주기
    - 기존 파일 스캔
    - 에러 처리
의존성: pytest, pytest-asyncio, core/watcher.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from core.job_queue import AsyncJobQueue, Job, JobQueue, JobQueueError
from core.watcher import (
    AlreadyWatchingError,
    FolderWatcher,
    WatchDirectoryError,
    WatcherError,
    _AudioFileHandler,
    _FileFingerprint,
    _OpenWriterState,
    _PreparedReadyAction,
)

_REAL_WRITER_PROBE = FolderWatcher._probe_writable_open


@pytest.fixture(autouse=True)
def _deterministic_writer_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """일반 watcher 단위 테스트에서 시스템 lsof 실행시간 변동을 제거한다."""
    monkeypatch.setattr(
        FolderWatcher,
        "_probe_writable_open",
        AsyncMock(return_value=_OpenWriterState.CLEAR),
    )


# === 테스트 픽스처 ===


def _make_config(tmp_path: Path) -> MagicMock:
    """테스트용 설정 목 객체를 생성한다.

    Args:
        tmp_path: pytest tmp_path 픽스처

    Returns:
        MagicMock 설정 객체
    """
    config = MagicMock()
    # paths 설정
    watch_dir = tmp_path / "audio_input"
    watch_dir.mkdir(exist_ok=True)
    config.paths.resolved_base_dir = tmp_path
    config.paths.resolved_audio_input_dir = watch_dir
    config.paths.resolved_audio_quarantine_dir = tmp_path / "audio_quarantine"
    config.paths.resolved_checkpoints_dir = tmp_path / "checkpoints"
    config.paths.resolved_outputs_dir = tmp_path / "outputs"

    # audio 설정
    config.audio.supported_input_formats = ["wav", "mp3", "m4a", "flac", "ogg", "webm"]

    # watcher 설정
    config.watcher.debounce_seconds = 0.3  # 테스트용 짧은 대기 시간
    config.watcher.check_interval_seconds = 0.1  # 테스트용 짧은 확인 간격
    config.watcher.file_ready_timeout_seconds = 1.0
    config.watcher.startup_probe_concurrency = 8
    config.watcher.excluded_subdirs = ["audio_quarantine"]

    # 일반 watcher 단위 테스트는 품질 게이트와 독립적으로 큐 동작을 검증한다.
    config.audio_quality.enabled = False

    return config


def _quality_result(
    status: object,
    *,
    failure_kind: str,
    quarantine_safe: bool,
    reason: str,
) -> SimpleNamespace:
    """신규 admission 분류 계약을 표현하는 watcher 테스트 double을 만든다."""
    # production enum이 아직 RED 단계에 없더라도 테스트 모듈 자체는 수집 가능하게
    # 유지한다. enum이 추가된 뒤에는 실제 enum 값으로 watcher 분기까지 검증한다.
    from core import audio_quality

    failure_kind_type = getattr(audio_quality, "AudioFailureKind", None)
    typed_failure_kind = (
        failure_kind_type(failure_kind) if failure_kind_type is not None else failure_kind
    )
    return SimpleNamespace(
        status=status,
        failure_kind=typed_failure_kind,
        quarantine_safe=quarantine_safe,
        mean_volume_db=None,
        duration_seconds=None,
        reason=reason,
    )


@pytest_asyncio.fixture
async def job_queue(tmp_path: Path) -> AsyncJobQueue:
    """테스트용 AsyncJobQueue를 생성한다."""
    db_path = tmp_path / "test_jobs.db"
    sync_queue = JobQueue(db_path, max_retries=3)
    async_queue = AsyncJobQueue(sync_queue)
    await async_queue.initialize()
    yield async_queue
    await async_queue.close()


@pytest_asyncio.fixture
async def watcher(tmp_path: Path, job_queue: AsyncJobQueue) -> FolderWatcher:
    """테스트용 FolderWatcher 인스턴스를 생성한다."""
    config = _make_config(tmp_path)
    w = FolderWatcher(async_job_queue=job_queue, config=config)
    yield w
    # 테스트 후 정리
    await w.stop()


# === 초기화 테스트 ===


class TestInit:
    """FolderWatcher 초기화 테스트."""

    @pytest.mark.asyncio
    async def test_기본_속성_초기화(self, watcher: FolderWatcher) -> None:
        """기본 속성이 올바르게 초기화되는지 확인한다."""
        assert watcher.is_watching is False
        assert watcher.watch_dir.exists()

    @pytest.mark.asyncio
    async def test_지원_확장자_설정(self, watcher: FolderWatcher) -> None:
        """config에서 읽은 확장자가 올바르게 설정되는지 확인한다."""
        expected = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm"}
        assert watcher._supported_extensions == expected

    @pytest.mark.asyncio
    async def test_설정_기본값_사용(self, tmp_path: Path, job_queue: AsyncJobQueue) -> None:
        """config 기본값(싱글턴)이 정상 사용되는지 확인한다."""
        config = _make_config(tmp_path)
        w = FolderWatcher(async_job_queue=job_queue, config=config)
        assert w._debounce_seconds == 0.3
        assert w._check_interval == 0.1


# === 확장자 필터링 테스트 ===


class TestAudioFileHandler:
    """_AudioFileHandler 확장자 필터링 테스트."""

    def test_오디오_파일_인식(self) -> None:
        """오디오 확장자를 올바르게 인식하는지 확인한다."""
        handler = _AudioFileHandler(
            supported_extensions={".wav", ".mp3", ".m4a"},
            on_new_file=AsyncMock(),
            loop=MagicMock(),
        )
        assert handler._is_audio_file(Path("test.wav")) is True
        assert handler._is_audio_file(Path("test.mp3")) is True
        assert handler._is_audio_file(Path("test.m4a")) is True

    def test_비오디오_파일_거부(self) -> None:
        """비오디오 확장자를 올바르게 거부하는지 확인한다."""
        handler = _AudioFileHandler(
            supported_extensions={".wav", ".mp3", ".m4a"},
            on_new_file=AsyncMock(),
            loop=MagicMock(),
        )
        assert handler._is_audio_file(Path("test.txt")) is False
        assert handler._is_audio_file(Path("test.py")) is False
        assert handler._is_audio_file(Path("test.json")) is False

    def test_대소문자_무시(self) -> None:
        """확장자 대소문자를 무시하는지 확인한다."""
        handler = _AudioFileHandler(
            supported_extensions={".wav", ".mp3"},
            on_new_file=AsyncMock(),
            loop=MagicMock(),
        )
        assert handler._is_audio_file(Path("test.WAV")) is True
        assert handler._is_audio_file(Path("test.Mp3")) is True

    def test_한국어_파일명_처리(self) -> None:
        """한국어 파일명을 올바르게 처리하는지 확인한다."""
        handler = _AudioFileHandler(
            supported_extensions={".wav", ".m4a"},
            on_new_file=AsyncMock(),
            loop=MagicMock(),
        )
        assert handler._is_audio_file(Path("2024년_회의록.wav")) is True
        assert handler._is_audio_file(Path("팀미팅_03월.m4a")) is True

    def test_디렉토리_이벤트_무시(self) -> None:
        """디렉토리 생성 이벤트를 무시하는지 확인한다."""
        mock_callback = AsyncMock()
        mock_loop = MagicMock()
        handler = _AudioFileHandler(
            supported_extensions={".wav"},
            on_new_file=mock_callback,
            loop=mock_loop,
        )

        # 디렉토리 이벤트
        event = MagicMock()
        event.is_directory = True
        handler.on_created(event)

        # 콜백이 호출되지 않아야 함
        mock_loop.call_soon_threadsafe.assert_not_called()

    def test_비오디오_파일_이벤트_무시(self) -> None:
        """비오디오 파일 이벤트를 무시하는지 확인한다."""
        mock_callback = AsyncMock()
        mock_loop = MagicMock()
        handler = _AudioFileHandler(
            supported_extensions={".wav"},
            on_new_file=mock_callback,
            loop=mock_loop,
        )

        event = MagicMock()
        event.is_directory = False
        event.src_path = "/tmp/test.txt"
        handler.on_created(event)

        mock_loop.call_soon_threadsafe.assert_not_called()

    def test_오디오_파일_이벤트_처리(self) -> None:
        """오디오 파일 이벤트가 올바르게 처리되는지 확인한다."""
        mock_callback = AsyncMock()
        mock_loop = MagicMock()
        handler = _AudioFileHandler(
            supported_extensions={".wav"},
            on_new_file=mock_callback,
            loop=mock_loop,
        )

        event = MagicMock()
        event.is_directory = False
        event.src_path = "/tmp/meeting.wav"

        scheduled: list[tuple[object, object]] = []

        def _fake_schedule(coro: object, loop: object) -> MagicMock:
            """생성된 coroutine을 닫아 unawaited 경고 없이 스케줄링만 검증한다."""
            scheduled.append((coro, loop))
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            return MagicMock()

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_fake_schedule) as mock_run:
            handler.on_created(event)

        mock_run.assert_called_once()
        assert scheduled[0][1] is mock_loop

    def test_처리중_파일의_modified_이벤트도_재검사를_예약(self) -> None:
        """writer가 이어 쓰면 modified 이벤트가 dirty 재검사를 일으켜야 한다."""
        mock_callback = AsyncMock()
        mock_loop = MagicMock()
        handler = _AudioFileHandler(
            supported_extensions={".wav"},
            on_new_file=mock_callback,
            loop=mock_loop,
        )
        event = MagicMock(is_directory=False, src_path="/tmp/meeting.wav")

        scheduled: list[tuple[object, object]] = []

        def _fake_schedule(coro: object, loop: object) -> MagicMock:
            scheduled.append((coro, loop))
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            return MagicMock()

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_fake_schedule) as mock_run:
            handler.on_modified(event)

        mock_run.assert_called_once()
        assert scheduled[0][1] is mock_loop


# === meeting_id 생성 테스트 ===


class TestMeetingIdGeneration:
    """meeting_id 생성 테스트."""

    @pytest.mark.asyncio
    async def test_파일명에서_meeting_id_생성(self, watcher: FolderWatcher) -> None:
        """파일명(확장자 제외)이 meeting_id로 사용되는지 확인한다."""
        assert watcher._generate_meeting_id(Path("/tmp/meeting_001.wav")) == "meeting_001"
        assert (
            watcher._generate_meeting_id(Path("/tmp/2024-03-04_standup.m4a"))
            == "2024-03-04_standup"
        )

    @pytest.mark.asyncio
    async def test_한국어_파일명_meeting_id(self, watcher: FolderWatcher) -> None:
        """한국어 파일명이 meeting_id로 올바르게 변환되는지 확인한다."""
        assert watcher._generate_meeting_id(Path("/tmp/3월_정기회의.wav")) == "3월_정기회의"
        assert watcher._generate_meeting_id(Path("/tmp/팀미팅.mp3")) == "팀미팅"
        assert watcher._generate_meeting_id(Path("/tmp/회의 1.wav")) == "회의 1"


# === debounce 테스트 ===


class TestDebounce:
    """파일 크기 안정화 대기 테스트."""

    @pytest.mark.asyncio
    async def test_안정된_파일_통과(self, watcher: FolderWatcher, tmp_path: Path) -> None:
        """크기가 안정된 파일이 정상 통과하는지 확인한다."""
        test_file = tmp_path / "audio_input" / "test.wav"
        test_file.write_bytes(b"fake audio data" * 100)

        result = await watcher._wait_for_stable_size(test_file)
        assert result is True

    @pytest.mark.asyncio
    async def test_사라진_파일_False(self, watcher: FolderWatcher, tmp_path: Path) -> None:
        """파일이 사라지면 False를 반환하는지 확인한다."""
        missing_file = tmp_path / "audio_input" / "missing.wav"

        result = await watcher._wait_for_stable_size(missing_file)
        assert result is False

    @pytest.mark.asyncio
    async def test_빈_파일_대기_후_안정화(self, watcher: FolderWatcher, tmp_path: Path) -> None:
        """빈 파일이 데이터 쓰기 후 안정화되는지 확인한다."""
        test_file = tmp_path / "audio_input" / "growing.wav"
        test_file.write_bytes(b"")  # 빈 파일 생성

        async def write_delayed() -> None:
            """지연 후 파일에 데이터 쓰기."""
            await asyncio.sleep(0.15)
            test_file.write_bytes(b"audio content here")

        # 병렬로 실행
        write_task = asyncio.create_task(write_delayed())
        result = await watcher._wait_for_stable_size(test_file)
        await write_task

        assert result is True

    @pytest.mark.asyncio
    async def test_영바이트_파일은_유한시간_후_보존하고_큐에_넣지_않음(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
    ) -> None:
        """0-byte/open writer 대기는 deadline을 넘어 무한 루프가 되면 안 된다."""
        test_file = tmp_path / "audio_input" / "empty-writer.wav"
        test_file.write_bytes(b"")
        watcher._debounce_seconds = 0.01
        watcher._check_interval = 0.005
        watcher._file_ready_timeout_seconds = 0.05

        await asyncio.wait_for(watcher._handle_new_file(test_file), timeout=0.3)

        assert test_file.exists()
        job = await asyncio.to_thread(
            job_queue.queue.get_job_by_meeting_id,
            "empty-writer",
        )
        assert job is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("returncode", "stdout", "stderr", "expected"),
        [
            (0, "p123\nf4\naw\n", "", _OpenWriterState.BUSY),
            (0, "p123\nf4\nau\n", "", _OpenWriterState.BUSY),
            (0, "p123\nf4\nar\n", "", _OpenWriterState.CLEAR),
            (1, "", "", _OpenWriterState.CLEAR),
            (0, "p123\nf4\n", "", _OpenWriterState.INDETERMINATE),
        ],
    )
    async def test_lsof_access_mode를_fail_closed로_분류(
        self,
        watcher: FolderWatcher,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        returncode: int,
        stdout: str,
        stderr: str,
        expected: _OpenWriterState,
    ) -> None:
        """aw/au만 busy, ar/holder 없음만 clear이고 나머지는 보류한다."""
        source = tmp_path / "audio_input" / "probe.wav"
        source.write_bytes(b"audio")
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
        run = MagicMock(return_value=completed)
        monkeypatch.setattr(Path, "is_file", lambda path: path == Path("/usr/sbin/lsof"))
        monkeypatch.setattr(os, "access", lambda path, mode: path == Path("/usr/sbin/lsof"))
        monkeypatch.setattr(subprocess, "run", run)

        result = await _REAL_WRITER_PROBE(watcher, source, timeout=0.5)

        assert result is expected
        command = run.call_args.args[0]
        assert command[-3:] == ["a", "--", str(source)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("returncode", "stdout", "stderr", "expected"),
        [
            (
                1,
                "",
                "lsof: WARNING: can't stat() hfs file system "
                "/private/var/folders/aa/bb/T/transient-volume\n"
                "      Output information may be incomplete.\n",
                _OpenWriterState.CLEAR,
            ),
            (
                0,
                "p123\nf4\nar\n",
                "lsof: WARNING: can't stat() apfs file system "
                "/private/var/folders/aa/bb/T/transient-volume\n"
                "      Output information may be incomplete.\n",
                _OpenWriterState.CLEAR,
            ),
            (
                1,
                "",
                "lsof: WARNING: can't stat() hfs file system "
                "/private/var/folders/aa/bb/T/transient-volume\n"
                "      Output information may be incomplete.\n"
                "lsof: Permission denied\n",
                _OpenWriterState.INDETERMINATE,
            ),
            (
                0,
                "p123\nf4\nar\n",
                "lsof: Operation not permitted\n",
                _OpenWriterState.INDETERMINATE,
            ),
            (
                0,
                "p123\nf4\naw\n",
                "lsof: WARNING: can't stat() hfs file system "
                "/private/var/folders/aa/bb/T/transient-volume\n",
                _OpenWriterState.BUSY,
            ),
        ],
    )
    async def test_lsof_대상과_무관한_임시_mount_warning만_허용(
        self,
        watcher: FolderWatcher,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        returncode: int,
        stdout: str,
        stderr: str,
        expected: _OpenWriterState,
    ) -> None:
        """알려진 임시 mount warning만 무시하고 나머지는 fail-closed 한다."""
        source = tmp_path / "audio_input" / "warning-probe.wav"
        source.write_bytes(b"audio")
        run = MagicMock(
            return_value=subprocess.CompletedProcess(
                args=[],
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )
        )
        monkeypatch.setattr(Path, "is_file", lambda path: path == Path("/usr/sbin/lsof"))
        monkeypatch.setattr(os, "access", lambda path, mode: path == Path("/usr/sbin/lsof"))
        monkeypatch.setattr(subprocess, "run", run)

        assert await _REAL_WRITER_PROBE(watcher, source, timeout=0.5) is expected
        assert "-w" not in run.call_args.args[0]

    @pytest.mark.asyncio
    async def test_lsof_warning_mount가_대상_상위면_INDETERMINATE(
        self,
        watcher: FolderWatcher,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """warning이 대상 mount 자체를 가리키면 unrelated로 오인하지 않는다."""
        del tmp_path
        mount = Path("/private/var/folders/aa/bb/T/target-volume")
        source = mount / "target-warning.wav"
        stderr = (
            f"lsof: WARNING: can't stat() hfs file system {mount}\n"
            "      Output information may be incomplete.\n"
        )
        monkeypatch.setattr(Path, "is_file", lambda path: path == Path("/usr/sbin/lsof"))
        monkeypatch.setattr(os, "access", lambda path, mode: path == Path("/usr/sbin/lsof"))
        monkeypatch.setattr(
            subprocess,
            "run",
            MagicMock(return_value=subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)),
        )

        assert (
            await _REAL_WRITER_PROBE(watcher, source, timeout=0.5)
            is _OpenWriterState.INDETERMINATE
        )

    @pytest.mark.asyncio
    async def test_lsof_timeout은_INDETERMINATE로_원본보존(
        self,
        watcher: FolderWatcher,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """lsof timeout을 holder 없음으로 오인하지 않는다."""
        source = tmp_path / "audio_input" / "probe-timeout.wav"
        source.write_bytes(b"audio")
        monkeypatch.setattr(Path, "is_file", lambda path: path == Path("/usr/sbin/lsof"))
        monkeypatch.setattr(os, "access", lambda path, mode: path == Path("/usr/sbin/lsof"))
        monkeypatch.setattr(
            subprocess,
            "run",
            MagicMock(side_effect=subprocess.TimeoutExpired(cmd="lsof", timeout=0.1)),
        )

        result = await _REAL_WRITER_PROBE(watcher, source, timeout=0.1)

        assert result is _OpenWriterState.INDETERMINATE

    @pytest.mark.asyncio
    async def test_고정_lsof_부재시_PATH_fallback을_실행하지_않는다(
        self,
        watcher: FolderWatcher,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """system lsof 부재는 PATH 도구를 신뢰하지 않고 보류한다."""
        source = tmp_path / "audio_input" / "probe-no-system-lsof.wav"
        source.write_bytes(b"audio")
        which = MagicMock(return_value="/attacker/path/lsof")
        run = MagicMock()
        monkeypatch.setattr(Path, "is_file", lambda path: False)
        monkeypatch.setattr(shutil, "which", which)
        monkeypatch.setattr(subprocess, "run", run)

        result = await _REAL_WRITER_PROBE(watcher, source, timeout=0.1)

        assert result is _OpenWriterState.INDETERMINATE
        which.assert_not_called()
        run.assert_not_called()

    @pytest.mark.asyncio
    async def test_쓰기_fd가_열린_파일은_품질게이트_disabled여도_SOURCE_BUSY(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """크기가 멈췄어도 writable fd가 열려 있으면 원본 보존·재시도 대상이다."""
        test_file = tmp_path / "audio_input" / "paused-writer.wav"
        test_file.write_bytes(b"initial audio bytes")
        watcher._debounce_seconds = 0.01
        watcher._check_interval = 0.005
        watcher._file_ready_timeout_seconds = 0.2
        monkeypatch.setattr(
            watcher,
            "_probe_writable_open",
            AsyncMock(return_value=_OpenWriterState.BUSY),
        )

        with test_file.open("ab") as writer:
            writer.write(b"more")
            writer.flush()
            await asyncio.wait_for(watcher._handle_new_file(test_file), timeout=1.0)
            assert test_file.exists()
            job = await asyncio.to_thread(
                job_queue.queue.get_job_by_meeting_id,
                "paused-writer",
            )
            assert job is None

    @pytest.mark.asyncio
    async def test_pending중_modified는_dirty로_남아_종료후_한번_재검사(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """첫 readiness가 busy여도 처리 중 들어온 변경을 잃지 않아야 한다."""
        test_file = tmp_path / "audio_input" / "dirty-retry.wav"
        test_file.write_bytes(b"audio")
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()
        calls = 0

        async def _fake_wait(path: Path) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                first_started.set()
                await release_first.wait()
                return False
            second_started.set()
            return True

        monkeypatch.setattr(watcher, "_wait_for_stable_size", _fake_wait)

        first_task = asyncio.create_task(watcher._handle_new_file(test_file))
        await asyncio.wait_for(first_started.wait(), timeout=0.2)
        await watcher._handle_new_file(test_file)
        release_first.set()
        await first_task

        await asyncio.wait_for(second_started.wait(), timeout=0.5)
        for _ in range(20):
            job = await asyncio.to_thread(
                job_queue.queue.get_job_by_meeting_id,
                "dirty-retry",
            )
            if job is not None:
                break
            await asyncio.sleep(0.01)

        assert calls == 2
        assert job is not None


# === 작업 큐 등록 테스트 ===


class TestJobRegistration:
    """작업 큐 자동 등록 테스트."""

    @pytest.mark.asyncio
    async def test_새_파일_큐_등록(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
    ) -> None:
        """새 오디오 파일이 큐에 등록되는지 확인한다."""
        test_file = tmp_path / "audio_input" / "new_meeting.wav"
        test_file.write_bytes(b"fake audio data")

        await watcher._handle_new_file(test_file)

        # 큐에서 확인
        job = await asyncio.to_thread(job_queue.queue.get_job_by_meeting_id, "new_meeting")
        assert job is not None
        assert job.meeting_id == "new_meeting"
        assert job.status == "recorded"

    @pytest.mark.asyncio
    async def test_중복_파일_등록_방지(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
    ) -> None:
        """이미 등록된 파일의 중복 등록을 방지하는지 확인한다."""
        test_file = tmp_path / "audio_input" / "duplicate.wav"
        test_file.write_bytes(b"fake audio data")

        # 첫 번째 등록
        await watcher._handle_new_file(test_file)

        # 두 번째 시도 — 에러 없이 스킵
        await watcher._handle_new_file(test_file)

        # 큐에 하나만 있어야 함
        all_jobs = await job_queue.get_all_jobs()
        meeting_jobs = [j for j in all_jobs if j.meeting_id == "duplicate"]
        assert len(meeting_jobs) == 1

    @pytest.mark.asyncio
    async def test_비오디오_파일_무시(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
    ) -> None:
        """비오디오 파일 확장자는 Handler 레벨에서 필터링된다.
        _handle_new_file은 이미 필터링된 후 호출되므로,
        Handler의 필터링 로직을 검증한다."""
        handler = _AudioFileHandler(
            supported_extensions={".wav", ".mp3"},
            on_new_file=AsyncMock(),
            loop=MagicMock(),
        )
        assert handler._is_audio_file(Path("test.txt")) is False

    @pytest.mark.asyncio
    async def test_한국어_파일명_등록(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
    ) -> None:
        """한국어 파일명 오디오가 정상 등록되는지 확인한다."""
        test_file = tmp_path / "audio_input" / "3월_정기회의.wav"
        test_file.write_bytes(b"fake audio data")

        await watcher._handle_new_file(test_file)

        job = await asyncio.to_thread(job_queue.queue.get_job_by_meeting_id, "3월_정기회의")
        assert job is not None
        assert job.meeting_id == "3월_정기회의"


# === 콜백 테스트 ===


class TestCallbacks:
    """파일 등록 콜백 테스트."""

    @pytest.mark.asyncio
    async def test_동기_콜백_호출(
        self,
        watcher: FolderWatcher,
        tmp_path: Path,
    ) -> None:
        """동기 콜백이 올바르게 호출되는지 확인한다."""
        called_with: list[Path] = []
        watcher.on_file_registered(lambda p: called_with.append(p))

        test_file = tmp_path / "audio_input" / "cb_test.wav"
        test_file.write_bytes(b"audio data")

        await watcher._handle_new_file(test_file)

        assert len(called_with) == 1
        assert called_with[0].name == "cb_test.wav"

    @pytest.mark.asyncio
    async def test_비동기_콜백_호출(
        self,
        watcher: FolderWatcher,
        tmp_path: Path,
    ) -> None:
        """비동기 콜백이 올바르게 호출되는지 확인한다."""
        called_with: list[Path] = []

        async def async_cb(p: Path) -> None:
            called_with.append(p)

        watcher.on_file_registered(async_cb)

        test_file = tmp_path / "audio_input" / "async_cb_test.wav"
        test_file.write_bytes(b"audio data")

        await watcher._handle_new_file(test_file)

        assert len(called_with) == 1

    @pytest.mark.asyncio
    async def test_콜백_에러_격리(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
    ) -> None:
        """콜백 에러가 파일 처리를 중단시키지 않는지 확인한다."""

        def bad_callback(p: Path) -> None:
            raise ValueError("콜백 에러")

        watcher.on_file_registered(bad_callback)

        test_file = tmp_path / "audio_input" / "error_cb.wav"
        test_file.write_bytes(b"audio data")

        # 에러 없이 처리 완료
        await watcher._handle_new_file(test_file)

        # 큐에 정상 등록
        job = await asyncio.to_thread(job_queue.queue.get_job_by_meeting_id, "error_cb")
        assert job is not None


# === 생명주기 테스트 ===


class TestLifecycle:
    """start/stop 생명주기 테스트."""

    @pytest.mark.asyncio
    async def test_시작_후_상태(self, watcher: FolderWatcher) -> None:
        """시작 후 is_watching이 True인지 확인한다."""
        await watcher.start()
        assert watcher.is_watching is True

    @pytest.mark.asyncio
    async def test_중지_후_상태(self, watcher: FolderWatcher) -> None:
        """중지 후 is_watching이 False인지 확인한다."""
        await watcher.start()
        await watcher.stop()
        assert watcher.is_watching is False

    @pytest.mark.asyncio
    async def test_이중_시작_에러(self, watcher: FolderWatcher) -> None:
        """이미 실행 중에 start() 호출 시 에러를 확인한다."""
        await watcher.start()
        with pytest.raises(AlreadyWatchingError):
            await watcher.start()

    @pytest.mark.asyncio
    async def test_이중_중지_안전(self, watcher: FolderWatcher) -> None:
        """이미 중지 상태에서 stop() 호출이 안전한지 확인한다."""
        await watcher.start()
        await watcher.stop()
        # 두 번째 stop은 에러 없이 통과
        await watcher.stop()
        assert watcher.is_watching is False

    @pytest.mark.asyncio
    async def test_미시작_상태_중지_안전(self, watcher: FolderWatcher) -> None:
        """시작하지 않은 상태에서 stop()이 안전한지 확인한다."""
        await watcher.stop()
        assert watcher.is_watching is False

    @pytest.mark.asyncio
    async def test_observer_부분_start_실패도_stop_join_정리(
        self,
        watcher: FolderWatcher,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """observer thread를 일부 만든 start 실패도 내부에서 회수한다."""

        class _PartialObserver:
            def __init__(self) -> None:
                self.started = False
                self.stop_calls = 0
                self.join_calls = 0

            def schedule(self, *args: object, **kwargs: object) -> None:
                del args, kwargs

            def start(self) -> None:
                self.started = True
                raise RuntimeError("partial observer failure")

            def stop(self) -> None:
                self.stop_calls += 1
                self.started = False

            def join(self, *, timeout: float) -> None:
                assert timeout == 5.0
                self.join_calls += 1

        observer = _PartialObserver()

        def _observer_factory(*, timeout: float) -> _PartialObserver:
            assert timeout > 0
            return observer

        monkeypatch.setattr(
            "core.watcher.PollingObserver",
            _observer_factory,
        )

        with pytest.raises(RuntimeError, match="partial observer failure"):
            await watcher.start()

        assert observer.started is False
        assert observer.stop_calls == 1
        assert observer.join_calls == 1
        assert watcher._observer is None
        assert watcher._handler is None
        assert watcher.is_watching is False

    @pytest.mark.asyncio
    async def test_감시_디렉토리_자동_생성(
        self,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
    ) -> None:
        """감시 디렉토리가 없으면 자동 생성하는지 확인한다."""
        config = _make_config(tmp_path)
        new_watch_dir = tmp_path / "new_audio_dir"
        config.paths.resolved_audio_input_dir = new_watch_dir

        w = FolderWatcher(async_job_queue=job_queue, config=config)
        await w.start()

        assert new_watch_dir.exists()
        await w.stop()


# === 기존 파일 스캔 테스트 ===


class TestScanExisting:
    """기존 파일 스캔 테스트."""

    @pytest.mark.asyncio
    async def test_기존_오디오_파일_등록(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
    ) -> None:
        """기존 오디오 파일이 큐에 등록되는지 확인한다."""
        watch_dir = tmp_path / "audio_input"

        # 파일 생성
        (watch_dir / "existing1.wav").write_bytes(b"audio1")
        (watch_dir / "existing2.mp3").write_bytes(b"audio2")
        (watch_dir / "readme.txt").write_bytes(b"text")  # 비오디오

        ids = await watcher.scan_existing()

        assert len(ids) == 2

        # 큐 확인
        job1 = await asyncio.to_thread(job_queue.queue.get_job_by_meeting_id, "existing1")
        job2 = await asyncio.to_thread(job_queue.queue.get_job_by_meeting_id, "existing2")
        assert job1 is not None
        assert job2 is not None

    @pytest.mark.asyncio
    async def test_startup_scan은_비치명_lsof_warning에도_안정파일을_한번등록(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """제보된 rc=1/HFS warning을 최초·final 검사 모두에서 안전 통과한다."""
        source = tmp_path / "audio_input" / "warning-startup.wav"
        original = b"stable audio"
        source.write_bytes(original)
        watcher._debounce_seconds = 0.0
        warning = (
            "lsof: WARNING: can't stat() hfs file system "
            "/private/var/folders/aa/bb/T/unrelated-volume\n"
            "      Output information may be incomplete.\n"
        )
        run = MagicMock(return_value=subprocess.CompletedProcess([], 1, stdout="", stderr=warning))
        monkeypatch.setattr(Path, "is_file", lambda path: path == Path("/usr/sbin/lsof"))
        monkeypatch.setattr(os, "access", lambda path, mode: path == Path("/usr/sbin/lsof"))
        monkeypatch.setattr(subprocess, "run", run)

        async def _real_probe(file_path: Path, *, timeout: float) -> _OpenWriterState:
            return await _REAL_WRITER_PROBE(watcher, file_path, timeout=timeout)

        monkeypatch.setattr(watcher, "_probe_writable_open", _real_probe)

        ids = await watcher.scan_existing()
        job = await asyncio.to_thread(
            job_queue.queue.get_job_by_meeting_id,
            "warning-startup",
        )

        assert len(ids) == 1
        assert job is not None and job.status == "recorded"
        assert run.call_count == 2
        assert source.read_bytes() == original

    @pytest.mark.asyncio
    async def test_복구스캔은_미등록_파일만_recorded로_멱등등록(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
    ) -> None:
        """비파괴 복구는 원본을 유지하고 같은 row를 한 번만 만든다."""
        source = tmp_path / "audio_input" / "recover-once.wav"
        original = b"stable recovery audio"
        source.write_bytes(original)
        watcher._debounce_seconds = 0.0

        first = await watcher.recover_unregistered_files(recent_days=7)
        second = await watcher.recover_unregistered_files(recent_days=7)
        job = await asyncio.to_thread(
            job_queue.queue.get_job_by_meeting_id,
            "recover-once",
        )

        assert first.phase == "completed"
        assert first.mode == "recovery"
        assert first.candidate_count == 1
        assert first.registered_count == 1
        assert first.registered_meeting_ids == ("recover-once",)
        assert first.recent_registered_meeting_ids == ("recover-once",)
        assert second.registered_count == 0
        assert second.already_registered_count == 1
        assert job is not None
        assert job.status == "recorded"
        assert Path(job.audio_path) == source
        assert source.read_bytes() == original

    @pytest.mark.asyncio
    async def test_복구스캔은_media_invalid도_quarantine하지_않음(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
    ) -> None:
        """복구 모드의 비수락 파일은 이동·삭제 없이 보존한다."""
        from core.audio_quality import AudioQualityStatus

        source = tmp_path / "audio_input" / "recover-invalid.wav"
        original = b"invalid but preserved"
        source.write_bytes(original)
        watcher._debounce_seconds = 0.0
        watcher._audio_validator = MagicMock(
            return_value=_quality_result(
                AudioQualityStatus.REJECT,
                failure_kind="media_invalid",
                quarantine_safe=True,
                reason="synthetic invalid",
            )
        )

        report = await watcher.recover_unregistered_files(recent_days=7)

        assert report.registered_count == 0
        assert report.preserved_count == 1
        assert source.read_bytes() == original
        assert not (tmp_path / "audio_quarantine").exists()
        assert (
            await asyncio.to_thread(
                job_queue.queue.get_job_by_meeting_id,
                "recover-invalid",
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_복구스캔_BUSY는_보류하고_CLEAR_재스캔에서_등록(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """실제 writable writer 증거는 등록하지 않고 다음 복구 때 다시 본다."""
        source = tmp_path / "audio_input" / "recover-busy.wav"
        original = b"writer-held audio"
        source.write_bytes(original)
        watcher._debounce_seconds = 0.0
        probe = AsyncMock(return_value=_OpenWriterState.BUSY)
        monkeypatch.setattr(watcher, "_probe_writable_open", probe)

        deferred = await watcher.recover_unregistered_files(recent_days=7)
        assert deferred.registered_count == 0
        assert deferred.deferred_count == 1
        assert source.read_bytes() == original
        assert (
            await asyncio.to_thread(
                job_queue.queue.get_job_by_meeting_id,
                "recover-busy",
            )
            is None
        )

        probe.return_value = _OpenWriterState.CLEAR
        recovered = await watcher.recover_unregistered_files(recent_days=7)
        job = await asyncio.to_thread(
            job_queue.queue.get_job_by_meeting_id,
            "recover-busy",
        )
        assert recovered.registered_count == 1
        assert job is not None and job.status == "recorded"
        assert source.read_bytes() == original

    @pytest.mark.asyncio
    async def test_복구스캔_same_meeting_id_다른경로는_충돌로_보존(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
    ) -> None:
        """같은 stem의 기존 row를 다른 원본으로 덮어쓰지 않는다."""
        source = tmp_path / "audio_input" / "recover-conflict.wav"
        source.write_bytes(b"new source stays")
        other_path = tmp_path / "audio_input" / "recover-conflict.mp3"
        await job_queue.add_job(
            "recover-conflict",
            str(other_path),
            initial_status="recorded",
        )
        watcher._debounce_seconds = 0.0

        report = await watcher.recover_unregistered_files(recent_days=7)
        existing = await asyncio.to_thread(
            job_queue.queue.get_job_by_meeting_id,
            "recover-conflict",
        )

        assert report.registered_count == 0
        assert report.conflict_count == 1
        assert existing is not None and Path(existing.audio_path) == other_path
        assert source.read_bytes() == b"new source stays"

    @pytest.mark.asyncio
    async def test_복구스캔은_source_mtime으로_최근7일_ID를_분리(
        self,
        watcher: FolderWatcher,
        tmp_path: Path,
    ) -> None:
        """DB 생성시각이 아니라 검증된 source mtime으로 최근 목록을 만든다."""
        old_source = tmp_path / "audio_input" / "recover-old.wav"
        recent_source = tmp_path / "audio_input" / "recover-recent.wav"
        old_source.write_bytes(b"old")
        recent_source.write_bytes(b"recent")
        old_time = time.time() - 8 * 24 * 60 * 60
        os.utime(old_source, (old_time, old_time))
        watcher._debounce_seconds = 0.0

        report = await watcher.recover_unregistered_files(recent_days=7)

        assert report.registered_count == 2
        assert report.registered_meeting_ids == ("recover-old", "recover-recent")
        assert report.recent_registered_meeting_ids == ("recover-recent",)

    @pytest.mark.asyncio
    async def test_복구스캔은_recordings_temp를_후보로_보지_않음(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
    ) -> None:
        """녹음 중 임시 파일은 완료 입력 디렉토리로 이동되기 전 등록하지 않는다."""
        temp_source = tmp_path / "recordings_temp" / "meeting-still-recording.wav"
        temp_source.parent.mkdir(parents=True, exist_ok=True)
        original = b"still recording"
        temp_source.write_bytes(original)

        report = await watcher.recover_unregistered_files(recent_days=7)

        assert report.candidate_count == 0
        assert report.registered_count == 0
        assert (
            await asyncio.to_thread(
                job_queue.queue.get_job_by_meeting_id,
                "meeting-still-recording",
            )
            is None
        )
        assert temp_source.read_bytes() == original

    @pytest.mark.asyncio
    async def test_복구스캔은_0건도_완료집계를_로그에_남김(
        self,
        watcher: FolderWatcher,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """운영 진단을 위해 빈 스캔도 후보·등록·보류·실패 합계를 기록한다."""
        with caplog.at_level(logging.INFO, logger="core.watcher"):
            report = await watcher.recover_unregistered_files(recent_days=7)

        assert report.candidate_count == 0
        assert report.registered_count == 0
        assert any(
            "오디오 입력 스캔 완료: mode=recovery" in record.message
            and "candidates=0" in record.message
            and "registered=0" in record.message
            and "deferred=0" in record.message
            and "failed=0" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_복구스캔_중에도_새_live_파일은_starvation없이_등록(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """복구 lock은 서로 다른 live 파일의 watcher 처리를 막지 않는다."""
        recovery_source = tmp_path / "audio_input" / "recover-slow.wav"
        live_source = tmp_path / "audio_input" / "live-during-recovery.wav"
        recovery_source.write_bytes(b"recovery")
        watcher._debounce_seconds = 0.0
        entered = asyncio.Event()
        release = asyncio.Event()
        real_prepare = watcher._prepare_ready_action

        async def _prepare(
            safe_path: Path,
            meeting_id: str,
            expected: _FileFingerprint,
            *,
            existing: object | None,
            quarantine_reason_prefix: str,
            notify: bool,
        ) -> _PreparedReadyAction | None:
            if safe_path == recovery_source:
                entered.set()
                await release.wait()
            return await real_prepare(
                safe_path,
                meeting_id,
                expected,
                existing=existing,
                quarantine_reason_prefix=quarantine_reason_prefix,
                notify=notify,
            )

        monkeypatch.setattr(watcher, "_prepare_ready_action", _prepare)
        recovery_task = asyncio.create_task(watcher.recover_unregistered_files(recent_days=7))
        await asyncio.wait_for(entered.wait(), timeout=1.0)

        live_source.write_bytes(b"live")
        await asyncio.wait_for(watcher._handle_new_file(live_source), timeout=1.0)
        live_job = await asyncio.to_thread(
            job_queue.queue.get_job_by_meeting_id,
            "live-during-recovery",
        )
        assert live_job is not None and live_job.status == "recorded"

        release.set()
        report = await asyncio.wait_for(recovery_task, timeout=1.0)
        assert report.registered_meeting_ids == ("recover-slow",)

    @pytest.mark.asyncio
    async def test_빈_파일_건너뜀(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
    ) -> None:
        """빈 오디오 파일은 건너뛰는지 확인한다."""
        watch_dir = tmp_path / "audio_input"
        (watch_dir / "empty.wav").write_bytes(b"")

        ids = await watcher.scan_existing()
        assert len(ids) == 0

    @pytest.mark.asyncio
    async def test_이미_등록된_파일_건너뜀(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
    ) -> None:
        """이미 등록된 파일은 건너뛰는지 확인한다."""
        watch_dir = tmp_path / "audio_input"
        (watch_dir / "registered.wav").write_bytes(b"audio")

        # 먼저 등록
        await job_queue.add_job("registered", str(watch_dir / "registered.wav"))

        # 스캔 — 건너뛰어야 함
        ids = await watcher.scan_existing()
        assert len(ids) == 0

    @pytest.mark.asyncio
    async def test_존재하지_않는_디렉토리(
        self,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
    ) -> None:
        """감시 디렉토리가 없을 때 빈 리스트를 반환하는지 확인한다."""
        config = _make_config(tmp_path)
        config.paths.resolved_audio_input_dir = tmp_path / "nonexistent"

        w = FolderWatcher(async_job_queue=job_queue, config=config)
        ids = await w.scan_existing()
        assert ids == []

    @pytest.mark.asyncio
    async def test_220개_startup_lsof는_bounded_concurrency(
        self,
        watcher: FolderWatcher,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """기존 파일 220개의 writer probe timeout을 순차 누적하지 않는다."""
        watch_dir = tmp_path / "audio_input"
        for index in range(220):
            (watch_dir / f"bulk-{index:03d}.wav").write_bytes(b"audio")

        watcher._debounce_seconds = 0.0
        watcher._startup_probe_concurrency = 8
        active = 0
        maximum = 0
        calls = 0

        async def _slow_probe(file_path: Path, *, timeout: float) -> _OpenWriterState:
            del file_path, timeout
            nonlocal active, maximum, calls
            active += 1
            maximum = max(maximum, active)
            calls += 1
            await asyncio.sleep(0.01)
            active -= 1
            return _OpenWriterState.CLEAR

        monkeypatch.setattr(watcher, "_probe_writable_open", _slow_probe)
        apply_action = AsyncMock(return_value=None)
        monkeypatch.setattr(watcher, "_apply_ready_action", apply_action)

        started_at = asyncio.get_running_loop().time()
        assert await watcher.scan_existing() == []
        elapsed = asyncio.get_running_loop().time() - started_at

        # 최초 preflight와 검증 뒤 final writer 확인 모두 같은 경계로 실행된다.
        assert calls == 440
        assert maximum == 8
        # 직렬 실행이면 synthetic probe만 4.4초가 필요하다. 절대 1.2초 상한은
        # CI 부하에 민감하므로 직렬 하한의 절반 안에 끝나는 구조만 고정한다.
        serial_probe_seconds = calls * 0.01
        assert elapsed < serial_probe_seconds / 2
        assert apply_action.await_count == 220

    @pytest.mark.asyncio
    async def test_writer_probe_전역_semaphore는_live_retry에도_적용(
        self,
        watcher: FolderWatcher,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """startup 외 경로도 실제 lsof 호출을 설정된 수만큼만 실행한다."""
        watcher._writer_probe_semaphore = asyncio.Semaphore(3)
        active = 0
        maximum = 0
        counter_lock = threading.Lock()

        def _slow_lsof(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            del args, kwargs
            nonlocal active, maximum
            with counter_lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with counter_lock:
                active -= 1
            return subprocess.CompletedProcess([], 1, stdout="", stderr="")

        monkeypatch.setattr(Path, "is_file", lambda _path: True)
        monkeypatch.setattr(os, "access", lambda _path, _mode: True)
        monkeypatch.setattr(subprocess, "run", _slow_lsof)
        paths = [tmp_path / f"probe-{index}.wav" for index in range(16)]

        results = await asyncio.gather(
            *(_REAL_WRITER_PROBE(watcher, path, timeout=0.5) for path in paths)
        )

        assert results == [_OpenWriterState.CLEAR] * 16
        assert maximum == 3

    @pytest.mark.asyncio
    async def test_startup_final_attestation_만료시_적용직전_재확인(
        self,
        watcher: FolderWatcher,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """병렬 final 확인이 오래되면 신규 ACCEPT도 세 번째로 확인한다."""
        source = tmp_path / "audio_input" / "stale-attestation.wav"
        source.write_bytes(b"audio")
        watcher._debounce_seconds = 0.0
        watcher._startup_writer_attestation_max_age_seconds = 0.25
        clock = 0.0

        def _monotonic() -> float:
            nonlocal clock
            clock += 1.0
            return clock

        probe = AsyncMock(return_value=_OpenWriterState.CLEAR)
        apply_action = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "core.watcher.time",
            SimpleNamespace(monotonic=_monotonic),
        )
        monkeypatch.setattr(watcher, "_probe_writable_open", probe)
        monkeypatch.setattr(watcher, "_apply_ready_action", apply_action)

        assert await watcher.scan_existing() == []

        assert probe.await_count == 3
        apply_action.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_startup_parallel_final_후_fingerprint_변경시_apply_차단(
        self,
        watcher: FolderWatcher,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """final writer 확인 직후 파일이 바뀌면 오래된 검증 결과를 적용하지 않는다."""
        source = tmp_path / "audio_input" / "changed-after-final.wav"
        source.write_bytes(b"audio")
        watcher._debounce_seconds = 0.0
        confirmations = 0

        async def _confirm(*args: object, **kwargs: object) -> bool:
            del args, kwargs
            nonlocal confirmations
            confirmations += 1
            if confirmations == 2:
                source.write_bytes(b"changed-after-final")
            return True

        apply_action = AsyncMock(return_value=None)
        monkeypatch.setattr(watcher, "_confirm_closed_unchanged", _confirm)
        monkeypatch.setattr(watcher, "_apply_ready_action", apply_action)
        monkeypatch.setattr(watcher, "_schedule_deferred_retry", MagicMock())

        assert await watcher.scan_existing() == []

        assert confirmations == 2
        apply_action.assert_not_awaited()
        assert source.read_bytes() == b"changed-after-final"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["existing", "quarantine"])
    async def test_startup_기존큐와_quarantine은_apply직전_writer_직렬확인(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
    ) -> None:
        """기존 큐 복원·격리는 신규 ACCEPT attestation cache를 사용하지 않는다."""
        from core.audio_quality import AudioQualityStatus

        paths = [tmp_path / "audio_input" / f"{mode}-{index}.wav" for index in range(2)]
        for path in paths:
            path.write_bytes(b"audio")
            if mode == "existing":
                job_id = await job_queue.add_job(
                    path.stem,
                    str(path),
                    initial_status="recorded",
                )
                await asyncio.to_thread(job_queue.queue.queue_job, job_id, "full")
        if mode == "quarantine":
            watcher._audio_validator = MagicMock(
                return_value=_quality_result(
                    AudioQualityStatus.REJECT,
                    failure_kind="media_invalid",
                    quarantine_safe=True,
                    reason="invalid",
                )
            )

        watcher._debounce_seconds = 0.0
        phase_final = False
        active = 0
        maximum = 0
        events: list[str] = []
        real_prepare = watcher._prepare_ready_action

        async def _prepare(
            safe_path: Path,
            meeting_id: str,
            expected: _FileFingerprint,
            *,
            existing: object | None,
            quarantine_reason_prefix: str,
            notify: bool,
        ) -> _PreparedReadyAction | None:
            nonlocal phase_final
            action = await real_prepare(
                safe_path,
                meeting_id,
                expected,
                existing=existing,
                quarantine_reason_prefix=quarantine_reason_prefix,
                notify=notify,
            )
            phase_final = True
            return action

        async def _confirm(file_path: Path, *args: object, **kwargs: object) -> bool:
            del args, kwargs
            nonlocal active, maximum
            if not phase_final:
                return True
            active += 1
            maximum = max(maximum, active)
            events.append(f"confirm:{file_path.name}")
            await asyncio.sleep(0.01)
            active -= 1
            return True

        async def _apply(action: _PreparedReadyAction) -> None:
            events.append(f"apply:{action.safe_path.name}")

        monkeypatch.setattr(watcher, "_prepare_ready_action", _prepare)
        monkeypatch.setattr(watcher, "_confirm_closed_unchanged", _confirm)
        monkeypatch.setattr(watcher, "_apply_ready_action", _apply)

        assert await watcher.scan_existing() == []

        assert maximum == 1
        assert events == [
            f"confirm:{paths[0].name}",
            f"apply:{paths[0].name}",
            f"confirm:{paths[1].name}",
            f"apply:{paths[1].name}",
        ]

    @pytest.mark.asyncio
    async def test_220개_반복_scan은_중복_등록이나_원본_삭제_없음(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
    ) -> None:
        """대량 startup scan을 반복해도 row와 source를 한 번만 보존한다."""
        watch_dir = tmp_path / "audio_input"
        expected: dict[str, bytes] = {}
        for index in range(220):
            name = f"idempotent-{index:03d}.wav"
            payload = f"audio-{index}".encode()
            expected[name] = payload
            (watch_dir / name).write_bytes(payload)

        watcher._debounce_seconds = 0.0
        first_ids = await watcher.scan_existing()
        second_ids = await watcher.scan_existing()
        jobs = await job_queue.get_all_jobs()

        assert len(first_ids) == 220
        assert second_ids == []
        assert len(jobs) == 220
        assert len({job.meeting_id for job in jobs}) == 220
        assert {
            path.name: path.read_bytes() for path in sorted(watch_dir.glob("*.wav"))
        } == expected

    @pytest.mark.asyncio
    async def test_startup_probe_하나_예외이_다음_파일을_막지_않음(
        self,
        watcher: FolderWatcher,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """lsof 파일별 오류를 격리하고 나머지 정상 파일을 계속 검사한다."""
        watch_dir = tmp_path / "audio_input"
        for name in ("before.wav", "broken.wav", "after.wav"):
            (watch_dir / name).write_bytes(b"audio")
        watcher._debounce_seconds = 0.0
        finished: list[str] = []

        async def _probe(file_path: Path, *, timeout: float) -> _OpenWriterState:
            del timeout
            if file_path.name == "broken.wav":
                raise RuntimeError("probe failed")
            return _OpenWriterState.CLEAR

        async def _apply(action: _PreparedReadyAction) -> None:
            finished.append(action.safe_path.name)

        monkeypatch.setattr(watcher, "_probe_writable_open", _probe)
        monkeypatch.setattr(watcher, "_apply_ready_action", _apply)
        monkeypatch.setattr(watcher, "_schedule_deferred_retry", MagicMock())

        assert await watcher.scan_existing() == []
        assert finished == ["after.wav", "before.wav"]

    @pytest.mark.asyncio
    async def test_startup_scan은_DB_await_전_live_event와_소유권_공유(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """admission hold await 중 동일 파일 live event가 이중 검증하지 않는다."""
        source = tmp_path / "audio_input" / "scan-live-race.wav"
        source.write_bytes(b"audio")
        await job_queue.add_job(
            "scan-live-race",
            str(source),
            initial_status="queued",
        )
        watcher._debounce_seconds = 0.0
        prepare_started = asyncio.Event()
        prepare_release = asyncio.Event()
        prepare_calls = 0

        async def _prepare(existing: object, safe_path: Path) -> bool:
            del existing, safe_path
            nonlocal prepare_calls
            prepare_calls += 1
            prepare_started.set()
            await prepare_release.wait()
            return True

        apply_action = AsyncMock(return_value=None)
        monkeypatch.setattr(watcher, "_prepare_existing_audit", _prepare)
        monkeypatch.setattr(watcher, "_apply_ready_action", apply_action)
        monkeypatch.setattr(watcher, "_schedule_deferred_retry", MagicMock())

        scan_task = asyncio.create_task(watcher.scan_existing())
        await asyncio.wait_for(prepare_started.wait(), timeout=1.0)
        live_task = asyncio.create_task(watcher._handle_new_file(source))
        await asyncio.sleep(0)
        prepare_release.set()
        await asyncio.gather(scan_task, live_task)

        assert prepare_calls == 1
        apply_action.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_startup_scan은_첫_DB조회_전_live_event와_소유권_공유(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """첫 DB await보다 먼저 pending을 예약해 live 처리와 중복하지 않는다."""
        source = tmp_path / "audio_input" / "scan-db-race.wav"
        source.write_bytes(b"audio")
        watcher._debounce_seconds = 0.0
        query_started = threading.Event()
        query_release = threading.Event()
        query_calls = 0
        real_get = job_queue.queue.get_job_by_meeting_id

        def _blocked_get(meeting_id: str) -> Job | None:
            nonlocal query_calls
            query_calls += 1
            if query_calls == 1:
                query_started.set()
                assert query_release.wait(timeout=1.0)
            return real_get(meeting_id)

        real_prepare = watcher._prepare_ready_action
        real_apply = watcher._apply_ready_action
        prepare_action = AsyncMock(wraps=real_prepare)
        apply_action = AsyncMock(wraps=real_apply)
        monkeypatch.setattr(job_queue.queue, "get_job_by_meeting_id", _blocked_get)
        monkeypatch.setattr(watcher, "_prepare_ready_action", prepare_action)
        monkeypatch.setattr(watcher, "_apply_ready_action", apply_action)
        monkeypatch.setattr(watcher, "_schedule_deferred_retry", MagicMock())

        scan_task = asyncio.create_task(watcher.scan_existing())
        assert await asyncio.to_thread(query_started.wait, 1.0)
        live_task = asyncio.create_task(watcher._handle_new_file(source))
        await asyncio.sleep(0)
        query_release.set()
        await asyncio.gather(scan_task, live_task)

        assert query_calls == 1
        prepare_action.assert_awaited_once()
        apply_action.assert_awaited_once()
        jobs = await job_queue.get_all_jobs()
        assert len(jobs) == 1
        assert jobs[0].meeting_id == "scan-db-race"


# === 에러 처리 테스트 ===


class TestErrorHandling:
    """에러 처리 테스트."""

    @pytest.mark.asyncio
    async def test_큐_등록_실패_시_계속_동작(
        self,
        watcher: FolderWatcher,
        tmp_path: Path,
    ) -> None:
        """작업 큐 등록 실패 시에도 감시가 계속되는지 확인한다."""
        test_file = tmp_path / "audio_input" / "fail_test.wav"
        test_file.write_bytes(b"audio data")

        # add_job이 에러를 던지도록 모킹
        with patch.object(
            watcher._job_queue,
            "add_job",
            new_callable=AsyncMock,
            side_effect=JobQueueError("DB 에러"),
        ):
            # 에러 없이 처리 완료
            await watcher._handle_new_file(test_file)

    @pytest.mark.asyncio
    async def test_파일_접근_에러_처리(
        self,
        watcher: FolderWatcher,
        tmp_path: Path,
    ) -> None:
        """파일 접근 에러 시 False를 반환하는지 확인한다."""
        bad_file = tmp_path / "audio_input" / "no_access.wav"
        # 파일이 존재하지 않음 → OSError 유발 가능
        result = await watcher._wait_for_stable_size(bad_file)
        assert result is False

    @pytest.mark.asyncio
    async def test_에러_계층_구조(self) -> None:
        """에러 클래스 계층 구조를 확인한다."""
        assert issubclass(AlreadyWatchingError, WatcherError)
        assert issubclass(WatchDirectoryError, WatcherError)
        assert issubclass(WatcherError, Exception)


# === 통합 테스트 ===


class TestIntegration:
    """watchdog Observer와의 통합 테스트."""

    @pytest.mark.asyncio
    async def test_실시간_파일_감지(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
    ) -> None:
        """watchdog Observer를 통한 실시간 파일 감지를 테스트한다."""
        await watcher.start()

        # 파일 생성
        watch_dir = tmp_path / "audio_input"
        test_file = watch_dir / "realtime_test.wav"
        test_file.write_bytes(b"real audio data " * 100)

        # debounce + 이벤트 전파 대기
        await asyncio.sleep(1.5)

        # 큐에 등록 확인
        job = await asyncio.to_thread(job_queue.queue.get_job_by_meeting_id, "realtime_test")
        assert job is not None
        assert job.status == "recorded"

        await watcher.stop()

    @pytest.mark.asyncio
    async def test_비오디오_파일_실시간_무시(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
    ) -> None:
        """watchdog Observer가 비오디오 파일을 무시하는지 테스트한다."""
        await watcher.start()

        # 비오디오 파일 생성
        watch_dir = tmp_path / "audio_input"
        (watch_dir / "notes.txt").write_bytes(b"text content")
        (watch_dir / "data.json").write_bytes(b'{"key": "value"}')

        await asyncio.sleep(1.0)

        # 큐에 아무것도 없어야 함
        all_jobs = await job_queue.get_all_jobs()
        assert len(all_jobs) == 0

        await watcher.stop()

    @pytest.mark.asyncio
    async def test_다중_파일_연속_감지(
        self,
        watcher: FolderWatcher,
        job_queue: AsyncJobQueue,
        tmp_path: Path,
    ) -> None:
        """여러 파일이 연속으로 감지되는지 테스트한다."""
        await watcher.start()

        watch_dir = tmp_path / "audio_input"
        for i in range(3):
            (watch_dir / f"multi_{i}.wav").write_bytes(b"audio " * 50)
            await asyncio.sleep(0.1)  # 약간의 간격

        # 전체 처리 대기
        await asyncio.sleep(2.0)

        all_jobs = await job_queue.get_all_jobs()
        assert len(all_jobs) == 3

        await watcher.stop()


# === Phase 1 (품질 게이트 + 제외 경로) 테스트 ===


def test_FolderWatcher가_config에서_excluded_subdirs_로드(monkeypatch, tmp_path):
    """WatcherConfig.excluded_subdirs가 FolderWatcher에 반영된다."""
    from config import AppConfig, PathsConfig, WatcherConfig
    from core.job_queue import AsyncJobQueue, JobQueue
    from core.watcher import FolderWatcher

    config = AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        watcher=WatcherConfig(excluded_subdirs=["audio_quarantine", "trash"]),
    )

    # 더미 큐 (실제 사용 안 함, 초기화만 통과하면 됨)
    queue_db = tmp_path / "pipeline.db"
    sync_queue = JobQueue(db_path=queue_db)
    async_queue = AsyncJobQueue(sync_queue)

    watcher = FolderWatcher(async_queue, config=config)

    assert "audio_quarantine" in watcher._excluded_subdirs
    assert "trash" in watcher._excluded_subdirs


def test_excluded_path_인지_판정(monkeypatch, tmp_path):
    """_is_excluded가 excluded_subdirs에 속한 경로를 정확히 판정한다."""
    from config import AppConfig, PathsConfig, WatcherConfig
    from core.job_queue import AsyncJobQueue, JobQueue
    from core.watcher import FolderWatcher

    watch_dir = tmp_path / "audio_input"
    watch_dir.mkdir()
    (tmp_path / "audio_quarantine").mkdir()

    config = AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        watcher=WatcherConfig(excluded_subdirs=["audio_quarantine"]),
    )

    queue_db = tmp_path / "pipeline.db"
    async_queue = AsyncJobQueue(JobQueue(db_path=queue_db))
    watcher = FolderWatcher(async_queue, config=config)

    # base_dir/audio_quarantine/x.wav 는 excluded
    assert watcher._is_excluded(tmp_path / "audio_quarantine" / "x.wav") is True
    # base_dir/audio_input/x.wav 는 excluded 아님
    assert watcher._is_excluded(watch_dir / "x.wav") is False


@pytest.mark.asyncio
async def test_품질_게이트_reject_시_quarantine_이동_후_큐등록_안함(monkeypatch, tmp_path):
    """저볼륨 파일은 quarantine으로 이동되고 큐에 들어가지 않는다."""
    from config import AppConfig, AudioQualityConfig, PathsConfig, WatcherConfig
    from core.audio_quality import AudioQualityStatus
    from core.job_queue import AsyncJobQueue, JobQueue
    from core.watcher import FolderWatcher

    watch_dir = tmp_path / "audio_input"
    watch_dir.mkdir()

    bad_file = watch_dir / "quiet.wav"
    bad_file.write_bytes(b"x" * 100)  # 파일 안정화 통과용

    config = AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        audio_quality=AudioQualityConfig(enabled=True, min_mean_volume_db=-40.0),
        watcher=WatcherConfig(),
    )

    queue_db = tmp_path / "pipeline.db"
    async_queue = AsyncJobQueue(JobQueue(db_path=queue_db))
    await async_queue.initialize()
    watcher = FolderWatcher(async_queue, config=config)

    # validator를 REJECT 반환하도록 monkeypatch
    def fake_validator(path: Path, **_kwargs: object) -> SimpleNamespace:
        return _quality_result(
            AudioQualityStatus.REJECT,
            failure_kind="media_invalid",
            quarantine_safe=True,
            reason="저볼륨 테스트",
        )

    monkeypatch.setattr(watcher, "_audio_validator", fake_validator)

    # debounce 우회: 파일 안정화를 항상 True로
    async def fake_stable(self, path):
        return True

    monkeypatch.setattr(FolderWatcher, "_wait_for_stable_size", fake_stable)

    # 실행
    await watcher._handle_new_file(bad_file)

    # 검증: 큐에 작업 없음, 파일은 quarantine으로 이동
    quarantine_dir = config.paths.resolved_audio_quarantine_dir
    assert not bad_file.exists()
    assert (quarantine_dir / "quiet.wav").exists()

    # 큐에 작업이 없어야 함
    import asyncio

    job = await asyncio.to_thread(
        async_queue.queue.get_job_by_meeting_id,
        "quiet",
    )
    assert job is None


@pytest.mark.asyncio
async def test_품질_게이트_accept_시_정상_큐등록(monkeypatch, tmp_path):
    """정상 파일은 큐에 등록된다."""
    from config import AppConfig, AudioQualityConfig, PathsConfig, WatcherConfig
    from core.audio_quality import AudioQualityResult, AudioQualityStatus
    from core.job_queue import AsyncJobQueue, JobQueue
    from core.watcher import FolderWatcher

    watch_dir = tmp_path / "audio_input"
    watch_dir.mkdir()
    good_file = watch_dir / "ok.wav"
    good_file.write_bytes(b"x" * 100)

    config = AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        audio_quality=AudioQualityConfig(enabled=True),
        watcher=WatcherConfig(),
    )

    queue_db = tmp_path / "pipeline.db"
    async_queue = AsyncJobQueue(JobQueue(db_path=queue_db))
    await async_queue.initialize()
    watcher = FolderWatcher(async_queue, config=config)

    def fake_validator(path: Path, **_kwargs: object) -> AudioQualityResult:
        return AudioQualityResult(
            status=AudioQualityStatus.ACCEPT,
            mean_volume_db=-25.0,
            duration_seconds=900.0,
            reason="",
        )

    monkeypatch.setattr(watcher, "_audio_validator", fake_validator)

    async def fake_stable(self, path):
        return True

    monkeypatch.setattr(FolderWatcher, "_wait_for_stable_size", fake_stable)

    await watcher._handle_new_file(good_file)

    # 큐에 작업 존재해야 함
    import asyncio

    job = await asyncio.to_thread(
        async_queue.queue.get_job_by_meeting_id,
        "ok",
    )
    assert job is not None


@pytest.mark.asyncio
async def test_watcher_validator에_identity와_configured_decode_timeout을_전달(
    tmp_path: Path,
) -> None:
    """watcher 품질 검증은 공통 cache identity와 timeout 설정을 그대로 쓴다."""
    from config import AppConfig, AudioQualityConfig, PathsConfig
    from core.audio_quality import AudioQualityResult, AudioQualityStatus
    from core.job_queue import AsyncJobQueue, JobQueue
    from core.watcher import FolderWatcher

    watch_dir = tmp_path / "audio_input"
    watch_dir.mkdir()
    source = watch_dir / "configured-timeout.wav"
    source.write_bytes(b"audio")
    config = AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        audio_quality=AudioQualityConfig(
            min_mean_volume_db=-37.5,
            min_duration_seconds=31.0,
            decode_timeout_base_seconds=7.0,
            decode_timeout_factor=0.75,
            decode_timeout_cap_seconds=19.0,
        ),
    )
    queue = AsyncJobQueue(JobQueue(db_path=tmp_path / "pipeline.db"))
    await queue.initialize()
    watcher = FolderWatcher(queue, config=config)
    inspected = watcher._inspect_input_file(source)
    assert inspected is not None
    _, fingerprint = inspected
    received: dict[str, object] = {}

    def validator(path: Path, **kwargs: object) -> AudioQualityResult:
        received["path"] = path
        received.update(kwargs)
        return AudioQualityResult(
            status=AudioQualityStatus.ACCEPT,
            mean_volume_db=-20.0,
            duration_seconds=31.0,
        )

    watcher._audio_validator = validator

    valid, result, validated = await watcher._validate_unchanged(source, fingerprint)

    assert valid is True
    assert result is not None and result.status is AudioQualityStatus.ACCEPT
    assert validated == fingerprint
    assert received == {
        "path": source,
        "min_mean_db": -37.5,
        "min_duration_s": 31.0,
        "expected_identity": fingerprint.as_tuple(),
        "decode_timeout_base_seconds": 7.0,
        "decode_timeout_factor": 0.75,
        "decode_timeout_cap_seconds": 19.0,
    }


@pytest.mark.asyncio
async def test_품질_게이트_infra_error_시_원본_보존_후_큐등록_안함(monkeypatch, tmp_path):
    """도구 부재/timeout ERROR는 격리하지 않고 fail-closed로 보존한다."""
    from config import AppConfig, PathsConfig
    from core.audio_quality import AudioQualityStatus
    from core.job_queue import AsyncJobQueue, JobQueue
    from core.watcher import FolderWatcher

    watch_dir = tmp_path / "audio_input"
    watch_dir.mkdir()
    file = watch_dir / "unknown.wav"
    file.write_bytes(b"x")

    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))

    queue_db = tmp_path / "pipeline.db"
    async_queue = AsyncJobQueue(JobQueue(db_path=queue_db))
    await async_queue.initialize()
    watcher = FolderWatcher(async_queue, config=config)

    def fake_validator(path: Path, **_kwargs: object) -> SimpleNamespace:
        return _quality_result(
            AudioQualityStatus.ERROR,
            failure_kind="infra_unavailable",
            quarantine_safe=False,
            reason="측정 실패",
        )

    monkeypatch.setattr(watcher, "_audio_validator", fake_validator)

    async def fake_stable(self, path):
        return True

    monkeypatch.setattr(FolderWatcher, "_wait_for_stable_size", fake_stable)

    await watcher._handle_new_file(file)

    quarantine_dir = config.paths.resolved_audio_quarantine_dir
    assert file.exists()
    assert not (quarantine_dir / "unknown.wav").exists()

    import asyncio

    job = await asyncio.to_thread(
        async_queue.queue.get_job_by_meeting_id,
        "unknown",
    )
    assert job is None


@pytest.mark.asyncio
async def test_품질_validator_예외_시_원본_보존_후_큐등록_안함(monkeypatch, tmp_path):
    """내부 validator 예외는 원본을 보존하되 fail-closed로 큐 등록을 막는다."""
    from config import AppConfig, PathsConfig
    from core.job_queue import AsyncJobQueue, JobQueue
    from core.watcher import FolderWatcher

    watch_dir = tmp_path / "audio_input"
    watch_dir.mkdir()
    file = watch_dir / "broken.wav"
    file.write_bytes(b"x")

    config = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    async_queue = AsyncJobQueue(JobQueue(db_path=tmp_path / "pipeline.db"))
    await async_queue.initialize()
    watcher = FolderWatcher(async_queue, config=config)

    def failing_validator(path: Path, **_kwargs: object) -> None:
        raise RuntimeError("validator bug")

    monkeypatch.setattr(watcher, "_audio_validator", failing_validator)

    async def fake_stable(self, path):
        return True

    monkeypatch.setattr(FolderWatcher, "_wait_for_stable_size", fake_stable)

    await watcher._handle_new_file(file)

    assert file.exists()
    assert not (config.paths.resolved_audio_quarantine_dir / "broken.wav").exists()
    job = await asyncio.to_thread(
        async_queue.queue.get_job_by_meeting_id,
        "broken",
    )
    assert job is None


# === Cleanup 1 (2026-04-21): scan_existing() 품질 게이트 누수 방지 ===


@pytest.mark.asyncio
async def test_scan_existing이_저볼륨_파일을_quarantine으로_이동(
    watcher: FolderWatcher, job_queue: AsyncJobQueue, tmp_path: Path, monkeypatch
):
    """앱 재기동 시 scan_existing이 저볼륨 파일을 큐에 올리지 않고 격리한다.

    Phase 1 최종 리뷰 Important #1: scan_existing 이 _handle_new_file 의
    품질 게이트를 우회하는 누수. 재기동 경로에서도 크래시 파일 재진입 차단.
    """
    from core.audio_quality import AudioQualityStatus

    watch_dir = tmp_path / "audio_input"
    bad_file = watch_dir / "bad_meeting.wav"
    bad_file.write_bytes(b"x" * 100)

    def fake_validator(path: Path, **_kwargs: object) -> SimpleNamespace:
        return _quality_result(
            AudioQualityStatus.REJECT,
            failure_kind="media_invalid",
            quarantine_safe=True,
            reason="저볼륨: mean=-48.0dB < -40.0dB",
        )

    monkeypatch.setattr(watcher, "_audio_validator", fake_validator)
    quarantine_dir = tmp_path / "audio_quarantine"
    monkeypatch.setattr(watcher, "_quarantine_dir", quarantine_dir)

    ids = await watcher.scan_existing()

    # 저볼륨 파일은 큐에 등록되지 않고 격리됨
    assert len(ids) == 0
    assert not bad_file.exists()
    moved = quarantine_dir / "bad_meeting.wav"
    assert moved.exists()

    # 큐 확인
    job = await asyncio.to_thread(
        job_queue.queue.get_job_by_meeting_id,
        "bad_meeting",
    )
    assert job is None


@pytest.mark.asyncio
async def test_scan_existing_accept_시_정상_등록(
    watcher: FolderWatcher, tmp_path: Path, monkeypatch
):
    """scan_existing 의 validator 가 ACCEPT 반환 시 정상 큐 등록."""
    from core.audio_quality import AudioQualityResult, AudioQualityStatus

    watch_dir = tmp_path / "audio_input"
    good_file = watch_dir / "good.wav"
    good_file.write_bytes(b"x")

    def fake_accept(path: Path, **_kwargs: object) -> AudioQualityResult:
        return AudioQualityResult(
            status=AudioQualityStatus.ACCEPT,
            mean_volume_db=-25.0,
            duration_seconds=600.0,
            reason="",
        )

    monkeypatch.setattr(watcher, "_audio_validator", fake_accept)

    ids = await watcher.scan_existing()

    assert len(ids) == 1


@pytest.mark.asyncio
async def test_scan_existing_infra_error_시_원본_보존(
    watcher: FolderWatcher, job_queue: AsyncJobQueue, tmp_path: Path, monkeypatch
):
    """재기동 스캔의 인프라 ERROR는 원본을 보존하고 큐에 넣지 않는다."""
    from core.audio_quality import AudioQualityStatus

    watch_dir = tmp_path / "audio_input"
    bad_file = watch_dir / "corrupt.wav"
    bad_file.write_bytes(b"x")

    def fake_validator(path: Path, **_kwargs: object) -> SimpleNamespace:
        return _quality_result(
            AudioQualityStatus.ERROR,
            failure_kind="infra_unavailable",
            quarantine_safe=False,
            reason="측정 실패: ffmpeg timeout",
        )

    monkeypatch.setattr(watcher, "_audio_validator", fake_validator)
    quarantine_dir = tmp_path / "audio_quarantine"
    monkeypatch.setattr(watcher, "_quarantine_dir", quarantine_dir)

    ids = await watcher.scan_existing()

    assert ids == []
    assert bad_file.exists()
    assert not (quarantine_dir / "corrupt.wav").exists()
    job = await asyncio.to_thread(
        job_queue.queue.get_job_by_meeting_id,
        "corrupt",
    )
    assert job is None


# === Fail-closed admission + startup cleanup RED 계약 ===


@pytest.mark.asyncio
async def test_new_event_input_symlink는_target을_검사하거나_이동하지_않음(
    tmp_path: Path,
    job_queue: AsyncJobQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """입력 symlink는 SECURITY_BLOCKED이며 링크와 외부 target 모두 보존한다."""
    from core.audio_quality import AudioQualityStatus

    base_dir = tmp_path / "base"
    base_dir.mkdir()
    config = _make_config(base_dir)
    watcher = FolderWatcher(job_queue, config=config)
    target = tmp_path / "external-target.wav"
    original = b"external source must survive"
    target.write_bytes(original)
    link = config.paths.resolved_audio_input_dir / "linked.wav"
    link.symlink_to(target)
    validator = MagicMock(
        return_value=_quality_result(
            AudioQualityStatus.REJECT,
            failure_kind="media_invalid",
            quarantine_safe=True,
            reason="short",
        )
    )
    monkeypatch.setattr(watcher, "_audio_validator", validator)

    async def _stable(path: Path) -> bool:
        return True

    monkeypatch.setattr(watcher, "_wait_for_stable_size", _stable)

    await watcher._handle_new_file(link)

    assert link.is_symlink()
    assert target.read_bytes() == original
    validator.assert_not_called()
    assert await job_queue.get_all_jobs() == []
    quarantine_dir = config.paths.resolved_audio_quarantine_dir
    assert not quarantine_dir.exists() or list(quarantine_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_scan_existing_input_symlink는_target을_검사하거나_이동하지_않음(
    tmp_path: Path,
    job_queue: AsyncJobQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """startup scan도 is_file/resolve로 symlink target을 따라가면 안 된다."""
    from core.audio_quality import AudioQualityStatus

    base_dir = tmp_path / "base"
    base_dir.mkdir()
    config = _make_config(base_dir)
    watcher = FolderWatcher(job_queue, config=config)
    target = tmp_path / "external-scan-target.wav"
    original = b"scan must not touch external target"
    target.write_bytes(original)
    link = config.paths.resolved_audio_input_dir / "linked-scan.wav"
    link.symlink_to(target)
    validator = MagicMock(
        return_value=_quality_result(
            AudioQualityStatus.REJECT,
            failure_kind="media_invalid",
            quarantine_safe=True,
            reason="short",
        )
    )
    monkeypatch.setattr(watcher, "_audio_validator", validator)

    assert await watcher.scan_existing() == []

    assert link.is_symlink()
    assert target.read_bytes() == original
    validator.assert_not_called()
    assert await job_queue.get_all_jobs() == []


@pytest.mark.asyncio
async def test_scan_existing_confirmed_corrupt만_quarantine_safe로_격리(
    watcher: FolderWatcher,
    job_queue: AsyncJobQueue,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """실제 decode가 media-invalid로 확정된 ERROR만 복구 가능한 격리 대상이다."""
    from core.audio_quality import AudioQualityStatus

    bad_file = tmp_path / "audio_input" / "decoded-corrupt.wav"
    bad_file.write_bytes(b"malformed")
    monkeypatch.setattr(
        watcher,
        "_audio_validator",
        MagicMock(
            return_value=_quality_result(
                AudioQualityStatus.ERROR,
                failure_kind="media_invalid",
                quarantine_safe=True,
                reason="invalid data found when processing input",
            )
        ),
    )

    assert await watcher.scan_existing() == []

    assert not bad_file.exists()
    moved = list((tmp_path / "audio_quarantine").glob("decoded-corrupt*.wav"))
    assert len(moved) == 1
    assert await job_queue.get_all_jobs() == []


@pytest.mark.asyncio
async def test_scan_existing_격리실패후에도_다음_valid_파일을_계속_등록(
    watcher: FolderWatcher,
    job_queue: AsyncJobQueue,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """파일 하나의 quarantine OSError가 startup 전체 scan을 중단하면 안 된다."""
    from core.audio_quality import AudioQualityStatus

    watch_dir = tmp_path / "audio_input"
    invalid = watch_dir / "a-invalid.wav"
    valid = watch_dir / "z-valid.wav"
    invalid.write_bytes(b"invalid")
    valid.write_bytes(b"valid")
    blocker = tmp_path / "not-a-directory"
    blocker.write_bytes(b"block quarantine mkdir")
    monkeypatch.setattr(watcher, "_quarantine_dir", blocker / "audio_quarantine")

    def _validate(path: Path, **_kwargs: object) -> SimpleNamespace:
        if path.name == invalid.name:
            return _quality_result(
                AudioQualityStatus.REJECT,
                failure_kind="media_invalid",
                quarantine_safe=True,
                reason="short",
            )
        return _quality_result(
            AudioQualityStatus.ACCEPT,
            failure_kind="media_invalid",
            quarantine_safe=False,
            reason="",
        )

    monkeypatch.setattr(watcher, "_audio_validator", _validate)

    ids = await watcher.scan_existing()

    assert invalid.exists()
    assert len(ids) == 1
    valid_job = await asyncio.to_thread(
        job_queue.queue.get_job_by_meeting_id,
        "z-valid",
    )
    assert valid_job is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_status", ["recorded", "queued", "failed"])
async def test_scan_existing_legacy_no_artifact_media_invalid은_격리후_row삭제(
    watcher: FolderWatcher,
    job_queue: AsyncJobQueue,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_status: str,
) -> None:
    """UI 오류를 남기는 legacy invalid row는 격리 성공과 원자적으로 정리한다."""
    from core.audio_quality import AudioQualityStatus

    source = tmp_path / "audio_input" / f"legacy-{legacy_status}.wav"
    payload = f"legacy-{legacy_status}".encode()
    source.write_bytes(payload)
    await job_queue.add_job(source.stem, str(source), initial_status=legacy_status)
    monkeypatch.setattr(
        watcher,
        "_audio_validator",
        MagicMock(
            return_value=_quality_result(
                AudioQualityStatus.REJECT,
                failure_kind="media_invalid",
                quarantine_safe=True,
                reason="decoded duration < 30 seconds",
            )
        ),
    )

    assert await watcher.scan_existing() == []

    assert not source.exists()
    quarantined = list((tmp_path / "audio_quarantine").glob(f"{source.stem}*.wav"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == payload
    row = await asyncio.to_thread(
        job_queue.queue.get_job_by_meeting_id,
        source.stem,
    )
    assert row is None


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["handle", "scan"])
@pytest.mark.parametrize("claim_status", ["recording", "recorded"])
async def test_retranscribe_claim은_invalid_source여도_legacy_audit에서_보존(
    watcher: FolderWatcher,
    job_queue: AsyncJobQueue,
    tmp_path: Path,
    entrypoint: str,
    claim_status: str,
) -> None:
    """startup recovery가 소유한 재전사 claim·staging을 watcher가 삭제하지 않는다."""
    from core.audio_quality import AudioQualityStatus
    from core.job_queue import parse_retranscribe_claim, retranscribe_staging_paths

    meeting_id = f"retranscribe-claim-{entrypoint}"
    source = tmp_path / "audio_input" / f"{meeting_id}.wav"
    payload = b"invalid source after retranscribe crash"
    source.write_bytes(payload)
    job_id = await job_queue.add_job(
        meeting_id,
        str(source),
        initial_status="completed",
    )
    token = f"claim-{entrypoint}"
    await asyncio.to_thread(
        job_queue.queue.claim_for_retranscribe,
        job_id,
        token,
    )
    await asyncio.to_thread(
        job_queue.queue.update_retranscribe_claim_phase,
        job_id,
        token,
        "staging",
    )

    if claim_status == "recorded":
        # nominal public claim은 ``recording``이지만, 이전 버전/부분
        # 마이그레이션에서 durable payload가 ``recorded`` row에 남아도
        # watcher가 legacy row로 오인해 삭제하면 안 된다.
        connection = job_queue.queue._ensure_connection()
        with job_queue.queue._write_lock:
            connection.execute(
                "UPDATE jobs SET status = ? WHERE id = ?",
                ("recorded", job_id),
            )
            connection.commit()

    checkpoint_stage, output_stage = retranscribe_staging_paths(
        tmp_path / "checkpoints",
        tmp_path / "outputs",
        meeting_id,
        token,
    )
    checkpoint_stage.mkdir(parents=True)
    (checkpoint_stage / "pipeline_state.json").write_text("{}", encoding="utf-8")
    output_stage.mkdir(parents=True)
    (output_stage / "summary.md").write_text("기존 요약", encoding="utf-8")

    validator = MagicMock(
        return_value=_quality_result(
            AudioQualityStatus.REJECT,
            failure_kind="media_invalid",
            quarantine_safe=True,
            reason="decoded duration < 30 seconds",
        )
    )
    watcher._audio_validator = validator

    if entrypoint == "handle":
        await watcher._handle_new_file(source)
    else:
        assert await watcher.scan_existing() == []

    preserved = await asyncio.to_thread(job_queue.queue.get_job, job_id)
    claim = parse_retranscribe_claim(preserved.requested_action)
    assert preserved.status == claim_status
    assert claim is not None
    assert claim.token == token
    assert source.read_bytes() == payload
    assert (checkpoint_stage / "pipeline_state.json").exists()
    assert (output_stage / "summary.md").read_text(encoding="utf-8") == "기존 요약"
    assert not (tmp_path / "audio_quarantine").exists()
    validator.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("protected_kind", ["completed", "transcript", "output"])
async def test_scan_existing_completed_또는_산출물보유_job은_자동정리하지_않음(
    watcher: FolderWatcher,
    job_queue: AsyncJobQueue,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protected_kind: str,
) -> None:
    """사용자 산출물이 있는 job은 startup audit가 원본/DB를 파괴하지 않는다."""
    from core.audio_quality import AudioQualityStatus

    meeting_id = f"protected-{protected_kind}"
    source = tmp_path / "audio_input" / f"{meeting_id}.wav"
    original = b"protected source"
    source.write_bytes(original)
    status = "completed" if protected_kind == "completed" else "recorded"
    await job_queue.add_job(meeting_id, str(source), initial_status=status)
    if protected_kind == "transcript":
        checkpoint_dir = tmp_path / "checkpoints" / meeting_id
        checkpoint_dir.mkdir(parents=True)
        (checkpoint_dir / "transcribe.json").write_text("{}", encoding="utf-8")
    elif protected_kind == "output":
        output_dir = tmp_path / "outputs" / meeting_id
        output_dir.mkdir(parents=True)
        (output_dir / "meeting_minutes.md").write_text("minutes", encoding="utf-8")
    validator = MagicMock(
        return_value=_quality_result(
            AudioQualityStatus.REJECT,
            failure_kind="media_invalid",
            quarantine_safe=True,
            reason="short",
        )
    )
    monkeypatch.setattr(watcher, "_audio_validator", validator)

    assert await watcher.scan_existing() == []

    assert source.read_bytes() == original
    row = await asyncio.to_thread(job_queue.queue.get_job_by_meeting_id, meeting_id)
    assert row is not None
    assert row.status == status
    validator.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_status", ["recorded", "queued", "failed"])
async def test_scan_existing_legacy_infra는_source_row보존하고_recorded로_해제(
    watcher: FolderWatcher,
    job_queue: AsyncJobQueue,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_status: str,
) -> None:
    """infra 불능은 격리/삭제하지 않고 UI failed·자동 실행 상태만 해제한다."""
    from core.audio_quality import AudioQualityStatus
    from core.job_queue import JobStatus

    meeting_id = f"infra-{legacy_status}"
    source = tmp_path / "audio_input" / f"{meeting_id}.wav"
    original = b"valid but validator infra unavailable"
    source.write_bytes(original)
    job_id = await job_queue.add_job(meeting_id, str(source), initial_status=legacy_status)
    if legacy_status == "failed":
        await asyncio.to_thread(
            job_queue.queue.force_set_status,
            job_id,
            JobStatus.FAILED,
            "stale media error",
        )
    validator = MagicMock(
        return_value=_quality_result(
            AudioQualityStatus.ERROR,
            failure_kind="infra_unavailable",
            quarantine_safe=False,
            reason="ffmpeg unavailable",
        )
    )
    monkeypatch.setattr(watcher, "_audio_validator", validator)

    assert await watcher.scan_existing() == []

    assert source.read_bytes() == original
    assert not (tmp_path / "audio_quarantine").exists()
    row = await asyncio.to_thread(job_queue.queue.get_job_by_meeting_id, meeting_id)
    assert row is not None
    assert row.status == "recorded"
    assert row.error_message == ""
    validator.assert_called_once()


# === Watcher/quarantine no-follow + final readiness 2차 RED 계약 ===


@pytest.mark.parametrize(
    "field_name",
    [
        "audio_input_dir",
        "audio_quarantine_subdir",
        "checkpoints_dir",
        "outputs_dir",
    ],
)
@pytest.mark.parametrize("unsafe_path", ["../outside", "/tmp/meeting-transcriber-outside"])
def test_watcher_설정_하위경로는_base_dir_밖을_허용하지_않음(
    tmp_path: Path,
    field_name: str,
    unsafe_path: str,
) -> None:
    """validation을 우회한 config도 watcher 경계에서 containment한다."""
    config = _make_config(tmp_path)
    config.paths.base_dir = str(tmp_path)
    setattr(config.paths, field_name, unsafe_path)

    with pytest.raises(WatcherError, match="base_dir"):
        FolderWatcher(MagicMock(), config=config)


def test_watcher_설정_하위경로의_nested_상대경로는_허용(
    tmp_path: Path,
) -> None:
    """중첩된 정상 상대경로는 기존 커스텀 구성 호환성을 유지한다."""
    from config import AppConfig, PathsConfig

    config = AppConfig(
        paths=PathsConfig(
            base_dir=str(tmp_path),
            audio_input_dir="media/incoming",
            audio_quarantine_subdir="hold/quarantine",
            checkpoints_dir="state/checkpoints",
            outputs_dir="state/outputs",
        ),
        audio_quality={"enabled": False},
    )

    watcher = FolderWatcher(MagicMock(), config=config)

    assert watcher.watch_dir == tmp_path / "media" / "incoming"
    assert watcher._quarantine_dir == tmp_path / "hold" / "quarantine"
    assert watcher._checkpoints_dir == tmp_path / "state" / "checkpoints"
    assert watcher._outputs_dir == tmp_path / "state" / "outputs"


@pytest.mark.asyncio
async def test_watch_root_중간_component_symlink는_외부_파일을_검사하지_않음(
    tmp_path: Path,
    job_queue: AsyncJobQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """watch root의 중간 symlink를 따라 external file을 등록하지 않는다."""
    from config import AppConfig, PathsConfig
    from core.audio_quality import AudioQualityStatus

    base = tmp_path / "base"
    base.mkdir()
    external = tmp_path / "external"
    external_audio = external / "audio_input"
    external_audio.mkdir(parents=True)
    target = external_audio / "escape.wav"
    target.write_bytes(b"external")
    (base / "linked").symlink_to(external, target_is_directory=True)
    config = AppConfig(
        paths=PathsConfig(base_dir=str(base), audio_input_dir="linked/audio_input"),
        audio_quality={"enabled": False},
    )
    watcher = FolderWatcher(job_queue, config=config)
    validator = MagicMock(
        return_value=_quality_result(
            AudioQualityStatus.ACCEPT,
            failure_kind="media_invalid",
            quarantine_safe=False,
            reason="",
        )
    )
    watcher._audio_validator = validator

    await watcher._handle_new_file(target)

    validator.assert_not_called()
    assert await job_queue.get_all_jobs() == []
    assert target.read_bytes() == b"external"


@pytest.mark.asyncio
async def test_dotdot_stem_audio는_meeting_id로_등록하지_않음(
    watcher: FolderWatcher,
    job_queue: AsyncJobQueue,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`...wav`의 stem `..`를 DB/path meeting_id로 허용하지 않는다."""
    source = tmp_path / "audio_input" / "...wav"
    source.write_bytes(b"audio")
    monkeypatch.setattr(watcher, "_wait_for_stable_size", AsyncMock(return_value=True))

    await watcher._handle_new_file(source)

    assert await job_queue.get_all_jobs() == []
    assert source.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("admission", ["accept", "reject"])
async def test_validation_후_queue_quarantine_직전_writer를_다시_확인(
    watcher: FolderWatcher,
    job_queue: AsyncJobQueue,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    admission: str,
) -> None:
    """validation 후 writer가 다시 열리면 queue와 quarantine 모두 금지한다."""
    from core.audio_quality import AudioQualityStatus
    from core.watcher import _OpenWriterState

    source = tmp_path / "audio_input" / f"final-{admission}.wav"
    source.write_bytes(b"audio")
    status = AudioQualityStatus.ACCEPT if admission == "accept" else AudioQualityStatus.REJECT
    watcher._audio_validator = MagicMock(
        return_value=_quality_result(
            status,
            failure_kind="media_invalid",
            quarantine_safe=admission == "reject",
            reason="short" if admission == "reject" else "",
        )
    )
    monkeypatch.setattr(watcher, "_wait_for_stable_size", AsyncMock(return_value=True))
    probe = AsyncMock(return_value=_OpenWriterState.BUSY)
    monkeypatch.setattr(watcher, "_probe_writable_open", probe)

    await watcher._handle_new_file(source)

    probe.assert_awaited()
    assert source.exists()
    assert await job_queue.get_all_jobs() == []
    assert not (tmp_path / "audio_quarantine").exists()


@pytest.mark.asyncio
async def test_open_writer_timeout은_close_event_없어도_한번만_deferred_retry(
    watcher: FolderWatcher,
    job_queue: AsyncJobQueue,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """timeout 후 close-only 시나리오를 위해 1회만 유한 재검사한다."""
    from core.watcher import _OpenWriterState

    source = tmp_path / "audio_input" / "close-only.wav"
    source.write_bytes(b"audio")
    calls = 0

    async def _wait(path: Path) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            watcher._close_retry_paths.add(path)
            return False
        return True

    monkeypatch.setattr(watcher, "_wait_for_stable_size", _wait)
    monkeypatch.setattr(
        watcher,
        "_probe_writable_open",
        AsyncMock(return_value=_OpenWriterState.CLEAR),
    )
    await watcher._handle_new_file(source)

    for _ in range(50):
        row = await asyncio.to_thread(
            job_queue.queue.get_job_by_meeting_id,
            "close-only",
        )
        if row is not None:
            break
        await asyncio.sleep(0.01)

    assert calls == 2
    assert row is not None


@pytest.mark.asyncio
async def test_live_queued_job은_audit_중_recorded로_hold하고_ACCEPT시_의도를_복원(
    watcher: FolderWatcher,
    job_queue: AsyncJobQueue,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """live event의 기존 queued row도 검증하되 검증 중 자동 처리를 막는다."""
    from core.audio_quality import AudioQualityStatus
    from core.watcher import _OpenWriterState

    source = tmp_path / "audio_input" / "queued-audit.wav"
    source.write_bytes(b"audio")
    job_id = await job_queue.add_job("queued-audit", str(source), initial_status="recorded")
    await asyncio.to_thread(job_queue.queue.queue_job, job_id, "full")
    statuses_seen: list[str] = []

    def _validate(path: Path, **_kwargs: object) -> SimpleNamespace:
        row = job_queue.queue.get_job(job_id)
        statuses_seen.append(row.status)
        return _quality_result(
            AudioQualityStatus.ACCEPT,
            failure_kind="media_invalid",
            quarantine_safe=False,
            reason="",
        )

    watcher._audio_validator = _validate
    monkeypatch.setattr(watcher, "_wait_for_stable_size", AsyncMock(return_value=True))
    monkeypatch.setattr(
        watcher,
        "_probe_writable_open",
        AsyncMock(return_value=_OpenWriterState.CLEAR),
    )

    await watcher._handle_new_file(source)

    row = await asyncio.to_thread(job_queue.queue.get_job, job_id)
    assert statuses_seen == ["recorded"]
    assert row.status == "queued"
    assert row.requested_action == "full"


@pytest.mark.asyncio
async def test_scan_queued_job도_audit_중_hold하고_ACCEPT시_의도를_복원(
    watcher: FolderWatcher,
    job_queue: AsyncJobQueue,
    tmp_path: Path,
) -> None:
    """startup scan의 queued row도 검증 중 실행을 막고 원래 action을 복원한다."""
    from core.audio_quality import AudioQualityStatus

    source = tmp_path / "audio_input" / "scan-queued-audit.wav"
    source.write_bytes(b"audio")
    job_id = await job_queue.add_job(
        "scan-queued-audit",
        str(source),
        initial_status="recorded",
    )
    await asyncio.to_thread(job_queue.queue.queue_job, job_id, "transcribe")
    statuses_seen: list[str] = []

    def _validate(path: Path, **_kwargs: object) -> SimpleNamespace:
        statuses_seen.append(job_queue.queue.get_job(job_id).status)
        return _quality_result(
            AudioQualityStatus.ACCEPT,
            failure_kind="media_invalid",
            quarantine_safe=False,
            reason="",
        )

    watcher._audio_validator = _validate

    assert await watcher.scan_existing() == []

    row = await asyncio.to_thread(job_queue.queue.get_job, job_id)
    assert statuses_seen == ["recorded"]
    assert row.status == "queued"
    assert row.requested_action == "transcribe"


@pytest.mark.asyncio
async def test_audio_admission_hold는_재시작_후에도_queued_의도를_복원(
    watcher: FolderWatcher,
    job_queue: AsyncJobQueue,
    tmp_path: Path,
) -> None:
    """SOURCE_BUSY hold는 메모리가 사라져도 DB payload로 queued action을 복원한다."""
    from core.audio_quality import AudioQualityStatus
    from core.job_queue import parse_audio_admission_hold

    source = tmp_path / "audio_input" / "restart-queued.wav"
    source.write_bytes(b"audio")
    job_id = await job_queue.add_job(
        "restart-queued",
        str(source),
        initial_status="recorded",
    )
    await asyncio.to_thread(job_queue.queue.queue_job, job_id, "full")
    watcher._audio_validator = MagicMock(
        return_value=_quality_result(
            AudioQualityStatus.ERROR,
            failure_kind="source_busy",
            quarantine_safe=False,
            reason="writer state indeterminate",
        )
    )

    assert await watcher.scan_existing() == []
    held = await asyncio.to_thread(job_queue.queue.get_job, job_id)
    hold = parse_audio_admission_hold(held.requested_action)
    assert held.status == "recorded"
    assert hold is not None
    assert hold.original_status == "queued"
    assert hold.original_requested_action == "full"

    restarted = FolderWatcher(async_job_queue=job_queue, config=watcher._config)
    restarted._audio_validator = MagicMock(
        return_value=_quality_result(
            AudioQualityStatus.ACCEPT,
            failure_kind="media_invalid",
            quarantine_safe=False,
            reason="",
        )
    )
    try:
        assert await restarted.scan_existing() == []
    finally:
        await restarted.stop()

    restored = await asyncio.to_thread(job_queue.queue.get_job, job_id)
    assert restored.status == "queued"
    assert restored.requested_action == "full"


@pytest.mark.asyncio
async def test_audio_admission_hold는_failed_origin을_재시작_후_recorded로_정상화(
    watcher: FolderWatcher,
    job_queue: AsyncJobQueue,
    tmp_path: Path,
) -> None:
    """infra hold의 failed origin은 재감사 ACCEPT 후 failed UI 없이 recorded가 된다."""
    from core.audio_quality import AudioQualityStatus
    from core.job_queue import JobStatus, parse_audio_admission_hold

    source = tmp_path / "audio_input" / "restart-failed.wav"
    source.write_bytes(b"audio")
    job_id = await job_queue.add_job(
        "restart-failed",
        str(source),
        initial_status="recorded",
    )
    await asyncio.to_thread(
        job_queue.queue.force_set_status,
        job_id,
        JobStatus.FAILED,
        "old failure",
    )
    watcher._audio_validator = MagicMock(
        return_value=_quality_result(
            AudioQualityStatus.ERROR,
            failure_kind="infra_unavailable",
            quarantine_safe=False,
            reason="ffmpeg unavailable",
        )
    )

    assert await watcher.scan_existing() == []
    held = await asyncio.to_thread(job_queue.queue.get_job, job_id)
    hold = parse_audio_admission_hold(held.requested_action)
    assert held.status == "recorded"
    assert hold is not None
    assert hold.original_status == "failed"

    restarted = FolderWatcher(async_job_queue=job_queue, config=watcher._config)
    restarted._audio_validator = MagicMock(
        return_value=_quality_result(
            AudioQualityStatus.ACCEPT,
            failure_kind="media_invalid",
            quarantine_safe=False,
            reason="",
        )
    )
    try:
        assert await restarted.scan_existing() == []
    finally:
        await restarted.stop()

    restored = await asyncio.to_thread(job_queue.queue.get_job, job_id)
    assert restored.status == "recorded"
    assert restored.requested_action == ""
    assert restored.error_message == ""


@pytest.mark.asyncio
async def test_scan_artifact_allowlist에_없는_DSStore_inputwav는_자동정리를_막지_않음(
    watcher: FolderWatcher,
    job_queue: AsyncJobQueue,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """의미 없는 output 파일만으로 media-invalid legacy row를 과보존하지 않는다."""
    from core.audio_quality import AudioQualityStatus
    from core.watcher import _OpenWriterState

    meeting_id = "allowlist-cleanup"
    source = tmp_path / "audio_input" / f"{meeting_id}.wav"
    source.write_bytes(b"invalid")
    await job_queue.add_job(meeting_id, str(source), initial_status="recorded")
    output = tmp_path / "outputs" / meeting_id
    output.mkdir(parents=True)
    (output / ".DS_Store").write_bytes(b"metadata")
    (output / "input.wav").write_bytes(b"copy")
    watcher._audio_validator = MagicMock(
        return_value=_quality_result(
            AudioQualityStatus.REJECT,
            failure_kind="media_invalid",
            quarantine_safe=True,
            reason="short",
        )
    )
    monkeypatch.setattr(
        watcher,
        "_probe_writable_open",
        AsyncMock(return_value=_OpenWriterState.CLEAR),
    )

    await watcher.scan_existing()

    assert not source.exists()
    assert (
        await asyncio.to_thread(
            job_queue.queue.get_job_by_meeting_id,
            meeting_id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_scan_summary_json_산출물은_legacy_media_invalid_자동정리에서_보존(
    watcher: FolderWatcher,
    job_queue: AsyncJobQueue,
    tmp_path: Path,
) -> None:
    """API fallback 산출물인 summary.json만 있어도 원본과 row를 보존한다."""
    from core.audio_quality import AudioQualityStatus

    meeting_id = "summary-json-preserved"
    source = tmp_path / "audio_input" / f"{meeting_id}.wav"
    source.write_bytes(b"invalid")
    job_id = await job_queue.add_job(
        meeting_id,
        str(source),
        initial_status="recorded",
    )
    output = tmp_path / "outputs" / meeting_id
    output.mkdir(parents=True)
    (output / "summary.json").write_text('{"summary": "기존 요약"}', encoding="utf-8")
    watcher._audio_validator = MagicMock(
        return_value=_quality_result(
            AudioQualityStatus.REJECT,
            failure_kind="media_invalid",
            quarantine_safe=True,
            reason="short",
        )
    )

    assert await watcher.scan_existing() == []

    assert source.exists()
    assert await asyncio.to_thread(job_queue.queue.get_job, job_id) is not None
    watcher._audio_validator.assert_not_called()


@pytest.mark.asyncio
async def test_zero_byte_closed는_timeout_대기_없이_즉시_검증·격리(
    watcher: FolderWatcher,
    job_queue: AsyncJobQueue,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """writer가 없는 0-byte를 file_ready_timeout 동안 대기하지 않는다."""
    from core.audio_quality import AudioQualityStatus
    from core.watcher import _OpenWriterState

    source = tmp_path / "audio_input" / "zero.wav"
    source.touch()
    watcher._file_ready_timeout_seconds = 5.0
    watcher._audio_validator = MagicMock(
        return_value=_quality_result(
            AudioQualityStatus.REJECT,
            failure_kind="media_invalid",
            quarantine_safe=True,
            reason="zero-byte",
        )
    )
    monkeypatch.setattr(
        watcher,
        "_probe_writable_open",
        AsyncMock(return_value=_OpenWriterState.CLEAR),
    )
    readiness_sleep = AsyncMock(
        side_effect=AssertionError("closed 0-byte readiness는 sleep하지 않아야 합니다")
    )
    monkeypatch.setattr("core.watcher.asyncio.sleep", readiness_sleep)

    await asyncio.wait_for(watcher._handle_new_file(source), timeout=3.0)

    readiness_sleep.assert_not_awaited()
    assert not source.exists()
    assert await job_queue.get_all_jobs() == []
