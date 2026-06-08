"""G1 후속④ — e5 쿼리 임베딩의 발열/동시성 가드(뮤텍스 직렬화) 검증.

불변식 #7(단일 대형모델·피크RAM≤9.5GB·발열): 위키 시맨틱 검색의 e5 쿼리 임베딩은
파이프라인 대형 모델과 동시에 메모리에 적재되면 안 된다. G1 의 기본 쿼리 임베더는
`ModelLoadManager.acquire("e5_search")` 로 모델 로드를 직렬화해 이를 보장한다.

여기서는 두 가지를 검증한다(둘 다 실 e5/실 ChromaDB 없이 결정적):
1. **배선**: 기본 쿼리 임베더가 실제로 `acquire("e5_search", ...)` 를 경유한다.
2. **실 직렬화**: 파이프라인 모델이 매니저를 점유 중이면 e5 쿼리 임베딩은 그 점유가
   풀릴 때까지 대기한다(동시 적재 불가). 실 `ModelLoadManager` + 가짜
   `sentence_transformers` 로 470MB e5 로드 없이 직렬화 동작만 검증한다.

`ModelLoadManager` 자체의 lock 직렬화는 `tests/test_model_manager.py` 가 이미
검증하므로, 여기서는 *G1 코드 경로가 그 lock 을 경유*함을 단언한다.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import numpy as np
import pytest

from config import get_config
from core.wiki.semantic_search import _make_default_embed_query

pytestmark = pytest.mark.asyncio


class _FakeEmbeddingModel:
    """e5 대체 — encode 가 고정 벡터를 반환(실 모델 로드 회피)."""

    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    def encode(self, texts: list[str], **_: Any) -> Any:
        # 실 e5 처럼 (batch, dim) ndarray 반환 → _encode 가 vec[0].tolist() 사용.
        return np.array([[0.5, -0.5, 0.25]])


async def test_e5_쿼리임베더가_model_manager_뮤텍스를_경유한다() -> None:
    """G1 기본 쿼리 임베더는 e5 로드를 acquire('e5_search') 로 직렬화한다(배선)."""
    acquired: list[str] = []

    class _SpyCtx:
        async def __aenter__(self) -> Any:
            return _FakeEmbeddingModel()

        async def __aexit__(self, *_: Any) -> bool:
            return False

    class _SpyManager:
        """acquire 호출만 기록 — loader 를 호출하지 않아 실 e5 import 도 없음."""

        def acquire(self, name: str, loader: Any, **_: Any) -> _SpyCtx:
            acquired.append(name)
            return _SpyCtx()

    embed = _make_default_embed_query(get_config(), _SpyManager())
    vec = await embed("재정 규모는 얼마로 정했나")

    # 정확히 'e5_search' 이름으로 뮤텍스를 1회 경유했는가(파이프라인 모델과 동일 lock)
    assert acquired == ["e5_search"]
    # 인코딩 결과를 그대로 반환했는가
    assert vec == pytest.approx([0.5, -0.5, 0.25])


async def test_파이프라인_점유중_e5쿼리임베딩은_직렬화_대기한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """파이프라인 모델이 매니저를 점유 중이면 e5 쿼리 임베딩은 풀릴 때까지 대기한다."""
    # sentence_transformers 를 가짜로 치환 — 실 e5(470MB) 로드 없이 직렬화만 검증.
    fake_mod = types.ModuleType("sentence_transformers")
    fake_mod.SentenceTransformer = _FakeEmbeddingModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)

    from core.model_manager import ModelLoadManager

    manager = ModelLoadManager(gpu_cache_cleanup_enabled=False)
    order: list[str] = []
    release_pipeline = asyncio.Event()

    async def hold_pipeline_model() -> None:
        # 파이프라인 대형 모델(예: Gemma)이 매니저를 점유한 상태를 모사.
        async with manager.acquire("pipeline_gemma", lambda: object()):
            order.append("pipeline_acquired")
            await release_pipeline.wait()
            order.append("pipeline_release")

    embed = _make_default_embed_query(get_config(), manager)

    async def run_e5_query() -> None:
        await asyncio.sleep(0.02)  # 파이프라인이 먼저 점유하도록 양보
        order.append("e5_request")
        await embed("재정 규모는 얼마로 정했나")  # acquire 에서 블록되어야 함
        order.append("e5_done")

    task_pipeline = asyncio.create_task(hold_pipeline_model())
    task_e5 = asyncio.create_task(run_e5_query())

    # 파이프라인이 점유 중인 동안 e5 는 임베딩을 완료하지 못한다(직렬화 대기 증명).
    await asyncio.sleep(0.05)
    assert "e5_done" not in order, f"점유 중인데 e5 가 임베딩됨(직렬화 실패): {order}"
    assert order[-1] == "e5_request"

    release_pipeline.set()  # 파이프라인 점유 해제
    await asyncio.gather(task_pipeline, task_e5)

    # e5 임베딩은 파이프라인 점유 해제 이후에야 완료된다(동시 적재 없음).
    assert order.index("pipeline_release") < order.index("e5_done"), order
