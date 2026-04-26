"""
STT 모델 선택기 API 테스트 모듈

목적: api/routes.py에 추가된 4개의 STT 모델 관련 엔드포인트를 검증한다.
주요 테스트:
    - GET /api/stt-models: 3개 모델 + 동적 상태 + 활성 모델 표시
    - POST /api/stt-models/{id}/download: 202 + job_id / 404 / 409
    - GET /api/stt-models/{id}/download-status: 진행 상태 / 404
    - POST /api/stt-models/{id}/activate: config.yaml 업데이트 / 400
의존성: pytest, fastapi.testclient, unittest.mock
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from config import AppConfig, PathsConfig, ServerConfig, STTConfig
from core.stt_model_downloader import DownloadConflictError, DownloadJob
from core.stt_model_status import ModelStatus

# === 헬퍼 ===


def _make_test_config(
    tmp_path: Path, stt_model: str = "youngouk/whisper-medium-komixv2-mlx"
) -> AppConfig:
    """테스트용 AppConfig를 생성한다.

    Args:
        tmp_path: pytest 임시 디렉토리
        stt_model: stt.model_name에 설정할 값

    Returns:
        테스트용 AppConfig 인스턴스
    """
    return AppConfig(
        paths=PathsConfig(base_dir=str(tmp_path)),
        server=ServerConfig(host="127.0.0.1", port=8765, log_level="warning"),
        stt=STTConfig(model_name=stt_model),
    )


def _make_test_app(tmp_path: Path, stt_model: str = "youngouk/whisper-medium-komixv2-mlx") -> Any:
    """테스트용 FastAPI 앱을 생성한다 (외부 의존성 모킹 포함).

    Args:
        tmp_path: pytest 임시 디렉토리
        stt_model: 활성 STT 모델 경로

    Returns:
        FastAPI 앱 인스턴스
    """
    from api.server import create_app

    config = _make_test_config(tmp_path, stt_model=stt_model)
    with (
        patch("search.hybrid_search.HybridSearchEngine", return_value=MagicMock()),
        patch("search.chat.ChatEngine", return_value=MagicMock()),
    ):
        app = create_app(config)
    return app


def _install_fake_downloader(app: Any) -> MagicMock:
    """app.state.stt_downloader를 MagicMock으로 교체하고 반환한다."""
    fake = MagicMock()
    fake.get_progress = MagicMock(return_value=None)
    fake.start_download = AsyncMock(return_value="stt-download-fake-1")
    app.state.stt_downloader = fake
    return fake


def _make_download_job(
    model_id: str,
    status: ModelStatus = ModelStatus.DOWNLOADING,
    progress: int = 42,
) -> DownloadJob:
    """테스트용 DownloadJob 인스턴스."""
    return DownloadJob(
        job_id=f"stt-download-{model_id}-000",
        model_id=model_id,
        status=status,
        progress_percent=progress,
        current_step="downloading",
        started_at=datetime(2026, 4, 7, 3, 0, 0),
    )


# === GET /api/stt-models ===


class TestGetSTTModels:
    """GET /api/stt-models 엔드포인트 테스트."""

    def test_GET_stt_models_등록된_수만큼_반환(self, tmp_path: Path) -> None:
        """등록된 모델 수만큼 반환하고 active_model_id 필드가 있는지 확인한다."""
        from core.stt_model_registry import STT_MODELS

        app = _make_test_app(tmp_path)
        with TestClient(app) as client:
            _install_fake_downloader(app)
            resp = client.get("/api/stt-models")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["models"]) == len(STT_MODELS)
        assert "active_model_id" in data
        assert "active_model_path" in data

    def test_GET_stt_models_각_모델_필수_필드(self, tmp_path: Path) -> None:
        """각 모델 항목에 필수 필드가 포함되는지 확인한다."""
        app = _make_test_app(tmp_path)
        with TestClient(app) as client:
            _install_fake_downloader(app)
            resp = client.get("/api/stt-models")

        assert resp.status_code == 200
        required = {
            "id",
            "label",
            "description",
            "base_model",
            "expected_size_mb",
            "cer_percent",
            "wer_percent",
            "memory_gb",
            "rtf",
            "license",
            "is_default",
            "is_recommended",
            "status",
            "is_active",
        }
        for m in resp.json()["models"]:
            missing = required - set(m.keys())
            assert not missing, f"누락된 필드: {missing}"

    def test_GET_stt_models_활성_모델_표시(self, tmp_path: Path) -> None:
        """config.stt.model_name과 매칭되는 모델만 is_active=True를 가진다."""
        # komixv2는 HF repo ID를 직접 model_path로 사용한다.
        app = _make_test_app(tmp_path, stt_model="youngouk/whisper-medium-komixv2-mlx")
        with TestClient(app) as client:
            _install_fake_downloader(app)
            resp = client.get("/api/stt-models")

        data = resp.json()
        active = [m for m in data["models"] if m["is_active"]]
        assert len(active) == 1
        assert active[0]["id"] == "komixv2"
        assert data["active_model_id"] == "komixv2"


# === POST /api/stt-models/{id}/download ===


class TestDownloadSTTModel:
    """POST /api/stt-models/{id}/download 엔드포인트 테스트."""

    def test_POST_download_시작_202_및_job_id(self, tmp_path: Path) -> None:
        """다운로드 시작 시 202와 job_id를 반환한다."""
        app = _make_test_app(tmp_path)
        with TestClient(app) as client:
            fake = _install_fake_downloader(app)
            fake.start_download = AsyncMock(return_value="stt-download-seastar-1")
            resp = client.post("/api/stt-models/seastar-medium-4bit/download")

        assert resp.status_code == 202
        body = resp.json()
        assert body["job_id"] == "stt-download-seastar-1"
        assert body["model_id"] == "seastar-medium-4bit"
        fake.start_download.assert_awaited_once_with("seastar-medium-4bit")

    def test_POST_download_알_수_없는_모델_404(self, tmp_path: Path) -> None:
        """존재하지 않는 모델 ID는 404를 반환한다."""
        app = _make_test_app(tmp_path)
        with TestClient(app) as client:
            _install_fake_downloader(app)
            resp = client.post("/api/stt-models/does-not-exist/download")

        assert resp.status_code == 404

    def test_POST_download_이미_진행중_409(self, tmp_path: Path) -> None:
        """다운로더에서 DownloadConflictError 발생 시 409를 반환한다."""
        app = _make_test_app(tmp_path)
        with TestClient(app) as client:
            fake = _install_fake_downloader(app)
            fake.start_download = AsyncMock(
                side_effect=DownloadConflictError("이미 다운로드 중인 모델이 있습니다")
            )
            resp = client.post("/api/stt-models/ghost613-turbo-4bit/download")

        assert resp.status_code == 409


# === GET /api/stt-models/{id}/download-status ===


class TestDownloadStatusSTTModel:
    """GET /api/stt-models/{id}/download-status 엔드포인트 테스트."""

    def test_GET_download_status_진행률_반환(self, tmp_path: Path) -> None:
        """진행 중인 작업의 상태/진행률을 반환한다."""
        app = _make_test_app(tmp_path)
        with TestClient(app) as client:
            fake = _install_fake_downloader(app)
            fake.get_progress = MagicMock(
                return_value=_make_download_job("seastar-medium-4bit", ModelStatus.DOWNLOADING, 55)
            )
            resp = client.get("/api/stt-models/seastar-medium-4bit/download-status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["model_id"] == "seastar-medium-4bit"
        assert body["progress_percent"] == 55
        assert body["status"] == "downloading"

    def test_GET_download_status_없으면_404(self, tmp_path: Path) -> None:
        """작업이 없으면 404를 반환한다."""
        app = _make_test_app(tmp_path)
        with TestClient(app) as client:
            fake = _install_fake_downloader(app)
            fake.get_progress = MagicMock(return_value=None)
            resp = client.get("/api/stt-models/seastar-medium-4bit/download-status")

        assert resp.status_code == 404


# === POST /api/stt-models/{id}/activate ===


class TestActivateSTTModel:
    """POST /api/stt-models/{id}/activate 엔드포인트 테스트."""

    def test_POST_activate_READY_상태_config_업데이트(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """READY 상태 모델 활성화 시 config.yaml을 업데이트한다 (주석 보존)."""
        app = _make_test_app(tmp_path)

        # 임시 config.yaml 준비 (주석 포함 — 보존 검증)
        tmp_config = tmp_path / "config.yaml"
        tmp_config.write_text(
            "stt:\n"
            '  model_name: "youngouk/whisper-medium-komixv2-mlx"  # 기본 STT 모델\n'
            '  language: "ko"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr("api.routes._get_config_path", lambda: tmp_config)

        with TestClient(app) as client:
            _install_fake_downloader(app)
            # seastar 모델이 READY 상태라고 모킹
            with patch("api.routes.get_model_status", return_value=ModelStatus.READY):
                resp = client.post("/api/stt-models/seastar-medium-4bit/activate")

        assert resp.status_code == 200
        body = resp.json()
        assert body["model_id"] == "seastar-medium-4bit"
        assert "seastar" in body["model_path"]
        # 회귀 방지: previous_model_path 필드명 (이전 명칭은 previous_model_id 였음)
        assert "previous_model_path" in body
        assert body["previous_model_path"] == "youngouk/whisper-medium-komixv2-mlx"
        # 한국어 안내 메시지 포함
        assert "활성 모델" in body["message"]
        assert "다음 전사" in body["message"]

        # config.yaml이 seastar 경로로 갱신되었는지 확인
        content = tmp_config.read_text(encoding="utf-8")
        assert "seastar-medium-ko-4bit" in content
        # 주석이 보존되었는지 확인
        assert "# 기본 STT 모델" in content

        # 런타임 config도 갱신됐는지 확인
        assert "seastar-medium-ko-4bit" in app.state.config.stt.model_name

    def test_POST_activate_NOT_DOWNLOADED_400(self, tmp_path: Path) -> None:
        """다운로드되지 않은 모델 활성화 시 400을 반환한다."""
        app = _make_test_app(tmp_path)
        with TestClient(app) as client:
            _install_fake_downloader(app)
            with patch(
                "api.routes.get_model_status",
                return_value=ModelStatus.NOT_DOWNLOADED,
            ):
                resp = client.post("/api/stt-models/ghost613-turbo-4bit/activate")

        assert resp.status_code == 400
