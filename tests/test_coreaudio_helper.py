"""
CoreAudio 헬퍼 모듈 테스트 (CoreAudio Helper Module Tests)

목적: core/coreaudio_helper.py 의 get_aggregate_device_names() 및
      _extract_aggregate_names() 를 검증한다.

주요 테스트:
    - get_aggregate_device_names() 정상 케이스 (plist 샘플 mock)
    - subprocess 실패 시 빈 set 반환
    - 타임아웃 시 빈 set 반환
    - plist 파싱 실패 시 빈 set 반환
    - system_profiler 미발견(FileNotFoundError) 시 빈 set 반환
    - 비 macOS 플랫폼에서 빈 set 반환
    - _extract_aggregate_names 엣지 케이스
의존성: pytest, unittest.mock, plistlib
"""

from __future__ import annotations

import plistlib
import subprocess
from unittest.mock import MagicMock, patch

from core.coreaudio_helper import (
    _AGGREGATE_TRANSPORT_VALUES,
    _extract_aggregate_names,
    get_aggregate_device_names,
)

# === plist 샘플 헬퍼 ===


def _build_plist_bytes(devices: list[dict]) -> bytes:
    """테스트용 system_profiler plist XML bytes 를 생성한다.

    실제 system_profiler -xml SPAudioDataType 출력 구조를 모방한다:
    [
        {
            "_items": [
                {
                    "_name": "coreaudio_device",
                    "_items": [ {장치1}, {장치2}, ... ]
                }
            ],
            ...
        }
    ]

    Args:
        devices: 장치 dict 목록. 각 dict 에는 _name, coreaudio_device_transport 포함.

    Returns:
        plistlib.dumps() 결과 bytes
    """
    plist_data = [
        {
            "_items": [
                {
                    "_name": "coreaudio_device",
                    "_items": devices,
                }
            ],
        }
    ]
    return plistlib.dumps(plist_data)


def _make_device(name: str, transport: str) -> dict:
    """테스트용 장치 dict 를 생성한다."""
    return {
        "_name": name,
        "coreaudio_device_transport": transport,
        "coreaudio_device_manufacturer": "Test",
        "coreaudio_device_srate": 48000.0,
    }


# === TestGetAggregateDeviceNames ===


class TestGetAggregateDeviceNames:
    """get_aggregate_device_names() 테스트."""

    def test_정상_케이스_Aggregate_감지(self) -> None:
        """coreaudio_device_type_aggregate 를 가진 장치 이름이 반환된다."""
        plist_bytes = _build_plist_bytes(
            [
                _make_device("MacBook Air 마이크", "coreaudio_device_type_builtin"),
                _make_device("BlackHole 2ch", "coreaudio_device_type_virtual"),
                _make_device("통합 마이크", "coreaudio_device_type_aggregate"),
            ]
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = plist_bytes

        with patch("subprocess.run", return_value=mock_result):
            names = get_aggregate_device_names()

        assert names == {"통합 마이크"}

    def test_정상_케이스_여러_Aggregate(self) -> None:
        """여러 Aggregate Device 가 있으면 모두 반환된다."""
        plist_bytes = _build_plist_bytes(
            [
                _make_device("My Combined Audio", "coreaudio_device_type_aggregate"),
                _make_device("Meeting Transcriber Aggregate", "coreaudio_device_type_aggregate"),
                _make_device("BlackHole 2ch", "coreaudio_device_type_virtual"),
            ]
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = plist_bytes

        with patch("subprocess.run", return_value=mock_result):
            names = get_aggregate_device_names()

        assert names == {"My Combined Audio", "Meeting Transcriber Aggregate"}

    def test_정상_케이스_Aggregate_없음(self) -> None:
        """Aggregate Device 가 없으면 빈 set 이 반환된다."""
        plist_bytes = _build_plist_bytes(
            [
                _make_device("MacBook Air 마이크", "coreaudio_device_type_builtin"),
                _make_device("BlackHole 2ch", "coreaudio_device_type_virtual"),
            ]
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = plist_bytes

        with patch("subprocess.run", return_value=mock_result):
            names = get_aggregate_device_names()

        assert names == set()

    def test_subprocess_실패_빈_set_반환(self) -> None:
        """system_profiler 가 비정상 종료(returncode != 0)하면 빈 set 반환."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = b""

        with patch("subprocess.run", return_value=mock_result):
            names = get_aggregate_device_names()

        assert names == set()

    def test_timeout_빈_set_반환(self) -> None:
        """system_profiler 타임아웃 시 빈 set 반환."""
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="system_profiler", timeout=5),
        ):
            names = get_aggregate_device_names()

        assert names == set()

    def test_FileNotFoundError_빈_set_반환(self) -> None:
        """system_profiler 가 없으면(FileNotFoundError) 빈 set 반환."""
        with patch("subprocess.run", side_effect=FileNotFoundError("system_profiler not found")):
            names = get_aggregate_device_names()

        assert names == set()

    def test_OSError_빈_set_반환(self) -> None:
        """subprocess.run 이 OSError 를 던지면 빈 set 반환."""
        with patch("subprocess.run", side_effect=OSError("Permission denied")):
            names = get_aggregate_device_names()

        assert names == set()

    def test_plist_파싱_실패_빈_set_반환(self) -> None:
        """stdout 이 유효하지 않은 plist 이면 빈 set 반환."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b"not a valid plist"

        with patch("subprocess.run", return_value=mock_result):
            names = get_aggregate_device_names()

        assert names == set()

    def test_빈_stdout_빈_set_반환(self) -> None:
        """stdout 이 비어있으면 빈 set 반환."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b""

        with patch("subprocess.run", return_value=mock_result):
            names = get_aggregate_device_names()

        assert names == set()

    def test_비_macOS_플랫폼_빈_set_반환(self) -> None:
        """macOS 외 플랫폼(darwin 아님)에서는 subprocess 호출 없이 빈 set 반환."""
        with (
            patch("sys.platform", "linux"),
            patch("subprocess.run") as mock_run,
        ):
            names = get_aggregate_device_names()

        assert names == set()
        # subprocess.run 이 호출되지 않아야 한다
        mock_run.assert_not_called()

    def test_aggregate_transport_값_방어적_처리(self) -> None:
        """'aggregate' (줄인 형식) 도 Aggregate 로 인식된다."""
        plist_bytes = _build_plist_bytes(
            [
                _make_device("My Device", "aggregate"),
            ]
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = plist_bytes

        with patch("subprocess.run", return_value=mock_result):
            names = get_aggregate_device_names()

        assert "My Device" in names


# === TestExtractAggregateNames ===


class TestExtractAggregateNames:
    """_extract_aggregate_names() 엣지 케이스 테스트."""

    def test_빈_리스트(self) -> None:
        """빈 리스트 입력 시 빈 set 반환."""
        assert _extract_aggregate_names([]) == set()

    def test_최상위_dict_없음(self) -> None:
        """최상위 _items 가 없는 dict 입력 시 빈 set 반환."""
        assert _extract_aggregate_names([{}]) == set()

    def test_items_없는_plist(self) -> None:
        """그룹에 _items 가 없으면 빈 set 반환."""
        data = [{"_items": [{"_name": "coreaudio_device"}]}]
        assert _extract_aggregate_names(data) == set()

    def test_Aggregate_없는_plist(self) -> None:
        """Aggregate Device 가 없으면 빈 set 반환."""
        data = [
            {
                "_items": [
                    {
                        "_name": "coreaudio_device",
                        "_items": [
                            {
                                "_name": "MacBook Air 마이크",
                                "coreaudio_device_transport": "coreaudio_device_type_builtin",
                            },
                            {
                                "_name": "BlackHole 2ch",
                                "coreaudio_device_transport": "coreaudio_device_type_virtual",
                            },
                        ],
                    }
                ]
            }
        ]
        result = _extract_aggregate_names(data)
        assert result == set()

    def test_Aggregate_있는_plist(self) -> None:
        """Aggregate Device 가 있으면 이름이 반환된다."""
        data = [
            {
                "_items": [
                    {
                        "_name": "coreaudio_device",
                        "_items": [
                            {
                                "_name": "통합 오디오",
                                "coreaudio_device_transport": "coreaudio_device_type_aggregate",
                            }
                        ],
                    }
                ]
            }
        ]
        result = _extract_aggregate_names(data)
        assert result == {"통합 오디오"}

    def test_transport_없는_장치_건너뜀(self) -> None:
        """coreaudio_device_transport 가 없는 장치는 건너뛴다."""
        data = [
            {
                "_items": [
                    {
                        "_name": "coreaudio_device",
                        "_items": [
                            {
                                "_name": "Unknown Device",
                                # coreaudio_device_transport 키 없음
                            }
                        ],
                    }
                ]
            }
        ]
        result = _extract_aggregate_names(data)
        assert result == set()

    def test_이름_없는_장치_건너뜀(self) -> None:
        """_name 이 없는 장치는 건너뛴다."""
        data = [
            {
                "_items": [
                    {
                        "_name": "coreaudio_device",
                        "_items": [
                            {
                                "coreaudio_device_transport": "coreaudio_device_type_aggregate",
                                # _name 키 없음
                            }
                        ],
                    }
                ]
            }
        ]
        result = _extract_aggregate_names(data)
        assert result == set()

    def test_dict_가_아닌_최상위_항목_건너뜀(self) -> None:
        """최상위 배열에 dict 가 아닌 항목이 있으면 건너뛴다."""
        result = _extract_aggregate_names(["string_item", 42, None])
        assert result == set()

    def test_한국어_이름_Aggregate(self) -> None:
        """한국어 이름의 Aggregate Device 도 정확히 감지된다."""
        data = [
            {
                "_items": [
                    {
                        "_name": "coreaudio_device",
                        "_items": [
                            {
                                "_name": "내 통합 마이크",
                                "coreaudio_device_transport": "coreaudio_device_type_aggregate",
                            }
                        ],
                    }
                ]
            }
        ]
        result = _extract_aggregate_names(data)
        assert "내 통합 마이크" in result

    def test_transport_상수_집합_확인(self) -> None:
        """_AGGREGATE_TRANSPORT_VALUES 에 필수 값이 포함되어 있다."""
        assert "coreaudio_device_type_aggregate" in _AGGREGATE_TRANSPORT_VALUES
        assert "aggregate" in _AGGREGATE_TRANSPORT_VALUES

    def test_실제_plist_구조_전체_파싱(self) -> None:
        """실제 system_profiler 출력 구조와 동일한 plist 를 올바르게 파싱한다."""
        # 실제 출력에서 수집한 구조와 유사한 샘플 plist
        plist_bytes = _build_plist_bytes(
            [
                {
                    "_name": "27QC7",
                    "coreaudio_device_transport": "coreaudio_device_type_displayport",
                    "coreaudio_device_manufacturer": "COS",
                },
                {
                    "_name": "BlackHole 2ch",
                    "coreaudio_device_transport": "coreaudio_device_type_virtual",
                    "coreaudio_device_manufacturer": "Existential Audio Inc.",
                },
                {
                    "_name": "MacBook Air 마이크",
                    "coreaudio_device_transport": "coreaudio_device_type_builtin",
                    "coreaudio_device_manufacturer": "Apple Inc.",
                },
                {
                    "_name": "My Combined Audio",
                    "coreaudio_device_transport": "coreaudio_device_type_aggregate",
                    "coreaudio_device_manufacturer": "Apple Inc.",
                },
            ]
        )

        parsed = plistlib.loads(plist_bytes)
        result = _extract_aggregate_names(parsed)

        assert result == {"My Combined Audio"}
        assert "BlackHole 2ch" not in result
        assert "MacBook Air 마이크" not in result
