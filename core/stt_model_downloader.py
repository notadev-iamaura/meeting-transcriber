"""STT 모델 다운로더 + 양자화 매니저 모듈

목적: HuggingFace Hub 에서 STT 모델을 다운로드하고, 필요 시 mlx-examples 의
convert.py 를 호출해 4bit 양자화까지 수행한다. 동시 1개 다운로드만 허용하며,
백그라운드 asyncio 태스크로 비동기 실행된다.

주요 기능:
    - DownloadConflictError: 중복 다운로드 충돌 예외
    - DownloadJob: 진행 상태 dataclass
    - STTModelDownloader: 다운로드/양자화/검증 오케스트레이션

의존성:
    - huggingface_hub.snapshot_download (런타임에 import — 테스트 시 mock)
    - asyncio.create_subprocess_exec (양자화 convert.py 실행)
    - core/stt_model_registry, core/stt_model_status
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .stt_model_registry import STTModelSpec, get_by_id
from .stt_model_status import ModelStatus

logger = logging.getLogger(__name__)


class DownloadConflictError(Exception):
    """다른 STT 모델 다운로드가 이미 진행 중일 때 발생한다."""


@dataclass
class DownloadJob:
    """STT 모델 다운로드 작업의 런타임 상태."""

    job_id: str
    model_id: str
    status: ModelStatus
    progress_percent: int = 0
    current_step: str = ""
    started_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None


class STTModelDownloader:
    """STT 모델 다운로드 + 양자화 매니저.

    특징:
        - 동시 1개 다운로드만 허용 (asyncio.Lock 으로 동시성 제어)
        - start_download() 는 job_id만 반환하고 실제 작업은 백그라운드 태스크
        - get_progress() 로 폴링
        - wait_for() 로 테스트/검증 시 완료 대기 가능
    """

    def __init__(
        self,
        models_dir: Path,
        mlx_examples_path: Optional[Path] = None,
    ) -> None:
        """다운로더 초기화.

        Args:
            models_dir: 모델이 저장될 베이스 디렉토리 (없으면 생성).
            mlx_examples_path: mlx-examples/whisper 디렉토리 (convert.py 포함).
                None이면 ~/Projects/mlx-examples/whisper 기본값.
        """
        self._models_dir = Path(models_dir).expanduser()
        self._models_dir.mkdir(parents=True, exist_ok=True)
        self._mlx_examples = Path(
            mlx_examples_path
            or Path.home() / "Projects" / "mlx-examples" / "whisper"
        )
        self._jobs: dict[str, DownloadJob] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------

    async def start_download(self, model_id: str) -> str:
        """다운로드를 백그라운드에서 시작하고 job_id를 반환한다.

        Args:
            model_id: STT_MODELS 에 등록된 모델 ID.

        Returns:
            job_id 문자열.

        Raises:
            ValueError: 알 수 없는 model_id.
            DownloadConflictError: 이미 다운로드 중인 모델이 있을 때.
        """
        spec = get_by_id(model_id)
        if spec is None:
            raise ValueError(f"알 수 없는 STT 모델: {model_id}")

        async with self._lock:
            for existing in self._jobs.values():
                if existing.status in (
                    ModelStatus.DOWNLOADING,
                    ModelStatus.QUANTIZING,
                ):
                    raise DownloadConflictError(
                        f"이미 다운로드 중인 모델이 있습니다: {existing.model_id}"
                    )

            job_id = f"stt-download-{model_id}-{int(time.time() * 1000)}"
            job = DownloadJob(
                job_id=job_id,
                model_id=model_id,
                status=ModelStatus.DOWNLOADING,
                progress_percent=0,
                current_step="queued",
            )
            self._jobs[model_id] = job
            self._tasks[model_id] = asyncio.create_task(
                self._run_download(spec, job)
            )
            logger.info("STT 모델 다운로드 시작: %s (%s)", model_id, job_id)

        return job_id

    def get_progress(self, model_id: str) -> Optional[DownloadJob]:
        """현재 진행 중이거나 최근 완료된 작업의 상태를 반환한다."""
        return self._jobs.get(model_id)

    async def wait_for(self, model_id: str) -> None:
        """백그라운드 태스크가 끝날 때까지 대기한다 (테스트/동기 호출용)."""
        task = self._tasks.get(model_id)
        if task is not None:
            try:
                await task
            except Exception:
                # 태스크 내부 예외는 _run_download 에서 이미 기록됨.
                logger.debug("wait_for: 태스크 예외 흡수 (%s)", model_id)

    # ------------------------------------------------------------
    # 내부 오케스트레이션
    # ------------------------------------------------------------

    async def _run_download(
        self, spec: STTModelSpec, job: DownloadJob
    ) -> None:
        """다운로드 → (양자화) → 검증 파이프라인을 순차 실행한다."""
        try:
            # 1단계: HF 스냅샷 다운로드
            job.status = ModelStatus.DOWNLOADING
            job.current_step = "downloading"
            job.progress_percent = 10
            await self._hf_download(spec, job)

            # 2단계: 양자화 (필요 시)
            if spec.needs_quantization:
                job.status = ModelStatus.QUANTIZING
                job.current_step = "quantizing"
                job.progress_percent = 60
                await self._quantize(spec, job)

            # 3단계: 검증
            job.current_step = "verifying"
            job.progress_percent = 95
            if not self._verify(spec):
                raise RuntimeError("모델 검증 실패: 필수 파일 누락")

            job.status = ModelStatus.READY
            job.progress_percent = 100
            job.completed_at = datetime.now()
            logger.info("STT 모델 다운로드 완료: %s", spec.id)
        except Exception as exc:
            job.status = ModelStatus.ERROR
            job.error_message = str(exc)
            job.completed_at = datetime.now()
            logger.exception("STT 모델 다운로드 실패: %s", spec.id)

    async def _hf_download(
        self, spec: STTModelSpec, job: DownloadJob
    ) -> None:
        """HuggingFace snapshot_download 를 별도 스레드에서 실행한다.

        needs_quantization=True 모델은 양자화 입력으로 사용될 임시 디렉토리
        (_get_source_dir) 에 저장하고, False 모델은 HF 캐시를 그대로 사용한다.
        """
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise RuntimeError(
                "huggingface_hub 이 설치되어 있지 않습니다"
            ) from exc

        if spec.needs_quantization:
            # 양자화 원본은 stt_models/<id>_hf_src/ 에 저장
            target_dir = self._get_source_dir(spec)
            target_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                snapshot_download,
                repo_id=spec.hf_source,
                local_dir=str(target_dir),
            )
        else:
            # komixv2 류: HF 캐시에만 받아두면 충분
            await asyncio.to_thread(
                snapshot_download,
                repo_id=spec.hf_source,
            )

    async def _quantize(
        self, spec: STTModelSpec, job: DownloadJob
    ) -> None:
        """mlx-examples/whisper/convert.py 로 4bit 양자화 수행."""
        convert_script = self._mlx_examples / "convert.py"
        if not convert_script.exists():
            raise RuntimeError(
                f"양자화 스크립트를 찾을 수 없습니다: {convert_script}"
            )

        source_dir = self._get_source_dir(spec)
        output_dir = Path(spec.model_path).expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            sys.executable,
            str(convert_script),
            "--torch-name-or-path",
            str(source_dir),
            "--mlx-path",
            str(output_dir),
            "-q",
            "--q-bits",
            "4",
            "--q-group-size",
            "64",
        ]
        logger.info("양자화 실행: %s", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            tail = stderr.decode(errors="replace")[-500:]
            raise RuntimeError(f"양자화 실패 (rc={proc.returncode}): {tail}")

        # 심볼릭 링크: weights.safetensors → model.safetensors
        model_file = output_dir / "model.safetensors"
        weights_link = output_dir / "weights.safetensors"
        if model_file.exists() and not weights_link.exists():
            try:
                weights_link.symlink_to("model.safetensors")
                logger.info("심볼릭 링크 생성: %s", weights_link)
            except OSError as exc:
                logger.warning(
                    "심볼릭 링크 생성 실패 (%s): %s — 복사로 대체",
                    weights_link,
                    exc,
                )
                shutil.copy2(model_file, weights_link)

    def _verify(self, spec: STTModelSpec) -> bool:
        """모델 경로에 weights.safetensors + config.json 이 있는지 확인한다."""
        if not spec.needs_quantization:
            # HF 캐시 기반 모델은 상태 모듈에 위임
            from .stt_model_status import get_model_status

            return get_model_status(spec) == ModelStatus.READY

        path = Path(spec.model_path).expanduser()
        return (
            path.exists()
            and (path / "weights.safetensors").exists()
            and (path / "config.json").exists()
        )

    def _get_source_dir(self, spec: STTModelSpec) -> Path:
        """양자화 원본 스냅샷이 저장되는 임시 디렉토리 경로."""
        return self._models_dir / f"{spec.id}_hf_src"
