"""분리 녹음의 마이크 전사 상태와 기존 발화를 보존하는 재시도 계약."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from core.io_utils import atomic_write_json_pinned, read_text_no_follow
from core.recording_channels import (
    MIC_CHANNEL,
    SELF_SPEAKER,
    extract_recording_tracks,
    has_separate_mic_track,
    prepare_recording_track,
)
from core.search_revision import checkpoint_dir, meeting_date, read_source

logger = logging.getLogger(__name__)
MIC_RETRY_ACTION = "retry_mic"
_ISSUES = {"empty", "low_volume", "partial", "retry_failed"}
PARTIAL_MESSAGE = "마이크 음성 구간 중 전사문이 없는 부분이 있습니다. 음성을 확인하거나 마이크만 다시 시도해 주세요."


def write_mic_status(
    path: Path,
    status: str,
    speech_detected: bool,
    *,
    model: str = "",
    message: str = "",
    **details: Any,
) -> None:
    """관측된 마이크 처리 결과를 원자적으로 저장한다."""
    try:
        previous = _read_json(path)
    except FileNotFoundError:
        previous = {}
    atomic_write_json_pinned(
        path,
        {
            "version": 1,
            "status": status,
            "speech_detected": speech_detected,
            "model": model,
            "message": message,
            "request_id": previous.get("request_id", ""),
            **details,
        },
        backup=False,
    )


def _read_json(path: Path) -> dict[str, Any]:
    """심볼릭 링크를 따르지 않고 저장된 객체만 읽는다."""
    value = json.loads(read_text_no_follow(path))
    if not isinstance(value, dict):
        raise ValueError("마이크 전사 상태가 올바르지 않습니다.")
    return value


def uncovered_mic_speech(speech: Any, rows: Any, settings: Any) -> list[list[float]]:
    """검증된 실제 음성에서 전사 시각을 제외한 연속 누락 후보만 반환한다."""
    tolerance = settings.transcript_time_tolerance_seconds
    coverage = sorted(
        (float(row["start"]) - tolerance, float(row["end"]) + tolerance) for row in rows
    )
    missing = []
    for left, right in speech:
        cursor = float(left)
        for start, end in coverage:
            if end <= cursor:
                continue
            if start >= right:
                break
            if start - cursor >= settings.missing_speech_min_seconds:
                missing.append([cursor, min(float(right), start)])
            cursor = max(cursor, end)
            if cursor >= right:
                break
        if right - cursor >= settings.missing_speech_min_seconds:
            missing.append([cursor, float(right)])
    return missing


def _new_mic_rows(
    source: dict[str, Any], candidates: list[dict[str, Any]], tolerance: float
) -> list[dict[str, Any]]:
    """기존 내 발화와 시간이 겹치는 결과는 바꾸지 않고 새 시간대만 더한다."""
    own = [row for row in source.get("utterances", []) if row.get("speaker") == SELF_SPEAKER]
    added: list[dict[str, Any]] = []
    for row in sorted(candidates, key=lambda item: (item["start"], item["end"])):
        if not str(row.get("text", "")).strip() or row["end"] <= row["start"]:
            continue
        if any(
            row["start"] < old["end"] + tolerance and row["end"] > old["start"] - tolerance
            for old in own
        ) or any(row["start"] < old["end"] and row["end"] > old["start"] for old in added):
            continue
        added.append(row)
    return added


def _source_or_empty(
    config: Any, meeting_id: str, audio_path: Path
) -> tuple[Path, dict[str, Any]]:
    """두 트랙 모두 전사에 실패한 경우만 비어 있는 복구 원문을 만든다."""
    try:
        return read_source(config, meeting_id)
    except FileNotFoundError:
        cp = checkpoint_dir(config, meeting_id)
        try:
            read_text_no_follow(cp / "transcribe.json")
        except FileNotFoundError:
            return cp / "correct.json", {
                "utterances": [],
                "audio_path": str(audio_path),
                "mic_retry_bootstrap": True,
            }
        raise ValueError("기존 전사 초안의 병합을 먼저 완료해 주세요.") from None


def _with_mic_rows(
    config: Any,
    meeting_id: str,
    source: dict[str, Any],
    mic_rows: list[dict[str, Any]],
    attempt: str,
) -> dict[str, Any]:
    """원래 발화·편집·실패 추적을 보존하며 마이크 발화를 시간순으로 삽입한다."""
    if not mic_rows:
        return dict(source)
    original = list(source["utterances"])
    tagged = [(row, index) for index, row in enumerate(original)] + [
        (row, None) for row in mic_rows
    ]
    tagged.sort(key=lambda pair: (pair[0]["start"], pair[0]["end"]))
    rows = [row for row, _ in tagged]
    remap = {old: new for new, (_, old) in enumerate(tagged) if old is not None}
    replacement = dict(source)
    if "correction_failures" in replacement:
        replacement["correction_failures"] = [
            dict(item, index=remap[item["index"]]) for item in source["correction_failures"]
        ]
    if "failed_indices" in replacement:
        replacement["failed_indices"] = [remap[index] for index in source["failed_indices"]]
    replacement.update(
        utterances=rows,
        num_speakers=len({u["speaker"] for u in rows if u["speaker"] != "UNKNOWN"}),
        meeting_date=meeting_date(config, meeting_id, source),
        search_revision=attempt,
        mic_retry_id=attempt,
    )
    return replacement


def mic_transcription_status(config: Any, meeting_id: str, job: Any) -> dict[str, Any]:
    """DB 실행 상태와 영속 결과를 합쳐 재진입 가능한 화면 상태를 반환한다."""
    from steps.transcriber import AudioAdmissionError

    path = checkpoint_dir(config, meeting_id) / "mic_transcription.json"
    try:
        saved = _read_json(path)
    except FileNotFoundError:
        saved = {}
    status = saved.get("status", "not_applicable")
    if not saved:
        try:
            prior = _read_json(path.with_name("transcribe.json"))
        except FileNotFoundError:
            prior = {}
        if prior.get("separate_mic_track") is True:
            status = "unknown"
        elif str(getattr(job, "status", "")) in {"completed", "failed", "recorded"}:
            try:
                if has_separate_mic_track(Path(job.audio_path)):
                    status = "unknown"
            except (OSError, ValueError, AudioAdmissionError):
                pass
    if not isinstance(status, str):
        raise ValueError("마이크 전사 상태 값이 올바르지 않습니다.")
    detected = saved.get("speech_detected") is True
    action = str(getattr(job, "requested_action", ""))
    job_status = str(getattr(job, "status", ""))
    if action == MIC_RETRY_ACTION and job_status == "queued":
        status = "retry_queued"
    elif action == MIC_RETRY_ACTION and job_status in {"transcribing", "embedding", "merging"}:
        status = "retrying"
    messages = {
        "empty": "마이크에서 처리할 음성 구간을 찾았지만 내 발화의 전사 결과가 비어 있습니다.",
        "low_volume": "마이크에서 처리할 음성 구간을 찾았지만 음량 문제로 내 발화를 전사하지 못했습니다.",
        "partial": PARTIAL_MESSAGE,
        "retry_failed": "마이크 재전사를 완료하지 못했습니다. 기존 전사문은 보존했습니다.",
        "retry_queued": "내 마이크 트랙 재전사를 기다리고 있습니다.",
        "retrying": "내 마이크 트랙을 다시 처리하고 있습니다.",
        "complete": "마이크 트랙 전사를 완료했습니다.",
        "no_speech": "마이크에서 전사할 음성 구간을 찾지 못했습니다.",
        "processing": "마이크 트랙을 전사하고 있습니다.",
        "unknown": "이 회의에는 마이크 처리 기록이 없어 발화 누락 여부를 확인할 수 없습니다.",
    }
    available = status in (_ISSUES | {"complete", "unknown"}) and job_status in {
        "completed",
        "failed",
        "recorded",
    }
    if available:
        try:
            _source_or_empty(config, meeting_id, Path(job.audio_path))
            available = has_separate_mic_track(Path(job.audio_path))
        except (OSError, ValueError, TypeError, AttributeError, AudioAdmissionError):
            available = False
    return {
        "meeting_id": meeting_id,
        "status": status,
        "speech_detected": detected,
        "retry_available": available,
        "message": (saved.get("message") if status == saved.get("status") else "")
        or messages.get(status, ""),
        "added_utterances": saved.get("added_utterances"),
        "no_new_speech": saved.get("no_new_speech") is True,
        "uncovered_intervals": saved.get("uncovered_intervals", []),
    }


async def retry_mic_track(
    pipeline: Any,
    audio_path: Path,
    meeting_id: str,
    *,
    model: str,
    on_step_start: Any = None,
    should_cancel: Any = None,
) -> None:
    """회의 lease를 가진 호출자가 마이크만 전사하고 원문 게시·재색인을 재개한다."""
    from core.reindex_recovery import _reindex_meeting_artifacts_locked
    from steps.transcriber import EmptyTranscriptError

    config = pipeline._config
    cp = checkpoint_dir(config, meeting_id)
    status_path = cp / "mic_transcription.json"
    journal_path = cp / "mic_retry.json"
    settings = config.recording.split_track_processing

    def finish(journal: dict[str, Any], source: dict[str, Any]) -> None:
        """추가 수와 남아 있는 음성 공백을 저장해 성공과 누락을 별도로 알린다."""
        missing = uncovered_mic_speech(
            journal.get("speech_intervals", []),
            [row for row in source["utterances"] if row.get("speaker") == SELF_SPEAKER],
            settings,
        )
        count = len(journal.get("mic_rows", []))
        message = PARTIAL_MESSAGE if missing else "마이크 트랙 전사를 완료했습니다."
        if not count:
            message = "새로 추가할 마이크 발화가 없습니다. 기존 발화와 편집을 그대로 보존했습니다."
            if missing:
                message += f" {PARTIAL_MESSAGE}"
        write_mic_status(
            status_path,
            "partial" if missing else "complete",
            True,
            model=model,
            message=message,
            added_utterances=count,
            no_new_speech=count == 0,
            uncovered_intervals=missing,
        )

    def check_cancel() -> None:
        """마이크 추론과 게시 사이에서 취소 요청을 확인한다."""
        if should_cancel is not None and should_cancel():
            raise asyncio.CancelledError("마이크 재전사를 취소했습니다.")

    async def step(name: str) -> None:
        """기존 처리 이벤트와 취소 경계를 재사용한다."""
        check_cancel()
        if on_step_start is not None:
            await on_step_start(name)

    try:
        try:
            previous = _read_json(status_path)
        except FileNotFoundError:
            previous = {}
        write_mic_status(status_path, "retrying", True, model=model)
        source_path, source = await asyncio.to_thread(
            _source_or_empty, config, meeting_id, audio_path
        )
        try:
            journal = await asyncio.to_thread(_read_json, journal_path)
        except FileNotFoundError:
            journal = {}
        if journal.get("phase") == "complete":
            if journal.get("request_id", "") == previous.get("request_id", ""):
                if not journal.get("no_change") and source.get("mic_retry_id") != journal.get(
                    "attempt"
                ):
                    raise ValueError("마이크 재시도 결과와 현재 원문이 일치하지 않습니다.")
                finish(journal, source)
                return
            journal = {}
        if journal:
            # 실패 뒤 명시 재요청으로 색인을 재개한 작업도 완료 직후 종료되면
            # 같은 요청으로 인식해야 불필요한 두 번째 STT를 실행하지 않는다.
            journal["request_id"] = previous.get("request_id", "")
        if journal.get("phase") == "prepared" and previous.get("status") == "retry_failed":
            # 이전 시도에서 게시 전 원문이 바뀌었다면 새 요청 시점의 편집본을 기준으로
            # 이미 얻은 마이크 발화만 다시 결합하며 기존 '나' 편집도 그대로 둔다.
            current = read_text_no_follow(source_path) if source_path.exists() else None
            attempt = uuid.uuid4().hex
            mic_rows = _new_mic_rows(
                source, journal["mic_rows"], settings.transcript_time_tolerance_seconds
            )
            journal.update(
                attempt=attempt,
                source_path=str(source_path),
                source_sha256=hashlib.sha256(current.encode()).hexdigest()
                if current is not None
                else None,
                replacement=_with_mic_rows(config, meeting_id, source, mic_rows, attempt),
                mic_rows=mic_rows,
                no_change=not mic_rows,
            )
            atomic_write_json_pinned(journal_path, journal, backup=True)
        if not journal:
            if not await asyncio.to_thread(has_separate_mic_track, audio_path):
                raise ValueError("마이크 트랙이 확인된 앱 녹음만 다시 처리할 수 있습니다.")
            identity = pipeline._validate_input(audio_path)
            await pipeline._validate_audio_duration(audio_path, identity)
            await step("transcribe")
            try:
                original = await asyncio.to_thread(read_text_no_follow, source_path)
            except FileNotFoundError:
                original = None
            with TemporaryDirectory(
                prefix=".recap-mic-retry-", dir=audio_path.parent
            ) as directory:
                tracks = await asyncio.to_thread(
                    extract_recording_tracks,
                    audio_path,
                    Path(directory),
                    (MIC_CHANNEL,),
                    identity,
                    echo_settings=settings,
                )
                if MIC_CHANNEL not in tracks:
                    raise EmptyTranscriptError("마이크 트랙에서 전사할 음성을 찾지 못했습니다.")
                minimum = (
                    config.audio_quality.min_mean_volume_db
                    if config.audio_quality.enabled
                    else settings.speech_floor_dbfs
                )
                track = await asyncio.to_thread(
                    prepare_recording_track, tracks[MIC_CHANNEL], settings, minimum
                )
                if track is None:
                    raise EmptyTranscriptError(
                        "마이크 트랙에서 전사할 음성 구간을 찾지 못했습니다."
                    )
                result = await pipeline._transcribe_wav(
                    track.path,
                    selected_provider="local",
                    selected_model=model,
                    prepared_track=track,
                )
                if not result.segments:
                    raise EmptyTranscriptError("마이크 재전사 결과가 비어 있습니다.")
            check_cancel()
            pipeline._assert_input_identity(audio_path, identity)
            mic_rows = [
                {
                    "text": segment.text,
                    "original_text": segment.text,
                    "speaker": SELF_SPEAKER,
                    "start": segment.start,
                    "end": segment.end,
                    "was_corrected": False,
                }
                for segment in result.segments
            ]
            mic_rows = _new_mic_rows(source, mic_rows, settings.transcript_time_tolerance_seconds)
            attempt = uuid.uuid4().hex
            replacement = _with_mic_rows(config, meeting_id, source, mic_rows, attempt)
            journal = {
                "version": 1,
                "attempt": attempt,
                "phase": "prepared",
                "source_path": str(source_path),
                "source_sha256": hashlib.sha256(original.encode()).hexdigest()
                if original is not None
                else None,
                "replacement": replacement,
                "request_id": previous.get("request_id", ""),
                "speech_intervals": track.speech_intervals,
                "mic_rows": mic_rows,
                "no_change": not mic_rows,
            }
            atomic_write_json_pinned(journal_path, journal, backup=True)
        check_cancel()
        # 재개할 때도 현재 정규 원문만 대상으로 하며 journal의 임의 경로에는 쓰지 않는다.
        source_path, source = await asyncio.to_thread(
            _source_or_empty, config, meeting_id, audio_path
        )
        if str(source_path) != journal.get("source_path"):
            raise ValueError("마이크 재시도 중 원문의 저장 위치가 변경되었습니다.")
        if source.get("mic_retry_id") != journal.get("attempt"):
            try:
                current = await asyncio.to_thread(read_text_no_follow, source_path)
            except FileNotFoundError:
                current = None
            digest = hashlib.sha256(current.encode()).hexdigest() if current is not None else None
            if digest != journal.get("source_sha256"):
                raise ValueError("마이크 재시도 중 원문이 변경되어 덮어쓰지 않았습니다.")
            if journal.get("no_change"):
                journal["phase"] = "complete"
                atomic_write_json_pinned(journal_path, journal, backup=False)
                finish(journal, source)
                return
            atomic_write_json_pinned(source_path, journal["replacement"], backup=True)
        journal["phase"] = "published"
        atomic_write_json_pinned(journal_path, journal, backup=False)
        await step("embed")
        await _reindex_meeting_artifacts_locked(config, pipeline._model_manager, meeting_id)
        journal["phase"] = "complete"
        atomic_write_json_pinned(journal_path, journal, backup=False)
        _, final_source = await asyncio.to_thread(read_source, config, meeting_id)
        finish(journal, final_source)
    except (Exception, asyncio.CancelledError):
        message = "마이크 재전사를 완료하지 못했습니다. 기존 발화와 편집은 보존했습니다."
        try:
            journal = _read_json(journal_path)
            if journal.get("phase") == "published":
                message = "내 마이크 발화는 저장했지만 검색 반영이 끝나지 않았습니다. 다시 시도하면 검색 반영만 재개합니다."
            write_mic_status(status_path, "retry_failed", True, model=model, message=message)
        except FileNotFoundError:
            write_mic_status(status_path, "retry_failed", True, model=model, message=message)
        raise
