"""
사용자 설정 저장소 모듈 (User Settings Storage Module)

목적: 사용자가 프론트엔드에서 편집 가능한 LLM 프롬프트(보정/요약/채팅)와
      고유명사 용어집을 JSON 파일 기반으로 안전하게 영속화한다.

주요 기능:
    - 프롬프트 3종(corrector/summarizer/chat)과 용어집 관리
    - 원자적 쓰기 (temp → fsync → rename) + filelock 기반 동시성 제어
    - mtime + size 기반 인메모리 캐시 (외부 편집도 자동 감지)
    - 파일 손상 시 .bak 백업으로부터 자동 복구
    - 기본값 파일(core/defaults/)에서 공장 초기화
    - 검증 실패 시 거부 (Pydantic 모델 + 커스텀 검증)
    - 잡 단위 스냅샷 (회의 처리 도중 일관성 보장)
    - 용어집을 LLM 프롬프트에 주입하는 헬퍼

의존성: filelock, pydantic, config 모듈
외부 의존성 추가: filelock (pyproject.toml에 명시)
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from filelock import FileLock, Timeout
from pydantic import BaseModel, Field, field_validator

from config import get_config

logger = logging.getLogger(__name__)


# === 상수 ===

# 파일 락 획득 타임아웃 (초). 단일 사용자 데스크톱 앱이므로 짧게 유지.
_LOCK_TIMEOUT_SECONDS: float = 5.0

# 프롬프트 최소/최대 길이
_PROMPT_MIN_LEN: int = 20
_PROMPT_MAX_LEN: int = 8000

# 용어집 제약
_VOCAB_MAX_TERMS: int = 500
_VOCAB_TERM_MAX_LEN: int = 100
_VOCAB_ALIAS_MAX_LEN: int = 100
_VOCAB_MAX_ALIASES: int = 20
_VOCAB_NOTE_MAX_LEN: int = 500

# 캐시 안전망 TTL (외부 편집 감지 실패 대비)
_CACHE_TTL_SECONDS: float = 60.0

# 필수 프롬프트 키 (스키마 고정)
_PROMPT_KEYS: tuple[str, ...] = ("corrector", "summarizer", "chat")

# corrector 프롬프트가 반드시 포함해야 할 포맷 지시 패턴
# (LLM 응답을 [번호] 텍스트로 파싱하므로 누락 시 보정이 동작하지 않음)
_CORRECTOR_FORMAT_TOKEN: str = "[번호]"

# 기본값 파일 경로 (패키지 내부)
_DEFAULTS_DIR: Path = Path(__file__).parent / "defaults"
_DEFAULT_PROMPTS_FILE: Path = _DEFAULTS_DIR / "prompts.default.json"
_DEFAULT_VOCABULARY_FILE: Path = _DEFAULTS_DIR / "vocabulary.default.json"


# === 에러 계층 ===


class UserSettingsError(Exception):
    """사용자 설정 저장소의 기본 예외."""


class UserSettingsCorruptError(UserSettingsError):
    """JSON 파일이 손상되어 파싱할 수 없고 복구도 실패했을 때."""


class UserSettingsLockError(UserSettingsError):
    """파일 락 획득 실패 (타임아웃)."""


class UserSettingsIOError(UserSettingsError):
    """디스크 I/O 실패 (권한 없음, 디스크 풀 등)."""


class UserSettingsValidationError(UserSettingsError):
    """입력 검증 실패."""


# === ULID 생성 (표준 라이브러리만 사용) ===

# Crockford Base32 알파벳 (ULID 표준)
_BASE32_ALPHABET: str = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _generate_ulid() -> str:
    """26자 ULID 문자열을 생성한다.

    ULID는 48비트 타임스탬프 + 80비트 랜덤으로 구성되며,
    Crockford Base32로 인코딩되어 26자 문자열이 된다.
    문자열 정렬만으로 생성 시간 순서가 보존된다.

    표준 라이브러리만 사용하여 외부 의존성을 추가하지 않는다.

    Returns:
        26자 ULID 문자열 (예: "01HXYZ...ABCD")
    """
    # 48비트 타임스탬프 (밀리초)
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    # 80비트 랜덤
    randomness = secrets.randbits(80)
    # 128비트로 결합
    value = (timestamp_ms << 80) | randomness

    # Base32 인코딩 (26자)
    chars: list[str] = []
    for _ in range(26):
        chars.append(_BASE32_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


# === Pydantic 도메인 모델 ===


class PromptEntry(BaseModel):
    """단일 프롬프트 항목 (보정/요약/채팅 공통).

    Attributes:
        system_prompt: LLM 시스템 프롬프트 본문
        updated_at: 마지막 수정 시각 (ISO 8601, UTC, 선택)
    """

    system_prompt: str = Field(
        ...,
        min_length=_PROMPT_MIN_LEN,
        max_length=_PROMPT_MAX_LEN,
        description="LLM에 전달될 시스템 프롬프트 본문",
    )
    updated_at: str | None = Field(default=None, description="ISO 8601 UTC 타임스탬프")


class PromptsData(BaseModel):
    """프롬프트 전체 (보정/요약/채팅 + 스키마 버전).

    Attributes:
        schema_version: 저장 포맷 버전 (마이그레이션용)
        corrector: STT 보정 프롬프트
        summarizer: 회의록 요약 프롬프트
        chat: RAG 채팅 프롬프트
    """

    schema_version: int = Field(default=1, ge=1)
    corrector: PromptEntry
    summarizer: PromptEntry
    chat: PromptEntry

    @field_validator("corrector")
    @classmethod
    def _validate_corrector_format(cls, v: PromptEntry) -> PromptEntry:
        """corrector 프롬프트에 [번호] 포맷 지시가 있는지 검증한다.

        Args:
            v: 검증할 PromptEntry

        Returns:
            검증된 PromptEntry

        Raises:
            ValueError: [번호] 토큰이 없을 때
        """
        if _CORRECTOR_FORMAT_TOKEN not in v.system_prompt:
            raise ValueError(
                f"보정 프롬프트는 반드시 '{_CORRECTOR_FORMAT_TOKEN}' "
                "출력 포맷 지시를 포함해야 합니다. "
                "예: '반드시 입력과 동일한 번호와 포맷([번호] 텍스트)으로 출력하세요.'"
            )
        return v


class VocabularyTerm(BaseModel):
    """고유명사 용어집의 단일 항목.

    Attributes:
        id: ULID 26자 (서버 생성, 영구 불변)
        term: 정답 표기 (예: "FastAPI")
        aliases: STT가 잘못 인식하는 오인식 변형 (예: ["패스트api", "패스트에이피아이"])
        category: 분류 (예: "인명", "제품", "회사"). 선택
        note: 사용자 메모. 선택
        enabled: 교정 적용 여부. false면 프롬프트 주입에서 제외
        created_at: 생성 시각 (ISO 8601 UTC, 서버 설정)
    """

    id: str = Field(..., min_length=26, max_length=26, description="ULID 26자")
    term: str = Field(..., min_length=1, max_length=_VOCAB_TERM_MAX_LEN)
    aliases: list[str] = Field(default_factory=list, max_length=_VOCAB_MAX_ALIASES)
    category: str | None = Field(default=None, max_length=50)
    note: str | None = Field(default=None, max_length=_VOCAB_NOTE_MAX_LEN)
    enabled: bool = Field(default=True)
    created_at: str | None = Field(default=None)

    @field_validator("term")
    @classmethod
    def _strip_term(cls, v: str) -> str:
        """앞뒤 공백을 제거하고 빈 문자열을 거부한다."""
        v = v.strip()
        if not v:
            raise ValueError("term은 공백만으로 구성될 수 없습니다.")
        return v

    @field_validator("aliases")
    @classmethod
    def _validate_aliases(cls, v: list[str]) -> list[str]:
        """별칭 리스트를 정제하고 검증한다.

        - 공백 trim
        - 빈 값 제거
        - 길이 제한 초과 거부
        - 중복 제거 (순서 유지)
        """
        cleaned: list[str] = []
        seen: set[str] = set()
        for a in v:
            a = a.strip()
            if not a:
                continue
            if len(a) > _VOCAB_ALIAS_MAX_LEN:
                raise ValueError(
                    f"별칭 길이는 {_VOCAB_ALIAS_MAX_LEN}자 이하여야 합니다: {a[:20]}..."
                )
            if a in seen:
                continue
            seen.add(a)
            cleaned.append(a)
        return cleaned


class VocabularyData(BaseModel):
    """용어집 전체.

    Attributes:
        schema_version: 저장 포맷 버전
        terms: 용어 목록
        updated_at: 마지막 수정 시각 (ISO 8601 UTC)
    """

    schema_version: int = Field(default=1, ge=1)
    terms: list[VocabularyTerm] = Field(default_factory=list, max_length=_VOCAB_MAX_TERMS)
    updated_at: str | None = Field(default=None)

    @field_validator("terms")
    @classmethod
    def _validate_term_uniqueness(cls, v: list[VocabularyTerm]) -> list[VocabularyTerm]:
        """term(대소문자 무시)의 유일성과 id의 유일성을 검증한다."""
        seen_terms: set[str] = set()
        seen_ids: set[str] = set()
        for t in v:
            key = t.term.strip().lower()
            if key in seen_terms:
                raise ValueError(f"중복된 용어: {t.term}")
            if t.id in seen_ids:
                raise ValueError(f"중복된 id: {t.id}")
            seen_terms.add(key)
            seen_ids.add(t.id)
        return v


# === 잡 단위 스냅샷 ===


@dataclass(frozen=True)
class CorrectorPromptSnapshot:
    """회의 처리 시작 시점에 고정되는 프롬프트 스냅샷.

    파이프라인이 한 회의를 처리하는 동안 사용자가 설정을 수정해도
    진행 중인 회의는 이 스냅샷의 값을 끝까지 사용한다 (잡 단위 일관성).

    Attributes:
        system_prompt: 베이스 프롬프트 + 용어집 섹션이 합쳐진 최종 시스템 프롬프트
        base_prompt: 사용자가 편집한 원본 (용어집 주입 전)
        vocab_term_count: 주입된 활성 용어 개수
        snapshot_at: 스냅샷 생성 시각 (로깅/디버깅)
    """

    system_prompt: str
    base_prompt: str
    vocab_term_count: int
    snapshot_at: datetime


# === 내부 캐시 ===


@dataclass
class _CacheEntry:
    """파일별 캐시 항목.

    mtime_ns=0 + size=0 은 "파일 없음" 상태를 의미한다.
    """

    data: Any
    mtime_ns: int
    size: int
    cached_at: float = field(default_factory=time.monotonic)


# 모듈 수준 캐시 (키: 파일 절대경로 문자열)
_cache: dict[str, _CacheEntry] = {}
_cache_lock = threading.RLock()


def _now_utc_iso() -> str:
    """현재 시각을 ISO 8601 UTC 문자열로 반환한다."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# === 경로 헬퍼 ===


def _user_data_dir() -> Path:
    """사용자 데이터 디렉토리 경로를 반환하고 필요 시 생성한다.

    config의 paths.resolved_base_dir 하위에 user_data/ 디렉토리를 두고,
    권한을 0o700으로 설정한다. 이미 존재하면 권한만 점검한다.

    Returns:
        절대 경로
    """
    cfg = get_config()
    path = cfg.paths.resolved_base_dir / "user_data"
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError as e:
            logger.warning(f"user_data 디렉토리 권한 설정 실패 (무시 가능): {e}")
    return path


def _prompts_path() -> Path:
    """프롬프트 파일 경로를 반환한다."""
    return _user_data_dir() / "prompts.json"


def _vocabulary_path() -> Path:
    """용어집 파일 경로를 반환한다."""
    return _user_data_dir() / "vocabulary.json"


def _backup_path(path: Path) -> Path:
    """백업 파일 경로를 반환한다."""
    return path.with_suffix(path.suffix + ".bak")


def _lock_path(path: Path) -> Path:
    """락 파일 경로를 반환한다."""
    return path.with_suffix(path.suffix + ".lock")


def _corrupt_path(path: Path) -> Path:
    """손상 파일 격리 경로를 반환한다 (타임스탬프 포함)."""
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return path.with_suffix(path.suffix + f".corrupted-{ts}")


# === 원자적 파일 I/O ===


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """JSON 데이터를 원자적으로 파일에 기록한다.

    공용 헬퍼 `core.io_utils.atomic_write_json` 으로 위임한다.
    이 모듈의 내부 호출자(`_save_generic` 등)와의 하위 호환을 위해 thin wrapper 로 유지.
    `backup=False` 인 이유: 백업 책임은 `_save_generic` 에 있다 (호출 전 .bak 복사 수행).

    Args:
        path: 최종 대상 경로
        data: JSON으로 직렬화 가능한 dict

    Raises:
        OSError: 쓰기 실패 (권한, 디스크 풀 등)
    """
    from .io_utils import atomic_write_json

    atomic_write_json(path, data, backup=False)


# === 기본값 로드 ===


def _load_default_prompts() -> PromptsData:
    """기본 프롬프트를 패키지 내부 JSON에서 로드한다.

    Returns:
        검증된 PromptsData

    Raises:
        UserSettingsIOError: 기본값 파일이 없거나 손상된 경우 (배포 오류)
    """
    try:
        raw = json.loads(_DEFAULT_PROMPTS_FILE.read_text(encoding="utf-8"))
        return PromptsData.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        raise UserSettingsIOError(
            f"기본 프롬프트 파일을 읽을 수 없습니다 (배포 오류): "
            f"{_DEFAULT_PROMPTS_FILE} — {e}"
        ) from e


def _load_default_vocabulary() -> VocabularyData:
    """기본 용어집을 패키지 내부 JSON에서 로드한다.

    Returns:
        검증된 VocabularyData
    """
    try:
        raw = json.loads(_DEFAULT_VOCABULARY_FILE.read_text(encoding="utf-8"))
        return VocabularyData.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.warning(f"기본 용어집 파일 로드 실패, 빈 목록 사용: {e}")
        return VocabularyData(schema_version=1, terms=[], updated_at=None)


# === 제네릭 로드/저장 ===


T = TypeVar("T", bound=BaseModel)


def _stat_mtime_size(path: Path) -> tuple[int, int]:
    """파일의 mtime_ns와 size를 반환한다. 파일이 없으면 (0, 0)."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def _cache_hit(cached: _CacheEntry | None, mtime_ns: int, size: int) -> bool:
    """캐시가 유효한지 판정한다.

    1) 파일이 존재해야 함 (mtime_ns != 0)
    2) mtime_ns와 size가 캐시와 일치
    3) 캐시 나이가 TTL 이내 (외부 편집 안전망)
    """
    if cached is None:
        return False
    if mtime_ns == 0 or size == 0:
        return False
    if cached.mtime_ns != mtime_ns or cached.size != size:
        return False
    if (time.monotonic() - cached.cached_at) > _CACHE_TTL_SECONDS:
        return False
    return True


def _load_generic(
    path: Path,
    model_cls: type[T],
    default_factory: Callable[[], T],
    force_reload: bool = False,
) -> T:
    """제네릭 로드 로직 (프롬프트/용어집 공통).

    1. mtime+size 조회 → 캐시 적중 시 즉시 반환
    2. 파일 없음 → 기본값 생성 + 디스크에 저장 후 반환
    3. 파일 존재 → 파싱 + Pydantic 검증
    4. 손상 시 → .bak 복구 시도 → 실패 시 기본값으로 재생성
    5. 캐시 갱신 후 반환

    Args:
        path: 읽을 파일 경로
        model_cls: Pydantic 모델 클래스
        default_factory: 기본값 생성 함수
        force_reload: True면 캐시를 무시하고 강제 재로드

    Returns:
        검증된 모델 인스턴스

    Raises:
        UserSettingsIOError: 디스크 쓰기 권한 없음 등 복구 불가 I/O 에러
    """
    key = str(path)

    with _cache_lock:
        mtime_ns, size = _stat_mtime_size(path)
        cached = _cache.get(key)

        if not force_reload and _cache_hit(cached, mtime_ns, size):
            return cached.data  # type: ignore[return-value]

        # 파일 없음 → 기본값 생성
        if mtime_ns == 0:
            default = default_factory()
            try:
                payload = default.model_dump(mode="json")
                # updated_at 자동 설정
                if "updated_at" in payload and payload.get("updated_at") is None:
                    payload["updated_at"] = _now_utc_iso()
                _atomic_write_json(path, payload)
                mtime_ns, size = _stat_mtime_size(path)
                # 다시 검증된 인스턴스로 복원 (updated_at 반영)
                default = model_cls.model_validate(payload)
                logger.info(f"사용자 설정 파일을 기본값으로 생성: {path.name}")
            except OSError as e:
                logger.warning(
                    f"기본값 파일 생성 실패 ({path.name}), 메모리 기본값만 사용: {e}"
                )

            _cache[key] = _CacheEntry(default, mtime_ns, size)
            return default

        # 파일 존재 → 파싱
        try:
            raw_text = path.read_text(encoding="utf-8")
            raw = json.loads(raw_text)
            data = model_cls.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.error(
                f"{path.name} 파일 파싱 실패, 백업에서 복구 시도: {e}"
            )
            data = _recover_from_backup(path, model_cls, default_factory)
            mtime_ns, size = _stat_mtime_size(path)

        _cache[key] = _CacheEntry(data, mtime_ns, size)
        return data


def _recover_from_backup(
    path: Path,
    model_cls: type[T],
    default_factory: Callable[[], T],
) -> T:
    """손상된 파일을 .bak에서 복구하거나 기본값으로 재생성한다.

    1. 손상된 원본을 .corrupted-{ts}로 이동 (포렌식용)
    2. .bak이 있으면 파싱 시도 → 성공 시 정상 파일로 승격
    3. 실패하거나 .bak도 없으면 기본값으로 재생성

    Args:
        path: 손상된 파일 경로
        model_cls: Pydantic 모델 클래스
        default_factory: 기본값 생성 함수

    Returns:
        복구된 또는 기본값 모델 인스턴스
    """
    # 1. 손상 파일 격리
    if path.exists():
        corrupt_path = _corrupt_path(path)
        try:
            shutil.move(str(path), str(corrupt_path))
            logger.warning(f"손상 파일 격리: {corrupt_path.name}")
        except OSError as e:
            logger.error(f"손상 파일 격리 실패: {e}")

    # 2. 백업에서 복구
    backup = _backup_path(path)
    if backup.exists():
        try:
            raw = json.loads(backup.read_text(encoding="utf-8"))
            data = model_cls.model_validate(raw)
            # 백업을 정상 파일로 승격
            _atomic_write_json(path, data.model_dump(mode="json"))
            logger.warning(f".bak에서 복구 성공: {path.name}")
            return data
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.error(f".bak 복구 실패: {e}")

    # 3. 기본값으로 재생성
    default = default_factory()
    try:
        payload = default.model_dump(mode="json")
        if "updated_at" in payload and payload.get("updated_at") is None:
            payload["updated_at"] = _now_utc_iso()
        _atomic_write_json(path, payload)
        default = model_cls.model_validate(payload)
        logger.warning(f"기본값으로 재생성: {path.name}")
    except OSError as e:
        logger.error(f"기본값 재생성 실패 (메모리 기본값만 사용): {e}")
    return default


def _save_generic(path: Path, data: BaseModel) -> None:
    """제네릭 저장 로직 (프롬프트/용어집 공통).

    1. updated_at을 현재 UTC로 갱신
    2. filelock 획득 (5초 타임아웃)
    3. 기존 파일이 있으면 .bak으로 백업
    4. 원자적 쓰기 (temp → fsync → rename)
    5. 캐시 갱신

    Args:
        path: 저장 대상 경로
        data: 저장할 Pydantic 모델

    Raises:
        UserSettingsLockError: 락 획득 실패
        UserSettingsIOError: 디스크 쓰기 실패
    """
    # updated_at 갱신
    payload = data.model_dump(mode="json")
    payload["updated_at"] = _now_utc_iso()

    lock_file = str(_lock_path(path))

    try:
        with FileLock(lock_file, timeout=_LOCK_TIMEOUT_SECONDS):
            # 백업 생성 (기존 파일이 있을 때만)
            if path.exists():
                try:
                    shutil.copy2(path, _backup_path(path))
                except OSError as e:
                    logger.warning(f"백업 생성 실패 (진행 계속): {e}")

            # 원자적 쓰기
            try:
                _atomic_write_json(path, payload)
            except OSError as e:
                raise UserSettingsIOError(f"{path.name} 저장 실패: {e}") from e

            # 캐시 갱신
            mtime_ns, size = _stat_mtime_size(path)
            with _cache_lock:
                # 저장된 payload로 모델 재구성 (updated_at 반영)
                refreshed = data.__class__.model_validate(payload)
                _cache[str(path)] = _CacheEntry(refreshed, mtime_ns, size)

    except Timeout as e:
        raise UserSettingsLockError(
            f"{path.name} 락 획득 실패 ({_LOCK_TIMEOUT_SECONDS}초 타임아웃)"
        ) from e

    logger.info(f"사용자 설정 저장 완료: {path.name}")


# === 공개 API: 프롬프트 ===


def load_prompts(force_reload: bool = False) -> PromptsData:
    """프롬프트 데이터를 로드한다 (캐시 활용).

    Args:
        force_reload: True면 캐시를 무시하고 디스크에서 강제 재로드

    Returns:
        검증된 PromptsData

    Raises:
        UserSettingsIOError: 복구 불가한 I/O 실패
    """
    return _load_generic(
        _prompts_path(), PromptsData, _load_default_prompts, force_reload=force_reload
    )


def save_prompts(data: PromptsData) -> PromptsData:
    """프롬프트 데이터를 저장한다 (원자적, 락, 백업).

    Args:
        data: 저장할 PromptsData

    Returns:
        updated_at이 갱신된 PromptsData

    Raises:
        UserSettingsLockError: 락 획득 실패
        UserSettingsIOError: 디스크 쓰기 실패
        UserSettingsValidationError: Pydantic 검증 실패 (corrector 포맷 등)
    """
    try:
        # 재검증 (호출자가 비정상 객체를 만들었을 수 있음)
        data = PromptsData.model_validate(data.model_dump())
    except ValueError as e:
        raise UserSettingsValidationError(f"프롬프트 검증 실패: {e}") from e

    _save_generic(_prompts_path(), data)
    return load_prompts(force_reload=False)


def reset_prompts_to_default() -> PromptsData:
    """프롬프트를 공장 기본값으로 복원한다.

    Returns:
        저장된 기본 PromptsData
    """
    default = _load_default_prompts()
    _save_generic(_prompts_path(), default)
    logger.info("프롬프트를 기본값으로 복원")
    return load_prompts(force_reload=True)


# === 공개 API: 용어집 ===


def load_vocabulary(force_reload: bool = False) -> VocabularyData:
    """용어집 데이터를 로드한다 (캐시 활용).

    Args:
        force_reload: True면 캐시를 무시

    Returns:
        검증된 VocabularyData
    """
    return _load_generic(
        _vocabulary_path(),
        VocabularyData,
        _load_default_vocabulary,
        force_reload=force_reload,
    )


def save_vocabulary(data: VocabularyData) -> VocabularyData:
    """용어집 데이터를 저장한다.

    Args:
        data: 저장할 VocabularyData

    Returns:
        updated_at이 갱신된 VocabularyData

    Raises:
        UserSettingsLockError, UserSettingsIOError, UserSettingsValidationError
    """
    try:
        data = VocabularyData.model_validate(data.model_dump())
    except ValueError as e:
        raise UserSettingsValidationError(f"용어집 검증 실패: {e}") from e

    _save_generic(_vocabulary_path(), data)
    return load_vocabulary(force_reload=False)


def reset_vocabulary_to_default() -> VocabularyData:
    """용어집을 공장 기본값(빈 목록)으로 복원한다."""
    default = _load_default_vocabulary()
    _save_generic(_vocabulary_path(), default)
    logger.info("용어집을 기본값으로 복원")
    return load_vocabulary(force_reload=True)


# === 용어집 CRUD 헬퍼 (API 계층에서 호출) ===


def add_vocabulary_term(
    term: str,
    aliases: list[str] | None = None,
    category: str | None = None,
    note: str | None = None,
    enabled: bool = True,
) -> VocabularyTerm:
    """용어를 추가한다 (ULID 서버 생성).

    Args:
        term: 정답 표기
        aliases: 오인식 변형 목록
        category: 분류
        note: 메모
        enabled: 활성화 여부

    Returns:
        생성된 VocabularyTerm

    Raises:
        UserSettingsValidationError: 중복, 최대 개수 초과, 검증 실패 등
    """
    vocab = load_vocabulary()
    normalized_term = term.strip().lower()

    # 중복 검사
    for existing in vocab.terms:
        if existing.term.strip().lower() == normalized_term:
            raise UserSettingsValidationError(
                f"이미 등록된 용어입니다: {term}"
            )

    # 최대 개수 검사
    if len(vocab.terms) >= _VOCAB_MAX_TERMS:
        raise UserSettingsValidationError(
            f"용어집은 최대 {_VOCAB_MAX_TERMS}개까지 등록할 수 있습니다."
        )

    try:
        new_term = VocabularyTerm(
            id=_generate_ulid(),
            term=term,
            aliases=aliases or [],
            category=category,
            note=note,
            enabled=enabled,
            created_at=_now_utc_iso(),
        )
    except ValueError as e:
        raise UserSettingsValidationError(f"용어 검증 실패: {e}") from e

    vocab.terms.append(new_term)
    save_vocabulary(vocab)
    return new_term


def update_vocabulary_term(
    term_id: str,
    term: str | None = None,
    aliases: list[str] | None = None,
    category: str | None = None,
    note: str | None = None,
    enabled: bool | None = None,
) -> VocabularyTerm:
    """용어를 부분 업데이트한다.

    None이 아닌 필드만 반영한다.

    Args:
        term_id: 업데이트 대상 ULID
        term, aliases, category, note, enabled: 변경할 필드 (선택)

    Returns:
        업데이트된 VocabularyTerm

    Raises:
        UserSettingsValidationError: 대상 없음, 중복, 검증 실패 등
    """
    vocab = load_vocabulary()
    target: VocabularyTerm | None = None
    target_idx: int = -1

    for i, t in enumerate(vocab.terms):
        if t.id == term_id:
            target = t
            target_idx = i
            break

    if target is None:
        raise UserSettingsValidationError(f"용어를 찾을 수 없습니다: {term_id}")

    # term 변경 시 중복 검사
    if term is not None and term.strip().lower() != target.term.strip().lower():
        normalized = term.strip().lower()
        for other in vocab.terms:
            if other.id != term_id and other.term.strip().lower() == normalized:
                raise UserSettingsValidationError(
                    f"이미 등록된 용어입니다: {term}"
                )

    # 부분 업데이트
    updates: dict[str, Any] = {}
    if term is not None:
        updates["term"] = term
    if aliases is not None:
        updates["aliases"] = aliases
    if category is not None:
        updates["category"] = category
    if note is not None:
        updates["note"] = note
    if enabled is not None:
        updates["enabled"] = enabled

    try:
        updated = target.model_copy(update=updates)
        # 재검증 (validator 실행)
        updated = VocabularyTerm.model_validate(updated.model_dump())
    except ValueError as e:
        raise UserSettingsValidationError(f"용어 검증 실패: {e}") from e

    vocab.terms[target_idx] = updated
    save_vocabulary(vocab)
    return updated


def delete_vocabulary_term(term_id: str) -> None:
    """용어를 삭제한다.

    Args:
        term_id: 삭제할 ULID

    Raises:
        UserSettingsValidationError: 대상을 찾을 수 없을 때
    """
    vocab = load_vocabulary()
    before = len(vocab.terms)
    vocab.terms = [t for t in vocab.terms if t.id != term_id]
    if len(vocab.terms) == before:
        raise UserSettingsValidationError(f"용어를 찾을 수 없습니다: {term_id}")
    save_vocabulary(vocab)


# === 스냅샷 빌드 (파이프라인용) ===


def _render_vocabulary_block(terms: list[VocabularyTerm]) -> tuple[str, int]:
    """활성 용어 목록을 자연어 섹션으로 렌더링한다.

    Args:
        terms: 전체 용어 목록 (enabled=False는 자동 제외)

    Returns:
        (렌더링된 텍스트, 활성 용어 개수) 튜플
    """
    active = [t for t in terms if t.enabled]
    if not active:
        return ("", 0)

    lines = ["", "## 고유명사 사전 (정확한 표기를 우선 적용하세요)"]
    for t in active:
        line = f"- {t.term}"
        if t.aliases:
            line += f" (오인식 가능: {', '.join(t.aliases)})"
        if t.note:
            line += f" — {t.note}"
        lines.append(line)
    lines.append(
        "위 목록에 등재된 단어가 전사문에 등장하면 정답 표기로 교정하세요."
    )
    return ("\n".join(lines), len(active))


def build_corrector_snapshot() -> CorrectorPromptSnapshot:
    """corrector 단계용 프롬프트 스냅샷을 빌드한다.

    Corrector.correct() 시작 시 1회 호출하여 회의 단위 일관성을 보장한다.

    Returns:
        CorrectorPromptSnapshot
    """
    try:
        prompts = load_prompts()
        base = prompts.corrector.system_prompt
    except Exception as e:
        logger.warning(f"프롬프트 로드 실패, 기본값 사용: {e}")
        base = _load_default_prompts().corrector.system_prompt

    try:
        vocab = load_vocabulary()
        vocab_terms = vocab.terms
    except Exception as e:
        logger.warning(f"용어집 로드 실패, 빈 목록 사용: {e}")
        vocab_terms = []

    vocab_block, active_count = _render_vocabulary_block(vocab_terms)
    final_prompt = base + vocab_block if vocab_block else base

    return CorrectorPromptSnapshot(
        system_prompt=final_prompt,
        base_prompt=base,
        vocab_term_count=active_count,
        snapshot_at=datetime.now(timezone.utc),
    )


def build_summarizer_system_prompt() -> str:
    """summarizer 단계용 시스템 프롬프트를 반환한다.

    요약 단계는 용어집을 주입하지 않는다 (요약 품질에 미치는 영향 미미 +
    컨텍스트 절약). 필요 시 향후 옵션으로 확장 가능.

    Returns:
        시스템 프롬프트 문자열
    """
    try:
        return load_prompts().summarizer.system_prompt
    except Exception as e:
        logger.warning(f"요약 프롬프트 로드 실패, 기본값 사용: {e}")
        return _load_default_prompts().summarizer.system_prompt


def build_chat_system_prompt() -> str:
    """chat 단계용 시스템 프롬프트를 반환한다.

    Returns:
        시스템 프롬프트 문자열
    """
    try:
        return load_prompts().chat.system_prompt
    except Exception as e:
        logger.warning(f"채팅 프롬프트 로드 실패, 기본값 사용: {e}")
        return _load_default_prompts().chat.system_prompt


# === 초기화 / 테스트 헬퍼 ===


def init_user_settings() -> None:
    """앱 부팅 시 사용자 설정을 초기화한다.

    파일이 없으면 기본값에서 자동 생성한다. 첫 API 호출 시의
    cold-start 지연을 제거하고, 에러가 있다면 부팅 시점에 발견되도록 한다.
    """
    try:
        _user_data_dir()
        load_prompts()
        load_vocabulary()
        logger.info("사용자 설정 초기화 완료")
    except Exception as e:
        logger.error(f"사용자 설정 초기화 실패 (진행 계속): {e}")


def invalidate_cache() -> None:
    """모듈 수준 캐시를 전부 비운다 (테스트/디버그 전용)."""
    with _cache_lock:
        _cache.clear()
