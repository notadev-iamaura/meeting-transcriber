"""tests/ui — Playwright 기반 UI 테스트 공용 fixture / 헬퍼.

본 모듈은 두 종류의 fixture 를 제공한다.

(A) 정적 데모 fixture
    `demo_swatch_url` 같이 file:// URL 만 반환하는 함수. 별도 서버 없이
    `ui/web/_demo/*.html` 을 직접 로드하는 단순 시나리오용.

(B) bulk-actions 통합 fixture (`ui_bulk_*` 계열)
    실제 FastAPI 서버 (main.py --no-menubar) 를 subprocess 로 띄우고
    `회의 5 건` 을 디스크/JobQueue 에 시드한 뒤 SPA 의 `/app` 화면을
    Playwright 로 검증한다. bulk-actions Phase 2A behavior / a11y / visual
    시나리오가 모두 본 fixture 위에 올라간다.

플러그인 의존성:
    본 모듈은 `pytest-playwright` 빌트인 fixture (`browser`, `browser_type`
    등) 에 의존한다. `pip install -e ".[dev]"` 로 설치되며, 별도로
    `playwright install chromium` 이 필요하다. 자체 `browser` fixture 를
    정의하지 않고 플러그인 빌트인을 사용한다 (test_e2e_edit_playwright.py 와
    다른 패턴 — 그쪽은 자체 session-scoped browser fixture 정의).

frontend-a 핸드오프 메모 (bulk-actions / Phase 2 → Phase 4):
    본 fixture 는 미구현 SPA 를 가정하므로 실제 셋업과 결합된 검증은
    아래 DOM 계약이 반영된 뒤 Green 으로 진입한다.

    1. `#listContent` 컨테이너에 다음 ARIA 속성 부여 필수
         - `role="listbox"`
         - `aria-multiselectable="true"`
       `meetings_listbox()` 헬퍼가 이 두 속성으로만 매칭하므로 컨테이너
       클래스 (`list-content` / `meetings-list`) 채택은 frontend-a 자유.
    2. `.bulk-action-bar`, `.home-action-dropdown` 루트에 컴포넌트 마커
         - `data-component="bulk-actions"`
       부여 권장. axe-core scoped scan (`tests/ui/a11y/...`) 의 명시적
       진입점이며, scope 외부 (기존 SPA) 의 무관한 위반에 영향받지 않게
       한다.
    3. 체크박스는 옵션 B 의 `<span aria-hidden="true" data-checkbox="true">`
       (input 요소가 아닌 시각 요소) — `aria-checked` 는 부모
       `.meeting-item[role="option"]` 가 보유하고 ARIA 동기화는 부모 책임.

Red 의도성 (Phase 2 시점):
    위 1~3 이 미구현이므로 본 fixture 는 정상적으로 가동되지만 시나리오는
    selector 부재로 깨끗한 FAIL 한다. fixture 자체가 ImportError /
    ERROR 를 내면 안 된다.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# bulk-actions 통합 시나리오용 격리 포트.
# - 8765 : 개발 기본 서버
# - 8766 : test_e2e_edit_playwright (회의 편집 e2e)
# - 8767 : 본 모듈 (bulk-actions UI)
_BULK_TEST_PORT = 8767


# ============================================================================
# (A) 정적 데모 fixture
# ============================================================================


@pytest.fixture
def demo_swatch_url() -> str:
    """`ui/web/_demo/swatch.html` 의 file:// URL.

    T-101 / T-301 류의 디자인 토큰 데모 시나리오에서 사용한다.
    실제 서버를 띄우지 않으므로 가볍다.
    """
    p = PROJECT_ROOT / "ui" / "web" / "_demo" / "swatch.html"
    return p.as_uri()


# ============================================================================
# (B) bulk-actions 통합 fixture — `ui_bulk_*` 계열
# ============================================================================


def _wait_for_health(base_url: str, timeout: float = 30.0) -> None:
    """`/api/health` 가 200 OK 를 반환할 때까지 폴링한다.

    `/api/status` 가 아닌 `/api/health` 를 사용 — bulk-actions 시나리오는
    pyannote 등 무거운 모델 매니저 로드와 무관하게 FastAPI 라우트만
    뜨면 충분하다.

    Args:
        base_url: 서버 베이스 URL (예: "http://127.0.0.1:8767").
        timeout: 최대 대기 시간 (초).

    Raises:
        RuntimeError: 타임아웃까지 200 응답을 받지 못했을 때.
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_error = e
            time.sleep(0.4)
    raise RuntimeError(
        f"bulk-actions 테스트 서버가 {timeout}초 안에 기동하지 않았습니다 "
        f"({base_url}/api/health, last_error={last_error!r})"
    )


def _seed_one_meeting(
    base_dir: Path,
    meeting_id: str,
    *,
    has_merge: bool,
    created_at: str,
) -> None:
    """회의 1 건을 디스크/JobQueue 에 시드한다.

    구성 요소:
      - `audio_input/{meeting_id}.wav` — 더미 바이트
      - `pipeline.db` 의 `jobs` 테이블 row (status="completed", title="")
      - (옵션) `checkpoints/{meeting_id}/merge.json` — 요약 후보 표식
        없으면 전사 후보 (`/api/meetings/batch` 가 action 분류에 사용)

    Args:
        base_dir: 격리된 base_dir (`MT_BASE_DIR`).
        meeting_id: 회의 ID. 형식: `meeting_YYYYMMDD_HHMMSS`.
        has_merge: True 면 `merge.json` 생성 (요약 가능 회의).
        created_at: ISO8601 문자열. 사이드바 정렬에 영향 (최신순).
    """
    # 1. 더미 오디오 (FastAPI 라우트가 audio_path 존재만 확인할 때 대비).
    audio_path = base_dir / "audio_input" / f"{meeting_id}.wav"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    if not audio_path.exists():
        audio_path.write_bytes(b"fake-audio-for-test")

    # 2. JobQueue row — INSERT OR IGNORE 로 idempotent.
    db_path = base_dir / "pipeline.db"
    conn = sqlite3.connect(str(db_path))
    try:
        # JobQueue.initialize() 가 만든 스키마와 동일한 컬럼만 사용.
        # title 컬럼은 이후 마이그레이션으로 추가됨 — 존재 여부에 따라 분기.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "title" in cols:
            conn.execute(
                """
                INSERT OR IGNORE INTO jobs
                    (meeting_id, audio_path, status, retry_count, max_retries,
                     error_message, created_at, updated_at, title)
                VALUES (?, ?, 'completed', 0, 3, '', ?, ?, '')
                """,
                (meeting_id, str(audio_path), created_at, created_at),
            )
        else:
            conn.execute(
                """
                INSERT OR IGNORE INTO jobs
                    (meeting_id, audio_path, status, retry_count, max_retries,
                     error_message, created_at, updated_at)
                VALUES (?, ?, 'completed', 0, 3, '', ?, ?)
                """,
                (meeting_id, str(audio_path), created_at, created_at),
            )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    finally:
        conn.close()

    # 3. 체크포인트 (요약 후보 표식).
    cp_dir = base_dir / "checkpoints" / meeting_id
    cp_dir.mkdir(parents=True, exist_ok=True)
    if has_merge:
        merge_path = cp_dir / "merge.json"
        if not merge_path.exists():
            merge_path.write_text(
                json.dumps(
                    {
                        "utterances": [
                            {
                                "text": f"테스트 회의 {meeting_id} 발화 1",
                                "speaker": "SPEAKER_00",
                                "start": 0.0,
                                "end": 2.0,
                            }
                        ],
                        "num_speakers": 1,
                        "audio_path": str(audio_path),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )


def _ensure_jobs_db(base_dir: Path) -> None:
    """JobQueue 가 사용하는 `pipeline.db` 의 jobs 테이블을 보장한다.

    `JobQueue(...).initialize()` 호출이 권장 경로지만, 본 헬퍼는 실제
    JobQueue 클래스 변경에 둔감하도록 SQL 로 직접 스키마를 만든다.
    실제 서버가 다시 띄워질 때 `initialize()` 가 idempotent 하게 보강한다.
    """
    db_path = base_dir / "pipeline.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id TEXT NOT NULL UNIQUE,
                audio_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 3,
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def ui_bulk_base_dir(tmp_path: Path) -> Path:
    """bulk-actions 시나리오의 격리된 base_dir.

    `MT_BASE_DIR` 로 서버 subprocess 에 전달될 디렉토리. tmp_path 위에
    표준 하위 폴더 (audio_input / outputs / checkpoints / chroma_db) 를
    미리 생성하고 0o700 권한을 부여한다 (`secure_dir` 이 요구하는
    쓰기 전용 보안 정책과 일치).

    함수 종료 시 정리는 pytest tmp_path 가 담당.
    """
    base = tmp_path / "ui-bulk"
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        # 일부 파일시스템(노트북 디스크 외) 에서 chmod 실패 가능 — 무시.
        pass

    for sub in ("audio_input", "outputs", "checkpoints", "chroma_db"):
        (base / sub).mkdir(parents=True, exist_ok=True)

    return base


@pytest.fixture
def ui_bulk_meeting_ids(ui_bulk_base_dir: Path) -> list[str]:
    """bulk-actions 시나리오에서 사이드바에 표시될 회의 5 건을 시드한다.

    분포:
      - id[0] : 가장 최신 (최상단). merge.json 있음 → 요약 후보.
      - id[1] : merge.json 없음 → 전사 후보.
      - id[2] : merge.json 있음 → 요약 후보.
      - id[3] : merge.json 없음 → 전사 후보.
      - id[4] : merge.json 있음 → 요약 후보. 가장 오래됨 (최하단).

    이 분포는 phase 2 시나리오의 가정과 일치한다:
      - 5 건 모두 사이드바에 보임 (`.meeting-item` count == 5)
      - "전사" 버튼 (action=transcribe) 은 merge.json 없는 항목 대상
      - "요약" 버튼 (action=summarize) 은 merge.json 있는 항목 대상
      - "최근 24시간" scope 도 모두 포함 (created_at 가 동일 시각대)

    ID 형식은 `meeting_YYYYMMDD_HHMMSS` — JobQueue 의 unique 제약과
    SPA 의 정렬 가정 (created_at desc) 모두 만족.

    Returns:
        시드 순서가 아니라 사이드바 표시 순서 (최신 → 오래된) 의 ID 리스트.
    """
    _ensure_jobs_db(ui_bulk_base_dir)

    # 의도적으로 분/초 단위로 시간차를 두어 created_at desc 정렬이 명확.
    seed_specs = [
        ("meeting_20260429_120500", True, "2026-04-29T12:05:00"),
        ("meeting_20260429_120400", False, "2026-04-29T12:04:00"),
        ("meeting_20260429_120300", True, "2026-04-29T12:03:00"),
        ("meeting_20260429_120200", False, "2026-04-29T12:02:00"),
        ("meeting_20260429_120100", True, "2026-04-29T12:01:00"),
    ]

    for meeting_id, has_merge, created_at in seed_specs:
        _seed_one_meeting(
            ui_bulk_base_dir,
            meeting_id,
            has_merge=has_merge,
            created_at=created_at,
        )

    # 사이드바 정렬 가정 (최신순) 과 동일한 순서로 반환.
    return [spec[0] for spec in seed_specs]


@pytest.fixture
def ui_bulk_server(
    ui_bulk_base_dir: Path,
    ui_bulk_meeting_ids: list[str],
) -> Iterator[subprocess.Popen]:
    """bulk-actions 시나리오용 FastAPI 서버 subprocess.

    `python main.py --no-menubar` 를 격리 포트 (8767) + 격리 base_dir 로
    띄우고 `/api/health` 200 OK 까지 폴링한다. 종료 시 SIGTERM → 5 초
    grace → SIGKILL 순으로 정리.

    pyannote / mlx-whisper 등 무거운 모델은 본 시나리오에서 호출되지
    않으므로 `MT_LLM_BACKEND=ollama` 같은 환경변수로 강제할 필요 없음
    (라우트 mock 으로 우회 가능). 다만 모델 매니저 부팅 자체가 실패해도
    `/api/health` 는 응답하므로 서버 기동에는 영향 없음.

    Yields:
        subprocess.Popen 객체 (테스트 코드는 직접 사용하지 않고
        `ui_bulk_base_url` 만 의존).
    """
    env = os.environ.copy()
    env["MT_BASE_DIR"] = str(ui_bulk_base_dir)
    env["MT_SERVER_PORT"] = str(_BULK_TEST_PORT)
    # 테스트 로그 노이즈 감소 — 사용자가 디버깅 시 INFO/DEBUG 로 변경 가능.
    env.setdefault("MT_LOG_LEVEL", "warning")

    log_file = ui_bulk_base_dir / "server.log"
    log_fd = open(log_file, "w", encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "main.py", "--no-menubar"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log_fd,
        stderr=subprocess.STDOUT,
    )

    base_url = f"http://127.0.0.1:{_BULK_TEST_PORT}"
    try:
        _wait_for_health(base_url, timeout=30.0)
        yield proc
    finally:
        # 그레이스풀 종료 — SIGTERM 5 초 대기 후 SIGKILL.
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        log_fd.close()


@pytest.fixture
def ui_bulk_base_url(ui_bulk_server: subprocess.Popen) -> str:
    """bulk-actions 시나리오의 서버 베이스 URL.

    Note:
        포트 (`8767`) 는 `_BULK_TEST_PORT` 단일 진실. 다른 e2e 테스트
        (`test_e2e_edit_playwright` 의 8766) 와 분리되어 병렬 실행
        시 충돌하지 않는다.
    """
    # ui_bulk_server 가 정상 기동했음을 _wait_for_health 가 보장 — 단순 반환.
    return f"http://127.0.0.1:{_BULK_TEST_PORT}"


# ============================================================================
# 페이지 헬퍼 — selector 단일 진실 공급원 (review-2b §3 결정)
# ============================================================================


def meetings_listbox(page: Page) -> Locator:
    """사이드바 회의 목록 컨테이너 locator.

    ARIA 속성 (`role="listbox"` + `aria-multiselectable="true"`) 만으로
    매칭하는 단일 진실 공급원. 컨테이너 클래스 (`list-content` /
    `meetings-list`) 가 frontend-a 의 결정 사항이므로 클래스/ID
    fallback chain 은 사용하지 않는다 (review-2b §3 — selector 컨벤션).

    사용 패턴:
        `meetings_listbox(page).count() == 1` 로 컨테이너 존재 강제 검증
        후 `.first` 로 클래스/속성 조회. 미구현 SPA 에서는
        `aria-multiselectable` 속성이 없어 0 매칭 → 시나리오 명확한 FAIL.

    Args:
        page: Playwright Page.

    Returns:
        Locator (count == 1 기대 — frontend-a 가 `#listContent` 에
        ARIA 속성을 부여하면 자동으로 매칭됨).
    """
    return page.locator("[role='listbox'][aria-multiselectable='true']")


def empty_meetings_dom(page: Page) -> None:
    """사이드바 회의 항목을 DOM 에서 모두 제거 (빈 상태 시뮬레이션).

    JobQueue 에 시드한 5 건의 회의를 서버 재기동 없이 클라이언트 DOM
    레벨에서만 비우는 헬퍼. 빈 사이드바 엣지 케이스 (B10 — Cmd+A no-op,
    B11 — selection mode 진입 불가) 시나리오가 사용한다.

    동작:
      1. `#listContent` 의 모든 자식 노드 제거 (`.meeting-item` 포함).
      2. 카운트 라벨 (`.list-header .count`) 이 있으면 "0" 으로 갱신.

    Note:
        실제 SPA 의 회의 목록 렌더링 로직 (`spa.js` 의 fetch + render) 은
        이미 5 건을 그렸고, 본 헬퍼는 그 결과 DOM 만 비운다. 이후
        라우터/render 가 다시 호출되면 5 건이 복원되므로 시나리오에서
        본 헬퍼 호출 직후 추가 라우팅을 발생시키면 안 된다.

        `#listContent` ID 는 frontend-a 핸드오프 합의 (위 모듈 docstring
        참고). ARIA 속성 미부여 단계에서도 ID 자체는 존재하므로 본
        헬퍼는 미구현 SPA 에서도 동작.

    Args:
        page: Playwright Page (이미 `/app` 로드 + 5 건 렌더 완료 상태).
    """
    page.evaluate(
        """() => {
            const list = document.getElementById('listContent');
            if (list) {
                while (list.firstChild) {
                    list.removeChild(list.firstChild);
                }
            }
            // 카운트 라벨이 있으면 0 으로 — 클래스 후보를 좁히지 않고 모두 갱신.
            const countNodes = document.querySelectorAll(
                '.list-header .count, .meeting-count, [data-meeting-count]'
            );
            countNodes.forEach((el) => { el.textContent = '0'; });
        }"""
    )
