"""
데이터 라이프사이클 관리 모듈 (Data Lifecycle Manager Module)

목적: 회의 데이터를 Hot/Warm/Cold 3단계 라이프사이클로 자동 관리하여
     디스크 공간을 최적화한다.
주요 기능:
    - Hot (기본 30일): 원본 WAV 유지, 모든 데이터 보존
    - Warm (기본 30~90일): WAV → FLAC 무손실 압축, 메타데이터 보존
    - Cold (기본 90일+): 오디오 삭제 또는 아카이브, 메타데이터 영구 보존
    - 멱등성 보장: 이미 처리된 회의는 재처리하지 않음
    - 원자성 보장: FLAC 변환 완료 후에만 WAV 삭제
의존성: config 모듈 (LifecycleConfig, PathsConfig), ffmpeg (시스템 바이너리)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

from config import AppConfig

logger = logging.getLogger(__name__)

# meeting_id 유효성 검증 정규식 (path traversal 방지)
_MEETING_ID_PATTERN = re.compile(r"^[\w\-\.]+$")

# FLAC 변환 대상 오디오 확장자
_COMPRESSIBLE_EXTENSIONS = {".wav"}

# 삭제 대상 오디오 확장자
_AUDIO_EXTENSIONS = {".wav", ".flac", ".mp3", ".m4a", ".ogg", ".webm"}


class DataTier(str, Enum):
    """데이터 라이프사이클 등급을 정의하는 열거형.

    Hot: 최근 데이터, 원본 유지
    Warm: 중간 데이터, FLAC 압축
    Cold: 오래된 데이터, 삭제 또는 아카이브
    """

    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class ColdAction(str, Enum):
    """Cold 등급 데이터에 적용할 정책.

    delete_audio: 오디오 파일만 삭제, 메타데이터(JSON/MD) 보존
    archive: 외장 디스크로 이동 (향후 구현)
    """

    DELETE_AUDIO = "delete_audio"
    ARCHIVE = "archive"


# === 에러 계층 ===


class LifecycleError(Exception):
    """라이프사이클 관리 중 발생하는 에러의 기본 클래스."""


class CompressionError(LifecycleError):
    """FLAC 압축 실패 시 발생한다."""


class DeletionError(LifecycleError):
    """파일 삭제 실패 시 발생한다."""


# === 데이터 클래스 ===


@dataclass
class MeetingInfo:
    """회의 데이터의 라이프사이클 정보를 담는 데이터 클래스.

    Attributes:
        meeting_id: 회의 고유 식별자
        meeting_dir: 회의 데이터 디렉토리 경로
        created_at: 회의 생성 시각
        age_days: 생성 후 경과 일수
        tier: 현재 라이프사이클 등급
        has_wav: WAV 파일 존재 여부
        has_flac: FLAC 파일 존재 여부
        audio_files: 오디오 파일 목록
    """

    meeting_id: str
    meeting_dir: Path
    created_at: datetime
    age_days: int
    tier: DataTier
    has_wav: bool = False
    has_flac: bool = False
    audio_files: list[Path] = field(default_factory=list)


@dataclass
class LifecycleResult:
    """라이프사이클 실행 결과를 담는 데이터 클래스.

    Attributes:
        total_scanned: 스캔한 회의 수
        compressed: FLAC 압축한 회의 수
        deleted: 오디오 삭제한 회의 수
        skipped: 처리 불필요하여 스킵한 회의 수
        errors: 에러 발생 회의 목록 (meeting_id, 에러 메시지)
        bytes_saved: 절약한 바이트 수
    """

    total_scanned: int = 0
    compressed: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    bytes_saved: int = 0


# === 메인 클래스 ===


class LifecycleManager:
    """데이터 라이프사이클을 관리하는 클래스.

    config.yaml의 lifecycle 섹션 설정값을 기반으로
    outputs 디렉토리 내 회의 데이터를 Hot/Warm/Cold로 분류하고
    적절한 관리 작업을 수행한다.

    Args:
        config: 애플리케이션 설정 인스턴스
        now: 현재 시각 (테스트용 주입, None이면 실제 시각 사용)

    사용 예시:
        config = load_config()
        manager = LifecycleManager(config)
        result = manager.run()
        logger.info(f"압축: {result.compressed}, 삭제: {result.deleted}")
    """

    def __init__(
        self,
        config: AppConfig,
        now: Optional[datetime] = None,
    ) -> None:
        self._config = config
        self._outputs_dir = config.paths.resolved_outputs_dir
        self._hot_days = config.lifecycle.hot_days
        self._warm_days = config.lifecycle.warm_days
        self._cold_action = ColdAction(config.lifecycle.cold_action)
        self._now = now or datetime.now()

    @property
    def outputs_dir(self) -> Path:
        """관리 대상 outputs 디렉토리 경로."""
        return self._outputs_dir

    def run(self) -> LifecycleResult:
        """전체 라이프사이클 관리를 실행한다.

        outputs 디렉토리 내 모든 회의를 스캔하고,
        각 회의의 나이에 따라 적절한 작업을 수행한다.

        Returns:
            실행 결과 (압축/삭제/스킵 수, 에러 목록, 절약 바이트)
        """
        result = LifecycleResult()

        if not self._outputs_dir.exists():
            logger.warning(f"outputs 디렉토리 없음: {self._outputs_dir}")
            return result

        meetings = self.scan_meetings()
        result.total_scanned = len(meetings)

        for info in meetings:
            try:
                self._process_meeting(info, result)
            except LifecycleError as e:
                result.errors.append((info.meeting_id, str(e)))
                logger.error(f"라이프사이클 처리 실패: {info.meeting_id} - {e}")
            except OSError as e:
                result.errors.append((info.meeting_id, str(e)))
                logger.error(f"파일 시스템 오류: {info.meeting_id} - {e}")

        logger.info(
            f"라이프사이클 관리 완료: "
            f"스캔={result.total_scanned}, 압축={result.compressed}, "
            f"삭제={result.deleted}, 스킵={result.skipped}, "
            f"에러={len(result.errors)}, "
            f"절약={result.bytes_saved / (1024 * 1024):.1f}MB"
        )
        return result

    async def run_async(self) -> LifecycleResult:
        """run()의 비동기 래퍼.

        이벤트 루프 블로킹을 방지하기 위해 별도 스레드에서 실행한다.

        Returns:
            실행 결과
        """
        return await asyncio.to_thread(self.run)

    def scan_meetings(self) -> list[MeetingInfo]:
        """outputs 디렉토리 내 모든 회의를 스캔하여 정보를 수집한다.

        Returns:
            회의 정보 목록 (나이 기준 내림차순 정렬)
        """
        meetings: list[MeetingInfo] = []

        if not self._outputs_dir.exists():
            return meetings

        for entry in sorted(self._outputs_dir.iterdir()):
            if not entry.is_dir():
                continue

            meeting_id = entry.name

            # meeting_id 유효성 검증 (path traversal 방지)
            if not _MEETING_ID_PATTERN.match(meeting_id):
                logger.warning(f"유효하지 않은 meeting_id 스킵: {meeting_id}")
                continue

            created_at = self._get_meeting_created_at(entry)
            age_days = (self._now - created_at).days
            tier = self.classify_tier(age_days)

            # 오디오 파일 탐색
            audio_files = self._find_audio_files(entry)
            has_wav = any(f.suffix.lower() == ".wav" for f in audio_files)
            has_flac = any(f.suffix.lower() == ".flac" for f in audio_files)

            meetings.append(MeetingInfo(
                meeting_id=meeting_id,
                meeting_dir=entry,
                created_at=created_at,
                age_days=age_days,
                tier=tier,
                has_wav=has_wav,
                has_flac=has_flac,
                audio_files=audio_files,
            ))

        # 오래된 회의부터 처리 (age_days 내림차순)
        meetings.sort(key=lambda m: m.age_days, reverse=True)
        return meetings

    def classify_tier(self, age_days: int) -> DataTier:
        """경과 일수를 기반으로 데이터 등급을 분류한다.

        Args:
            age_days: 생성 후 경과 일수

        Returns:
            데이터 라이프사이클 등급
        """
        if age_days < self._hot_days:
            return DataTier.HOT
        elif age_days < self._warm_days:
            return DataTier.WARM
        else:
            return DataTier.COLD

    def compress_to_flac(self, wav_path: Path) -> Path:
        """WAV 파일을 FLAC 무손실 압축으로 변환한다.

        ffmpeg을 사용하여 WAV → FLAC 변환을 수행한다.
        변환 성공 후 원본 WAV 파일을 삭제한다.

        Args:
            wav_path: 변환할 WAV 파일 경로

        Returns:
            생성된 FLAC 파일 경로

        Raises:
            CompressionError: ffmpeg 변환 실패 시
        """
        flac_path = wav_path.with_suffix(".flac")

        # 이미 FLAC이 존재하면 스킵 (멱등성)
        if flac_path.exists():
            logger.debug(f"FLAC 이미 존재, 스킵: {flac_path}")
            # WAV가 아직 남아있으면 삭제
            if wav_path.exists():
                wav_size = wav_path.stat().st_size
                wav_path.unlink()
                logger.info(f"잔여 WAV 삭제: {wav_path} ({wav_size} bytes)")
                return flac_path
            return flac_path

        if not wav_path.exists():
            raise CompressionError(f"WAV 파일 없음: {wav_path}")

        wav_size = wav_path.stat().st_size

        # ffmpeg으로 WAV → FLAC 변환
        # -compression_level 8: 최대 압축 (시간은 더 걸리지만 용량 절약)
        try:
            cmd = [
                "ffmpeg", "-y",
                "-i", str(wav_path),
                "-c:a", "flac",
                "-compression_level", "8",
                str(flac_path),
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5분 타임아웃
            )

            if proc.returncode != 0:
                # 실패 시 불완전한 FLAC 파일 정리
                if flac_path.exists():
                    flac_path.unlink()
                raise CompressionError(
                    f"ffmpeg FLAC 변환 실패 (코드 {proc.returncode}): "
                    f"{proc.stderr.strip()[:200]}"
                )

        except FileNotFoundError:
            raise CompressionError(
                "ffmpeg이 설치되어 있지 않습니다. "
                "brew install ffmpeg으로 설치해주세요."
            )
        except subprocess.TimeoutExpired:
            # 타임아웃 시 불완전한 파일 정리
            if flac_path.exists():
                flac_path.unlink()
            raise CompressionError(f"ffmpeg 변환 타임아웃 (5분 초과): {wav_path}")

        # FLAC 파일 생성 확인
        if not flac_path.exists() or flac_path.stat().st_size == 0:
            raise CompressionError(f"FLAC 파일 생성 실패: {flac_path}")

        flac_size = flac_path.stat().st_size

        # 원자성: FLAC 변환 성공 후에만 WAV 삭제
        wav_path.unlink()
        saved = wav_size - flac_size

        logger.info(
            f"FLAC 압축 완료: {wav_path.name} → {flac_path.name} "
            f"({wav_size:,} → {flac_size:,} bytes, "
            f"{saved:,} bytes 절약, {saved / wav_size * 100:.1f}% 감소)"
        )
        return flac_path

    def apply_cold_policy(self, meeting_info: MeetingInfo) -> int:
        """Cold 등급 회의에 정책을 적용한다.

        Args:
            meeting_info: 회의 정보

        Returns:
            삭제/이동으로 절약한 바이트 수

        Raises:
            DeletionError: 파일 삭제 실패 시
        """
        if self._cold_action == ColdAction.DELETE_AUDIO:
            return self._delete_audio_files(meeting_info)
        elif self._cold_action == ColdAction.ARCHIVE:
            logger.info(
                f"아카이브 정책은 아직 미구현입니다: {meeting_info.meeting_id}"
            )
            return 0
        return 0

    def get_summary(self) -> dict[str, int]:
        """현재 데이터 등급별 회의 수를 요약한다.

        Returns:
            등급별 회의 수 딕셔너리
            예: {"hot": 5, "warm": 3, "cold": 2, "total": 10}
        """
        meetings = self.scan_meetings()
        summary: dict[str, int] = {"hot": 0, "warm": 0, "cold": 0, "total": len(meetings)}
        for m in meetings:
            summary[m.tier.value] += 1
        return summary

    # === 내부 메서드 ===

    def _process_meeting(
        self,
        info: MeetingInfo,
        result: LifecycleResult,
    ) -> None:
        """개별 회의에 라이프사이클 정책을 적용한다.

        Args:
            info: 회의 정보
            result: 실행 결과 (누적)
        """
        if info.tier == DataTier.HOT:
            result.skipped += 1
            return

        if info.tier == DataTier.WARM:
            # WAV → FLAC 압축
            if info.has_wav:
                wav_files = [
                    f for f in info.audio_files
                    if f.suffix.lower() in _COMPRESSIBLE_EXTENSIONS
                ]
                for wav_file in wav_files:
                    self.compress_to_flac(wav_file)
                    wav_size = 0  # 이미 삭제됨
                    # 바이트 절약은 compress_to_flac 내부에서 계산
                result.compressed += 1
            else:
                result.skipped += 1
            return

        if info.tier == DataTier.COLD:
            # 먼저 WAV가 남아있으면 FLAC으로 변환
            if info.has_wav:
                wav_files = [
                    f for f in info.audio_files
                    if f.suffix.lower() in _COMPRESSIBLE_EXTENSIONS
                ]
                for wav_file in wav_files:
                    self.compress_to_flac(wav_file)

            # Cold 정책 적용
            saved = self.apply_cold_policy(info)
            if saved > 0:
                result.deleted += 1
                result.bytes_saved += saved
            else:
                result.skipped += 1

    def _get_meeting_created_at(self, meeting_dir: Path) -> datetime:
        """회의의 생성 시각을 결정한다.

        pipeline_state.json의 created_at 필드를 우선 사용하고,
        없으면 디렉토리의 수정 시각을 사용한다.

        Args:
            meeting_dir: 회의 데이터 디렉토리

        Returns:
            회의 생성 시각
        """
        state_path = meeting_dir / "pipeline_state.json"

        if state_path.exists():
            try:
                with open(state_path, encoding="utf-8") as f:
                    data = json.load(f)
                created_str = data.get("created_at", "")
                if created_str:
                    return datetime.fromisoformat(created_str)
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                logger.warning(
                    f"pipeline_state.json 파싱 실패, "
                    f"디렉토리 mtime 사용: {meeting_dir} - {e}"
                )

        # 폴백: 디렉토리 수정 시각
        mtime = meeting_dir.stat().st_mtime
        return datetime.fromtimestamp(mtime)

    def _find_audio_files(self, meeting_dir: Path) -> list[Path]:
        """회의 디렉토리에서 오디오 파일을 찾는다.

        Args:
            meeting_dir: 회의 데이터 디렉토리

        Returns:
            오디오 파일 경로 목록
        """
        audio_files: list[Path] = []
        for f in meeting_dir.iterdir():
            if f.is_file() and f.suffix.lower() in _AUDIO_EXTENSIONS:
                audio_files.append(f)
        return sorted(audio_files)

    def _delete_audio_files(self, meeting_info: MeetingInfo) -> int:
        """회의의 오디오 파일을 삭제한다.

        메타데이터(JSON, MD)는 보존하고 오디오 파일만 삭제한다.

        Args:
            meeting_info: 회의 정보

        Returns:
            삭제로 절약한 바이트 수

        Raises:
            DeletionError: 파일 삭제 실패 시
        """
        total_freed = 0

        # 최신 오디오 파일 목록 재탐색 (FLAC 변환 후 변경되었을 수 있음)
        audio_files = self._find_audio_files(meeting_info.meeting_dir)

        if not audio_files:
            logger.debug(
                f"삭제할 오디오 파일 없음: {meeting_info.meeting_id}"
            )
            return 0

        for audio_file in audio_files:
            try:
                file_size = audio_file.stat().st_size
                audio_file.unlink()
                total_freed += file_size
                logger.info(
                    f"오디오 삭제: {audio_file.name} "
                    f"({file_size:,} bytes) - {meeting_info.meeting_id}"
                )
            except OSError as e:
                raise DeletionError(
                    f"오디오 파일 삭제 실패: {audio_file} - {e}"
                ) from e

        logger.info(
            f"Cold 정책 적용 완료: {meeting_info.meeting_id}, "
            f"삭제 {len(audio_files)}개 파일, {total_freed:,} bytes 해제"
        )
        return total_freed


def run_lifecycle(config: Optional[AppConfig] = None) -> LifecycleResult:
    """라이프사이클 관리의 편의 함수.

    LifecycleManager 인스턴스를 생성하고 run()을 호출한다.

    Args:
        config: 애플리케이션 설정. None이면 싱글턴에서 가져온다.

    Returns:
        실행 결과
    """
    if config is None:
        from config import get_config
        config = get_config()

    manager = LifecycleManager(config)
    return manager.run()
