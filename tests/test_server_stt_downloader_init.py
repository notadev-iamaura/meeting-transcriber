"""
api/server.py lifespan 의 STTModelDownloader 등록 테스트

목적: startup 이벤트에서 app.state.stt_downloader 가 올바르게 등록되는지 검증한다.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from config import AppConfig, PathsConfig, ServerConfig
from core.stt_model_downloader import STTModelDownloader


def _make_test_config(tmp_path: Path) -> AppConfig:
    """테스트용 AppConfig."""
    return AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        server=ServerConfig(host="127.0.0.1", port=8765, log_level="warning"),
    )


class TestSTTDownloaderInit:
    """api/server.py lifespan의 STT 다운로더 초기화 테스트."""

    def test_app_state_에_stt_downloader_등록(self, tmp_path: Path) -> None:
        """lifespan startup 후 app.state.stt_downloader 가 STTModelDownloader 인스턴스여야 한다."""
        from api.server import create_app

        config = _make_test_config(tmp_path)
        with (
            patch(
                "search.hybrid_search.HybridSearchEngine",
                return_value=MagicMock(),
            ),
            patch("search.chat.ChatEngine", return_value=MagicMock()),
        ):
            app = create_app(config)

        with TestClient(app):
            assert hasattr(app.state, "stt_downloader")
            assert app.state.stt_downloader is not None
            assert isinstance(app.state.stt_downloader, STTModelDownloader)
            # models_dir 이 config.paths.resolved_base_dir / "stt_models" 여야 함
            expected = config.paths.resolved_base_dir / "stt_models"
            assert app.state.stt_downloader._models_dir == expected
            assert expected.exists()
