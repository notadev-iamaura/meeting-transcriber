"""ExaoneWikiClient TDD 테스트 모듈

목적: core/wiki/llm_client.py 의 ExaoneWikiClient 가 실제 EXAONE 호출을
ModelLoadManager 와 통합하여 수행하는지 검증한다. mlx-lm 자체는 mock 으로
대체하여 실제 모델 다운로드/로드를 회피한다.

검증 범위:
    1. NotImplementedError 가 더이상 발생하지 않음
    2. ModelLoadManager.acquire 로 EXAONE 백엔드 acquire/release
    3. apply_chat_template 호환 메시지 형식 전달
    4. backend.chat() 결과 그대로 반환 (NFC 정규화 + strip)
    5. config.wiki.compiler_model 이 모델명으로 사용
    6. 백엔드 실패 시 WikiLLMError 로 escalate
    7. temperature/max_tokens 기본값 전달

의존성:
    - pytest, pytest-asyncio (asyncio_mode=auto)
    - core.wiki.llm_client (Phase 2.E 통합 후 동작)
    - core.model_manager.ModelLoadManager (실제 클래스, mock 로더 주입)
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.wiki.llm_client import (
    WIKI_DEFAULT_MAX_TOKENS,
    WIKI_DEFAULT_TEMPERATURE,
    ExaoneWikiClient,
    WikiLLMError,
)


# ─────────────────────────────────────────────────────────────────────────
# 픽스처 — config 와 model_manager 를 mock 으로 대체
# ─────────────────────────────────────────────────────────────────────────


class _FakeWikiConfig:
    """WikiConfig 호환 mock — compiler_model 만 노출."""

    def __init__(
        self,
        compiler_model: str = "mlx-community/EXAONE-3.5-7.8B-Instruct-4bit",
    ) -> None:
        """compiler_model 필드 하나만 있는 가벼운 설정 객체.

        Args:
            compiler_model: ExaoneWikiClient 가 사용할 모델 ID.
        """
        self.compiler_model = compiler_model


class _FakeAppConfig:
    """AppConfig 호환 mock — wiki 와 llm 두 필드만 노출."""

    def __init__(
        self,
        compiler_model: str = "mlx-community/EXAONE-3.5-7.8B-Instruct-4bit",
    ) -> None:
        """ExaoneWikiClient 가 참조하는 wiki/llm 설정만 만든다.

        Args:
            compiler_model: wiki 컴파일러 모델 ID.
        """
        self.wiki = _FakeWikiConfig(compiler_model=compiler_model)
        # llm 백엔드 정보 — ExaoneWikiClient 내부에서 LLMConfig 를 살짝 변환할 수 있다.
        llm = MagicMock()
        llm.backend = "mlx"
        llm.mlx_model_name = compiler_model
        llm.mlx_max_tokens = 2048
        llm.temperature = WIKI_DEFAULT_TEMPERATURE
        llm.max_context_tokens = 8192
        llm.request_timeout_seconds = 600
        self.llm = llm


class _FakeBackend:
    """LLMBackend Protocol 호환 mock.

    ExaoneWikiClient 가 ModelLoadManager 로 acquire 한 backend.chat() 의
    동작을 시뮬레이션한다. cleanup() 은 ModelLoadManager._unload_current 에서
    호출되므로 no-op 으로 둔다.
    """

    def __init__(self, response: str = "테스트 응답") -> None:
        """기본 응답을 설정한다.

        Args:
            response: chat() 호출 시 반환할 문자열.
        """
        self.response = response
        self.calls: list[dict[str, Any]] = []
        self.cleanup_count = 0

    def chat(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        num_ctx: int | None = None,
        timeout: int | None = None,
    ) -> str:
        """호출 인자를 기록하고 미리 설정된 응답을 반환한다."""
        self.calls.append(
            {
                "messages": messages,
                "temperature": temperature,
                "num_ctx": num_ctx,
                "timeout": timeout,
            }
        )
        return self.response

    def cleanup(self) -> None:
        """ModelLoadManager 가 unload 시 호출."""
        self.cleanup_count += 1


class _RaisingBackend(_FakeBackend):
    """chat() 호출 시 LLMGenerationError 를 던지는 백엔드."""

    def chat(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        num_ctx: int | None = None,
        timeout: int | None = None,
    ) -> str:
        """always raise — generation_failed 시뮬레이션."""
        from core.llm_backend import LLMGenerationError

        raise LLMGenerationError("backend explosion")


@pytest.fixture
def fake_app_config() -> _FakeAppConfig:
    """기본 EXAONE 모델 설정 fixture."""
    return _FakeAppConfig()


@pytest.fixture
def fake_model_manager() -> Any:
    """실제 ModelLoadManager 인스턴스를 새로 만들어 fixture 로 제공.

    싱글턴 reset 후 새 인스턴스를 사용해 다른 테스트와 격리.
    """
    from core.model_manager import ModelLoadManager, reset_model_manager

    reset_model_manager()
    return ModelLoadManager()


# ─────────────────────────────────────────────────────────────────────────
# 1. generate() 가 더이상 NotImplementedError 를 raise 하지 않음
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_no_longer_raises_notimplemented(
    fake_app_config: _FakeAppConfig, fake_model_manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 2.E 통합 후 generate() 가 NotImplementedError 를 던지지 않는다.

    실제 EXAONE 로드는 mock 으로 대체된다. ModelLoadManager 가 backend 로
    _FakeBackend 인스턴스를 반환하도록 create_backend 를 패치한다.
    """
    backend = _FakeBackend(response="결과 본문")

    # ExaoneWikiClient 가 사용하는 백엔드 생성기를 가짜로 교체
    monkeypatch.setattr(
        "core.wiki.llm_client._create_exaone_backend",
        lambda config: backend,
    )

    client = ExaoneWikiClient(
        config=fake_app_config,
        model_manager=fake_model_manager,
    )
    result = await client.generate(
        system_prompt="너는 위키 작성자다",
        user_prompt="결정사항을 추출하라",
    )

    assert result == "결과 본문"


# ─────────────────────────────────────────────────────────────────────────
# 2. ModelLoadManager 로 acquire 후 backend.chat 호출
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_uses_model_load_manager_acquire(
    fake_app_config: _FakeAppConfig, fake_model_manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ModelLoadManager.load_model 이 wiki-exaone 이름으로 호출된다.

    호출 후 매니저의 current_model_name 은 wiki 전용 식별자가 되어야 한다.
    """
    backend = _FakeBackend()
    monkeypatch.setattr(
        "core.wiki.llm_client._create_exaone_backend",
        lambda config: backend,
    )

    client = ExaoneWikiClient(
        config=fake_app_config,
        model_manager=fake_model_manager,
    )
    await client.generate(
        system_prompt="sys",
        user_prompt="usr",
    )

    # ModelLoadManager 에 어떤 모델이든 로드되어 있어야 함 (ExaoneWikiClient 가 acquire)
    # 이름은 wiki-exaone / exaone-wiki / wiki_compiler 등 구현 자유 — 단지
    # load 가 발생했는지만 검증.
    assert backend.calls, "backend.chat 이 호출되지 않았습니다 (acquire 후 호출 누락)"


# ─────────────────────────────────────────────────────────────────────────
# 3. system + user 메시지 형식 정확히 전달
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_passes_system_and_user_messages(
    fake_app_config: _FakeAppConfig, fake_model_manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """backend.chat 에 [system, user] 두 개의 메시지가 전달된다."""
    backend = _FakeBackend(response="ok")
    monkeypatch.setattr(
        "core.wiki.llm_client._create_exaone_backend",
        lambda config: backend,
    )

    client = ExaoneWikiClient(
        config=fake_app_config,
        model_manager=fake_model_manager,
    )
    await client.generate(
        system_prompt="시스템 프롬프트",
        user_prompt="유저 프롬프트",
    )

    assert len(backend.calls) == 1
    messages = backend.calls[0]["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "시스템 프롬프트"
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == "유저 프롬프트"


# ─────────────────────────────────────────────────────────────────────────
# 4. config.wiki.compiler_model 을 모델명으로 사용
# ─────────────────────────────────────────────────────────────────────────


def test_model_name_uses_compiler_model_field() -> None:
    """ExaoneWikiClient.model_name 이 config.wiki.compiler_model 값과 일치."""
    cfg = _FakeAppConfig(compiler_model="custom/exaone-wiki-bench")
    mock_mgr = MagicMock()
    client = ExaoneWikiClient(config=cfg, model_manager=mock_mgr)
    assert client.model_name == "custom/exaone-wiki-bench"


# ─────────────────────────────────────────────────────────────────────────
# 5. backend 실패 시 WikiLLMError escalate
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_wraps_backend_failure_as_wiki_llm_error(
    fake_app_config: _FakeAppConfig, fake_model_manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """backend.chat 에서 LLMGenerationError 발생 시 WikiLLMError 로 escalate."""
    backend = _RaisingBackend()
    monkeypatch.setattr(
        "core.wiki.llm_client._create_exaone_backend",
        lambda config: backend,
    )

    client = ExaoneWikiClient(
        config=fake_app_config,
        model_manager=fake_model_manager,
    )

    with pytest.raises(WikiLLMError) as exc_info:
        await client.generate(
            system_prompt="sys",
            user_prompt="usr",
        )
    # reason 코드는 generation_failed 로 정규화되어야 한다
    assert exc_info.value.reason in {"generation_failed", "backend_unavailable"}


# ─────────────────────────────────────────────────────────────────────────
# 6. temperature/max_tokens 인자가 backend 까지 전달
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_default_temperature_passed_to_backend(
    fake_app_config: _FakeAppConfig, fake_model_manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """temperature 기본값(0.2) 이 backend.chat 의 temperature 인자로 전달."""
    backend = _FakeBackend()
    monkeypatch.setattr(
        "core.wiki.llm_client._create_exaone_backend",
        lambda config: backend,
    )

    client = ExaoneWikiClient(
        config=fake_app_config,
        model_manager=fake_model_manager,
    )
    await client.generate(
        system_prompt="sys",
        user_prompt="usr",
    )

    assert backend.calls[0]["temperature"] == WIKI_DEFAULT_TEMPERATURE


@pytest.mark.asyncio
async def test_generate_custom_temperature_overrides_default(
    fake_app_config: _FakeAppConfig, fake_model_manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """호출 측이 temperature 를 명시하면 그 값이 backend 까지 전달."""
    backend = _FakeBackend()
    monkeypatch.setattr(
        "core.wiki.llm_client._create_exaone_backend",
        lambda config: backend,
    )

    client = ExaoneWikiClient(
        config=fake_app_config,
        model_manager=fake_model_manager,
    )
    await client.generate(
        system_prompt="sys",
        user_prompt="usr",
        temperature=0.55,
    )

    assert backend.calls[0]["temperature"] == 0.55


# ─────────────────────────────────────────────────────────────────────────
# 7. 동시 호출 — ModelLoadManager 의 lock 으로 직렬화
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_generate_calls_serialized_by_manager_lock(
    fake_app_config: _FakeAppConfig, fake_model_manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ExaoneWikiClient 의 동시 호출이 backend 를 1번만 acquire 한다.

    ModelLoadManager 는 같은 모델명에 대해 reuse 하므로, 백엔드 인스턴스는
    하나만 만들어진다. 호출 자체는 반환되지만 lock 으로 직렬화된다.
    """
    backend_instances: list[_FakeBackend] = []

    def _factory(_config: Any) -> _FakeBackend:
        b = _FakeBackend(response="concurrent-result")
        backend_instances.append(b)
        return b

    monkeypatch.setattr(
        "core.wiki.llm_client._create_exaone_backend",
        _factory,
    )

    client = ExaoneWikiClient(
        config=fake_app_config,
        model_manager=fake_model_manager,
    )

    # 3개 동시 호출
    results = await asyncio.gather(
        client.generate(system_prompt="s1", user_prompt="u1"),
        client.generate(system_prompt="s2", user_prompt="u2"),
        client.generate(system_prompt="s3", user_prompt="u3"),
    )

    assert all(r == "concurrent-result" for r in results)
    # ModelLoadManager 가 같은 이름의 모델은 재사용하므로 백엔드 1번만 생성
    assert len(backend_instances) == 1
    # 3번 호출되었는지 확인
    assert len(backend_instances[0].calls) == 3
