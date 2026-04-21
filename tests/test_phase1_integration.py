"""Phase 1 통합 시나리오: 크래시 방지 다층 방어 협력 검증.

Phase 1 의 5개 방어막이 실제 유스케이스에서 협력하는지 end-to-end 검증:
1. 오디오 품질 게이트 (core/audio_quality.py)
2. 재시도 정책 (core/retry_policy.py)
3. Quarantine (core/quarantine.py)
4. 동적 타임아웃 (core/pipeline.compute_dynamic_timeout)
5. Watcher 통합 (core/watcher.py)

각 방어막의 개별 테스트는 해당 모듈의 test_*.py 에 있고,
이 파일은 **시나리오 레벨 협력**을 검증한다.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


# === 시나리오 1: 저볼륨 파일 (이번 크래시의 실제 트리거) ===


def test_저볼륨_파일_시나리오_end_to_end(tmp_path: Path):
    """저볼륨 파일이 검증→거부→격리 전체 경로를 통과한다.

    재현 수치: meeting_20260420_100536.wav
    - mean_volume=-48.6dB, duration=1359.87s (22분)
    """
    from core.audio_quality import (
        AudioQualityStatus,
        validate_audio_quality,
    )
    from core.quarantine import move_to_quarantine

    audio_input = tmp_path / "audio_input"
    audio_input.mkdir()
    quarantine = tmp_path / "audio_quarantine"

    bad_file = audio_input / "meeting_crash.wav"
    bad_file.write_bytes(b"fake audio")

    # 오디오 측정 (실제 ffmpeg 호출 대신 mock)
    with (
        patch("core.audio_quality._measure_mean_volume_db", return_value=-48.6),
        patch("core.audio_quality._measure_duration_seconds", return_value=1359.87),
    ):
        result = validate_audio_quality(
            bad_file,
            min_mean_db=-40.0,
            min_duration_s=5.0,
        )

    assert result.status == AudioQualityStatus.REJECT
    assert result.mean_volume_db == -48.6
    assert result.duration_seconds == 1359.87
    assert "저볼륨" in result.reason or "볼륨" in result.reason

    # 거부된 파일을 격리실로 이동
    dest = move_to_quarantine(bad_file, quarantine, reason=result.reason)

    assert not bad_file.exists()
    assert dest.exists()
    assert dest.parent == quarantine


# === 시나리오 2: 타임아웃 재시도 차단 ===


def test_타임아웃_시나리오_재시도_차단():
    """전사 타임아웃 발생 시 재시도가 즉시 차단된다."""
    from core.retry_policy import (
        NonRetryableError,
        RetryableError,
        TranscriptionTimeoutError,
        should_retry,
    )

    # 타임아웃은 NonRetryableError 하위
    timeout_err = TranscriptionTimeoutError("1800초 초과")
    assert isinstance(timeout_err, NonRetryableError)

    # 첫 번째 시도에서도 재시도 금지
    assert should_retry(timeout_err, attempt=1, max_attempts=3) is False
    assert should_retry(timeout_err, attempt=2, max_attempts=3) is False

    # 대조: RetryableError는 재시도 허용
    retry_err = RetryableError("일시적 오류")
    assert should_retry(retry_err, attempt=1, max_attempts=3) is True


# === 시나리오 3: 동적 타임아웃 경계 ===


def test_동적_타임아웃_경계값_시나리오():
    """다양한 오디오 길이에 대한 타임아웃 계산."""
    from core.pipeline import compute_dynamic_timeout

    # 15분 오디오 × 3 = 45분 (하한 10분 이상, 상한 3시간 이하)
    assert compute_dynamic_timeout(
        duration_seconds=900.0,
        multiplier=3.0,
        min_seconds=600,
        max_seconds=10800,
    ) == 2700

    # 1시간 오디오 × 3 = 3시간 (상한 정확히 도달)
    assert compute_dynamic_timeout(
        duration_seconds=3600.0,
        multiplier=3.0,
        min_seconds=600,
        max_seconds=10800,
    ) == 10800

    # 3시간 오디오 × 3 = 9시간 → 상한 3시간으로 절단
    assert compute_dynamic_timeout(
        duration_seconds=10800.0,
        multiplier=3.0,
        min_seconds=600,
        max_seconds=10800,
    ) == 10800

    # 30초 오디오 × 3 = 90s → 하한 600s로 상승
    assert compute_dynamic_timeout(
        duration_seconds=30.0,
        multiplier=3.0,
        min_seconds=600,
        max_seconds=10800,
    ) == 600

    # 실측 크래시 파일 22분 × 3 = 66분 (경계 안쪽)
    assert compute_dynamic_timeout(
        duration_seconds=1359.87,
        multiplier=3.0,
        min_seconds=600,
        max_seconds=10800,
    ) == 4079  # int(1359.87 * 3.0) = int(4079.61) = 4079


# === 시나리오 4: 정상 파일 통과 ===


def test_정상_파일은_파이프라인_진입():
    """정상 볼륨·길이 파일은 ACCEPT 후 정상 진행."""
    from core.audio_quality import AudioQualityStatus, validate_audio_quality

    fake_path = Path("/tmp/normal_meeting.wav")
    # 일반적인 회의: -25dB, 60분
    with (
        patch("core.audio_quality._measure_mean_volume_db", return_value=-25.0),
        patch("core.audio_quality._measure_duration_seconds", return_value=3600.0),
    ):
        result = validate_audio_quality(
            fake_path,
            min_mean_db=-40.0,
            min_duration_s=5.0,
        )

    assert result.status == AudioQualityStatus.ACCEPT
    assert result.reason == ""


# === 시나리오 5: Config 기본값이 크래시 방지를 보장 ===


def test_Phase1_Config_기본값은_안전_설정():
    """AppConfig 기본값만으로도 Phase 1 방어막이 활성화되어야 한다."""
    from config import AppConfig

    c = AppConfig()

    # 오디오 품질 게이트 활성화
    assert c.audio_quality.enabled is True
    assert c.audio_quality.min_mean_volume_db == -40.0

    # 재시도 축소
    assert c.pipeline.retry_max_count == 1

    # 동적 타임아웃 활성화
    assert c.pipeline.dynamic_timeout_enabled is True
    assert c.pipeline.dynamic_timeout_multiplier == 3.0
    assert c.pipeline.dynamic_timeout_min_seconds == 600
    assert c.pipeline.dynamic_timeout_max_seconds == 10800

    # Watcher 제외 경로
    assert "audio_quarantine" in c.watcher.excluded_subdirs

    # Quarantine 경로 존재
    assert c.paths.audio_quarantine_subdir == "audio_quarantine"


# === 시나리오 6: 크래시 파일 재투입 방지 ===


def test_격리된_파일은_watcher_재감지_안됨(tmp_path: Path):
    """quarantine 으로 이동된 파일은 watcher의 excluded_subdirs 검사에 걸린다."""
    from config import AppConfig, PathsConfig, WatcherConfig
    from core.job_queue import AsyncJobQueue, JobQueue
    from core.watcher import FolderWatcher

    base_dir = tmp_path / "mt"
    base_dir.mkdir()
    (base_dir / "audio_input").mkdir()
    (base_dir / "audio_quarantine").mkdir()

    config = AppConfig(
        paths=PathsConfig(base_dir=str(base_dir)),
        watcher=WatcherConfig(excluded_subdirs=["audio_quarantine"]),
    )

    queue_db = base_dir / "pipeline.db"
    async_queue = AsyncJobQueue(JobQueue(db_path=queue_db))
    watcher = FolderWatcher(async_queue, config=config)

    # 격리 디렉토리 내 파일
    quarantined = base_dir / "audio_quarantine" / "crashed.wav"
    quarantined.write_bytes(b"x")

    # 입력 디렉토리 내 파일
    normal = base_dir / "audio_input" / "new.wav"
    normal.write_bytes(b"x")

    # _is_excluded 판정
    assert watcher._is_excluded(quarantined) is True
    assert watcher._is_excluded(normal) is False


# === 시나리오 7: DELETE → Quarantine → Watcher 제외 체인 ===


def test_삭제된_파일은_격리되고_재감지_안됨(tmp_path: Path):
    """DELETE API → move_to_quarantine → watcher 제외 전체 체인."""
    from config import AppConfig, PathsConfig, WatcherConfig
    from core.job_queue import AsyncJobQueue, JobQueue
    from core.quarantine import move_to_quarantine
    from core.watcher import FolderWatcher

    base_dir = tmp_path / "mt"
    audio_input = base_dir / "audio_input"
    audio_input.mkdir(parents=True)

    # 사용자가 삭제 시도할 파일
    audio_file = audio_input / "meeting_old.wav"
    audio_file.write_bytes(b"old meeting audio")

    config = AppConfig(
        paths=PathsConfig(base_dir=str(base_dir)),
        watcher=WatcherConfig(excluded_subdirs=["audio_quarantine"]),
    )

    # DELETE API 가 수행할 작업: move_to_quarantine
    quarantine_dir = config.paths.resolved_audio_quarantine_dir
    moved = move_to_quarantine(
        audio_file,
        quarantine_dir,
        reason="사용자 삭제: meeting_id=meeting_old",
    )

    # 이동됨
    assert not audio_file.exists()
    assert moved.exists()

    # watcher 가 이 파일을 다시 보지 않음
    queue_db = base_dir / "pipeline.db"
    async_queue = AsyncJobQueue(JobQueue(db_path=queue_db))
    watcher = FolderWatcher(async_queue, config=config)

    assert watcher._is_excluded(moved) is True
