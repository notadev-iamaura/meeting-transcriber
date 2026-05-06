"""운영 문서가 legacy review workflow 를 권장하지 않는지 검증."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.harness


def test_harness_quickstart_uses_consensus_workflow() -> None:
    readme = Path("harness/README.md").read_text(encoding="utf-8")

    assert "review record --ticket" not in readme
    assert "review status --ticket" not in readme
    assert "compatibility 용" in readme
    assert "consensus require" in readme
    assert "review submit" in readme


def test_legacy_review_policy_is_compatibility_not_removal() -> None:
    policy_sources = [
        Path("harness/README.md"),
        Path("docs/agentic-ops/README.md"),
        Path("docs/agentic-ops/06-consensus-harness-implementation.md"),
        Path("docs/agentic-ops/07-consensus-integration-wave.md"),
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in policy_sources)

    assert "historical compatibility" in combined
    assert "운영 권한은 consensus 모델만" in combined
    assert "warning 추가나 제거는 현 단계에서 하지 않는다" in combined


def test_active_agentic_docs_do_not_recommend_legacy_review_record() -> None:
    docs = sorted(Path("docs/agentic-ops").glob("*.md"))
    offenders: list[str] = []
    for path in docs:
        text = path.read_text(encoding="utf-8")
        if "harness review record" in text or "review status --ticket" in text:
            offenders.append(str(path))

    assert offenders == []
