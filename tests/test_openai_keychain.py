"""OpenAI API 키의 macOS Keychain 저장 계약을 검증한다.

실제 Keychain이나 실제 환경의 비밀에는 접근하지 않고 Security.framework 브리지를
메모리 fake로 교체한다.
"""

from __future__ import annotations

from typing import Any

import pytest

from security import openai_keychain


class FakeSecurity:
    """SecItem 계열 API를 메모리에서 모사하는 최소 Security 브리지."""

    kSecClass = "class"
    kSecClassGenericPassword = "generic-password"
    kSecAttrService = "service"
    kSecAttrAccount = "account"
    kSecMatchLimit = "match-limit"
    kSecMatchLimitOne = "one"
    kSecReturnData = "return-data"
    kSecReturnAttributes = "return-attributes"
    kSecValueData = "value-data"

    errSecSuccess = 0
    errSecItemNotFound = -25300

    def __init__(self, stored_value: bytes | None = None) -> None:
        self.stored_value = stored_value
        self.copy_queries: list[dict[Any, Any]] = []
        self.add_queries: list[dict[Any, Any]] = []
        self.update_calls: list[tuple[dict[Any, Any], dict[Any, Any]]] = []
        self.delete_queries: list[dict[Any, Any]] = []

    def SecItemCopyMatching(  # noqa: N802 - 실제 Security.framework 이름을 모사한다.
        self,
        query: dict[Any, Any],
        result: Any,
    ) -> tuple[int, Any]:
        del result
        self.copy_queries.append(dict(query))
        if self.stored_value is None:
            return self.errSecItemNotFound, None
        if query.get(self.kSecReturnData):
            return self.errSecSuccess, self.stored_value
        return self.errSecSuccess, {self.kSecAttrService: query[self.kSecAttrService]}

    def SecItemAdd(  # noqa: N802 - 실제 Security.framework 이름을 모사한다.
        self,
        query: dict[Any, Any],
        result: Any,
    ) -> int:
        del result
        self.add_queries.append(dict(query))
        self.stored_value = bytes(query[self.kSecValueData])
        return self.errSecSuccess

    def SecItemUpdate(  # noqa: N802 - 실제 Security.framework 이름을 모사한다.
        self,
        query: dict[Any, Any],
        attributes: dict[Any, Any],
    ) -> int:
        self.update_calls.append((dict(query), dict(attributes)))
        self.stored_value = bytes(attributes[self.kSecValueData])
        return self.errSecSuccess

    def SecItemDelete(self, query: dict[Any, Any]) -> int:  # noqa: N802
        self.delete_queries.append(dict(query))
        if self.stored_value is None:
            return self.errSecItemNotFound
        self.stored_value = None
        return self.errSecSuccess


@pytest.fixture(autouse=True)
def clear_openai_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """호스트 프로세스의 API 키가 테스트 결과에 영향을 주지 않게 한다."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_status는_키를_반환하지_않고_내부_형식까지_검증한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """설정 상태는 손상 항목을 configured로 오인하지 않고 비밀을 반환하지 않는다."""
    security = FakeSecurity(b"sk-keychain-secret-value-123456")
    monkeypatch.setattr(openai_keychain, "_load_security", lambda: security)

    status = openai_keychain.get_status()

    assert status.configured is True
    assert status.source == "keychain"
    assert security.copy_queries
    assert any(query.get(security.kSecReturnData) is True for query in security.copy_queries)
    assert "sk-keychain" not in repr(status)


def test_손상된_Keychain_항목은_configured로_표시하지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """과거/수동 변조로 생긴 invalid 항목은 실제 요청 가능 상태가 아니다."""
    security = FakeSecurity("가".encode() * 20)
    monkeypatch.setattr(openai_keychain, "_load_security", lambda: security)

    assert openai_keychain.get_api_key() is None
    assert openai_keychain.get_status() == openai_keychain.OpenAICredentialStatus(
        configured=False,
        source=None,
    )


def test_새_키를_추가하고_조회하고_삭제한다(monkeypatch: pytest.MonkeyPatch) -> None:
    """add/get/delete 전 과정은 고정 service/account 항목만 사용한다."""
    security = FakeSecurity()
    monkeypatch.setattr(openai_keychain, "_load_security", lambda: security)
    secret = "sk-new-secret-value-1234567890"

    openai_keychain.set_api_key(secret)

    assert security.stored_value == secret.encode("utf-8")
    assert len(security.add_queries) == 1
    add_query = security.add_queries[0]
    assert add_query[security.kSecClass] == security.kSecClassGenericPassword
    assert add_query[security.kSecAttrService] == openai_keychain._SERVICE
    assert add_query[security.kSecAttrAccount] == openai_keychain._ACCOUNT
    assert openai_keychain.get_api_key() == secret
    assert openai_keychain.delete_api_key() is True
    assert openai_keychain.delete_api_key() is False


def test_기존_키는_새_항목을_추가하지_않고_갱신한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """이미 존재하는 항목은 SecItemUpdate로 교체한다."""
    security = FakeSecurity(b"sk-old-secret-value-1234567890")
    monkeypatch.setattr(openai_keychain, "_load_security", lambda: security)
    replacement = "sk-replacement-secret-1234567890"

    openai_keychain.set_api_key(replacement)

    assert not security.add_queries
    assert len(security.update_calls) == 1
    assert security.stored_value == replacement.encode("utf-8")


def test_keychain이_환경변수보다_우선한다(monkeypatch: pytest.MonkeyPatch) -> None:
    """UI에서 저장한 Keychain 키가 개발용 환경변수보다 우선한다."""
    security = FakeSecurity(b"sk-keychain-secret-value-123456")
    monkeypatch.setattr(openai_keychain, "_load_security", lambda: security)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-environment-secret-value-123456")

    assert openai_keychain.get_api_key() == "sk-keychain-secret-value-123456"
    assert openai_keychain.get_status().source == "keychain"


def test_keychain_항목이_없으면_환경변수를_폴백한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """개발·CI에서는 환경변수를 읽기 전용 자격 증명으로 사용할 수 있다."""
    security = FakeSecurity()
    monkeypatch.setattr(openai_keychain, "_load_security", lambda: security)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-environment-secret-value-123456")

    assert openai_keychain.get_api_key() == "sk-environment-secret-value-123456"
    assert openai_keychain.get_status() == openai_keychain.OpenAICredentialStatus(
        configured=True,
        source="environment",
    )


@pytest.mark.parametrize(
    "invalid_key",
    [
        "short",
        " sk-valid-looking-secret-value-123456",
        "sk-valid-looking-secret-value-123456 ",
        "sk-valid-looking\nsecret-value-123456",
        "가" * 20,
        "x" * 513,
    ],
)
def test_잘못된_키는_Security_브리지_호출_전에_거부한다(
    monkeypatch: pytest.MonkeyPatch,
    invalid_key: str,
) -> None:
    """공백·제어문자·길이 오류가 있는 키는 Keychain에 전달하지 않는다."""
    loaded = False

    def fail_if_loaded() -> FakeSecurity:
        nonlocal loaded
        loaded = True
        return FakeSecurity()

    monkeypatch.setattr(openai_keychain, "_load_security", fail_if_loaded)

    with pytest.raises(ValueError, match="형식이 올바르지 않습니다"):
        openai_keychain.set_api_key(invalid_key)

    assert loaded is False


def test_Security_브리지를_쓸_수_없어도_환경변수_폴백은_동작한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PyObjC가 없는 CI에서도 실제 Keychain 접근 없이 env를 사용할 수 있다."""
    secret = "sk-environment-secret-value-123456"
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    def unavailable() -> Any:
        raise openai_keychain.OpenAIKeychainError("bridge unavailable")

    monkeypatch.setattr(openai_keychain, "_load_security", unavailable)

    assert openai_keychain.get_api_key() == secret
    assert openai_keychain.get_status().source == "environment"


@pytest.mark.parametrize(
    "unsafe_value",
    [
        "short",
        "sk-valid-prefix\nInjected: header-value-123456",
        " sk-valid-looking-secret-value-123456",
        "가" * 20,
    ],
)
def test_안전하지_않은_환경변수는_자격증명으로_사용하지_않는다(
    monkeypatch: pytest.MonkeyPatch,
    unsafe_value: str,
) -> None:
    """환경변수 폴백도 길이·공백·제어문자 검증을 거쳐 header injection을 막는다."""
    security = FakeSecurity()
    monkeypatch.setattr(openai_keychain, "_load_security", lambda: security)
    monkeypatch.setenv("OPENAI_API_KEY", unsafe_value)

    assert openai_keychain.get_api_key() is None
    assert openai_keychain.get_status().configured is False
