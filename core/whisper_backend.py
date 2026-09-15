"""Whisper의 지연 연산과 전역 캐시를 전용 스레드에서 관리한다."""

from __future__ import annotations

import gc
import importlib
import inspect
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

logger = logging.getLogger(__name__)


class ThreadBoundWhisperBackend:
    """모듈 로딩부터 캐시 해제까지 같은 스레드에서 실행한다."""

    def __init__(self, loader: Callable[[], Any]) -> None:
        """모듈을 전용 worker에서 로드한다."""
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx-whisper")
        self._closed = False
        try:
            self._module = self._executor.submit(loader).result()
        except BaseException:
            self._executor.shutdown(wait=True)
            raise

    @property
    def supports_batch_size(self) -> bool:
        """래핑 전 라이브러리의 명시적 batch_size 지원 여부를 반환한다."""
        try:
            return "batch_size" in inspect.signature(self._module.transcribe).parameters
        except (AttributeError, TypeError, ValueError):
            return False

    def transcribe(self, audio: str, **kwargs: Any) -> dict[str, Any]:
        """호출자 스레드와 무관하게 동일 worker에서 추론한다."""
        if self._closed:
            raise RuntimeError("Whisper backend가 이미 정리되었습니다")

        def run() -> dict[str, Any]:
            logger.debug(f"Whisper 추론 thread={threading.get_ident()}")
            try:
                return cast(dict[str, Any], self._module.transcribe(audio, **kwargs))
            except NotImplementedError:
                raise
            except Exception:
                logger.exception(f"Whisper native 추론 실패 thread={threading.get_ident()}")
                raise

        return self._executor.submit(run).result()

    def cleanup(self) -> None:
        """실제 추론 종료 후 모델과 스레드 소유 캐시를 해제한다."""
        if self._closed:
            return

        def clear() -> None:
            transcribe = importlib.import_module("mlx_whisper.transcribe")
            audio = importlib.import_module("mlx_whisper.audio")
            transcribe.ModelHolder.model = None
            transcribe.ModelHolder.model_path = None
            audio.mel_filters.cache_clear()
            audio.hanning.cache_clear()
            self._module = None
            gc.collect()

        try:
            self._executor.submit(clear).result()
        finally:
            self._closed = True
            self._executor.shutdown(wait=True)
