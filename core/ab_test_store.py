"""
A/B 테스트 파일 저장소 모듈.

목적: ~/.meeting-transcriber/ab_tests/{test_id}/ 디렉터리 기반의 단순한 파일
저장소. metadata.json 읽기/쓰기, test_id 화이트리스트 검증, 경로 이탈 방지,
테스트 목록/삭제를 제공한다.

주요 기능:
    - test_id 정규식 화이트리스트 검증 (path traversal 방지)
    - metadata.json 원자적 쓰기 (tmp → rename)
    - variant_a / variant_b 서브 디렉터리 준비
    - 디렉터리 스캔 기반 목록 조회 (최신순)
    - 디렉터리 삭제

의존성: config.AppConfig
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

from config import AppConfig

logger = logging.getLogger(__name__)

# test_id 화이트리스트 정규식 — ADR-2
# 형식: ab_{YYYYMMDD-HHMMSS}_{8자 16진수}
_TEST_ID_PATTERN = re.compile(r"^ab_\d{8}-\d{6}_[a-f0-9]{8}$")

# A/B 테스트 저장소 루트 디렉터리명
_AB_TESTS_DIRNAME = "ab_tests"

# metadata 파일명
METADATA_FILENAME = "metadata.json"
VARIANT_DIRS: tuple[str, str] = ("variant_a", "variant_b")


def is_valid_test_id(test_id: str) -> bool:
    """test_id 가 화이트리스트 정규식과 일치하는지 검증한다.

    Args:
        test_id: 검증 대상 문자열

    Returns:
        유효하면 True, 아니면 False
    """
    if not isinstance(test_id, str) or not test_id:
        return False
    return bool(_TEST_ID_PATTERN.match(test_id))


def get_ab_test_root(config: AppConfig) -> Path:
    """A/B 테스트 저장소 루트 디렉터리를 반환한다 (없으면 생성).

    Args:
        config: 앱 설정 인스턴스

    Returns:
        절대 경로 (Path)
    """
    root = config.paths.resolved_base_dir / _AB_TESTS_DIRNAME
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error(f"ab_tests 루트 생성 실패: {root} ({exc})")
        raise
    return root


def resolve_test_dir(config: AppConfig, test_id: str) -> Path:
    """test_id 를 검증한 뒤 테스트 디렉터리 경로를 반환한다.

    - 정규식 화이트리스트 검증
    - Path.resolve() 후 루트 하위 여부 재검사 (심볼릭 링크/역참조 방어)

    Args:
        config: 앱 설정
        test_id: 테스트 식별자

    Returns:
        테스트 디렉터리 절대 경로 (존재 여부 보장 X)

    Raises:
        ValueError: test_id 가 유효하지 않거나 루트 이탈 시
    """
    if not is_valid_test_id(test_id):
        raise ValueError(f"유효하지 않은 test_id: {test_id!r}")

    root = get_ab_test_root(config).resolve()
    candidate = (root / test_id).resolve()

    # 루트 하위 여부 재확인
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"test_id 가 ab_tests 루트를 벗어났습니다: {test_id!r}"
        ) from exc

    return candidate


def create_test_dir(config: AppConfig, test_id: str) -> Path:
    """테스트 루트 및 variant 서브 디렉터리를 생성한다.

    Args:
        config: 앱 설정
        test_id: 테스트 식별자

    Returns:
        테스트 디렉터리 절대 경로

    Raises:
        ValueError: test_id 부적합
        OSError: 디렉터리 생성 실패
    """
    test_dir = resolve_test_dir(config, test_id)
    test_dir.mkdir(parents=True, exist_ok=True)
    for variant in VARIANT_DIRS:
        (test_dir / variant).mkdir(parents=True, exist_ok=True)
    logger.debug(f"A/B 테스트 디렉터리 생성: {test_dir}")
    return test_dir


def _metadata_path(config: AppConfig, test_id: str) -> Path:
    """테스트 metadata.json 절대 경로를 반환한다."""
    return resolve_test_dir(config, test_id) / METADATA_FILENAME


def read_metadata(config: AppConfig, test_id: str) -> dict[str, Any]:
    """metadata.json 을 읽어 딕셔너리로 반환한다.

    Args:
        config: 앱 설정
        test_id: 테스트 식별자

    Returns:
        metadata 딕셔너리

    Raises:
        FileNotFoundError: 파일이 없을 때
        ValueError: JSON 파싱 실패 또는 test_id 부적합
    """
    path = _metadata_path(config, test_id)
    if not path.exists():
        raise FileNotFoundError(f"metadata.json 이 없습니다: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        logger.error(f"metadata.json 파싱 실패: {path} ({exc})")
        raise ValueError(f"metadata.json 파싱 실패: {exc}") from exc


def write_metadata(
    config: AppConfig, test_id: str, data: dict[str, Any]
) -> None:
    """metadata.json 을 원자적으로 쓴다 (tmp → rename).

    Args:
        config: 앱 설정
        test_id: 테스트 식별자
        data: 저장할 딕셔너리
    """
    test_dir = resolve_test_dir(config, test_id)
    test_dir.mkdir(parents=True, exist_ok=True)
    path = test_dir / METADATA_FILENAME
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # 일부 환경(tmpfs)에서는 fsync 실패 — 치명적이지 않음
                pass
        os.replace(tmp, path)
    except OSError as exc:
        logger.error(f"metadata.json 저장 실패: {path} ({exc})")
        # 임시 파일 정리
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def update_metadata(
    config: AppConfig, test_id: str, **patch: Any
) -> dict[str, Any]:
    """metadata.json 을 읽고 patch 를 병합한 뒤 다시 쓴다.

    단순한 read-modify-write 이며, 동시성 보호는 호출자의 asyncio.Lock
    (러너의 `_ab_test_lock`) 에 의존한다.

    Args:
        config: 앱 설정
        test_id: 테스트 식별자
        **patch: 덮어쓸 필드

    Returns:
        갱신된 metadata 딕셔너리
    """
    try:
        data = read_metadata(config, test_id)
    except FileNotFoundError:
        data = {}
    data.update(patch)
    write_metadata(config, test_id, data)
    return data


def list_test_ids(
    config: AppConfig, source_meeting_id: str | None = None
) -> list[str]:
    """저장된 모든 테스트 ID 를 최신순으로 반환한다.

    Args:
        config: 앱 설정
        source_meeting_id: 지정되면 해당 소스 회의에 속한 테스트만 필터

    Returns:
        test_id 리스트 (최신순)
    """
    root = get_ab_test_root(config)
    if not root.exists():
        return []

    ids: list[str] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        if not is_valid_test_id(entry.name):
            continue
        if source_meeting_id is not None:
            try:
                meta = read_metadata(config, entry.name)
            except (FileNotFoundError, ValueError):
                continue
            if meta.get("source_meeting_id") != source_meeting_id:
                continue
        ids.append(entry.name)

    # test_id 에 타임스탬프가 내장되어 있으므로 문자열 역순이 곧 최신순
    ids.sort(reverse=True)
    return ids


def delete_test_dir(config: AppConfig, test_id: str) -> None:
    """테스트 디렉터리를 통째로 삭제한다.

    Args:
        config: 앱 설정
        test_id: 테스트 식별자

    Raises:
        ValueError: test_id 부적합
    """
    test_dir = resolve_test_dir(config, test_id)
    if not test_dir.exists():
        logger.warning(f"삭제 대상 디렉터리가 없음: {test_dir}")
        return
    try:
        shutil.rmtree(test_dir)
        logger.info(f"A/B 테스트 디렉터리 삭제: {test_dir}")
    except OSError as exc:
        logger.error(f"A/B 테스트 디렉터리 삭제 실패: {test_dir} ({exc})")
        raise
