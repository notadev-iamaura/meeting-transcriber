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

from .stt_model_registry import (
    STTModelSpec,
    get_by_id,
    get_hf_download_urls,
    get_manual_import_dir,
)
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

    async def start_download(
        self, model_id: str, *, prefer_direct: bool = False
    ) -> str:
        """다운로드를 백그라운드에서 시작하고 job_id를 반환한다.

        Args:
            model_id: STT_MODELS 에 등록된 모델 ID.
            prefer_direct: True면 `huggingface_hub` 를 건너뛰고 HF 직접 URL 로만
                다운로드한다. 사용자가 "URL로 직접 받기" 옵션을 명시적으로 선택했을 때
                사용된다 (기업 프록시·SSL 검사 환경).

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
                self._run_download(spec, job, prefer_direct=prefer_direct)
            )
            logger.info(
                "STT 모델 다운로드 시작: %s (%s, prefer_direct=%s)",
                model_id,
                job_id,
                prefer_direct,
            )

        return job_id

    def get_progress(self, model_id: str) -> Optional[DownloadJob]:
        """현재 진행 중이거나 최근 완료된 작업의 상태를 반환한다."""
        return self._jobs.get(model_id)

    def clear_job(self, model_id: str) -> bool:
        """특정 모델의 완료/에러 작업 상태를 in-memory 에서 제거한다.

        사용 시나리오:
            1. 자동 다운로드가 SSL/네트워크 오류로 실패 → job.status = ERROR
            2. 사용자가 수동 다운로드 + 가져오기(import) 로 파일 배치 → 디스크는 READY
            3. 그러나 downloader 의 in-memory job 이 여전히 ERROR 를 반환 →
               /api/stt-models 응답이 runtime_status(ERROR) 로 표시됨
            4. 이 메서드로 stale 한 에러 job 을 지우면 API 가 disk_status(READY)
               를 반환하게 된다.

        진행 중(DOWNLOADING)인 작업은 안전하게 제거할 수 없으므로 false 를 반환.

        Args:
            model_id: 제거할 작업의 모델 ID

        Returns:
            True: 제거 성공 (또는 해당 job 이 원래 없었음)
            False: 진행 중이라 제거하지 않음
        """
        job = self._jobs.get(model_id)
        if job is not None and job.status == ModelStatus.DOWNLOADING:
            logger.warning(
                "clear_job 거부: 진행 중인 작업 (%s, status=%s)",
                model_id,
                job.status,
            )
            return False

        self._jobs.pop(model_id, None)
        # 관련 태스크 참조도 정리 (태스크는 이미 완료된 상태여야 함)
        task = self._tasks.pop(model_id, None)
        if task is not None and not task.done():
            logger.warning(
                "clear_job: 아직 완료되지 않은 태스크 발견 (%s)", model_id
            )
        logger.info("STT 다운로드 작업 상태 초기화: %s", model_id)
        return True

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
        self,
        spec: STTModelSpec,
        job: DownloadJob,
        *,
        prefer_direct: bool = False,
    ) -> None:
        """다운로드 → 검증 파이프라인을 실행한다.

        기본적으로 `huggingface_hub` 를 먼저 시도하고, 네트워크·SSL·인증 오류 등으로
        실패하면 자동으로 HF 직접 URL 을 이용한 `urllib` 스트리밍 다운로드로
        폴백한다. 두 방법 모두 실패하면 ERROR 상태로 종료한다.

        Args:
            spec: 다운로드할 모델 spec
            job: 진행 상태가 갱신될 DownloadJob (in-place mutation)
            prefer_direct: True면 `huggingface_hub` 를 건너뛰고 곧바로 직접 URL
                다운로드를 시도한다. 사용자가 "URL로 직접 받기" 를 명시적으로
                선택했을 때 사용된다.
        """
        job.status = ModelStatus.DOWNLOADING
        job.progress_percent = 5
        hf_error: Exception | None = None

        if not prefer_direct:
            try:
                job.current_step = "downloading"
                job.progress_percent = 10
                await self._hf_download(spec, job)
                job.progress_percent = 90
                if self._verify(spec):
                    job.status = ModelStatus.READY
                    job.progress_percent = 100
                    job.completed_at = datetime.now()
                    logger.info("STT 모델 다운로드 완료 (huggingface_hub): %s", spec.id)
                    return
                # 다운로드는 성공했으나 검증 실패 → direct 폴백 시도
                logger.warning(
                    "huggingface_hub 다운로드 후 검증 실패, direct URL 폴백: %s",
                    spec.id,
                )
            except Exception as exc:
                hf_error = exc
                logger.warning(
                    "huggingface_hub 다운로드 실패, direct URL 폴백 시도: %s — %s",
                    spec.id,
                    exc,
                )

        # 직접 URL 다운로드 (폴백 또는 명시적 선택)
        try:
            job.current_step = "downloading_direct"
            job.progress_percent = 10
            await self._direct_url_download(spec, job)
            job.current_step = "verifying"
            job.progress_percent = 95
            if not self._verify(spec):
                raise RuntimeError(
                    "모델 검증 실패: 다운로드된 파일이 올바르지 않아요"
                )
            job.status = ModelStatus.READY
            job.progress_percent = 100
            job.completed_at = datetime.now()
            method = "direct URL" if prefer_direct else "direct URL 폴백"
            logger.info("STT 모델 다운로드 완료 (%s): %s", method, spec.id)
        except Exception as exc:
            job.status = ModelStatus.ERROR
            # 두 방법 모두 실패했을 때 원인을 명확히 제공
            if hf_error is not None:
                job.error_message = (
                    f"huggingface_hub 실패: {hf_error} / "
                    f"직접 URL 다운로드도 실패: {exc}"
                )
            else:
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

    async def _direct_url_download(
        self, spec: STTModelSpec, job: DownloadJob
    ) -> None:
        """HF 직접 URL 을 이용해 파일을 스트리밍 다운로드한다.

        `huggingface_hub` 가 실패하는 환경(기업 프록시, MITM SSL 검사, ISP 필터링
        등)에서 대체 경로로 사용된다. `urllib.request` 만 사용하므로 추가
        의존성이 없다.

        각 파일은 temp 파일(`.tmp`)로 먼저 받은 뒤 `os.replace` 로 원자적 이동하며,
        저장 위치는 `get_manual_import_dir(spec)` 이다. 이렇게 하면
        `get_model_status` 가 수동 임포트 경로에서 READY 를 감지하고, 활성화 시
        `get_effective_model_path` 가 이 경로를 우선 사용한다.

        Args:
            spec: STTModelSpec
            job: 진행률 업데이트 대상 DownloadJob (10% ~ 90% 범위)

        Raises:
            RuntimeError: 네트워크 오류, HTTP 에러, 디스크 쓰기 실패 등
        """
        import os
        import urllib.error
        import urllib.request
        from pathlib import Path

        urls = get_hf_download_urls(spec)
        if not urls:
            raise RuntimeError(
                f"직접 다운로드 URL 을 찾을 수 없어요: {spec.id}"
            )

        target_dir = Path(get_manual_import_dir(spec))
        target_dir.mkdir(parents=True, exist_ok=True)

        # 진행률 범위를 파일 개수에 맞춰 할당 (10% → 90%)
        total_span = 80
        per_file = total_span // len(urls)

        def _download_one(url: str, dest: Path, base_percent: int) -> None:
            """단일 파일을 urllib 로 스트리밍 다운로드한다 (동기, 스레드에서 호출)."""
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            # 기존 임시 파일 정리
            tmp.unlink(missing_ok=True)

            req = urllib.request.Request(
                url,
                headers={
                    # 일부 CDN 은 User-Agent 없는 요청을 차단한다
                    "User-Agent": "meeting-transcriber/1.0 (+https://github.com/youngouk/meeting-transcriber)",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    # 리다이렉트는 urllib 가 자동 처리 (HF → CDN)
                    total = int(resp.headers.get("Content-Length", "0") or "0")
                    downloaded = 0
                    chunk = 1024 * 256  # 256KB
                    with open(tmp, "wb") as out:
                        while True:
                            buf = resp.read(chunk)
                            if not buf:
                                break
                            out.write(buf)
                            downloaded += len(buf)
                            if total > 0:
                                file_pct = downloaded / total
                                job.progress_percent = min(
                                    89,
                                    base_percent + int(file_pct * per_file),
                                )
            except urllib.error.HTTPError as exc:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(
                    f"HTTP {exc.code} {exc.reason}: {url}"
                ) from exc
            except urllib.error.URLError as exc:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"네트워크 오류: {exc.reason}") from exc
            except OSError as exc:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"디스크 쓰기 실패: {exc}") from exc

            # 원자적 이동
            os.replace(str(tmp), str(dest))

        for idx, file_info in enumerate(urls):
            base_percent = 10 + (idx * per_file)
            dest = target_dir / file_info["name"]
            logger.info(
                "direct URL 다운로드: %s → %s", file_info["url"], dest
            )
            await asyncio.to_thread(
                _download_one, file_info["url"], dest, base_percent
            )

    def _verify(self, spec: STTModelSpec) -> bool:
        """다운로드 직후 모델이 READY 상태인지 확인한다."""
        return get_model_status(spec) == ModelStatus.READY
