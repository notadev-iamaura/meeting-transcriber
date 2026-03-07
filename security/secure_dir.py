"""
보안 디렉토리 관리 모듈 (Secure Directory Manager Module)

목적: 데이터 디렉토리에 보안 설정을 적용하여 민감한 회의 데이터를 보호한다.
주요 기능:
  - 디렉토리 권한 설정 (chmod 700: 소유자만 접근)
  - macOS Spotlight 인덱싱 제외 (.metadata_never_index 생성)
  - macOS Time Machine 백업 제외 (com.apple.metadata:com_apple_backup_excludeItem)
  - .gitignore 생성 (데이터 파일의 실수 커밋 방지)
의존성: config 모듈 (SecurityConfig, PathsConfig)
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

from config import AppConfig

logger = logging.getLogger(__name__)


class SecureDirError(Exception):
    """보안 디렉토리 설정 중 발생하는 에러의 기본 클래스."""


class PermissionChangeError(SecureDirError):
    """디렉토리 권한 변경에 실패했을 때 발생한다."""


class DirectoryCreationError(SecureDirError):
    """디렉토리 생성에 실패했을 때 발생한다."""


# .gitignore에 기본으로 포함할 패턴 목록
_DEFAULT_GITIGNORE_PATTERNS: list[str] = [
    "# 회의 데이터 파일 (민감 정보)",
    "*.wav",
    "*.mp3",
    "*.m4a",
    "*.flac",
    "*.ogg",
    "*.webm",
    "",
    "# 데이터베이스 파일",
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "",
    "# 벡터 저장소",
    "chroma_db/",
    "",
    "# 파이프라인 출력",
    "outputs/",
    "checkpoints/",
    "",
    "# 시스템 파일",
    ".DS_Store",
    "__pycache__/",
]


class SecureDirManager:
    """데이터 디렉토리의 보안 설정을 관리하는 클래스.

    config.yaml의 security 섹션 설정값을 기반으로
    데이터 디렉토리에 권한, Spotlight 제외, gitignore 등을 적용한다.

    Args:
        config: 애플리케이션 설정 인스턴스

    사용 예시:
        config = load_config()
        manager = SecureDirManager(config)
        manager.ensure_secure_dirs()
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._base_dir = config.paths.resolved_base_dir
        self._permissions = config.security.data_dir_permissions
        self._exclude_spotlight = config.security.exclude_from_spotlight
        self._exclude_timemachine = config.security.exclude_from_timemachine

    @property
    def base_dir(self) -> Path:
        """보안 설정이 적용될 기본 데이터 디렉토리 경로."""
        return self._base_dir

    def ensure_secure_dirs(self) -> list[Path]:
        """데이터 디렉토리 구조를 생성하고 보안 설정을 적용한다.

        1. base_dir 및 하위 디렉토리 생성
        2. 권한 설정 (chmod 700)
        3. Spotlight 인덱싱 제외
        4. Time Machine 백업 제외
        5. .gitignore 생성

        Returns:
            보안 설정이 적용된 디렉토리 목록

        Raises:
            DirectoryCreationError: 디렉토리 생성 실패 시
            PermissionChangeError: 권한 변경 실패 시
        """
        # 보안 설정을 적용할 디렉토리 목록
        dirs_to_secure = self._get_dirs_to_secure()

        # 1. 디렉토리 생성
        for dir_path in dirs_to_secure:
            self._create_directory(dir_path)

        # 2. 권한 설정
        for dir_path in dirs_to_secure:
            self._set_permissions(dir_path)

        # 3. Spotlight 제외
        if self._exclude_spotlight:
            for dir_path in dirs_to_secure:
                self._exclude_from_spotlight(dir_path)

        # 4. Time Machine 제외
        if self._exclude_timemachine:
            self._exclude_from_timemachine(self._base_dir)

        # 5. .gitignore 생성
        self._create_gitignore(self._base_dir)

        logger.info(
            f"보안 디렉토리 설정 완료: {len(dirs_to_secure)}개 디렉토리, "
            f"권한={oct(self._permissions)}"
        )
        return dirs_to_secure

    async def ensure_secure_dirs_async(self) -> list[Path]:
        """ensure_secure_dirs의 비동기 래퍼.

        이벤트 루프 블로킹을 방지하기 위해 별도 스레드에서 실행한다.

        Returns:
            보안 설정이 적용된 디렉토리 목록
        """
        return await asyncio.to_thread(self.ensure_secure_dirs)

    def verify_security(self) -> dict[str, bool]:
        """현재 보안 설정 상태를 검증한다.

        Returns:
            각 검증 항목의 통과 여부를 담은 딕셔너리
            예: {"permissions_ok": True, "spotlight_excluded": True, ...}
        """
        result: dict[str, bool] = {}
        dirs = self._get_dirs_to_secure()

        # 디렉토리 존재 여부
        result["dirs_exist"] = all(d.exists() for d in dirs)

        # 권한 검증
        result["permissions_ok"] = all(self._check_permissions(d) for d in dirs if d.exists())

        # Spotlight 제외 검증
        if self._exclude_spotlight:
            result["spotlight_excluded"] = all(
                (d / ".metadata_never_index").exists() for d in dirs if d.exists()
            )
        else:
            result["spotlight_excluded"] = True

        # .gitignore 존재 여부
        result["gitignore_exists"] = (self._base_dir / ".gitignore").exists()

        return result

    def _get_dirs_to_secure(self) -> list[Path]:
        """보안 설정을 적용할 디렉토리 목록을 반환한다.

        Returns:
            base_dir 및 주요 하위 디렉토리 경로 목록
        """
        paths_config = self._config.paths
        return [
            self._base_dir,
            paths_config.resolved_audio_input_dir,
            paths_config.resolved_outputs_dir,
            paths_config.resolved_checkpoints_dir,
            paths_config.resolved_chroma_db_dir,
        ]

    def _create_directory(self, dir_path: Path) -> None:
        """디렉토리를 생성한다. 이미 존재하면 스킵.

        Args:
            dir_path: 생성할 디렉토리 경로

        Raises:
            DirectoryCreationError: 디렉토리 생성 실패 시
        """
        try:
            dir_path.mkdir(parents=True, exist_ok=True)
            logger.debug(f"디렉토리 확인/생성: {dir_path}")
        except OSError as e:
            raise DirectoryCreationError(f"디렉토리 생성 실패: {dir_path} - {e}") from e

    def _set_permissions(self, dir_path: Path) -> None:
        """디렉토리 권한을 설정한다. (기본: 0o700 = 소유자만 rwx)

        Args:
            dir_path: 권한을 설정할 디렉토리 경로

        Raises:
            PermissionChangeError: 권한 변경 실패 시
        """
        try:
            current_mode = dir_path.stat().st_mode & 0o777
            if current_mode != self._permissions:
                dir_path.chmod(self._permissions)
                logger.info(
                    f"권한 변경: {dir_path} {oct(current_mode)} → {oct(self._permissions)}"
                )
            else:
                logger.debug(f"권한 이미 설정됨: {dir_path} ({oct(self._permissions)})")
        except OSError as e:
            raise PermissionChangeError(f"권한 변경 실패: {dir_path} - {e}") from e

    def _check_permissions(self, dir_path: Path) -> bool:
        """디렉토리 권한이 설정값과 일치하는지 확인한다.

        Args:
            dir_path: 확인할 디렉토리 경로

        Returns:
            권한이 일치하면 True
        """
        try:
            current_mode = dir_path.stat().st_mode & 0o777
            return current_mode == self._permissions
        except OSError:
            return False

    def _exclude_from_spotlight(self, dir_path: Path) -> None:
        """macOS Spotlight 인덱싱에서 디렉토리를 제외한다.

        .metadata_never_index 파일을 생성하면
        macOS Spotlight가 해당 디렉토리를 인덱싱하지 않는다.

        Args:
            dir_path: Spotlight에서 제외할 디렉토리 경로
        """
        marker = dir_path / ".metadata_never_index"
        if not marker.exists():
            try:
                marker.touch()
                logger.info(f"Spotlight 제외 마커 생성: {marker}")
            except OSError as e:
                logger.warning(f"Spotlight 제외 마커 생성 실패: {marker} - {e}")

    def _exclude_from_timemachine(self, dir_path: Path) -> None:
        """macOS Time Machine 백업에서 디렉토리를 제외한다.

        tmutil addexclusion 명령으로 Time Machine 제외를 설정한다.
        관리자 권한 없이도 사용자 수준 제외가 가능하다.

        Args:
            dir_path: Time Machine에서 제외할 디렉토리 경로
        """
        try:
            result = subprocess.run(
                ["tmutil", "addexclusion", str(dir_path)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                logger.info(f"Time Machine 제외 설정: {dir_path}")
            else:
                logger.warning(
                    f"Time Machine 제외 설정 실패: {dir_path} - {result.stderr.strip()}"
                )
        except FileNotFoundError:
            # tmutil이 없는 환경 (Linux 등)
            logger.debug("tmutil 미설치. Time Machine 제외 생략.")
        except subprocess.TimeoutExpired:
            logger.warning(f"Time Machine 제외 명령 타임아웃: {dir_path}")

    def _create_gitignore(self, dir_path: Path) -> None:
        """.gitignore 파일을 생성한다. 이미 존재하면 스킵.

        Args:
            dir_path: .gitignore를 생성할 디렉토리 경로
        """
        gitignore_path = dir_path / ".gitignore"
        if gitignore_path.exists():
            logger.debug(f".gitignore 이미 존재: {gitignore_path}")
            return

        try:
            content = "\n".join(_DEFAULT_GITIGNORE_PATTERNS) + "\n"
            gitignore_path.write_text(content, encoding="utf-8")
            logger.info(f".gitignore 생성: {gitignore_path}")
        except OSError as e:
            logger.warning(f".gitignore 생성 실패: {gitignore_path} - {e}")


def ensure_secure_dirs(config: AppConfig | None = None) -> list[Path]:
    """보안 디렉토리 설정의 편의 함수.

    SecureDirManager 인스턴스를 생성하고 ensure_secure_dirs()를 호출한다.

    Args:
        config: 애플리케이션 설정. None이면 싱글턴에서 가져온다.

    Returns:
        보안 설정이 적용된 디렉토리 목록
    """
    if config is None:
        from config import get_config

        config = get_config()

    manager = SecureDirManager(config)
    return manager.ensure_secure_dirs()
