"""
회의 제목 / 요약 / 전사 편집 API 테스트.

PATCH /api/meetings/{id}                     — 제목 수정
PUT   /api/meetings/{id}/summary              — 요약 마크다운 수정
PUT   /api/meetings/{id}/transcript           — 전사 전체 교체
POST  /api/meetings/{id}/transcript/replace   — 패턴 치환 + 용어집 자동 등록
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from api.routes import router
from core import user_settings as us
from core.job_queue import JobQueue, JobStatus


# === 공용 fixture ===


@pytest.fixture
def isolated_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """임시 base dir + user_data 격리."""
    base = tmp_path / "meeting-transcriber-test"
    base.mkdir(parents=True, exist_ok=True)

    # user_settings 격리
    monkeypatch.setattr(us, "_user_data_dir", lambda: base / "user_data")
    us.invalidate_cache()
    yield base
    us.invalidate_cache()


@pytest.fixture
def job_queue(tmp_path: Path) -> JobQueue:
    q = JobQueue(db_path=tmp_path / "jobs.db")
    q.initialize()
    return q


@pytest.fixture
def app_with_state(
    job_queue: JobQueue, isolated_base: Path
) -> FastAPI:
    """FastAPI 앱 + 필수 state 설정."""
    from types import SimpleNamespace

    app = FastAPI()
    app.include_router(router)

    # config mock: outputs_dir, checkpoints_dir, base_dir
    outputs = isolated_base / "outputs"
    checkpoints = isolated_base / "checkpoints"
    outputs.mkdir(parents=True, exist_ok=True)
    checkpoints.mkdir(parents=True, exist_ok=True)

    paths = SimpleNamespace(
        resolved_outputs_dir=outputs,
        resolved_checkpoints_dir=checkpoints,
        resolved_base_dir=isolated_base,
    )
    config = SimpleNamespace(
        paths=paths,
        stt=SimpleNamespace(model_name="test"),
        llm=SimpleNamespace(backend="mlx"),
    )
    app.state.config = config
    # 기존 라우트들은 app.state.job_queue 가 AsyncJobQueue 래퍼이고
    # `.queue` 속성으로 raw JobQueue 를 노출한다고 가정한다. 테스트용 최소 래퍼.
    app.state.job_queue = SimpleNamespace(queue=job_queue)
    return app


@pytest.fixture
def client(app_with_state: FastAPI) -> TestClient:
    return TestClient(app_with_state)


@pytest.fixture
def seeded_meeting(
    job_queue: JobQueue, isolated_base: Path
) -> str:
    """테스트용 회의 1개 + correct.json + meeting_minutes.md 생성."""
    meeting_id = "meeting_20260101_120000"
    job_queue.add_job(
        meeting_id=meeting_id,
        audio_path="/tmp/test.wav",
        initial_status=JobStatus.RECORDED.value,
    )

    # 전사 파일
    correct = isolated_base / "checkpoints" / meeting_id / "correct.json"
    correct.parent.mkdir(parents=True, exist_ok=True)
    correct.write_text(
        json.dumps(
            {
                "utterances": [
                    {
                        "text": "안녕하세요 파이선 관련 회의입니다.",
                        "original_text": "안녕하세요 파이선 관련 회의입니다.",
                        "speaker": "SPEAKER_00",
                        "start": 0.0,
                        "end": 2.0,
                        "was_corrected": False,
                    },
                    {
                        "text": "네, 파이선 성능 이슈 확인했어요.",
                        "original_text": "네, 파이선 성능 이슈 확인했어요.",
                        "speaker": "SPEAKER_01",
                        "start": 2.0,
                        "end": 4.0,
                        "was_corrected": False,
                    },
                    {
                        "text": "다른 얘기도 해볼까요.",
                        "original_text": "다른 얘기도 해볼까요.",
                        "speaker": "SPEAKER_00",
                        "start": 4.0,
                        "end": 6.0,
                        "was_corrected": False,
                    },
                ],
                "num_speakers": 2,
                "audio_path": "/tmp/test.wav",
                "total_corrected": 0,
                "total_failed": 0,
            },
            ensure_ascii=False,
        )
    )

    # 요약 파일
    minutes = isolated_base / "outputs" / meeting_id / "meeting_minutes.md"
    minutes.parent.mkdir(parents=True, exist_ok=True)
    minutes.write_text(
        "## 회의 개요\n- 참석자: SPEAKER_00, SPEAKER_01\n\n## 주요 안건\n- 파이선 관련 논의\n"
    )

    return meeting_id


# === 제목 수정 ===


class TestPatchMeetingTitle:
    def test_제목_수정(
        self, client: TestClient, seeded_meeting: str
    ) -> None:
        resp = client.patch(
            f"/api/meetings/{seeded_meeting}",
            json={"title": "Q1 제품 로드맵 회의"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Q1 제품 로드맵 회의"

        # 재조회 시 유지
        get_resp = client.get(f"/api/meetings/{seeded_meeting}")
        assert get_resp.json()["title"] == "Q1 제품 로드맵 회의"

    def test_빈_제목으로_초기화(
        self, client: TestClient, seeded_meeting: str
    ) -> None:
        """빈 문자열을 보내면 자동 타임스탬프 폴백으로 돌아간다."""
        client.patch(
            f"/api/meetings/{seeded_meeting}", json={"title": "임시"}
        )
        resp = client.patch(
            f"/api/meetings/{seeded_meeting}", json={"title": ""}
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == ""

    def test_너무_긴_제목_거부(
        self, client: TestClient, seeded_meeting: str
    ) -> None:
        resp = client.patch(
            f"/api/meetings/{seeded_meeting}", json={"title": "가" * 201}
        )
        # Pydantic max_length=200 또는 저장소 검증 → 400/422
        assert resp.status_code in (400, 422)

    def test_존재하지_않는_회의(self, client: TestClient) -> None:
        resp = client.patch(
            "/api/meetings/meeting_20260101_000000", json={"title": "test"}
        )
        assert resp.status_code == 404

    def test_공백_제목은_trim(
        self, client: TestClient, seeded_meeting: str
    ) -> None:
        resp = client.patch(
            f"/api/meetings/{seeded_meeting}",
            json={"title": "  여백 테스트  "},
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "여백 테스트"


# === 요약 편집 ===


class TestUpdateSummary:
    def test_요약_덮어쓰기(
        self,
        client: TestClient,
        seeded_meeting: str,
        isolated_base: Path,
    ) -> None:
        new_md = "## 수정된 회의 개요\n\n완전히 다른 내용입니다.\n"
        resp = client.put(
            f"/api/meetings/{seeded_meeting}/summary",
            json={"markdown": new_md},
        )
        assert resp.status_code == 200
        assert resp.json()["markdown"] == new_md

        # 파일 내용 확인
        minutes = (
            isolated_base / "outputs" / seeded_meeting / "meeting_minutes.md"
        )
        assert minutes.read_text() == new_md

        # .bak 생성 확인
        backup = minutes.with_suffix(".md.bak")
        assert backup.exists()
        assert "## 회의 개요" in backup.read_text()  # 원본 내용

    def test_GET으로_수정본_재조회(
        self, client: TestClient, seeded_meeting: str
    ) -> None:
        new_md = "## E2E\n한 줄 요약."
        client.put(
            f"/api/meetings/{seeded_meeting}/summary",
            json={"markdown": new_md},
        )
        resp = client.get(f"/api/meetings/{seeded_meeting}/summary")
        assert resp.status_code == 200
        assert "E2E" in resp.json()["markdown"]

    def test_빈_본문_거부(
        self, client: TestClient, seeded_meeting: str
    ) -> None:
        resp = client.put(
            f"/api/meetings/{seeded_meeting}/summary", json={"markdown": ""}
        )
        assert resp.status_code == 422  # Pydantic min_length

    def test_존재하지_않는_회의(self, client: TestClient) -> None:
        resp = client.put(
            "/api/meetings/meeting_20260101_000000/summary",
            json={"markdown": "## test"},
        )
        assert resp.status_code == 404


# === 전사 편집 ===


class TestUpdateTranscript:
    def test_전체_교체(
        self,
        client: TestClient,
        seeded_meeting: str,
        isolated_base: Path,
    ) -> None:
        new_utterances = [
            {
                "text": "새로운 내용입니다.",
                "original_text": "새로운 내용입니다.",
                "speaker": "SPEAKER_00",
                "start": 0.0,
                "end": 1.5,
                "was_corrected": True,
            },
        ]
        resp = client.put(
            f"/api/meetings/{seeded_meeting}/transcript",
            json={"utterances": new_utterances},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_utterances"] == 1
        assert data["utterances"][0]["text"] == "새로운 내용입니다."

        # 파일 확인
        cp = (
            isolated_base / "checkpoints" / seeded_meeting / "correct.json"
        )
        raw = json.loads(cp.read_text())
        assert len(raw["utterances"]) == 1
        assert raw["utterances"][0]["text"] == "새로운 내용입니다."
        # .bak 확인
        assert cp.with_suffix(".json.bak").exists()

    def test_전사_파일_없음_404(self, client: TestClient, job_queue: JobQueue) -> None:
        job_queue.add_job(
            meeting_id="meeting_20260202_222222",
            audio_path="/tmp/x.wav",
            initial_status=JobStatus.RECORDED.value,
        )
        resp = client.put(
            "/api/meetings/meeting_20260202_222222/transcript",
            json={
                "utterances": [
                    {
                        "text": "test",
                        "speaker": "SPEAKER_00",
                        "start": 0,
                        "end": 1,
                    }
                ]
            },
        )
        assert resp.status_code == 404


# === 전사 패턴 치환 + 용어집 자동 등록 ===


class TestTranscriptReplace:
    def test_패턴_치환_모두_바꾸기(
        self,
        client: TestClient,
        seeded_meeting: str,
        isolated_base: Path,
    ) -> None:
        resp = client.post(
            f"/api/meetings/{seeded_meeting}/transcript/replace",
            json={
                "find": "파이선",
                "replace": "FastAPI",
                "add_to_vocabulary": False,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # 첫 번째 발화 1회 + 두 번째 발화 1회 = 2번 치환
        assert data["changes"] == 2
        assert data["updated_utterances"] == 2
        assert data["vocabulary_action"] is None

        # 파일 확인
        cp = (
            isolated_base / "checkpoints" / seeded_meeting / "correct.json"
        )
        raw = json.loads(cp.read_text())
        assert "파이선" not in raw["utterances"][0]["text"]
        assert "FastAPI" in raw["utterances"][0]["text"]
        assert raw["utterances"][0]["was_corrected"] is True
        # 3번째 발화는 변경 없어야 함
        assert raw["utterances"][2]["text"] == "다른 얘기도 해볼까요."
        assert raw["utterances"][2]["was_corrected"] is False

    def test_패턴_없음_0_changes(
        self, client: TestClient, seeded_meeting: str
    ) -> None:
        resp = client.post(
            f"/api/meetings/{seeded_meeting}/transcript/replace",
            json={
                "find": "존재하지않는단어",
                "replace": "바꿀값",
                "add_to_vocabulary": False,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["changes"] == 0
        assert resp.json()["updated_utterances"] == 0

    def test_find과_replace_동일_거부(
        self, client: TestClient, seeded_meeting: str
    ) -> None:
        resp = client.post(
            f"/api/meetings/{seeded_meeting}/transcript/replace",
            json={"find": "같음", "replace": "같음"},
        )
        assert resp.status_code == 400

    def test_용어집_자동_등록_신규(
        self, client: TestClient, seeded_meeting: str
    ) -> None:
        """기존에 term 이 없으면 새로 생성하고 find 를 alias 로 등록."""
        resp = client.post(
            f"/api/meetings/{seeded_meeting}/transcript/replace",
            json={
                "find": "파이선",
                "replace": "FastAPI",
                "add_to_vocabulary": True,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["changes"] == 2
        assert data["vocabulary_action"] == "term_created"
        assert data["vocabulary_term_id"] is not None

        # 용어집 확인
        vocab = us.load_vocabulary(force_reload=True)
        terms = [t for t in vocab.terms if t.term == "FastAPI"]
        assert len(terms) == 1
        assert "파이선" in terms[0].aliases

    def test_용어집_기존_term에_alias_추가(
        self, client: TestClient, seeded_meeting: str
    ) -> None:
        """기존 term 이 있으면 alias 에 find 추가."""
        # 미리 용어집에 등록
        us.add_vocabulary_term(term="FastAPI", aliases=["fastapi"])

        resp = client.post(
            f"/api/meetings/{seeded_meeting}/transcript/replace",
            json={
                "find": "파이선",
                "replace": "FastAPI",
                "add_to_vocabulary": True,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["vocabulary_action"] == "alias_added"

        vocab = us.load_vocabulary(force_reload=True)
        terms = [t for t in vocab.terms if t.term == "FastAPI"]
        assert len(terms) == 1
        assert "파이선" in terms[0].aliases
        assert "fastapi" in terms[0].aliases  # 기존 alias 유지

    def test_용어집_중복_alias_재등록_무해(
        self, client: TestClient, seeded_meeting: str
    ) -> None:
        """이미 같은 alias 가 있으면 alias_already_exists 반환."""
        us.add_vocabulary_term(term="FastAPI", aliases=["파이선"])

        resp = client.post(
            f"/api/meetings/{seeded_meeting}/transcript/replace",
            json={
                "find": "파이선",
                "replace": "FastAPI",
                "add_to_vocabulary": True,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["vocabulary_action"] == "alias_already_exists"
