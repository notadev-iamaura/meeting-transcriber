"""
Playwright 기반 E2E 테스트 — 제목/요약/전사 편집 + 모두 바꾸기 + 용어집 자동 등록.

실제 FastAPI 서버를 subprocess 로 별도 포트에 띄우고, Chromium 브라우저로
UI 를 조작해 전체 플로우를 검증한다. 테스트 데이터는 `MT_BASE_DIR` 로 격리된
tmp 디렉토리에만 생성되어 사용자 환경을 오염시키지 않는다.

전제 조건:
    - playwright + pytest-playwright 설치됨 (`.venv/bin/pip install playwright pytest-playwright`)
    - chromium 설치됨 (`.venv/bin/playwright install chromium`)
    - 포트 8766 가용 (테스트 전용)

실행:
    .venv/bin/pytest tests/test_e2e_edit_playwright.py -v
    .venv/bin/pytest tests/test_e2e_edit_playwright.py -v --headed   # 브라우저 창 표시
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect, sync_playwright

# 모든 테스트에 'e2e' 마커 적용 — pytest-playwright 와 pytest-asyncio 가
# 동시에 활성화될 때 async 테스트가 깨지므로 이 파일은 기본 실행에서 제외되고
# `pytest -m e2e` 로 명시적으로 실행해야 한다.
pytestmark = pytest.mark.e2e

# =============================================================================
# 설정
# =============================================================================

TEST_PORT = 8766
BASE_URL = f"http://127.0.0.1:{TEST_PORT}"
REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_READY_TIMEOUT = 30  # seconds


# =============================================================================
# 헬퍼
# =============================================================================


def _port_in_use(port: int) -> bool:
    """포트가 사용 중인지 확인."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_for_server(url: str, timeout: float = SERVER_READY_TIMEOUT) -> None:
    """서버가 /api/status 에 응답할 때까지 대기."""
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"{url}/api/status", timeout=2
            ) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_error = e
            time.sleep(0.5)
    raise RuntimeError(
        f"테스트 서버가 {timeout}초 안에 기동하지 않았습니다: {last_error}"
    )


def _seed_meeting(base_dir: Path, meeting_id: str) -> None:
    """테스트용 회의 데이터를 디스크에 시드한다.

    - pipeline.db 의 jobs 테이블에 completed 상태 row 삽입
    - checkpoints/{id}/correct.json (3개 발화, 파이선 오인식 포함)
    - outputs/{id}/meeting_minutes.md (초기 AI 요약)
    """
    import sqlite3

    from core.job_queue import JobQueue

    # 1. JobQueue 초기화 + 작업 등록
    db_path = base_dir / "pipeline.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    queue = JobQueue(db_path=db_path)
    queue.initialize()
    queue.close()

    audio_path = base_dir / "audio_input" / f"{meeting_id}.wav"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"fake-audio-for-test")

    # INSERT OR IGNORE 로 idempotent 등록 (fixture 여러 번 호출 대비)
    # 상태는 항상 completed, title 은 빈 문자열로 초기화
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        INSERT OR IGNORE INTO jobs
            (meeting_id, audio_path, status, retry_count, max_retries,
             error_message, created_at, updated_at, title)
        VALUES (?, ?, 'completed', 0, 3, '', ?, ?, '')
        """,
        (meeting_id, str(audio_path), "2026-04-07T14:00:00", "2026-04-07T14:00:00"),
    )
    # 이미 있으면 상태·title 초기화
    conn.execute(
        "UPDATE jobs SET status='completed', title='' WHERE meeting_id=?",
        (meeting_id,),
    )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    conn.close()

    # 2. 전사 체크포인트
    correct_path = base_dir / "checkpoints" / meeting_id / "correct.json"
    correct_path.parent.mkdir(parents=True, exist_ok=True)
    correct_path.write_text(
        json.dumps(
            {
                "utterances": [
                    {
                        "text": "안녕하세요 오늘 파이선 성능 이야기를 해볼게요.",
                        "original_text": "안녕하세요 오늘 파이선 성능 이야기를 해볼게요.",
                        "speaker": "SPEAKER_00",
                        "start": 0.0,
                        "end": 3.0,
                        "was_corrected": False,
                    },
                    {
                        "text": "네, 파이선 최신 버전에서 큰 폭으로 좋아졌다고 들었어요.",
                        "original_text": "네, 파이선 최신 버전에서 큰 폭으로 좋아졌다고 들었어요.",
                        "speaker": "SPEAKER_01",
                        "start": 3.0,
                        "end": 6.0,
                        "was_corrected": False,
                    },
                    {
                        "text": "회의는 여기까지 하고 내일 또 뵙겠습니다.",
                        "original_text": "회의는 여기까지 하고 내일 또 뵙겠습니다.",
                        "speaker": "SPEAKER_00",
                        "start": 6.0,
                        "end": 9.0,
                        "was_corrected": False,
                    },
                ],
                "num_speakers": 2,
                "audio_path": str(audio_path),
                "total_corrected": 0,
                "total_failed": 0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 3. 요약 마크다운
    minutes_path = (
        base_dir / "outputs" / meeting_id / "meeting_minutes.md"
    )
    minutes_path.parent.mkdir(parents=True, exist_ok=True)
    minutes_path.write_text(
        "## 회의 개요\n"
        "- 참석자: SPEAKER_00, SPEAKER_01\n"
        "- 주요 주제: 파이선 성능 논의\n\n"
        "## 주요 안건\n"
        "- 파이선 최신 버전 성능 개선 검토\n\n"
        "## 결정 사항\n"
        "- 없음\n\n"
        "## 액션 아이템\n"
        "- 없음\n",
        encoding="utf-8",
    )


# =============================================================================
# Fixtures — 세션 범위 (서버 한 번만 기동)
# =============================================================================


@pytest.fixture(scope="session")
def test_base_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """세션 전체에서 사용할 격리된 base dir."""
    base = tmp_path_factory.mktemp("e2e-meeting-transcriber")
    # audio_input, outputs, checkpoints 등은 시드 함수가 생성
    return base


@pytest.fixture(scope="session")
def seeded_meeting_id(test_base_dir: Path) -> str:
    """시드된 회의 ID를 반환."""
    meeting_id = "meeting_20260407_140000"
    _seed_meeting(test_base_dir, meeting_id)
    return meeting_id


@pytest.fixture(scope="session")
def server(test_base_dir: Path, seeded_meeting_id: str):
    """테스트 서버 subprocess 기동 + 종료."""
    if _port_in_use(TEST_PORT):
        pytest.fail(
            f"포트 {TEST_PORT} 가 이미 사용 중입니다. 테스트 전용 포트를 확인하세요."
        )

    # 서버 subprocess
    env = os.environ.copy()
    env["MT_BASE_DIR"] = str(test_base_dir)
    env["MT_SERVER_PORT"] = str(TEST_PORT)
    env["MT_LOG_LEVEL"] = "warning"  # 테스트 로그 노이즈 감소
    # pyannote 모델 다운로드를 피하기 위해 파이프라인 매니저 초기화 건너뛰도록
    # 해야 하는데, 현재 구조상 --no-menubar 기동 시 PipelineManager가 만들어진다.
    # 그러나 우리는 API 만 쓰므로 PipelineManager 가 실패해도 /api/meetings
    # /transcript /summary /PATCH 엔드포인트는 동작한다 (앞서 구현된 에러 핸들링 덕분).
    # 따라서 HUGGINGFACE_TOKEN 설정은 선택 사항. 기존 token 이 env 에 있으면 그대로 사용.

    log_file = test_base_dir / "server.log"
    log_fd = open(log_file, "w", encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "main.py", "--no-menubar"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_fd,
        stderr=subprocess.STDOUT,
    )

    try:
        _wait_for_server(BASE_URL, timeout=SERVER_READY_TIMEOUT)
        yield proc
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_fd.close()


@pytest.fixture(scope="session")
def browser():
    """Playwright 브라우저 — 세션 내 1개."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(server, browser, test_base_dir: Path, seeded_meeting_id: str):
    """각 테스트마다 새 컨텍스트 + 페이지.

    시드된 correct.json / meeting_minutes.md 는 이전 테스트가 수정했을 수 있으므로
    테스트 시작 전에 원본 상태로 복원한다.
    """
    # 원본 상태 복원 (test isolation)
    _seed_meeting(test_base_dir, seeded_meeting_id)

    # user_settings 초기화 (이전 테스트가 용어집에 추가한 내용 제거)
    user_data = test_base_dir / "user_data"
    if user_data.exists():
        for f in ("vocabulary.json", "vocabulary.json.bak"):
            fp = user_data / f
            if fp.exists():
                fp.unlink()

    # PATCH title 로 이전 테스트의 title 초기화 (API 호출)
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(
            f"{BASE_URL}/api/meetings/{seeded_meeting_id}",
            data=json.dumps({"title": ""}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
    except urllib.error.URLError:
        pass  # 초기화 실패는 테스트에 영향 없음

    context = browser.new_context()
    page = context.new_page()
    yield page
    context.close()


# =============================================================================
# E2E 테스트
# =============================================================================


def _open_viewer(page: Page, meeting_id: str) -> None:
    """뷰어로 직접 네비게이션 + 초기 로딩 대기."""
    page.goto(f"{BASE_URL}/app/viewer/{meeting_id}", wait_until="networkidle")
    # meetingInfo (제목·상태) 가 뜰 때까지 대기
    expect(page.locator("#viewerMeetingTitle")).to_be_visible(timeout=10000)


class TestTitleEdit:
    def test_제목_기본_표시_타임스탬프(
        self, page: Page, seeded_meeting_id: str
    ) -> None:
        """title 이 비어있으면 meeting_id 의 타임스탬프가 표시된다."""
        _open_viewer(page, seeded_meeting_id)
        title_text = page.locator(".viewer-title-text")
        # meeting_20260407_140000 → "2026-04-07 14:00"
        expect(title_text).to_contain_text("2026-04-07 14:00")

    def test_제목_인라인_편집_저장(
        self, page: Page, seeded_meeting_id: str
    ) -> None:
        """제목 텍스트 클릭 → input → Enter → 변경 저장."""
        _open_viewer(page, seeded_meeting_id)
        page.locator(".viewer-title-text").click()

        input_el = page.locator(".viewer-title-input")
        expect(input_el).to_be_visible(timeout=3000)
        expect(input_el).to_be_focused()

        input_el.fill("Q1 제품 로드맵 회의")
        input_el.press("Enter")

        # 편집 모드 해제 후 새 텍스트 표시
        expect(page.locator(".viewer-title-text")).to_contain_text(
            "Q1 제품 로드맵 회의", timeout=5000
        )
        # 인풋이 사라져야 함
        expect(page.locator(".viewer-title-input")).to_have_count(0)
        # 브라우저 탭 제목도 갱신
        page.wait_for_function(
            "document.title.includes('Q1 제품 로드맵 회의')", timeout=3000
        )

    def test_제목_Esc_취소(
        self, page: Page, seeded_meeting_id: str
    ) -> None:
        """편집 중 Esc 누르면 원래 값 유지."""
        _open_viewer(page, seeded_meeting_id)
        original = page.locator(".viewer-title-text").text_content()

        page.locator(".viewer-title-text").click()
        input_el = page.locator(".viewer-title-input")
        expect(input_el).to_be_visible()
        input_el.fill("버려질 제목")
        input_el.press("Escape")

        expect(page.locator(".viewer-title-text")).to_have_text(original or "")

    def test_제목_빈값으로_초기화(
        self, page: Page, seeded_meeting_id: str
    ) -> None:
        """빈 문자열 저장 시 타임스탬프 폴백으로 돌아간다."""
        _open_viewer(page, seeded_meeting_id)
        # 1) 먼저 제목 설정
        page.locator(".viewer-title-text").click()
        page.locator(".viewer-title-input").fill("일시적 제목")
        page.locator(".viewer-title-input").press("Enter")
        expect(page.locator(".viewer-title-text")).to_contain_text(
            "일시적 제목", timeout=5000
        )

        # 2) 빈 값으로 초기화
        page.locator(".viewer-title-text").click()
        input_el = page.locator(".viewer-title-input")
        input_el.fill("")
        input_el.press("Enter")

        expect(page.locator(".viewer-title-text")).to_contain_text(
            "2026-04-07 14:00", timeout=5000
        )


class TestSummaryEdit:
    def test_요약_편집_저장(
        self, page: Page, seeded_meeting_id: str, test_base_dir: Path
    ) -> None:
        """요약 탭 → 편집 → textarea 수정 → 저장."""
        _open_viewer(page, seeded_meeting_id)

        # 요약 탭 열기
        page.locator('#viewerTabSummary').click()
        expect(page.locator("#viewerPanelSummary")).to_be_visible()

        # 렌더된 마크다운에서 편집 버튼 찾기
        edit_btn = page.locator(".summary-toolbar button").filter(
            has_text="편집"
        )
        expect(edit_btn).to_be_visible(timeout=10000)
        edit_btn.click()

        textarea = page.locator(".summary-textarea")
        expect(textarea).to_be_visible()
        new_content = (
            "## 회의 개요\n\n**E2E 테스트로 수정된 내용**\n\n"
            "- 참석자: SPEAKER_00, SPEAKER_01\n"
            "- 수정 마커: EDIT-SUMMARY-MARKER-123\n"
        )
        textarea.fill(new_content)
        page.locator(".summary-edit-actions button").filter(
            has_text="저장"
        ).click()

        # 편집 모드 종료 + 렌더된 결과에 마커 포함
        expect(page.locator(".summary-rendered")).to_be_visible(timeout=5000)
        expect(page.locator(".summary-rendered")).to_contain_text(
            "EDIT-SUMMARY-MARKER-123"
        )

        # 디스크 파일에도 반영
        minutes_path = (
            test_base_dir / "outputs" / seeded_meeting_id / "meeting_minutes.md"
        )
        assert "EDIT-SUMMARY-MARKER-123" in minutes_path.read_text(
            encoding="utf-8"
        )
        # .bak 파일 생성 확인
        assert minutes_path.with_suffix(".md.bak").exists()

    def test_요약_편집_취소(
        self, page: Page, seeded_meeting_id: str
    ) -> None:
        """편집 취소 시 원래 렌더링으로 복귀."""
        _open_viewer(page, seeded_meeting_id)
        page.locator('#viewerTabSummary').click()

        edit_btn = page.locator(".summary-toolbar button").filter(
            has_text="편집"
        )
        expect(edit_btn).to_be_visible(timeout=10000)
        edit_btn.click()

        textarea = page.locator(".summary-textarea")
        expect(textarea).to_be_visible()
        textarea.fill("취소될 내용")

        page.locator(".summary-edit-actions button").filter(
            has_text="취소"
        ).click()

        # 원래 렌더링 복귀
        expect(page.locator(".summary-rendered")).to_be_visible()
        expect(page.locator(".summary-rendered")).not_to_contain_text(
            "취소될 내용"
        )


class TestTranscriptEdit:
    def test_개별_발화_더블클릭_편집(
        self, page: Page, seeded_meeting_id: str, test_base_dir: Path
    ) -> None:
        """발화 텍스트 더블클릭 → textarea → 수정 → 저장 버튼."""
        _open_viewer(page, seeded_meeting_id)

        # 전사 탭 (기본 활성)
        first_utterance = page.locator(".utterance-text").first
        expect(first_utterance).to_be_visible(timeout=10000)
        expect(first_utterance).to_contain_text("파이선")

        first_utterance.dblclick()

        textarea = page.locator(".utterance-textarea")
        expect(textarea).to_be_visible(timeout=3000)
        textarea.fill("개별 편집된 발화입니다. FastAPI 멋집니다.")

        # 저장 버튼
        page.locator(".utterance-edit-actions button").filter(
            has_text="저장"
        ).click()

        # 재렌더된 발화에 새 텍스트
        expect(page.locator(".utterance-text").first).to_contain_text(
            "개별 편집된 발화입니다", timeout=5000
        )

        # 디스크 파일에도 반영
        correct_path = (
            test_base_dir / "checkpoints" / seeded_meeting_id / "correct.json"
        )
        data = json.loads(correct_path.read_text(encoding="utf-8"))
        assert "개별 편집" in data["utterances"][0]["text"]
        assert data["utterances"][0]["was_corrected"] is True

    def test_발화_편집_Esc_취소(
        self, page: Page, seeded_meeting_id: str
    ) -> None:
        """편집 중 Esc → 원래 텍스트 유지."""
        _open_viewer(page, seeded_meeting_id)

        first_utterance = page.locator(".utterance-text").first
        expect(first_utterance).to_be_visible(timeout=10000)
        original = first_utterance.text_content()

        first_utterance.dblclick()
        textarea = page.locator(".utterance-textarea")
        expect(textarea).to_be_visible()
        textarea.fill("버려질 편집")
        textarea.press("Escape")

        expect(page.locator(".utterance-text").first).to_have_text(
            original or ""
        )


class TestBulkReplaceAndVocabulary:
    def test_모두_바꾸기_버튼_존재(
        self, page: Page, seeded_meeting_id: str
    ) -> None:
        """completed 상태 + 전사 로드 후 '모두 바꾸기' 버튼이 표시된다."""
        _open_viewer(page, seeded_meeting_id)
        replace_btn = page.locator(".viewer-action-btn.replace")
        expect(replace_btn).to_be_visible(timeout=10000)
        expect(replace_btn).to_contain_text("모두 바꾸기")

    def test_모두_바꾸기_패턴_치환(
        self, page: Page, seeded_meeting_id: str, test_base_dir: Path
    ) -> None:
        """파이선 → FastAPI 치환 후 모든 발화에 반영."""
        _open_viewer(page, seeded_meeting_id)

        # 초기: 첫 번째·두 번째 발화에 "파이선" 포함
        expect(page.locator(".utterance-text").first).to_contain_text("파이선")

        page.locator(".viewer-action-btn.replace").click()

        modal = page.locator("#transcriptReplaceModal")
        expect(modal).to_be_visible(timeout=3000)

        page.locator("#replaceFind").fill("파이선")
        page.locator("#replaceReplace").fill("FastAPI")

        # "용어집에도 추가" 기본 체크 확인
        vocab_cb = page.locator("#replaceAddVocab")
        expect(vocab_cb).to_be_checked()

        # 적용 버튼
        page.locator("#replaceApplyBtn").click()

        # 모달이 닫혀야 함
        expect(page.locator("#transcriptReplaceModal")).to_have_count(
            0, timeout=5000
        )

        # 치환 반영 — 최소 0.5초 후 DOM 재렌더 기대
        page.wait_for_timeout(1000)
        first_text = page.locator(".utterance-text").first.text_content() or ""
        assert "파이선" not in first_text
        assert "FastAPI" in first_text

        # 디스크 확인
        correct_path = (
            test_base_dir
            / "checkpoints"
            / seeded_meeting_id
            / "correct.json"
        )
        data = json.loads(correct_path.read_text(encoding="utf-8"))
        utterance_texts = [u["text"] for u in data["utterances"]]
        for t in utterance_texts[:2]:
            assert "파이선" not in t
            assert "FastAPI" in t
        # 세 번째 발화는 원래부터 "파이선" 없음 → 그대로
        assert "여기까지" in utterance_texts[2]

    def test_용어집_자동_등록_HTTP_확인(
        self, page: Page, seeded_meeting_id: str, test_base_dir: Path
    ) -> None:
        """모두 바꾸기 후 용어집에 term 이 신규 등록되었는지 /api/vocabulary 로 확인."""
        _open_viewer(page, seeded_meeting_id)

        page.locator(".viewer-action-btn.replace").click()
        expect(page.locator("#transcriptReplaceModal")).to_be_visible()

        page.locator("#replaceFind").fill("파이선")
        page.locator("#replaceReplace").fill("FastAPI")
        # 기본 체크 상태에서 적용
        page.locator("#replaceApplyBtn").click()
        expect(page.locator("#transcriptReplaceModal")).to_have_count(
            0, timeout=5000
        )

        # 백엔드 용어집 API 로 확인
        import urllib.request

        with urllib.request.urlopen(
            f"{BASE_URL}/api/vocabulary", timeout=5
        ) as resp:
            vocab = json.loads(resp.read().decode())

        terms = [t for t in vocab["terms"] if t["term"] == "FastAPI"]
        assert len(terms) == 1, f"FastAPI term 이 용어집에 없음: {vocab}"
        assert "파이선" in terms[0]["aliases"]
        assert "전사 편집에서 자동 등록" in (terms[0].get("note") or "")

    def test_find과_replace_동일_거부(
        self, page: Page, seeded_meeting_id: str
    ) -> None:
        """find 와 replace 가 같으면 에러 표시."""
        _open_viewer(page, seeded_meeting_id)
        page.locator(".viewer-action-btn.replace").click()
        expect(page.locator("#transcriptReplaceModal")).to_be_visible()

        page.locator("#replaceFind").fill("같음")
        page.locator("#replaceReplace").fill("같음")
        page.locator("#replaceApplyBtn").click()

        # 모달이 닫히지 않고 에러 메시지 표시
        expect(page.locator("#transcriptReplaceModal")).to_be_visible()
        expect(page.locator("#replaceError")).to_contain_text("같아")


class TestFullEditingFlow:
    def test_종단간_시나리오_제목_요약_전사_용어집(
        self,
        page: Page,
        seeded_meeting_id: str,
        test_base_dir: Path,
    ) -> None:
        """사용자가 수행할 전체 편집 플로우를 한 번에 검증.

        1) 제목 편집
        2) 요약 편집
        3) 전사 모두 바꾸기 + 용어집 등록
        4) 모든 변경사항이 파일·API 에 반영됐는지 종합 확인
        """
        _open_viewer(page, seeded_meeting_id)

        # 1) 제목 편집
        page.locator(".viewer-title-text").click()
        page.locator(".viewer-title-input").fill("E2E 종합 시나리오")
        page.locator(".viewer-title-input").press("Enter")
        expect(page.locator(".viewer-title-text")).to_contain_text(
            "E2E 종합 시나리오", timeout=5000
        )

        # 2) 요약 편집
        page.locator("#viewerTabSummary").click()
        page.locator(".summary-toolbar button").filter(
            has_text="편집"
        ).click()
        page.locator(".summary-textarea").fill(
            "## E2E 종합 요약\n- 마커: E2E-FULL-FLOW\n"
        )
        page.locator(".summary-edit-actions button").filter(
            has_text="저장"
        ).click()
        expect(page.locator(".summary-rendered")).to_contain_text(
            "E2E-FULL-FLOW", timeout=5000
        )

        # 3) 전사 탭으로 돌아가서 모두 바꾸기
        page.locator("#viewerTabTranscript").click()
        # 전사 탭이 활성화될 때까지 대기
        expect(page.locator(".viewer-action-btn.replace")).to_be_visible(
            timeout=5000
        )
        page.locator(".viewer-action-btn.replace").click()
        expect(page.locator("#transcriptReplaceModal")).to_be_visible()

        page.locator("#replaceFind").fill("파이선")
        page.locator("#replaceReplace").fill("Pyannote")
        page.locator("#replaceApplyBtn").click()
        expect(page.locator("#transcriptReplaceModal")).to_have_count(
            0, timeout=5000
        )

        # 4) 검증
        page.wait_for_timeout(1000)

        # 제목 API
        import urllib.request

        with urllib.request.urlopen(
            f"{BASE_URL}/api/meetings/{seeded_meeting_id}", timeout=5
        ) as resp:
            meeting = json.loads(resp.read())
        assert meeting["title"] == "E2E 종합 시나리오"

        # 요약 파일
        minutes = (
            test_base_dir / "outputs" / seeded_meeting_id / "meeting_minutes.md"
        )
        assert "E2E-FULL-FLOW" in minutes.read_text(encoding="utf-8")
        assert minutes.with_suffix(".md.bak").exists()

        # 전사 파일
        correct = (
            test_base_dir / "checkpoints" / seeded_meeting_id / "correct.json"
        )
        data = json.loads(correct.read_text(encoding="utf-8"))
        first_text = data["utterances"][0]["text"]
        assert "파이선" not in first_text
        assert "Pyannote" in first_text

        # 용어집
        with urllib.request.urlopen(
            f"{BASE_URL}/api/vocabulary", timeout=5
        ) as resp:
            vocab = json.loads(resp.read())
        terms = [t for t in vocab["terms"] if t["term"] == "Pyannote"]
        assert len(terms) == 1
        assert "파이선" in terms[0]["aliases"]
