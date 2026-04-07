"""
원자적 파일 I/O 유틸리티 모듈 (Atomic File I/O Utilities)

목적: 파일 쓰기 도중 프로세스가 죽거나 디스크가 가득 차도 기존 파일이
손상되지 않도록 보장하는 공용 헬퍼를 제공한다.

전략:
    1. 같은 디렉토리에 임시 파일(`{name}.tmp.{pid}.{rand}`) 생성
    2. 내용 쓰기 → flush → fsync (디스크에 강제 동기)
    3. `os.replace()` 로 원자적 교체 (POSIX 보장)
    4. (선택) 기존 파일을 `.bak` 로 백업

이 모듈을 만들기 전에는 같은 패턴이 `api/routes.py::_atomic_write_text` 와
`core/user_settings.py::_atomic_write_json` 두 곳에 별도 구현되어 있었고,
`api/routes.py::update_settings` / `activate_stt_model` 의 `config.yaml` 쓰기는
원시 `open("w")` 를 사용해 손상 위험이 있었다. 이 모듈은 그 세 가지 경로를
모두 통합한다.

의존성: 표준 라이브러리만 사용 (os, shutil, tempfile, json, pathlib).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def atomic_write_text(
    path: Path,
    content: str,
    *,
    backup: bool = True,
) -> None:
    """텍스트 파일을 원자적으로 덮어쓴다.

    부모 디렉토리가 없으면 생성한다. `backup=True` 이면 기존 파일을 같은
    디렉토리의 `{name}.bak` 로 복사한 뒤 새 내용을 쓴다. 임시 파일은 같은
    디렉토리에 만들어 같은 파일시스템에서 `os.replace()` 가 원자적임을 보장한다.

    Args:
        path: 최종 대상 파일 경로 (절대 경로 권장).
        content: 새로 쓸 텍스트.
        backup: True 이면 `.bak` 백업 생성.

    Raises:
        OSError: 디스크 쓰기 실패 (권한, 디스크 풀 등).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if backup and path.exists():
        backup_path = path.with_suffix(path.suffix + ".bak")
        try:
            shutil.copy2(path, backup_path)
        except OSError as exc:
            logger.warning(f"백업 생성 실패 (진행 계속): {exc}")

    # delete=False 로 NamedTemporaryFile 을 만들고 즉시 tmp_name 캡처.
    # 이렇게 해야 write/flush/fsync 어디서 실패해도 finally 가 정리할 수 있다.
    tf = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    tmp_name: str | None = tf.name
    try:
        try:
            tf.write(content)
            tf.flush()
            os.fsync(tf.fileno())
        finally:
            tf.close()
        os.replace(tmp_name, path)
        tmp_name = None  # 성공 — finally 에서 unlink 안 함
    finally:
        if tmp_name is not None:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    backup: bool = True,
    indent: int = 2,
) -> None:
    """JSON 데이터를 원자적으로 덮어쓴다.

    `atomic_write_text` 의 thin wrapper. 직렬화 옵션은 한국어 보존을 위해
    `ensure_ascii=False` 고정.

    Args:
        path: 최종 대상 파일 경로.
        data: JSON 으로 직렬화 가능한 객체.
        backup: True 이면 `.bak` 백업 생성.
        indent: JSON pretty-print 들여쓰기.

    Raises:
        OSError: 디스크 쓰기 실패.
        TypeError: data 가 JSON 직렬화 불가능.
    """
    content = json.dumps(data, ensure_ascii=False, indent=indent)
    atomic_write_text(path, content, backup=backup)
