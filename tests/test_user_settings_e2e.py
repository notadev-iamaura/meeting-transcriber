"""
사용자 설정 end-to-end 테스트.

FastAPI TestClient로 실제 HTTP 라운드트립을 통해 다음을 검증한다:
    - 초기 상태 → 편집 → 조회 → 파일 영속성
    - 용어집 전체 CRUD 라운드트립
    - /api/prompts/reset → 원상 복구
    - 잘못된 입력에 대한 에러 응답 정합성
    - 락 파일과 .bak 파일이 정상 생성되는 디스크 상태
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from api.routes import router
from core import user_settings as us


@pytest.fixture(autouse=True)
def isolated_user_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "user_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(us, "_user_data_dir", lambda: data_dir)
    us.invalidate_cache()
    yield data_dir
    us.invalidate_cache()


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_e2e_prompts_full_lifecycle(client: TestClient, isolated_user_data: Path) -> None:
    """프롬프트 전체 생명주기: 조회 → 편집 → 파일 영속성 → 재조회 → reset."""
    # 1. 초기 조회 (파일 자동 생성됨)
    resp = client.get("/api/prompts")
    assert resp.status_code == 200
    resp.json()["prompts"]
    assert (isolated_user_data / "prompts.json").exists()

    # 2. 3종 프롬프트 동시 편집
    resp = client.put(
        "/api/prompts",
        json={
            "corrector": {
                "system_prompt": (
                    "E2E 보정 프롬프트. [번호] 텍스트 포맷으로 출력하세요. 마커: E2E-CORRECTOR"
                )
            },
            "summarizer": {
                "system_prompt": (
                    "E2E 요약 프롬프트. 회의록을 마크다운으로 작성하세요. 마커: E2E-SUMMARIZER"
                )
            },
            "chat": {
                "system_prompt": (
                    "E2E 채팅 프롬프트. 회의 내용을 기반으로 답변하세요. 마커: E2E-CHAT"
                )
            },
        },
    )
    assert resp.status_code == 200
    saved = resp.json()["prompts"]
    assert "E2E-CORRECTOR" in saved["corrector"]["system_prompt"]
    assert "E2E-SUMMARIZER" in saved["summarizer"]["system_prompt"]
    assert "E2E-CHAT" in saved["chat"]["system_prompt"]

    # 3. 파일에 실제로 기록되었는지 (캐시 우회)
    raw = json.loads((isolated_user_data / "prompts.json").read_text())
    assert "E2E-CORRECTOR" in raw["corrector"]["system_prompt"]
    assert raw["updated_at"] is not None

    # 4. .bak 파일이 생성되었는지 (기본값 덮어쓰기 이전 상태 백업)
    assert (isolated_user_data / "prompts.json.bak").exists()

    # 5. 재조회 시 동일 값
    resp = client.get("/api/prompts")
    re = resp.json()["prompts"]
    assert re["corrector"]["system_prompt"] == saved["corrector"]["system_prompt"]

    # 6. reset으로 초기화
    resp = client.post("/api/prompts/reset")
    assert resp.status_code == 200
    reset_data = resp.json()["prompts"]
    assert "E2E-CORRECTOR" not in reset_data["corrector"]["system_prompt"]
    assert "[번호]" in reset_data["corrector"]["system_prompt"]


def test_e2e_vocabulary_full_crud(client: TestClient, isolated_user_data: Path) -> None:
    """용어집 전체 CRUD: 추가 3개 → 수정 → 삭제 → reset."""
    # 추가
    ids: list[str] = []
    for term, aliases, note in [
        ("FastAPI", ["패스트api"], "웹 프레임워크"),
        ("EXAONE", ["엑사원"], None),
        ("Pyannote", ["파이아노트", "피아노트"], "화자분리"),
    ]:
        resp = client.post(
            "/api/vocabulary/terms",
            json={"term": term, "aliases": aliases, "note": note},
        )
        assert resp.status_code == 201, resp.text
        ids.append(resp.json()["id"])

    # 조회
    resp = client.get("/api/vocabulary")
    data = resp.json()
    assert data["total"] == 3
    assert "FastAPI" in [t["term"] for t in data["terms"]]

    # 파일에 기록되었는지
    raw = json.loads((isolated_user_data / "vocabulary.json").read_text())
    assert len(raw["terms"]) == 3

    # 수정: Pyannote의 enabled를 false로
    resp = client.put(
        f"/api/vocabulary/terms/{ids[2]}",
        json={"enabled": False, "note": "비활성 처리"},
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False

    # 스냅샷 빌드: disabled 항목은 제외되어야 함
    snapshot = us.build_corrector_snapshot()
    assert snapshot.vocab_term_count == 2  # FastAPI + EXAONE만
    assert "Pyannote" not in snapshot.system_prompt
    assert "FastAPI" in snapshot.system_prompt

    # 삭제
    resp = client.delete(f"/api/vocabulary/terms/{ids[0]}")
    assert resp.status_code == 204

    resp = client.get("/api/vocabulary")
    assert resp.json()["total"] == 2

    # reset
    resp = client.post("/api/vocabulary/reset")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_e2e_rejects_invalid_inputs(client: TestClient) -> None:
    """잘못된 입력에 대한 에러 응답 정합성."""
    # 너무 짧은 프롬프트 → 422 (Pydantic)
    resp = client.put("/api/prompts", json={"corrector": {"system_prompt": "짧음"}})
    assert resp.status_code == 422

    # [번호] 없는 프롬프트 → 400 (저장소 계층 검증)
    resp = client.put(
        "/api/prompts",
        json={
            "corrector": {
                "system_prompt": ("포맷 지시가 없는 프롬프트입니다. 반드시 거부되어야 합니다.")
            }
        },
    )
    assert resp.status_code in (400, 422)
    assert "[번호]" in resp.json()["detail"] or "번호" in str(resp.json())

    # 빈 term
    resp = client.post("/api/vocabulary/terms", json={"term": ""})
    assert resp.status_code == 422

    # 존재하지 않는 id 삭제
    resp = client.delete("/api/vocabulary/terms/NONEXISTENT123456789012345X")
    assert resp.status_code == 400

    # 중복 term
    client.post("/api/vocabulary/terms", json={"term": "dup-test"})
    resp = client.post("/api/vocabulary/terms", json={"term": "dup-test"})
    assert resp.status_code == 400


def test_e2e_atomic_write_leaves_no_tmp_files(
    client: TestClient, isolated_user_data: Path
) -> None:
    """원자적 쓰기 후 임시 파일이 남지 않는다."""
    for i in range(5):
        client.put(
            "/api/prompts",
            json={
                "corrector": {
                    "system_prompt": (f"반복 저장 테스트 #{i}. [번호] 텍스트 포맷으로 출력하세요.")
                }
            },
        )

    # 디렉토리에 .tmp 파일이 없어야 함
    tmp_files = list(isolated_user_data.glob("*.tmp"))
    assert tmp_files == [], f"임시 파일이 남음: {tmp_files}"

    # .bak 파일은 있어야 함
    assert (isolated_user_data / "prompts.json.bak").exists()


def test_e2e_partial_update_does_not_affect_other_prompts(
    client: TestClient,
) -> None:
    """한 프롬프트만 수정해도 나머지는 그대로 유지된다."""
    # 각각 독립적으로 3번 수정
    client.put(
        "/api/prompts",
        json={
            "corrector": {
                "system_prompt": ("전용 보정 MARK-A. [번호] 텍스트 포맷으로 출력하세요.")
            }
        },
    )
    client.put(
        "/api/prompts",
        json={
            "summarizer": {"system_prompt": "전용 요약 MARK-B. 회의록을 마크다운으로 작성하세요."}
        },
    )
    client.put(
        "/api/prompts",
        json={"chat": {"system_prompt": "전용 채팅 MARK-C. 회의 내용 기반으로만 답변하세요."}},
    )

    resp = client.get("/api/prompts")
    p = resp.json()["prompts"]
    assert "MARK-A" in p["corrector"]["system_prompt"]
    assert "MARK-B" in p["summarizer"]["system_prompt"]
    assert "MARK-C" in p["chat"]["system_prompt"]
