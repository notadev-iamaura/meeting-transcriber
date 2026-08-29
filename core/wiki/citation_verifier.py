"""Wiki citation verifier — D2 인용 실재성 실제 검증.

목적: Phase 3 의 `_NullVerifier` (steps/wiki_compiler.py) 를 교체한다.
LLM 이 출력한 [meeting:{id}@HH:MM:SS] 인용이 실제 회의의 utterances 시간대에
존재하는 발화를 가리키는지 timestamp 매칭으로 확인한다.

PRD §6 D2 핵심 요구사항:
    - meeting_id 가 알려진 회의(이번 ingest 의 회의)인지
    - timestamp ±tolerance(기본 2초) 윈도우 안에 실제 발화가 존재하는지
    - 보수적 정책: 검증 정보가 없는 회의의 인용은 False (phantom 처리)

현재 회의는 메모리의 보정 발화로 즉시 검증한다. 누적 위키에 다시 포함되는 과거
회의 인용은 `checkpoints/{meeting_id}/correct.json`(없을 때만 `merge.json`)을
no-follow로 읽어 검증한다. 체크포인트가 없거나 안전하게 읽을 수 없으면 보수적으로
phantom 처리한다.

의존성:
    - core.wiki.guard.CitationVerifier (Protocol — 만족시킴)
    - corrector.CorrectedUtterance 호환 (duck-typing: speaker/text/start/end)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class Utterance(Protocol):
    """corrector 단계의 발화. duck-typing 계약.

    Attributes:
        speaker: 화자 레이블.
        text:    발화 텍스트.
        start:   발화 시작 (초).
        end:     발화 종료 (초).
    """

    speaker: str
    text: str
    start: float
    end: float


def _read_utterance_field(utterance: Any, field_name: str) -> Any:
    """dict/object 양쪽 utterance 스키마에서 필드 값을 읽는다."""
    if isinstance(utterance, Mapping):
        return utterance.get(field_name)
    return getattr(utterance, field_name)


def _read_utterance_float(utterance: Any, field_name: str) -> float:
    """utterance 필드를 float 로 읽는다."""
    return float(_read_utterance_field(utterance, field_name))


def _utterance_start_sort_key(utterance: Any) -> float:
    """정렬용 start 값을 반환한다. 비정상 발화는 뒤로 보낸다."""
    try:
        return _read_utterance_float(utterance, "start")
    except (TypeError, ValueError, AttributeError):
        return float("inf")


def _checkpoint_root_lexical_path(path: Path) -> Path:
    """체크포인트 root를 symlink 해석 없이 안전한 절대 경로로 정규화한다."""
    raw_path = Path(path).expanduser()
    raw_text = os.fspath(raw_path)
    raw_parts = raw_path.parts[1:] if raw_path.is_absolute() else raw_path.parts
    if "\x00" in raw_text or any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError("안전하지 않은 체크포인트 root 경로입니다.")
    if not raw_path.is_absolute():
        raw_path = Path.cwd() / raw_path
    return Path(os.path.abspath(os.fspath(raw_path)))


def _is_safe_meeting_id(meeting_id: str) -> bool:
    """체크포인트 direct-child 조회에 쓸 meeting_id를 검증한다."""
    return (
        isinstance(meeting_id, str)
        and bool(meeting_id)
        and meeting_id not in {".", ".."}
        and "\x00" not in meeting_id
        and "/" not in meeting_id
        and "\\" not in meeting_id
        and Path(meeting_id).name == meeting_id
    )


def _directory_open_flags() -> int:
    """체크포인트 directory walk용 no-follow open flags를 반환한다."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("O_NOFOLLOW를 지원하지 않아 체크포인트를 안전하게 열 수 없습니다.")
    return int(
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | no_follow
    )


def _file_open_flags() -> int:
    """체크포인트 파일 읽기용 no-follow open flags를 반환한다."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("O_NOFOLLOW를 지원하지 않아 체크포인트를 안전하게 열 수 없습니다.")
    return int(os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | no_follow)


def _open_directory_tree_no_follow(path: Path) -> int:
    """root부터 모든 경로 요소를 openat+O_NOFOLLOW로 열어 directory fd를 반환한다."""
    directory_flags = _directory_open_flags()
    current_fd = os.open(path.anchor, directory_flags)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            try:
                if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                    raise OSError(f"디렉터리가 아닌 체크포인트 경로 요소입니다: {component}")
            except BaseException:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _read_checkpoint_json_no_follow(
    checkpoints_dir: Path,
    meeting_id: str,
    filename: str,
) -> tuple[str, object | None]:
    """안전한 direct-child checkpoint JSON을 읽는다.

    Returns:
        ("ok", payload): 정상 읽기.
        ("missing", None): root/회의/파일이 존재하지 않음.
        ("invalid", None): symlink·비정규 파일·읽기/JSON 오류 등 신뢰 불가 상태.
    """
    root_fd: int | None = None
    meeting_fd: int | None = None
    file_fd: int | None = None
    try:
        try:
            root_fd = _open_directory_tree_no_follow(checkpoints_dir)
        except FileNotFoundError:
            return ("missing", None)
        except OSError as exc:
            logger.warning("D2 과거 체크포인트 root 안전 열기 실패: %r", exc)
            return ("invalid", None)

        try:
            meeting_fd = os.open(meeting_id, _directory_open_flags(), dir_fd=root_fd)
        except FileNotFoundError:
            return ("missing", None)
        except OSError as exc:
            logger.warning("D2 과거 체크포인트 회의 디렉터리 안전 열기 실패: %r", exc)
            return ("invalid", None)

        try:
            file_fd = os.open(filename, _file_open_flags(), dir_fd=meeting_fd)
        except FileNotFoundError:
            return ("missing", None)
        except OSError as exc:
            logger.warning("D2 과거 체크포인트 파일 안전 열기 실패: %r", exc)
            return ("invalid", None)

        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            logger.warning("D2 과거 체크포인트가 일반 파일이 아님")
            return ("invalid", None)

        with os.fdopen(file_fd, "r", encoding="utf-8") as handle:
            file_fd = None
            try:
                return ("ok", json.load(handle))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                logger.warning("D2 과거 체크포인트 JSON 파싱 실패: %r", exc)
                return ("invalid", None)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if meeting_fd is not None:
            os.close(meeting_fd)
        if root_fd is not None:
            os.close(root_fd)


def _checkpoint_utterances(payload: object) -> list[dict[str, Any]] | None:
    """체크포인트 payload의 발화 시간 원장을 엄격하게 검증해 반환한다."""
    if not isinstance(payload, Mapping):
        return None
    raw_utterances = payload.get("utterances")
    if not isinstance(raw_utterances, list):
        return None

    utterances: list[dict[str, Any]] = []
    for raw_utterance in raw_utterances:
        if not isinstance(raw_utterance, Mapping):
            return None
        text = raw_utterance.get("text")
        start_raw = raw_utterance.get("start")
        end_raw = raw_utterance.get("end")
        if (
            not isinstance(text, str)
            or not text.strip()
            or start_raw is None
            or end_raw is None
            or isinstance(start_raw, bool)
            or isinstance(end_raw, bool)
        ):
            return None
        try:
            start = float(start_raw)
            end = float(end_raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            return None
        utterances.append({"text": text, "start": start, "end": end})
    return utterances


# ─────────────────────────────────────────────────────────────────────────
# 1. UtterancesCitationVerifier — 단일 회의 utterances 기반 검증자
# ─────────────────────────────────────────────────────────────────────────


class UtterancesCitationVerifier:
    """utterances 기반 timestamp 매칭 검증.

    `core.wiki.guard.CitationVerifier` Protocol 을 만족하므로
    `WikiGuard(verifier=UtterancesCitationVerifier(...))` 로 즉시 주입 가능.

    Threading: 인스턴스는 immutable 설계. 같은 회의 ingest 동안 단일 코루틴에서
    호출되며, 회의별로 새 인스턴스를 생성한다.

    Attributes:
        _utterances_by_meeting: meeting_id → 정렬된 Utterance 리스트 (start asc).
            보수적 정책상 이 dict 에 키가 없는 meeting_id 의 인용은
            모두 False (phantom 처리) 로 판정.
        _tolerance_seconds: timestamp 허용 오차 (±초).
    """

    def __init__(
        self,
        utterances_by_meeting: dict[str, list[Utterance]],
        tolerance_seconds: int = 2,
    ) -> None:
        """utterances 매핑과 tolerance 를 받아 인덱스를 사전 빌드한다.

        보수적 정책 보장:
            - utterances_by_meeting 에 키가 없는 meeting_id → 항상 False.
            - utterances 가 빈 리스트인 meeting_id → 항상 False.
            - tolerance_seconds < 0 → ValueError.

        Args:
            utterances_by_meeting: meeting_id → Utterance 시퀀스. 생성자가
                start 기준 정렬·인덱스를 빌드한다.
            tolerance_seconds: ±허용 오차 (기본 2). PRD §6 D2.

        Raises:
            ValueError: tolerance_seconds < 0.
        """
        if tolerance_seconds < 0:
            raise ValueError(f"tolerance_seconds 는 0 이상이어야 합니다: {tolerance_seconds}")

        # 외부 변경 영향 차단을 위해 즉시 복사 + 정렬 인덱스 빌드
        self._utterances_by_meeting: dict[str, list[Any]] = {}
        for meeting_id, utts in utterances_by_meeting.items():
            # list() 로 복사 + start 기준 오름차순 정렬
            sorted_utts = sorted(list(utts), key=_utterance_start_sort_key)
            self._utterances_by_meeting[meeting_id] = sorted_utts

        self._tolerance_seconds: int = tolerance_seconds

    async def verify_exists(
        self,
        meeting_id: str,
        timestamp_seconds: int,
    ) -> bool:
        """주어진 (meeting_id, ts) 가 실제 발화에 매핑되는지 검사한다.

        알고리즘:
            1. meeting_id ∈ _utterances_by_meeting 인가? — 아니면 즉시 False.
            2. utterances 가 비어있는가? — 비었으면 False.
            3. ts ± tolerance 윈도우 내 발화 존재 여부 검사:
               (utt.start ≤ ts + tolerance) AND (utt.end ≥ ts - tolerance).

        Args:
            meeting_id: 실제 회의 ID 또는 하위 호환 8자리 hex.
            timestamp_seconds: 인용의 timestamp 를 초 단위로 변환한 정수.

        Returns:
            True: tolerance 윈도우 내 발화 존재.
            False: 알 수 없는 meeting_id, 빈 utterances, 또는 매칭 발화 없음.

        Note:
            절대 예외를 raise 하지 않는다 (WikiGuard.verify 의 graceful 정책 호환).
        """
        # 1. 알려지지 않은 meeting_id — 보수적 phantom
        utts = self._utterances_by_meeting.get(meeting_id)
        if utts is None:
            logger.warning(
                "D2 phantom: meeting_id=%s, ts=%d, reason=unknown_meeting",
                meeting_id,
                timestamp_seconds,
            )
            return False

        # 2. 빈 utterances — phantom
        if not utts:
            logger.warning(
                "D2 phantom: meeting_id=%s, ts=%d, reason=empty_utterances",
                meeting_id,
                timestamp_seconds,
            )
            return False

        # 3. 구간 겹침 검사 — 전체 순회 (단일 회의 발화 수백~수천 건 가정)
        tol = self._tolerance_seconds
        ts = timestamp_seconds
        for utt in utts:
            try:
                start = _read_utterance_float(utt, "start")
                end = _read_utterance_float(utt, "end")
            except (TypeError, ValueError, AttributeError):
                # 비정상 발화는 skip
                continue
            # (utt.start ≤ ts + tol) AND (utt.end ≥ ts - tol)
            if start <= ts + tol and end >= ts - tol:
                return True

        logger.warning(
            "D2 phantom: meeting_id=%s, ts=%d, reason=no_utterance",
            meeting_id,
            timestamp_seconds,
        )
        return False

    async def fetch_utterance(
        self,
        meeting_id: str,
        timestamp_seconds: int,
    ) -> str | None:
        """매핑된 발화 텍스트를 반환한다.

        verify_exists 와 동일한 매칭 로직을 적용하되, 텍스트를 반환한다.
        매칭 발화가 여러 건이면 ts 와 가장 가까운 발화의 text 를 반환.

        Args:
            meeting_id: 실제 회의 ID 또는 하위 호환 8자리 hex.
            timestamp_seconds: 초 단위 정수.

        Returns:
            매칭 발화의 text. 없으면 None.
        """
        utts = self._utterances_by_meeting.get(meeting_id)
        if not utts:
            return None

        tol = self._tolerance_seconds
        ts = timestamp_seconds

        # 매칭되는 발화 중 ts 와 가장 가까운(거리 최소) 발화를 선택
        best_text: str | None = None
        best_distance: float = float("inf")
        for utt in utts:
            try:
                start = _read_utterance_float(utt, "start")
                end = _read_utterance_float(utt, "end")
                text = str(_read_utterance_field(utt, "text"))
            except (TypeError, ValueError, AttributeError):
                continue
            if start <= ts + tol and end >= ts - tol:
                # 발화 구간 중심점과의 거리 계산
                center = (start + end) / 2.0
                distance = abs(center - ts)
                if distance < best_distance:
                    best_distance = distance
                    best_text = text

        return best_text


class CheckpointCitationVerifier(UtterancesCitationVerifier):
    """현재 발화와 과거 체크포인트를 함께 사용하는 fail-closed D2 verifier.

    현재 ingest의 발화는 상위 ``UtterancesCitationVerifier``처럼 메모리에서
    검증한다. 이 인스턴스가 모르는 과거 meeting_id는 해당 회의의 ``correct.json``을
    우선 읽고, 파일 자체가 없을 때만 ``merge.json`` 원장을 사용한다. 어느 단계에서든
    신뢰할 수 없는 상태면 False를 반환하므로 guard가 기존 action page를 덮어쓰지
    못하게 한다.
    """

    _CHECKPOINT_FILENAMES: tuple[str, ...] = ("correct.json", "merge.json")

    def __init__(
        self,
        *,
        checkpoints_dir: Path,
        utterances_by_meeting: dict[str, list[Utterance]] | None = None,
        tolerance_seconds: int = 2,
    ) -> None:
        """현재 발화와 영속 체크포인트 root를 받는다.

        Args:
            checkpoints_dir: ``checkpoints/`` root. 모든 component를 no-follow로 연다.
            utterances_by_meeting: 현재 ingest 등 이미 메모리에 있는 검증 원장.
            tolerance_seconds: timestamp 허용 오차(±초).
        """
        super().__init__(utterances_by_meeting or {}, tolerance_seconds=tolerance_seconds)
        try:
            self._checkpoints_dir: Path | None = _checkpoint_root_lexical_path(checkpoints_dir)
        except (TypeError, ValueError) as exc:
            logger.warning("D2 과거 체크포인트 root 거부: %r", exc)
            self._checkpoints_dir = None
        self._checkpoint_load_attempted: set[str] = set()

    async def _load_historical_meeting(self, meeting_id: str) -> None:
        """아직 없는 과거 meeting 원장을 checkpoint에서 1회만 안전하게 적재한다."""
        if (
            meeting_id in self._utterances_by_meeting
            or meeting_id in self._checkpoint_load_attempted
        ):
            return
        self._checkpoint_load_attempted.add(meeting_id)
        if self._checkpoints_dir is None or not _is_safe_meeting_id(meeting_id):
            logger.warning("D2 phantom: 과거 meeting_id/checkpoint root를 신뢰할 수 없음")
            return

        try:
            utterances = await asyncio.to_thread(
                self._load_checkpoint_utterances,
                self._checkpoints_dir,
                meeting_id,
            )
        except Exception as exc:  # noqa: BLE001 -- verifier는 guard로 예외를 전파하지 않는다.
            logger.warning("D2 과거 체크포인트 원장 읽기 실패: %r", exc)
            return
        if utterances is None:
            logger.warning("D2 phantom: 과거 회의 원장을 검증할 수 없음")
            return
        self._utterances_by_meeting[meeting_id] = sorted(
            utterances,
            key=_utterance_start_sort_key,
        )

    @classmethod
    def _load_checkpoint_utterances(
        cls,
        checkpoints_dir: Path,
        meeting_id: str,
    ) -> list[dict[str, Any]] | None:
        """correct 우선, 파일 부재시에만 merge 원장을 안전하게 읽는다."""
        for filename in cls._CHECKPOINT_FILENAMES:
            status, payload = _read_checkpoint_json_no_follow(
                checkpoints_dir,
                meeting_id,
                filename,
            )
            if status == "missing":
                continue
            if status != "ok":
                return None
            utterances = _checkpoint_utterances(payload)
            if utterances is None:
                logger.warning("D2 과거 체크포인트 발화 스키마가 올바르지 않음")
                return None
            return utterances
        return None

    async def verify_exists(
        self,
        meeting_id: str,
        timestamp_seconds: int,
    ) -> bool:
        """현재/과거 회의 원장을 모두 이용해 citation 실재성을 검증한다."""
        await self._load_historical_meeting(meeting_id)
        return await super().verify_exists(meeting_id, timestamp_seconds)

    async def fetch_utterance(
        self,
        meeting_id: str,
        timestamp_seconds: int,
    ) -> str | None:
        """현재/과거 checkpoint 원장에서 citation에 대응하는 발화를 반환한다."""
        await self._load_historical_meeting(meeting_id)
        return await super().fetch_utterance(meeting_id, timestamp_seconds)
