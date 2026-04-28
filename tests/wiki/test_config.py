"""WikiConfig Pydantic 설정 단위 테스트

테스트 범위:
    - WikiConfig 의 기본값 (enabled=False, dry_run=True 등)
    - AppConfig 에 wiki 필드가 등록되어 있는지
    - 환경변수 오버라이드 (MT_WIKI_ENABLED, MT_WIKI_ROOT 등)
    - YAML 파싱 + 환경변수 오버라이드 통합

이 테스트는 TDD Red 단계에서 작성되었으며, WikiConfig 가 추가되기 전에는
ImportError 또는 AttributeError 로 실패해야 한다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from config import AppConfig, WikiConfig, _apply_env_overrides, load_config


class TestWikiConfigDefaults:
    """WikiConfig 의 기본값을 검증한다."""

    def test_기본값으로_생성하면_enabled가_False다(self) -> None:
        """Phase 1 안전 기본값: enabled=False (실제 LLM 호출 차단)."""
        cfg = WikiConfig()
        assert cfg.enabled is False

    def test_기본값으로_생성하면_dry_run이_True다(self) -> None:
        """Phase 1 골격: dry_run=True 로 강제."""
        cfg = WikiConfig()
        assert cfg.dry_run is True

    def test_기본_compiler_model이_EXAONE이다(self) -> None:
        """기본 LLM 모델은 EXAONE 3.5 4bit."""
        cfg = WikiConfig()
        assert cfg.compiler_model == "mlx-community/EXAONE-3.5-7.8B-Instruct-4bit"

    def test_기본_lint_interval이_5다(self) -> None:
        """N 회의마다 lint 의 N 기본값."""
        cfg = WikiConfig()
        assert cfg.lint_interval == 5

    def test_기본_confidence_threshold가_7이다(self) -> None:
        """D3 confidence 컷오프 기본값."""
        cfg = WikiConfig()
        assert cfg.confidence_threshold == 7

    def test_root_경로는_meeting_transcriber_wiki다(self) -> None:
        """기본 wiki 루트는 ~/.meeting-transcriber/wiki/."""
        cfg = WikiConfig()
        # 사용자 입력 그대로 또는 ~ 확장된 형태 둘 다 허용
        root_str = str(cfg.root)
        assert "wiki" in root_str
        assert ".meeting-transcriber" in root_str


class TestAppConfigIntegration:
    """AppConfig 에 wiki 필드가 등록되었는지 검증한다."""

    def test_AppConfig는_wiki_필드를_가진다(self) -> None:
        """AppConfig() 호출 시 wiki: WikiConfig 필드가 자동 생성되어야 한다."""
        app = AppConfig()
        assert hasattr(app, "wiki")
        assert isinstance(app.wiki, WikiConfig)

    def test_AppConfig_wiki_기본값은_disabled다(self) -> None:
        """기본 AppConfig 상태에서 wiki 는 안전하게 꺼져 있어야 한다."""
        app = AppConfig()
        assert app.wiki.enabled is False
        assert app.wiki.dry_run is True


class TestEnvOverrides:
    """환경변수 오버라이드 동작을 검증한다."""

    def test_MT_WIKI_ENABLED_true_이면_enabled가_True가_된다(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`MT_WIKI_ENABLED=true` 환경변수가 _apply_env_overrides 에서 반영되어야 한다."""
        monkeypatch.setenv("MT_WIKI_ENABLED", "true")
        data: dict = {}
        result = _apply_env_overrides(data)
        assert result.get("wiki", {}).get("enabled") is True

    def test_MT_WIKI_ENABLED_false_이면_enabled가_False가_된다(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`MT_WIKI_ENABLED=false` 도 정상 처리되어야 한다."""
        monkeypatch.setenv("MT_WIKI_ENABLED", "false")
        data: dict = {}
        result = _apply_env_overrides(data)
        assert result.get("wiki", {}).get("enabled") is False

    def test_MT_WIKI_ENABLED_미설정_시_데이터_변경_없음(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """환경변수가 없으면 wiki 키가 추가되어선 안 된다."""
        monkeypatch.delenv("MT_WIKI_ENABLED", raising=False)
        monkeypatch.delenv("MT_WIKI_ROOT", raising=False)
        monkeypatch.delenv("MT_WIKI_DRY_RUN", raising=False)
        data: dict = {}
        result = _apply_env_overrides(data)
        # wiki 키 자체가 추가되지 않거나, 추가되었어도 비어있어야 함
        wiki_section = result.get("wiki", {})
        # enabled / root / dry_run 어느 것도 환경변수로 들어오지 않았으므로 키 없음
        assert "enabled" not in wiki_section
        assert "root" not in wiki_section
        assert "dry_run" not in wiki_section

    def test_MT_WIKI_ROOT_환경변수가_root를_오버라이드한다(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`MT_WIKI_ROOT=/custom/path` 가 root 필드에 반영되어야 한다."""
        custom_root = tmp_path / "my-wiki"
        monkeypatch.setenv("MT_WIKI_ROOT", str(custom_root))
        data: dict = {}
        result = _apply_env_overrides(data)
        assert result.get("wiki", {}).get("root") == str(custom_root)

    def test_MT_WIKI_DRY_RUN_false_이면_dry_run이_False가_된다(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`MT_WIKI_DRY_RUN=false` 가 dry_run=False 로 반영되어야 한다."""
        monkeypatch.setenv("MT_WIKI_DRY_RUN", "false")
        data: dict = {}
        result = _apply_env_overrides(data)
        assert result.get("wiki", {}).get("dry_run") is False


class TestLoadConfigEndToEnd:
    """load_config() 가 환경변수와 함께 WikiConfig 를 정상 생성하는지 검증한다."""

    def test_load_config_with_MT_WIKI_ENABLED_true(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """환경변수만으로 wiki.enabled=True 인 AppConfig 가 만들어져야 한다."""
        monkeypatch.setenv("MT_WIKI_ENABLED", "true")
        monkeypatch.setenv("MT_WIKI_ROOT", str(tmp_path / "wiki"))
        # 기본 config.yaml 을 사용하지 않도록 빈 임시 파일 사용
        empty_config = tmp_path / "config_empty.yaml"
        empty_config.write_text("")

        cfg = load_config(empty_config)
        assert cfg.wiki.enabled is True
        assert str(cfg.wiki.root) == str(tmp_path / "wiki")
