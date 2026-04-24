"""
단일 인스턴스 락 테스트 모듈

목적: core/single_instance.py 의 SingleInstanceLock 동작을 검증한다.
주요 테스트:
    - 첫 획득 성공
    - 동일 프로세스 이중 획득 시 에러 없음
    - 다른 파일 디스크립터로 두 번째 획득 시 AlreadyRunningError
    - release 후 재획득 가능
    - PID 파일 내용 기록 확인
의존성: pytest, 표준 라이브러리
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.single_instance import AlreadyRunningError, SingleInstanceLock


class TestSingleInstanceLock:
    """SingleInstanceLock 기본 동작 테스트."""

    def test_첫_획득_성공(self, tmp_path: Path) -> None:
        """빈 디렉토리에서 첫 락 획득은 성공해야 한다."""
        lock = SingleInstanceLock(tmp_path)
        lock.acquire()
        try:
            assert lock.lock_path.exists()
        finally:
            lock.release()

    def test_동일_인스턴스_중복_acquire_무해(self, tmp_path: Path) -> None:
        """같은 인스턴스로 acquire 를 두 번 호출해도 에러가 없어야 한다."""
        lock = SingleInstanceLock(tmp_path)
        lock.acquire()
        try:
            lock.acquire()  # 두 번째 호출은 조용히 통과
        finally:
            lock.release()

    def test_다른_인스턴스_두번째_획득_실패(self, tmp_path: Path) -> None:
        """같은 경로에 두 번째 인스턴스가 접근하면 AlreadyRunningError."""
        first = SingleInstanceLock(tmp_path)
        first.acquire()
        try:
            second = SingleInstanceLock(tmp_path)
            with pytest.raises(AlreadyRunningError) as ei:
                second.acquire()
            # 에러 메시지에 PID 포함 확인
            assert ei.value.pid == os.getpid()
            assert ei.value.lock_path == first.lock_path
        finally:
            first.release()

    def test_release_후_재획득_가능(self, tmp_path: Path) -> None:
        """release 후 새 인스턴스가 락을 다시 잡을 수 있어야 한다."""
        first = SingleInstanceLock(tmp_path)
        first.acquire()
        first.release()

        second = SingleInstanceLock(tmp_path)
        second.acquire()
        try:
            assert second.lock_path.exists()
        finally:
            second.release()

    def test_PID_파일_내용(self, tmp_path: Path) -> None:
        """락 파일에 현재 프로세스 PID 가 첫 줄에 기록되어야 한다."""
        lock = SingleInstanceLock(tmp_path)
        lock.acquire()
        try:
            content = lock.lock_path.read_text(encoding="utf-8")
            first_line = content.split("\n", 1)[0].strip()
            assert first_line == str(os.getpid())
        finally:
            lock.release()

    def test_컨텍스트_매니저(self, tmp_path: Path) -> None:
        """with 블록 내에서 락 획득/해제가 정상 동작해야 한다."""
        with SingleInstanceLock(tmp_path) as lock:
            assert lock.lock_path.exists()
            # 블록 내에서는 다른 인스턴스가 접근 불가
            another = SingleInstanceLock(tmp_path)
            with pytest.raises(AlreadyRunningError):
                another.acquire()
        # 블록 종료 후에는 재획득 가능
        retry = SingleInstanceLock(tmp_path)
        retry.acquire()
        retry.release()

    def test_존재하지_않는_디렉토리_자동_생성(self, tmp_path: Path) -> None:
        """base_dir 가 없어도 자동으로 생성되어 획득에 성공해야 한다."""
        nested = tmp_path / "deep" / "nested" / "dir"
        assert not nested.exists()
        lock = SingleInstanceLock(nested)
        lock.acquire()
        try:
            assert nested.exists()
            assert lock.lock_path.exists()
        finally:
            lock.release()
