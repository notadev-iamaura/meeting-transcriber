"""api.dependencies — app.state 의존성 접근 헬퍼 테스트."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException


def _request_with_state(**values: object) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**values)))


def test_get_job_queue_returns_state_value() -> None:
    from api import dependencies

    queue = object()
    request = _request_with_state(job_queue=queue)

    assert dependencies.get_job_queue(request) is queue


def test_get_job_queue_raises_503_when_missing() -> None:
    from api import dependencies

    request = _request_with_state()

    with pytest.raises(HTTPException) as exc_info:
        dependencies.get_job_queue(request)

    assert exc_info.value.status_code == 503
    assert "작업 큐" in exc_info.value.detail


def test_get_outputs_dir_returns_config_path() -> None:
    from api import dependencies

    outputs_dir = object()
    config = SimpleNamespace(paths=SimpleNamespace(resolved_outputs_dir=outputs_dir))
    request = _request_with_state(config=config)

    assert dependencies.get_outputs_dir(request) is outputs_dir
