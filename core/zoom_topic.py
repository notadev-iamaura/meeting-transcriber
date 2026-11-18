"""Zoom이 직접 표시한 회의 주제를 정확히 같은 녹음 파일에 결합한다."""

from __future__ import annotations

import json
import logging
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from config import AppConfig
from core.io_utils import atomic_write_json_pinned, read_text_no_follow
from core.search_revision import checkpoint_dir

logger = logging.getLogger(__name__)


def normalize_zoom_topic(value: object) -> str:
    """일반 창 제목과 표시할 수 없는 문자열을 제외하고 Zoom 주제를 돌려준다."""
    if not isinstance(value, str) or len(value) > 200:
        return ""
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        return ""
    title = value.strip()
    if title.casefold() in {
        "zoom",
        "zoom workplace",
        "zoom meeting",
        "zoom 회의",
        "회의",
        "meeting",
    }:
        return ""
    return title


@dataclass
class ZoomTopicObservation:
    """하나의 자동 녹음에서 고유하게 관측한 주제만 유지한다."""

    title: str = ""
    ambiguous: bool = False

    def observe(self, candidate: object, *, ambiguous: bool = False) -> None:
        """다른 회의나 상충하는 주제를 한 번이라도 보면 제목 사용을 중단한다."""
        title = normalize_zoom_topic(candidate)
        if self.ambiguous or ambiguous or (self.title and title and self.title != title):
            self.ambiguous = True
            self.title = ""
        elif title:
            self.title = title


def _topic_path(config: AppConfig, destination: Path, identity: tuple[int, int, int, int]) -> Path:
    """같은 이름으로 들어온 다른 파일과 주제 기록을 분리한다."""
    dev, ino, _, _ = identity
    return checkpoint_dir(config, destination.stem) / f"zoom_topic_{dev:x}_{ino:x}.json"


def save_recording_topic(
    config: AppConfig, destination: Path, identity: os.stat_result, title: str
) -> None:
    """파일 공개 전에 같은 inode에만 적용될 주제를 기록한다. 녹음은 항상 우선한다."""
    title = normalize_zoom_topic(title)
    if not title:
        return
    fingerprint = (identity.st_dev, identity.st_ino, identity.st_size, identity.st_mtime_ns)
    try:
        atomic_write_json_pinned(
            _topic_path(config, destination, fingerprint),
            {"filename": destination.name, "identity": fingerprint, "topic": title},
        )
    except (OSError, ValueError):
        logger.warning("Zoom 회의명을 보관할 수 없어 녹음 파일만 저장합니다.")


def read_recording_topic(
    config: AppConfig, destination: Path, identity: tuple[int, int, int, int]
) -> str:
    """파일 등록 직전 생성 파일과 실제 수신 파일의 이름·identity를 대조한다."""
    try:
        row = json.loads(read_text_no_follow(_topic_path(config, destination, identity)))
        current = destination.stat(follow_symlinks=False)
    except (OSError, ValueError):
        return ""
    if not isinstance(row, dict) or row.get("filename") != destination.name:
        return ""
    if row.get("identity") != list(identity) or identity != (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    ):
        return ""
    return normalize_zoom_topic(row.get("topic"))
