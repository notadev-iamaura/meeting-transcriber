"""실제 WikiView와 API 클라이언트의 편집·충돌·이력 사용자 흐름을 검증한다."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Browser, Page, Route, expect

pytestmark = pytest.mark.ui
WEB = Path(__file__).resolve().parents[3] / "ui" / "web"
BODY = "# 예산 검토\n\n예산은 아직 미정이다. [meeting:audit_meeting@00:01:02]\n"


def test_action_identity_is_hidden_without_hiding_task_or_user_text(browser: Browser) -> None:
    """저장용 작업 메타데이터만 숨기고 본문·다른 주석은 기존대로 보인다."""
    context, page, _writes = _open_wiki(browser)
    try:
        rendered = page.evaluate(
            """() => MeetingApp.renderMarkdown(
                '<!-- action-identity-v1: eyJvd25lciI6ImRlbW8ifQ== -->\\n' +
                '- [ ] 담당자: 출시 점검 [meeting:demo@00:01:00]\\n' +
                '<!-- confidence: 9 -->\\n' +
                '<!-- 사용자 메모 -->'
            )"""
        )
        assert "action-identity" not in rendered
        assert "eyJvd25lciI6ImRlbW8ifQ==" not in rendered
        assert "confidence:" not in rendered
        assert "출시 점검" in rendered
        assert "사용자 메모" in rendered
    finally:
        context.close()


def _open_wiki(
    browser: Browser,
    *,
    conflict: bool = False,
    mobile: bool = False,
    api_client: Any = None,
    review_reason: str | None = None,
    review_metadata: dict[str, Any] | None = None,
) -> tuple[Any, Page, list[dict[str, Any]]]:
    """사용자 데이터·서버·모델 없이 실제 웹 모듈을 API 경계에서 격리한다."""
    context = browser.new_context(viewport={"width": 390 if mobile else 1280, "height": 844})
    page = context.new_page()
    writes: list[dict[str, Any]] = []
    detail: dict[str, Any] = {
        "path": "decisions/budget.md",
        "type": "decisions",
        "title": "예산 검토",
        "content": BODY,
        "editable_content": BODY,
        "revision": "a" * 64,
        "frontmatter": {"title": "예산 검토", "confidence": 9},
        "citations": [],
        "human_edited": False,
        "human_reviewed": False,
        "related_pages": [
            {
                "path": "projects/apollo.md",
                "type": "project",
                "title": "Apollo",
                "relation": "관련 프로젝트",
            }
        ],
    }
    if review_reason:
        detail["frontmatter"].update(
            review_reason=review_reason,
            citation_unresolved=True,
            unresolved_citation_fields=["decision_text"],
        )
    if review_metadata:
        detail["frontmatter"].update(review_metadata)

    def respond(route: Route) -> None:
        """정적 자산은 실제 파일을, 데이터 경계에는 결정적인 API 응답을 제공한다."""
        url = route.request.url
        if "/static/" in url:
            name = url.rsplit("/", 1)[-1]
            route.fulfill(path=str(WEB / name))
            return
        if "/api/" not in url:
            route.fulfill(
                content_type="text/html",
                body="""<!doctype html><html lang="ko"><head>
                <meta charset="utf-8"><link rel="stylesheet" href="/static/style.css">
                <link rel="stylesheet" href="/static/wiki.css"></head><body>
                <main id="fixture" style="height:100vh"></main>
                <script src="/static/app.js"></script><script src="/static/wiki-view.js"></script>
                <script>const Router={getContentEl:()=>document.getElementById('fixture'),navigate:()=>testView.canLeave()};
                const View=MeetingWikiView.create({App:MeetingApp,Router});window.testView=new View();</script>
                </body></html>""",
            )
            return
        if api_client is not None:
            parsed = urlsplit(url)
            response = api_client.request(
                route.request.method,
                parsed.path + ("?" + parsed.query if parsed.query else ""),
                content=route.request.post_data,
                headers={"content-type": "application/json"},
            )
            route.fulfill(
                status=response.status_code, content_type="application/json", body=response.content
            )
            return
        result: Any = {}
        if "/wiki/pages/" in url:
            if route.request.method == "PUT":
                payload = route.request.post_data_json
                writes.append(payload)
                if conflict:
                    route.fulfill(status=409, json={"detail": "위키 내용이 변경됐습니다."})
                    return
                detail.update(
                    content=payload["content"],
                    editable_content=payload["content"],
                    revision="b" * 64,
                    human_edited=True,
                    human_reviewed=payload["reviewed"],
                    index_status="current",
                )
            result = (
                detail
                if "/decisions/" in url or "/decision/" in url
                else {**detail, "path": "projects/apollo.md", "title": "Apollo"}
            )
        elif "/wiki/history/" in url:
            result = {
                "revisions": [
                    {
                        "revision": "c" * 64,
                        "created_at": "2026-11-03T09:00:00",
                        "content": "이전 내용",
                        "human_reviewed": False,
                    }
                ]
            }
        elif "/wiki/restore/" in url:
            writes.append(route.request.post_data_json)
            detail.update(
                content="이전 내용",
                editable_content="이전 내용",
                revision="d" * 64,
                human_edited=True,
            )
            result = detail
        elif "/wiki/pages" in url:
            result = {
                "pages": [
                    {"path": "decisions/budget.md", "type": "decision", "title": "예산 검토"}
                ],
                "total": 1,
            }
        elif "/wiki/digest" in url:
            result = {
                "generated_for": "2026-11-03",
                "total_open_actions": 0,
                "open_actions": [],
                "recent_decisions": [],
                "project_status": [],
            }
        elif "/wiki/health" in url:
            result = {"status": "no_lint_yet"}
        route.fulfill(json=result)

    page.route("http://wiki.test/**", respond)
    page.goto("http://wiki.test/app/wiki?tab=search")
    page.get_by_role("button", name="예산 검토", exact=True).click()
    expect(page.get_by_role("button", name="수정", exact=True)).to_be_visible()
    return context, page, writes


def test_unresolved_citation_reason_is_explained_without_internal_fields(browser: Browser) -> None:
    """미해결 인용 후보에는 한국어 안내를 보여주고 내부 필드명은 노출하지 않는다."""
    context, page, _ = _open_wiki(browser, review_reason="citation_unresolved")
    try:
        expect(
            page.get_by_text("근거 발화의 인용 위치를 확인하지 못함 · 원문 검토 필요", exact=True)
        ).to_be_visible()
        expect(page.get_by_text("citation_unresolved", exact=True)).to_have_count(0)
        expect(page.get_by_text("unresolved_citation_fields", exact=True)).to_have_count(0)
    finally:
        context.close()


@pytest.mark.parametrize(
    "result,label",
    [
        ("held", "추가 근거 필요"),
        ("verified", "근거 확인됨"),
        ("historical", "과거 버전"),
        ("identity_unresolved", "화자 이름 확인 필요"),
    ],
)
def test_ai_review_metadata_is_readable_and_separate_from_user_review(
    browser: Browser, result: str, label: str
) -> None:
    """재검수 결과는 한국어로 보이고 AI 확인만으로 사용자 검토 표시를 바꾸지 않는다."""
    reviewed_at = "2026-11-05T12:34:56+09:00"
    detail = "원문에는 요청 <확정 아님>만 있어 해당 범위를 확인했습니다."
    context, page, writes = _open_wiki(
        browser,
        review_metadata={
            "ai_reviewed_at": reviewed_at,
            "ai_review_result": result,
            "review_detail": detail,
        },
    )
    try:
        for key, value in (
            ("AI 재검수 일시", reviewed_at),
            ("AI 재검수 결과", label),
            ("검수 메모", detail),
        ):
            row = page.locator(".wiki-preview-frontmatter-row").filter(
                has=page.get_by_text(key, exact=True)
            )
            expect(row.locator(".wiki-preview-frontmatter-value")).to_have_text(value)
        for raw in ("ai_reviewed_at", "ai_review_result", "review_detail", result):
            expect(page.get_by_text(raw, exact=True)).to_have_count(0)
        expect(page.locator(".wiki-page-review")).to_have_text("자동 생성 · 내용 확인 필요")
        expect(page.get_by_text("사용자 검토 완료", exact=True)).to_have_count(0)
        page.get_by_role("button", name="수정", exact=True).click()
        expect(
            page.get_by_role("checkbox", name="원문과 비교해 내용을 검토했습니다")
        ).not_to_be_checked()
        page.get_by_role("button", name="취소", exact=True).click()
        assert writes == []
    finally:
        context.close()


@pytest.mark.parametrize("mobile", [False, True])
def test_edit_save_protects_manual_content_and_keeps_citations(
    browser: Browser, mobile: bool
) -> None:
    """원문 인용을 유지한 수정·검토 선택·버전 전달과 작은 화면 동작을 확인한다."""
    context, page, writes = _open_wiki(browser, mobile=mobile)
    try:
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.get_by_role("button", name="수정", exact=True).click()
        editor = page.get_by_role("textbox", name="위키 내용 수정")
        expect(editor).to_be_focused()
        editor.fill(BODY + "\n다음 회의에서 다시 확인한다.")
        page.get_by_role("checkbox", name="원문과 비교해 내용을 검토했습니다").check()
        editor.press("Control+Enter")
        expect(page.locator(".wiki-page-message")).to_have_text("수정본을 저장했습니다.")
        expect(page.locator(".wiki-page-review")).to_have_text("사용자 검토 완료")
        assert writes[0]["expected_revision"] == "a" * 64
        assert "[meeting:audit_meeting@00:01:02]" in writes[0]["content"]
        assert writes[0]["reviewed"] is True
        assert not errors
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        context.close()


def test_conflict_preserves_input_and_blocks_navigation(browser: Browser) -> None:
    """동시 수정 충돌 후 입력을 보존하고 명시적인 취소 전 화면 이탈을 막는다."""
    context, page, writes = _open_wiki(browser, conflict=True)
    try:
        page.get_by_role("button", name="수정", exact=True).click()
        editor = page.get_by_role("textbox", name="위키 내용 수정")
        editor.fill(BODY + "\n사용자 추가 메모")
        page.get_by_role("button", name="저장", exact=False).click()
        expect(page.locator(".wiki-page-message")).to_contain_text("입력한 내용은 유지했습니다")
        expect(editor).to_have_value(BODY + "\n사용자 추가 메모")
        assert page.evaluate("testView.canLeave()") is False
        page.get_by_role("tab", name="현황").click()
        expect(page.get_by_role("tab", name="검색")).to_have_attribute("aria-selected", "true")
        page.get_by_role("button", name="취소", exact=True).click()
        assert page.evaluate("testView.canLeave()") is True
        assert len(writes) == 1
    finally:
        context.close()


def test_history_restore_and_related_navigation(browser: Browser) -> None:
    """이전 내용을 확인한 뒤 최신 버전을 조건으로 복원하고 관련 프로젝트를 연다."""
    context, page, writes = _open_wiki(browser)
    try:
        page.get_by_role("button", name="변경 이력", exact=True).click()
        page.locator(".wiki-history summary").click()
        expect(page.locator(".wiki-history pre")).to_have_text("이전 내용")
        page.get_by_role("button", name="이 내용으로 복원").click()
        expect(page.locator(".wiki-page-message")).to_contain_text("새 수정본으로 복원")
        assert writes[0] == {"revision": "c" * 64, "expected_revision": "a" * 64}
        page.get_by_role("button", name="Apollo", exact=True).click()
        expect(page.locator(".wiki-preview-page-path")).to_have_text("projects/apollo.md")
    finally:
        context.close()


def test_browser_and_real_api_roundtrip_save_reload_and_restore(
    browser: Browser, tmp_path: Path
) -> None:
    """실제 라우터·파일·검색 색인과 브라우저 편집기를 연결해 저장과 복원을 확인한다."""
    from core.wiki.store import WikiStoreError
    from tests.wiki.test_editing import _client, _seed

    store = _seed(tmp_path)
    original = store.read_raw_page(Path("decisions/budget.md"))
    with _client(store, tmp_path) as client:
        context, page, _ = _open_wiki(browser, api_client=client)
        try:
            page.get_by_role("button", name="수정", exact=True).click()
            editor = page.get_by_role("textbox", name="위키 내용 수정")
            editor.fill(editor.input_value().replace("미정이다", "다음 회의에서 재논의한다"))
            page.get_by_role("checkbox", name="원문과 비교해 내용을 검토했습니다").check()
            page.get_by_role("button", name="저장", exact=False).click()
            expect(page.locator(".wiki-page-message")).to_contain_text("키워드 검색에 반영")
            assert "재논의" in store.read_page(Path("decisions/budget.md")).content
            with pytest.raises(WikiStoreError, match="human_protected"):
                store.write_page(Path("decisions/budget.md"), original)
            page.goto("http://wiki.test/app/wiki?tab=search")
            expect(page.locator("#wikiTree")).to_contain_text("예산 검토")
            page.get_by_role("button", name=re.compile(r"^예산 검토")).click()
            expect(page.locator(".wiki-page-review")).to_have_text("사용자 검토 완료")
            page.get_by_role("button", name="변경 이력", exact=True).click()
            old_version = page.locator(".wiki-history-version").filter(
                has=page.locator('[data-wiki-action="restore"]')
            )
            old_version.locator("summary").click()
            old_version.get_by_role("button", name="이 내용으로 복원").click()
            expect(page.locator(".wiki-page-message")).to_contain_text("새 수정본으로 복원")
            restored = store.read_page(Path("decisions/budget.md"))
            assert "미정이다" in restored.content
            assert restored.frontmatter["human_reviewed"] == "false"
        finally:
            context.close()
