"""macOS Core Audio 헬퍼 — 오디오 장치의 정식 속성을 조회한다.

system_profiler SPAudioDataType 의 plist XML 출력을 파싱하여
장치 이름(이름 키워드)에 의존하지 않고 장치 종류를 정확히 판정한다.
특히 Aggregate Device 는 사용자가 임의 이름을 지정할 수 있으므로
coreaudio_device_transport 속성을 기준으로 판별한다.

의존성: Python 표준 라이브러리만 사용 (subprocess + plistlib).
외부 패키지(PyObjC, ctypes 등) 없이 macOS 내장 도구만 활용한다.
"""

from __future__ import annotations

import logging
import plistlib
import subprocess
import sys

logger = logging.getLogger(__name__)

# macOS system_profiler 가 Aggregate Device 에 부여하는 transport 타입 값.
# 실제 Aggregate Device 가 없는 환경에서는 하드코딩으로 상수 정의하며,
# macOS Core Audio 공식 kAudioDeviceTransportTypeAggregate = 'grup'(0x67727570) 에서
# system_profiler 가 사람이 읽을 수 있는 문자열로 변환한 결과이다.
# 방어적으로 두 가지 후보를 모두 허용한다:
#   - "coreaudio_device_type_aggregate": system_profiler 표준 형식
#   - "aggregate": 일부 macOS 버전에서 줄인 형식
_AGGREGATE_TRANSPORT_VALUES: frozenset[str] = frozenset(
    {
        "coreaudio_device_type_aggregate",
        "aggregate",
    }
)

# system_profiler 호출 기본 타임아웃(초).
# 일반적으로 0.1초 내외이나 시스템 부하를 고려해 넉넉히 잡는다.
_DEFAULT_TIMEOUT: float = 5.0


def get_aggregate_device_names(
    timeout_seconds: float = _DEFAULT_TIMEOUT,
) -> set[str]:
    """macOS 에 등록된 Aggregate Device 이름 집합을 반환한다.

    system_profiler -xml SPAudioDataType 명령으로 CoreAudio 장치 목록을 가져와
    coreaudio_device_transport 속성이 Aggregate 를 나타내는 장치의 이름만 추출한다.

    실패 시 (macOS 외 환경, system_profiler 에러, plist 파싱 실패 등) 빈 set 을
    반환하여 호출자가 기존 키워드 매칭으로 폴백할 수 있도록 한다 (graceful degradation).

    Args:
        timeout_seconds: system_profiler 서브프로세스 타임아웃 (초).
                         기본값 5.0초.

    Returns:
        Aggregate Device 이름 집합.
        macOS 외 환경이거나 조회 실패 시 빈 set.
    """
    # macOS 이외 플랫폼에서는 즉시 빈 set 반환 (Windows, Linux 불필요 조회 방지)
    if sys.platform != "darwin":
        logger.debug("비 macOS 플랫폼 — Aggregate 장치 조회 건너뜀")
        return set()

    try:
        result = subprocess.run(
            ["system_profiler", "-xml", "SPAudioDataType"],
            capture_output=True,
            timeout=timeout_seconds,
            check=False,  # 실패해도 예외 발생 안 함 — returncode 로 판단
        )
    except subprocess.TimeoutExpired:
        logger.debug(
            f"system_profiler 타임아웃 ({timeout_seconds}초) — "
            "Aggregate 판정 불가, 키워드 매칭으로 폴백"
        )
        return set()
    except FileNotFoundError:
        # system_profiler 가 없는 환경 (macOS 가 아닌 CI 등)
        logger.debug("system_profiler 미발견 — 키워드 매칭으로 폴백")
        return set()
    except OSError as e:
        logger.debug(f"system_profiler 실행 실패: {e} — 키워드 매칭으로 폴백")
        return set()

    if result.returncode != 0:
        logger.debug(
            f"system_profiler 비정상 종료: returncode={result.returncode} — "
            "키워드 매칭으로 폴백"
        )
        return set()

    if not result.stdout:
        logger.debug("system_profiler 빈 출력 — 키워드 매칭으로 폴백")
        return set()

    try:
        plist_data = plistlib.loads(result.stdout)
    except (plistlib.InvalidFileException, Exception) as e:
        logger.debug(f"plist 파싱 실패: {e} — 키워드 매칭으로 폴백")
        return set()

    names = _extract_aggregate_names(plist_data)
    if names:
        logger.info(f"CoreAudio Aggregate 장치 감지: {names}")
    else:
        logger.debug("CoreAudio Aggregate 장치 없음 (정상)")

    return names


def _extract_aggregate_names(plist_data: list) -> set[str]:
    """plist 데이터에서 Aggregate Device 이름 집합을 추출한다.

    system_profiler -xml SPAudioDataType 의 파싱 결과 구조:
    [
        {
            "_items": [
                {
                    "_name": "coreaudio_device",
                    "_items": [
                        {
                            "_name": "27QC7",
                            "coreaudio_device_transport": "coreaudio_device_type_displayport",
                            ...
                        },
                        {
                            "_name": "BlackHole 2ch",
                            "coreaudio_device_transport": "coreaudio_device_type_virtual",
                            ...
                        },
                        ...
                    ]
                }
            ],
            ...
        }
    ]

    coreaudio_device_transport 값이 _AGGREGATE_TRANSPORT_VALUES 에 포함된
    장치의 _name 을 반환한다.

    이 함수는 테스트 용이성을 위해 모듈 내부에서만 호출되도록 _ 접두사를 붙이지만
    unit test 에서 직접 호출할 수 있도록 모듈 레벨에 정의한다.

    Args:
        plist_data: plistlib.loads() 결과. list[dict] 형식.

    Returns:
        Aggregate Device 이름 집합. 파싱 실패 또는 없을 때 빈 set.
    """
    aggregate_names: set[str] = set()

    if not isinstance(plist_data, list):
        logger.debug(f"예상치 못한 plist 최상위 타입: {type(plist_data)}")
        return aggregate_names

    for top_level in plist_data:
        if not isinstance(top_level, dict):
            continue

        # 최상위 _items 배열 순회 (보통 하나의 "coreaudio_device" 그룹)
        top_items = top_level.get("_items", [])
        if not isinstance(top_items, list):
            continue

        for group in top_items:
            if not isinstance(group, dict):
                continue

            # 그룹 내 _items 배열 = 실제 장치 목록
            device_list = group.get("_items", [])
            if not isinstance(device_list, list):
                continue

            for device in device_list:
                if not isinstance(device, dict):
                    continue

                device_name = device.get("_name", "")
                transport = device.get("coreaudio_device_transport", "")

                if not device_name or not transport:
                    continue

                # transport 값이 Aggregate 판정 집합에 속하면 이름 추가
                if transport in _AGGREGATE_TRANSPORT_VALUES:
                    aggregate_names.add(str(device_name))
                    logger.debug(
                        f"Aggregate 장치 확인: '{device_name}' "
                        f"(transport={transport})"
                    )

    return aggregate_names
