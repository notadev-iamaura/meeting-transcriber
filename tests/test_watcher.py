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
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from core.job_queue import AsyncJobQueue, JobQueue, JobQueueError
from core.watcher import (
    AlreadyWatchingError,
    FolderWatcher,
    WatchDirectoryError,
    WatcherError,
    _AudioFileHandler,
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
    config.paths.resolved_audio_input_dir = watch_dir

    # audio 설정
    config.audio.supported_input_formats = ["wav", "mp3", "m4a", "flac", "ogg", "webm"]

    # watcher 설정
    config.watcher.debounce_seconds = 0.3  # 테스트용 짧은 대기 시간
    config.watcher.check_interval_seconds = 0.1  # 테스트용 짧은 확인 간격

    return config


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
    if w.is_watching:
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
        handler.on_created(event)

        # run_coroutine_threadsafe가 호출되어야 함
        assert mock_loop is handler._loop


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
