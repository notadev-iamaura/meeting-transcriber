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

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import Page, Route, expect, sync_playwright

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
            with urllib.request.urlopen(f"{url}/api/status", timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_error = e
            time.sleep(0.5)
    raise RuntimeError(f"테스트 서버가 {timeout}초 안에 기동하지 않았습니다: {last_error}")


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
        "UPDATE jobs SET status='completed', title='', error_message='', "
        "stt_provider='', stt_model='' WHERE meeting_id=?",
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
    minutes_path = base_dir / "outputs" / meeting_id / "meeting_minutes.md"
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
        pytest.fail(f"포트 {TEST_PORT} 가 이미 사용 중입니다. 테스트 전용 포트를 확인하세요.")

    # 서버 subprocess
    env = os.environ.copy()
    env["MT_BASE_DIR"] = str(test_base_dir)
    test_config = test_base_dir / "config.yaml"
    test_config.write_text(
        (REPO_ROOT / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    env["MT_CONFIG_PATH"] = str(test_config)
    env["MT_SERVER_PORT"] = str(TEST_PORT)
    env["MT_LOG_LEVEL"] = "warning"  # 테스트 로그 노이즈 감소
    # 편집 UI/API 테스트는 실제 모델 추론·오디오 감시를 실행하지 않는다.
    # desktop 프로필은 편집 후 자동 재색인이 회의 lease를 보유하므로 다음
    # fixture 초기화가 모델 로드에 종속된다. 전용 api-test 프로필로 격리한다.

    log_file = test_base_dir / "server.log"
    log_fd = open(log_file, "w", encoding="utf-8")

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from config import load_config; "
            "from api.server import create_app; import uvicorn; "
            "config = load_config(); "
            "uvicorn.run(create_app(config, runtime_profile='api-test'), "
            "host='127.0.0.1', port=config.server.port)",
        ],
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


@pytest.fixture(scope="module")
def browser():
    """Playwright 브라우저 — 모듈 내 1개."""
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


def test_self_speaker_assign_change_clear_and_export(
    page: Page, seeded_meeting_id: str, test_base_dir: Path
) -> None:
    """화자 버튼에서 회의 단위 지정·변경·해제하며 새로고침과 내보내기에도 일치한다."""
    url = f"{BASE_URL}/app/viewer/{seeded_meeting_id}"
    page.goto(url)
    names = page.locator(".utterance-speaker")
    expect(names).to_have_text(["참석자 1", "참석자 2", "참석자 1"])
    names.last.focus()
    page.keyboard.press("Enter")
    dialog = page.get_by_role("dialog", name="내 화자 지정")
    expect(dialog.get_by_role("combobox")).to_have_value("SPEAKER_00")
    dialog.get_by_role("button", name="나로 지정").click()
    expect(names).to_have_text(["나", "참석자 2", "나"])
    expect(names.last).to_be_focused()
    expect(page.locator(".utterance-badge.is-self svg")).to_have_count(2)
    expect(page.locator(".speaker-chip.is-self")).to_have_text("나직접 지정")
    page.reload()
    expect(names).to_have_text(["나", "참석자 2", "나"])
    page.locator(".viewer-more-actions > summary").click()
    # 외부 클립보드에는 접근하지 않고 제품 복사 버튼의 결과를 확인한다.
    page.evaluate(
        "MeetingApp.copyToClipboard = async text => {document.body.dataset.copied = text; return true}"
    )
    page.get_by_role("button", name="전사문을 클립보드로 복사").click()
    with page.expect_download() as download:
        page.get_by_role("button", name="전사문을 텍스트 파일로 다운로드").click()
    downloaded = test_base_dir / "self-speaker-export.txt"
    download.value.save_as(downloaded)
    for contents in (downloaded.read_text(), page.locator("body").get_attribute("data-copied")):
        assert contents and contents.count("[나]") == 2 and contents.count("[참석자 2]") == 1
    page.locator(".viewer-more-actions > summary").click()
    names.nth(1).click()
    change = page.get_by_role("dialog", name="내 화자 변경")
    expect(change.get_by_role("combobox")).to_have_value("SPEAKER_01")
    change.get_by_role("button", name="나로 지정").click()
    expect(names).to_have_text(["참석자 1", "나", "참석자 1"])
    page.get_by_role("button", name="내 화자 변경", exact=True).click()
    change.get_by_role("combobox").select_option("")
    change.get_by_role("button", name="지정 해제").click()
    page.reload()
    expect(names).to_have_text(["참석자 1", "참석자 2", "참석자 1"])


def test_self_speaker_dialog_refreshes_after_conflicting_change(
    page: Page, test_base_dir: Path
) -> None:
    """다른 창이 먼저 바꾼 화자가 오래 열린 선택창에 의해 덮이지 않는다."""
    meeting_id = "meeting_20260407_140010"
    _seed_meeting(test_base_dir, meeting_id)
    page.goto(f"{BASE_URL}/app/viewer/{meeting_id}")
    page.locator(".utterance-speaker").first.click()
    endpoint = f"{BASE_URL}/api/meetings/{meeting_id}"
    transcript = page.request.get(f"{endpoint}/transcript").json()
    response = page.request.put(
        f"{endpoint}/self-speaker",
        data={
            "speaker_id": "SPEAKER_01",
            "expected_self_speaker_id": None,
            "expected_revision": transcript["self_speaker_revision"],
        },
    )
    assert response.ok
    page.get_by_role("button", name="나로 지정").click()
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.locator(".utterance-speaker")).to_have_text(["참석자 1", "나", "참석자 1"])


def test_self_speaker_changes_preserve_an_open_utterance_editor(
    page: Page, test_base_dir: Path
) -> None:
    """창 안팎의 화자 변경 시 미저장 발화 문구와 저장 동작을 유지한다."""
    meeting_id = "meeting_20260407_140030"
    _seed_meeting(test_base_dir, meeting_id)
    page.goto(f"{BASE_URL}/app/viewer/{meeting_id}")
    page.locator(".utterance-text").first.dblclick()
    editor = page.locator(".utterance-textarea")
    draft = "저장 전 회의록 수정 사항"
    editor.fill(draft)
    names = page.locator(".utterance-speaker")
    names.nth(1).click()
    page.get_by_role("button", name="나로 지정").click()
    expect(names).to_have_text(["참석자 1", "나", "참석자 1"])
    expect(editor).to_have_value(draft)
    endpoint = f"{BASE_URL}/api/meetings/{meeting_id}"
    transcript = page.request.get(f"{endpoint}/transcript").json()
    response = page.request.put(
        f"{endpoint}/self-speaker",
        data={
            "speaker_id": "SPEAKER_00",
            "expected_self_speaker_id": "SPEAKER_01",
            "expected_revision": transcript["self_speaker_revision"],
        },
    )
    assert response.ok
    page.evaluate(
        "id => document.dispatchEvent(new CustomEvent('ws:pipeline_status', {detail: {meeting_id: id}}))",
        meeting_id,
    )
    expect(names).to_have_text(["나", "참석자 2", "나"])
    expect(editor).to_have_value(draft)
    page.locator(".utterance-edit-actions").get_by_role("button", name="저장").click()
    expect(page.locator(".utterance-text").first).to_have_text(draft)
    page.reload()
    expect(page.locator(".utterance-text").first).to_have_text(draft)
    expect(names).to_have_text(["나", "참석자 2", "나"])


@pytest.mark.parametrize("has_mic_rows", [True, False])
def test_self_speaker_microphone_evidence_locks_identity(
    page: Page, test_base_dir: Path, has_mic_rows: bool
) -> None:
    """분리 트랙에서 '나'를 자동 표시하고 마이크 발화가 없어도 상대방을 지정하지 않는다."""
    meeting_id = f"meeting_20260407_14002{int(has_mic_rows)}"
    _seed_meeting(test_base_dir, meeting_id)
    path = test_base_dir / "checkpoints" / meeting_id / "correct.json"
    transcript = json.loads(path.read_text())
    transcript["separate_mic_track"] = True
    if has_mic_rows:
        transcript["utterances"][1]["speaker"] = "나"
    path.write_text(json.dumps(transcript, ensure_ascii=False))
    page.goto(f"{BASE_URL}/app/viewer/{meeting_id}")
    expected = "나" if has_mic_rows else "참석자 2"
    expect(page.locator(".utterance-speaker")).to_have_text(["참석자 1", expected, "참석자 1"])
    expect(page.locator("button.utterance-speaker, .self-speaker-trigger")).to_have_count(0)
    expect(page.locator(".utterance-badge.is-self svg")).to_have_count(int(has_mic_rows))
    if has_mic_rows:
        expect(page.locator(".speaker-chip.is-self")).to_have_text("나내 마이크")
        for scheme in ("light", "dark"):
            page.emulate_media(color_scheme=scheme)
            for badge in page.locator(".utterance-badge.is-self, .speaker-badge.is-self").all():
                icon_color = badge.evaluate("el => getComputedStyle(el).color")
                expect(badge.locator("svg")).to_have_css("color", icon_color)


def _seed_mic_echo_meeting(base_dir: Path, meeting_id: str) -> Path:
    """같은 문장의 스피커 유입과 서로 다른 동시 발화를 한 회의에 시드한다."""
    _seed_meeting(base_dir, meeting_id)
    path = base_dir / "checkpoints" / meeting_id / "correct.json"
    transcript = json.loads(path.read_text())
    template = transcript["utterances"][0]
    phrase = "다음 회의에서는 검색 화면을 먼저 검토하겠습니다."
    transcript["utterances"] = [
        {
            **template,
            "start": start,
            "end": start + 3,
            "speaker": speaker,
            "text": text,
            "original_text": text,
        }
        for start, speaker, text in (
            (17.0, "나", phrase),
            (18.0, "SPEAKER_01", phrase),
            (25.0, "나", "저는 마이크 설정을 직접 확인해 보겠습니다."),
            (25.0, "SPEAKER_01", "그동안 제가 자료를 정리하겠습니다."),
        )
    ]
    transcript["separate_mic_track"] = True
    path.write_text(json.dumps(transcript, ensure_ascii=False))
    return path


def test_mic_echo_review_fold_search_and_original(page: Page, test_base_dir: Path) -> None:
    """중복 후보만 접고, 검색 시 원문 양쪽과 내용이 다른 동시 발화를 보존한다."""
    meeting_id = "meeting_20260407_143051"
    path = _seed_mic_echo_meeting(test_base_dir, meeting_id)
    original = path.read_bytes()
    page.goto(f"{BASE_URL}/app/viewer/{meeting_id}")
    rows = page.locator("#viewerTimeline .utterance")
    expect(rows).to_have_count(4)
    expect(rows.nth(0).locator(".mic-echo-label")).to_contain_text("00:18")
    toggle = page.locator("#viewerMicEchoToggle")
    expect(toggle).to_have_text("중복 후보 1개 접기")
    toggle.click()
    expect(rows.nth(0)).to_be_hidden()
    for i in (1, 2, 3):
        expect(rows.nth(i)).to_be_visible()
    page.locator("#viewerSearchInput").fill("검색 화면")
    expect(toggle).to_be_disabled()
    expect(rows.nth(0)).to_be_visible()
    expect(page.locator("#viewerSearchInfo")).to_contain_text("2건")
    page.locator("#viewerSearchClear").click()
    expect(rows.nth(0)).to_be_hidden()
    toggle.click()
    expect(rows.nth(0)).to_be_visible()
    assert path.read_bytes() == original


def test_audio_retention_settings_persist_and_retry(page: Page) -> None:
    """실패한 삭제 설정을 재시도하고 다른 창에서 끈 정책을 덮어쓰지 않는다."""
    setting = "lifecycle_audio_retention_enabled"
    page.goto(f"{BASE_URL}/app/settings")
    toggle = page.get_by_role("checkbox", name="녹음 45일 후 음성 자동 삭제")
    expect(toggle).to_be_enabled()
    expect(toggle).not_to_be_checked()
    expect(page.locator("#settingsAudioRetentionHelp")).to_contain_text("기존 녹음")
    page.locator('label[for="settingsAudioRetention"]').click()
    expect(toggle).to_be_checked()
    save = page.get_by_role("button", name="변경사항 저장")
    page.route("**/api/settings", lambda route: route.fulfill(status=503), times=1)
    save.click()
    expect(page.locator("#settingsSaveStatus")).to_contain_text("저장 실패")
    expect(toggle).to_be_enabled()
    expect(toggle).to_be_checked()
    assert page.request.get(f"{BASE_URL}/api/settings").json()[setting] is False
    save.click()
    expect(page.locator("#settingsSaveStatus")).to_contain_text("저장 완료")
    page.reload()
    expect(toggle).to_be_checked()
    expect(page.locator("#settingsLifecycleEnabled")).not_to_be_checked()

    # 다른 창에서 자동 삭제를 해제한 뒤 현재 창에서 일반 설정만 저장한다.
    assert page.request.put(f"{BASE_URL}/api/settings", data={setting: False}).ok
    page.locator("#settingsTemp").dispatch_event("input")
    with page.expect_request(
        lambda req: req.method == "PUT" and req.url.endswith("/api/settings")
    ) as request:
        save.click()
    assert setting not in request.value.post_data_json
    expect(toggle).not_to_be_checked()
    assert page.request.get(f"{BASE_URL}/api/settings").json()[setting] is False


def test_audio_retention_viewer_keeps_transcript_and_minutes(
    page: Page, test_base_dir: Path
) -> None:
    """음성 만료 후 전사 편집·요약 열람은 허용하고 재생·재전사 동작은 숨긴다."""
    import sqlite3

    meeting_id = "meeting_20240307_163117"
    _seed_meeting(test_base_dir, meeting_id)
    with sqlite3.connect(test_base_dir / "pipeline.db") as connection:
        connection.execute(
            "UPDATE jobs SET audio_expired_at=? WHERE meeting_id=?",
            ("2024-04-21T16:31:17", meeting_id),
        )
    page.goto(f"{BASE_URL}/app/viewer/{meeting_id}")
    expect(page.locator("#viewerMetaFile")).to_contain_text("보관 기간 45일")
    expect(page.locator("#viewerTimeline .utterance-text")).to_have_count(3)
    expect(page.locator(".utterance-play-btn, .viewer-action-btn.retranscribe")).to_have_count(0)
    page.locator(".viewer-more-actions > summary").click()
    expect(page.get_by_role("button", name="전사문을 클립보드로 복사")).to_be_visible()
    page.locator(".viewer-more-actions > summary").click()
    expect(page.locator(".utterance-text").first).to_have_attribute("tabindex", "0")
    page.get_by_role("tab", name="요약").click()
    expect(page.locator("#viewerPanelSummary")).to_contain_text("파이선 최신 버전 성능 개선 검토")
    assert page.request.get(f"{BASE_URL}/api/meetings/{meeting_id}/audio").status == 410


def test_openai_failure_visible_with_partial_transcript(
    page: Page, test_base_dir: Path, seeded_meeting_id: str
) -> None:
    """부분 전사가 있어도 HTTP 400과 로컬 대안을 표시하고 자동 재요청하지 않는다."""
    import sqlite3

    with sqlite3.connect(str(test_base_dir / "pipeline.db")) as db:
        db.execute(
            "UPDATE jobs SET status='failed', stt_provider='openai', "
            "stt_model='gpt-4o-transcribe-diarize', error_message=? WHERE meeting_id=?",
            (
                "OpenAI 전사 요청이 실패했습니다 (HTTP 400). <script>unsafe</script>",
                seeded_meeting_id,
            ),
        )
    mutations: list[str] = []
    page.on(
        "request",
        lambda request: mutations.append(request.url) if request.method == "POST" else None,
    )
    page.goto(f"{BASE_URL}/app/viewer/{seeded_meeting_id}")
    notice = page.locator(".viewer-failure-notice")
    expect(notice).to_be_visible()
    expect(notice).to_contain_text("HTTP 400")
    expect(notice).to_contain_text("이 Mac에서 로컬로 다시 전사할까요?")
    expect(notice).to_contain_text("기존 전사·요약은 교체됩니다")
    expect(notice.locator("script")).to_have_count(0)
    expect(page.locator(".utterance-text").first).to_be_visible()
    page.screenshot(path=str(test_base_dir / "openai-failure.png"), full_page=True)
    local_button = page.get_by_role("button", name="네, 이 회의만 로컬로 전사")
    page.once("dialog", lambda dialog: dialog.dismiss())
    local_button.click()
    assert mutations == []
    import math
    import wave
    from array import array

    audio = test_base_dir / "audio_input" / f"{seeded_meeting_id}.wav"
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        tone = array(
            "h", (int(8000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(16000))
        )
        output.writeframes(tone.tobytes() * 35)
    config_before = (test_base_dir / "config.yaml").read_bytes()
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_response(lambda response: "/re-transcribe" in response.url) as result:
        local_button.click()
    assert result.value.status == 200, result.value.text()
    with sqlite3.connect(str(test_base_dir / "pipeline.db")) as db:
        state = db.execute(
            "SELECT status, stt_provider FROM jobs WHERE meeting_id=?", (seeded_meeting_id,)
        ).fetchone()
    assert state == ("queued", "local")
    assert (test_base_dir / "config.yaml").read_bytes() == config_before
    assert audio.is_file()


def _open_viewer(page: Page, meeting_id: str) -> None:
    """뷰어로 직접 네비게이션 + 초기 로딩 대기."""
    page.goto(f"{BASE_URL}/app/viewer/{meeting_id}", wait_until="networkidle")
    # meetingInfo (제목·상태) 가 뜰 때까지 대기
    expect(page.locator("#viewerMeetingTitle")).to_be_visible(timeout=10000)


class TestTitleEdit:
    @pytest.mark.parametrize(
        "status", ["queued", "recording", "transcribing", "diarizing", "merging", "embedding"]
    )
    def test_처리중_전사문이_있어도_AI_제목을_비활성화한다(
        self, page: Page, test_base_dir: Path, seeded_meeting_id: str, status: str
    ) -> None:
        """실제 작업 단계 상태 모두에서 AI 제목 생성을 막는다."""
        import sqlite3

        with sqlite3.connect(str(test_base_dir / "pipeline.db")) as db:
            db.execute("UPDATE jobs SET status=? WHERE meeting_id=?", (status, seeded_meeting_id))
        _open_viewer(page, seeded_meeting_id)
        expect(page.locator(".utterance-text").first).to_be_visible()
        expect(page.get_by_role("button", name="제목 자동 정리")).to_be_disabled()

    def test_전사문이_없으면_AI_제목을_비활성화한다(
        self, page: Page, seeded_meeting_id: str
    ) -> None:
        """전사할 내용이 없는 회의는 AI 요청을 시작하지 않는다."""
        page.route(
            "**/api/meetings/*/transcript",
            lambda route: route.fulfill(json={"utterances": []}),
        )
        _open_viewer(page, seeded_meeting_id)
        expect(page.get_by_role("button", name="제목 자동 정리")).to_be_disabled()
        expect(page.get_by_role("button", name="녹음 폴더 열기")).to_be_enabled()

    def test_AI_제목은_백그라운드에서_적용하고_화면이동을_허용한다(
        self, page: Page, seeded_meeting_id: str
    ) -> None:
        """제목 작업 접수 후 모달 없이 진행하며 완료된 제목을 현재 화면에 반영한다."""
        receipts: list[dict] = []
        calls: list[dict] = []

        def enqueue(route) -> None:
            body = route.request.post_data_json
            calls.append(body)
            receipt = {
                "request_id": body["request_id"],
                "action": "title",
                "queued": 1,
                "skipped": 0,
                "created_at": "2026-09-23T14:00:00",
                "candidates": [{"meeting_id": seeded_meeting_id, "admission": "queued"}],
                "events": [
                    {
                        "meeting_id": seeded_meeting_id,
                        "status": "running",
                        "status_label": "제목 정리 중",
                    }
                ],
            }
            receipts[:] = [receipt]
            route.fulfill(json=receipt)

        page.route("**/api/meetings/titles", enqueue)
        page.route(
            "**/api/batch-requests", lambda route: route.fulfill(json={"requests": receipts})
        )
        _open_viewer(page, seeded_meeting_id)
        page.get_by_role("button", name="제목 자동 정리", exact=True).click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        expect(page.get_by_role("button", name="제목 정리 중…")).to_be_disabled()
        expect(page.locator("#backgroundWorkStatus")).to_contain_text("진행 중")
        page.evaluate("window.SPA.Router.navigate('/app')")
        expect(page.locator("#backgroundWorkStatus")).to_be_visible()
        page.evaluate(f"window.SPA.Router.navigate('/app/viewer/{seeded_meeting_id}')")
        expect(page.get_by_role("button", name="제목 정리 중…")).to_be_disabled()
        title = "2026-04-07 · Python 성능 개선 결정"
        page.request.patch(f"{BASE_URL}/api/meetings/{seeded_meeting_id}", data={"title": title})
        receipts[0]["events"].append(
            {
                "meeting_id": seeded_meeting_id,
                "status": "completed",
                "status_label": "제목 정리 완료",
                "applied_title": title,
                "previous_title": "",
                "date_source": "audio_mtime",
                "sampled": True,
            }
        )
        expect(page.locator(".viewer-title-text")).to_have_text(title, timeout=8000)
        expect(page.get_by_role("button", name="제목 자동 정리", exact=True)).to_be_enabled()
        assert len(calls) == 1
        page.locator("#backgroundWorkHistory").click()
        dialog = page.get_by_role("dialog", name="작업 내역")
        expect(dialog).to_contain_text(title)
        expect(dialog).to_contain_text("원본 파일 수정일 기준")
        expect(dialog).to_contain_text("긴 녹취 일부 발췌")
        expect(dialog.get_by_role("button", name="이전 제목으로 되돌리기")).to_be_visible()

    def test_AI_제목_접수실패는_선택과_기존제목을_보존한다(
        self, page: Page, seeded_meeting_id: str
    ) -> None:
        """통신 오류 뒤 같은 요청 ID로 재시도하여 중복 실행을 막는다."""
        calls: list[dict] = []

        def fail(route) -> None:
            calls.append(route.request.post_data_json)
            route.fulfill(status=503, json={"detail": "일시적인 연결 오류"})

        page.route("**/api/meetings/titles", fail)
        _open_viewer(page, seeded_meeting_id)
        original = page.locator(".viewer-title-text").inner_text()
        button = page.get_by_role("button", name="제목 자동 정리", exact=True)
        button.click()
        expect(page.locator("#backgroundWorkMessage")).to_contain_text("접수 확인 실패")
        expect(button).to_be_enabled()
        button.click()
        expect(button).to_be_enabled()
        assert len(calls) == 2
        assert calls[0]["request_id"] == calls[1]["request_id"]
        expect(page.locator(".viewer-title-text")).to_have_text(original)

    def test_녹음_폴더_열기_실패후_재시도(self, page: Page, seeded_meeting_id: str) -> None:
        """Finder 요청은 사용자 클릭 때만 보내고 실패 뒤 재시도를 허용한다."""
        requests: list[str] = []

        def handle(route) -> None:
            requests.append(route.request.method)
            if len(requests) == 1:
                route.fulfill(status=404, json={"detail": "원본 파일이 없습니다"})
            elif len(requests) == 2:
                route.fulfill(json={"opened": False, "path": "/test/audio_input"})
            else:
                route.fulfill(json={"opened": True, "path": "/test/audio_input"})

        page.route("**/api/meetings/*/open-audio-folder", handle)
        _open_viewer(page, seeded_meeting_id)
        assert requests == []
        button = page.get_by_role("button", name="녹음 폴더 열기")
        button.click()
        expect(page.locator("#errorBanner")).to_contain_text("원본 파일이 없습니다")
        expect(button).to_be_enabled()
        button.click()
        expect(page.locator("#errorBanner")).to_contain_text("/test/audio_input")
        expect(page.locator("#errorBanner")).to_contain_text("직접 열어 주세요")
        expect(button).to_be_enabled()
        button.click()
        expect(button).to_be_enabled()
        assert requests == ["POST", "POST", "POST"]

    def test_제목_기본_표시_타임스탬프(self, page: Page, seeded_meeting_id: str) -> None:
        """제목이 비어도 첫 줄에 회의 시각을 남기고 제목 줄에 기본 이름을 표시한다."""
        _open_viewer(page, seeded_meeting_id)
        title_text = page.locator(".viewer-title-text")
        # meeting_20260407_140000 → "2026-04-07 14:00"
        expect(page.locator("#viewerMeetingDate")).to_have_text("2026-04-07 14:00")
        expect(title_text).to_have_text("제목 없는 회의")

    def test_제목_인라인_편집_저장(self, page: Page, seeded_meeting_id: str) -> None:
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
        page.wait_for_function("document.title.includes('Q1 제품 로드맵 회의')", timeout=3000)

    def test_제목_Esc_취소(self, page: Page, seeded_meeting_id: str) -> None:
        """편집 중 Esc 누르면 원래 값 유지."""
        _open_viewer(page, seeded_meeting_id)
        original = page.locator(".viewer-title-text").text_content()

        page.locator(".viewer-title-text").click()
        input_el = page.locator(".viewer-title-input")
        expect(input_el).to_be_visible()
        input_el.fill("버려질 제목")
        input_el.press("Escape")

        expect(page.locator(".viewer-title-text")).to_have_text(original or "")

    def test_제목_빈값으로_초기화(self, page: Page, seeded_meeting_id: str) -> None:
        """빈 문자열 저장 시 타임스탬프 폴백으로 돌아간다."""
        _open_viewer(page, seeded_meeting_id)
        # 1) 먼저 제목 설정
        page.locator(".viewer-title-text").click()
        page.locator(".viewer-title-input").fill("일시적 제목")
        page.locator(".viewer-title-input").press("Enter")
        expect(page.locator(".viewer-title-text")).to_contain_text("일시적 제목", timeout=5000)

        # 2) 빈 값으로 초기화
        page.locator(".viewer-title-text").click()
        input_el = page.locator(".viewer-title-input")
        input_el.fill("")
        input_el.press("Enter")

        expect(page.locator(".viewer-title-text")).to_have_text("제목 없는 회의", timeout=5000)
        expect(page.locator("#viewerMeetingDate")).to_have_text("2026-04-07 14:00")


class TestSummaryEdit:
    def test_요약_초안_이동_새로고침_저장실패_재시도(
        self, page: Page, seeded_meeting_id: str, test_base_dir: Path
    ) -> None:
        """탭과 회의 화면을 벗어나도 초안만 복원되며 저장 성공 후 본문에 반영된다."""
        _open_viewer(page, seeded_meeting_id)
        page.locator("#viewerTabSummary").click()
        page.get_by_role("button", name="✎ 편집").click()
        draft = "## 편집 중\n겹쳐 말한 부분까지 확인했습니다."
        editor = page.get_by_role("textbox", name="회의 요약 편집")
        editor.fill(draft)
        minutes = test_base_dir / "outputs" / seeded_meeting_id / "meeting_minutes.md"
        before = minutes.read_text(encoding="utf-8")
        page.locator("#viewerTabTranscript").click()
        page.locator("#viewerTabSummary").click()
        expect(editor).to_have_value(draft)
        page.get_by_role("link", name="홈으로 이동").click()
        page.go_back()
        expect(editor).to_have_value(draft)
        page.reload()
        expect(editor).to_have_value(draft)
        assert minutes.read_text(encoding="utf-8") == before

        url = f"**/api/meetings/{seeded_meeting_id}/summary"
        page.route(
            url,
            lambda route: route.abort() if route.request.method == "PUT" else route.continue_(),
        )
        page.get_by_role("button", name="저장", exact=True).click()
        expect(page.locator(".summary-draft-status")).to_contain_text("저장 실패")
        expect(editor).to_have_value(draft)
        page.unroute(url)
        editor.press("Control+Enter")
        expect(page.locator(".summary-rendered")).to_contain_text("겹쳐 말한 부분")
        assert minutes.read_text(encoding="utf-8") == draft
        page.reload()
        page.locator("#viewerTabSummary").click()
        expect(editor).to_have_count(0)

    def test_요약_편집_저장(self, page: Page, seeded_meeting_id: str, test_base_dir: Path) -> None:
        """요약 탭 → 편집 → textarea 수정 → 저장."""
        _open_viewer(page, seeded_meeting_id)

        # 요약 탭 열기
        page.locator("#viewerTabSummary").click()
        expect(page.locator("#viewerPanelSummary")).to_be_visible()

        # 렌더된 마크다운에서 편집 버튼 찾기
        edit_btn = page.locator(".summary-toolbar button").filter(has_text="편집")
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
        page.locator(".summary-edit-actions button").filter(has_text="저장").click()

        # 편집 모드 종료 + 렌더된 결과에 마커 포함
        expect(page.locator(".summary-rendered")).to_be_visible(timeout=5000)
        expect(page.locator(".summary-rendered")).to_contain_text("EDIT-SUMMARY-MARKER-123")

        # 디스크 파일에도 반영
        minutes_path = test_base_dir / "outputs" / seeded_meeting_id / "meeting_minutes.md"
        assert "EDIT-SUMMARY-MARKER-123" in minutes_path.read_text(encoding="utf-8")
        # .bak 파일 생성 확인
        assert minutes_path.with_suffix(".md.bak").exists()

    def test_요약_편집_취소(self, page: Page, seeded_meeting_id: str) -> None:
        """편집 취소 시 원래 렌더링으로 복귀."""
        _open_viewer(page, seeded_meeting_id)
        page.locator("#viewerTabSummary").click()

        edit_btn = page.locator(".summary-toolbar button").filter(has_text="편집")
        expect(edit_btn).to_be_visible(timeout=10000)
        edit_btn.click()

        textarea = page.locator(".summary-textarea")
        expect(textarea).to_be_visible()
        textarea.fill("취소될 내용")

        page.locator(".summary-edit-actions button").filter(has_text="취소").click()

        # 원래 렌더링 복귀
        expect(page.locator(".summary-rendered")).to_be_visible()
        expect(page.locator(".summary-rendered")).not_to_contain_text("취소될 내용")


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
        page.locator(".utterance-edit-actions button").filter(has_text="저장").click()

        # 재렌더된 발화에 새 텍스트
        expect(page.locator(".utterance-text").first).to_contain_text(
            "개별 편집된 발화입니다", timeout=5000
        )

        # 디스크 파일에도 반영
        correct_path = test_base_dir / "checkpoints" / seeded_meeting_id / "correct.json"
        data = json.loads(correct_path.read_text(encoding="utf-8"))
        assert "개별 편집" in data["utterances"][0]["text"]
        assert data["utterances"][0]["was_corrected"] is True

    def test_발화_편집_Esc_취소(self, page: Page, seeded_meeting_id: str) -> None:
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

        expect(page.locator(".utterance-text").first).to_have_text(original or "")


class TestTranscriptDraft:
    def test_conflict_keeps_draft_when_reading_latest(
        self, page: Page, seeded_meeting_id: str
    ) -> None:
        """다른 창이 먼저 저장하면 최신 원문과 내 초안을 함께 확인한 뒤 적용한다."""
        _open_viewer(page, seeded_meeting_id)
        url = f"{BASE_URL}/api/meetings/{seeded_meeting_id}/transcript"
        row = page.locator(".utterance-text").first
        row.dblclick()
        editor = page.get_by_role("textbox", name="발화 편집")
        editor.fill("내가 편집하고 있던 문장입니다.")
        rows = page.request.get(url).json()["utterances"]
        rows[0]["text"] = "다른 창에서 저장한 문장입니다."
        assert page.request.put(url, data={"utterances": rows}).ok
        editor.press("Control+Enter")
        row.get_by_role("button", name="최신 전사문 보기").click()
        expect(row.get_by_role("alert")).to_contain_text(rows[0]["text"])
        expect(editor).to_have_value("내가 편집하고 있던 문장입니다.")
        assert page.request.get(url).json()["utterances"][0]["text"] == rows[0]["text"]
        row.get_by_role("button", name="현재 문장을 초안으로 바꾸기").click()
        expect(row).to_have_text("내가 편집하고 있던 문장입니다.")

    @pytest.mark.parametrize("fail", [False, True], ids=["saved", "failed"])
    def test_search_and_return_while_saving(
        self, page: Page, seeded_meeting_id: str, test_base_dir: Path, fail: bool
    ) -> None:
        """PUT 응답을 보류한 동안 검색·다른 회의 이동 후 돌아와도 저장 결과를 확인한다."""
        other = "meeting_20260408_100000"
        _seed_meeting(test_base_dir, other)
        _open_viewer(page, seeded_meeting_id)
        row = page.locator(".utterance-text").first
        row.dblclick()
        editor = page.get_by_role("textbox", name="발화 편집")
        revised = "응답을 기다리는 동안에도 보존해야 하는 발화입니다."
        editor.fill(revised)
        pending: list[Route] = []
        url = f"{BASE_URL}/api/meetings/{seeded_meeting_id}/transcript"

        def delay_save(route: Route) -> None:
            """PUT 하나만 보류하고 나머지 요청은 실제 서버에 전달한다."""
            if route.request.method == "PUT":
                pending.append(route)
            else:
                route.continue_()

        page.route(url, delay_save)
        editor.press("Control+Enter")
        query = page.get_by_role("textbox", name="전사문 내 검색")
        query.fill("파이선")
        expect(page.get_by_text("편집 초안 복원됨")).to_be_visible()
        expect(row.get_by_role("button", name="저장 중…")).to_be_disabled()
        expect(editor).to_have_value(revised)
        page.locator(f'.meeting-item[data-meeting-id="{other}"]').press("Enter")
        expect(page).to_have_url(f"{BASE_URL}/app/viewer/{other}")
        expect(row).to_be_visible()
        expect(editor).to_have_count(0)
        page.locator(f'.meeting-item[data-meeting-id="{seeded_meeting_id}"]').press("Enter")
        expect(page).to_have_url(f"{BASE_URL}/app/viewer/{seeded_meeting_id}")
        assert len(pending) == 1
        if fail:
            pending[0].fulfill(status=503, json={"detail": "테스트 저장 중단"})
            expect(row.get_by_role("alert")).to_contain_text("테스트 저장 중단")
            expect(editor).to_have_value(revised)
            page.unroute(url, delay_save)
            editor.press("Control+Enter")
        else:
            pending[0].continue_()
        expect(row).to_have_text(revised)
        assert page.request.get(url).json()["utterances"][0]["text"] == revised

    def test_search_switch_reload_save(
        self, page: Page, seeded_meeting_id: str, test_base_dir: Path
    ) -> None:
        """검색과 회의 이동 뒤 같은 발화의 초안을 복원하고 명시적 저장 때만 게시한다."""
        other = "meeting_20260408_100000"
        _seed_meeting(test_base_dir, other)
        _open_viewer(page, seeded_meeting_id)
        original = page.locator(".utterance-text").first.inner_text()
        draft = original + " 수정한 초안입니다."
        editor = page.get_by_role("textbox", name="발화 편집")
        page.locator(".utterance-text").first.dblclick()
        editor.fill(draft)
        query = page.get_by_role("textbox", name="전사문 내 검색")
        query.fill("파이선")
        expect(page.get_by_text("편집 초안 복원됨")).to_be_visible()
        expect(query).to_be_focused()
        expect(editor).to_have_value(draft)
        page.locator(f'.meeting-item[data-meeting-id="{other}"]').press("Enter")
        expect(page).to_have_url(f"{BASE_URL}/app/viewer/{other}")
        expect(page.locator(".utterance-text").first).to_have_text(original)
        expect(editor).to_have_count(0)
        page.locator(f'.meeting-item[data-meeting-id="{seeded_meeting_id}"]').press("Enter")
        expect(editor).to_have_value(draft)
        page.reload()
        expect(editor).to_have_value(draft)
        url = f"{BASE_URL}/api/meetings/{seeded_meeting_id}/transcript"
        assert page.request.get(url).json()["utterances"][0]["text"] == original
        editor.press("Control+Enter")
        expect(editor).to_have_count(0)
        assert page.request.get(url).json()["utterances"][0]["text"] == draft
        page.reload()
        expect(page.locator(".utterance-text").first).to_have_text(draft)
        expect(editor).to_have_count(0)


class TestInlineVocabulary:
    """개별 발화 저장과 선택한 용어 등록을 실제 회의 API에서 검증한다."""

    @staticmethod
    def _save_first(page: Page, text: str) -> None:
        """첫 문장의 편집기를 열고 키보드로 저장한다."""
        page.locator(".utterance-text").first.dblclick()
        editor = page.locator(".utterance-textarea")
        editor.fill(text)
        editor.press("Control+Enter")
        expect(page.locator(".utterance-text").first).to_have_text(text)

    def test_register_selected_change_and_preserve_other_utterances(
        self, page: Page, seeded_meeting_id: str
    ) -> None:
        """두 개의 교정 후보 중 선택한 하나만 저장하고 다른 발화는 그대로 둔다."""
        url = f"{BASE_URL}/api/meetings/{seeded_meeting_id}/transcript"
        rows = page.request.get(url).json()["utterances"]
        rows[0]["text"] = "파이선 성능을 확인한 후 젠데시크 설정을 검토합니다."
        assert page.request.put(url, data={"utterances": rows}).ok
        vocab_url = f"{BASE_URL}/api/vocabulary"
        before = page.request.get(vocab_url).json()
        _open_viewer(page, seeded_meeting_id)
        self._save_first(page, "파이썬 성능을 확인한 후 젠데스크 설정을 검토합니다.")
        form = page.get_by_role("form", name="용어집에 추가할 표현")
        expect(form.get_by_role("checkbox")).to_have_count(2)
        expect(form.get_by_role("textbox", name="기존 표기 1")).to_have_value("파이선")
        expect(form.get_by_role("textbox", name="정답 표기 1")).to_have_value("파이썬")
        expect(form.get_by_role("textbox", name="기존 표기 2")).to_have_value("젠데시크")
        assert page.request.get(vocab_url).json() == before
        form.get_by_role("checkbox", name="표현 2 선택").uncheck()
        form.get_by_role("textbox", name="정답 표기 1").fill("Python")
        form.get_by_role("button", name="용어집에 추가").click()
        expect(form).to_have_count(0)
        terms = page.request.get(vocab_url).json()["terms"]
        assert any(t["term"] == "Python" and "파이선" in t["aliases"] for t in terms)
        assert not any("젠데시크" in t["aliases"] for t in terms)
        page.reload()
        expect(page.locator(".utterance-text").nth(1)).to_have_text(rows[1]["text"])
        expect(page.locator(".utterance-text").first).to_contain_text("파이썬 성능")

    def test_retry_vocabulary_without_resaving_transcript(
        self, page: Page, seeded_meeting_id: str
    ) -> None:
        """용어집 오류 후 저장된 전사는 유지되고 용어만 다시 등록할 수 있다."""
        _open_viewer(page, seeded_meeting_id)
        original = page.locator(".utterance-text").first.inner_text()
        updated = original.replace("파이선", "파이썬")
        self._save_first(page, updated)
        form = page.get_by_role("form", name="용어집에 추가할 표현")
        expect(form).to_be_visible()
        page.route("**/api/vocabulary/corrections", lambda route: route.abort(), times=1)
        form.get_by_role("button", name="용어집에 추가").click()
        expect(form.get_by_role("alert")).to_be_visible()
        url = f"{BASE_URL}/api/meetings/{seeded_meeting_id}/transcript"
        saved = page.request.get(url).json()
        assert saved["utterances"][0]["text"] == updated
        form.get_by_role("button", name="용어집에 추가").click()
        expect(form).to_have_count(0)
        assert (
            page.request.get(url).json()["self_speaker_revision"] == saved["self_speaker_revision"]
        )
        terms = page.request.get(f"{BASE_URL}/api/vocabulary").json()["terms"]
        assert sum(t["term"] == "파이썬" and "파이선" in t["aliases"] for t in terms) == 1

    def test_skip_and_punctuation_only_make_no_vocabulary_changes(
        self, page: Page, seeded_meeting_id: str
    ) -> None:
        """후보를 건너뛰거나 문장부호만 고쳐도 문장은 저장된다."""
        _open_viewer(page, seeded_meeting_id)
        before = page.request.get(f"{BASE_URL}/api/vocabulary").json()
        original = page.locator(".utterance-text").first.inner_text()
        updated = original.replace("파이선", "파이썬")
        self._save_first(page, updated)
        form = page.get_by_role("form", name="용어집에 추가할 표현")
        form.get_by_role("button", name="건너뛰기").click()
        expect(form).to_have_count(0)
        self._save_first(page, updated.rstrip(".") + "!")
        expect(form).to_have_count(0)
        assert page.request.get(f"{BASE_URL}/api/vocabulary").json() == before

    def test_lost_transcript_response_keeps_editor_and_recovers_suggestions(
        self, page: Page, seeded_meeting_id: str
    ) -> None:
        """서버 저장 직후 응답이 끊겨도 재시도로 후보를 복구하고 수정본을 유지한다."""
        _open_viewer(page, seeded_meeting_id)
        row = page.locator(".utterance-text").first
        revised = row.inner_text().replace("파이선", "파이썬")
        row.dblclick()
        editor = page.get_by_role("textbox", name="발화 편집")
        editor.fill(revised)
        url = f"{BASE_URL}/api/meetings/{seeded_meeting_id}/transcript"

        def lose_response(route: Route) -> None:
            """편집을 서버에 전달한 뒤 첫 응답의 네트워크 단절을 재현한다."""
            if route.request.method != "PUT":
                route.continue_()
                return
            assert route.fetch().ok
            route.abort()

        page.route(url, lose_response)
        page.get_by_role("button", name="저장", exact=True).click()
        expect(row.get_by_role("alert")).to_be_visible()
        expect(editor).to_have_value(revised)
        saved = page.request.get(url).json()
        assert saved["utterances"][0]["text"] == revised
        page.unroute(url, lose_response)
        page.get_by_role("button", name="저장", exact=True).click()
        form = page.get_by_role("form", name="용어집에 추가할 표현")
        expect(form.get_by_role("textbox", name="정답 표기 1")).to_have_value("파이썬")
        assert (
            page.request.get(url).json()["self_speaker_revision"] == saved["self_speaker_revision"]
        )


class TestBulkReplaceAndVocabulary:
    def test_모두_바꾸기_버튼_존재(self, page: Page, seeded_meeting_id: str) -> None:
        """completed 상태 + 전사 로드 후 '모두 바꾸기' 버튼이 표시된다."""
        _open_viewer(page, seeded_meeting_id)
        page.locator(".viewer-more-actions > summary").click()
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

        page.locator(".viewer-more-actions").evaluate("el => el.open = true")
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
        expect(page.locator("#transcriptReplaceModal")).to_have_count(0, timeout=5000)

        # 치환 반영 — 최소 0.5초 후 DOM 재렌더 기대
        page.wait_for_timeout(1000)
        first_text = page.locator(".utterance-text").first.text_content() or ""
        assert "파이선" not in first_text
        assert "FastAPI" in first_text

        # 디스크 확인
        correct_path = test_base_dir / "checkpoints" / seeded_meeting_id / "correct.json"
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

        page.locator(".viewer-more-actions").evaluate("el => el.open = true")
        page.locator(".viewer-action-btn.replace").click()
        expect(page.locator("#transcriptReplaceModal")).to_be_visible()

        page.locator("#replaceFind").fill("파이선")
        page.locator("#replaceReplace").fill("FastAPI")
        # 기본 체크 상태에서 적용
        page.locator("#replaceApplyBtn").click()
        expect(page.locator("#transcriptReplaceModal")).to_have_count(0, timeout=5000)

        # 백엔드 용어집 API 로 확인
        import urllib.request

        with urllib.request.urlopen(f"{BASE_URL}/api/vocabulary", timeout=5) as resp:
            vocab = json.loads(resp.read().decode())

        terms = [t for t in vocab["terms"] if t["term"] == "FastAPI"]
        assert len(terms) == 1, f"FastAPI term 이 용어집에 없음: {vocab}"
        assert "파이선" in terms[0]["aliases"]
        assert "전사 편집에서 자동 등록" in (terms[0].get("note") or "")

    def test_find과_replace_동일_거부(self, page: Page, seeded_meeting_id: str) -> None:
        """find 와 replace 가 같으면 에러 표시."""
        _open_viewer(page, seeded_meeting_id)
        page.locator(".viewer-more-actions").evaluate("el => el.open = true")
        page.locator(".viewer-action-btn.replace").click()
        expect(page.locator("#transcriptReplaceModal")).to_be_visible()

        page.locator("#replaceFind").fill("같음")
        page.locator("#replaceReplace").fill("같음")
        page.locator("#replaceApplyBtn").click()

        # 모달이 닫히지 않고 에러 메시지 표시
        expect(page.locator("#transcriptReplaceModal")).to_be_visible()
        expect(page.locator("#replaceError")).to_contain_text("같아")


class TestSTTModelActivation:
    """설정 페이지 STT 모델 활성화 UX 회귀 방지."""

    def test_수동_임포트_후_활성화_버튼이_현재_사용_중_으로_변한다(
        self, page: Page, test_base_dir: Path
    ) -> None:
        """회귀 방지: 수동 임포트된 seastar 를 활성화하면 UI 가
        '활성화' 버튼 대신 '현재 사용 중' 라벨을 표시해야 한다.

        버그 재현 절차:
            1. 수동 임포트 디렉토리에 파일 배치 → 디스크는 READY
            2. POST /activate → config.stt.model_name 에 로컬 경로 저장
            3. 설정 페이지 새로고침
            4. 이전: 3개 모델 모두 "활성화" 버튼 (is_active=false 버그)
            5. 수정 후: seastar 카드만 "현재 사용 중" + .active 보더
        """
        import urllib.request

        # 1. 수동 임포트 경로에 유효한 파일 배치
        manual_dir = test_base_dir / "stt_models" / "seastar-medium-4bit-manual"
        manual_dir.mkdir(parents=True, exist_ok=True)
        (manual_dir / "config.json").write_text('{"n_mels": 80}')
        (manual_dir / "weights.safetensors").write_bytes(b"x" * 2048)

        # 2. GET /api/stt-models 로 seastar 가 READY 인지 확인
        with urllib.request.urlopen(f"{BASE_URL}/api/stt-models", timeout=5) as resp:
            data = json.loads(resp.read())
        models = {m["id"]: m for m in data["models"]}
        assert models["seastar-medium-4bit"]["status"] == "ready", (
            f"디스크에 파일 배치했는데 READY 아님: {models['seastar-medium-4bit']}"
        )

        # 3. POST /activate 호출 (API 로 활성화 시뮬레이션)
        req = urllib.request.Request(
            f"{BASE_URL}/api/stt-models/seastar-medium-4bit/activate",
            data=b"",
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200

        # 4. 설정 페이지 열고 상태 확인
        page.goto(f"{BASE_URL}/app/settings", wait_until="networkidle")

        # STT 모델 목록이 로드될 때까지 대기
        page.wait_for_selector(".stt-model-card", timeout=10000)

        # seastar 카드를 찾는다 (label 로 식별)
        seastar_card = page.locator(".stt-model-card").filter(has_text="seastar medium-ko-zeroth")
        expect(seastar_card).to_be_visible()

        # 핵심 검증: is_active=true 여야 하므로
        # (a) 카드에 .active 클래스 (주황이 아닌 accent 보더)
        expect(seastar_card).to_have_class(__import__("re").compile(r".*\bactive\b.*"))
        # (b) "활성화" 버튼 대신 "현재 사용 중" 라벨 표시
        expect(seastar_card.locator(".stt-active-label")).to_be_visible()
        expect(seastar_card.locator(".stt-active-label")).to_contain_text("현재 사용 중")
        # (c) 활성화 버튼이 없음
        expect(seastar_card.locator("button.stt-action-btn.activate")).to_have_count(0)

        # 다른 모델은 여전히 활성화 버튼 (ready 상태일 때)
        komixv2_card = page.locator(".stt-model-card").filter(
            has=page.locator(".stt-model-name").get_by_text(
                "komixv2 (medium 한국어 fine-tune)", exact=True
            )
        )
        expect(komixv2_card).not_to_have_class(__import__("re").compile(r".*\bactive\b.*"))


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
        page.locator(".summary-toolbar button").filter(has_text="편집").click()
        page.locator(".summary-textarea").fill("## E2E 종합 요약\n- 마커: E2E-FULL-FLOW\n")
        page.locator(".summary-edit-actions button").filter(has_text="저장").click()
        expect(page.locator(".summary-rendered")).to_contain_text("E2E-FULL-FLOW", timeout=5000)

        # 3) 전사 탭으로 돌아가서 모두 바꾸기
        page.locator("#viewerTabTranscript").click()
        # 전사 탭이 활성화될 때까지 대기
        page.locator(".viewer-more-actions > summary").click()
        expect(page.locator(".viewer-action-btn.replace")).to_be_visible(timeout=5000)
        page.locator(".viewer-more-actions").evaluate("el => el.open = true")
        page.locator(".viewer-action-btn.replace").click()
        expect(page.locator("#transcriptReplaceModal")).to_be_visible()

        page.locator("#replaceFind").fill("파이선")
        page.locator("#replaceReplace").fill("Pyannote")
        page.locator("#replaceApplyBtn").click()
        expect(page.locator("#transcriptReplaceModal")).to_have_count(0, timeout=5000)

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
        minutes = test_base_dir / "outputs" / seeded_meeting_id / "meeting_minutes.md"
        assert "E2E-FULL-FLOW" in minutes.read_text(encoding="utf-8")
        assert minutes.with_suffix(".md.bak").exists()

        # 전사 파일
        correct = test_base_dir / "checkpoints" / seeded_meeting_id / "correct.json"
        data = json.loads(correct.read_text(encoding="utf-8"))
        first_text = data["utterances"][0]["text"]
        assert "파이선" not in first_text
        assert "Pyannote" in first_text

        # 용어집
        with urllib.request.urlopen(f"{BASE_URL}/api/vocabulary", timeout=5) as resp:
            vocab = json.loads(resp.read())
        terms = [t for t in vocab["terms"] if t["term"] == "Pyannote"]
        assert len(terms) == 1
        assert "파이선" in terms[0]["aliases"]
