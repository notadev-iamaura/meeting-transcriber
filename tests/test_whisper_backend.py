"""Whisper 스레드 고정과 native lease 정리 계약을 검증한다."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from core.model_manager import ModelLoadManager, await_native_inference
from core.whisper_backend import ThreadBoundWhisperBackend


@pytest.mark.asyncio
async def test_fallback_and_cleanup_stay_on_loader_thread() -> None:
    """첫 실패 뒤 다른 호출자 스레드에서 재사용해도 소유 스레드를 유지한다."""
    threads = []
    holder = SimpleNamespace(model=object(), model_path="model")
    audio = SimpleNamespace(mel_filters=Mock(), hanning=Mock())

    def transcribe(path: str, **kwargs: object) -> dict:
        threads.append(threading.get_ident())
        if kwargs.get("beam_size"):
            raise NotImplementedError("beam")
        return {"text": "검증"}

    def loader() -> object:
        threads.append(threading.get_ident())
        return SimpleNamespace(transcribe=transcribe)

    def module(name: str) -> object:
        threads.append(threading.get_ident())
        return SimpleNamespace(ModelHolder=holder) if name.endswith("transcribe") else audio

    backend = ThreadBoundWhisperBackend(loader)
    assert not backend.supports_batch_size
    with patch("core.whisper_backend.importlib.import_module", side_effect=module):
        with pytest.raises(NotImplementedError):
            await asyncio.to_thread(backend.transcribe, "sample", beam_size=5)
        assert await asyncio.to_thread(backend.transcribe, "sample") == {"text": "검증"}
        await asyncio.to_thread(backend.cleanup)
    assert len(set(threads)) == 1
    assert holder.model is None and holder.model_path is None
    audio.mel_filters.cache_clear.assert_called_once()
    audio.hanning.cache_clear.assert_called_once()
    backend.cleanup()
    with pytest.raises(RuntimeError, match="정리"):
        backend.transcribe("sample")


@pytest.mark.asyncio
async def test_timeout_retains_whisper_until_native_worker_finishes() -> None:
    """중첩 전용 worker도 timeout 이후 끝날 때까지 모델 lease로 보호한다."""
    entered = threading.Event()
    release = threading.Event()
    cleaned = threading.Event()

    def transcribe(path: str, **kwargs: object) -> dict:
        entered.set()
        assert release.wait(5)
        return {}

    backend = ThreadBoundWhisperBackend(lambda: SimpleNamespace(transcribe=transcribe))
    original_cleanup = backend.cleanup

    def cleanup() -> None:
        assert release.is_set()
        original_cleanup()
        cleaned.set()

    holder = SimpleNamespace(model=object(), model_path="model")
    audio = SimpleNamespace(mel_filters=Mock(), hanning=Mock())
    manager = ModelLoadManager(gpu_cache_cleanup_enabled=False)
    with (
        patch.object(backend, "cleanup", side_effect=cleanup),
        patch(
            "core.whisper_backend.importlib.import_module",
            side_effect=[
                SimpleNamespace(ModelHolder=holder),
                audio,
            ],
        ),
    ):

        async def infer() -> None:
            async with manager.acquire("whisper", lambda: backend):
                await await_native_inference(backend.transcribe, "sample")

        task = asyncio.create_task(infer())
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(task, timeout=0.01)
            assert manager.get_status()["native_cleanup_pending"]
            assert manager.current_model is backend
            assert not cleaned.is_set()
        finally:
            release.set()
        for _ in range(200):
            if cleaned.is_set():
                break
            await asyncio.sleep(0.01)
        assert cleaned.is_set()
        assert manager.current_model is None
