"""
Quarantine 디렉토리 관리

목적: 품질 불량·사용자 삭제 오디오 파일을 입력 감시 폴더 바깥의
     격리실로 이동하여 watcher 재감지를 차단한다.

근거: 2026-04-21 DELETE /api/meetings/{id}가 DB만 삭제하여 오디오 파일이
     잔존 → watcher가 재등록 → 동일 크래시 반복. 이 헬퍼가 이동까지 담당.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class QuarantineError(Exception):
    """Quarantine 이동 실패."""


def move_to_quarantine(
    src_path: Path,
    quarantine_dir: Path,
    *,
    reason: str,
) -> Path:
    """오디오 파일을 격리 디렉토리로 이동한다.

    Args:
        src_path: 이동할 원본 파일 경로
        quarantine_dir: 격리 디렉토리 (없으면 생성)
        reason: 이동 사유 (로깅용)

    Returns:
        이동된 파일의 새 경로

    Raises:
        QuarantineError: 원본 파일이 없거나 이동 실패
    """
    if not src_path.exists():
        raise QuarantineError(f"원본 파일이 없습니다: {src_path}")

    quarantine_dir.mkdir(parents=True, exist_ok=True)
    dest = quarantine_dir / src_path.name

    if dest.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = quarantine_dir / f"{src_path.stem}_{timestamp}{src_path.suffix}"

    try:
        shutil.move(str(src_path), str(dest))
    except OSError as e:
        raise QuarantineError(f"이동 실패: {src_path} → {dest}: {e}") from e

    logger.info(f"Quarantine 이동: {src_path.name} → {dest} (사유: {reason})")
    return dest
