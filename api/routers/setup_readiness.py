"""최초 설정 마법사용 readiness API 라우터."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from security.setup_readiness import ReadinessActionKind, collect_setup_readiness

router = APIRouter()


class ReadinessActionItem(BaseModel):
    """설정 마법사용 read-only 다음 단계 안내."""

    id: str
    label: str
    kind: ReadinessActionKind
    value: str
    description: str = ""


class ReadinessCheckItem(BaseModel):
    """설정 마법사용 단일 준비 상태 항목."""

    id: str
    status: str
    ready: bool
    message: str
    action_hint: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    actions: list[ReadinessActionItem] = Field(default_factory=list)


class SetupReadinessResponse(BaseModel):
    """GET /api/setup/readiness 응답 스키마."""

    status: str
    configured: bool
    ready: bool
    capabilities: dict[str, bool] = Field(default_factory=dict)
    checks: list[ReadinessCheckItem] = Field(default_factory=list)


@router.get("/setup/readiness", response_model=SetupReadinessResponse)
async def get_setup_readiness(request: Request) -> SetupReadinessResponse:
    """최초 설정 마법사용 로컬 준비 상태를 반환한다."""
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="서버 설정이 초기화되지 않았습니다.")

    report = collect_setup_readiness(config)
    return SetupReadinessResponse(
        status=report.status,
        configured=report.configured,
        ready=report.ready,
        capabilities=report.capabilities.to_dict(),
        checks=[
            ReadinessCheckItem(
                id=check.id,
                status=check.status,
                ready=check.ready,
                message=check.message,
                action_hint=check.action_hint,
                details=check.details,
                actions=[
                    ReadinessActionItem(
                        id=action.id,
                        label=action.label,
                        kind=action.kind,
                        value=action.value,
                        description=action.description,
                    )
                    for action in check.actions
                ],
            )
            for check in report.checks
        ],
    )
