"""STT 모델 다운로더 모듈

목적: HuggingFace Hub 에서 사전 양자화된 STT 모델을 다운로드한다.
동시 1개 다운로드만 허용하며, 백그라운드 asyncio 태스크로 비동기 실행된다.

모든 지원 모델은 이미 HuggingFace 에 사전 양자화된 4bit 형태로 배포되므로
로컬 양자화 단계가 없다. mlx-examples/whisper/convert.py 같은 외부 스크립트
의존성이 필요하지 않다.

주요 기능:
    - DownloadConflictError: 중복 다운로드 충돌 예외
    - DownloadJob: 진행 상태 dataclass
    - STTModelDownloader: 다운로드/검증 오케스트레이션

의존성:
    - huggingface_hub.snapshot_download (런타임에 import — 테스트 시 mock)
    - core/stt_model_registry, core/stt_model_status
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .stt_model_registry import STTModelSpec, get_by_id
from .stt_model_status import ModelStatus, get_model_status

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
    """STT 모델 다운로드 매니저.

    특징:
        - 동시 1개 다운로드만 허용 (asyncio.Lock 으로 동시성 제어)
        - start_download() 는 job_id만 반환하고 실제 작업은 백그라운드 태스크
        - get_progress() 로 폴링
        - wait_for() 로 테스트/검증 시 완료 대기 가능
    """

    def __init__(self, models_dir: Path) -> None:
        """다운로더 초기화.

        Args:
            models_dir: 로컬 임시 디렉토리 (현재는 사용되지 않지만, 향후 수동 임포트
                등의 기능 확장을 위해 유지). 존재하지 않으면 자동 생성한다.
        """
        self._models_dir = Path(models_dir).expanduser()
        self._models_dir.mkdir(parents=True, exist_ok=True)
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
                if existing.status == ModelStatus.DOWNLOADING:
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
        """HF 스냅샷 다운로드 → 검증 파이프라인을 순차 실행한다."""
        try:
            # 1단계: HF 스냅샷 다운로드
            job.status = ModelStatus.DOWNLOADING
            job.current_step = "downloading"
            job.progress_percent = 10
            await self._hf_download(spec, job)
            job.progress_percent = 90

            # 2단계: 검증
            job.current_step = "verifying"
            job.progress_percent = 95
            if not self._verify(spec):
                raise RuntimeError("모델 검증 실패: HF 캐시에 파일 없음")

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

        모든 지원 모델은 사전 양자화된 HF repo 를 가리키므로 HF 캐시에
        바로 저장하면 된다. 별도의 로컬 작업 디렉토리가 필요하지 않다.
        """
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise RuntimeError(
                "huggingface_hub 이 설치되어 있지 않습니다"
            ) from exc

        await asyncio.to_thread(
            snapshot_download,
            repo_id=spec.hf_source,
        )

    def _verify(self, spec: STTModelSpec) -> bool:
        """다운로드 직후 모델이 READY 상태인지 확인한다."""
        return get_model_status(spec) == ModelStatus.READY
