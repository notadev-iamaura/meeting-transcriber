"""PipelineManager × WikiCompiler 통합 테스트 (Phase 1 9단계)

테스트 시나리오:
    1. wiki.enabled=False 일 때 — pipeline 통과, wiki 디렉토리 안 만들어짐
    2. wiki.enabled=True 일 때 — pipeline 통과, wiki/log.md 1줄 + .git 존재
    3. wiki.enabled=True 이지만 init_repo 실패 (권한 거부) — pipeline 정상 종료 (non-fatal)
    4. 같은 meeting_id 두 번 ingest — log.md 2줄

각 테스트는 PipelineManager 의 다른 모든 단계 (CONVERT~SUMMARIZE) 를 mock 한다.
9단계만 진짜로 실행되어 wiki 디스크 효과를 검증한다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config import AppConfig, WikiConfig
from core.pipeline import PipelineManager


# ─────────────────────────────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────────────────────────────


def _build_config(
    *,
    tmp_path: Path,
    wiki_enabled: bool,
    wiki_root: Path | None = None,
) -> MagicMock:
    """PipelineManager 가 요구하는 최소 config 모킹 객체.

    test_pipeline.py 의 mock_config 패턴을 차용하되, wiki 필드는 진짜
    WikiConfig pydantic 인스턴스를 사용해 PipelineManager 에서 `enabled` /
    `resolved_root` 를 정상적으로 읽을 수 있게 한다.
    """
    config = MagicMock()
    config.pipeline.checkpoint_enabled = True
    config.pipeline.retry_max_count = 1
    config.pipeline.peak_ram_limit_gb = 9.5
    config.pipeline.min_disk_free_gb = 0.0  # 테스트 환경 디스크 체크 우회
    config.pipeline.min_memory_free_gb = 0.0
    config.pipeline.skip_llm_steps = False
    config.pipeline.correct_timeout_seconds = 1800
    config.pipeline.summarize_timeout_seconds = 600
    config.pipeline.llm_lock_acquire_timeout_seconds = 3600
    config.pipeline.llm_recommended_memory_gb = 6.5

    config.paths.resolved_outputs_dir = tmp_path / "outputs"
    config.paths.resolved_checkpoints_dir = tmp_path / "checkpoints"

    config.audio.sample_rate = 16000
    config.audio.channels = 1
    config.audio.format = "wav"
    config.audio.supported_input_formats = ["wav", "mp3", "m4a"]

    config.stt.model_name = "test"
    config.stt.language = "ko"
    config.stt.beam_size = 5

    config.diarization.model_name = "test"
    config.diarization.device = "cpu"
    config.diarization.min_speakers = 1
    config.diarization.max_speakers = 10
    config.diarization.huggingface_token = "test-token"

    config.llm.model_name = "test"
    config.llm.host = "http://127.0.0.1:11434"
    config.llm.temperature = 0.3
    config.llm.max_context_tokens = 8192
    config.llm.correction_batch_size = 10
    config.llm.request_timeout_seconds = 120

    # ── wiki: 진짜 WikiConfig 객체 ──────────────────────────────────────
    config.wiki = WikiConfig(
        enabled=wiki_enabled,
        root=(wiki_root if wiki_root is not None else (tmp_path / "wiki")),
        dry_run=True,
    )
    return config


def _make_audio_file(tmp_path: Path) -> Path:
    """테스트용 가짜 오디오 파일."""
    audio = tmp_path / "test_meeting.m4a"
    audio.write_bytes(b"fake audio content for testing")
    return audio


def _make_mock_wav(tmp_path: Path) -> Path:
    """테스트용 가짜 WAV 파일."""
    wav = tmp_path / "test_16k.wav"
    wav.write_bytes(b"fake wav content")
    return wav


def _make_mock_step_results() -> dict[str, MagicMock]:
    """6개 파이프라인 단계의 Mock 결과를 만든다."""
    transcript = MagicMock()
    transcript.segments = [MagicMock(text="안녕하세요", start=0.0, end=2.0)]
    transcript.save_checkpoint = MagicMock()

    diarization = MagicMock()
    diarization.segments = [MagicMock(speaker="SPEAKER_00", start=0.0, end=2.0)]
    diarization.num_speakers = 1
    diarization.save_checkpoint = MagicMock()

    merged = MagicMock()
    merged.utterances = [
        MagicMock(text="안녕하세요", speaker="SPEAKER_00", start=0.0, end=2.0)
    ]
    merged.num_speakers = 1
    merged.save_checkpoint = MagicMock()

    corrected = MagicMock()
    corrected.utterances = merged.utterances
    corrected.save_checkpoint = MagicMock()

    summary = MagicMock()
    summary.summary = "테스트 요약"
    summary.save_checkpoint = MagicMock()
    summary.save_markdown = MagicMock()

    return {
        "transcript": transcript,
        "diarization": diarization,
        "merged": merged,
        "corrected": corrected,
        "summary": summary,
    }


def _patched_pipeline(pipeline: PipelineManager, wav_path: Path) -> Any:
    """6단계 모두 mock 하는 contextmanager 셋업."""
    mocks = _make_mock_step_results()
    return [
        patch.object(
            pipeline,
            "_run_step_convert",
            new_callable=AsyncMock,
            return_value=wav_path,
        ),
        patch.object(
            pipeline,
            "_run_step_transcribe",
            new_callable=AsyncMock,
            return_value=mocks["transcript"],
        ),
        patch.object(
            pipeline,
            "_run_step_diarize",
            new_callable=AsyncMock,
            return_value=mocks["diarization"],
        ),
        patch.object(
            pipeline,
            "_run_step_merge",
            new_callable=AsyncMock,
            return_value=mocks["merged"],
        ),
        patch.object(
            pipeline,
            "_run_step_correct",
            new_callable=AsyncMock,
            return_value=mocks["corrected"],
        ),
        patch.object(
            pipeline,
            "_run_step_summarize",
            new_callable=AsyncMock,
            return_value=mocks["summary"],
        ),
    ]


def _git_commit_count(repo: Path) -> int:
    """git repo 의 커밋 개수."""
    if not (repo / ".git").exists():
        return 0
    proc = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    return int(proc.stdout.strip() or "0") if proc.returncode == 0 else 0


# ─────────────────────────────────────────────────────────────────────────
# 1. wiki disabled — wiki 디렉토리 자체가 안 생긴다
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wiki_disabled_시_pipeline은_정상_완료_wiki_디렉토리_없음(
    tmp_path: Path,
) -> None:
    """`wiki.enabled=False` 일 때 9단계가 호출되지 않아야 한다."""
    audio = _make_audio_file(tmp_path)
    wav = _make_mock_wav(tmp_path)
    wiki_root = tmp_path / "wiki"
    config = _build_config(
        tmp_path=tmp_path, wiki_enabled=False, wiki_root=wiki_root
    )
    pipeline = PipelineManager(config, MagicMock())

    patches = _patched_pipeline(pipeline, wav)
    for p in patches:
        p.start()
    try:
        state = await pipeline.run(audio, meeting_id="aaa11111")
    finally:
        for p in patches:
            p.stop()

    assert state.status == "completed"
    # 9단계는 PIPELINE_STEPS 메인 루프 밖이므로 completed_steps 에는 없음
    assert "wiki_compile" not in state.completed_steps
    # wiki 디렉토리가 만들어지지 않았는지 확인
    assert not wiki_root.exists()


# ─────────────────────────────────────────────────────────────────────────
# 2. wiki enabled — log.md 1줄 + .git 존재
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wiki_enabled_시_log_md와_git이_생성된다(tmp_path: Path) -> None:
    """`wiki.enabled=True` 일 때 9단계가 실행되어 wiki/log.md + .git 이 생긴다."""
    audio = _make_audio_file(tmp_path)
    wav = _make_mock_wav(tmp_path)
    wiki_root = tmp_path / "wiki"
    config = _build_config(
        tmp_path=tmp_path, wiki_enabled=True, wiki_root=wiki_root
    )
    pipeline = PipelineManager(config, MagicMock())

    patches = _patched_pipeline(pipeline, wav)
    for p in patches:
        p.start()
    try:
        state = await pipeline.run(audio, meeting_id="bbb22222")
    finally:
        for p in patches:
            p.stop()

    assert state.status == "completed"
    assert wiki_root.exists()
    assert (wiki_root / ".git").exists()
    log_text = (wiki_root / "log.md").read_text(encoding="utf-8")
    assert "bbb22222" in log_text
    # 9단계 결과가 step_results 에 기록되었는지
    wiki_steps = [
        sr for sr in state.step_results if sr.get("step") == "wiki_compile"
    ]
    assert len(wiki_steps) == 1
    assert wiki_steps[0]["success"] is True


# ─────────────────────────────────────────────────────────────────────────
# 3. wiki enabled but failure — non-fatal escalation
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wiki_실패해도_pipeline은_정상_종료_non_fatal(tmp_path: Path) -> None:
    """init_repo 가 WikiStoreError 를 던져도 pipeline 은 status=completed 로 끝나야 한다."""
    from core.wiki.store import WikiStoreError

    audio = _make_audio_file(tmp_path)
    wav = _make_mock_wav(tmp_path)
    wiki_root = tmp_path / "wiki-denied"
    config = _build_config(
        tmp_path=tmp_path, wiki_enabled=True, wiki_root=wiki_root
    )
    pipeline = PipelineManager(config, MagicMock())

    # WikiStore.init_repo 가 항상 권한 오류를 던지도록 강제
    def _raise_perm(*_args: Any, **_kwargs: Any) -> None:
        raise WikiStoreError("permission_denied", "테스트용 강제 실패")

    patches = _patched_pipeline(pipeline, wav)
    for p in patches:
        p.start()
    try:
        with patch(
            "core.wiki.store.WikiStore.init_repo",
            side_effect=_raise_perm,
        ):
            state = await pipeline.run(audio, meeting_id="ccc33333")
    finally:
        for p in patches:
            p.stop()

    # 9단계 실패는 non-fatal — 메인 status 는 completed
    assert state.status == "completed"
    # 경고에 기록되었어야 함
    assert any("wiki 9단계" in w for w in state.warnings)
    # step_results 에 실패 기록
    wiki_steps = [
        sr for sr in state.step_results if sr.get("step") == "wiki_compile"
    ]
    assert len(wiki_steps) == 1
    assert wiki_steps[0]["success"] is False


# ─────────────────────────────────────────────────────────────────────────
# 4. 같은 meeting_id 두 번 ingest — log.md 2줄
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_같은_meeting_id_두_번_ingest_시_log_md_두_줄(tmp_path: Path) -> None:
    """동일 meeting_id 로 pipeline.run 을 두 번 호출해도 log.md 에 누적 기록되어야 한다."""
    audio1 = _make_audio_file(tmp_path)
    audio2 = tmp_path / "test_meeting2.m4a"
    audio2.write_bytes(b"fake audio 2")
    wav = _make_mock_wav(tmp_path)
    wiki_root = tmp_path / "wiki"
    config = _build_config(
        tmp_path=tmp_path, wiki_enabled=True, wiki_root=wiki_root
    )

    # 두 번 다 같은 meeting_id 로 호출 — wiki 입장에서는 각각 별개 ingest 라인
    for audio_file, mid in [(audio1, "ddd44444"), (audio2, "eee55555")]:
        pipeline = PipelineManager(config, MagicMock())
        patches = _patched_pipeline(pipeline, wav)
        for p in patches:
            p.start()
        try:
            await pipeline.run(audio_file, meeting_id=mid)
        finally:
            for p in patches:
                p.stop()

    log_text = (wiki_root / "log.md").read_text(encoding="utf-8")
    ingest_lines = [
        ln for ln in log_text.splitlines() if "ingest meeting:" in ln
    ]
    assert len(ingest_lines) == 2
    assert any("ddd44444" in ln for ln in ingest_lines)
    assert any("eee55555" in ln for ln in ingest_lines)
    # 커밋 카운트: init_repo(+1) + ingest 1(+1) + ingest 2(+1) ≥ 3
    assert _git_commit_count(wiki_root) >= 3
