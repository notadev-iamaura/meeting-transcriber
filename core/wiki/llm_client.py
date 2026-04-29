"""Wiki 전용 LLM 클라이언트 모듈

목적: Phase 2 의 WikiCompiler / extractors / guard 가 호출하는 LLM 추상화.
기존 `core/llm_backend.LLMBackend` 와 `core/mlx_client.MLXBackend` 위에 얇은
어댑터를 두어, ① 테스트에서는 `MockWikiLLMClient` 로 응답을 시뮬레이션하고,
② 실제 운영에서는 `MlxWikiClient` 가 ModelLoadManager 를 통해 Gemma 4 4bit
MLX 를 in-process 로 로드해 호출한다 (EXAONE 도 동일 인터페이스로 사용 가능).

설계 원칙:
    - 결정성 우선: temperature 기본 0.2 (PRD §5.4 한국어 고유명사 정확성).
    - 모델 통합: 사용자 환경(Gemma 4) 에 맞춰 8단계(Summarizer) 와 9단계(Wiki)
      가 동일 모델을 reuse 하므로 메모리 스왑 비용이 0. 단, ModelLoadManager
      식별자는 `wiki-llm` 으로 분리하여 명시적 격리 의도를 표현한다.
    - 프롬프트 인젝션 방어 책임 위치: sanitize_utterance_text 헬퍼로 호출자가 정제.

의존성:
    - core.config.WikiConfig (compiler_model, temperature 기본값)
    - core.model_manager.ModelLoadManager (단일 로드 보장)
    - core.llm_backend.* (간접 — _loader 내부에서만 import)
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────
# 1.1 LLM 결정성·안전 기본값 (모듈 상수)
# ─────────────────────────────────────────────────────────────────────────

# Wiki 컴파일 기본 temperature — PRD §5.4 한국어 고유명사 정확성 + 환각 최소화.
WIKI_DEFAULT_TEMPERATURE: float = 0.2

# 페이지 갱신용 max_tokens — 단일 페이지가 8K 토큰을 넘는 경우는 비정상이므로
# 보수적 상한.
WIKI_DEFAULT_MAX_TOKENS: int = 2048

# 프롬프트 인젝션 방어용 sanitize 패턴 — Utterance.text 에 들어 있을 수 있는
# LLM 제어 마커를 무력화한다.
#
# 커버하는 변형:
#   1. XML 스타일: <system>, </system>, <user>, <assistant>, <s>, <inst> (공백 허용)
#   2. llama/Mistral 특수 토큰: <|im_start|>, <|im_end|>, <|endoftext|>
#   3. [INST] / [/INST] 계열 — 공백 변형([ INST ], [ I N S T ]) 포함
#   4. alpaca/Vicuna 스타일: ### Instruction:, ### System:, ### Human:, ### Assistant:
#   5. 한국어 인젝션: 시스템:, [지시], 이전 지시 무시, 이전 지시를 무시
#   6. 영어 인젝션 문구: ignore previous instructions, disregard previous
#
# 처리 방식: 위험 토큰을 백틱으로 감싸서 의미는 보존하되 모델이 지시로 해석하지 못하게 함.
_INJECTION_TOKENS: re.Pattern[str] = re.compile(
    r"("
    # 1. XML 스타일 태그 (공백/슬래시 허용)
    r"<\s*/?\s*(system|user|assistant|s|inst)\s*>"
    # 2. llama/Mistral 특수 토큰
    r"|<\|im_start\|>"
    r"|<\|im_end\|>"
    r"|<\|endoftext\|>"
    # 3. [INST] 계열 — 단어 문자 사이 공백 변형 포함
    r"|\[\s*/?(?:[I]\s*[N]\s*[S]\s*[T]|INST)\s*\]"
    # 4. alpaca/Vicuna/ChatML 스타일 헤더
    r"|###\s*(Instruction|System|Human|Assistant)\s*:"
    # 5. 한국어 인젝션 문구
    r"|시스템\s*:"
    r"|\[지시\]"
    r"|이전\s*지시(?:를)?\s*무시"
    # 6. 영어 인젝션 문구 (단어 경계)
    r"|(?i:ignore\s+previous\s+instructions?)"
    r"|(?i:disregard\s+previous)"
    r")",
    re.IGNORECASE,
)

# 발화 텍스트 최대 길이 — LLM 컨텍스트 폭주 방지.
# 회의 발화 1건이 이 길이를 초과하는 경우는 비정상(스크립트 삽입 의심).
_MAX_UTTERANCE_CHARS: int = 8_000

# 다중 공백 정규화 패턴
_MULTI_SPACE: re.Pattern[str] = re.compile(r"\s+")


def sanitize_utterance_text(text: str) -> str:
    """Utterance.text 를 LLM 프롬프트에 안전하게 삽입하기 위해 정제한다.

    동작:
        1. 길이 상한 적용 (_MAX_UTTERANCE_CHARS 초과 시 truncate + 경고 로그).
        2. 위험 토큰을 백틱으로 감싸서 무력화 (의미는 보존).
        3. 연속 공백 정규화.

    커버하는 인젝션 변형:
        - <system>, <|im_start|> 등 XML/특수 토큰
        - [INST], [ INST ], [ I N S T ] (공백 변형)
        - ### Instruction:, ### System: (alpaca 스타일)
        - 시스템:, [지시], 이전 지시 무시 (한국어 변형)
        - ignore previous instructions (영어 변형)

    Args:
        text: corrector 가 출력한 발화 본문.

    Returns:
        프롬프트 삽입에 안전한 문자열. 최대 _MAX_UTTERANCE_CHARS 글자.
    """
    if not text:
        return ""
    # 길이 상한 — 비정상적으로 긴 발화는 LLM 컨텍스트 폭주 방지
    if len(text) > _MAX_UTTERANCE_CHARS:
        logger.warning(
            "sanitize_utterance_text: 발화 텍스트 길이 초과 (%d > %d), truncate",
            len(text),
            _MAX_UTTERANCE_CHARS,
        )
        text = text[:_MAX_UTTERANCE_CHARS]
    # 위험 토큰을 백틱으로 감싸서 무력화 (의미는 보존)
    cleaned = _INJECTION_TOKENS.sub(lambda m: f"`{m.group(0)}`", text)
    # 연속 공백 정규화
    cleaned = _MULTI_SPACE.sub(" ", cleaned).strip()
    return cleaned


# ─────────────────────────────────────────────────────────────────────────
# 1.2 Protocol — 테스트/실구현 모두 만족
# ─────────────────────────────────────────────────────────────────────────


@runtime_checkable
class WikiLLMClient(Protocol):
    """Wiki 컴파일 전용 LLM 추상화.

    이 Protocol 만이 `core/wiki/extractors/*.py`, `core/wiki/compiler.py`,
    `core/wiki/guard.py` 의 LLM 의존성 표면이다. 테스트에서는 `MockWikiLLMClient`
    로 응답을 시뮬레이션한다.
    """

    async def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = WIKI_DEFAULT_MAX_TOKENS,
        temperature: float = WIKI_DEFAULT_TEMPERATURE,
    ) -> str:
        """system + user 프롬프트로 단일 응답을 생성한다."""
        ...

    @property
    def model_name(self) -> str:
        """로깅용 모델 식별자."""
        ...


# ─────────────────────────────────────────────────────────────────────────
# 1.3 통합 에러
# ─────────────────────────────────────────────────────────────────────────


class WikiLLMError(Exception):
    """WikiLLMClient.generate() 가 백엔드 실패를 escalate 할 때 사용.

    예시 reason:
        - "backend_unavailable": LLMConnectionError wrap.
        - "generation_failed": LLMGenerationError 또는 타임아웃 wrap.
        - "model_load_failed": ModelLoadManager 가 EXAONE 로드 실패.
    """

    def __init__(self, reason: str, detail: str | None = None) -> None:
        """기술적 실패 사유 코드와 상세 메시지를 받는다.

        Args:
            reason: 안정적 코드(snake_case).
            detail: 사람이 읽는 메시지.
        """
        super().__init__(reason)
        self.reason: str = reason
        self.detail: str | None = detail


# ─────────────────────────────────────────────────────────────────────────
# 1.4 실제 구현 — Gemma 4 (또는 EXAONE) 4bit MLX (Phase 2.E 통합 완료)
# ─────────────────────────────────────────────────────────────────────────


# ModelLoadManager 에서 wiki 컴파일러 모델을 식별하기 위한 안정적인 이름.
# Summarizer 가 사용하는 식별자와 분리하여, 같은 모델(Gemma 4) 을 사용하더라도
# 9단계 진입 시 호출 의도를 명확히 표현한다. 사용자 환경(Gemma 4)에서는
# 8단계와 9단계가 같은 모델을 재사용하므로 추가 메모리 스왑 비용이 없다.
WIKI_LLM_MODEL_NAME: str = "wiki-llm"

# 하위 호환을 위해 이전 이름도 유지 (Phase 3 에서 제거 예정).
WIKI_EXAONE_MODEL_NAME: str = WIKI_LLM_MODEL_NAME


def _create_wiki_backend(config: Any) -> Any:
    """WikiCompilerV2 전용 4bit MLX 백엔드를 생성한다.

    내부 구현 상세:
        - `core.llm_backend.create_backend` 를 직접 호출하지 않고, wiki 전용
          LLMConfig 변환을 거친 뒤 MLX/Ollama 백엔드를 만든다.
        - config.wiki.compiler_model 을 mlx_model_name 으로 강제 주입하여
          summarizer 와 다른 모델을 쓰는 경우에도 정확히 분리된다.
        - 사용자 환경 기본값은 Gemma 4 (`mlx-community/gemma-4-e4b-it-4bit`).
          EXAONE 도 동일 코드 경로로 사용 가능하다.

    Args:
        config: AppConfig 또는 WikiConfig 인스턴스. AppConfig 면 wiki/llm 모두
            참조한다. WikiConfig 만 받아도 동작 (mlx 백엔드 가정).

    Returns:
        LLMBackend Protocol 을 만족하는 백엔드 인스턴스.

    Raises:
        WikiLLMError(reason="model_load_failed"): 백엔드 초기화 실패 시.
    """
    # 1) wiki.compiler_model 추출 — AppConfig 또는 WikiConfig 모두 지원
    wiki_cfg = getattr(config, "wiki", config)
    compiler_model: str = getattr(
        wiki_cfg,
        "compiler_model",
        "mlx-community/gemma-4-e4b-it-4bit",
    )

    # 2) llm 백엔드 설정 — wiki 전용 LLMConfig 파생
    base_llm = getattr(config, "llm", None)

    class _WikiLLMConfig:
        """LLMBackend 가 요구하는 최소 필드를 노출하는 어댑터."""

        backend: str = getattr(base_llm, "backend", "mlx") if base_llm else "mlx"
        # MLX 모델명: wiki.compiler_model 을 강제 사용 (Summarizer 와 분리)
        mlx_model_name: str = compiler_model
        mlx_max_tokens: int = (
            getattr(base_llm, "mlx_max_tokens", WIKI_DEFAULT_MAX_TOKENS)
            if base_llm
            else WIKI_DEFAULT_MAX_TOKENS
        )
        # Ollama 호환 필드 (사용 안 되더라도 create_backend 가 참조)
        model_name: str = compiler_model
        host: str = (
            getattr(base_llm, "host", "http://127.0.0.1:11434")
            if base_llm
            else ("http://127.0.0.1:11434")
        )
        temperature: float = (
            getattr(base_llm, "temperature", WIKI_DEFAULT_TEMPERATURE)
            if (base_llm)
            else WIKI_DEFAULT_TEMPERATURE
        )
        max_context_tokens: int = (
            getattr(base_llm, "max_context_tokens", 8192) if base_llm else 8192
        )
        request_timeout_seconds: int = (
            getattr(base_llm, "request_timeout_seconds", 600) if base_llm else 600
        )

    try:
        from core.llm_backend import create_backend

        return create_backend(_WikiLLMConfig())
    except Exception as exc:  # noqa: BLE001 — 모든 백엔드 실패를 통합 에러로 escalate
        raise WikiLLMError(
            reason="model_load_failed",
            detail=f"wiki 백엔드 초기화 실패: {exc}",
        ) from exc


# 하위 호환 alias (이전 이름 — Phase 3 에서 제거 예정).
_create_exaone_backend = _create_wiki_backend


class MlxWikiClient:
    """`WikiLLMClient` 의 MLX(Gemma 4 / EXAONE 등) 4bit 실제 구현.

    동작 흐름:
        1. generate() 호출마다 ModelLoadManager.acquire(WIKI_LLM_MODEL_NAME, ...)
           로 백엔드를 확보한다.
        2. system_prompt + user_prompt 를 [{"role":"system",...},{"role":"user",...}]
           로 변환해 backend.chat() 으로 전달.
        3. asyncio.to_thread 로 동기 chat() 을 비동기 컨텍스트에서 안전 호출.
        4. NFC 정규화 + strip 을 적용해 결정성 보장.

    Threading: ModelLoadManager.lock 으로 동시 로드 방지. 같은 클라이언트의
    동시 호출은 lock 으로 직렬화된다.

    사용자 환경 (Gemma 4) 메모리 안전:
        - 8단계(Summarizer) 가 Gemma 4 를 이미 로드해 두었을 가능성이 높음.
        - 9단계(Wiki) 진입 시 같은 모델을 reuse → 추가 메모리 스왑 0.
        - WIKI_LLM_MODEL_NAME 식별자는 호출 의도 분리용이며, 실제 모델은 동일.

    EXAONE 사용자 (사용자가 직접 변경) 의 경우:
        - config.wiki.compiler_model 을 EXAONE repo 로 변경하면 동일 코드 경로로
          작동. 단, 8단계가 Gemma 라면 9단계 진입 시 unload→reload 가 발생.
    """

    def __init__(
        self,
        config: Any,
        model_manager: Any,
    ) -> None:
        """Wiki LLM 클라이언트를 초기화한다 (실제 모델 로드는 lazy).

        Args:
            config: AppConfig 또는 WikiConfig — compiler_model 필드 참조.
            model_manager: ModelLoadManager 인스턴스 — 단일 로드 보장.
        """
        self._config = config
        self._model_manager = model_manager
        # WikiConfig 직접 또는 AppConfig.wiki 모두 지원
        wiki_cfg = getattr(config, "wiki", config)
        self._model_name: str = getattr(
            wiki_cfg,
            "compiler_model",
            "mlx-community/gemma-4-e4b-it-4bit",
        )

    @property
    def model_name(self) -> str:
        """`config.wiki.compiler_model` 그대로 반환 (로깅 식별용)."""
        return self._model_name

    def _make_loader(self) -> Any:
        """ModelLoadManager 가 호출할 백엔드 생성 콜백을 반환한다.

        Returns:
            인자 없는 콜백 (config 는 closure 로 캡처).

        Note:
            모듈에서 동적으로 `_create_exaone_backend` 를 lookup 한다.
            테스트에서 `monkeypatch.setattr(module, "_create_exaone_backend", ...)`
            로 가짜 백엔드를 주입할 수 있도록 하기 위함.
            (`_create_exaone_backend` 는 `_create_wiki_backend` 의 alias 이지만
            monkeypatch 는 모듈 attribute 만 교체하므로 동적 lookup 이 필요.)
        """

        def _loader() -> Any:
            # 모듈 레벨에서 매번 attribute 를 가져와 monkeypatch 적용 보장
            import sys

            mod = sys.modules[__name__]
            backend_factory = getattr(mod, "_create_exaone_backend", _create_wiki_backend)
            return backend_factory(self._config)

        return _loader

    async def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = WIKI_DEFAULT_MAX_TOKENS,
        temperature: float = WIKI_DEFAULT_TEMPERATURE,
    ) -> str:
        """ModelLoadManager 를 통해 Wiki LLM 으로 단일 응답을 생성한다.

        ModelLoadManager.acquire 는 try/finally 로 release 를 보장한다 (블록
        종료 시 자동 unload — 단, 같은 모델명이 재호출되면 reuse).

        Args:
            system_prompt: 역할/지시 본문.
            user_prompt: 회의 컨텍스트.
            max_tokens: 응답 토큰 상한 (현재 backend.chat 에서는 무시되지만
                인터페이스 호환을 위해 받음).
            temperature: 디코딩 temperature.

        Returns:
            NFC 정규화된 응답 본문 (앞뒤 공백 제거).

        Raises:
            WikiLLMError: 백엔드 초기화/생성 실패 시.
        """
        # ModelLoadManager 의 acquire 컨텍스트로 모델 확보.
        # keep_loaded=True 면 다음 generate() 호출도 같은 인스턴스를 재사용.
        # 9단계 종료 후 호출 측(WikiCompilerV2 wrapper) 이 명시적 unload 책임.
        try:
            async with self._model_manager.acquire(
                WIKI_LLM_MODEL_NAME,
                self._make_loader(),
                keep_loaded=True,
            ) as backend:
                # backend.chat 은 동기 메서드 — to_thread 로 이벤트 루프 보호
                try:
                    raw = await asyncio.to_thread(
                        backend.chat,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        temperature=temperature,
                    )
                except WikiLLMError:
                    raise
                except Exception as gen_exc:  # noqa: BLE001 — 백엔드 종류 무관 wrap
                    # LLMConnectionError 는 백엔드 연결 불가, LLMGenerationError 는 응답 실패
                    from core.llm_backend import (
                        LLMConnectionError,
                        LLMGenerationError,
                    )

                    if isinstance(gen_exc, LLMConnectionError):
                        reason = "backend_unavailable"
                    elif isinstance(gen_exc, LLMGenerationError):
                        reason = "generation_failed"
                    else:
                        reason = "generation_failed"
                    raise WikiLLMError(reason=reason, detail=str(gen_exc)) from gen_exc
        except WikiLLMError:
            raise
        except Exception as load_exc:  # noqa: BLE001 — 모델 로드 자체 실패
            raise WikiLLMError(
                reason="model_load_failed",
                detail=f"EXAONE 모델 로드 실패: {load_exc}",
            ) from load_exc

        # NFC 정규화 + 공백 정리 — 결정성 강화
        return unicodedata.normalize("NFC", str(raw).strip())


# ─────────────────────────────────────────────────────────────────────────
# 1.5 테스트 헬퍼 — MockWikiLLMClient
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class MockResponse:
    """`MockWikiLLMClient` 가 시퀀스로 반환할 단일 응답.

    Attributes:
        body: 응답 본문 문자열.
        delay_seconds: 응답 전 인위적 지연 (타임아웃 테스트용, 기본 0.0).
        raise_error: 설정 시 응답 대신 WikiLLMError(reason=...) 발생.
    """

    body: str
    delay_seconds: float = 0.0
    raise_error: str | None = None


class MockWikiLLMClient:
    """테스트 전용 mock 구현. 호출 순서대로 미리 셋팅된 응답을 반환한다.

    Attributes:
        responses: FIFO 큐. 호출마다 pop.
        calls: 실제 호출 기록 (system_prompt, user_prompt, kwargs) — assert 용.
    """

    def __init__(self, responses: list[MockResponse] | None = None) -> None:
        """초기 응답 시퀀스를 주입한다.

        Args:
            responses: 호출 순서대로 반환할 응답 시퀀스. None 이면 빈 리스트.
        """
        self.responses: list[MockResponse] = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    @property
    def model_name(self) -> str:
        """고정값 "mock-exaone" 반환 (로깅 식별용)."""
        return "mock-exaone"

    async def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = WIKI_DEFAULT_MAX_TOKENS,
        temperature: float = WIKI_DEFAULT_TEMPERATURE,
    ) -> str:
        """다음 응답을 pop 하여 반환한다. responses 가 비면 AssertionError."""
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        assert self.responses, "MockWikiLLMClient: 응답 시퀀스가 소진되었습니다."
        resp = self.responses.pop(0)
        if resp.delay_seconds > 0:
            await asyncio.sleep(resp.delay_seconds)
        if resp.raise_error:
            raise WikiLLMError(reason=resp.raise_error)
        return resp.body


# ─────────────────────────────────────────────────────────────────────────
# 1.6 하위 호환 alias (Phase 2.E 통합 — Phase 3 에서 제거 예정)
# ─────────────────────────────────────────────────────────────────────────

# 이전 클래스 이름 — Gemma 통합 전 EXAONE 만 가정하던 시기의 잔재.
# 신규 코드는 MlxWikiClient 를 사용하라.
ExaoneWikiClient = MlxWikiClient
