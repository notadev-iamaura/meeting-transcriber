"""
단일 인스턴스 락 모듈 (Single Instance Lock Module)

목적: 프로세스 간 MLX Metal 충돌을 방지하기 위해 한 번에 한 앱 인스턴스만 실행되도록 강제한다.
     asyncio.Lock 은 같은 프로세스 내에서만 유효하므로, 사용자가 실수로 앱을
     두 번 실행하면 두 프로세스가 동일 Metal 장치를 공유하여 이슈 H 의 크래시가
     다시 발생할 수 있다. 본 모듈은 base_dir 하위에 PID 파일을 두고 fcntl 로
     advisory lock 을 걸어 중복 실행을 차단한다.

주요 기능:
    - 부팅 시 `acquire()` 로 단일 인스턴스 확보 (실패 시 AlreadyRunningError)
    - PID 파일에 현재 프로세스 PID + 시작 시각 기록
    - 비정상 종료 후 재시작해도 fcntl 이 자동 해제되어 바로 획득 가능
    - 프로세스 종료 시 자동 해제 (컨텍스트 매니저로 사용 권장)

플랫폼:
    - macOS, Linux: fcntl.LOCK_EX | LOCK_NB
    - Windows: 미지원 (이 프로젝트는 macOS 전용)

의존성: 표준 라이브러리만 사용 (fcntl, os, pathlib)
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import IO

logger = logging.getLogger(__name__)


class AlreadyRunningError(RuntimeError):
    """이미 다른 인스턴스가 실행 중일 때 발생한다.

    Attributes:
        pid: 이미 실행 중인 프로세스의 PID (읽을 수 있는 경우)
        lock_path: 확인한 락 파일 경로
    """

    def __init__(self, pid: int | None, lock_path: Path) -> None:
        self.pid = pid
        self.lock_path = lock_path
        detail = f"PID={pid}" if pid else "PID 미상"
        super().__init__(
            f"이미 다른 Meeting Transcriber 인스턴스가 실행 중입니다 ({detail}). "
            f"락 파일: {lock_path}"
        )


class SingleInstanceLock:
    """단일 인스턴스 advisory lock.

    사용 예시:
        lock = SingleInstanceLock(base_dir)
        lock.acquire()  # 실패 시 AlreadyRunningError
        try:
            run_app()
        finally:
            lock.release()

    또는 컨텍스트 매니저:
        with SingleInstanceLock(base_dir):
            run_app()

    주의:
        - 같은 프로세스에서 acquire() 를 두 번 호출하면 두 번째는 이미 획득된 상태로 간주한다.
        - 프로세스 kill -9 등 비정상 종료 시 OS 가 fcntl 을 해제하므로 재실행 가능.
        - 파일 자체는 남아있지만 락이 풀려있으면 재획득 시 내용을 덮어쓴다.
    """

    LOCK_FILE_NAME = ".meeting-transcriber.pid"

    def __init__(self, base_dir: Path) -> None:
        """락 초기화.

        Args:
            base_dir: 앱 데이터 디렉토리 (보통 ~/.meeting-transcriber). 없으면 생성.
        """
        self._base_dir = base_dir
        self._lock_path = base_dir / self.LOCK_FILE_NAME
        self._fp: IO[str] | None = None

    @property
    def lock_path(self) -> Path:
        """락 파일 경로."""
        return self._lock_path

    def acquire(self) -> None:
        """락을 획득한다.

        이미 다른 프로세스가 점유 중이면 AlreadyRunningError 를 발생시킨다.
        같은 프로세스에서 중복 호출 시 조용히 성공 처리한다.

        Raises:
            AlreadyRunningError: 다른 인스턴스가 실행 중일 때
            OSError: 디렉토리 생성이나 파일 열기 실패 시
        """
        if self._fp is not None:
            logger.debug("SingleInstanceLock: 이미 획득된 상태")
            return

        self._base_dir.mkdir(parents=True, exist_ok=True)

        # r+ 모드로 열되 없으면 생성 (파일 내용을 덮어쓰지 않도록 a+ 대신 수동 처리)
        try:
            fp = open(self._lock_path, "a+", encoding="utf-8")
        except OSError:
            logger.exception(f"락 파일 열기 실패: {self._lock_path}")
            raise

        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            # EAGAIN / EWOULDBLOCK: 다른 프로세스가 보유 중
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                other_pid = self._read_other_pid(fp)
                fp.close()
                raise AlreadyRunningError(other_pid, self._lock_path) from e
            fp.close()
            raise

        # 획득 성공 → PID + 시작 시각 기록
        try:
            fp.seek(0)
            fp.truncate()
            fp.write(f"{os.getpid()}\n{datetime.now().isoformat()}\n")
            fp.flush()
            os.fsync(fp.fileno())
        except OSError:
            # 파일 쓰기 실패는 락 자체를 무효화하지 않음 (로그만)
            logger.warning("PID 파일 기록 실패 (락은 유지)", exc_info=True)

        self._fp = fp
        logger.info(f"단일 인스턴스 락 획득: pid={os.getpid()}, path={self._lock_path}")

    def release(self) -> None:
        """락을 해제한다.

        이미 해제된 상태면 조용히 통과한다.
        """
        if self._fp is None:
            return
        try:
            fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
        except OSError:
            logger.warning("락 해제 실패 (프로세스 종료 시 OS 가 자동 회수)", exc_info=True)
        finally:
            try:
                self._fp.close()
            except OSError:
                pass
            self._fp = None
            logger.info("단일 인스턴스 락 해제")

    @staticmethod
    def _read_other_pid(fp: IO[str]) -> int | None:
        """락을 못 얻었을 때 기존 보유자의 PID 를 추정해 반환한다.

        파일 내용이 없거나 형식이 맞지 않으면 None.
        """
        try:
            fp.seek(0)
            first_line = fp.readline().strip()
            if first_line.isdigit():
                return int(first_line)
        except OSError:
            return None
        return None

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.release()
