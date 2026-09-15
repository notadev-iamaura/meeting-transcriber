#!/usr/bin/env python3
"""임시 합성 음성 두 건으로 실제 JobProcessor 단계 묶음 경로를 검증한다.

기존 앱 저장소·녹음·모델 캐시는 변경하지 않는다. 부족한 화자분리/임베딩 모델은
다운로드하지 않으며 명시적인 --stub-diarization / --stub-index로만 대체한다.
기본 서멀 쿨다운은 유지한다. 최종 보고서는 내용 대신 상태·단계·시간만 포함한다.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import wave
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.benchmark_bulk_models import environment_metadata

logger = logging.getLogger(__name__)


def cache_readiness() -> dict[str, Any]:
    """HF 캐시 파일과 credential 권한만 확인하며 토큰·가중치 내용은 읽지 않는다."""
    hf_home = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache/huggingface")))
    hub = Path(os.environ.get("HF_HUB_CACHE", str(hf_home / "hub")))
    report: dict[str, Any] = {}
    for label, repo in (
        ("diarization", "pyannote/speaker-diarization-community-1"),
        ("segmentation", "pyannote/segmentation-3.0"),
        ("embedding", "intfloat/multilingual-e5-small"),
    ):
        snapshots = hub / f"models--{repo.replace('/', '--')}" / "snapshots"
        files = [p for p in snapshots.glob("**/*") if p.is_file()]
        weights = any(p.name in {"model.safetensors", "pytorch_model.bin"} for p in files)
        tokenizer = any(p.name in {"tokenizer.json", "sentencepiece.bpe.model"} for p in files)
        report[label] = {
            "snapshot_exists": snapshots.is_dir(),
            "file_count": len(files),
            "weights_present": weights,
            "tokenizer_present": tokenizer,
        }
    credential = Path(os.environ.get("HF_TOKEN_PATH", str(hf_home / "token")))
    try:
        identity = credential.lstat()
        credential_ready = (
            stat.S_ISREG(identity.st_mode)
            and identity.st_uid == os.getuid()
            and identity.st_mode & 0o077 == 0
            and identity.st_size > 0
        )
    except OSError:
        credential_ready = False
    report["credential_file_metadata_ready"] = credential_ready
    report["embedding_offline_files_ready"] = bool(
        report["embedding"]["weights_present"] and report["embedding"]["tokenizer_present"]
    )
    report["diarization_ready_not_proven"] = True
    return report


def prepare_workspace(audio: Path, output: Path) -> tuple[list[Path], str]:
    """임시 디렉터리의 합성 원본을 새 저장소의 독립 파일 두 개로 복사한다."""
    temp_roots = (Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve())
    if not any(audio.resolve().is_relative_to(root) for root in temp_roots):
        raise ValueError("임시 디렉터리에 명시적으로 준비한 합성 음성만 허용합니다")
    identity = audio.lstat()
    if not stat.S_ISREG(identity.st_mode):
        raise ValueError("합성 원본은 symlink가 아닌 일반 파일이어야 합니다")
    if not any(output.parent.resolve().is_relative_to(root) for root in temp_roots):
        raise ValueError("출력은 임시 디렉터리 아래의 새 폴더여야 합니다")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    inputs = output / "audio_input"
    inputs.mkdir(mode=0o700)
    digest = hashlib.sha256(audio.read_bytes()).hexdigest()
    copies = []
    for index in (1, 2):
        target = inputs / f"synthetic_cohort_{index}.wav"
        shutil.copyfile(audio, target)
        copies.append(target)
    return copies, digest


def database_snapshot(db: Path) -> dict[str, Any]:
    """격리 DB에서 사용자 내용 없이 작업·단계 상태만 조회한다."""
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        jobs = [
            dict(row)
            for row in conn.execute("SELECT id, meeting_id, status FROM jobs ORDER BY id")
        ]
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stage_work_items'"
        ).fetchone()
        stages = (
            []
            if not exists
            else [
                dict(row)
                for row in conn.execute(
                    "SELECT id, cohort_id, meeting_id, stage, status, attempt_count, error_code "
                    "FROM stage_work_items ORDER BY id"
                )
            ]
        )
    return {"jobs": jobs, "stages": stages}


def completed_cohort(snapshot: dict[str, Any]) -> bool:
    """DB 완료 두 건과 같은 묶음의 단계 완료 여덟 건이 모두 있어야 성공으로 본다."""
    jobs, stages = snapshot["jobs"], snapshot["stages"]
    return (
        len(jobs) == 2
        and all(j["status"] == "completed" for j in jobs)
        and len(stages) == 8
        and all(s["status"] == "completed" for s in stages)
        and len({s["cohort_id"] for s in stages}) == 1
    )


def artifacts_valid(artifacts: list[dict[str, Any]]) -> bool:
    """DB 완료와 별도로 전사·교정·요약의 실제 산출물 조건을 확인한다."""
    return len(artifacts) == 2 and all(
        artifact.get("pipeline_status") == "completed"
        and artifact.get("transcript_segments", 0) > 0
        and artifact.get("correction_failed") == 0
        and artifact.get("summary_chars", 0) > 0
        and not artifact.get("summary_fallback_marker", True)
        for artifact in artifacts
    )


class EventRecorder:
    """앱 이벤트 타입·회의·단계·시간만 기록하는 WebSocket 대역."""

    def __init__(self, started: float) -> None:
        """내용을 저장하지 않는 이벤트 목록을 초기화한다."""
        self.started = started
        self.events: list[dict[str, Any]] = []

    async def broadcast_event(self, event: Any) -> None:
        """이벤트의 명시적 metadata 필드만 복사한다."""
        data = event.data
        self.events.append(
            {
                "event": event.event_type,
                "wall_seconds": time.perf_counter() - self.started,
                **{
                    key: data[key]
                    for key in ("meeting_id", "step", "status", "phase", "elapsed")
                    if key in data
                },
            }
        )


def install_stage_stubs(stack: ExitStack, *, diarization: bool, index: bool) -> None:
    """명시된 모델 단계만 대체하며 pipeline 저장·병합·상태 처리 계약은 유지한다."""
    if diarization:
        from steps.diarizer import DiarizationResult, DiarizationSegment

        async def diarize(self: Any, audio_path: Path, **kwargs: Any) -> Any:
            """합성 음성 전체를 한 화자 구간으로 대체한다."""
            with wave.open(str(audio_path), "rb") as wav:
                duration = wav.getnframes() / wav.getframerate()
            return DiarizationResult(
                segments=[DiarizationSegment("SPEAKER_00", 0.0, duration)],
                num_speakers=1,
                audio_path=str(audio_path),
                model_name="diagnostic-single-speaker-stub",
                output_mode="stub",
            )

        stack.enter_context(patch("steps.diarizer.Diarizer.diarize", diarize))
    if index:
        from steps.embedder import EmbeddedResult

        async def embed(self: Any, chunked: Any) -> Any:
            """검색 저장 성공을 주장하지 않는 빈 임베딩 결과를 반환한다."""
            return EmbeddedResult(
                chunks=[],
                meeting_id=chunked.meeting_id,
                date=chunked.date,
                chroma_stored=False,
                fts_stored=False,
            )

        stack.enter_context(patch("steps.embedder.Embedder.embed", embed))


async def run_verification(args: argparse.Namespace, readiness: dict[str, Any]) -> dict[str, Any]:
    """실제 앱 구성요소의 큐 접수부터 두 작업의 완료까지 실행한다."""
    from config import get_config
    from core.job_queue import AsyncJobQueue, JobQueue, JobStatus
    from core.model_manager import ModelLoadManager
    from core.orchestrator import JobProcessor
    from core.perf_stats import PerfStats
    from core.pipeline import PipelineManager
    from core.thermal_manager import ThermalManager
    from core.transcription_models import selection_from_config

    copies, source_hash = prepare_workspace(args.audio, args.output_dir)
    config = get_config()
    config.paths.base_dir = str(args.output_dir)
    config.pipeline.bulk_stage_batching = True
    config.pipeline.bulk_max_items = 2
    config.pipeline.skip_llm_steps = False
    config.stt.provider = "local"
    config.stt.model_name = "mlx-community/whisper-large-v3-turbo"
    config.llm.backend = "mlx"
    config.llm.mlx_model_name = "mlx-community/gemma-4-e4b-it-4bit"
    for name in ("outputs", "checkpoints", "chroma_db"):
        (args.output_dir / name).mkdir(mode=0o700)
    selection = selection_from_config(config)
    queue = AsyncJobQueue(JobQueue(config.paths.resolved_pipeline_db))
    await queue.initialize()
    for index, audio in enumerate(copies, 1):
        job_id = await queue.add_job(
            f"synthetic_cohort_{index}", str(audio), initial_status=JobStatus.RECORDED.value
        )
        await queue.queue_job(
            job_id, "full", stt_provider=selection.provider, stt_model=selection.model
        )
    manager = ModelLoadManager()
    pipeline = PipelineManager(config, manager)
    started = time.perf_counter()
    recorder = EventRecorder(started)
    report: dict[str, Any] = {
        "scope": "actual_jobprocessor_two_synthetic_files",
        "environment": environment_metadata(),
        "readiness": readiness,
        "substitutions": {"diarization": args.stub_diarization, "index": args.stub_index},
        "source_sha256": source_hash,
        "copied_source_inodes": [p.stat().st_ino for p in copies],
        "thermal": {
            "batch_size": config.thermal.batch_size,
            "cooldown_seconds": config.thermal.cooldown_seconds,
        },
        "success": False,
    }
    with ExitStack() as stack:
        install_stage_stubs(stack, diarization=args.stub_diarization, index=args.stub_index)
        from steps import summarizer

        fallback_calls = stack.enter_context(
            patch(
                "steps.summarizer._build_fallback_markdown",
                wraps=summarizer._build_fallback_markdown,
            )
        )
        isolated_stats = PerfStats.load(stats_path=args.output_dir / "perf_stats.json")
        # JobProcessor의 기존 기본 경로는 MT_BASE_DIR를 따르지 않으므로 생성부터 격리한다.
        with patch("core.orchestrator.PerfStats.load", return_value=isolated_stats):
            processor = JobProcessor(
                queue, pipeline, ThermalManager(config), recorder, poll_interval=0.2
            )
        try:
            await processor.start()
            while time.perf_counter() - started < args.timeout_seconds:
                snapshot = await asyncio.to_thread(
                    database_snapshot, config.paths.resolved_pipeline_db
                )
                if completed_cohort(snapshot):
                    report["success"] = True
                    break
                if any(j["status"] == "failed" for j in snapshot["jobs"]):
                    report["failure"] = "JOB_FAILED"
                    break
                await asyncio.sleep(0.5)
            else:
                report["failure"] = "TIMEOUT"
        finally:
            await processor.stop()
            await manager.unload_model()
            report["database"] = database_snapshot(config.paths.resolved_pipeline_db)
            report["events"] = recorder.events
            report["manager_after_cleanup"] = manager.get_status()
            report["wall_seconds_including_cooldown"] = time.perf_counter() - started
            report["source_unchanged"] = (
                hashlib.sha256(args.audio.read_bytes()).hexdigest() == source_hash
            )
            report["artifacts"] = []
            for index in (1, 2):
                mid = f"synthetic_cohort_{index}"
                state = pipeline.get_status(mid)
                summary = config.paths.resolved_outputs_dir / mid / "summary.md"
                minutes = config.paths.resolved_outputs_dir / mid / "meeting_minutes.md"
                transcript_path = config.paths.resolved_checkpoints_dir / mid / "transcribe.json"
                correct_path = config.paths.resolved_checkpoints_dir / mid / "correct.json"
                transcript = (
                    json.loads(transcript_path.read_text()) if transcript_path.is_file() else {}
                )
                correct = json.loads(correct_path.read_text()) if correct_path.is_file() else {}
                summary_text = (
                    summary.read_text()
                    if summary.is_file()
                    else minutes.read_text()
                    if minutes.is_file()
                    else ""
                )
                report["artifacts"].append(
                    {
                        "meeting_id": mid,
                        "pipeline_status": state.status if state else None,
                        "completed_steps": state.completed_steps if state else [],
                        "summary_present": summary.is_file() or minutes.is_file(),
                        "transcript_segments": len(transcript.get("segments", [])),
                        "correction_failed": correct.get("total_failed"),
                        "summary_chars": len(summary_text.strip()),
                        "summary_fallback_marker": any(
                            marker in summary_text
                            for marker in ("AI 요약 실패", "AI 통합 요약 실패", "요약 실패 — 원본")
                        ),
                        "steps": state.step_results if state else [],
                    }
                )
            report["summary_fallback_calls"] = fallback_calls.call_count
            report["success"] = (
                report["success"]
                and artifacts_valid(report["artifacts"])
                and fallback_calls.call_count == 0
                and report["source_unchanged"]
                and not manager.is_model_loaded
            )
            if not report["success"] and "failure" not in report:
                report["failure"] = "ARTIFACT_OR_CLEANUP_VALIDATION_FAILED"
            await queue.close()
            (args.output_dir / "verification.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    return report


def main() -> None:
    """모델 준비 상태를 확인하고 명시된 격리 진단만 수행한다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--stub-diarization", action="store_true")
    parser.add_argument("--stub-index", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    args = parser.parse_args()
    args.audio = args.audio.absolute()
    args.output_dir = args.output_dir.absolute()
    if args.timeout_seconds < 1:
        parser.error("timeout-seconds는 양수여야 합니다")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["MT_BASE_DIR"] = str(args.output_dir)
    readiness = cache_readiness()
    if not args.stub_diarization and not readiness["credential_file_metadata_ready"]:
        parser.error("화자분리 credential 준비 미확인: 진단 대체에는 --stub-diarization 필요")
    if not args.stub_index and not readiness["embedding_offline_files_ready"]:
        parser.error("e5 weights/tokenizer cache 부족: 진단 대체에는 --stub-index 필요")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(threadName)s %(message)s")
    report = asyncio.run(run_verification(args, readiness))
    logger.info(f"격리 진단 결과: success={report['success']}, output={args.output_dir}")
    if not report["success"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
