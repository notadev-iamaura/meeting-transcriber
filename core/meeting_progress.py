"""DB 상태와 완료 단계로 목록·상세·접수 기록의 공통 표시를 만든다."""

from typing import Any

STEP_LABELS = {
    "convert": "오디오 변환",
    "transcribe": "음성 인식",
    "diarize": "화자분리",
    "merge": "전사 병합",
    "correct": "AI 교정",
    "summarize": "요약",
    "chunk": "검색 준비",
    "embed": "검색 반영",
}
STATUS_LABELS = {
    "recorded": "녹음 완료",
    "recording": "녹음 중",
    "queued": "대기열 등록",
    "transcribing": "전사 중",
    "diarizing": "화자분리 중",
    "merging": "병합 중",
    "embedding": "후처리 중",
    "completed": "완료",
    "failed": "처리 실패",
    "cancelled": "사용자가 취소함",
    "blocked": "입력 검증으로 실행 보류",
}


def effective_meeting_status(status: str, state: dict[str, Any] | None = None) -> str:
    """전사 완료 DB row에서 별도 후처리의 실행·실패 상태를 표시한다."""
    state = state or {}
    if status == "completed" and state.get("status") in {"running", "failed"}:
        return "failed" if state["status"] == "failed" else "embedding"
    return status


def meeting_progress(status: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """성공한 단계와 실패 단계를 분리하며 대기 작업에 과거 실패를 표시하지 않는다."""
    state = state or {}
    status = effective_meeting_status(status, state)
    skipped = state.get("skipped_steps", [])
    skipped = skipped if isinstance(skipped, list) else []
    raw = state.get("completed_steps", [])
    completed = (
        [s for s in raw if isinstance(s, str) and s in STEP_LABELS and s not in skipped]
        if isinstance(raw, list)
        else []
    )
    step = state.get("current_step", "")
    step = step if isinstance(step, str) and step in STEP_LABELS else ""
    failed = step if status == "failed" else ""
    transcript = "merge" in completed
    label = STATUS_LABELS.get(status, status)
    if failed:
        prefix = (
            "전사 완료 · "
            if transcript
            else ("음성 인식 완료 · " if "transcribe" in completed else "")
        )
        label = prefix + STEP_LABELS[failed] + " 실패"
    elif status == "completed" and transcript and "summarize" not in completed:
        label = "전사 완료 · 교정·요약 대기"
    elif status not in {"recorded", "recording", "queued", "completed", "failed"} and step:
        label = ("전사 완료 · " if transcript else "") + STEP_LABELS[step] + " 중"
    return {
        "status_label": label,
        "completed_steps": completed,
        "failed_step": failed,
        "current_step": step if status not in {"recorded", "queued"} else "",
        "transcript_available": transcript,
        "retry_label": {
            "correct": "교정 재시도",
            "summarize": "요약 재시도",
            "embed": "검색 반영 재시도",
        }.get(failed, "실패한 단계부터 다시 시도"),
    }
