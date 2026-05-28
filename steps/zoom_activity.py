"""
Zoom 오디오 활동 감지 모듈.

CoreAudio process list를 우선 사용해 Zoom 계열 프로세스가 실제 오디오 I/O를
실행 중인지 확인한다. CoreAudio helper를 사용할 수 없으면 기존 pgrep 기반
프로세스 감지로 폴백한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


class ZoomActivityError(Exception):
    """Zoom 활동 감지 중 에러가 발생했을 때."""


@dataclass(frozen=True)
class ZoomActivityResult:
    """Zoom 활동 감지 결과."""

    active: bool
    source: str
    process_count: int = 0


class ZoomAudioActivityChecker:
    """Zoom 회의 오디오 활동 여부를 확인한다."""

    def __init__(
        self,
        process_name: str,
        *,
        prefer_coreaudio: bool = True,
        strict_process_errors: bool = False,
        helper_source: Path | None = None,
        helper_binary: Path | None = None,
    ) -> None:
        self._process_name = process_name
        self._prefer_coreaudio = prefer_coreaudio
        self._strict_process_errors = strict_process_errors
        repo_root = Path(__file__).resolve().parent.parent
        self._helper_source = helper_source or repo_root / "scripts" / "zoom_audio_activity.swift"
        self._helper_binary = helper_binary or Path(
            "/private/tmp/meeting-transcriber-zoom-audio-activity"
        )
        self._compile_lock = asyncio.Lock()
        self._coreaudio_warning_logged = False

    async def is_active(self) -> bool:
        """Zoom이 실제 오디오 활동 중이면 True를 반환한다."""
        result = await self.check()
        return result.active

    async def check(self) -> ZoomActivityResult:
        """Zoom 오디오 활동을 확인하고 감지 소스를 함께 반환한다."""
        if not self._prefer_coreaudio:
            return await self._check_process_fallback()

        try:
            return await self._check_coreaudio()
        except ZoomActivityError as e:
            if not self._coreaudio_warning_logged:
                logger.warning(f"CoreAudio Zoom 활동 확인 실패. pgrep 폴백 사용: {e}")
                self._coreaudio_warning_logged = True
            return await self._check_process_fallback()

    async def _check_coreaudio(self) -> ZoomActivityResult:
        """CoreAudio helper를 실행해 Zoom 오디오 I/O 상태를 확인한다."""
        await self._ensure_helper_compiled()

        try:
            proc = await asyncio.create_subprocess_exec(
                str(self._helper_binary),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        except TimeoutError as e:
            raise ZoomActivityError("CoreAudio helper 실행 타임아웃") from e
        except OSError as e:
            raise ZoomActivityError(f"CoreAudio helper 실행 실패: {e}") from e

        if proc.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise ZoomActivityError(message or f"CoreAudio helper 종료 코드 {proc.returncode}")

        try:
            payload = json.loads(stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            raise ZoomActivityError("CoreAudio helper JSON 파싱 실패") from e

        if not isinstance(payload, dict) or not payload.get("ok", False):
            raise ZoomActivityError(str(payload.get("error", "CoreAudio helper 실패")))

        processes = payload.get("processes", [])
        process_count = len(processes) if isinstance(processes, list) else 0
        return ZoomActivityResult(
            active=bool(payload.get("active", False)),
            source="coreaudio",
            process_count=process_count,
        )

    async def _ensure_helper_compiled(self) -> None:
        """Swift helper 바이너리가 없거나 오래되었으면 컴파일한다."""
        if not self._helper_source.exists():
            raise ZoomActivityError(f"CoreAudio helper 소스가 없습니다: {self._helper_source}")

        async with self._compile_lock:
            if self._helper_binary.exists():
                source_mtime = self._helper_source.stat().st_mtime
                binary_mtime = self._helper_binary.stat().st_mtime
                if binary_mtime >= source_mtime:
                    return

            self._helper_binary.parent.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ)
            env.setdefault(
                "CLANG_MODULE_CACHE_PATH",
                "/private/tmp/meeting-transcriber-clang-module-cache",
            )

            try:
                proc = await asyncio.create_subprocess_exec(
                    "swiftc",
                    str(self._helper_source),
                    "-o",
                    str(self._helper_binary),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            except TimeoutError as e:
                raise ZoomActivityError("CoreAudio helper 컴파일 타임아웃") from e
            except FileNotFoundError as e:
                raise ZoomActivityError("swiftc 명령을 찾을 수 없습니다") from e
            except OSError as e:
                raise ZoomActivityError(f"CoreAudio helper 컴파일 실패: {e}") from e

            if proc.returncode != 0:
                message = stderr.decode("utf-8", errors="replace").strip()
                raise ZoomActivityError(message or f"swiftc 종료 코드 {proc.returncode}")

    async def _check_process_fallback(self) -> ZoomActivityResult:
        """기존 pgrep 기반 감지로 폴백한다."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "pgrep",
                "-f",
                self._process_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            returncode = await asyncio.wait_for(proc.wait(), timeout=10.0)
            return ZoomActivityResult(active=returncode == 0, source="pgrep")
        except TimeoutError:
            logger.warning("pgrep Zoom 폴백 확인 타임아웃. 안전하게 active 로 간주합니다.")
            return ZoomActivityResult(active=True, source="pgrep-timeout")
        except FileNotFoundError as e:
            if self._strict_process_errors:
                raise ZoomActivityError("pgrep 명령을 찾을 수 없습니다") from e
            logger.warning("pgrep 명령을 찾을 수 없어 Zoom 폴백 감지를 비활성으로 처리합니다.")
            return ZoomActivityResult(active=False, source="pgrep-missing")
        except OSError as e:
            if self._strict_process_errors:
                raise ZoomActivityError(f"프로세스 확인 중 OS 에러 발생: {e}") from e
            logger.warning(f"pgrep Zoom 폴백 확인 실패. 안전하게 active 로 간주합니다: {e}")
            return ZoomActivityResult(active=True, source="pgrep-error")
