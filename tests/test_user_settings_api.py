"""
사용자 설정 API 엔드포인트 통합 테스트.

/api/prompts 와 /api/vocabulary 라우트의 동작을 FastAPI TestClient로 검증한다.
실제 파일 I/O는 각 테스트마다 격리된 tmp_path 디렉토리를 사용한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from api.routes import router
from core import user_settings as us


@pytest.fixture(autouse=True)
def isolated_user_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """각 테스트마다 임시 user_data 디렉토리로 격리하고 캐시를 비운다."""
    data_dir = tmp_path / "user_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(us, "_user_data_dir", lambda: data_dir)
    us.invalidate_cache()
    yield data_dir
    us.invalidate_cache()


@pytest.fixture
def client() -> TestClient:
    """FastAPI 테스트 클라이언트를 생성한다."""
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# === 프롬프트 API ===


def test_get_prompts_returns_default(client: TestClient) -> None:
    """초기 상태에서 GET /api/prompts는 기본값을 반환한다."""
    resp = client.get("/api/prompts")
    assert resp.status_code == 200
    data = resp.json()
    assert "prompts" in data
    p = data["prompts"]
    assert "corrector" in p
    assert "summarizer" in p
    assert "chat" in p
    assert "[번호]" in p["corrector"]["system_prompt"]


def test_put_prompts_updates_corrector(client: TestClient) -> None:
    """PUT /api/prompts가 보정 프롬프트를 변경한다."""
    new_text = (
        "새로운 보정 프롬프트입니다. 반드시 [번호] 텍스트 포맷으로 출력하세요. 추가 설명 없이."
    )
    resp = client.put(
        "/api/prompts",
        json={"corrector": {"system_prompt": new_text}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["prompts"]["corrector"]["system_prompt"] == new_text

    # 재조회 시에도 유지
    resp2 = client.get("/api/prompts")
    assert resp2.json()["prompts"]["corrector"]["system_prompt"] == new_text


def test_put_prompts_rejects_missing_format_directive(client: TestClient) -> None:
    """[번호] 토큰이 없는 corrector 프롬프트는 400으로 거부된다."""
    resp = client.put(
        "/api/prompts",
        json={
            "corrector": {
                "system_prompt": ("번호 없이 교정해주세요 이 프롬프트는 반드시 실패해야 합니다")
            }
        },
    )
    assert resp.status_code == 400


def test_put_prompts_rejects_too_short(client: TestClient) -> None:
    """너무 짧은 프롬프트는 422 (Pydantic) 로 거부된다."""
    resp = client.put(
        "/api/prompts",
        json={"corrector": {"system_prompt": "짧음"}},
    )
    assert resp.status_code == 422


def test_put_prompts_partial_update_preserves_other_fields(
    client: TestClient,
) -> None:
    """summarizer만 변경해도 corrector는 유지된다."""
    before = client.get("/api/prompts").json()["prompts"]
    original_corrector = before["corrector"]["system_prompt"]

    new_summarizer = "새로운 요약 프롬프트입니다. 회의록을 마크다운으로 작성하세요. 간결하게."
    resp = client.put(
        "/api/prompts",
        json={"summarizer": {"system_prompt": new_summarizer}},
    )
    assert resp.status_code == 200
    updated = resp.json()["prompts"]
    assert updated["summarizer"]["system_prompt"] == new_summarizer
    assert updated["corrector"]["system_prompt"] == original_corrector


def test_post_prompts_reset(client: TestClient) -> None:
    """POST /api/prompts/reset이 기본값으로 복원한다."""
    # 먼저 변경
    client.put(
        "/api/prompts",
        json={
            "chat": {
                "system_prompt": (
                    "사용자 정의 채팅 프롬프트입니다. 이것을 reset으로 없앨 것입니다."
                )
            }
        },
    )

    resp = client.post("/api/prompts/reset")
    assert resp.status_code == 200
    reset = resp.json()["prompts"]
    assert "사용자 정의" not in reset["chat"]["system_prompt"]
    assert "AI 어시스턴트" in reset["chat"]["system_prompt"]


# === 용어집 API ===


def test_get_vocabulary_initially_empty(client: TestClient) -> None:
    """초기 용어집은 빈 리스트."""
    resp = client.get("/api/vocabulary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["terms"] == []
    assert data["total"] == 0


def test_corrections_merge_and_retry_preserving_existing_term(client: TestClient) -> None:
    """여러 표기를 합치고 재접수에서도 비활성 여부·메모·기존 별칭을 유지한다."""
    old = us.add_vocabulary_term("Python", ["파이손"], note="언어", enabled=False)
    pairs = [
        {"alias": "파이손", "term": "python"},
        {"alias": " 파이선 ", "term": " Python "},
        {"alias": "파이선", "term": "PYTHON"},
        {"alias": "깃헙", "term": "GitHub"},
    ]
    url = "/api/vocabulary/corrections"
    response = client.post(url, json={"corrections": pairs})
    assert response.status_code == 200
    assert response.json() == {"added": 2, "existing": 1}
    assert client.post(url, json={"corrections": pairs}).json() == {"added": 0, "existing": 3}
    us.invalidate_cache()
    first, second = us.load_vocabulary().terms
    assert first == old.model_copy(update={"aliases": ["파이손", "파이선"]})
    assert second.term == "GitHub" and second.aliases == ["깃헙"]


@pytest.mark.parametrize("aliases", [["오픈에이아이"], ["오픈에이아이", "OpenAI", "openai"]])
def test_corrections_fix_existing_canonical_case(client: TestClient, aliases: list[str]) -> None:
    """정답 자체의 영문 대소문자 편집을 반영하고 같은 alias를 재등록하지 않는다."""
    before = us.add_vocabulary_term("openai", aliases, note="제품", enabled=False)
    url = "/api/vocabulary/corrections"
    edit = {"corrections": [{"alias": "openai", "term": "OpenAI"}]}
    assert client.post(url, json=edit).json() == {"added": 1, "existing": 0}
    assert client.post(url, json=edit).json() == {"added": 0, "existing": 1}
    saved = us.load_vocabulary(force_reload=True).terms
    assert saved == [
        before.model_copy(update={"term": "OpenAI", "aliases": ["오픈에이아이", "openai"]})
    ]


def test_correction_batch_is_atomic_on_limit_or_io_failure(
    client: TestClient, isolated_user_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """한 쌍이 별칭 상한을 넘거나 쓰기가 실패해도 파일과 공개 캐시를 보존한다."""
    us.add_vocabulary_term("부서", [f"표기{i}" for i in range(20)])
    path = isolated_user_data / "vocabulary.json"
    before = path.read_bytes()
    new = {"alias": "파이선", "term": "Python"}
    url = "/api/vocabulary/corrections"
    assert (
        client.post(
            url, json={"corrections": [new, {"alias": "부써", "term": "부서"}]}
        ).status_code
        == 400
    )
    assert path.read_bytes() == before
    assert [item.term for item in us.load_vocabulary().terms] == ["부서"]

    def fail(path: Path, data: dict) -> None:
        raise OSError("테스트 쓰기 실패")

    monkeypatch.setattr(us, "_atomic_write_json", fail)
    assert client.post(url, json={"corrections": [new]}).status_code == 500
    assert path.read_bytes() == before
    assert [item.term for item in us.load_vocabulary().terms] == ["부서"]


@pytest.mark.parametrize(
    "pairs",
    [
        [],
        [{"alias": " ", "term": "정답"}],
        [{"alias": "오인식", "term": " "}],
        [{"alias": "표기 ", "term": "표기"}],
        [{"alias": "오" * 101, "term": "정답"}],
        [{"alias": "오인식", "term": "정" * 101}],
    ],
)
def test_corrections_reject_invalid_pairs(client: TestClient, pairs: list[dict[str, str]]) -> None:
    """빈 입력이나 변경이 없는 입력은 저장 전에 거부한다."""
    assert (
        client.post("/api/vocabulary/corrections", json={"corrections": pairs}).status_code == 422
    )
    assert us.load_vocabulary().terms == []


def test_post_vocabulary_term_creates_with_ulid(client: TestClient) -> None:
    """용어 추가 시 201과 ULID가 반환된다."""
    resp = client.post(
        "/api/vocabulary/terms",
        json={
            "term": "FastAPI",
            "aliases": ["패스트api", "패스트에이피아이"],
            "note": "웹 프레임워크",
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["term"] == "FastAPI"
    assert data["aliases"] == ["패스트api", "패스트에이피아이"]
    assert data["note"] == "웹 프레임워크"
    assert len(data["id"]) == 26
    assert data["enabled"] is True
    assert data["created_at"] is not None


def test_post_vocabulary_term_duplicate_returns_400(client: TestClient) -> None:
    """중복 term은 400."""
    client.post("/api/vocabulary/terms", json={"term": "EXAONE"})
    resp = client.post("/api/vocabulary/terms", json={"term": "exaone"})
    assert resp.status_code == 400
    assert "이미 등록" in resp.json()["detail"]


def test_put_vocabulary_term_partial_update(client: TestClient) -> None:
    """용어 부분 업데이트."""
    created = client.post(
        "/api/vocabulary/terms",
        json={"term": "Pyannote", "aliases": ["파이아노트"]},
    ).json()

    resp = client.put(
        f"/api/vocabulary/terms/{created['id']}",
        json={"aliases": ["파이아노트", "피아노트"], "note": "화자분리"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["term"] == "Pyannote"  # 유지
    assert data["aliases"] == ["파이아노트", "피아노트"]
    assert data["note"] == "화자분리"


def test_put_vocabulary_term_not_found(client: TestClient) -> None:
    """존재하지 않는 ID 업데이트는 400."""
    resp = client.put(
        "/api/vocabulary/terms/NOTEXIST12345678901234567X",
        json={"term": "something"},
    )
    assert resp.status_code == 400


def test_delete_vocabulary_term(client: TestClient) -> None:
    """용어 삭제 후 204, 목록에서 사라짐."""
    created = client.post("/api/vocabulary/terms", json={"term": "삭제대상"}).json()

    resp = client.delete(f"/api/vocabulary/terms/{created['id']}")
    assert resp.status_code == 204

    list_resp = client.get("/api/vocabulary")
    assert list_resp.json()["total"] == 0


def test_delete_vocabulary_term_not_found(client: TestClient) -> None:
    """존재하지 않는 ID 삭제는 400."""
    resp = client.delete("/api/vocabulary/terms/NOTEXIST12345678901234567X")
    assert resp.status_code == 400


def test_post_vocabulary_reset(client: TestClient) -> None:
    """용어집 초기화."""
    client.post("/api/vocabulary/terms", json={"term": "A"})
    client.post("/api/vocabulary/terms", json={"term": "B"})
    client.post("/api/vocabulary/terms", json={"term": "C"})

    resp = client.post("/api/vocabulary/reset")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_vocabulary_lifecycle(client: TestClient) -> None:
    """전체 생명주기: 추가 → 조회 → 수정 → 삭제."""
    # 추가
    a = client.post(
        "/api/vocabulary/terms",
        json={"term": "파이썬", "aliases": ["파이선"]},
    ).json()
    b = client.post(
        "/api/vocabulary/terms",
        json={"term": "Pyannote", "aliases": ["파이아노트"]},
    ).json()

    # 조회 (2개)
    list1 = client.get("/api/vocabulary").json()
    assert list1["total"] == 2

    # 수정
    client.put(
        f"/api/vocabulary/terms/{a['id']}",
        json={"enabled": False},
    )

    # 삭제
    client.delete(f"/api/vocabulary/terms/{b['id']}")

    # 최종 확인
    final = client.get("/api/vocabulary").json()
    assert final["total"] == 1
    assert final["terms"][0]["id"] == a["id"]
    assert final["terms"][0]["enabled"] is False
