"""현재 회의록 우선순위·안건 분리·인용 보존의 독립 회귀."""

import hashlib
import json
from pathlib import Path

import pytest

from config import AppConfig, PathsConfig
from search.topic_minutes import current_minutes_markdown, topic_minutes


@pytest.fixture
def store(tmp_path: Path) -> tuple[AppConfig, Path]:
    cfg = AppConfig(paths=PathsConfig(base_dir=str(tmp_path)))
    output = cfg.paths.resolved_outputs_dir / "example"
    output.mkdir(parents=True)
    return cfg, output


def test_exact_excerpt_status_and_distinct_agendas(store: tuple[AppConfig, Path]) -> None:
    cfg, output = store
    md = """# 정원 회의
## 주요 안건
1. 온실 통풍
   - 기존 환기 원칙을 유지하기로 함.
2. 선박 정비
   - 선체 정비는 이번 주에 시작하기로 함.
## 결정 사항
- 온실 통풍 자동화는 제안 단계이며 아직 확정되지 않음.
- 선박 통풍 장치는 기존 것을 교체하기로 함.
## 액션 아이템
- [ ] 보라: 온실 통풍 측정기를 점검하기로 함. 자료 수신 뒤 약 2~3일이 걸릴 전망이며 금요일은 확정 기한이 아님.
- [ ] 민수: 선박 수리 업체에 화요일 연락하기로 함.
"""
    (output / "meeting_minutes.md").write_text(md)
    result = topic_minutes(cfg, "example", "온실 통풍")
    assert result and result.digest == hashlib.sha256(md.encode()).hexdigest()
    assert "아직 확정되지 않음" in result.facts["decision"][0].text
    assert "보라" in result.facts["action"][0].text
    assert "금요일은 확정 기한이 아님" in result.facts["schedule"][0].text
    for facts in result.facts.values():
        assert 1 <= len(facts) <= 2
        for fact in facts:
            assert fact.text in fact.excerpt and fact.excerpt in md
            assert fact.source_kind == "minutes"
            assert "선박" not in fact.text and "민수" not in fact.text


def test_viewer_edit_priority_and_empty_fallback(store: tuple[AppConfig, Path]) -> None:
    cfg, output = store
    cp = cfg.paths.resolved_checkpoints_dir / "example"
    cp.mkdir(parents=True)
    (cp / "summarize.json").write_text('{"markdown":"checkpoint"}')
    assert current_minutes_markdown(cfg, "example") == "checkpoint"
    (output / "summary.json").write_text(json.dumps({"markdown": "json"}))
    assert current_minutes_markdown(cfg, "example") == "json"
    (output / "meeting_minutes.md").write_text("generated")
    assert current_minutes_markdown(cfg, "example") == "generated"
    (output / "summary.md").write_text("edited")
    assert current_minutes_markdown(cfg, "example") == "edited"
    (output / "summary.md").write_text("")
    assert current_minutes_markdown(cfg, "example") == "json"
    (output / "summary.json").write_text("{")
    assert current_minutes_markdown(cfg, "example") is None


def test_missing_damaged_or_unsafe_artifacts(store: tuple[AppConfig, Path]) -> None:
    cfg, output = store
    assert topic_minutes(cfg, "example", "청진기") is None
    assert topic_minutes(cfg, "../example", "청진기") is None
    (output / "meeting_minutes.md").write_bytes(b"\xff")
    assert topic_minutes(cfg, "example", "청진기") is None
    (output / "meeting_minutes.md").write_text("old")
    (output / "summary.md").symlink_to(output / "meeting_minutes.md")
    assert topic_minutes(cfg, "example", "청진기") is None
    assert (output / "summary.md").is_symlink()


def test_history_unknown_and_unrelated_task_not_assigned(store: tuple[AppConfig, Path]) -> None:
    cfg, output = store
    (output / "meeting_minutes.md").write_text(
        "## 주요 안건\n### 심해 측량\n- 지난주 심해 측량에 8시간이 걸렸음.\n"
        "### 선박 입찰\n- 내일 입찰을 진행할 예정임.\n"
        "## 액션 아이템\n- [ ] 담당자 3: 내일 자료를 보내기로 함.\n"
    )
    result = topic_minutes(cfg, "example", "심해 측량")
    assert result and not any(result.facts.values())
    unknown = topic_minutes(cfg, "example", "과수원")
    assert unknown and not any(unknown.facts.values())


def test_parallel_scopes_and_named_action_override_repeated_decision(
    store: tuple[AppConfig, Path],
) -> None:
    cfg, output = store
    markdown = """## 주요 안건
1. 관측소 운영
   - 북부 관측망: 남부와 같은 방식으로 복구 순서를 변경함.
   - 남부 관측망: 중단 원인 검토가 필요함.
## 결정 사항
- 북부 관측망 중단에 대비하여 복구 순서를 변경하기로 함.
- 남부 관측망 중단에 대비하여 내일 예비선을 설치하기로 함.
## 액션 아이템
- [ ] 현주: 남부 관측망 중단 원인을 확인하고 금요일까지 복구.
- [ ] 지후: 관측망 중단 로그를 전문기관에 전달할 예정.
"""
    (output / "meeting_minutes.md").write_text(markdown)
    result = topic_minutes(cfg, "example", "북부 관측망 중단 복구")
    assert result
    assert "지후" in result.facts["action"][0].text
    assert any("남부와 같은 방식" in fact.text for fact in result.facts["decision"])
    assert not result.facts["schedule"]
    for found in result.facts.values():
        assert all("현주" not in fact.text and "예비선" not in fact.text for fact in found)


def test_sibling_subtopics_and_outstanding_clarification(store: tuple[AppConfig, Path]) -> None:
    cfg, output = store
    md = """## 주요 안건
1. 현장 장비
   - 야간 수문 고장의 판정 방법은 현장 측 확인이 필요하다.
   - 수문 검사 자동 촬영은 시공사 측에 문의함.
   - 수문 경보 기록은 별도 업체에 맡긴다.
## 액션 아이템
- [ ] 주현: 야간 고장을 현장에 다시 문의함.
- [ ] 연수: 수문 검사 자동 촬영은 다음 주 수요일에 시작할 예정.
- [ ] 도아: 수문 경보 기록을 내일 전달하기로 함.
"""
    (output / "meeting_minutes.md").write_text(md)
    result = topic_minutes(cfg, "example", "야간 수문 고장의 자동 처리")
    assert result
    assert "확인이 필요하다" in result.facts["decision"][0].text
    assert len(result.facts["action"]) == 1 and "주현" in result.facts["action"][0].text
    assert not result.facts["schedule"]


def test_close_repetition_keeps_new_numbers_conditions_and_names() -> None:
    from search.topic_minutes import _redundant

    original = "센터 직원이 야간 온도 기록을 수요일까지 관리자에게 보내기로 했다."
    assert _redundant(original.replace("했다", "합의했다"), original)
    assert not _redundant(original.replace("직원이", "직원 2명이"), original)
    assert not _redundant(original.replace("직원이", "직원이 승인 이후"), original)
    assert not _redundant(original.replace("직원이", "직원 하린님이"), original)
