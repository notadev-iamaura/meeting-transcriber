"""Wiki 현황의 익명 담당자 그룹이 회의 범위와 확인 필요 상태를 표시하는지 검사한다."""

from pathlib import Path

import pytest
from playwright.sync_api import Browser

pytestmark = pytest.mark.ui
WEB = Path(__file__).resolve().parents[3] / "ui" / "web"


def test_anonymous_group_headers_show_meeting_scope_and_unresolved_state(browser: Browser) -> None:
    """같은 번호 두 카드가 서로 다른 회의임을 표시하고 이름을 새로 추정하지 않는다."""
    context = browser.new_context()
    page = context.new_page()
    try:
        page.set_content('<main id="fixture"></main>')
        for name in ("app.js", "wiki-view.js"):
            page.add_script_tag(path=str(WEB / name))
        data = [
            dict(
                owner="SPEAKER_02",
                meeting_id="meeting_20261105_100001",
                identity_scope="meeting",
                items=[dict(description="첫 업무", citations=[])],
            ),
            dict(
                owner="SPEAKER_02",
                meeting_id="meeting_20261105_100002",
                identity_scope="meeting",
                items=[dict(description="다른 업무", citations=[])],
            ),
            dict(
                owner="SPEAKER_02",
                meeting_id=None,
                identity_scope="unresolved",
                items=[dict(description="범위 확인 업무", citations=[])],
            ),
            dict(owner="윤 담당", items=[dict(description="기존 실명 업무", citations=[])]),
        ]
        result = page.evaluate(
            """groups => {
            const W=MeetingWikiView.create({App:MeetingApp,Router:{},ListPanel:{}});
            const view=Object.create(W.prototype);
            document.querySelector('#fixture').innerHTML=view._renderOpenActionsSection(groups);
            return Array.from(document.querySelectorAll('.wiki-ov-card')).map(card=>({
                owner:card.querySelector('.wiki-ov-card__owner').textContent,
                scope:card.querySelector('.wiki-ov-card__meta')?.textContent || '',
                count:card.querySelector('.wiki-ov-count').textContent,
                body:card.textContent
            }));
        }""",
            data,
        )
        assert len(result) == 4
        assert result[0]["owner"] == result[1]["owner"] == "SPEAKER_02"
        assert "2026-11-05 10:00:01" in result[0]["scope"]
        assert "2026-11-05 10:00:02" in result[1]["scope"]
        assert "회의 확인 필요" in result[2]["scope"] and "담당자 확인 필요" in result[2]["scope"]
        assert result[3]["owner"] == "윤 담당" and result[3]["scope"] == ""
        assert all(r["count"] == "1건" for r in result)
    finally:
        context.close()
