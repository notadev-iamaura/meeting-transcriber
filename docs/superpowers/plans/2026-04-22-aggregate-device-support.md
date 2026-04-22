# Plan: Aggregate Device를 녹음 소스로 사용할 수 있게 지원

- **작성일**: 2026-04-22
- **제보자 노트**: 녹음된 파일을 실제로 들어봤을 때 본인 목소리 누락 확인
- **우선순위**: High — 기본 구성에서 **사용자 본인 목소리 미녹음**의 직접 원인
- **라벨**: bug, recording, audio
- **관련 브랜치**: `feat/aggregate-device-support`

---

## 0. TL;DR

`steps/recorder.py` 가 macOS Aggregate Device 를 `virtual_keywords` 필터로 **가상 장치로 잘못 분류**하여 선택 대상에서 제외한다. 그 결과 `prefer_system_audio: true` 구성에서 BlackHole 만 녹음되고 본인 마이크 입력이 녹음 파일에 포함되지 않는다.

**해결책**: Aggregate Device 를 별도 플래그(`is_aggregate`) 로 식별하고, 장치 선택 우선순위에서 **BlackHole 보다 먼저** 선택하도록 변경한다.

---

## 1. 배경 — 왜 문제가 되는가

### 현재 동작
- 기본 구성(`recording.prefer_system_audio: true`, `multi_track: false`) 은 BlackHole 2ch 를 녹음 장치로 선택한다.
- BlackHole 은 Zoom 등 앱의 **출력 오디오(=상대방 목소리)만** 받는 루프백 장치다.
- 본인 마이크 입력은 Zoom 으로만 전달되고 BlackHole 에는 흐르지 않으므로 **녹음 파일에서 본인 목소리가 완전히 누락**된다.

### 실제 확인된 영향
- 2026-04-22 운영 환경에서, 사용자가 녹음된 파일을 들어보고 "본인 목소리가 하나도 녹음 안 됐다" 고 보고.
- 분석 결과: 113 건의 완료 회의 중 본인이 발화한 부분은 전부 pyannote 화자분리 시점에 `UNKNOWN` 또는 잘못된 화자로 처리되거나, 아예 녹음 자체가 없었음.
- 직전 QA 에서 확인한 "전사 누락 18%" 중 일부도 이 문제의 2차 증상으로 판단됨 (본인 발화 구간이 처음부터 녹음되지 않음 → 전사 대상 자체가 없음).

### 해결 방향
macOS Aggregate Device 기능을 사용해 `MacBook 내장 마이크 + BlackHole 2ch` 를 하나의 가상 입력 장치로 묶으면, **한 번의 녹음에 본인 목소리(ch0) + 상대방 목소리(ch1/2)** 가 모두 포함된 3채널 WAV 가 생성된다. 현재 ffmpeg 녹음 경로(`-f avfoundation -i ":Device Name"`) 는 이 장치를 그대로 사용할 수 있다.

**남은 문제는 앱 코드가 Aggregate 장치를 인식·선택하도록 되어 있지 않다는 점**이다.

---

## 2. 근본 원인 — 코드 분석

### 문제 지점 1: `aggregate` 가 virtual_keywords 에 들어있음

**파일**: `steps/recorder.py:353~363`

```python
# 현재 코드
is_blackhole = "blackhole" in name.lower()
# 가상 장치 감지 (BlackHole은 별도 is_blackhole로 처리)
virtual_keywords = [
    "zoom",
    "virtual",
    "aggregate",     # ← 이것 때문에 Aggregate Device가 is_virtual=True로 태깅됨
    "soundflower",
    "loopback",
]
name_lower = name.lower()
is_virtual = any(kw in name_lower for kw in virtual_keywords)
```

→ 사용자가 `Meeting Transcriber Aggregate` 라는 이름의 Aggregate Device 를 만들면 `is_virtual=True` 로 판정되어 **실제 장치 목록에서 제외**된다.

### 문제 지점 2: 장치 선택 우선순위에 Aggregate 항목이 없음

**파일**: `steps/recorder.py:387~433` (`_select_audio_device()`)

현재 우선순위:

1. BlackHole 우선 (`prefer_system_audio=True` 일 때)
2. 가상 장치 제외한 실제 장치
3. 마이크 키워드 매칭
4. 실제 장치 중 첫 번째
5. 가상 장치 폴백

Aggregate Device 가 설령 `is_virtual=False` 로 태깅된다 하더라도, **BlackHole 이 먼저 선택**되므로 본인 목소리는 여전히 포함되지 않는다.

### 문제 지점 3: `AudioDevice` 데이터클래스에 Aggregate 구별 필드 없음

**파일**: `steps/recorder.py:95~116`

```python
@dataclass
class AudioDevice:
    index: int
    name: str
    is_blackhole: bool = False   # BlackHole 전용 플래그
    is_virtual: bool = False     # ZoomAudioDevice, SoundFlower 등
    # is_aggregate 플래그 없음  ← 이것이 없어서 Aggregate를 구분할 방법이 없음
```

---

## 3. 수정 제안

### 변경 요약 (3 개 파일)

| 파일 | 변경 내용 |
|------|----------|
| `steps/recorder.py` | `AudioDevice` 에 `is_aggregate` 필드 추가 + 감지 로직 + 선택 우선순위 상위화 |
| `config.py` | `RecordingConfig` 에 `preferred_device_name` 신규 옵션 추가 (선택적, 명시적 지정용) |
| `config.yaml` | 신규 옵션 기본값 주석 (기본 사용에는 영향 없음) |

### 상세 패치

#### 3.1 `steps/recorder.py` — AudioDevice 확장

```python
@dataclass
class AudioDevice:
    """오디오 장치 정보를 담는 데이터 클래스.

    Attributes:
        index: ffmpeg AVFoundation 장치 인덱스
        name: 장치 이름
        is_blackhole: BlackHole 가상 장치 여부
        is_virtual: 다른 앱 전용 가상 장치 여부 (ZoomAudioDevice, SoundFlower 등)
        is_aggregate: macOS Aggregate Device 여부 (본인 마이크 + BlackHole 통합용)
    """

    index: int
    name: str
    is_blackhole: bool = False
    is_virtual: bool = False
    is_aggregate: bool = False   # ← 신규

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "is_blackhole": self.is_blackhole,
            "is_virtual": self.is_virtual,
            "is_aggregate": self.is_aggregate,   # ← 신규
        }
```

#### 3.2 `steps/recorder.py` — 감지 로직 수정

**위치**: 기존 353 행 부근의 `virtual_keywords` 블록

```python
# 변경 전
is_blackhole = "blackhole" in name.lower()
virtual_keywords = ["zoom", "virtual", "aggregate", "soundflower", "loopback"]
name_lower = name.lower()
is_virtual = any(kw in name_lower for kw in virtual_keywords)

# 변경 후
name_lower = name.lower()
is_blackhole = "blackhole" in name_lower
is_aggregate = "aggregate" in name_lower    # 먼저 Aggregate 체크
# Aggregate는 본인 마이크를 포함한 합성 장치이므로 virtual에서 제외
virtual_keywords = ["zoom", "virtual", "soundflower", "loopback"]
is_virtual = (
    not is_aggregate
    and any(kw in name_lower for kw in virtual_keywords)
)

devices.append(
    AudioDevice(
        index=idx,
        name=name,
        is_blackhole=is_blackhole,
        is_virtual=is_virtual,
        is_aggregate=is_aggregate,
    )
)
```

**로그 출력 보강** (383 행 부근):

```python
for dev in devices:
    if dev.is_aggregate:
        label = " (Aggregate)"
    elif dev.is_blackhole:
        label = " (BlackHole)"
    elif dev.is_virtual:
        label = " (가상 장치)"
    else:
        label = ""
    logger.info(f"  [{dev.index}] {dev.name}{label}")
```

#### 3.3 `steps/recorder.py` — 장치 선택 우선순위 수정

**위치**: `_select_audio_device()` 메서드, 387 행 부근

```python
async def _select_audio_device(self) -> AudioDevice:
    """녹음에 사용할 오디오 장치를 선택한다.

    선택 우선순위:
        0단계: config에서 preferred_device_name 으로 명시 지정 (정확/부분 매칭)
        1단계: Aggregate Device (본인 마이크 + BlackHole 통합, prefer_system_audio 시)
        2단계: BlackHole (Aggregate 없을 때, prefer_system_audio 시)
        3단계: 실제 물리 마이크 (위 2단계 모두 실패 시)
        ...
    """
    devices = await self.detect_audio_devices()
    if not devices:
        raise AudioDeviceError("사용 가능한 오디오 입력 장치가 없습니다.")

    # 0단계: 명시적 장치명 지정 (신규)
    preferred = getattr(
        self._recording_config, "preferred_device_name", ""
    ) or ""
    if preferred:
        pref_lower = preferred.lower()
        # 정확 매칭 우선, 없으면 부분 매칭
        for dev in devices:
            if dev.name.lower() == pref_lower:
                logger.info(f"명시 지정 장치 선택 (정확): [{dev.index}] {dev.name}")
                return dev
        for dev in devices:
            if pref_lower in dev.name.lower():
                logger.info(f"명시 지정 장치 선택 (부분): [{dev.index}] {dev.name}")
                return dev
        logger.warning(
            f"preferred_device_name='{preferred}' 장치 미발견 → 자동 선택으로 폴백"
        )

    if self._recording_config.prefer_system_audio:
        # 1단계: Aggregate Device (신규)
        for dev in devices:
            if dev.is_aggregate:
                logger.info(
                    f"Aggregate 장치 선택 (본인 + 시스템 오디오 통합): "
                    f"[{dev.index}] {dev.name}"
                )
                return dev

        # 2단계: BlackHole (기존 1단계)
        for dev in devices:
            if dev.is_blackhole:
                logger.info(f"시스템 오디오 장치 선택: [{dev.index}] {dev.name}")
                return dev

    # 3단계 이하: 기존 로직 유지
    real_devices = [
        d for d in devices
        if not d.is_virtual and not d.is_blackhole and not d.is_aggregate
    ]
    mic_keywords = ["microphone", "마이크", "built-in", "internal", "macbook"]
    for dev in real_devices:
        if any(kw in dev.name.lower() for kw in mic_keywords):
            logger.info(f"마이크 장치 선택: [{dev.index}] {dev.name}")
            return dev

    if real_devices:
        selected = real_devices[0]
        logger.info(f"기본 오디오 장치 선택: [{selected.index}] {selected.name}")
        return selected

    selected = devices[0]
    logger.warning(
        f"실제 마이크를 찾을 수 없어 가상 장치를 사용합니다: "
        f"[{selected.index}] {selected.name}"
    )
    return selected
```

#### 3.4 `config.py` — `preferred_device_name` 옵션 추가

**파일**: `config.py:483` (`RecordingConfig`)

```python
class RecordingConfig(BaseModel):
    """오디오 녹음 설정 (Zoom 자동 녹음 포함)"""

    enabled: bool = True
    auto_record_on_zoom: bool = True
    prefer_system_audio: bool = True
    preferred_device_name: str = ""   # ← 신규. 빈 문자열이면 자동 선택
    sample_rate: int = Field(default=16000, ge=8000, le=48000)
    channels: int = Field(default=1, ge=1, le=2)
    max_duration_seconds: int = Field(default=14400, ge=60)
    min_duration_seconds: int = Field(default=5, ge=1)
    ffmpeg_graceful_timeout_seconds: int = Field(default=10, ge=1, le=60)
    multi_track: bool = False
    silence_threshold_rms: float = Field(default=0.001, ge=0.0, le=1.0)
```

#### 3.5 `config.yaml` — 신규 옵션 주석

```yaml
recording:
  enabled: true
  auto_record_on_zoom: true
  prefer_system_audio: true
  # (옵션) 녹음에 쓸 정확한 장치명. 비워두면 자동 선택(1순위 Aggregate, 2순위 BlackHole).
  # 예: "Meeting Transcriber Aggregate"
  preferred_device_name: ""
  sample_rate: 16000
  channels: 1
  # ... 기존 항목 그대로
```

> ⚠️ `channels` 는 현재 `1`(모노) 로 고정되어 있다. Aggregate Device 는 3채널 장치이므로 `-ac 1` 다운믹스 시 모든 채널이 1/3 가중치로 평균화되어 **전체 볼륨이 10 dB 이상 낮아지는 부작용**이 있다. 별도 이슈로 추적 권장 (아래 "후속 이슈" 참조).

---

## 4. 수동 검증 방법

```bash
# 1. Aggregate Device 생성 (최초 1회)
swiftc scripts/create_aggregate_device.swift -o /tmp/create_aggregate
/tmp/create_aggregate
# 기대: SUCCESS:<디바이스ID>

# 2. 앱이 인식하는지 확인 (수정 후)
ffmpeg -f avfoundation -list_devices true -i "" 2>&1 | grep Aggregate
# 기대: [N] Meeting Transcriber Aggregate

# 3. 서버 재기동 후 녹음 테스트
# 로그에서 다음 메시지 확인:
#   "Aggregate 장치 선택 (본인 + 시스템 오디오 통합): [N] Meeting Transcriber Aggregate"

# 4. 실제 녹음 파일 채널별 검증
LATEST=$(ls -t ~/.meeting-transcriber/audio_input/*.wav | head -1)
ffmpeg -i "$LATEST" -filter_complex \
  "[0:a]channelsplit=channel_layout=2.1[mic][bh_l][bh_r]; \
   [mic]volumedetect[m]; [bh_l]volumedetect[b]" \
  -map "[m]" -f null - -map "[b]" -f null - 2>&1 | grep mean_volume
# 기대:
#   Channel 0 (mic):       mean ≈ -20 ~ -30 dB (본인 발화가 있었다면)
#   Channel 1 (BlackHole): mean ≈ -20 ~ -30 dB (상대방 발화가 있었다면)
# 양쪽 모두 -91 dB(완전 무음)가 아니어야 함
```

---

## 5. 회귀 위험 (Regression Risk)

| 시나리오 | 영향 | 회피 방법 |
|----------|------|-----------|
| 사용자가 Aggregate Device 를 안 만든 환경 | 없음 — 2단계(BlackHole) 로 자동 폴백 | 기존 동작 유지 |
| BlackHole 이 없는 환경 (순수 마이크만) | 없음 — 3단계 이하에서 물리 마이크 선택 | 기존 동작 유지 |
| `prefer_system_audio: false` | 없음 — Aggregate/BlackHole 모두 skip | 기존 동작 유지 |
| 이름에 "aggregate" 가 들어간 **다른 종류의 가상 장치** | 잘못된 선택 가능 (매우 드문 엣지 케이스) | 향후 CoreAudio API 로 실제 Aggregate 속성 확인 (별도 이슈) |
| 사용자가 여러 Aggregate Device 를 만든 경우 | 첫 번째로 감지된 것 선택 (비결정적) | `preferred_device_name` 지정하면 해결 |

### 테스트 권고 케이스

```python
# tests/test_recorder.py 에 추가할 만한 케이스

def test_aggregate_device_preferred_over_blackhole():
    """Aggregate와 BlackHole이 모두 있을 때 Aggregate가 우선 선택되어야 한다."""
    devices = [
        AudioDevice(0, "BlackHole 2ch", is_blackhole=True),
        AudioDevice(1, "MacBook Air 마이크"),
        AudioDevice(2, "Meeting Transcriber Aggregate", is_aggregate=True),
    ]
    selected = _select_from(devices, prefer_system_audio=True)
    assert selected.is_aggregate
    assert selected.name == "Meeting Transcriber Aggregate"

def test_aggregate_detection_excludes_from_virtual():
    """'aggregate' 이름이 is_aggregate=True로 태깅되고 is_virtual=False여야 한다."""
    dev = _parse_device_line("[4] Meeting Transcriber Aggregate")
    assert dev.is_aggregate is True
    assert dev.is_virtual is False

def test_preferred_device_name_exact_match():
    """config.preferred_device_name 이 지정되면 다른 우선순위를 무시한다."""
    devices = [
        AudioDevice(0, "BlackHole 2ch", is_blackhole=True),
        AudioDevice(1, "My Custom Device"),
    ]
    selected = _select_from(
        devices, prefer_system_audio=True,
        preferred_device_name="My Custom Device"
    )
    assert selected.name == "My Custom Device"
```

---

## 6. 후속 이슈 (별도 티켓 권장)

### 이슈 A — 3채널 다운믹스 볼륨 저하
Aggregate Device 가 3채널이면 현재 `-ac 1` 다운믹스로 전체 평균이 되어 **본인 발화가 있을 때 마이크 채널의 에너지가 1/3 로 희석**된다. 해결책은 `-filter_complex` 로 채널별 가중치 지정:

```
[0:a]channelsplit=channel_layout=2.1[mic][bh_l][bh_r];
[bh_l][bh_r]amerge=inputs=2,pan=mono|c0=0.5*c0+0.5*c1[bh_mono];
[mic][bh_mono]amix=inputs=2:weights=2 1,pan=mono|c0=0.66*c0+0.34*c1[out]
```

- 마이크 2배 가중치 + BlackHole 좌우 평균 후 합성
- 비율은 실측 후 조정 필요

### 이슈 B — Aggregate Device 자동 생성 통합
현재 `scripts/create_aggregate_device.swift` 는 사용자가 수동으로 빌드·실행해야 함. `scripts/install.sh` 또는 앱 최초 기동 시 자동 생성하면 UX 개선.

### 이슈 C — Aggregate 실제 검증 (키워드 매칭 대체)
이름 키워드 대신 CoreAudio 의 `kAudioDevicePropertyTransportType == kAudioDeviceTransportTypeAggregate` 로 확실하게 판정하는 것이 안전함. pyobjc 또는 Swift 헬퍼로 구현 가능.

---

## 7. 실행 체크리스트 (Main PR)

단일 PR 로 묶되, 커밋은 논리적 단위로 분리한다.

### 7.1 코어 구현
- [ ] **[T1]** `steps/recorder.py`: `AudioDevice` 에 `is_aggregate` 필드 추가 + `to_dict()` 확장
- [ ] **[T2]** `steps/recorder.py`: `virtual_keywords` 에서 `"aggregate"` 제거, 별도 감지 로직 추가
- [ ] **[T3]** `steps/recorder.py`: 장치 목록 로그에 `(Aggregate)` 라벨 추가
- [ ] **[T4]** `steps/recorder.py`: `_select_audio_device()` 우선순위 수정 (0~2단계)
- [ ] **[T5]** `steps/recorder.py`: `real_devices` 필터에 `not d.is_aggregate` 추가 (중복 선택 방지)
- [ ] **[T6]** `config.py`: `RecordingConfig.preferred_device_name` 필드 추가
- [ ] **[T7]** `config.yaml`: 신규 옵션 주석 문서화

### 7.2 테스트
- [ ] **[T8]** `tests/test_recorder.py`: Aggregate 가 BlackHole 보다 우선 선택되는지
- [ ] **[T9]** `tests/test_recorder.py`: Aggregate 감지 시 `is_virtual=False` 유지되는지
- [ ] **[T10]** `tests/test_recorder.py`: `preferred_device_name` 정확/부분 매칭
- [ ] **[T11]** `tests/test_recorder.py`: `preferred_device_name` 미발견 시 자동 선택 폴백
- [ ] **[T12]** `tests/test_config.py`: `preferred_device_name` 기본값("")/오버라이드

### 7.3 실환경 검증 (수동, 머지 전에 1회)
- [ ] **[V1]** 로컬에서 Aggregate Device 생성 (수동 또는 임시 Swift 스크립트)
- [ ] **[V2]** `ffmpeg -f avfoundation -list_devices true -i ""` 로 장치 등록 확인
- [ ] **[V3]** 서버 재기동 후 녹음 로그에 `Aggregate 장치 선택` 메시지 확인
- [ ] **[V4]** 실제 Zoom 회의 1 건 녹음 후 `volumedetect` 로 양쪽 채널 mean_volume 확인

### 7.4 문서
- [ ] **[D1]** `docs/AGGREGATE_DEVICE_SETUP.md` 신규 작성 (사용자 가이드 — 이 이슈의 원래 참조 문서)
- [ ] **[D2]** 해당 문서에서 "4단계(앱 설정)" 섹션을 "자동 인식됩니다" 로 단순화 (이 PR 로 자동화되므로)

---

## 8. 순서 (진행 계획)

본 이슈는 단일 PR 로 묶되, 아래 순서로 구현한다.

### Phase 1: 코어 구현 + 단위 테스트 (코드 1회 리뷰 후 머지)
1. T1 ~ T7 구현 (논리적 커밋 단위로 3~4개 분리 권장)
   - 커밋 A: `AudioDevice.is_aggregate` 필드 + 감지 로직 (T1, T2, T3)
   - 커밋 B: `_select_audio_device()` 우선순위 (T4, T5)
   - 커밋 C: `preferred_device_name` config 추가 (T6, T7)
2. T8 ~ T12 테스트 추가 (커밋 D)
3. `ruff check`/`ruff format`/`pytest` 통과 확인
4. PR 생성 → 본 계획 링크 + 체크리스트 상태

### Phase 2: 실환경 검증 (PR 리뷰 중 병행)
- V1 ~ V4 수동 실행, 결과를 PR 본문에 추가
- 실패 시 필요한 보정 커밋만 추가

### Phase 3: 사용자 문서 (별도 PR 권장)
- D1, D2 수행. 별도 PR 로 내는 이유: 코드 변경이 끝난 뒤 실제 동작 기준으로 문서를 쓰는 게 정확성 높음.

### 후속 (별도 이슈 티켓)
- 이슈 A: 3채널 다운믹스 볼륨 보정
- 이슈 B: Aggregate 자동 생성 통합
- 이슈 C: CoreAudio API 기반 판정

---

## 9. 제보자 노트

- 발견 일자: 2026-04-22
- 발견 경위: 녹음된 파일을 실제로 들어봤을 때 본인 목소리 누락 확인
- 임시 회피 방법: **없음** — 코드 수정 없이는 사용자 설정만으로 해결 불가
- 영향 범위: macOS 사용자 중 Aggregate Device 기반 양방향 녹음을 원하는 모든 사용자
- 재현 100%: BlackHole 사용자는 모두 본인 목소리 미녹음 상태에서 전사 진행 중
