"""STT 모델 다운로더 테스트

STTModelDownloader 의 비동기 HF 다운로드 + 검증 파이프라인을 검증한다.
모든 지원 모델은 사전 양자화된 HF repo 를 사용하므로 로컬 양자화 경로는 없다.
huggingface_hub 호출은 전부 mock 처리한다.
"""

from __future__ import annotations

import asyncio

import pytest

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def tmp_models_dir(tmp_path):
    d = tmp_path / "stt_models"
    d.mkdir()
    return d


@pytest.fixture
def downloader(tmp_models_dir):
    from core.stt_model_downloader import STTModelDownloader

    return STTModelDownloader(models_dir=tmp_models_dir)


# ============================================================
# Tests
# ============================================================


class TestSTTModelDownloader:
    async def test_다운로드_시작시_job_id_반환(self, downloader, monkeypatch):
        """start_download 호출 시 job_id를 즉시 반환해야 한다."""

        async def fake_hf(spec, job):
            await asyncio.sleep(0.05)

        monkeypatch.setattr(downloader, "_hf_download", fake_hf)
        # 검증도 모킹 (실제 HF 캐시 확인하지 않음)
        monkeypatch.setattr(downloader, "_verify", lambda spec: True)

        job_id = await downloader.start_download("seastar-medium-4bit")
        assert job_id.startswith("stt-download-seastar-medium-4bit-")

        progress = downloader.get_progress("seastar-medium-4bit")
        assert progress is not None
        assert progress.model_id == "seastar-medium-4bit"

        # 백그라운드 태스크 완료 대기
        await downloader.wait_for("seastar-medium-4bit")

    async def test_동일_모델_중복_다운로드시_Conflict(self, downloader, monkeypatch):
        from core.stt_model_downloader import DownloadConflictError

        async def slow_hf(spec, job):
            await asyncio.sleep(0.3)

        monkeypatch.setattr(downloader, "_hf_download", slow_hf)
        monkeypatch.setattr(downloader, "_verify", lambda spec: True)

        await downloader.start_download("seastar-medium-4bit")
        with pytest.raises(DownloadConflictError):
            await downloader.start_download("seastar-medium-4bit")

        await downloader.wait_for("seastar-medium-4bit")

    async def test_다운로드_완료시_status_READY(self, downloader, monkeypatch):
        from core.stt_model_status import ModelStatus

        async def fake_hf(spec, job):
            pass

        monkeypatch.setattr(downloader, "_hf_download", fake_hf)
        monkeypatch.setattr(downloader, "_verify", lambda spec: True)

        await downloader.start_download("seastar-medium-4bit")
        await downloader.wait_for("seastar-medium-4bit")

        progress = downloader.get_progress("seastar-medium-4bit")
        assert progress.status == ModelStatus.READY
        assert progress.progress_percent == 100
        assert progress.completed_at is not None
        assert progress.error_message is None

    async def test_HF_실패시_direct_URL_폴백_후_성공(self, downloader, monkeypatch):
        """HF 다운로드 실패 시 direct URL 로 자동 폴백하여 성공해야 한다.

        구체적인 폴백 시나리오는 test_stt_direct_download.py 에서 더 상세히 검증한다.
        """
        from core.stt_model_status import ModelStatus

        async def failing_hf(spec, job):
            raise RuntimeError("네트워크 오류")

        async def successful_direct(spec, job):
            pass

        monkeypatch.setattr(downloader, "_hf_download", failing_hf)
        monkeypatch.setattr(downloader, "_direct_url_download", successful_direct)
        monkeypatch.setattr(downloader, "_verify", lambda spec: True)

        await downloader.start_download("seastar-medium-4bit")
        await downloader.wait_for("seastar-medium-4bit")

        progress = downloader.get_progress("seastar-medium-4bit")
        # 폴백 덕분에 최종 상태는 READY
        assert progress.status == ModelStatus.READY

    async def test_검증_실패시_ERROR(self, downloader, monkeypatch):
        """HF 다운로드는 성공했지만 _verify 가 False 이면 ERROR."""
        from core.stt_model_status import ModelStatus

        async def fake_hf(spec, job):
            pass

        monkeypatch.setattr(downloader, "_hf_download", fake_hf)
        monkeypatch.setattr(downloader, "_verify", lambda spec: False)

        await downloader.start_download("seastar-medium-4bit")
        await downloader.wait_for("seastar-medium-4bit")

        progress = downloader.get_progress("seastar-medium-4bit")
        assert progress.status == ModelStatus.ERROR
        assert "검증 실패" in progress.error_message

    async def test_여러_모델_순차_다운로드(self, downloader, monkeypatch):
        """첫 다운로드 완료 후 다른 모델 다운로드가 가능해야 한다."""
        from core.stt_model_status import ModelStatus

        async def fake_hf(spec, job):
            pass

        monkeypatch.setattr(downloader, "_hf_download", fake_hf)
        monkeypatch.setattr(downloader, "_verify", lambda spec: True)

        await downloader.start_download("seastar-medium-4bit")
        await downloader.wait_for("seastar-medium-4bit")

        await downloader.start_download("ghost613-turbo-4bit")
        await downloader.wait_for("ghost613-turbo-4bit")

        assert downloader.get_progress("seastar-medium-4bit").status == ModelStatus.READY
        assert downloader.get_progress("ghost613-turbo-4bit").status == ModelStatus.READY

    async def test_진행률_0에서_100까지_증가(self, downloader, monkeypatch):
        """진행률이 최종 100 으로 끝나야 한다."""
        observed: list[int] = []

        async def fake_hf(spec, job):
            observed.append(job.progress_percent)

        monkeypatch.setattr(downloader, "_hf_download", fake_hf)
        monkeypatch.setattr(downloader, "_verify", lambda spec: True)

        await downloader.start_download("seastar-medium-4bit")
        await downloader.wait_for("seastar-medium-4bit")

        progress = downloader.get_progress("seastar-medium-4bit")
        assert progress.progress_percent == 100
        # _hf_download 진입 시점에 이미 >= 10
        assert observed and observed[0] >= 10

    async def test_알_수_없는_모델은_ValueError(self, downloader):
        with pytest.raises(ValueError, match="알 수 없는 STT 모델"):
            await downloader.start_download("does-not-exist")
