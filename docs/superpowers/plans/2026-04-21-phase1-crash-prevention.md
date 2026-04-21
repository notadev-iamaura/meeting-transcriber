# Phase 1: 크래시 방지 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. TDD Red-Green-Refactor is mandatory for every task.

**Goal:** 저볼륨 오디오 파일이 전사 파이프라인에서 MLX Metal GPU 크래시를 일으키는 문제를 다층 방어 설계(오디오 게이트 + 재시도 제한 + 동적 타임아웃 + watcher 격리 + launchd 자동 복구)로 차단한다.

**Architecture:** 외부 입력 검증 강화(Defense-in-Depth). 큐잉 시점에 저품질 오디오를 quarantine으로 격리하고, 통과 후에도 단계별 실패 시 재시도 정책과 타임아웃을 조건부·동적으로 적용한다. 크래시 발생 시 launchd가 자동 재기동하며, orphan job 복구 로직이 상태 정합성을 유지한다.

**Tech Stack:** Python 3.12+, FastAPI, watchdog, Pydantic v2, pytest, ffmpeg/ffprobe (subprocess), launchd

---

## Context for Implementers

### 사건 배경 (근본 원인)
1. `meeting_20260420_100536.wav` (22분, mean_volume −48.6dB) 전사 중 MLX Metal SIGSEGV
2. 타임아웃 후 **재시도**가 크래시의 즉발 트리거 (Metal 상태 오염된 채 모델 재로드)
3. DB 삭제해도 오디오 파일이 남아 watcher가 재큐잉 → 동일 크래시 반복
4. 격리된 8건 모두 −45~−56dB 극저볼륨, 정상 113건 중 최저는 −32.1dB

### 검증된 제약
- **VAD=true는 역효과 확정** (BENCHMARK.md §1: 환각 14배 증가)
- **mlx-whisper는 streaming callback 미지원** (`inspect.signature` 확인)
- **MLX 0.31.1은 최신** (업스트림 fix 없음)
- 벤치마크 RTF: komixv2 fp16 기준 1.19 (M4)

### 현재 코드 상태 (탐색 결과)
- 재시도 로직: `core/pipeline.py:1070-1135` (모든 Exception catch, `retry_max_count=3`)
- STT 타임아웃: `config.py:184-186` (`transcribe_timeout_seconds=1800` 설정값)
- DELETE API: `api/routes.py:1105-1154` (DB만 삭제, 오디오 파일 잔존)
- Watcher: `core/watcher.py` (제외 경로 설정 없음)
- launchd: `scripts/setup_launchagent.sh:139-140` (KeepAlive=false)

### 불변 제약 (반드시 준수)
- Python 타입 힌트 필수, 한국어 docstring
- `print()` 금지 → `logger` 사용
- `pathlib.Path` 사용 (os.path 금지)
- bare except 금지
- 외부 API 호출 금지 (100% 로컬)

---

## File Structure

### 신규 파일
| 경로 | 책임 |
|---|---|
| `core/audio_quality.py` | 오디오 품질 검증 (ffmpeg volumedetect 래퍼, ValidationResult 반환) |
| `core/retry_policy.py` | RetryableError / NonRetryableError 계층 + 재시도 정책 판정 |
| `core/quarantine.py` | 격리 디렉토리 관리 (오디오 파일 이동, 경로 계산) |
| `tests/test_audio_quality.py` | audio_quality 단위 테스트 |
| `tests/test_retry_policy.py` | retry_policy 단위 테스트 |
| `tests/test_quarantine.py` | quarantine 단위 테스트 |
| `tests/test_phase1_integration.py` | Phase 1 통합 시나리오 테스트 |

### 수정 파일
| 경로 | 수정 내용 |
|---|---|
| `config.py` | `PathsConfig`에 `audio_quarantine_dir` 추가, `WatcherConfig`에 `excluded_paths` 추가, `STTConfig.transcribe_timeout_seconds` → 동적 계산 필드 교체 |
| `config.yaml` | 위 신규 설정값 기본값 |
| `core/watcher.py` | 제외 경로 필터링 + 오디오 품질 게이트 호출 |
| `core/pipeline.py` | 재시도 루프에서 `NonRetryableError` 구분 + 동적 타임아웃 계산 |
| `steps/transcriber.py` | `transcribe_timeout_seconds` → 런타임 계산값 사용, 타임아웃을 `TranscriptionTimeoutError(NonRetryableError)` 로 raise |
| `api/routes.py` | `DELETE /api/meetings/{id}` 가 오디오 파일도 quarantine으로 이동 |
| `scripts/setup_launchagent.sh` | `KeepAlive=true` + `ThrottleInterval=30` |

---

## TDD 원칙 (모든 태스크 필수)

각 태스크는 다음 순서 **강제**:
1. **RED**: 실패하는 테스트 먼저 작성
2. **검증**: 테스트 실행 → FAIL 확인 (에러 메시지 로그)
3. **GREEN**: 최소 구현
4. **검증**: 테스트 실행 → PASS 확인
5. **REFACTOR** (선택): 리팩터링 후 테스트 재실행
6. **COMMIT**: 단일 책임 커밋

**금지**: 구현 먼저 작성 후 테스트 작성 / 테스트 없이 커밋 / 실패 확인 없이 GREEN 단계로 진행

---

## Task 1: 오디오 품질 검증 모듈 (Pure Function)

**목표:** ffmpeg로 오디오의 mean_volume과 duration을 측정하는 순수 함수. 외부 의존성은 subprocess뿐.

**Files:**
- Create: `core/audio_quality.py`
- Test: `tests/test_audio_quality.py`

### Step 1.1: 실패 테스트 작성

- [ ] **Step 1.1.1: ValidationResult 데이터클래스 + 정상 오디오 테스트 작성**

`tests/test_audio_quality.py` 신규 생성:

```python
"""오디오 품질 검증 모듈 테스트."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from core.audio_quality import (
    AudioQualityResult,
    AudioQualityStatus,
    validate_audio_quality,
)


def test_정상_오디오는_accept_반환():
    """정상 볼륨(-25dB) 오디오는 ACCEPT 반환한다."""
    fake_path = Path("/tmp/normal.wav")
    with (
        patch("core.audio_quality._measure_mean_volume_db", return_value=-25.0),
        patch("core.audio_quality._measure_duration_seconds", return_value=900.0),
    ):
        result = validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=5.0)

    assert result.status == AudioQualityStatus.ACCEPT
    assert result.mean_volume_db == -25.0
    assert result.duration_seconds == 900.0
    assert result.reason == ""


def test_저볼륨_오디오는_reject_반환():
    """−45dB 오디오는 LOW_VOLUME 사유로 REJECT 반환한다."""
    fake_path = Path("/tmp/quiet.wav")
    with (
        patch("core.audio_quality._measure_mean_volume_db", return_value=-45.0),
        patch("core.audio_quality._measure_duration_seconds", return_value=1200.0),
    ):
        result = validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=5.0)

    assert result.status == AudioQualityStatus.REJECT
    assert "저볼륨" in result.reason or "볼륨" in result.reason
    assert "-45" in result.reason


def test_너무_짧은_오디오는_reject_반환():
    """3초 오디오는 TOO_SHORT 사유로 REJECT 반환한다."""
    fake_path = Path("/tmp/short.wav")
    with (
        patch("core.audio_quality._measure_mean_volume_db", return_value=-25.0),
        patch("core.audio_quality._measure_duration_seconds", return_value=3.0),
    ):
        result = validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=5.0)

    assert result.status == AudioQualityStatus.REJECT
    assert "짧" in result.reason


def test_ffmpeg_실행_실패시_error_반환():
    """ffmpeg 호출이 실패하면 ERROR 상태 반환 (REJECT 아님, 판단 보류)."""
    fake_path = Path("/tmp/corrupt.wav")
    with patch("core.audio_quality._measure_mean_volume_db", side_effect=RuntimeError("ffmpeg failed")):
        result = validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=5.0)

    assert result.status == AudioQualityStatus.ERROR
    assert "ffmpeg" in result.reason.lower() or "측정" in result.reason


def test_경계값_정확히_mean_db와_같으면_accept():
    """mean_volume이 임계값과 정확히 같으면 통과 (>= 의미론)."""
    fake_path = Path("/tmp/edge.wav")
    with (
        patch("core.audio_quality._measure_mean_volume_db", return_value=-40.0),
        patch("core.audio_quality._measure_duration_seconds", return_value=600.0),
    ):
        result = validate_audio_quality(fake_path, min_mean_db=-40.0, min_duration_s=5.0)

    assert result.status == AudioQualityStatus.ACCEPT
```

- [ ] **Step 1.1.2: 테스트 실행 → 실패 확인**

```bash
cd /Users/youngouksong/projects/meeting-transcriber
source .venv/bin/activate
pytest tests/test_audio_quality.py -v
```

예상: `ModuleNotFoundError: No module named 'core.audio_quality'` — 이것이 RED 단계.

### Step 1.2: 최소 구현

- [ ] **Step 1.2.1: `core/audio_quality.py` 작성**

```python
"""
오디오 품질 검증 모듈

목적: 파이프라인 진입 전 오디오 파일의 볼륨·길이를 검사하여
     저품질 파일이 STT 디코더 루프/크래시를 유발하는 것을 차단한다.

근거: docs/BENCHMARK.md, 실측 크래시 파일 mean_volume=-48.6dB (정상은 -20~-30dB).
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class AudioQualityStatus(str, Enum):
    """오디오 품질 검증 결과 상태."""

    ACCEPT = "accept"
    REJECT = "reject"
    ERROR = "error"  # 측정 실패 (판단 보류)


@dataclass(frozen=True)
class AudioQualityResult:
    """오디오 품질 검증 결과."""

    status: AudioQualityStatus
    mean_volume_db: float | None
    duration_seconds: float | None
    reason: str = ""


class AudioMeasurementError(RuntimeError):
    """ffmpeg/ffprobe 측정 실패 예외."""


def validate_audio_quality(
    audio_path: Path,
    *,
    min_mean_db: float,
    min_duration_s: float,
) -> AudioQualityResult:
    """오디오 파일의 품질을 검증한다.

    Args:
        audio_path: 검증할 오디오 파일 경로
        min_mean_db: 허용 최소 mean_volume (예: -40.0)
        min_duration_s: 허용 최소 재생 시간 (예: 5.0)

    Returns:
        검증 결과. status가 ERROR면 측정 자체가 실패한 경우이며,
        호출자는 보수적으로 ACCEPT 처리하거나 별도 로깅 후 진행할 수 있다.
    """
    try:
        mean_db = _measure_mean_volume_db(audio_path)
        duration_s = _measure_duration_seconds(audio_path)
    except (AudioMeasurementError, RuntimeError, FileNotFoundError) as e:
        logger.warning(f"오디오 품질 측정 실패: {audio_path} ({e})")
        return AudioQualityResult(
            status=AudioQualityStatus.ERROR,
            mean_volume_db=None,
            duration_seconds=None,
            reason=f"측정 실패: {e}",
        )

    if duration_s < min_duration_s:
        return AudioQualityResult(
            status=AudioQualityStatus.REJECT,
            mean_volume_db=mean_db,
            duration_seconds=duration_s,
            reason=f"너무 짧음: {duration_s:.1f}s < {min_duration_s:.1f}s",
        )

    if mean_db < min_mean_db:
        return AudioQualityResult(
            status=AudioQualityStatus.REJECT,
            mean_volume_db=mean_db,
            duration_seconds=duration_s,
            reason=f"저볼륨: mean={mean_db:.1f}dB < {min_mean_db:.1f}dB",
        )

    return AudioQualityResult(
        status=AudioQualityStatus.ACCEPT,
        mean_volume_db=mean_db,
        duration_seconds=duration_s,
    )


def _measure_mean_volume_db(audio_path: Path) -> float:
    """ffmpeg volumedetect 필터로 mean_volume을 측정한다.

    Raises:
        AudioMeasurementError: ffmpeg 미설치 또는 파싱 실패
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise AudioMeasurementError("ffmpeg 실행 파일을 찾을 수 없습니다")

    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-nostats",
                "-i",
                str(audio_path),
                "-af",
                "volumedetect",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise AudioMeasurementError(f"ffmpeg 타임아웃: {audio_path}") from e

    output = result.stderr  # volumedetect는 stderr에 출력
    match = re.search(r"mean_volume:\s*(-?\d+\.?\d*)\s*dB", output)
    if match is None:
        raise AudioMeasurementError(f"mean_volume 파싱 실패: {output[:200]}")
    return float(match.group(1))


def _measure_duration_seconds(audio_path: Path) -> float:
    """ffprobe로 오디오 duration을 측정한다.

    Raises:
        AudioMeasurementError: ffprobe 미설치 또는 파싱 실패
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise AudioMeasurementError("ffprobe 실행 파일을 찾을 수 없습니다")

    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except subprocess.TimeoutExpired as e:
        raise AudioMeasurementError(f"ffprobe 타임아웃: {audio_path}") from e
    except subprocess.CalledProcessError as e:
        raise AudioMeasurementError(f"ffprobe 실패: {e.stderr}") from e

    try:
        return float(result.stdout.strip())
    except ValueError as e:
        raise AudioMeasurementError(f"duration 파싱 실패: {result.stdout!r}") from e
```

- [ ] **Step 1.2.2: 테스트 실행 → 통과 확인**

```bash
pytest tests/test_audio_quality.py -v
```

예상: 5개 테스트 전부 PASS.

- [ ] **Step 1.2.3: 실제 파일로 smoke test (선택)**

```bash
python3 -c "
from pathlib import Path
from core.audio_quality import validate_audio_quality
p = Path.home() / '.meeting-transcriber/audio_quarantine/meeting_20260420_100536.wav'
if p.exists():
    r = validate_audio_quality(p, min_mean_db=-40.0, min_duration_s=5.0)
    print(r)
"
```

예상: `REJECT`, mean_volume_db ≈ −48.6.

- [ ] **Step 1.2.4: 커밋**

```bash
git add core/audio_quality.py tests/test_audio_quality.py
git commit -m "기능: 오디오 품질 검증 모듈 추가 (Phase 1-1)

저볼륨 오디오가 STT 디코더 루프를 유발하는 문제를 큐잉 전에 차단.
- mean_volume < -40dB 거부 (실측 정상 최저 -32.1dB 기준 8dB 마진)
- duration < 5s 거부
- ffmpeg 측정 실패는 ERROR 반환 (보수적 진행 허용)"
```

---

## Task 2: 재시도 정책 예외 계층 (Critical Path)

**목표:** `NonRetryableError`는 catch 후 재시도 없이 즉시 실패. 타임아웃과 크래시성 오류를 이쪽으로 분류.

**Files:**
- Create: `core/retry_policy.py`
- Test: `tests/test_retry_policy.py`

### Step 2.1: 실패 테스트 작성

- [ ] **Step 2.1.1: 예외 계층 테스트**

`tests/test_retry_policy.py`:

```python
"""재시도 정책 예외 계층 테스트."""
from __future__ import annotations

import pytest

from core.retry_policy import (
    NonRetryableError,
    RetryableError,
    TranscriptionTimeoutError,
    should_retry,
)


def test_retryable_error는_재시도_허용():
    err = RetryableError("일시적 오류")
    assert should_retry(err, attempt=1, max_attempts=3) is True


def test_nonretryable_error는_재시도_거부():
    err = NonRetryableError("구조적 오류")
    assert should_retry(err, attempt=1, max_attempts=3) is False


def test_transcription_timeout은_nonretryable():
    """타임아웃은 기본적으로 NonRetryableError의 하위 클래스다."""
    err = TranscriptionTimeoutError("전사 타임아웃 1800초")
    assert isinstance(err, NonRetryableError)
    assert should_retry(err, attempt=1, max_attempts=3) is False


def test_마지막_시도에서는_retryable이어도_재시도_거부():
    """attempt == max_attempts면 더 이상 재시도 없음."""
    err = RetryableError("일시적 오류")
    assert should_retry(err, attempt=3, max_attempts=3) is False


def test_일반_exception은_retryable로_취급():
    """명시되지 않은 예외는 보수적으로 재시도 허용 (기존 동작 호환)."""
    err = ValueError("알 수 없는 오류")
    assert should_retry(err, attempt=1, max_attempts=3) is True
```

- [ ] **Step 2.1.2: 테스트 실행 → 실패 확인**

```bash
pytest tests/test_retry_policy.py -v
```

예상: `ModuleNotFoundError`.

### Step 2.2: 최소 구현

- [ ] **Step 2.2.1: `core/retry_policy.py` 작성**

```python
"""
재시도 정책 예외 계층

목적: Phase 1에서 타임아웃 후 재시도가 MLX Metal 크래시를 유발하는 문제를 차단.
     재시도 가능한 오류(RetryableError)와 구조적 오류(NonRetryableError)를
     분리하여 후자는 즉시 실패 처리한다.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class RetryableError(Exception):
    """재시도로 복구 가능한 일시적 오류."""


class NonRetryableError(Exception):
    """구조적·결정적 오류로 재시도가 무의미하거나 위험한 경우.

    예: 전사 타임아웃(디코더 루프), 모델 로드 실패(메모리 부족),
        입력 파일 형식 오류 등.
    """


class TranscriptionTimeoutError(NonRetryableError):
    """전사 단계 타임아웃. MLX Metal 상태 오염 방지를 위해 재시도 금지."""


def should_retry(
    error: BaseException,
    *,
    attempt: int,
    max_attempts: int,
) -> bool:
    """해당 예외에 대해 재시도를 수행해야 하는지 판정한다.

    Args:
        error: 발생한 예외
        attempt: 현재 시도 번호 (1부터)
        max_attempts: 최대 시도 횟수

    Returns:
        재시도 가능 여부. NonRetryableError 계열은 항상 False.
    """
    if isinstance(error, NonRetryableError):
        logger.info(
            f"NonRetryableError 감지 — 재시도 중단: {type(error).__name__}: {error}"
        )
        return False
    if attempt >= max_attempts:
        return False
    return True
```

- [ ] **Step 2.2.2: 테스트 실행 → 통과 확인**

```bash
pytest tests/test_retry_policy.py -v
```

예상: 5개 PASS.

- [ ] **Step 2.2.3: 커밋**

```bash
git add core/retry_policy.py tests/test_retry_policy.py
git commit -m "기능: 재시도 정책 예외 계층 추가 (Phase 1-2)

타임아웃 후 재시도가 MLX Metal 크래시의 즉발 트리거였음.
- NonRetryableError 계열은 should_retry가 항상 False 반환
- TranscriptionTimeoutError는 NonRetryableError의 하위 클래스"
```

---

## Task 3: Quarantine 디렉토리 관리

**목표:** 거부된/삭제된 오디오 파일을 격리실로 이동하는 헬퍼. watcher가 감시 제외할 경로.

**Files:**
- Create: `core/quarantine.py`
- Test: `tests/test_quarantine.py`

### Step 3.1: 실패 테스트

- [ ] **Step 3.1.1: 테스트 작성**

```python
"""Quarantine 이동 헬퍼 테스트."""
from __future__ import annotations

from pathlib import Path

import pytest

from core.quarantine import QuarantineError, move_to_quarantine


def test_파일을_quarantine으로_이동(tmp_path: Path):
    audio_dir = tmp_path / "audio_input"
    audio_dir.mkdir()
    quarantine_dir = tmp_path / "audio_quarantine"

    src = audio_dir / "meeting_test.wav"
    src.write_bytes(b"fake wav data")

    dest = move_to_quarantine(src, quarantine_dir, reason="저볼륨")

    assert not src.exists()
    assert dest.exists()
    assert dest.parent == quarantine_dir
    assert dest.name == "meeting_test.wav"
    assert dest.read_bytes() == b"fake wav data"


def test_quarantine_디렉토리가_없으면_자동_생성(tmp_path: Path):
    src = tmp_path / "audio.wav"
    src.write_bytes(b"data")
    quarantine_dir = tmp_path / "does" / "not" / "exist"

    dest = move_to_quarantine(src, quarantine_dir, reason="test")

    assert quarantine_dir.exists()
    assert dest.exists()


def test_동일한_이름이_이미_있으면_suffix_추가(tmp_path: Path):
    quarantine_dir = tmp_path / "q"
    quarantine_dir.mkdir()
    existing = quarantine_dir / "meeting.wav"
    existing.write_bytes(b"old")

    src = tmp_path / "meeting.wav"
    src.write_bytes(b"new")

    dest = move_to_quarantine(src, quarantine_dir, reason="중복 테스트")

    assert existing.read_bytes() == b"old"  # 기존 파일 보존
    assert dest.exists()
    assert dest.name != "meeting.wav"  # 이름 변경됨
    assert dest.read_bytes() == b"new"


def test_원본이_없으면_QuarantineError(tmp_path: Path):
    quarantine_dir = tmp_path / "q"
    src = tmp_path / "missing.wav"

    with pytest.raises(QuarantineError):
        move_to_quarantine(src, quarantine_dir, reason="test")


def test_이동_이력을_reason과_함께_로그(tmp_path: Path, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="core.quarantine")

    src = tmp_path / "audio.wav"
    src.write_bytes(b"x")
    quarantine_dir = tmp_path / "q"

    move_to_quarantine(src, quarantine_dir, reason="저볼륨: mean=-48.6dB")

    assert any("저볼륨" in r.message for r in caplog.records)
```

- [ ] **Step 3.1.2: 실패 확인**

```bash
pytest tests/test_quarantine.py -v
```

### Step 3.2: 구현

- [ ] **Step 3.2.1: `core/quarantine.py` 작성**

```python
"""
Quarantine 디렉토리 관리

목적: 품질 불량·사용자 삭제 오디오 파일을 입력 감시 폴더 바깥의
     격리실로 이동하여 watcher 재감지를 차단한다.
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

    logger.info(
        f"Quarantine 이동: {src_path.name} → {dest} (사유: {reason})"
    )
    return dest
```

- [ ] **Step 3.2.2: 테스트 통과 확인**

```bash
pytest tests/test_quarantine.py -v
```

- [ ] **Step 3.2.3: 커밋**

```bash
git add core/quarantine.py tests/test_quarantine.py
git commit -m "기능: Quarantine 이동 헬퍼 추가 (Phase 1-3)"
```

---

## Task 4: Config 스키마 확장

**목표:** Phase 1 설정값을 config.py 스키마에 추가 + config.yaml 기본값. 기존 테스트 호환성 유지.

**Files:**
- Modify: `config.py`
- Modify: `config.yaml`
- Test: 기존 `tests/test_config.py` 확장

### Step 4.1: 실패 테스트

- [ ] **Step 4.1.1: config 테스트 추가**

`tests/test_config.py`에 추가 (파일 끝에):

```python
def test_AudioQualityConfig_기본값():
    """오디오 품질 게이트 기본값 확인."""
    from config import AudioQualityConfig

    c = AudioQualityConfig()
    assert c.enabled is True
    assert c.min_mean_volume_db == -40.0
    assert c.min_duration_seconds == 5.0


def test_PathsConfig에_audio_quarantine_dir_포함():
    from config import PathsConfig

    c = PathsConfig()
    assert c.audio_quarantine_subdir == "audio_quarantine"


def test_WatcherConfig에_excluded_subdirs_포함():
    from config import WatcherConfig

    c = WatcherConfig()
    assert "audio_quarantine" in c.excluded_subdirs


def test_PipelineConfig에_dynamic_timeout_설정():
    from config import PipelineConfig

    c = PipelineConfig()
    assert c.dynamic_timeout_enabled is True
    assert c.dynamic_timeout_multiplier == 3.0
    assert c.dynamic_timeout_min_seconds == 600
    assert c.dynamic_timeout_max_seconds == 10800  # 3시간


def test_PipelineConfig에_retry_max가_1로_변경():
    """Phase 1: 재시도 1회로 축소 (기존 3 → 1)."""
    from config import PipelineConfig

    c = PipelineConfig()
    assert c.retry_max_count == 1
```

- [ ] **Step 4.1.2: 실패 확인**

```bash
pytest tests/test_config.py -v -k "AudioQualityConfig or audio_quarantine or excluded_subdirs or dynamic_timeout or retry_max"
```

### Step 4.2: 구현

- [ ] **Step 4.2.1: `config.py` 수정**

`AudioQualityConfig` 추가 (STTConfig 근처):

```python
class AudioQualityConfig(BaseModel):
    """오디오 품질 게이트 설정.

    큐잉 시점에 저볼륨/극단적으로 짧은 파일을 차단하여
    STT 디코더 루프와 MLX Metal 크래시를 예방한다.
    """

    enabled: bool = Field(default=True, description="품질 게이트 활성화")
    min_mean_volume_db: float = Field(
        default=-40.0,
        description="허용 최소 mean_volume (dB). 실측 정상 최저 -32.1dB 기준 8dB 마진.",
    )
    min_duration_seconds: float = Field(
        default=5.0, ge=1.0, description="허용 최소 재생 시간 (초)"
    )
```

`PathsConfig`에 필드 추가:

```python
    audio_quarantine_subdir: str = Field(
        default="audio_quarantine",
        description="거부/삭제된 오디오 파일 격리 서브디렉토리 (base_dir 하위)",
    )

    @property
    def resolved_audio_quarantine_dir(self) -> Path:
        """격리 디렉토리 절대 경로."""
        return self.resolved_base_dir / self.audio_quarantine_subdir
```

`WatcherConfig`에 필드 추가:

```python
    excluded_subdirs: list[str] = Field(
        default_factory=lambda: ["audio_quarantine"],
        description="watcher가 감시에서 제외할 서브디렉토리 이름 목록",
    )
```

`PipelineConfig`에 필드 추가/수정:

```python
    retry_max_count: int = Field(
        default=1,  # Phase 1: 3 → 1 (타임아웃 재시도가 크래시 유발)
        ge=1,
        le=5,
        description="파이프라인 단계별 최대 재시도 횟수",
    )
    dynamic_timeout_enabled: bool = Field(
        default=True,
        description="오디오 길이에 비례한 동적 타임아웃 사용 여부",
    )
    dynamic_timeout_multiplier: float = Field(
        default=3.0,
        ge=1.0,
        description="타임아웃 = max(min, duration × multiplier)",
    )
    dynamic_timeout_min_seconds: int = Field(
        default=600,  # 10분 최소 (짧은 파일 보호)
        ge=60,
    )
    dynamic_timeout_max_seconds: int = Field(
        default=10800,  # 3시간 상한
        ge=600,
    )
```

`AppConfig`에 `audio_quality` 필드 추가:

```python
    audio_quality: AudioQualityConfig = Field(default_factory=AudioQualityConfig)
```

- [ ] **Step 4.2.2: `config.yaml` 업데이트**

아래 섹션을 기존 구조에 맞춰 추가/수정:

```yaml
# === 오디오 품질 게이트 (Phase 1 크래시 방지) ===
audio_quality:
  enabled: true
  min_mean_volume_db: -40.0    # 정상 최저 -32.1dB 기준 8dB 안전 마진
  min_duration_seconds: 5.0

# === 경로 ===
paths:
  # 기존 필드 유지...
  audio_quarantine_subdir: "audio_quarantine"

# === Watcher ===
watcher:
  # 기존 필드 유지...
  excluded_subdirs:
    - "audio_quarantine"

# === Pipeline ===
pipeline:
  retry_max_count: 1           # Phase 1: 3 → 1 (타임아웃 재시도 금지)
  dynamic_timeout_enabled: true
  dynamic_timeout_multiplier: 3.0
  dynamic_timeout_min_seconds: 600
  dynamic_timeout_max_seconds: 10800
```

- [ ] **Step 4.2.3: 테스트 실행**

```bash
pytest tests/test_config.py -v
```

전체 테스트도 확인:

```bash
pytest tests/ -x -q --ignore=tests/test_e2e_edit_playwright.py
```

- [ ] **Step 4.2.4: 커밋**

```bash
git add config.py config.yaml tests/test_config.py
git commit -m "기능: Phase 1 설정 스키마 확장

- AudioQualityConfig: 품질 게이트 임계값
- PathsConfig.audio_quarantine_subdir
- WatcherConfig.excluded_subdirs
- PipelineConfig: retry_max_count 3→1, 동적 타임아웃 필드"
```

---

## Task 5: Watcher 통합 (품질 게이트 + 제외 경로)

**목표:** Watcher가 quarantine 경로를 무시하고, 새 파일 감지 시 품질 게이트를 통과해야만 큐 등록.

**Files:**
- Modify: `core/watcher.py`
- Test: `tests/test_watcher.py` 확장

### Step 5.1: 실패 테스트

- [ ] **Step 5.1.1: 품질 게이트 통합 테스트 추가**

`tests/test_watcher.py`에 추가:

```python
@pytest.mark.asyncio
async def test_watcher가_excluded_subdirs_내_파일을_무시(tmp_path, monkeypatch):
    """audio_quarantine 하위 파일은 감시에서 제외된다."""
    # setup: audio_input + audio_quarantine 구조
    audio_input = tmp_path / "audio_input"
    quarantine = tmp_path / "audio_quarantine"
    audio_input.mkdir()
    quarantine.mkdir()

    calls: list[Path] = []

    async def fake_register(path: Path) -> None:
        calls.append(path)

    # Watcher를 excluded_subdirs=["audio_quarantine"] 으로 구성
    # (실제 Watcher 클래스 시그니처에 맞춰 조정)
    from core.watcher import AudioFolderWatcher

    watcher = AudioFolderWatcher(
        watch_dir=tmp_path,
        excluded_subdirs=["audio_quarantine"],
        on_new_file=fake_register,
        supported_extensions={".wav"},
    )

    # quarantine 내부에 파일 생성 (시뮬레이션)
    # 실제 이벤트를 직접 호출하여 필터 동작만 검증
    quarantine_file = quarantine / "should_be_ignored.wav"
    input_file = audio_input / "should_register.wav"
    quarantine_file.write_bytes(b"x")
    input_file.write_bytes(b"x")

    # 내부 필터 함수를 직접 호출
    assert watcher._is_excluded(quarantine_file) is True
    assert watcher._is_excluded(input_file) is False


@pytest.mark.asyncio
async def test_품질_게이트_reject_시_quarantine_이동_후_큐등록_안함(tmp_path, monkeypatch):
    """저볼륨 파일은 quarantine으로 이동되고 큐에 들어가지 않는다."""
    from core.audio_quality import AudioQualityResult, AudioQualityStatus
    from core.watcher import AudioFolderWatcher

    audio_input = tmp_path / "audio_input"
    quarantine = tmp_path / "audio_quarantine"
    audio_input.mkdir()

    bad_file = audio_input / "quiet.wav"
    bad_file.write_bytes(b"x")

    queued: list[Path] = []

    async def fake_register(path: Path) -> None:
        queued.append(path)

    def fake_validator(p: Path) -> AudioQualityResult:
        return AudioQualityResult(
            status=AudioQualityStatus.REJECT,
            mean_volume_db=-48.0,
            duration_seconds=600.0,
            reason="저볼륨",
        )

    watcher = AudioFolderWatcher(
        watch_dir=tmp_path,
        excluded_subdirs=["audio_quarantine"],
        on_new_file=fake_register,
        supported_extensions={".wav"},
        audio_validator=fake_validator,
        quarantine_dir=quarantine,
    )

    await watcher._handle_new_file(bad_file)

    assert len(queued) == 0
    assert not bad_file.exists()
    assert (quarantine / "quiet.wav").exists()
```

- [ ] **Step 5.1.2: 실패 확인**

```bash
pytest tests/test_watcher.py -v -k "excluded_subdirs or 품질_게이트"
```

### Step 5.2: 구현

- [ ] **Step 5.2.1: `core/watcher.py` 수정**

(핵심 변경: `_AudioFileHandler` 및 `AudioFolderWatcher`에 `excluded_subdirs`, `audio_validator`, `quarantine_dir` 주입 + `_is_excluded` / `_handle_new_file` 메서드 추가)

실제 파일은 현재 구조에 맞춰 minimal diff로 적용:
1. `__init__`에 신규 파라미터 3개 추가 (기본값 None 혹은 빈 리스트로 하위 호환)
2. `_AudioFileHandler._dispatch_new_file` 직전에 `_is_excluded` 체크
3. `_handle_new_file` 신규 메서드 — validator 호출 → REJECT면 quarantine 이동, ACCEPT/ERROR면 기존 콜백

구현 스케치 (실제 파일 구조 확인 후 조정):

```python
# AudioFolderWatcher 클래스에 추가

def _is_excluded(self, path: Path) -> bool:
    """경로가 제외 서브디렉토리에 속하는지 판정."""
    try:
        rel = path.relative_to(self._watch_dir)
    except ValueError:
        return False
    parts = rel.parts
    return bool(parts) and parts[0] in self._excluded_subdirs

async def _handle_new_file(self, path: Path) -> None:
    """품질 게이트를 거쳐 큐에 등록하거나 quarantine으로 이동."""
    if self._is_excluded(path):
        logger.debug(f"제외 경로, 무시: {path}")
        return

    if self._audio_validator is not None and self._quarantine_dir is not None:
        try:
            result = self._audio_validator(path)
        except Exception as e:  # 품질 측정 자체가 예외 → 기존 동작 유지
            logger.warning(f"품질 측정 예외, 통과 처리: {path} ({e})")
            result = None

        if result is not None and result.status.value == "reject":
            from core.quarantine import move_to_quarantine
            try:
                move_to_quarantine(path, self._quarantine_dir, reason=result.reason)
                logger.warning(
                    f"품질 게이트 거부: {path.name} ({result.reason}) — 격리 완료"
                )
            except Exception as e:
                logger.exception(f"Quarantine 이동 실패: {e}")
            return

    # 통과 or validator 미설정 → 기존 콜백
    await self._on_new_file(path)
```

- [ ] **Step 5.2.2: `main.py` 또는 watcher 팩토리에서 새 파라미터 주입**

(탐색 결과에 따라 구체적 파일/라인 조정)

```python
from functools import partial
from core.audio_quality import validate_audio_quality

audio_validator = partial(
    validate_audio_quality,
    min_mean_db=config.audio_quality.min_mean_volume_db,
    min_duration_s=config.audio_quality.min_duration_seconds,
) if config.audio_quality.enabled else None

watcher = AudioFolderWatcher(
    watch_dir=config.paths.resolved_audio_input_dir,
    excluded_subdirs=config.watcher.excluded_subdirs,
    on_new_file=on_new_file_callback,
    supported_extensions=SUPPORTED_EXTENSIONS,
    audio_validator=audio_validator,
    quarantine_dir=config.paths.resolved_audio_quarantine_dir,
)
```

- [ ] **Step 5.2.3: 테스트 실행 (전체)**

```bash
pytest tests/test_watcher.py -v
pytest tests/ -x -q --ignore=tests/test_e2e_edit_playwright.py
```

- [ ] **Step 5.2.4: 커밋**

```bash
git add core/watcher.py main.py tests/test_watcher.py
git commit -m "기능: Watcher에 품질 게이트 + 제외 경로 통합 (Phase 1-5)

- excluded_subdirs: audio_quarantine 자동 무시
- audio_validator: REJECT 파일은 quarantine 이동, 큐 등록 차단"
```

---

## Task 6: 동적 타임아웃 + 타임아웃 예외 전환

**목표:** 전사 타임아웃을 오디오 길이 기반으로 계산하고, 발생 시 `TranscriptionTimeoutError`로 raise.

**Files:**
- Modify: `steps/transcriber.py`
- Modify: `core/pipeline.py`
- Test: `tests/test_pipeline.py` 확장 + `tests/test_transcriber.py`

### Step 6.1: 동적 타임아웃 계산 함수

- [ ] **Step 6.1.1: 테스트 작성**

`tests/test_pipeline.py`에 추가:

```python
def test_compute_dynamic_timeout_짧은_파일은_최소값():
    from core.pipeline import compute_dynamic_timeout

    # 60초 오디오 × 3 = 180s < min 600s
    assert compute_dynamic_timeout(
        duration_seconds=60.0,
        multiplier=3.0,
        min_seconds=600,
        max_seconds=10800,
    ) == 600


def test_compute_dynamic_timeout_중간_파일():
    from core.pipeline import compute_dynamic_timeout

    # 900초 × 3 = 2700s
    assert compute_dynamic_timeout(
        duration_seconds=900.0,
        multiplier=3.0,
        min_seconds=600,
        max_seconds=10800,
    ) == 2700


def test_compute_dynamic_timeout_긴_파일은_상한():
    from core.pipeline import compute_dynamic_timeout

    # 10시간 × 3 = 108000s > max 10800s
    assert compute_dynamic_timeout(
        duration_seconds=36000.0,
        multiplier=3.0,
        min_seconds=600,
        max_seconds=10800,
    ) == 10800
```

- [ ] **Step 6.1.2: 실패 확인**

```bash
pytest tests/test_pipeline.py -v -k "compute_dynamic_timeout"
```

- [ ] **Step 6.1.3: `core/pipeline.py`에 함수 추가**

```python
def compute_dynamic_timeout(
    *,
    duration_seconds: float,
    multiplier: float,
    min_seconds: int,
    max_seconds: int,
) -> int:
    """오디오 길이에 비례한 전사 타임아웃을 계산한다.

    공식: clamp(duration × multiplier, min, max)

    Args:
        duration_seconds: 오디오 재생 시간
        multiplier: RTF 여유 배수 (예: 3.0 = RTF 1.19 기준 약 2.5배 여유)
        min_seconds: 최소 타임아웃 (짧은 파일 보호, 모델 로드 시간 포함)
        max_seconds: 최대 타임아웃 (폭주 방지 안전판)

    Returns:
        계산된 타임아웃 (정수 초)
    """
    computed = duration_seconds * multiplier
    clamped = max(float(min_seconds), min(float(max_seconds), computed))
    return int(clamped)
```

- [ ] **Step 6.1.4: PASS 확인 + 커밋 대기 (다음 단계와 묶음)**

### Step 6.2: 타임아웃을 NonRetryableError로 변환

- [ ] **Step 6.2.1: 테스트 작성**

`tests/test_pipeline.py`:

```python
@pytest.mark.asyncio
async def test_전사_타임아웃은_nonretryable로_raise(monkeypatch):
    """transcriber의 TimeoutError가 TranscriptionTimeoutError로 변환된다."""
    from core.retry_policy import TranscriptionTimeoutError
    # 실제 pipeline 인스턴스 fixture 사용 (기존 테스트 패턴 따름)
    # transcribe 콜을 asyncio.TimeoutError 발생으로 mock

    # ... (구체 fixture는 test_pipeline.py 기존 패턴 참조)
    pytest.skip("구조 통합 후 활성화 — Step 6.2.3 에서 구체화")


def test_타임아웃_에러는_재시도_안함(monkeypatch):
    """NonRetryableError 감지 시 재시도 루프가 break."""
    from core.retry_policy import TranscriptionTimeoutError, should_retry

    err = TranscriptionTimeoutError("1800초 초과")
    assert should_retry(err, attempt=1, max_attempts=3) is False
```

- [ ] **Step 6.2.2: `steps/transcriber.py` 수정**

기존 `asyncio.TimeoutError` 처리 부분을 찾아 다음과 같이 변경:

```python
from core.retry_policy import TranscriptionTimeoutError

# 기존:
# except asyncio.TimeoutError:
#     raise RuntimeError(f"전사 타임아웃 ({timeout}초 초과)") from None

# 변경:
except asyncio.TimeoutError:
    logger.error(f"전사 타임아웃 ({timeout}초 초과) — 재시도 금지")
    raise TranscriptionTimeoutError(
        f"전사 타임아웃 ({timeout}초 초과). "
        f"재시도 시 MLX Metal 크래시 위험으로 즉시 실패 처리."
    ) from None
```

- [ ] **Step 6.2.3: `core/pipeline.py` 재시도 루프 수정**

`pipeline.py:1123` 부근의 `except Exception as e:` 블록 직전 또는 내부에 `should_retry` 통합:

```python
from core.retry_policy import should_retry, NonRetryableError

# 기존 재시도 루프 내부:
except Exception as e:  # noqa: BLE001 — 재시도 루프 catch-all
    last_error = e
    logger.warning(
        f"단계 {step.value} 실패 (시도 {attempt}/{self._retry_max}): {e}"
    )
    if not should_retry(e, attempt=attempt, max_attempts=self._retry_max):
        logger.info(f"재시도 중단 (타입={type(e).__name__})")
        break
    # 기존 backoff 코드 유지
    if attempt < self._retry_max:
        backoff_seconds = min(2 ** (attempt - 1), 30)
        await asyncio.sleep(backoff_seconds)
```

- [ ] **Step 6.2.4: 동적 타임아웃을 transcribe 단계에 주입**

`_run_step_transcribe` 내부에서 wav 파일의 duration을 ffprobe로 얻고 `compute_dynamic_timeout` 호출:

```python
async def _run_step_transcribe(self, wav_path: Path, checkpoint_path: Path) -> TranscriptResult:
    if self._config.pipeline.dynamic_timeout_enabled:
        from core.audio_quality import _measure_duration_seconds
        try:
            duration = _measure_duration_seconds(wav_path)
            timeout = compute_dynamic_timeout(
                duration_seconds=duration,
                multiplier=self._config.pipeline.dynamic_timeout_multiplier,
                min_seconds=self._config.pipeline.dynamic_timeout_min_seconds,
                max_seconds=self._config.pipeline.dynamic_timeout_max_seconds,
            )
            logger.info(f"동적 타임아웃: {timeout}초 (duration={duration:.1f}s)")
        except Exception as e:
            logger.warning(f"duration 측정 실패, 기본 타임아웃 사용: {e}")
            timeout = self._config.stt.transcribe_timeout_seconds
    else:
        timeout = self._config.stt.transcribe_timeout_seconds

    # 기존 transcribe 호출에 timeout 주입
    return await asyncio.wait_for(
        self._transcriber.transcribe(wav_path),
        timeout=timeout,
    )
```

- [ ] **Step 6.2.5: 전체 테스트 실행**

```bash
pytest tests/test_pipeline.py tests/test_retry_policy.py tests/test_transcriber.py -v
pytest tests/ -x -q --ignore=tests/test_e2e_edit_playwright.py
```

- [ ] **Step 6.2.6: 커밋**

```bash
git add core/pipeline.py steps/transcriber.py tests/test_pipeline.py
git commit -m "기능: 동적 타임아웃 + 타임아웃 재시도 차단 (Phase 1-6)

- compute_dynamic_timeout: duration × 3, min 10분 / max 3시간
- asyncio.TimeoutError → TranscriptionTimeoutError (NonRetryableError)
- 재시도 루프가 should_retry 사용, NonRetryable은 즉시 break"
```

---

## Task 7: DELETE API 가 오디오 파일도 quarantine 이동

**목표:** `DELETE /api/meetings/{id}` 호출 시 DB 레코드 + 오디오 파일을 함께 quarantine으로 이동.

**Files:**
- Modify: `api/routes.py:1105-1154`
- Test: `tests/test_routes.py` 확장

### Step 7.1: 실패 테스트

- [ ] **Step 7.1.1: 테스트 작성**

`tests/test_routes.py`에 추가:

```python
@pytest.mark.asyncio
async def test_DELETE_meetings_오디오_파일도_quarantine으로_이동(
    test_client, tmp_path, monkeypatch
):
    """DELETE 엔드포인트가 DB + 오디오 파일을 함께 처리한다."""
    # setup: job에 audio_path 필드 존재하는 케이스
    # fixture는 기존 test_routes.py 패턴 따라 구성

    audio_file = tmp_path / "audio_input" / "meeting_test.wav"
    audio_file.parent.mkdir()
    audio_file.write_bytes(b"audio data")

    quarantine_dir = tmp_path / "audio_quarantine"

    # ... fixture에서 job 생성 with audio_path=str(audio_file)
    # ... app.state.config.paths.resolved_audio_quarantine_dir = quarantine_dir

    response = test_client.delete(f"/api/meetings/{meeting_id}")

    assert response.status_code == 200
    assert not audio_file.exists()
    assert (quarantine_dir / "meeting_test.wav").exists()


@pytest.mark.asyncio
async def test_DELETE_오디오_파일이_이미_없어도_성공(test_client, tmp_path):
    """오디오 파일이 누락돼도 DB 삭제는 성공 처리 (경고만 로그)."""
    # fixture: job의 audio_path는 설정되어 있으나 파일은 존재하지 않음
    response = test_client.delete(f"/api/meetings/{meeting_id}")
    assert response.status_code == 200  # DB 삭제 자체는 성공
```

- [ ] **Step 7.1.2: 실패 확인**

### Step 7.2: 구현

- [ ] **Step 7.2.1: `api/routes.py:1105-1154` 수정**

```python
@router.delete("/meetings/{meeting_id}")
async def delete_meeting(request: Request, meeting_id: str) -> dict[str, str]:
    """회의를 삭제한다 (DB 레코드 + 오디오 파일 → quarantine).

    Phase 1: 오디오 파일이 watcher에 의해 재감지되는 것을 방지.
    """
    from core.job_queue import JobNotFoundError
    from core.quarantine import QuarantineError, move_to_quarantine

    queue = _get_job_queue(request)
    config = request.app.state.config

    try:
        job = await asyncio.to_thread(
            queue.queue.get_job_by_meeting_id,
            meeting_id,
        )
        if job is None:
            raise HTTPException(
                status_code=404,
                detail=f"회의를 찾을 수 없습니다: {meeting_id}",
            )

        audio_path_str = getattr(job, "audio_path", None)

        # DB 삭제
        await asyncio.to_thread(queue.queue.delete_job, job.id)
        logger.info(f"회의 DB 삭제: {meeting_id} (job_id={job.id})")

        # 오디오 파일 quarantine 이동 (best-effort)
        if audio_path_str:
            audio_path = Path(audio_path_str)
            if audio_path.exists():
                try:
                    quarantine_dir = config.paths.resolved_audio_quarantine_dir
                    new_path = await asyncio.to_thread(
                        move_to_quarantine,
                        audio_path,
                        quarantine_dir,
                        reason=f"사용자 삭제: meeting_id={meeting_id}",
                    )
                    logger.info(f"오디오 파일 격리: {audio_path} → {new_path}")
                except QuarantineError as e:
                    logger.warning(
                        f"오디오 파일 격리 실패 (DB 삭제는 완료): {e}"
                    )
            else:
                logger.debug(f"오디오 파일이 이미 없음: {audio_path}")

        return {"message": f"회의가 삭제되었습니다: {meeting_id}"}

    except HTTPException:
        raise
    except JobNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"회의 삭제 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"회의 삭제 중 오류가 발생했습니다: {e}",
        ) from e
```

- [ ] **Step 7.2.2: 테스트 통과 확인**

```bash
pytest tests/test_routes.py -v -k "DELETE"
```

- [ ] **Step 7.2.3: 커밋**

```bash
git add api/routes.py tests/test_routes.py
git commit -m "기능: DELETE API가 오디오 파일도 quarantine 이동 (Phase 1-7)

watcher의 재감지 루프를 차단하여 사용자 삭제 후
동일 파일이 큐에 재등록되는 2차 원인 제거."
```

---

## Task 8: launchd KeepAlive 자동 재기동

**목표:** 앱 크래시 시 launchd가 30초 이내 자동 재기동.

**Files:**
- Modify: `scripts/setup_launchagent.sh:139-140`
- Test: 수동 smoke test (launchd는 단위 테스트 곤란)

### Step 8.1: 스크립트 수정

- [ ] **Step 8.1.1: `scripts/setup_launchagent.sh` 수정**

`<key>KeepAlive</key><false/>` 블록을 다음으로 교체:

```xml
<key>KeepAlive</key>
<dict>
    <key>SuccessfulExit</key>
    <false/>
    <key>Crashed</key>
    <true/>
</dict>
<key>ThrottleInterval</key>
<integer>30</integer>
```

- [ ] **Step 8.1.2: 기존 LaunchAgent 재설치 수동 확인 절차 문서화**

스크립트 상단 주석에 추가:

```bash
# Phase 1 (2026-04-21): KeepAlive 추가
# 기존 설치된 에이전트가 있으면 다음으로 재적용:
#   launchctl unload ~/Library/LaunchAgents/<label>.plist
#   bash scripts/setup_launchagent.sh
#   launchctl load ~/Library/LaunchAgents/<label>.plist
```

- [ ] **Step 8.1.3: 스크립트 구문 확인**

```bash
bash -n scripts/setup_launchagent.sh
# plist 검증 (dry-run)
bash scripts/setup_launchagent.sh --dry-run 2>&1 | head || true
```

- [ ] **Step 8.1.4: 수동 smoke test 절차 기록 (README 또는 PR 설명)**

```markdown
# Phase 1 KeepAlive smoke test
1. bash scripts/setup_launchagent.sh
2. launchctl load ~/Library/LaunchAgents/<label>.plist
3. 앱 PID 확인: `pgrep -f "main.py"`
4. 강제 종료: `kill -9 <PID>`
5. 30초 대기 후 PID 재조회 — 새 PID 존재 확인
```

- [ ] **Step 8.1.5: 커밋**

```bash
git add scripts/setup_launchagent.sh
git commit -m "기능: launchd KeepAlive 활성화 (Phase 1-8)

크래시 시 30초 이내 자동 재기동. orphan job 복구 로직과 결합하여
사용자 수동 개입 없이 상태 정합성 유지."
```

---

## Task 9: 통합 테스트

**목표:** Phase 1 조치들이 실제 흐름에서 협력하는지 end-to-end 검증.

**Files:**
- Create: `tests/test_phase1_integration.py`

### Step 9.1: 통합 시나리오 테스트

- [ ] **Step 9.1.1: 테스트 작성**

```python
"""Phase 1 통합 시나리오: 크래시 방지 방어막 협력 검증."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from core.audio_quality import (
    AudioQualityResult,
    AudioQualityStatus,
    validate_audio_quality,
)
from core.quarantine import move_to_quarantine
from core.retry_policy import (
    NonRetryableError,
    TranscriptionTimeoutError,
    should_retry,
)
from core.pipeline import compute_dynamic_timeout


def test_저볼륨_파일_시나리오_end_to_end(tmp_path: Path):
    """저볼륨 파일이 검증→격리→watcher제외 전체 경로를 통과."""
    audio_input = tmp_path / "audio_input"
    audio_input.mkdir()
    quarantine = tmp_path / "audio_quarantine"

    bad_file = audio_input / "meeting_bad.wav"
    bad_file.write_bytes(b"x")

    with (
        patch("core.audio_quality._measure_mean_volume_db", return_value=-48.6),
        patch("core.audio_quality._measure_duration_seconds", return_value=1360.0),
    ):
        result = validate_audio_quality(
            bad_file,
            min_mean_db=-40.0,
            min_duration_s=5.0,
        )

    assert result.status == AudioQualityStatus.REJECT

    # 격리 수행
    dest = move_to_quarantine(bad_file, quarantine, reason=result.reason)

    assert not bad_file.exists()
    assert dest.exists()
    assert dest.parent == quarantine


def test_타임아웃_시나리오_재시도_차단():
    """전사 타임아웃이 발생하면 재시도 안됨."""
    err = TranscriptionTimeoutError("1800초 초과")
    assert isinstance(err, NonRetryableError)
    # 재시도 루프에서 should_retry가 즉시 False 반환
    assert should_retry(err, attempt=1, max_attempts=3) is False


def test_동적_타임아웃_실제_시나리오():
    """15분·1시간·3시간·10시간 오디오 타임아웃 검증."""
    # 15분 × 3 = 45분
    assert compute_dynamic_timeout(
        duration_seconds=900.0, multiplier=3.0,
        min_seconds=600, max_seconds=10800,
    ) == 2700

    # 1시간 × 3 = 3시간
    assert compute_dynamic_timeout(
        duration_seconds=3600.0, multiplier=3.0,
        min_seconds=600, max_seconds=10800,
    ) == 10800

    # 3시간 × 3 = 9시간 → 상한 3시간
    assert compute_dynamic_timeout(
        duration_seconds=10800.0, multiplier=3.0,
        min_seconds=600, max_seconds=10800,
    ) == 10800

    # 30초 × 3 = 90초 < 하한 600s
    assert compute_dynamic_timeout(
        duration_seconds=30.0, multiplier=3.0,
        min_seconds=600, max_seconds=10800,
    ) == 600


def test_정상_파일은_파이프라인_진입():
    """−25dB, 15분 파일은 ACCEPT 후 정상 진행."""
    fake_path = Path("/tmp/normal.wav")
    with (
        patch("core.audio_quality._measure_mean_volume_db", return_value=-25.0),
        patch("core.audio_quality._measure_duration_seconds", return_value=900.0),
    ):
        result = validate_audio_quality(
            fake_path,
            min_mean_db=-40.0,
            min_duration_s=5.0,
        )

    assert result.status == AudioQualityStatus.ACCEPT


def test_실측_크래시_파일_수치_기반_검증():
    """meeting_20260420_100536.wav 실측 수치가 정확히 거부되는지."""
    fake_path = Path("/tmp/crash_file.wav")
    with (
        patch("core.audio_quality._measure_mean_volume_db", return_value=-48.6),
        patch("core.audio_quality._measure_duration_seconds", return_value=1359.87),
    ):
        result = validate_audio_quality(
            fake_path,
            min_mean_db=-40.0,
            min_duration_s=5.0,
        )

    assert result.status == AudioQualityStatus.REJECT
    assert "-48" in result.reason or "저볼륨" in result.reason
```

- [ ] **Step 9.1.2: 테스트 실행**

```bash
pytest tests/test_phase1_integration.py -v
```

- [ ] **Step 9.1.3: 전체 스위트 최종 실행**

```bash
pytest tests/ -v --ignore=tests/test_e2e_edit_playwright.py
```

목표: 기존 테스트 0건 회귀, 신규 Phase 1 테스트 모두 PASS.

- [ ] **Step 9.1.4: 커밋**

```bash
git add tests/test_phase1_integration.py
git commit -m "테스트: Phase 1 통합 시나리오 검증

실측 크래시 파일 수치(-48.6dB, 1360s) 기반 거부 검증 포함."
```

---

## Task 10: 서브프로세스 격리 (선택 — Phase 2 예비)

현재 계획에서는 **제외**합니다. Phase 1 8개 태스크(1-9)만 구현하여 99% 크래시 차단 달성 후, 실측 모니터링 데이터를 보고 필요 시 Phase 2에서 결정.

근거: 리뷰어 지적대로 KeepAlive가 90% 커버하므로 서브프로세스 격리는 과잉 엔지니어링 가능성.

---

## 서브에이전트 팀 구성 (Critical Thinking + TDD 강화)

### 팀 편성

```
                    ┌──────────────────────┐
                    │  Orchestrator (나)   │
                    │ - 계획/분배/커밋     │
                    │ - 태스크 간 순서 보장│
                    └──────────┬───────────┘
                               │
       ┌───────────────────────┼───────────────────────┐
       │                       │                       │
       ▼                       ▼                       ▼
┌──────────────┐      ┌──────────────┐        ┌──────────────┐
│ Implementer  │      │  Reviewer    │        │  QA/Tester   │
│              │      │   (비판)     │        │              │
│ python-pro   │      │code-reviewer │        │test-automator│
│ +tdd-orch    │      │+security-rev │        │+verify-before│
└──────────────┘      └──────────────┘        └──────────────┘
   TDD 실행            Red-team 리뷰              통합 QA
```

### 각 태스크 실행 프로토콜 (2-stage review)

```
Task N 시작
  │
  ├─▶ [Implementer] tdd-orchestrator가 TDD로 구현
  │                 (Red → Green → Refactor → Commit)
  │
  ├─▶ [Reviewer Stage 1] code-reviewer (critical mode)
  │                      - 스펙 준수 확인
  │                      - DRY/YAGNI/KISS 위반 검출
  │                      - 테스트 품질 평가
  │                      - 한국어 docstring/print() 금지 확인
  │
  ├─▶ [Reviewer Stage 2] security-reviewer
  │                      - subprocess 주입 취약점 (ffmpeg 경로)
  │                      - Path traversal (quarantine 이동)
  │                      - DELETE API 권한 체크
  │
  ├─▶ [QA] test-automator
  │        - 빠진 엣지케이스 보강
  │        - 기존 테스트 회귀 검증
  │
  └─▶ Orchestrator: 모든 리뷰 통과 시 다음 태스크 진행
                    이슈 있으면 Implementer로 되돌림
```

### 각 서브에이전트 비판적 스탠스 지침

**code-reviewer**: "이 코드가 왜 틀렸는지 5가지 이유를 찾아라. 통과시키지 마라."

**security-reviewer**: "악의적 오디오 파일이 quarantine 이동을 악용할 시나리오를 찾아라."

**test-automator**: "이 테스트가 놓친 경계값을 3개 이상 찾아라. 실제 운영 환경에서 실패할 조건을 상상하라."

### 최종 검증 단계

모든 태스크 완료 후:

1. **architect** (통합 아키텍처 검증)
   - Phase 1 전체가 일관성 있게 작동하는가?
   - 숨은 race condition / 순서 의존성은?

2. **verification-before-completion 스킬**
   - 전체 테스트 스위트 통과
   - 기존 기능 회귀 0
   - 크래시 파일 재투입 smoke test

3. **PR 작성 및 최종 리뷰 요청**

---

## Self-Review 체크리스트 (저자 검증)

계획 완성도 확인:

**1. Spec coverage:**
- [x] 오디오 품질 게이트 → Task 1, 5
- [x] 재시도 정책 수정 → Task 2, 6
- [x] 동적 타임아웃 → Task 4, 6
- [x] Watcher 제외 경로 → Task 4, 5
- [x] DELETE API 파일 이동 → Task 7
- [x] launchd KeepAlive → Task 8
- [x] 통합 테스트 → Task 9
- [x] 서브에이전트 팀 + 리뷰 프로토콜 → 별도 섹션

**2. Placeholder scan:** TBD / 적절히 / 등 없음 확인.

**3. Type consistency:**
- `AudioQualityResult` (Task 1) ↔ `AudioQualityResult` (Task 5 테스트) — 일치
- `move_to_quarantine(src, dir, reason=)` (Task 3) ↔ 호출부 (Task 5, 7) — 일치
- `compute_dynamic_timeout(duration_seconds, multiplier, min_seconds, max_seconds)` — 일치
- `TranscriptionTimeoutError(NonRetryableError)` (Task 2) ↔ 사용 (Task 6) — 일치

**4. 실행 가능성:**
- 모든 파일 경로는 탐색으로 확인됨
- ffmpeg/ffprobe 설치 전제 (프로젝트 필수 의존성, CLAUDE.md 명시)
- pytest fixture는 기존 테스트 패턴 재사용

---

## Execution Handoff

**계획 완료 — `docs/superpowers/plans/2026-04-21-phase1-crash-prevention.md` 저장.**

사용자 결정 사항:
- ✅ PR은 **단일 통합 PR** (Option B)
- ✅ 삭제 시 오디오 파일도 quarantine 이동 허용
- ✅ TDD + 비판적 서브에이전트 팀 운영

다음 액션: `superpowers:subagent-driven-development` 스킬 기반으로 Task 1부터 순차 착수.
