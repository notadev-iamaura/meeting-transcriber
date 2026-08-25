"""GitHub Actions와 로컬 타입 검사 설정의 정합성 계약."""

import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.harness


def test_mypy_ci_python은_프로젝트_target과_일치한다() -> None:
    """CI mypy 런타임이 pyproject의 분석 대상 Python과 같아야 한다."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected_version = str(pyproject["tool"]["mypy"]["python_version"])

    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["type-check"]["steps"]
    setup_python = next(
        step for step in steps if str(step.get("uses", "")).startswith("actions/setup-python@")
    )

    assert str(setup_python["with"]["python-version"]) == expected_version
