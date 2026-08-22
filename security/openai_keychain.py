"""OpenAI API 키를 macOS Keychain에서 안전하게 관리한다.

키 값은 config.yaml, SQLite, 로그에 저장하지 않는다. macOS Security.framework를
직접 호출해 프로세스 argv에도 비밀이 노출되지 않도록 한다. 개발/CI 환경에서는
``OPENAI_API_KEY``를 읽기 전용 폴백으로만 지원한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

_SERVICE = "com.recap.meeting-transcriber.openai-api-key"
_ACCOUNT = "openai"
_ENV_NAME = "OPENAI_API_KEY"


class OpenAIKeychainError(RuntimeError):
    """Keychain 작업이 실패했을 때 비밀을 포함하지 않고 발생하는 오류."""


@dataclass(frozen=True)
class OpenAICredentialStatus:
    """API 키 존재 여부만 표현하는 공개 가능한 상태."""

    configured: bool
    source: str | None = None


def validated_api_key(value: str | None) -> str | None:
    """헤더에 안전하게 사용할 수 있는 키만 원문 그대로 반환한다."""
    if value is None or value != value.strip():
        return None
    if len(value) < 20 or len(value) > 512:
        return None
    # http.client가 Authorization 값을 latin-1로 인코딩하기 전에 visible ASCII로
    # 제한해 header injection과 UnicodeEncodeError를 함께 차단한다.
    if any(not 33 <= ord(char) <= 126 for char in value):
        return None
    return value


def _load_security() -> Any:
    """PyObjC Security.framework 브리지를 지연 로드한다."""
    try:
        import Security  # type: ignore[import-not-found]
    except ImportError as exc:
        raise OpenAIKeychainError("macOS Keychain을 사용할 수 없습니다.") from exc
    return Security


def _status_code(result: Any) -> int:
    """PyObjC 함수별 반환 형태에서 OSStatus 정수를 추출한다."""
    if isinstance(result, tuple):
        return int(result[0])
    return int(result)


def _query(*, return_data: bool = False, return_attributes: bool = False) -> dict[Any, Any]:
    """고정 service/account에 대한 Keychain 조회 사전을 만든다."""
    security = _load_security()
    query: dict[Any, Any] = {
        security.kSecClass: security.kSecClassGenericPassword,
        security.kSecAttrService: _SERVICE,
        security.kSecAttrAccount: _ACCOUNT,
    }
    if return_data or return_attributes:
        query[security.kSecMatchLimit] = security.kSecMatchLimitOne
    if return_data:
        query[security.kSecReturnData] = True
    if return_attributes:
        query[security.kSecReturnAttributes] = True
    return query


def _copy_matching(query: dict[Any, Any]) -> tuple[int, Any]:
    """SecItemCopyMatching 결과를 일관된 튜플로 변환한다."""
    security = _load_security()
    result = security.SecItemCopyMatching(query, None)
    if isinstance(result, tuple):
        return int(result[0]), result[1]
    return int(result), None


def _read_keychain_api_key() -> str | None:
    """Keychain 값을 내부에서 읽고 검증하되 유효한 키만 반환한다."""
    security = _load_security()
    status, value = _copy_matching(_query(return_data=True))
    if status == int(security.errSecSuccess):
        try:
            if isinstance(value, bytes):
                decoded = value.decode("utf-8")
            else:
                decoded = bytes(value).decode("utf-8") if value is not None else ""
        except (TypeError, ValueError, UnicodeError):
            return None
        return validated_api_key(decoded)
    if status == int(security.errSecItemNotFound):
        return None
    raise OpenAIKeychainError(f"macOS Keychain에서 API 키를 읽을 수 없습니다 (OSStatus {status}).")


def get_status() -> OpenAICredentialStatus:
    """키 값을 노출하지 않고 현재 자격 증명 상태를 반환한다."""
    try:
        if _read_keychain_api_key() is not None:
            return OpenAICredentialStatus(configured=True, source="keychain")
    except OpenAIKeychainError:
        # macOS 외 테스트 환경에서도 env 폴백은 계속 동작해야 한다.
        if validated_api_key(os.environ.get(_ENV_NAME)) is None:
            return OpenAICredentialStatus(configured=False, source=None)
    if validated_api_key(os.environ.get(_ENV_NAME)) is not None:
        return OpenAICredentialStatus(configured=True, source="environment")
    return OpenAICredentialStatus(configured=False, source=None)


def get_api_key() -> str | None:
    """Keychain을 우선하고 환경변수를 폴백으로 사용해 API 키를 반환한다."""
    try:
        keychain_value = _read_keychain_api_key()
        if keychain_value is not None:
            return keychain_value
    except OpenAIKeychainError:
        pass
    return validated_api_key(os.environ.get(_ENV_NAME))


def set_api_key(api_key: str) -> None:
    """검증된 API 키를 고정 Keychain 항목에 추가하거나 갱신한다."""
    value = validated_api_key(api_key)
    if value is None:
        raise ValueError("OpenAI API 키 형식이 올바르지 않습니다.")

    security = _load_security()
    base_query = _query()
    encoded = value.encode("utf-8")
    status, _attrs = _copy_matching(_query(return_attributes=True))
    if status == int(security.errSecSuccess):
        update_status = _status_code(
            security.SecItemUpdate(base_query, {security.kSecValueData: encoded})
        )
        if update_status != int(security.errSecSuccess):
            raise OpenAIKeychainError(
                f"macOS Keychain API 키 갱신에 실패했습니다 (OSStatus {update_status})."
            )
        return
    if status != int(security.errSecItemNotFound):
        raise OpenAIKeychainError(f"macOS Keychain 상태를 확인할 수 없습니다 (OSStatus {status}).")

    add_query = dict(base_query)
    add_query[security.kSecValueData] = encoded
    add_status = _status_code(security.SecItemAdd(add_query, None))
    if add_status != int(security.errSecSuccess):
        raise OpenAIKeychainError(
            f"macOS Keychain API 키 저장에 실패했습니다 (OSStatus {add_status})."
        )


def delete_api_key() -> bool:
    """Keychain에 저장된 API 키를 삭제하고 실제 삭제 여부를 반환한다."""
    security = _load_security()
    status = _status_code(security.SecItemDelete(_query()))
    if status == int(security.errSecSuccess):
        return True
    if status == int(security.errSecItemNotFound):
        return False
    raise OpenAIKeychainError(f"macOS Keychain API 키 삭제에 실패했습니다 (OSStatus {status}).")
