"""
사용자 설정 저장소 단위 테스트.

`core/user_settings.py`의 다음을 검증한다:
    - 기본값 자동 생성
    - 캐시 적중/무효화
    - 원자적 저장 + 백업
    - 파일 손상 복구
    - Pydantic 검증 (corrector 포맷, 용어집 중복 등)
    - 용어집 CRUD 헬퍼
    - 스냅샷 빌더 (프롬프트 + 용어집 주입)
    - 기본값 복원 (reset)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import user_settings as us

# === fixtures ===


@pytest.fixture(autouse=True)
def isolated_user_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """각 테스트마다 독립된 user_data 디렉토리를 사용하고 캐시를 비운다."""
    data_dir = tmp_path / "user_data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # _user_data_dir을 임시 경로로 치환
    monkeypatch.setattr(us, "_user_data_dir", lambda: data_dir)

    # 캐시 초기화
    us.invalidate_cache()
    yield data_dir
    us.invalidate_cache()


@pytest.fixture
def valid_corrector_prompt() -> str:
    """검증을 통과하는 corrector 프롬프트 예시."""
    return (
        "당신은 한국어 전사문 보정 전문가입니다.\n"
        "규칙: 반드시 [번호] 텍스트 포맷으로 출력하세요. "
        "오타와 문법만 수정하고 의미는 변경하지 마세요."
    )


# === 1. 기본값 자동 생성 ===


def test_load_prompts_creates_default_when_missing(isolated_user_data: Path) -> None:
    """파일이 없을 때 load_prompts가 기본값을 디스크에 생성한다."""
    prompts_file = isolated_user_data / "prompts.json"
    assert not prompts_file.exists()

    data = us.load_prompts()

    assert prompts_file.exists()
    assert data.corrector.system_prompt
    assert data.summarizer.system_prompt
    assert data.chat.system_prompt
    assert "[번호]" in data.corrector.system_prompt


def test_load_vocabulary_creates_default_when_missing(isolated_user_data: Path) -> None:
    """용어집 파일이 없을 때 빈 기본값이 생성된다."""
    vocab_file = isolated_user_data / "vocabulary.json"
    assert not vocab_file.exists()

    data = us.load_vocabulary()

    assert vocab_file.exists()
    assert data.terms == []


# === 2. 캐시 적중/무효화 ===


def test_load_prompts_uses_cache_on_second_call(isolated_user_data: Path) -> None:
    """두 번째 호출은 동일 인스턴스를 반환 (캐시 적중)."""
    first = us.load_prompts()
    second = us.load_prompts()
    assert first is second  # 캐시된 객체


def test_load_prompts_invalidates_cache_on_mtime_change(
    isolated_user_data: Path,
) -> None:
    """외부에서 파일을 수정하면 다음 load에서 새 데이터를 반환한다."""
    first = us.load_prompts()
    original_corrector = first.corrector.system_prompt

    # 외부 에디터가 파일을 수정한 것처럼 시뮬레이션
    prompts_file = isolated_user_data / "prompts.json"
    raw = json.loads(prompts_file.read_text())
    raw["corrector"]["system_prompt"] = (
        "수정된 프롬프트입니다. [번호] 포맷을 지키세요. 추가 설명입니다."
    )
    # mtime이 확실히 달라지도록 약간 대기
    import time as _time

    _time.sleep(0.01)
    prompts_file.write_text(json.dumps(raw, ensure_ascii=False))

    second = us.load_prompts()
    assert second.corrector.system_prompt != original_corrector
    assert "수정된" in second.corrector.system_prompt


def test_force_reload_bypasses_cache(isolated_user_data: Path) -> None:
    """force_reload=True는 캐시를 무시하고 디스크를 다시 읽는다."""
    us.load_prompts()
    reloaded = us.load_prompts(force_reload=True)
    assert reloaded is not None


# === 3. 원자적 저장 + 백업 ===


def test_save_prompts_creates_backup(
    isolated_user_data: Path, valid_corrector_prompt: str
) -> None:
    """save_prompts는 기존 파일을 .bak으로 백업한다."""
    initial = us.load_prompts()

    modified = initial.model_copy(
        update={
            "corrector": us.PromptEntry(system_prompt=valid_corrector_prompt),
        }
    )
    us.save_prompts(modified)

    backup = isolated_user_data / "prompts.json.bak"
    assert backup.exists()


def test_save_prompts_updates_timestamp(
    isolated_user_data: Path, valid_corrector_prompt: str
) -> None:
    """저장 시 updated_at이 갱신된다."""
    initial = us.load_prompts()

    modified = initial.model_copy(
        update={"corrector": us.PromptEntry(system_prompt=valid_corrector_prompt)}
    )
    saved = us.save_prompts(modified)
    assert saved.corrector.updated_at is None  # entry 자체는 None (상위만 갱신)

    # 디스크에서 updated_at 확인
    prompts_file = isolated_user_data / "prompts.json"
    raw = json.loads(prompts_file.read_text())
    assert raw["updated_at"] is not None


def test_save_prompts_persists_across_loads(
    isolated_user_data: Path, valid_corrector_prompt: str
) -> None:
    """저장한 값이 다음 load에서 보존된다."""
    initial = us.load_prompts()
    modified = initial.model_copy(
        update={"corrector": us.PromptEntry(system_prompt=valid_corrector_prompt)}
    )
    us.save_prompts(modified)

    us.invalidate_cache()
    reloaded = us.load_prompts()
    assert reloaded.corrector.system_prompt == valid_corrector_prompt


# === 4. 검증 실패 ===


def test_save_prompts_rejects_missing_format_directive(
    isolated_user_data: Path,
) -> None:
    """corrector 프롬프트에 [번호] 토큰이 없으면 저장이 거부된다."""
    initial = us.load_prompts()
    with pytest.raises(us.UserSettingsValidationError):
        bad = initial.model_copy(
            update={
                "corrector": us.PromptEntry(
                    system_prompt="번호 없이 교정해주세요. 이 프롬프트는 실패해야 합니다."
                )
            }
        )
        us.save_prompts(bad)


def test_save_prompts_rejects_too_short(isolated_user_data: Path) -> None:
    """프롬프트가 너무 짧으면 Pydantic 검증에서 거부된다."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        us.PromptEntry(system_prompt="짧음")


# === 5. 손상 복구 ===


def test_load_prompts_recovers_from_corrupted_json(
    isolated_user_data: Path,
) -> None:
    """JSON 손상 시 백업에서 복구한다."""
    # 정상 파일 생성
    us.load_prompts()
    # 정상 상태를 다시 저장해서 백업 만들기
    current = us.load_prompts()
    us.save_prompts(current)  # 이 시점에 .bak 생성됨

    # 정상 파일 손상
    prompts_file = isolated_user_data / "prompts.json"
    prompts_file.write_text("{ not valid json")

    us.invalidate_cache()
    recovered = us.load_prompts()
    assert recovered is not None
    assert "[번호]" in recovered.corrector.system_prompt


def test_load_prompts_falls_back_to_default_when_all_corrupt(
    isolated_user_data: Path,
) -> None:
    """원본과 백업 모두 손상되어도 기본값으로 복구한다."""
    us.load_prompts()

    prompts_file = isolated_user_data / "prompts.json"
    prompts_file.write_text("corrupted")
    backup = isolated_user_data / "prompts.json.bak"
    backup.write_text("also corrupted")

    us.invalidate_cache()
    recovered = us.load_prompts()
    assert recovered is not None
    assert "[번호]" in recovered.corrector.system_prompt


# === 6. 용어집 CRUD 헬퍼 ===


def test_add_vocabulary_term(isolated_user_data: Path) -> None:
    """용어 추가가 정상 동작한다."""
    new = us.add_vocabulary_term(
        term="FastAPI", aliases=["패스트api", "패스트에이피아이"], note="웹 프레임워크"
    )
    assert new.id
    assert len(new.id) == 26
    assert new.term == "FastAPI"
    assert new.aliases == ["패스트api", "패스트에이피아이"]

    vocab = us.load_vocabulary(force_reload=True)
    assert len(vocab.terms) == 1
    assert vocab.terms[0].term == "FastAPI"


def test_add_vocabulary_term_rejects_duplicate(isolated_user_data: Path) -> None:
    """동일 term 중복 추가는 거부된다."""
    us.add_vocabulary_term(term="EXAONE", aliases=["엑사원"])
    with pytest.raises(us.UserSettingsValidationError, match="이미 등록"):
        us.add_vocabulary_term(term="exaone", aliases=["엑사원"])


def test_update_vocabulary_term(isolated_user_data: Path) -> None:
    """용어 부분 업데이트가 동작한다."""
    created = us.add_vocabulary_term(term="Pyannote", aliases=["파이아노트"])
    updated = us.update_vocabulary_term(
        term_id=created.id,
        aliases=["파이아노트", "피아노트"],
        note="화자분리 라이브러리",
    )
    assert updated.term == "Pyannote"  # 변경 안 함
    assert updated.aliases == ["파이아노트", "피아노트"]
    assert updated.note == "화자분리 라이브러리"


def test_update_vocabulary_term_not_found(isolated_user_data: Path) -> None:
    """존재하지 않는 ID는 에러."""
    with pytest.raises(us.UserSettingsValidationError, match="찾을 수 없"):
        us.update_vocabulary_term(term_id="NOTEXIST12345678901234567X", term="test")


def test_delete_vocabulary_term(isolated_user_data: Path) -> None:
    """용어 삭제가 동작한다."""
    created = us.add_vocabulary_term(term="테스트용어")
    us.delete_vocabulary_term(created.id)
    vocab = us.load_vocabulary(force_reload=True)
    assert len(vocab.terms) == 0


def test_delete_vocabulary_term_not_found(isolated_user_data: Path) -> None:
    """존재하지 않는 ID 삭제는 에러."""
    with pytest.raises(us.UserSettingsValidationError):
        us.delete_vocabulary_term("NOTEXIST12345678901234567X")


def test_vocabulary_aliases_dedup_and_strip(isolated_user_data: Path) -> None:
    """중복/공백 별칭은 자동 정제된다."""
    new = us.add_vocabulary_term(
        term="파이썬",
        aliases=["파이선", "파이선", "  파이썬3  ", ""],
    )
    assert new.aliases == ["파이선", "파이썬3"]


# === 7. reset 동작 ===


def test_reset_prompts_to_default(isolated_user_data: Path, valid_corrector_prompt: str) -> None:
    """reset이 기본값을 복원한다."""
    initial = us.load_prompts()
    modified = initial.model_copy(
        update={"corrector": us.PromptEntry(system_prompt=valid_corrector_prompt)}
    )
    us.save_prompts(modified)

    reset = us.reset_prompts_to_default()
    assert reset.corrector.system_prompt != valid_corrector_prompt
    assert "음성인식" in reset.corrector.system_prompt  # 기본값 특정 문구


def test_reset_vocabulary_to_default(isolated_user_data: Path) -> None:
    """용어집 reset이 동작한다."""
    us.add_vocabulary_term(term="지울용어1")
    us.add_vocabulary_term(term="지울용어2")

    reset = us.reset_vocabulary_to_default()
    assert len(reset.terms) == 0


# === 8. 스냅샷 빌더 ===


def test_build_corrector_snapshot_without_vocabulary(
    isolated_user_data: Path,
) -> None:
    """빈 용어집 시 스냅샷은 베이스 프롬프트만 사용한다."""
    snapshot = us.build_corrector_snapshot()
    assert snapshot.vocab_term_count == 0
    assert snapshot.system_prompt == snapshot.base_prompt
    assert "고유명사 사전" not in snapshot.system_prompt


def test_build_corrector_snapshot_with_vocabulary(isolated_user_data: Path) -> None:
    """용어집이 있으면 스냅샷에 주입된다."""
    us.add_vocabulary_term(term="FastAPI", aliases=["패스트api"])
    us.add_vocabulary_term(term="Pyannote", aliases=["파이아노트"], note="화자분리")

    snapshot = us.build_corrector_snapshot()
    assert snapshot.vocab_term_count == 2
    assert snapshot.system_prompt != snapshot.base_prompt
    assert "고유명사 사전" in snapshot.system_prompt
    assert "FastAPI" in snapshot.system_prompt
    assert "패스트api" in snapshot.system_prompt
    assert "Pyannote" in snapshot.system_prompt
    assert "화자분리" in snapshot.system_prompt


def test_build_corrector_snapshot_excludes_disabled(
    isolated_user_data: Path,
) -> None:
    """enabled=False 용어는 스냅샷에서 제외된다."""
    us.add_vocabulary_term(term="활성용어")
    disabled = us.add_vocabulary_term(term="비활성용어")
    us.update_vocabulary_term(term_id=disabled.id, enabled=False)

    snapshot = us.build_corrector_snapshot()
    assert snapshot.vocab_term_count == 1
    assert "활성용어" in snapshot.system_prompt
    assert "비활성용어" not in snapshot.system_prompt


def test_build_summarizer_and_chat_prompts(isolated_user_data: Path) -> None:
    """요약/채팅 프롬프트 빌더가 동작한다."""
    summarizer = us.build_summarizer_system_prompt()
    chat = us.build_chat_system_prompt()
    assert "회의록" in summarizer
    assert "AI 어시스턴트" in chat


# === 9. ULID 생성 ===


def test_ulid_format() -> None:
    """ULID는 26자 문자열이며 Crockford Base32 알파벳만 사용한다."""
    ulid = us._generate_ulid()
    assert len(ulid) == 26
    assert all(c in us._BASE32_ALPHABET for c in ulid)


def test_ulid_time_sortable() -> None:
    """두 ULID를 순차 생성하면 문자열 정렬 순서가 시간 순이다."""
    import time as _time

    first = us._generate_ulid()
    _time.sleep(0.005)
    second = us._generate_ulid()
    # 타임스탬프 부분이 앞에 있어서 문자열 비교가 시간 비교와 같음
    assert first < second or first[:10] <= second[:10]


def test_ulid_uniqueness() -> None:
    """동일 시점에 생성된 ULID도 서로 다르다."""
    ids = {us._generate_ulid() for _ in range(100)}
    assert len(ids) == 100


# === 10. init_user_settings ===


def test_init_user_settings_creates_files(isolated_user_data: Path) -> None:
    """초기화 함수가 파일을 생성한다."""
    us.init_user_settings()
    assert (isolated_user_data / "prompts.json").exists()
    assert (isolated_user_data / "vocabulary.json").exists()


def test_init_user_settings_idempotent(isolated_user_data: Path) -> None:
    """초기화를 여러 번 호출해도 안전하다."""
    us.init_user_settings()
    us.init_user_settings()
    us.init_user_settings()
    # 에러 없이 완료되면 성공


# === 11. 최대 개수 제한 ===


def test_vocabulary_max_terms_enforced(isolated_user_data: Path) -> None:
    """최대 용어 수 초과 시 거부된다."""
    # 500개 제한이지만 테스트 빠르게 하기 위해 상수 직접 패치
    import unittest.mock as mock

    with mock.patch.object(us, "_VOCAB_MAX_TERMS", 3):
        us.add_vocabulary_term(term="용어1")
        us.add_vocabulary_term(term="용어2")
        us.add_vocabulary_term(term="용어3")
        with pytest.raises(us.UserSettingsValidationError, match="최대"):
            us.add_vocabulary_term(term="용어4")
