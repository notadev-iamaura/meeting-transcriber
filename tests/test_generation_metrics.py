"""생성 계측이 응답 내용을 노출하거나 요청 간 상태를 섞지 않는지 검증한다."""

from __future__ import annotations

import sys
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.llm_backend import ThreadBoundLLMBackend
from core.mlx_client import MLXBackend, MLXGenerationError


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> MLXBackend:
    """실제 모델·Metal을 불러오지 않는 생성 테스트용 백엔드를 만든다."""
    instance = MLXBackend.__new__(MLXBackend)
    instance._model = object()
    instance._tokenizer = object()
    instance._processor = object()
    instance._temperature = 0.3
    instance._max_tokens = 96
    instance._model_name = "synthetic-test-model"
    instance._use_vlm = True
    instance._vlm_generate = None
    instance._vlm_stream_generate = None
    instance._vlm_prompt_cache_state = None
    instance._lm_prompt_cache = None
    instance._last_system_prompt_hash = None
    instance._generation_metrics = {}
    monkeypatch.setattr(instance, "_apply_chat_template", lambda msgs: msgs[-1]["content"])
    monkeypatch.setitem(sys.modules, "mlx_vlm.generate", SimpleNamespace(PromptCacheState=object))
    return instance


def test_metrics_initial_missing_and_copy(backend: MLXBackend) -> None:
    """미측정은 0이 아닌 None이며 반환 사본 수정은 내부 계측을 바꾸지 않는다."""
    first = backend.get_generation_metrics()
    assert len(first) == 6
    assert all(value is None for value in first.values())
    first["generation_tokens"] = 100
    assert backend.get_generation_metrics()["generation_tokens"] is None


@pytest.mark.parametrize("invalid", [True, False, -1, float("nan"), float("inf"), "42", {}])
def test_metrics_reject_nonfinite_negative_or_nonnumeric(
    backend: MLXBackend, invalid: Any
) -> None:
    """유효하지 않은 계측과 텍스트 필드는 사용자에게 전달되지 않는다."""
    backend._capture_generation_metrics(
        SimpleNamespace(
            prompt_tokens=invalid,
            generation_tokens=invalid,
            prompt_tps=invalid,
            generation_tps=invalid,
            peak_memory=invalid,
            text="합성 응답",
            prompt="합성 입력",
        )
    )
    metrics = backend.get_generation_metrics()
    assert all(value is None for value in metrics.values())
    assert "text" not in metrics
    assert "prompt" not in metrics


@pytest.mark.parametrize("method", ["chat", "chat_stream"])
@pytest.mark.parametrize("temperature,expected", [(None, 0.3), (0.0, 0.0), (0.7, 0.7)])
def test_vlm_temperature_and_latest_metrics(
    backend: MLXBackend, method: str, temperature: float | None, expected: float
) -> None:
    """기본·명시적 0·사용자 온도를 VLM에 전달하고 이전 요청 계측은 남기지 않는다."""
    backend._generation_metrics = {"generation_tokens": 999, "prompt_tps": 42.0}
    result = SimpleNamespace(text="고정 합성 응답", prompt_tokens=12, generation_tokens=3)
    generate = MagicMock(return_value=result)
    backend._vlm_generate = generate
    output = getattr(backend, method)(
        messages=[{"role": "user", "content": "고정 합성 입력"}],
        temperature=temperature,
        max_tokens=37,
    )
    if method == "chat_stream":
        output = "".join(output)
    assert output == result.text
    assert generate.call_args.kwargs["temperature"] == expected
    assert generate.call_args.kwargs["max_tokens"] == 37
    metrics = backend.get_generation_metrics()
    assert metrics["generation_tokens"] == 3
    assert metrics["prompt_tokens"] == 12
    assert metrics["prompt_tps"] is None
    assert isinstance(metrics["duration_seconds"], float)
    assert metrics["duration_seconds"] >= 0


def test_vlm_stream_chunks_keep_last_numeric_metrics(backend: MLXBackend) -> None:
    """스트림 텍스트 조각을 합쳐 반환하며 마지막 누적 계측을 기록한다."""
    backend._vlm_stream_generate = MagicMock(
        return_value=iter(
            [
                SimpleNamespace(text="합성 ", prompt_tokens=12, generation_tokens=1),
                SimpleNamespace(
                    text="응답",
                    prompt_tokens=12,
                    generation_tokens=2,
                    generation_tps=4.0,
                    peak_memory=5.5,
                ),
            ]
        )
    )
    assert backend.chat(messages=[{"role": "user", "content": "입력"}]) == "합성 응답"
    stats = backend.get_generation_metrics()
    assert stats["generation_tokens"] == 2
    assert stats["generation_tps"] == 4.0
    assert stats["peak_memory_gb"] == 5.5


def test_failed_vlm_generation_clears_cache_for_retry(backend: MLXBackend) -> None:
    """부분 생성 실패 후 같은 프롬프트 재시도는 오염된 캐시를 재사용하지 않는다."""
    seen_caches = []

    def generate(*args: Any, **kwargs: Any) -> Any:
        seen_caches.append(kwargs["prompt_cache_state"])
        if len(seen_caches) == 1:
            raise RuntimeError("synthetic generation failure")
        yield SimpleNamespace(text="정상", generation_tokens=1)

    backend._vlm_stream_generate = generate
    messages = [{"role": "system", "content": "고정"}, {"role": "user", "content": "입력"}]
    with pytest.raises(MLXGenerationError, match="synthetic generation failure"):
        backend.chat(messages=messages)
    assert backend._vlm_prompt_cache_state is None
    assert backend.chat(messages=messages) == "정상"
    assert seen_caches[0] is not seen_caches[1]


def test_raw_lm_requests_do_not_inherit_previous_output(
    backend: MLXBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """같은 시스템 프롬프트여도 새 요청의 raw cache에 이전 응답이 들어가지 않는다."""
    backend._use_vlm = False
    caches = []

    def generate(*args: Any, **kwargs: Any) -> str:
        cache = kwargs["prompt_cache"]
        assert cache == []
        caches.append(cache)
        cache.extend([kwargs["prompt"], "synthetic previous response"])
        return kwargs["prompt"]

    monkeypatch.setitem(sys.modules, "mlx_lm", SimpleNamespace(generate=generate))
    monkeypatch.setitem(
        sys.modules, "mlx_lm.models.cache", SimpleNamespace(make_prompt_cache=lambda model: [])
    )
    monkeypatch.setitem(
        sys.modules, "mlx_lm.sample_utils", SimpleNamespace(make_sampler=lambda **kwargs: object())
    )
    for text in ("synthetic A", "synthetic B"):
        assert (
            backend.chat(
                messages=[
                    {"role": "system", "content": "same system"},
                    {"role": "user", "content": text},
                ]
            )
            == text
        )
    assert caches[0] is not caches[1]
    assert backend.get_generation_metrics()["prompt_tokens"] is None


def test_thread_bound_metrics_read_on_owner_thread_and_return_copy() -> None:
    """계측 조회는 생성과 같은 전용 스레드에서 실행되며 사본만 반환한다."""
    owner_threads = []
    values = {"generation_tokens": 7}

    class FakeBackend:
        """스레드 소유권만 검사하는 가벼운 백엔드."""

        def __init__(self) -> None:
            owner_threads.append(threading.get_ident())

        def get_generation_metrics(self) -> dict[str, int]:
            assert threading.get_ident() == owner_threads[0]
            return values

        def cleanup(self) -> None:
            assert threading.get_ident() == owner_threads[0]

    wrapper = ThreadBoundLLMBackend(FakeBackend)
    try:
        metrics = wrapper.get_generation_metrics()
        metrics["generation_tokens"] = 999
        assert wrapper.get_generation_metrics()["generation_tokens"] == 7
        assert owner_threads[0] != threading.get_ident()
    finally:
        wrapper.cleanup()
