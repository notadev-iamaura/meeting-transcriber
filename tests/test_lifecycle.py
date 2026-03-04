"""
데이터 라이프사이클 관리 모듈 테스트 (Data Lifecycle Manager Tests)

목적: security/lifecycle.py의 모든 기능을 검증한다.
주요 테스트:
  - 데이터 등급 분류 (Hot/Warm/Cold)
  - WAV → FLAC 압축 (ffmpeg 모킹)
  - Cold 정책 (오디오 삭제, 아카이브 스텁)
  - 멱등성 (이미 처리된 회의 스킵)
  - 원자성 (변환 실패 시 원본 보존)
  - 빈 디렉토리, state 파일 없음 등 엣지 케이스
  - scan_meetings 정렬 및 필터링
  - 편의 함수 (run_lifecycle)
의존성: pytest, config 모듈
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from config import AppConfig, PathsConfig, LifecycleConfig
from security.lifecycle import (
    LifecycleManager,
    LifecycleResult,
    MeetingInfo,
    DataTier,
    ColdAction,
    LifecycleError,
    CompressionError,
    DeletionError,
    run_lifecycle,
    _MEETING_ID_PATTERN,
    _AUDIO_EXTENSIONS,
)


# === 픽스처 (Fixtures) ===


@pytest.fixture
def base_dir(tmp_path: Path) -> Path:
    """테스트용 기본 데이터 디렉토리 경로."""
    return tmp_path / "meeting-data"


@pytest.fixture
def outputs_dir(base_dir: Path) -> Path:
    """테스트용 outputs 디렉토리."""
    d = base_dir / "outputs"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def mock_config(base_dir: Path) -> AppConfig:
    """테스트용 AppConfig 인스턴스.

    tmp_path 기반 경로를 사용하여 실제 파일시스템에 영향 없이 테스트한다.
    """
    config = AppConfig(
        paths=PathsConfig(
            base_dir=str(base_dir),
            outputs_dir="outputs",
        ),
        lifecycle=LifecycleConfig(
            hot_days=30,
            warm_days=90,
            cold_action="delete_audio",
        ),
    )
    return config


@pytest.fixture
def now() -> datetime:
    """테스트 기준 시각."""
    return datetime(2026, 3, 4, 12, 0, 0)


@pytest.fixture
def manager(mock_config: AppConfig, outputs_dir: Path, now: datetime) -> LifecycleManager:
    """테스트용 LifecycleManager 인스턴스."""
    return LifecycleManager(mock_config, now=now)


def _create_meeting(
    outputs_dir: Path,
    meeting_id: str,
    created_at: datetime,
    has_wav: bool = True,
    has_flac: bool = False,
    wav_size: int = 1000,
    flac_size: int = 500,
    extra_files: list[str] | None = None,
) -> Path:
    """테스트용 회의 디렉토리를 생성하는 헬퍼 함수.

    Args:
        outputs_dir: outputs 디렉토리 경로
        meeting_id: 회의 고유 식별자
        created_at: 생성 시각
        has_wav: WAV 파일 생성 여부
        has_flac: FLAC 파일 생성 여부
        wav_size: WAV 파일 크기 (bytes)
        flac_size: FLAC 파일 크기 (bytes)
        extra_files: 추가 생성할 파일명 목록

    Returns:
        생성된 회의 디렉토리 경로
    """
    meeting_dir = outputs_dir / meeting_id
    meeting_dir.mkdir(parents=True, exist_ok=True)

    # pipeline_state.json 생성
    state = {
        "meeting_id": meeting_id,
        "created_at": created_at.isoformat(),
        "status": "completed",
    }
    state_path = meeting_dir / "pipeline_state.json"
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    # 오디오 파일 생성
    if has_wav:
        wav_file = meeting_dir / "audio.wav"
        wav_file.write_bytes(b"\x00" * wav_size)

    if has_flac:
        flac_file = meeting_dir / "audio.flac"
        flac_file.write_bytes(b"\x00" * flac_size)

    # 메타데이터 파일 (항상 생성)
    (meeting_dir / "corrected.json").write_text(
        '{"utterances": []}', encoding="utf-8"
    )
    (meeting_dir / "summary.md").write_text(
        "# 회의록\n내용", encoding="utf-8"
    )

    # 추가 파일
    if extra_files:
        for fname in extra_files:
            (meeting_dir / fname).write_bytes(b"\x00" * 100)

    return meeting_dir


# === DataTier 분류 테스트 ===


class TestClassifyTier:
    """데이터 등급 분류 함수를 검증한다."""

    def test_hot_tier_day_0(self, manager: LifecycleManager) -> None:
        """당일 생성 데이터는 Hot 등급이다."""
        assert manager.classify_tier(0) == DataTier.HOT

    def test_hot_tier_day_29(self, manager: LifecycleManager) -> None:
        """29일된 데이터는 아직 Hot 등급이다."""
        assert manager.classify_tier(29) == DataTier.HOT

    def test_warm_tier_day_30(self, manager: LifecycleManager) -> None:
        """30일된 데이터는 Warm 등급으로 전환된다."""
        assert manager.classify_tier(30) == DataTier.WARM

    def test_warm_tier_day_89(self, manager: LifecycleManager) -> None:
        """89일된 데이터는 아직 Warm 등급이다."""
        assert manager.classify_tier(89) == DataTier.WARM

    def test_cold_tier_day_90(self, manager: LifecycleManager) -> None:
        """90일된 데이터는 Cold 등급으로 전환된다."""
        assert manager.classify_tier(90) == DataTier.COLD

    def test_cold_tier_day_365(self, manager: LifecycleManager) -> None:
        """1년된 데이터는 Cold 등급이다."""
        assert manager.classify_tier(365) == DataTier.COLD


# === scan_meetings 테스트 ===


class TestScanMeetings:
    """회의 스캔 기능을 검증한다."""

    def test_empty_outputs_dir(self, manager: LifecycleManager) -> None:
        """빈 outputs 디렉토리에서는 빈 목록을 반환한다."""
        meetings = manager.scan_meetings()
        assert meetings == []

    def test_outputs_dir_not_exists(
        self, mock_config: AppConfig, now: datetime
    ) -> None:
        """outputs 디렉토리가 없으면 빈 목록을 반환한다."""
        # outputs_dir를 생성하지 않은 config 사용
        config = AppConfig(
            paths=PathsConfig(base_dir="/tmp/nonexistent-lifecycle-test"),
            lifecycle=LifecycleConfig(hot_days=30, warm_days=90),
        )
        mgr = LifecycleManager(config, now=now)
        assert mgr.scan_meetings() == []

    def test_scan_single_meeting(
        self, manager: LifecycleManager, outputs_dir: Path, now: datetime
    ) -> None:
        """단일 회의를 정상적으로 스캔한다."""
        created = now - timedelta(days=15)
        _create_meeting(outputs_dir, "meeting-001", created)

        meetings = manager.scan_meetings()
        assert len(meetings) == 1
        assert meetings[0].meeting_id == "meeting-001"
        assert meetings[0].tier == DataTier.HOT
        assert meetings[0].has_wav is True
        assert meetings[0].age_days == 15

    def test_scan_sorted_by_age_descending(
        self, manager: LifecycleManager, outputs_dir: Path, now: datetime
    ) -> None:
        """스캔 결과는 나이 기준 내림차순으로 정렬된다."""
        _create_meeting(outputs_dir, "new-meeting", now - timedelta(days=5))
        _create_meeting(outputs_dir, "old-meeting", now - timedelta(days=100))
        _create_meeting(outputs_dir, "mid-meeting", now - timedelta(days=50))

        meetings = manager.scan_meetings()
        ages = [m.age_days for m in meetings]
        assert ages == sorted(ages, reverse=True)
        assert meetings[0].meeting_id == "old-meeting"
        assert meetings[2].meeting_id == "new-meeting"

    def test_skip_invalid_meeting_id(
        self, manager: LifecycleManager, outputs_dir: Path, now: datetime
    ) -> None:
        """유효하지 않은 meeting_id(path traversal 등)는 스킵한다."""
        # 정상 회의
        _create_meeting(outputs_dir, "valid-meeting", now - timedelta(days=10))

        # 비정상 디렉토리 (path traversal 시도)
        bad_dir = outputs_dir / ".." / "escape"
        bad_dir.mkdir(parents=True, exist_ok=True)

        meetings = manager.scan_meetings()
        ids = [m.meeting_id for m in meetings]
        assert "valid-meeting" in ids
        # ".." 같은 디렉토리는 필터링됨

    def test_skip_non_directory_entries(
        self, manager: LifecycleManager, outputs_dir: Path, now: datetime
    ) -> None:
        """파일은 스캔에서 제외한다."""
        _create_meeting(outputs_dir, "real-meeting", now - timedelta(days=5))

        # outputs 디렉토리에 파일 생성 (디렉토리가 아닌 항목)
        (outputs_dir / "some-file.txt").write_text("hello")

        meetings = manager.scan_meetings()
        assert len(meetings) == 1
        assert meetings[0].meeting_id == "real-meeting"

    def test_fallback_to_mtime_when_no_state_file(
        self, mock_config: AppConfig, outputs_dir: Path
    ) -> None:
        """pipeline_state.json이 없으면 디렉토리 mtime을 사용한다."""
        meeting_dir = outputs_dir / "no-state-meeting"
        meeting_dir.mkdir()
        (meeting_dir / "audio.wav").write_bytes(b"\x00" * 100)

        # now를 None으로 주면 실제 시각 사용 → mtime과 동일한 날
        mgr = LifecycleManager(mock_config, now=None)
        meetings = mgr.scan_meetings()
        assert len(meetings) == 1
        assert meetings[0].meeting_id == "no-state-meeting"
        # 방금 생성했으므로 age_days == 0
        assert meetings[0].age_days == 0

    def test_fallback_on_invalid_state_json(
        self, manager: LifecycleManager, outputs_dir: Path
    ) -> None:
        """pipeline_state.json이 잘못된 JSON이면 mtime 폴백한다."""
        meeting_dir = outputs_dir / "bad-json-meeting"
        meeting_dir.mkdir()
        (meeting_dir / "pipeline_state.json").write_text(
            "not valid json!", encoding="utf-8"
        )
        (meeting_dir / "audio.wav").write_bytes(b"\x00" * 100)

        meetings = manager.scan_meetings()
        assert len(meetings) == 1

    def test_audio_file_detection(
        self, manager: LifecycleManager, outputs_dir: Path, now: datetime
    ) -> None:
        """WAV/FLAC 파일 존재 여부를 정확히 감지한다."""
        _create_meeting(
            outputs_dir, "both-audio", now - timedelta(days=10),
            has_wav=True, has_flac=True,
        )

        meetings = manager.scan_meetings()
        assert meetings[0].has_wav is True
        assert meetings[0].has_flac is True
        assert len(meetings[0].audio_files) == 2


# === compress_to_flac 테스트 ===


class TestCompressToFlac:
    """FLAC 압축 기능을 검증한다."""

    @patch("security.lifecycle.subprocess.run")
    def test_successful_compression(
        self, mock_run: MagicMock, manager: LifecycleManager, outputs_dir: Path
    ) -> None:
        """WAV → FLAC 변환이 성공적으로 수행된다."""
        meeting_dir = outputs_dir / "test-meeting"
        meeting_dir.mkdir()
        wav_path = meeting_dir / "audio.wav"
        wav_path.write_bytes(b"\x00" * 1000)
        flac_path = meeting_dir / "audio.flac"

        # ffmpeg 성공 시뮬레이션
        def fake_ffmpeg(*args, **kwargs):
            flac_path.write_bytes(b"\x00" * 500)
            result = MagicMock()
            result.returncode = 0
            return result

        mock_run.side_effect = fake_ffmpeg

        result = manager.compress_to_flac(wav_path)

        assert result == flac_path
        assert flac_path.exists()
        assert not wav_path.exists()  # 원본 삭제됨
        mock_run.assert_called_once()

    def test_skip_when_flac_exists(
        self, manager: LifecycleManager, outputs_dir: Path
    ) -> None:
        """FLAC이 이미 존재하면 스킵한다 (멱등성)."""
        meeting_dir = outputs_dir / "already-compressed"
        meeting_dir.mkdir()
        wav_path = meeting_dir / "audio.wav"
        wav_path.write_bytes(b"\x00" * 1000)
        flac_path = meeting_dir / "audio.flac"
        flac_path.write_bytes(b"\x00" * 500)

        result = manager.compress_to_flac(wav_path)

        assert result == flac_path
        # WAV가 삭제되어야 함
        assert not wav_path.exists()

    def test_skip_when_flac_exists_and_wav_gone(
        self, manager: LifecycleManager, outputs_dir: Path
    ) -> None:
        """FLAC 존재 + WAV 없는 경우도 정상 처리된다."""
        meeting_dir = outputs_dir / "only-flac"
        meeting_dir.mkdir()
        wav_path = meeting_dir / "audio.wav"  # 존재하지 않음
        flac_path = meeting_dir / "audio.flac"
        flac_path.write_bytes(b"\x00" * 500)

        result = manager.compress_to_flac(wav_path)
        assert result == flac_path

    def test_error_when_wav_not_found(
        self, manager: LifecycleManager, outputs_dir: Path
    ) -> None:
        """WAV 파일이 없으면 CompressionError가 발생한다."""
        wav_path = outputs_dir / "nonexistent" / "audio.wav"

        with pytest.raises(CompressionError, match="WAV 파일 없음"):
            manager.compress_to_flac(wav_path)

    @patch("security.lifecycle.subprocess.run")
    def test_error_on_ffmpeg_failure(
        self, mock_run: MagicMock, manager: LifecycleManager, outputs_dir: Path
    ) -> None:
        """ffmpeg 실패 시 CompressionError가 발생하고 원본은 보존된다."""
        meeting_dir = outputs_dir / "ffmpeg-fail"
        meeting_dir.mkdir()
        wav_path = meeting_dir / "audio.wav"
        wav_path.write_bytes(b"\x00" * 1000)

        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="ffmpeg error",
        )

        with pytest.raises(CompressionError, match="ffmpeg FLAC 변환 실패"):
            manager.compress_to_flac(wav_path)

        # 원본 보존 확인
        assert wav_path.exists()

    @patch("security.lifecycle.subprocess.run", side_effect=FileNotFoundError)
    def test_error_when_ffmpeg_not_installed(
        self, mock_run: MagicMock, manager: LifecycleManager, outputs_dir: Path
    ) -> None:
        """ffmpeg이 설치되지 않았으면 CompressionError가 발생한다."""
        meeting_dir = outputs_dir / "no-ffmpeg"
        meeting_dir.mkdir()
        wav_path = meeting_dir / "audio.wav"
        wav_path.write_bytes(b"\x00" * 1000)

        with pytest.raises(CompressionError, match="ffmpeg이 설치되어 있지 않습니다"):
            manager.compress_to_flac(wav_path)

        # 원본 보존 확인
        assert wav_path.exists()

    @patch(
        "security.lifecycle.subprocess.run",
        side_effect=__import__("subprocess").TimeoutExpired(cmd="ffmpeg", timeout=300),
    )
    def test_error_on_ffmpeg_timeout(
        self, mock_run: MagicMock, manager: LifecycleManager, outputs_dir: Path
    ) -> None:
        """ffmpeg 타임아웃 시 CompressionError가 발생한다."""
        meeting_dir = outputs_dir / "timeout"
        meeting_dir.mkdir()
        wav_path = meeting_dir / "audio.wav"
        wav_path.write_bytes(b"\x00" * 1000)

        with pytest.raises(CompressionError, match="타임아웃"):
            manager.compress_to_flac(wav_path)

        # 원본 보존 확인
        assert wav_path.exists()

    @patch("security.lifecycle.subprocess.run")
    def test_cleanup_incomplete_flac_on_failure(
        self, mock_run: MagicMock, manager: LifecycleManager, outputs_dir: Path
    ) -> None:
        """ffmpeg 실패 시 불완전한 FLAC 파일이 정리된다."""
        meeting_dir = outputs_dir / "cleanup-test"
        meeting_dir.mkdir()
        wav_path = meeting_dir / "audio.wav"
        wav_path.write_bytes(b"\x00" * 1000)
        flac_path = meeting_dir / "audio.flac"

        # 불완전한 FLAC 파일이 생성되는 시뮬레이션
        def fake_ffmpeg_partial(*args, **kwargs):
            flac_path.write_bytes(b"\x00" * 10)  # 불완전한 파일
            result = MagicMock()
            result.returncode = 1
            result.stderr = "error"
            return result

        mock_run.side_effect = fake_ffmpeg_partial

        with pytest.raises(CompressionError):
            manager.compress_to_flac(wav_path)

        # 불완전한 FLAC 파일이 정리되었는지 확인
        assert not flac_path.exists()
        assert wav_path.exists()  # 원본 보존


# === apply_cold_policy 테스트 ===


class TestApplyColdPolicy:
    """Cold 정책 적용 기능을 검증한다."""

    def test_delete_audio_files(
        self, manager: LifecycleManager, outputs_dir: Path, now: datetime
    ) -> None:
        """delete_audio 정책으로 오디오 파일만 삭제한다."""
        meeting_dir = _create_meeting(
            outputs_dir, "cold-meeting", now - timedelta(days=100),
            has_wav=False, has_flac=True, flac_size=500,
        )

        info = MeetingInfo(
            meeting_id="cold-meeting",
            meeting_dir=meeting_dir,
            created_at=now - timedelta(days=100),
            age_days=100,
            tier=DataTier.COLD,
            has_flac=True,
            audio_files=[meeting_dir / "audio.flac"],
        )

        freed = manager.apply_cold_policy(info)

        assert freed == 500
        assert not (meeting_dir / "audio.flac").exists()
        # 메타데이터는 보존
        assert (meeting_dir / "corrected.json").exists()
        assert (meeting_dir / "summary.md").exists()

    def test_delete_multiple_audio_files(
        self, manager: LifecycleManager, outputs_dir: Path, now: datetime
    ) -> None:
        """여러 오디오 파일이 있으면 모두 삭제한다."""
        meeting_dir = _create_meeting(
            outputs_dir, "multi-audio", now - timedelta(days=100),
            has_wav=True, has_flac=True,
            extra_files=["backup.mp3"],
        )

        info = MeetingInfo(
            meeting_id="multi-audio",
            meeting_dir=meeting_dir,
            created_at=now - timedelta(days=100),
            age_days=100,
            tier=DataTier.COLD,
        )

        freed = manager.apply_cold_policy(info)

        assert freed > 0
        # 오디오 파일 모두 삭제됨
        audio_exts = {f.suffix.lower() for f in meeting_dir.iterdir() if f.is_file()}
        assert not audio_exts.intersection({".wav", ".flac", ".mp3"})

    def test_no_audio_to_delete(
        self, manager: LifecycleManager, outputs_dir: Path, now: datetime
    ) -> None:
        """삭제할 오디오 파일이 없으면 0을 반환한다."""
        meeting_dir = _create_meeting(
            outputs_dir, "no-audio", now - timedelta(days=100),
            has_wav=False, has_flac=False,
        )

        info = MeetingInfo(
            meeting_id="no-audio",
            meeting_dir=meeting_dir,
            created_at=now - timedelta(days=100),
            age_days=100,
            tier=DataTier.COLD,
        )

        freed = manager.apply_cold_policy(info)
        assert freed == 0

    def test_archive_policy_stub(
        self, mock_config: AppConfig, outputs_dir: Path, now: datetime
    ) -> None:
        """archive 정책은 현재 미구현이므로 0을 반환한다."""
        config = AppConfig(
            paths=mock_config.paths,
            lifecycle=LifecycleConfig(
                hot_days=30, warm_days=90, cold_action="archive"
            ),
        )
        mgr = LifecycleManager(config, now=now)

        meeting_dir = outputs_dir / "archive-test"
        meeting_dir.mkdir()

        info = MeetingInfo(
            meeting_id="archive-test",
            meeting_dir=meeting_dir,
            created_at=now - timedelta(days=100),
            age_days=100,
            tier=DataTier.COLD,
        )

        freed = mgr.apply_cold_policy(info)
        assert freed == 0


# === run (전체 실행) 테스트 ===


class TestRun:
    """전체 라이프사이클 관리 실행을 검증한다."""

    def test_empty_outputs(self, manager: LifecycleManager) -> None:
        """빈 outputs 디렉토리에서는 모든 카운터가 0이다."""
        result = manager.run()
        assert result.total_scanned == 0
        assert result.compressed == 0
        assert result.deleted == 0
        assert result.skipped == 0

    def test_hot_meetings_skipped(
        self, manager: LifecycleManager, outputs_dir: Path, now: datetime
    ) -> None:
        """Hot 등급 회의는 스킵된다."""
        _create_meeting(outputs_dir, "hot-1", now - timedelta(days=5))
        _create_meeting(outputs_dir, "hot-2", now - timedelta(days=20))

        result = manager.run()
        assert result.total_scanned == 2
        assert result.skipped == 2
        assert result.compressed == 0
        assert result.deleted == 0

    @patch("security.lifecycle.subprocess.run")
    def test_warm_meetings_compressed(
        self, mock_run: MagicMock,
        manager: LifecycleManager, outputs_dir: Path, now: datetime
    ) -> None:
        """Warm 등급 회의의 WAV는 FLAC으로 압축된다."""
        _create_meeting(
            outputs_dir, "warm-meeting", now - timedelta(days=45),
            has_wav=True, wav_size=2000,
        )

        # ffmpeg 성공 시뮬레이션
        def fake_ffmpeg(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            # ffmpeg 출력 경로에서 FLAC 파일 생성
            for i, arg in enumerate(cmd):
                if arg.endswith(".flac"):
                    Path(arg).write_bytes(b"\x00" * 1000)
                    break
            result_mock = MagicMock()
            result_mock.returncode = 0
            return result_mock

        mock_run.side_effect = fake_ffmpeg

        result = manager.run()
        assert result.total_scanned == 1
        assert result.compressed == 1

    def test_warm_meeting_already_compressed(
        self, manager: LifecycleManager, outputs_dir: Path, now: datetime
    ) -> None:
        """이미 FLAC만 있는 Warm 회의는 스킵된다."""
        _create_meeting(
            outputs_dir, "already-warm", now - timedelta(days=45),
            has_wav=False, has_flac=True,
        )

        result = manager.run()
        assert result.total_scanned == 1
        assert result.skipped == 1
        assert result.compressed == 0

    @patch("security.lifecycle.subprocess.run")
    def test_cold_meetings_deleted(
        self, mock_run: MagicMock,
        manager: LifecycleManager, outputs_dir: Path, now: datetime
    ) -> None:
        """Cold 등급 회의의 오디오 파일이 삭제된다."""
        _create_meeting(
            outputs_dir, "cold-meeting", now - timedelta(days=120),
            has_wav=True, wav_size=3000,
        )

        # WAV → FLAC 변환 시뮬레이션 (Cold 처리 시 먼저 압축)
        def fake_ffmpeg(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            for arg in cmd:
                if arg.endswith(".flac"):
                    Path(arg).write_bytes(b"\x00" * 1500)
                    break
            result_mock = MagicMock()
            result_mock.returncode = 0
            return result_mock

        mock_run.side_effect = fake_ffmpeg

        result = manager.run()
        assert result.total_scanned == 1
        assert result.deleted == 1
        assert result.bytes_saved > 0

        # 메타데이터는 보존됨
        meeting_dir = outputs_dir / "cold-meeting"
        assert (meeting_dir / "corrected.json").exists()
        assert (meeting_dir / "summary.md").exists()

    @patch("security.lifecycle.subprocess.run")
    def test_mixed_tiers(
        self, mock_run: MagicMock,
        manager: LifecycleManager, outputs_dir: Path, now: datetime
    ) -> None:
        """Hot/Warm/Cold 혼합 상황에서 각각 적절히 처리된다."""
        _create_meeting(outputs_dir, "hot", now - timedelta(days=5))
        _create_meeting(
            outputs_dir, "warm", now - timedelta(days=50),
            has_wav=True, wav_size=2000,
        )
        _create_meeting(
            outputs_dir, "cold", now - timedelta(days=120),
            has_wav=False, has_flac=True, flac_size=1000,
        )

        # Warm 회의의 ffmpeg 변환
        def fake_ffmpeg(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            for arg in cmd:
                if arg.endswith(".flac"):
                    Path(arg).write_bytes(b"\x00" * 1000)
                    break
            result_mock = MagicMock()
            result_mock.returncode = 0
            return result_mock

        mock_run.side_effect = fake_ffmpeg

        result = manager.run()
        assert result.total_scanned == 3
        assert result.skipped == 1    # hot
        assert result.compressed == 1  # warm
        assert result.deleted == 1     # cold

    @patch("security.lifecycle.subprocess.run")
    def test_compression_error_counted(
        self, mock_run: MagicMock,
        manager: LifecycleManager, outputs_dir: Path, now: datetime
    ) -> None:
        """압축 실패 시 에러가 기록된다."""
        _create_meeting(
            outputs_dir, "fail-meeting", now - timedelta(days=50),
            has_wav=True,
        )

        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="ffmpeg error details",
        )

        result = manager.run()
        assert len(result.errors) == 1
        assert result.errors[0][0] == "fail-meeting"


# === get_summary 테스트 ===


class TestGetSummary:
    """등급별 요약 기능을 검증한다."""

    def test_empty_summary(self, manager: LifecycleManager) -> None:
        """빈 디렉토리에서는 모든 카운트가 0이다."""
        summary = manager.get_summary()
        assert summary == {"hot": 0, "warm": 0, "cold": 0, "total": 0}

    def test_mixed_summary(
        self, manager: LifecycleManager, outputs_dir: Path, now: datetime
    ) -> None:
        """혼합 데이터에서 정확한 카운트를 반환한다."""
        _create_meeting(outputs_dir, "h1", now - timedelta(days=5))
        _create_meeting(outputs_dir, "h2", now - timedelta(days=10))
        _create_meeting(outputs_dir, "w1", now - timedelta(days=50))
        _create_meeting(outputs_dir, "c1", now - timedelta(days=120))

        summary = manager.get_summary()
        assert summary["hot"] == 2
        assert summary["warm"] == 1
        assert summary["cold"] == 1
        assert summary["total"] == 4


# === run_async 테스트 ===


class TestRunAsync:
    """비동기 실행을 검증한다."""

    def test_async_run_returns_result(
        self, manager: LifecycleManager, outputs_dir: Path, now: datetime
    ) -> None:
        """비동기 실행이 정상적으로 결과를 반환한다."""
        _create_meeting(outputs_dir, "async-test", now - timedelta(days=5))

        result = asyncio.get_event_loop().run_until_complete(manager.run_async())
        assert isinstance(result, LifecycleResult)
        assert result.total_scanned == 1


# === 편의 함수 테스트 ===


class TestRunLifecycle:
    """편의 함수 run_lifecycle을 검증한다."""

    def test_with_config(self, mock_config: AppConfig, outputs_dir: Path) -> None:
        """config를 전달하면 해당 설정으로 실행된다."""
        result = run_lifecycle(mock_config)
        assert isinstance(result, LifecycleResult)

    @patch("config.get_config")
    def test_without_config(self, mock_get_config: MagicMock, tmp_path: Path) -> None:
        """config가 None이면 싱글턴에서 가져온다."""
        config = AppConfig(
            paths=PathsConfig(base_dir=str(tmp_path)),
        )
        mock_get_config.return_value = config

        result = run_lifecycle()
        assert isinstance(result, LifecycleResult)
        mock_get_config.assert_called_once()


# === meeting_id 유효성 검증 테스트 ===


class TestMeetingIdValidation:
    """meeting_id 정규식 검증을 테스트한다."""

    @pytest.mark.parametrize("valid_id", [
        "meeting-001",
        "2026-03-04_zoom",
        "test.meeting.123",
        "simple",
        "with_underscore",
    ])
    def test_valid_meeting_ids(self, valid_id: str) -> None:
        """유효한 meeting_id 패턴을 통과한다."""
        assert _MEETING_ID_PATTERN.match(valid_id) is not None

    @pytest.mark.parametrize("invalid_id", [
        "../escape",
        "path/traversal",
        "space name",
        "",
    ])
    def test_invalid_meeting_ids(self, invalid_id: str) -> None:
        """유효하지 않은 meeting_id 패턴을 거부한다."""
        assert _MEETING_ID_PATTERN.match(invalid_id) is None


# === DataTier / ColdAction enum 테스트 ===


class TestEnums:
    """열거형 값을 검증한다."""

    def test_data_tier_values(self) -> None:
        """DataTier 열거형 값을 확인한다."""
        assert DataTier.HOT.value == "hot"
        assert DataTier.WARM.value == "warm"
        assert DataTier.COLD.value == "cold"

    def test_cold_action_values(self) -> None:
        """ColdAction 열거형 값을 확인한다."""
        assert ColdAction.DELETE_AUDIO.value == "delete_audio"
        assert ColdAction.ARCHIVE.value == "archive"

    def test_error_hierarchy(self) -> None:
        """에러 계층 구조를 확인한다."""
        assert issubclass(CompressionError, LifecycleError)
        assert issubclass(DeletionError, LifecycleError)


# === outputs_dir 프로퍼티 테스트 ===


class TestProperties:
    """프로퍼티 접근을 검증한다."""

    def test_outputs_dir_property(
        self, manager: LifecycleManager, outputs_dir: Path
    ) -> None:
        """outputs_dir 프로퍼티가 올바른 경로를 반환한다."""
        assert manager.outputs_dir == outputs_dir
