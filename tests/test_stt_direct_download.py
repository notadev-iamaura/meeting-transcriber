"""
STT 모델 직접 URL 다운로드 테스트.

`huggingface_hub` 실패 시 자동 폴백 + 명시적 `prefer_direct` 경로 검증.
urllib.request 호출은 mock 처리한다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from api.routes import router
from core import stt_model_registry
from core.stt_model_downloader import STTModelDownloader
from core.stt_model_status import ModelStatus


@pytest.fixture
def tmp_models_dir(tmp_path):
    d = tmp_path / "stt_models"
    d.mkdir()
    return d


@pytest.fixture
def downloader(tmp_models_dir, tmp_path, monkeypatch):
    """테스트용 다운로더. 수동 임포트 타겟을 임시 경로로 우회."""
    manual_base = tmp_path / "app-data"
    monkeypatch.setattr(
        stt_model_registry,
        "get_manual_import_dir",
        lambda spec, base_dir=None: str(
            manual_base / "stt_models" / f"{spec.id}-manual"
        ),
    )
    # downloader 모듈 내부에서도 동일하게 참조되도록 패치
    import core.stt_model_downloader as dl_mod
    monkeypatch.setattr(
        dl_mod,
        "get_manual_import_dir",
        lambda spec, base_dir=None: str(
            manual_base / "stt_models" / f"{spec.id}-manual"
        ),
    )
    # stt_model_status 에서도 패치 (verify 경로)
    import core.stt_model_status as status_mod
    monkeypatch.setattr(
        status_mod,
        "get_manual_import_dir",
        lambda spec, base_dir=None: str(
            manual_base / "stt_models" / f"{spec.id}-manual"
        ),
    )
    return STTModelDownloader(models_dir=tmp_models_dir)


def _make_fake_urlopen(file_contents: dict[str, bytes]):
    """urllib.request.urlopen 대신 사용할 가짜 컨텍스트 매니저 factory.

    Args:
        file_contents: {파일명 접미사: 바이트 내용} 매핑
    """

    class _FakeResponse:
        def __init__(self, data: bytes):
            self._data = data
            self._pos = 0
            self.headers = {"Content-Length": str(len(data))}

        def read(self, chunk: int = -1) -> bytes:
            if chunk < 0:
                result = self._data[self._pos :]
                self._pos = len(self._data)
                return result
            result = self._data[self._pos : self._pos + chunk]
            self._pos += len(result)
            return result

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _fake(req, timeout=None):
        # req 는 urllib.request.Request 또는 str URL
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for suffix, content in file_contents.items():
            if url.endswith(suffix):
                return _FakeResponse(content)
        raise RuntimeError(f"unexpected URL in test: {url}")

    return _fake


# === 자동 폴백 ===


class TestDownloadFallback:
    async def test_hf_실패시_direct_URL_자동_폴백(self, downloader, monkeypatch):
        """huggingface_hub 가 실패하면 direct URL 다운로드로 자동 폴백한다."""
        hf_called = {"n": 0}
        direct_called = {"n": 0}

        async def failing_hf(spec, job):
            hf_called["n"] += 1
            raise RuntimeError("SSL 오류: CERTIFICATE_VERIFY_FAILED")

        async def successful_direct(spec, job):
            direct_called["n"] += 1
            # 검증 통과를 위해 가짜 파일 생성
            from core.stt_model_registry import get_manual_import_dir

            target = Path(get_manual_import_dir(spec))
            target.mkdir(parents=True, exist_ok=True)
            (target / "config.json").write_text('{"n_mels": 80}')
            (target / "weights.safetensors").write_bytes(b"fake" * 100)

        monkeypatch.setattr(downloader, "_hf_download", failing_hf)
        monkeypatch.setattr(downloader, "_direct_url_download", successful_direct)

        await downloader.start_download("seastar-medium-4bit")
        await downloader.wait_for("seastar-medium-4bit")

        progress = downloader.get_progress("seastar-medium-4bit")
        assert progress.status == ModelStatus.READY
        assert hf_called["n"] == 1
        assert direct_called["n"] == 1

    async def test_hf_성공시_direct_미호출(self, downloader, monkeypatch):
        """huggingface_hub 가 성공하면 direct URL 경로는 호출되지 않는다."""
        direct_called = {"n": 0}

        async def successful_hf(spec, job):
            pass

        async def never_called_direct(spec, job):
            direct_called["n"] += 1

        monkeypatch.setattr(downloader, "_hf_download", successful_hf)
        monkeypatch.setattr(downloader, "_direct_url_download", never_called_direct)
        monkeypatch.setattr(downloader, "_verify", lambda spec: True)

        await downloader.start_download("seastar-medium-4bit")
        await downloader.wait_for("seastar-medium-4bit")

        progress = downloader.get_progress("seastar-medium-4bit")
        assert progress.status == ModelStatus.READY
        assert direct_called["n"] == 0

    async def test_두_방법_모두_실패시_ERROR_메시지(self, downloader, monkeypatch):
        """둘 다 실패하면 두 오류 정보를 모두 포함한 에러 메시지를 반환."""

        async def failing_hf(spec, job):
            raise RuntimeError("HF 오류")

        async def failing_direct(spec, job):
            raise RuntimeError("네트워크 오류")

        monkeypatch.setattr(downloader, "_hf_download", failing_hf)
        monkeypatch.setattr(downloader, "_direct_url_download", failing_direct)

        await downloader.start_download("seastar-medium-4bit")
        await downloader.wait_for("seastar-medium-4bit")

        progress = downloader.get_progress("seastar-medium-4bit")
        assert progress.status == ModelStatus.ERROR
        assert "HF 오류" in progress.error_message
        assert "네트워크 오류" in progress.error_message


# === 명시적 prefer_direct ===


class TestPreferDirect:
    async def test_prefer_direct_True_이면_huggingface_hub_건너뜀(
        self, downloader, monkeypatch
    ):
        """prefer_direct=True 이면 HF 호출 없이 바로 direct URL 로 간다."""
        hf_called = {"n": 0}
        direct_called = {"n": 0}

        async def fake_hf(spec, job):
            hf_called["n"] += 1

        async def successful_direct(spec, job):
            direct_called["n"] += 1
            from core.stt_model_registry import get_manual_import_dir

            target = Path(get_manual_import_dir(spec))
            target.mkdir(parents=True, exist_ok=True)
            (target / "config.json").write_text("{}")
            (target / "weights.safetensors").write_bytes(b"x")

        monkeypatch.setattr(downloader, "_hf_download", fake_hf)
        monkeypatch.setattr(downloader, "_direct_url_download", successful_direct)

        await downloader.start_download("seastar-medium-4bit", prefer_direct=True)
        await downloader.wait_for("seastar-medium-4bit")

        progress = downloader.get_progress("seastar-medium-4bit")
        assert progress.status == ModelStatus.READY
        assert hf_called["n"] == 0  # HF 호출 완전 건너뜀
        assert direct_called["n"] == 1


# === _direct_url_download 실제 동작 (urllib mock) ===


class TestDirectURLDownload:
    async def test_urllib_스트리밍_다운로드(self, downloader):
        """실제 _direct_url_download 가 urllib 로 두 파일을 받아 배치한다."""
        from core.stt_model_registry import (
            get_by_id,
            get_manual_import_dir,
        )
        from core.stt_model_downloader import DownloadJob

        spec = get_by_id("seastar-medium-4bit")
        job = DownloadJob(
            job_id="test",
            model_id=spec.id,
            status=ModelStatus.DOWNLOADING,
        )

        fake_contents = {
            "config.json": b'{"n_mels": 80, "test": true}',
            "weights.safetensors": b"FAKE_WEIGHTS_DATA" * 500,
        }

        with patch(
            "urllib.request.urlopen",
            side_effect=_make_fake_urlopen(fake_contents),
        ):
            await downloader._direct_url_download(spec, job)

        target = Path(get_manual_import_dir(spec))
        assert (target / "config.json").read_bytes() == fake_contents["config.json"]
        assert (
            (target / "weights.safetensors").read_bytes()
            == fake_contents["weights.safetensors"]
        )
        # 임시 파일이 남아있지 않아야 함
        assert not list(target.glob("*.tmp"))

    async def test_HTTP_에러는_RuntimeError_로_래핑(self, downloader):
        """HTTPError 가 발생하면 한국어 메시지 RuntimeError 로 변환된다."""
        from urllib.error import HTTPError

        from core.stt_model_registry import get_by_id
        from core.stt_model_downloader import DownloadJob

        spec = get_by_id("seastar-medium-4bit")
        job = DownloadJob(
            job_id="test",
            model_id=spec.id,
            status=ModelStatus.DOWNLOADING,
        )

        def raising_urlopen(req, timeout=None):
            raise HTTPError(
                url="https://example.com/test",
                code=403,
                msg="Forbidden",
                hdrs=None,  # type: ignore
                fp=None,
            )

        with patch("urllib.request.urlopen", side_effect=raising_urlopen):
            with pytest.raises(RuntimeError, match="HTTP 403"):
                await downloader._direct_url_download(spec, job)


# === API 엔드포인트 ===


class TestDownloadDirectEndpoint:
    @pytest.fixture
    def client_with_downloader(self, tmp_models_dir):
        app = FastAPI()
        app.include_router(router)
        app.state.stt_downloader = STTModelDownloader(
            models_dir=tmp_models_dir
        )
        return TestClient(app)

    def test_download_direct_엔드포인트_202_반환(
        self, client_with_downloader, monkeypatch
    ):
        """POST /download-direct 가 prefer_direct=True 로 다운로더를 호출한다."""
        calls = []

        async def fake_start(self, model_id, *, prefer_direct=False):
            calls.append({"model_id": model_id, "prefer_direct": prefer_direct})
            return "stt-download-test-1"

        monkeypatch.setattr(
            STTModelDownloader, "start_download", fake_start
        )

        resp = client_with_downloader.post(
            "/api/stt-models/seastar-medium-4bit/download-direct"
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["method"] == "direct_url"
        assert data["job_id"] == "stt-download-test-1"

        assert len(calls) == 1
        assert calls[0]["model_id"] == "seastar-medium-4bit"
        assert calls[0]["prefer_direct"] is True

    def test_download_direct_404_for_unknown_model(
        self, client_with_downloader
    ):
        resp = client_with_downloader.post(
            "/api/stt-models/nonexistent/download-direct"
        )
        assert resp.status_code == 404
