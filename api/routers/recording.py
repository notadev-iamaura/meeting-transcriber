"""수동 녹음 API 라우터."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from api.dependencies import get_recorder as _get_recorder

logger = logging.getLogger(__name__)

router = APIRouter()


class RecordingStatusResponse(BaseModel):
    """녹음 상태 응답 스키마.

    Attributes:
        state: 녹음 상태 ("idle", "recording", "stopping")
        is_recording: 녹음 중 여부
        duration_seconds: 현재 녹음 경과 시간 (초)
        meeting_id: 현재 녹음 중인 회의 ID
        device: 사용 중인 오디오 장치명
        is_system_audio: 시스템 오디오 캡처 여부
    """

    state: str
    is_recording: bool = False
    duration_seconds: float = 0.0
    meeting_id: str | None = None
    device: str | None = None
    is_system_audio: bool = False


class AudioDeviceItem(BaseModel):
    """오디오 장치 응답 스키마.

    Attributes:
        index: ffmpeg 장치 인덱스
        name: 장치 이름
        is_blackhole: BlackHole 가상 장치 여부
        is_aggregate: macOS Aggregate Device 여부 (본인 마이크 + 시스템 오디오 통합)
    """

    index: int
    name: str
    is_blackhole: bool = False
    is_aggregate: bool = False


class RecordingStartRequest(BaseModel):
    """녹음 시작 요청 스키마.

    Attributes:
        meeting_id: 회의 식별자 (선택, 없으면 자동 생성)
    """

    meeting_id: str | None = None


@router.get("/recording/status", response_model=RecordingStatusResponse)
async def get_recording_status(
    request: Request,
) -> RecordingStatusResponse:
    """녹음 상태를 조회한다.

    Args:
        request: FastAPI Request 객체

    Returns:
        RecordingStatusResponse: 현재 녹음 상태
    """
    recorder = _get_recorder(request)
    status = recorder.get_status()
    return RecordingStatusResponse(**status)


@router.post("/recording/start")
async def start_recording(
    request: Request,
    body: RecordingStartRequest | None = None,
) -> dict[str, Any]:
    """수동 녹음을 시작한다.

    Args:
        request: FastAPI Request 객체
        body: 녹음 시작 요청 (선택)

    Returns:
        녹음 시작 결과

    Raises:
        HTTPException: 이미 녹음 중(409), 장치 에러(500), 서버 에러(500)
    """
    recorder = _get_recorder(request)
    meeting_id = body.meeting_id if body else None

    try:
        from steps.recorder import AlreadyRecordingError, AudioDeviceError

        await recorder.start_recording(meeting_id=meeting_id)
        return {
            "status": "ok",
            "message": "녹음을 시작했습니다.",
            "meeting_id": recorder._meeting_id,
            "device": recorder.current_device_name,
        }
    except AlreadyRecordingError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except AudioDeviceError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"녹음 시작 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"녹음 시작 중 오류가 발생했습니다: {e}",
        ) from e


@router.post("/recording/stop")
async def stop_recording(request: Request) -> dict[str, Any]:
    """녹음을 정지한다.

    Args:
        request: FastAPI Request 객체

    Returns:
        녹음 정지 결과

    Raises:
        HTTPException: 서버 에러(500)
    """
    recorder = _get_recorder(request)

    try:
        result = await recorder.stop_recording()
        if result is None:
            return {
                "status": "ok",
                "message": "녹음이 정지되었습니다. (최소 시간 미달로 파일 파기)",
                "discarded": True,
            }

        return {
            "status": "ok",
            "message": "녹음이 정지되었습니다.",
            "file_path": str(result.file_path),
            "duration_seconds": result.duration_seconds,
            "audio_device": result.audio_device,
        }
    except Exception as e:
        logger.exception(f"녹음 정지 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"녹음 정지 중 오류가 발생했습니다: {e}",
        ) from e


@router.get("/recording/devices", response_model=list[AudioDeviceItem])
async def get_recording_devices(
    request: Request,
) -> list[AudioDeviceItem]:
    """사용 가능한 오디오 장치 목록을 반환한다.

    Args:
        request: FastAPI Request 객체

    Returns:
        오디오 장치 목록

    Raises:
        HTTPException: 장치 검색 실패(500)
    """
    recorder = _get_recorder(request)

    try:
        devices = await recorder.detect_audio_devices()
        return [
            AudioDeviceItem(
                index=dev.index,
                name=dev.name,
                is_blackhole=dev.is_blackhole,
                is_aggregate=dev.is_aggregate,
            )
            for dev in devices
        ]
    except Exception as e:
        logger.exception(f"오디오 장치 조회 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"오디오 장치 조회 중 오류가 발생했습니다: {e}",
        ) from e
