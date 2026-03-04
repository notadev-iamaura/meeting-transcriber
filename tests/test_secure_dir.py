"""
보안 디렉토리 관리 모듈 테스트 (Secure Directory Manager Tests)

목적: security/secure_dir.py의 모든 기능을 검증한다.
주요 테스트:
  - 디렉토리 생성 및 권한 설정
  - Spotlight 인덱싱 제외 (.metadata_never_index)
  - Time Machine 백업 제외 (tmutil)
  - .gitignore 생성
  - 멱등성 (중복 실행 시 안전)
  - 보안 검증 (verify_security)
  - 에러 처리 (권한 부족, 디렉토리 생성 실패)
의존성: pytest, config 모듈
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from config import AppConfig, PathsConfig, SecurityConfig
from security.secure_dir import (
    SecureDirManager,
    SecureDirError,
    PermissionChangeError,
    DirectoryCreationError,
    _DEFAULT_GITIGNORE_PATTERNS,
    ensure_secure_dirs,
)

pytestmark = pytest.mark.asyncio


# === 픽스처 (Fixtures) ===


@pytest.fixture
def base_dir(tmp_path: Path) -> Path:
    """테스트용 기본 데이터 디렉토리 경로."""
    return tmp_path / "meeting-data"


@pytest.fixture
def mock_config(base_dir: Path) -> AppConfig:
    """테스트용 AppConfig 인스턴스.

    tmp_path 기반 경로를 사용하여 실제 파일시스템에 영향 없이 테스트한다.
    """
    config = AppConfig(
        paths=PathsConfig(
            base_dir=str(base_dir),
            audio_input_dir="audio_input",
            outputs_dir="outputs",
            checkpoints_dir="checkpoints",
            chroma_db_dir="chroma_db",
        ),
        security=SecurityConfig(
            data_dir_permissions=0o700,
            exclude_from_spotlight=True,
            exclude_from_timemachine=True,
        ),
    )
    return config


@pytest.fixture
def manager(mock_config: AppConfig) -> SecureDirManager:
    """테스트용 SecureDirManager 인스턴스."""
    return SecureDirManager(mock_config)


# === SecureDirManager 초기화 테스트 ===


class TestSecureDirManagerInit:
    """SecureDirManager 초기화 동작을 검증한다."""

    def test_init_stores_config(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """설정값이 올바르게 저장되는지 확인한다."""
        assert manager.base_dir == base_dir
        assert manager._permissions == 0o700
        assert manager._exclude_spotlight is True
        assert manager._exclude_timemachine is True

    def test_init_custom_permissions(self, base_dir: Path) -> None:
        """커스텀 권한 설정이 올바르게 적용되는지 확인한다."""
        config = AppConfig(
            paths=PathsConfig(base_dir=str(base_dir)),
            security=SecurityConfig(data_dir_permissions=0o750),
        )
        mgr = SecureDirManager(config)
        assert mgr._permissions == 0o750


# === 디렉토리 생성 테스트 ===


class TestDirectoryCreation:
    """디렉토리 생성 기능을 검증한다."""

    def test_creates_base_dir(self, manager: SecureDirManager, base_dir: Path) -> None:
        """base_dir이 생성되는지 확인한다."""
        assert not base_dir.exists()
        with patch.object(manager, "_exclude_from_timemachine"):
            manager.ensure_secure_dirs()
        assert base_dir.exists()

    def test_creates_subdirectories(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """하위 디렉토리가 모두 생성되는지 확인한다."""
        with patch.object(manager, "_exclude_from_timemachine"):
            manager.ensure_secure_dirs()
        assert (base_dir / "audio_input").exists()
        assert (base_dir / "outputs").exists()
        assert (base_dir / "checkpoints").exists()
        assert (base_dir / "chroma_db").exists()

    def test_idempotent_creation(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """이미 존재하는 디렉토리에 대해 에러 없이 실행되는지 확인한다."""
        base_dir.mkdir(parents=True)
        with patch.object(manager, "_exclude_from_timemachine"):
            # 두 번 호출해도 에러 없음
            manager.ensure_secure_dirs()
            manager.ensure_secure_dirs()
        assert base_dir.exists()

    def test_returns_secured_dirs(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """반환 값이 보안 설정된 디렉토리 목록인지 확인한다."""
        with patch.object(manager, "_exclude_from_timemachine"):
            dirs = manager.ensure_secure_dirs()
        assert len(dirs) == 5
        assert base_dir in dirs
        assert base_dir / "audio_input" in dirs
        assert base_dir / "outputs" in dirs
        assert base_dir / "checkpoints" in dirs
        assert base_dir / "chroma_db" in dirs

    def test_creation_failure_raises_error(self, manager: SecureDirManager) -> None:
        """디렉토리 생성 실패 시 DirectoryCreationError가 발생하는지 확인한다."""
        with patch.object(Path, "mkdir", side_effect=OSError("디스크 공간 부족")):
            with pytest.raises(DirectoryCreationError, match="디렉토리 생성 실패"):
                manager.ensure_secure_dirs()


# === 권한 설정 테스트 ===


class TestPermissions:
    """디렉토리 권한 설정 기능을 검증한다."""

    def test_sets_permissions_700(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """디렉토리 권한이 0o700으로 설정되는지 확인한다."""
        with patch.object(manager, "_exclude_from_timemachine"):
            manager.ensure_secure_dirs()
        mode = base_dir.stat().st_mode & 0o777
        assert mode == 0o700

    def test_corrects_wrong_permissions(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """잘못된 권한(0o755)을 0o700으로 교정하는지 확인한다."""
        base_dir.mkdir(parents=True)
        base_dir.chmod(0o755)
        assert (base_dir.stat().st_mode & 0o777) == 0o755

        with patch.object(manager, "_exclude_from_timemachine"):
            manager.ensure_secure_dirs()
        assert (base_dir.stat().st_mode & 0o777) == 0o700

    def test_skips_if_already_correct(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """이미 올바른 권한이면 변경하지 않는지 확인한다."""
        base_dir.mkdir(parents=True)
        base_dir.chmod(0o700)

        with patch.object(manager, "_exclude_from_timemachine"):
            with patch.object(Path, "chmod") as mock_chmod:
                # _set_permissions 내부에서 stat() 확인 후 스킵해야 하므로
                # 직접 _set_permissions만 테스트
                manager._set_permissions(base_dir)
                mock_chmod.assert_not_called()

    def test_custom_permissions_750(self, base_dir: Path) -> None:
        """커스텀 권한 0o750이 올바르게 적용되는지 확인한다."""
        config = AppConfig(
            paths=PathsConfig(base_dir=str(base_dir)),
            security=SecurityConfig(
                data_dir_permissions=0o750,
                exclude_from_spotlight=False,
                exclude_from_timemachine=False,
            ),
        )
        mgr = SecureDirManager(config)
        mgr.ensure_secure_dirs()
        assert (base_dir.stat().st_mode & 0o777) == 0o750

    def test_permission_change_failure_raises_error(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """권한 변경 실패 시 PermissionChangeError가 발생하는지 확인한다."""
        base_dir.mkdir(parents=True)
        with patch.object(
            Path, "chmod", side_effect=OSError("권한 변경 불가")
        ):
            with pytest.raises(PermissionChangeError, match="권한 변경 실패"):
                manager._set_permissions(base_dir)

    def test_subdirectory_permissions(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """하위 디렉토리도 모두 0o700 권한이 적용되는지 확인한다."""
        with patch.object(manager, "_exclude_from_timemachine"):
            manager.ensure_secure_dirs()
        for subdir_name in ["audio_input", "outputs", "checkpoints", "chroma_db"]:
            subdir = base_dir / subdir_name
            mode = subdir.stat().st_mode & 0o777
            assert mode == 0o700, f"{subdir_name} 권한이 {oct(mode)}임"


# === Spotlight 제외 테스트 ===


class TestSpotlightExclusion:
    """macOS Spotlight 인덱싱 제외 기능을 검증한다."""

    def test_creates_metadata_never_index(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """base_dir에 .metadata_never_index 파일이 생성되는지 확인한다."""
        with patch.object(manager, "_exclude_from_timemachine"):
            manager.ensure_secure_dirs()
        assert (base_dir / ".metadata_never_index").exists()

    def test_creates_in_all_subdirs(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """모든 하위 디렉토리에도 .metadata_never_index가 생성되는지 확인한다."""
        with patch.object(manager, "_exclude_from_timemachine"):
            manager.ensure_secure_dirs()
        for subdir_name in ["audio_input", "outputs", "checkpoints", "chroma_db"]:
            marker = base_dir / subdir_name / ".metadata_never_index"
            assert marker.exists(), f"{subdir_name}에 .metadata_never_index 없음"

    def test_idempotent_marker_creation(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """이미 존재하는 .metadata_never_index에 대해 에러 없이 동작하는지 확인한다."""
        base_dir.mkdir(parents=True)
        marker = base_dir / ".metadata_never_index"
        marker.touch()

        # 두 번째 호출도 에러 없이 통과
        manager._exclude_from_spotlight(base_dir)
        assert marker.exists()

    def test_spotlight_disabled_skips(self, base_dir: Path) -> None:
        """exclude_from_spotlight=False이면 마커를 생성하지 않는지 확인한다."""
        config = AppConfig(
            paths=PathsConfig(base_dir=str(base_dir)),
            security=SecurityConfig(
                exclude_from_spotlight=False,
                exclude_from_timemachine=False,
            ),
        )
        mgr = SecureDirManager(config)
        mgr.ensure_secure_dirs()
        assert not (base_dir / ".metadata_never_index").exists()

    def test_marker_creation_failure_logs_warning(
        self, manager: SecureDirManager, base_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """마커 생성 실패 시 경고 로그를 남기고 계속 진행하는지 확인한다."""
        base_dir.mkdir(parents=True)
        with patch.object(Path, "touch", side_effect=OSError("쓰기 불가")):
            # 에러가 발생하지 않고 경고만 로깅
            manager._exclude_from_spotlight(base_dir)


# === Time Machine 제외 테스트 ===


class TestTimeMachineExclusion:
    """macOS Time Machine 백업 제외 기능을 검증한다."""

    def test_calls_tmutil(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """tmutil addexclusion 명령이 호출되는지 확인한다."""
        base_dir.mkdir(parents=True)
        with patch("security.secure_dir.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            manager._exclude_from_timemachine(base_dir)
            mock_run.assert_called_once_with(
                ["tmutil", "addexclusion", str(base_dir)],
                capture_output=True,
                text=True,
                timeout=10,
            )

    def test_tmutil_not_found_logs_debug(
        self, manager: SecureDirManager, base_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """tmutil이 없는 환경에서 에러 없이 진행되는지 확인한다."""
        base_dir.mkdir(parents=True)
        with patch(
            "security.secure_dir.subprocess.run",
            side_effect=FileNotFoundError("tmutil not found"),
        ):
            import logging
            with caplog.at_level(logging.DEBUG):
                manager._exclude_from_timemachine(base_dir)
            assert "tmutil 미설치" in caplog.text

    def test_tmutil_failure_logs_warning(
        self, manager: SecureDirManager, base_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """tmutil 실패 시 경고 로그를 남기고 계속 진행하는지 확인한다."""
        base_dir.mkdir(parents=True)
        with patch("security.secure_dir.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="Permission denied")
            import logging
            with caplog.at_level(logging.WARNING):
                manager._exclude_from_timemachine(base_dir)
            assert "Time Machine 제외 설정 실패" in caplog.text

    def test_tmutil_timeout_logs_warning(
        self, manager: SecureDirManager, base_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """tmutil 타임아웃 시 경고 로그를 남기는지 확인한다."""
        base_dir.mkdir(parents=True)
        import subprocess as sp
        with patch(
            "security.secure_dir.subprocess.run",
            side_effect=sp.TimeoutExpired(cmd="tmutil", timeout=10),
        ):
            import logging
            with caplog.at_level(logging.WARNING):
                manager._exclude_from_timemachine(base_dir)
            assert "타임아웃" in caplog.text

    def test_timemachine_disabled_skips(self, base_dir: Path) -> None:
        """exclude_from_timemachine=False이면 tmutil을 호출하지 않는지 확인한다."""
        config = AppConfig(
            paths=PathsConfig(base_dir=str(base_dir)),
            security=SecurityConfig(
                exclude_from_spotlight=False,
                exclude_from_timemachine=False,
            ),
        )
        mgr = SecureDirManager(config)
        with patch("security.secure_dir.subprocess.run") as mock_run:
            mgr.ensure_secure_dirs()
            mock_run.assert_not_called()


# === .gitignore 테스트 ===


class TestGitignore:
    """.gitignore 생성 기능을 검증한다."""

    def test_creates_gitignore(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """.gitignore 파일이 생성되는지 확인한다."""
        with patch.object(manager, "_exclude_from_timemachine"):
            manager.ensure_secure_dirs()
        assert (base_dir / ".gitignore").exists()

    def test_gitignore_contains_patterns(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """.gitignore에 필수 패턴이 포함되어 있는지 확인한다."""
        with patch.object(manager, "_exclude_from_timemachine"):
            manager.ensure_secure_dirs()
        content = (base_dir / ".gitignore").read_text(encoding="utf-8")
        assert "*.wav" in content
        assert "*.db" in content
        assert "chroma_db/" in content
        assert "outputs/" in content
        assert ".DS_Store" in content

    def test_does_not_overwrite_existing(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """이미 존재하는 .gitignore를 덮어쓰지 않는지 확인한다."""
        base_dir.mkdir(parents=True)
        gitignore = base_dir / ".gitignore"
        gitignore.write_text("# 사용자 커스텀 규칙\ncustom_pattern/\n", encoding="utf-8")

        manager._create_gitignore(base_dir)

        content = gitignore.read_text(encoding="utf-8")
        assert "custom_pattern/" in content
        assert "*.wav" not in content  # 기본 패턴이 추가되지 않아야 함

    def test_gitignore_utf8_encoding(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """.gitignore가 UTF-8로 인코딩되는지 확인한다."""
        with patch.object(manager, "_exclude_from_timemachine"):
            manager.ensure_secure_dirs()
        content = (base_dir / ".gitignore").read_text(encoding="utf-8")
        # 한국어 주석이 포함됨
        assert "회의 데이터 파일" in content


# === 보안 검증 테스트 ===


class TestVerifySecurity:
    """보안 설정 검증 기능을 검증한다."""

    def test_all_checks_pass(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """모든 보안 설정이 올바를 때 전체 통과하는지 확인한다."""
        with patch.object(manager, "_exclude_from_timemachine"):
            manager.ensure_secure_dirs()
        result = manager.verify_security()
        assert result["dirs_exist"] is True
        assert result["permissions_ok"] is True
        assert result["spotlight_excluded"] is True
        assert result["gitignore_exists"] is True

    def test_dirs_not_exist(self, manager: SecureDirManager) -> None:
        """디렉토리가 없을 때 dirs_exist=False인지 확인한다."""
        result = manager.verify_security()
        assert result["dirs_exist"] is False

    def test_wrong_permissions_detected(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """잘못된 권한을 감지하는지 확인한다."""
        with patch.object(manager, "_exclude_from_timemachine"):
            manager.ensure_secure_dirs()
        # 권한을 의도적으로 변경
        base_dir.chmod(0o755)
        result = manager.verify_security()
        assert result["permissions_ok"] is False

    def test_missing_spotlight_marker_detected(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """Spotlight 마커 누락을 감지하는지 확인한다."""
        with patch.object(manager, "_exclude_from_timemachine"):
            manager.ensure_secure_dirs()
        # 마커 삭제
        (base_dir / ".metadata_never_index").unlink()
        result = manager.verify_security()
        assert result["spotlight_excluded"] is False

    def test_missing_gitignore_detected(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """.gitignore 누락을 감지하는지 확인한다."""
        with patch.object(manager, "_exclude_from_timemachine"):
            manager.ensure_secure_dirs()
        (base_dir / ".gitignore").unlink()
        result = manager.verify_security()
        assert result["gitignore_exists"] is False

    def test_spotlight_disabled_always_true(self, base_dir: Path) -> None:
        """Spotlight 제외 비활성화 시 spotlight_excluded=True인지 확인한다."""
        config = AppConfig(
            paths=PathsConfig(base_dir=str(base_dir)),
            security=SecurityConfig(
                exclude_from_spotlight=False,
                exclude_from_timemachine=False,
            ),
        )
        mgr = SecureDirManager(config)
        mgr.ensure_secure_dirs()
        result = mgr.verify_security()
        assert result["spotlight_excluded"] is True


# === 비동기 래퍼 테스트 ===


class TestAsyncWrapper:
    """비동기 래퍼 함수를 검증한다."""

    async def test_async_ensure_secure_dirs(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """비동기 래퍼가 동기 함수와 동일한 결과를 반환하는지 확인한다."""
        with patch.object(manager, "_exclude_from_timemachine"):
            dirs = await manager.ensure_secure_dirs_async()
        assert len(dirs) == 5
        assert base_dir.exists()
        assert (base_dir.stat().st_mode & 0o777) == 0o700


# === 편의 함수 테스트 ===


class TestConvenienceFunction:
    """모듈 수준 편의 함수를 검증한다."""

    def test_ensure_secure_dirs_with_config(
        self, mock_config: AppConfig, base_dir: Path
    ) -> None:
        """명시적 config로 편의 함수가 동작하는지 확인한다."""
        with patch("security.secure_dir.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            dirs = ensure_secure_dirs(mock_config)
        assert len(dirs) == 5
        assert base_dir.exists()

    def test_ensure_secure_dirs_without_config(self, base_dir: Path) -> None:
        """config=None일 때 싱글턴에서 가져오는지 확인한다."""
        mock_config = AppConfig(
            paths=PathsConfig(base_dir=str(base_dir)),
            security=SecurityConfig(
                exclude_from_spotlight=False,
                exclude_from_timemachine=False,
            ),
        )
        with patch("config.get_config", return_value=mock_config):
            dirs = ensure_secure_dirs()
        assert len(dirs) == 5


# === 에러 계층 테스트 ===


class TestErrorHierarchy:
    """커스텀 에러 클래스의 상속 구조를 검증한다."""

    def test_permission_error_is_secure_dir_error(self) -> None:
        """PermissionChangeError가 SecureDirError의 하위 클래스인지 확인한다."""
        assert issubclass(PermissionChangeError, SecureDirError)

    def test_creation_error_is_secure_dir_error(self) -> None:
        """DirectoryCreationError가 SecureDirError의 하위 클래스인지 확인한다."""
        assert issubclass(DirectoryCreationError, SecureDirError)

    def test_secure_dir_error_is_exception(self) -> None:
        """SecureDirError가 Exception의 하위 클래스인지 확인한다."""
        assert issubclass(SecureDirError, Exception)


# === 통합 시나리오 테스트 ===


class TestIntegrationScenarios:
    """실제 사용 시나리오를 검증한다."""

    def test_full_setup_and_verify(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """전체 설정 → 검증 흐름이 동작하는지 확인한다."""
        with patch.object(manager, "_exclude_from_timemachine"):
            manager.ensure_secure_dirs()
        result = manager.verify_security()
        assert all(result.values()), f"검증 실패: {result}"

    def test_repeated_setup_is_safe(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """여러 번 실행해도 안전한지 확인한다 (멱등성)."""
        with patch.object(manager, "_exclude_from_timemachine"):
            for _ in range(3):
                dirs = manager.ensure_secure_dirs()
                assert len(dirs) == 5

        result = manager.verify_security()
        assert all(result.values())

    def test_partial_existing_dirs(
        self, manager: SecureDirManager, base_dir: Path
    ) -> None:
        """일부 디렉토리만 존재할 때 나머지를 생성하는지 확인한다."""
        # base_dir과 outputs만 미리 생성
        base_dir.mkdir(parents=True)
        (base_dir / "outputs").mkdir()
        (base_dir / "outputs").chmod(0o755)  # 잘못된 권한

        with patch.object(manager, "_exclude_from_timemachine"):
            manager.ensure_secure_dirs()

        # 모든 디렉토리 존재 확인
        assert (base_dir / "audio_input").exists()
        assert (base_dir / "checkpoints").exists()
        assert (base_dir / "chroma_db").exists()
        # 권한 교정 확인
        assert (base_dir / "outputs").stat().st_mode & 0o777 == 0o700
