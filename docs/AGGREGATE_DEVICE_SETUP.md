# Aggregate Device 설정 가이드 (본인 + 상대방 양방향 녹음)

> macOS 에서 회의 녹음 시 **본인 마이크 + 시스템 오디오(상대방 목소리)** 를 하나의 WAV 로 함께 녹음하기 위한 설정 안내입니다.
> 이 문서를 완료하면 Meeting Transcriber 앱이 자동으로 Aggregate Device 를 감지·선택합니다.

## 배경 — 왜 이 설정이 필요한가

- **BlackHole 만 쓰면**: Zoom 등 앱의 출력(=상대방 목소리) 만 캡처되고 **본인 마이크 입력은 녹음 파일에 들어오지 않는다**.
- **본인 마이크만 쓰면**: 상대방 목소리가 스피커를 통해 공기 중으로 새면서 에코/울림이 섞이고, 이어폰을 쓰면 아예 녹음 안 됨.
- **해결**: macOS 의 **Aggregate Device** 기능으로 `MacBook 내장 마이크 + BlackHole 2ch` 를 하나의 합성 입력 장치로 묶는다. 한 번의 녹음에 두 소스가 모두 들어가 전사·화자분리 정확도가 올라간다.

---

## 사전 준비

1. **BlackHole 2ch 설치** (무료, 오픈소스)

   ```bash
   brew install blackhole-2ch
   ```

   설치 후 `시스템 설정 → 사운드 → 입력/출력` 에 `BlackHole 2ch` 가 보이는지 확인.

2. **Homebrew 또는 Audio MIDI 설정 접근 권한** — Aggregate Device 는 GUI 로 만듭니다.

---

## 1. Aggregate Device 생성 (최초 1회)

### GUI 방법 (권장)

1. Spotlight 에서 **Audio MIDI 설정** (Audio MIDI Setup) 실행.
2. 왼쪽 하단 `+` 버튼 → **Create Aggregate Device** 선택.
3. 오른쪽 체크박스 목록에서 다음 두 장치를 **모두** 체크:
   - `MacBook Air Microphone` (또는 사용 중인 내장·외장 마이크)
   - `BlackHole 2ch`
4. 장치 이름을 `Meeting Transcriber Aggregate` 로 변경 (권장 — 이름에 `aggregate` 가 포함되어야 앱이 자동 인식합니다).
5. `Drift Correction` 은 **BlackHole 2ch 만** 체크. 하드웨어 마이크는 끕니다.
6. `Clock Source` 는 내장 마이크(MacBook Air Microphone) 로 지정.
7. 창을 닫으면 자동 저장됩니다.

### 확인

```bash
ffmpeg -f avfoundation -list_devices true -i "" 2>&1 | grep -i aggregate
# 기대 출력: [AVFoundation indev @ ...] [N] Meeting Transcriber Aggregate
```

---

## 2. Zoom (및 다른 화상 앱) 설정

Zoom 이 **자기 마이크 입력** 과 **자기 출력(상대방 목소리)** 을 모두 Aggregate 에 흘려보내도록 설정합니다.

1. Zoom → **설정 → 오디오**
2. **스피커**: `BlackHole 2ch` 선택 → 상대방 목소리가 BlackHole 로 흐릅니다.
3. **마이크**: 평소 쓰던 마이크(예: `MacBook Air Microphone`) 그대로.
   - ⚠️ `Meeting Transcriber Aggregate` 를 Zoom 마이크로 잡으면 **하울링** 이 발생합니다. Zoom 의 마이크는 원래 마이크로 유지하세요.
4. `자동으로 마이크 볼륨 조절` 은 **비활성화** 권장 (녹음 볼륨 변동 방지).

### 스피커 설정 후 본인이 소리를 못 듣는 문제 해결

BlackHole 2ch 만 스피커로 잡으면 본인은 상대방 목소리를 들을 수 없습니다. 모니터링이 필요하면:

- **Multi-Output Device** 를 별도로 만들어 `BlackHole 2ch + 실제 스피커/이어폰` 을 동시 출력으로 구성한 뒤, macOS 시스템 출력 또는 Zoom 스피커를 이 Multi-Output 으로 지정.
- 간단한 대안으로, Zoom 의 `설정 → 오디오 → 고급` 에서 스피커를 BlackHole 로 유지하되 macOS 시스템 출력은 이어폰으로 따로 잡는 조합도 가능.

---

## 3. Meeting Transcriber 앱 설정

이 문서의 1, 2 단계를 마쳤다면 **앱 쪽에서는 아무 설정도 필요 없습니다**.

앱이 기동될 때 자동으로 장치 목록을 감지하고 다음 우선순위로 선택합니다:

1. `config.yaml` 의 `recording.preferred_device_name` 이 지정되어 있으면 그 이름 장치 (정확 매칭 → 부분 매칭)
2. **Aggregate Device** (이름에 `aggregate` 포함)
3. BlackHole (Aggregate 없을 때 폴백)
4. 물리 마이크

### (선택) 특정 장치를 명시 지정

여러 Aggregate Device 가 있거나 이름 매칭이 모호할 때:

```yaml
# config.yaml
recording:
  preferred_device_name: "Meeting Transcriber Aggregate"
```

정확 이름으로 찾고, 없으면 부분 매칭으로 찾습니다. 둘 다 실패하면 자동 선택으로 폴백하며 경고 로그를 남깁니다.

### 서버 로그로 확인

앱을 재기동한 뒤 로그에서 다음 메시지가 나와야 합니다:

```
Aggregate 장치 선택 (본인 + 시스템 오디오 통합): [N] Meeting Transcriber Aggregate
```

---

## 4. 녹음 결과 검증 (권장)

실제 Zoom 회의 1 건을 녹음한 뒤, 두 채널 모두 소리가 들어갔는지 확인합니다.

```bash
LATEST=$(ls -t ~/.meeting-transcriber/audio_input/*.wav | head -1)

# 전체 파일 볼륨
ffmpeg -i "$LATEST" -af volumedetect -f null - 2>&1 | grep -E "mean_volume|max_volume"

# 채널별 볼륨 (Aggregate 가 3채널일 때)
ffmpeg -i "$LATEST" -filter_complex \
  "channelsplit=channel_layout=2.1[c0][c1][c2]; \
   [c0]volumedetect[v0]; [c1]volumedetect[v1]; [c2]volumedetect[v2]" \
  -map '[v0]' -f null - -map '[v1]' -f null - -map '[v2]' -f null - 2>&1 \
  | grep mean_volume
```

**정상 범위**
- 본인 마이크 채널: `mean ≈ -20 ~ -30 dB` (본인이 말했다면)
- BlackHole 채널(좌/우): `mean ≈ -20 ~ -30 dB` (상대방이 말했다면)
- 한쪽이 **`-91 dB`(완전 무음)** 이면 그 경로가 끊긴 것 — 1, 2 단계 설정 재확인.

---

## 5. 알려진 제약·후속 이슈

| 항목 | 상태 | 해결 |
|------|------|------|
| 3채널 → 1채널 다운믹스 시 **본인 목소리 볼륨 약 10 dB 저하** | 추적 중 | 별도 이슈 (filter_complex 로 채널별 가중치 지정) |
| Aggregate Device 자동 생성 스크립트 | 추적 중 | 별도 이슈 (Swift/pyobjc 헬퍼) |
| 이름 키워드 대신 CoreAudio transportType 로 정확 판정 | 추적 중 | 별도 이슈 |

---

## 6. 트러블슈팅

### 로그에 `Aggregate 장치 선택` 이 안 나옴
- `ffmpeg -f avfoundation -list_devices true -i ""` 에 Aggregate 가 나오는가?
  - 안 나오면 1 단계 Audio MIDI 설정 재확인.
- 장치 이름에 `aggregate` 문자열이 포함되어 있는가?
  - 포함되어 있지 않으면 `config.yaml` 의 `preferred_device_name` 에 정확한 이름을 넣거나, Audio MIDI 설정에서 이름을 바꿔주세요.

### 녹음은 되지만 본인 목소리가 여전히 안 나옴
- 앱이 정말 Aggregate 를 선택했는지 로그 재확인 (`Aggregate 장치 선택` 메시지).
- Audio MIDI 설정에서 Aggregate 구성에 내장 마이크 체크가 풀리지는 않았는지 확인 (재부팅 후 체크가 풀리는 경우가 있음).
- `ffmpeg -i <녹음파일> -af volumedetect -f null -` 결과가 `mean_volume: -91 dB` 에 가까우면 OS 권한 문제 — `시스템 설정 → 개인정보 보호 → 마이크` 에서 터미널/앱 권한 확인.

### 모든 설정이 맞는데도 안 되는 경우
로그 파일(`~/.meeting-transcriber/logs/app.log` 또는 stdout) 과 함께 이슈 등록 바랍니다.
