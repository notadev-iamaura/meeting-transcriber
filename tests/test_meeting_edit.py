"""
회의 제목 / 요약 / 전사 편집 API 테스트.

PATCH /api/meetings/{id}                     — 제목 수정
PUT   /api/meetings/{id}/summary              — 요약 마크다운 수정
PUT   /api/meetings/{id}/transcript           — 전사 전체 교체
POST  /api/meetings/{id}/transcript/replace   — 패턴 치환 + 용어집 자동 등록
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from api.routes import router
from core import user_settings as us
from core.job_queue import JobQueue, JobStatus
from steps.embedder import IndexPurgeResult

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
def app_with_state(job_queue: JobQueue, isolated_base: Path) -> FastAPI:
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
def seeded_meeting(job_queue: JobQueue, isolated_base: Path) -> str:
    """테스트용 회의 1개 + correct.json + meeting_minutes.md 생성."""
    meeting_id = "meeting_20260101_120000"
    job_queue.add_job(
        meeting_id=meeting_id,
        audio_path="/tmp/test.wav",
        initial_status=JobStatus.COMPLETED.value,
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
    def test_제목_수정(self, client: TestClient, seeded_meeting: str) -> None:
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

    def test_빈_제목으로_초기화(self, client: TestClient, seeded_meeting: str) -> None:
        """빈 문자열을 보내면 자동 타임스탬프 폴백으로 돌아간다."""
        client.patch(f"/api/meetings/{seeded_meeting}", json={"title": "임시"})
        resp = client.patch(f"/api/meetings/{seeded_meeting}", json={"title": ""})
        assert resp.status_code == 200
        assert resp.json()["title"] == ""

    def test_너무_긴_제목_거부(self, client: TestClient, seeded_meeting: str) -> None:
        resp = client.patch(f"/api/meetings/{seeded_meeting}", json={"title": "가" * 201})
        # Pydantic max_length=200 또는 저장소 검증 → 400/422
        assert resp.status_code in (400, 422)

    def test_존재하지_않는_회의(self, client: TestClient) -> None:
        resp = client.patch("/api/meetings/meeting_20260101_000000", json={"title": "test"})
        assert resp.status_code == 404

    def test_공백_제목은_trim(self, client: TestClient, seeded_meeting: str) -> None:
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
        minutes = isolated_base / "outputs" / seeded_meeting / "meeting_minutes.md"
        assert minutes.read_text() == new_md

        # .bak 생성 확인
        backup = minutes.with_suffix(".md.bak")
        assert backup.exists()
        assert "## 회의 개요" in backup.read_text()  # 원본 내용

    def test_GET으로_수정본_재조회(self, client: TestClient, seeded_meeting: str) -> None:
        new_md = "## E2E\n한 줄 요약."
        client.put(
            f"/api/meetings/{seeded_meeting}/summary",
            json={"markdown": new_md},
        )
        resp = client.get(f"/api/meetings/{seeded_meeting}/summary")
        assert resp.status_code == 200
        assert "E2E" in resp.json()["markdown"]

    def test_빈_본문_거부(self, client: TestClient, seeded_meeting: str) -> None:
        resp = client.put(f"/api/meetings/{seeded_meeting}/summary", json={"markdown": ""})
        assert resp.status_code == 422  # Pydantic min_length

    def test_존재하지_않는_회의(self, client: TestClient) -> None:
        resp = client.put(
            "/api/meetings/meeting_20260101_000000/summary",
            json={"markdown": "## test"},
        )
        assert resp.status_code == 404

    def test_처리중_회의록_수정_거부(
        self,
        client: TestClient,
        seeded_meeting: str,
        isolated_base: Path,
        job_queue: JobQueue,
    ) -> None:
        """재전사·파이프라인 실행 상태에서는 기존 회의록을 바꾸지 않는다."""
        job = job_queue.get_job_by_meeting_id(seeded_meeting)
        assert job is not None
        job_queue.force_set_status(job.id, JobStatus.EMBEDDING)
        minutes = isolated_base / "outputs" / seeded_meeting / "meeting_minutes.md"
        before = minutes.read_text(encoding="utf-8")

        response = client.put(
            f"/api/meetings/{seeded_meeting}/summary",
            json={"markdown": "## 처리 중 수정"},
        )

        assert response.status_code == 409
        assert minutes.read_text(encoding="utf-8") == before

    def test_요약_파일_symlink는_조회하거나_덮어쓰지_않는다(
        self,
        client: TestClient,
        seeded_meeting: str,
        isolated_base: Path,
    ) -> None:
        """정적 파일 symlink의 외부 내용을 읽거나 수정하지 않는다."""
        minutes = isolated_base / "outputs" / seeded_meeting / "meeting_minutes.md"
        external = isolated_base.parent / "external-summary.md"
        sentinel = "외부 파일 원문"
        external.write_text(sentinel, encoding="utf-8")
        minutes.unlink()
        minutes.symlink_to(external)

        get_response = client.get(f"/api/meetings/{seeded_meeting}/summary")
        put_response = client.put(
            f"/api/meetings/{seeded_meeting}/summary",
            json={"markdown": "## 침범 시도"},
        )

        assert get_response.status_code == 409
        assert put_response.status_code == 409
        assert "SECURITY_BLOCKED" in get_response.json()["detail"]
        assert "SECURITY_BLOCKED" in put_response.json()["detail"]
        assert external.read_text(encoding="utf-8") == sentinel
        assert minutes.is_symlink()
        assert not minutes.with_suffix(".md.bak").exists()

    def test_다른_회의로_향한_요약_디렉터리_symlink를_거부한다(
        self,
        client: TestClient,
        isolated_base: Path,
    ) -> None:
        """root 내부 meeting symlink도 다른 회의 산출물로 따라가지 않는다."""
        linked_id = "meeting_20260102_120000"
        victim_dir = isolated_base / "outputs" / "meeting_20260103_120000"
        victim_dir.mkdir(parents=True)
        victim = victim_dir / "meeting_minutes.md"
        sentinel = "다른 회의 회의록"
        victim.write_text(sentinel, encoding="utf-8")
        (isolated_base / "outputs" / linked_id).symlink_to(
            victim_dir,
            target_is_directory=True,
        )

        get_response = client.get(f"/api/meetings/{linked_id}/summary")
        put_response = client.put(
            f"/api/meetings/{linked_id}/summary",
            json={"markdown": "## 잘못된 수정"},
        )

        assert get_response.status_code == 409
        assert put_response.status_code == 409
        assert victim.read_text(encoding="utf-8") == sentinel


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
        cp = isolated_base / "checkpoints" / seeded_meeting / "correct.json"
        raw = json.loads(cp.read_text())
        assert len(raw["utterances"]) == 1
        assert raw["utterances"][0]["text"] == "새로운 내용입니다."
        # .bak 확인
        assert cp.with_suffix(".json.bak").exists()

    def test_미전사_recorded_회의는_편집_409(
        self, client: TestClient, job_queue: JobQueue
    ) -> None:
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
        assert resp.status_code == 409

    def test_처리중_전사문_수정_거부(
        self,
        client: TestClient,
        seeded_meeting: str,
        isolated_base: Path,
        job_queue: JobQueue,
    ) -> None:
        """correct.json이 있어도 파이프라인 완료 전에는 전사문 편집을 막는다."""
        job = job_queue.get_job_by_meeting_id(seeded_meeting)
        assert job is not None
        job_queue.force_set_status(job.id, JobStatus.EMBEDDING)
        cp = isolated_base / "checkpoints" / seeded_meeting / "correct.json"
        before = cp.read_text()

        resp = client.put(
            f"/api/meetings/{seeded_meeting}/transcript",
            json={
                "utterances": [
                    {
                        "text": "처리 중 수정 시도",
                        "speaker": "SPEAKER_00",
                        "start": 0,
                        "end": 1,
                    }
                ]
            },
        )

        assert resp.status_code == 409
        assert cp.read_text() == before

    def test_전사_파일_symlink는_조회하거나_수정하지_않는다(
        self,
        client: TestClient,
        seeded_meeting: str,
        isolated_base: Path,
    ) -> None:
        """정적 전사 파일 symlink의 외부 JSON을 읽거나 수정하지 않는다."""
        source = isolated_base / "checkpoints" / seeded_meeting / "correct.json"
        external = isolated_base.parent / "external-correct.json"
        sentinel = source.read_text(encoding="utf-8")
        external.write_text(sentinel, encoding="utf-8")
        source.unlink()
        source.symlink_to(external)
        replacement = {
            "utterances": [
                {
                    "text": "침범 시도",
                    "speaker": "SPEAKER_00",
                    "start": 0,
                    "end": 1,
                }
            ]
        }

        get_response = client.get(f"/api/meetings/{seeded_meeting}/transcript")
        put_response = client.put(
            f"/api/meetings/{seeded_meeting}/transcript",
            json=replacement,
        )
        replace_response = client.post(
            f"/api/meetings/{seeded_meeting}/transcript/replace",
            json={"find": "파이선", "replace": "FastAPI"},
        )

        assert get_response.status_code == 409
        assert put_response.status_code == 409
        assert replace_response.status_code == 409
        assert external.read_text(encoding="utf-8") == sentinel
        assert source.is_symlink()
        assert not source.with_suffix(".json.bak").exists()

    def test_다른_회의로_향한_전사_디렉터리_symlink를_거부한다(
        self,
        client: TestClient,
        isolated_base: Path,
        job_queue: JobQueue,
    ) -> None:
        """root 내부 meeting symlink도 다른 회의 전사 파일로 따라가지 않는다."""
        linked_id = "meeting_20260104_120000"
        victim_dir = isolated_base / "outputs" / "meeting_20260105_120000"
        victim_dir.mkdir(parents=True)
        victim = victim_dir / "corrected.json"
        sentinel = json.dumps(
            {
                "utterances": [
                    {
                        "text": "다른 회의 원문",
                        "speaker": "SPEAKER_00",
                        "start": 0,
                        "end": 1,
                    }
                ],
                "num_speakers": 1,
            },
            ensure_ascii=False,
        )
        victim.write_text(sentinel, encoding="utf-8")
        (isolated_base / "outputs" / linked_id).symlink_to(
            victim_dir,
            target_is_directory=True,
        )
        job_queue.add_job(
            meeting_id=linked_id,
            audio_path="/tmp/linked.wav",
            initial_status=JobStatus.COMPLETED.value,
        )

        get_response = client.get(f"/api/meetings/{linked_id}/transcript")
        put_response = client.put(
            f"/api/meetings/{linked_id}/transcript",
            json={
                "utterances": [
                    {
                        "text": "잘못된 수정",
                        "speaker": "SPEAKER_00",
                        "start": 0,
                        "end": 1,
                    }
                ]
            },
        )
        replace_response = client.post(
            f"/api/meetings/{linked_id}/transcript/replace",
            json={"find": "다른", "replace": "침범"},
        )

        assert get_response.status_code == 409
        assert put_response.status_code == 409
        assert replace_response.status_code == 409
        assert victim.read_text(encoding="utf-8") == sentinel

    def test_손상된_UTF8_전사는_모든_조회_편집_API에서_500을_반환한다(
        self,
        client: TestClient,
        seeded_meeting: str,
        isolated_base: Path,
    ) -> None:
        """UnicodeDecodeError를 TestClient 밖으로 누출하지 않는다."""
        source = isolated_base / "checkpoints" / seeded_meeting / "correct.json"
        corrupt = b"\xff\xfe\x00broken"
        source.write_bytes(corrupt)

        get_response = client.get(f"/api/meetings/{seeded_meeting}/transcript")
        put_response = client.put(
            f"/api/meetings/{seeded_meeting}/transcript",
            json={
                "utterances": [
                    {
                        "text": "수정 시도",
                        "speaker": "SPEAKER_00",
                        "start": 0,
                        "end": 1,
                    }
                ]
            },
        )
        replace_response = client.post(
            f"/api/meetings/{seeded_meeting}/transcript/replace",
            json={"find": "broken", "replace": "fixed"},
        )

        assert get_response.status_code == 500
        assert put_response.status_code == 500
        assert replace_response.status_code == 500
        assert source.read_bytes() == corrupt


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
        cp = isolated_base / "checkpoints" / seeded_meeting / "correct.json"
        raw = json.loads(cp.read_text())
        assert "파이선" not in raw["utterances"][0]["text"]
        assert "FastAPI" in raw["utterances"][0]["text"]
        assert raw["utterances"][0]["was_corrected"] is True
        # 3번째 발화는 변경 없어야 함
        assert raw["utterances"][2]["text"] == "다른 얘기도 해볼까요."
        assert raw["utterances"][2]["was_corrected"] is False

    def test_패턴_없음_0_changes(self, client: TestClient, seeded_meeting: str) -> None:
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

    def test_처리중_모두바꾸기_거부(
        self,
        client: TestClient,
        seeded_meeting: str,
        isolated_base: Path,
        job_queue: JobQueue,
    ) -> None:
        """correct.json이 있어도 파이프라인 완료 전에는 패턴 치환을 막는다."""
        job = job_queue.get_job_by_meeting_id(seeded_meeting)
        assert job is not None
        job_queue.force_set_status(job.id, JobStatus.EMBEDDING)
        cp = isolated_base / "checkpoints" / seeded_meeting / "correct.json"
        before = cp.read_text()

        resp = client.post(
            f"/api/meetings/{seeded_meeting}/transcript/replace",
            json={
                "find": "파이선",
                "replace": "FastAPI",
                "add_to_vocabulary": False,
            },
        )

        assert resp.status_code == 409
        assert cp.read_text() == before

    def test_find과_replace_동일_거부(self, client: TestClient, seeded_meeting: str) -> None:
        resp = client.post(
            f"/api/meetings/{seeded_meeting}/transcript/replace",
            json={"find": "같음", "replace": "같음"},
        )
        assert resp.status_code == 400

    def test_용어집_자동_등록_신규(self, client: TestClient, seeded_meeting: str) -> None:
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

    def test_용어집_기존_term에_alias_추가(self, client: TestClient, seeded_meeting: str) -> None:
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

    def test_용어집_중복_alias_재등록_무해(self, client: TestClient, seeded_meeting: str) -> None:
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


def _seed_retranscribe_concurrency_meeting(
    job_queue: JobQueue,
    isolated_base: Path,
    meeting_id: str,
) -> tuple[Path, Path]:
    """재전사·수동 편집 경쟁 테스트용 완료 회의와 산출물을 만든다."""
    audio = isolated_base / "audio_input" / f"{meeting_id}.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"safe-audio")
    job_queue.add_job(
        meeting_id=meeting_id,
        audio_path=str(audio),
        initial_status=JobStatus.COMPLETED.value,
    )
    correct = isolated_base / "checkpoints" / meeting_id / "correct.json"
    correct.parent.mkdir(parents=True, exist_ok=True)
    correct.write_text(
        json.dumps(
            {
                "utterances": [
                    {
                        "text": "기존 전사",
                        "original_text": "기존 전사",
                        "speaker": "SPEAKER_00",
                        "start": 0.0,
                        "end": 1.0,
                        "was_corrected": False,
                    }
                ],
                "num_speakers": 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output_dir = isolated_base / "outputs" / meeting_id
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "meeting_minutes.md").write_text("기존 회의록", encoding="utf-8")
    return audio, correct


def _local_stt_selection() -> SimpleNamespace:
    """재전사 동시성 테스트용 로컬 STT 스냅샷을 반환한다."""
    return SimpleNamespace(
        provider="local",
        model="mlx-community/test",
        external_upload=False,
    )


class TestMeetingMutationConcurrency:
    @pytest.mark.asyncio
    async def test_편집이_먼저면_재전사가_최신_편집까지_stage한다(
        self,
        app_with_state: FastAPI,
        job_queue: JobQueue,
        isolated_base: Path,
    ) -> None:
        """늦은 편집 파일이 canonical checkpoint로 재생성되지 않는다."""
        meeting_id = "meeting_20260106_120000"
        _, correct = _seed_retranscribe_concurrency_meeting(
            job_queue,
            isolated_base,
            meeting_id,
        )
        writer_entered = threading.Event()
        allow_writer = threading.Event()

        from api.routers import meeting_detail

        original_writer = meeting_detail._atomic_write_json_pinned

        def _blocking_writer(*args: object, **kwargs: object) -> None:
            writer_entered.set()
            if not allow_writer.wait(timeout=5):
                raise TimeoutError("수동 편집 writer 해제 대기 타임아웃")
            original_writer(*args, **kwargs)

        transport = httpx.ASGITransport(app=app_with_state)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with (
                patch(
                    "core.transcription_models.selection_from_config",
                    return_value=_local_stt_selection(),
                ),
                patch(
                    "api.routers.meeting_detail._purge_meeting_search_index",
                    new=AsyncMock(return_value=IndexPurgeResult(meeting_id=meeting_id)),
                ),
                patch(
                    "api.routers.meeting_detail._atomic_write_json_pinned",
                    side_effect=_blocking_writer,
                ),
            ):
                edit_task = asyncio.create_task(
                    client.put(
                        f"/api/meetings/{meeting_id}/transcript",
                        json={
                            "utterances": [
                                {
                                    "text": "최신 수동 편집",
                                    "speaker": "SPEAKER_00",
                                    "start": 0,
                                    "end": 1,
                                }
                            ]
                        },
                    )
                )
                assert await asyncio.to_thread(writer_entered.wait, 2)
                retranscribe_task = asyncio.create_task(
                    client.post(f"/api/meetings/{meeting_id}/re-transcribe")
                )
                try:
                    await asyncio.sleep(0.1)
                    assert not retranscribe_task.done()
                finally:
                    allow_writer.set()
                edit_response, retranscribe_response = await asyncio.gather(
                    edit_task,
                    retranscribe_task,
                )

        assert edit_response.status_code == 200
        assert retranscribe_response.status_code == 200
        assert not correct.exists()
        assert not list((isolated_base / "checkpoints").glob(".retranscribe-*"))
        job = job_queue.get_job_by_meeting_id(meeting_id)
        assert job is not None
        assert job.status == JobStatus.QUEUED.value

    @pytest.mark.asyncio
    async def test_재전사가_먼저면_늦은_편집은_queued_상태에서_거부된다(
        self,
        app_with_state: FastAPI,
        job_queue: JobQueue,
        isolated_base: Path,
    ) -> None:
        """재전사 claim 뒤 시작한 편집은 staging 완료 후 상태를 다시 확인한다."""
        meeting_id = "meeting_20260107_120000"
        _, correct = _seed_retranscribe_concurrency_meeting(
            job_queue,
            isolated_base,
            meeting_id,
        )
        stage_entered = threading.Event()
        allow_stage = threading.Event()

        from api.routers import meeting_detail

        original_stage = meeting_detail._stage_retranscribe_artifacts

        def _blocking_stage(*args: object, **kwargs: object) -> None:
            stage_entered.set()
            if not allow_stage.wait(timeout=5):
                raise TimeoutError("재전사 staging 해제 대기 타임아웃")
            original_stage(*args, **kwargs)

        transport = httpx.ASGITransport(app=app_with_state)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with (
                patch(
                    "core.transcription_models.selection_from_config",
                    return_value=_local_stt_selection(),
                ),
                patch(
                    "api.routers.meeting_detail._purge_meeting_search_index",
                    new=AsyncMock(return_value=IndexPurgeResult(meeting_id=meeting_id)),
                ),
                patch(
                    "api.routers.meeting_detail._stage_retranscribe_artifacts",
                    side_effect=_blocking_stage,
                ),
            ):
                retranscribe_task = asyncio.create_task(
                    client.post(f"/api/meetings/{meeting_id}/re-transcribe")
                )
                assert await asyncio.to_thread(stage_entered.wait, 2)
                edit_task = asyncio.create_task(
                    client.put(
                        f"/api/meetings/{meeting_id}/transcript",
                        json={
                            "utterances": [
                                {
                                    "text": "늦은 수동 편집",
                                    "speaker": "SPEAKER_00",
                                    "start": 0,
                                    "end": 1,
                                }
                            ]
                        },
                    )
                )
                try:
                    await asyncio.sleep(0.1)
                    assert not edit_task.done()
                finally:
                    allow_stage.set()
                retranscribe_response, edit_response = await asyncio.gather(
                    retranscribe_task,
                    edit_task,
                )

        assert retranscribe_response.status_code == 200
        assert edit_response.status_code == 409
        assert not correct.exists()
        job = job_queue.get_job_by_meeting_id(meeting_id)
        assert job is not None
        assert job.status == JobStatus.QUEUED.value

    @pytest.mark.asyncio
    async def test_지연요약_background_task가_끝난_뒤에만_재전사한다(
        self,
        app_with_state: FastAPI,
        job_queue: JobQueue,
        isolated_base: Path,
    ) -> None:
        """요약 성공 응답 직후 재전사가 와도 늦은 산출물이 canonical에 남지 않는다."""
        meeting_id = "meeting_20260108_120000"
        _seed_retranscribe_concurrency_meeting(
            job_queue,
            isolated_base,
            meeting_id,
        )
        checkpoint_dir = isolated_base / "checkpoints" / meeting_id
        output_dir = isolated_base / "outputs" / meeting_id
        (checkpoint_dir / "merge.json").write_text("{}", encoding="utf-8")
        (checkpoint_dir / "pipeline_state.json").write_text("{}", encoding="utf-8")

        llm_entered = asyncio.Event()
        release_llm = asyncio.Event()

        async def _delayed_llm(_meeting_id: str) -> SimpleNamespace:
            llm_entered.set()
            await release_llm.wait()
            (checkpoint_dir / "correct.json").write_text("late-correct", encoding="utf-8")
            (checkpoint_dir / "summarize.json").write_text("late-summary", encoding="utf-8")
            (checkpoint_dir / "pipeline_state.json").write_text("late-state", encoding="utf-8")
            (output_dir / "meeting_minutes.md").write_text("late-minutes", encoding="utf-8")
            return SimpleNamespace(status="completed")

        pipeline = SimpleNamespace(
            _get_checkpoint_path=lambda _meeting_id, step: checkpoint_dir / f"{step.value}.json",
            _get_state_path=lambda _meeting_id: checkpoint_dir / "pipeline_state.json",
            validate_llm_steps_non_destructive=lambda _meeting_id: None,
            run_llm_steps=_delayed_llm,
        )
        app_with_state.state.pipeline_manager = pipeline
        app_with_state.state.running_tasks = set()

        transport = httpx.ASGITransport(app=app_with_state)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with (
                patch(
                    "core.transcription_models.selection_from_config",
                    return_value=_local_stt_selection(),
                ),
                patch(
                    "api.routers.meeting_detail._purge_meeting_search_index",
                    new=AsyncMock(return_value=IndexPurgeResult(meeting_id=meeting_id)),
                ),
            ):
                summarize_response = await client.post(
                    f"/api/meetings/{meeting_id}/summarize",
                )
                assert summarize_response.status_code == 200
                await asyncio.wait_for(llm_entered.wait(), timeout=2)

                retranscribe_task = asyncio.create_task(
                    client.post(f"/api/meetings/{meeting_id}/re-transcribe")
                )
                try:
                    await asyncio.sleep(0.1)
                    assert retranscribe_task.done() is False
                finally:
                    release_llm.set()
                retranscribe_response = await retranscribe_task

        assert retranscribe_response.status_code == 200
        for name in ("merge.json", "correct.json", "summarize.json", "pipeline_state.json"):
            assert not (checkpoint_dir / name).exists()
        assert not (output_dir / "meeting_minutes.md").exists()
        job = job_queue.get_job_by_meeting_id(meeting_id)
        assert job is not None
        assert job.status == JobStatus.QUEUED.value

    @pytest.mark.asyncio
    async def test_재색인이_끝난_뒤에만_재전사해_stale_index_산출물을_제거한다(
        self,
        app_with_state: FastAPI,
        job_queue: JobQueue,
        isolated_base: Path,
    ) -> None:
        """재색인 read→index→publish 전체가 재전사 staging과 겹치지 않는다."""
        meeting_id = "meeting_20260109_120000"
        _seed_retranscribe_concurrency_meeting(
            job_queue,
            isolated_base,
            meeting_id,
        )
        checkpoint_dir = isolated_base / "checkpoints" / meeting_id
        reindex_entered = asyncio.Event()
        release_reindex = asyncio.Event()

        async def _delayed_reindex(*_args: object, **_kwargs: object) -> dict[str, object]:
            reindex_entered.set()
            await release_reindex.wait()
            (checkpoint_dir / "chunk.json").write_text("late-chunk", encoding="utf-8")
            (checkpoint_dir / "embed.json").write_text("late-embed", encoding="utf-8")
            return {"chunks": 1, "chroma_stored": True, "fts_stored": True}

        app_with_state.state.pipeline_manager = SimpleNamespace(_model_manager=object())
        transport = httpx.ASGITransport(app=app_with_state)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with (
                patch(
                    "core.transcription_models.selection_from_config",
                    return_value=_local_stt_selection(),
                ),
                patch(
                    "api.routers.meeting_detail._purge_meeting_search_index",
                    new=AsyncMock(return_value=IndexPurgeResult(meeting_id=meeting_id)),
                ),
                patch(
                    "api.routers.reindex._reindex_meeting",
                    new=AsyncMock(side_effect=_delayed_reindex),
                ),
            ):
                reindex_task = asyncio.create_task(
                    client.post(f"/api/meetings/{meeting_id}/reindex")
                )
                await asyncio.wait_for(reindex_entered.wait(), timeout=2)
                retranscribe_task = asyncio.create_task(
                    client.post(f"/api/meetings/{meeting_id}/re-transcribe")
                )
                try:
                    await asyncio.sleep(0.1)
                    assert retranscribe_task.done() is False
                finally:
                    release_reindex.set()
                reindex_response, retranscribe_response = await asyncio.gather(
                    reindex_task,
                    retranscribe_task,
                )

        assert reindex_response.status_code == 200
        assert retranscribe_response.status_code == 200
        assert not (checkpoint_dir / "chunk.json").exists()
        assert not (checkpoint_dir / "embed.json").exists()
        job = job_queue.get_job_by_meeting_id(meeting_id)
        assert job is not None
        assert job.status == JobStatus.QUEUED.value
