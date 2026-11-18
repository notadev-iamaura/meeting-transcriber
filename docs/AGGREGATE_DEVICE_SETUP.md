# Aggregate Device 설정 가이드 (본인 + 상대방 양방향 녹음)

> macOS에서 **본인 마이크와 시스템 오디오(상대방 목소리)** 를 한 WAV의 별도 채널에 녹음하기 위한 안내입니다.
> 이 문서를 완료하면 Meeting Transcriber 앱이 자동으로 Aggregate Device 를 감지·선택합니다.

## 배경 — 왜 이 설정이 필요한가

- **BlackHole 만 쓰면**: Zoom 등 앱의 출력(=상대방 목소리) 만 캡처되고 **본인 마이크 입력은 녹음 파일에 들어오지 않는다**.
- **본인 마이크만 쓰면**: 상대방 목소리가 스피커를 통해 공기 중으로 새면서 에코/울림이 섞이고, 이어폰을 쓰면 아예 녹음 안 됨.
- **Aggregate Device**: `MacBook 내장 마이크 + BlackHole 2ch` 를 묶으면 두 입력을 같은 시간축으로 녹음할 수 있다.

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

Zoom에서는 마이크와 스피커를 각각 지정합니다. 앱은 Aggregate에서 두 입력을 함께 받습니다.

1. Zoom → **설정 → 오디오**
2. **스피커**: `BlackHole 2ch` 선택 → 상대방 목소리가 BlackHole 로 흐릅니다.
3. **마이크**: 평소 쓰던 마이크(예: `MacBook Air Microphone`) 그대로.
   - ⚠️ `Meeting Transcriber Aggregate` 를 Zoom 마이크로 잡으면 **하울링** 이 발생합니다. Zoom 의 마이크는 원래 마이크로 유지하세요.
4. `자동으로 마이크 볼륨 조절` 은 **비활성화** 권장 (녹음 볼륨 변동 방지).

### 스피커 설정 후 본인이 소리를 못 듣는 문제 해결

BlackHole 2ch 만 스피커로 잡으면 본인은 상대방 목소리를 들을 수 없습니다. 모니터링이 필요하면:

- **Multi-Output Device** 를 별도로 만들어 `BlackHole 2ch + 실제 스피커/이어폰` 을 동시 출력으로 구성한 뒤, macOS 시스템 출력 또는 Zoom 스피커를 이 Multi-Output 으로 지정.
- Zoom 출력만 녹음하려면 Zoom 스피커에 Multi-Output을 지정하고 다른 앱은 이어폰 등 별도 장치로 재생합니다.

---

## 3. Meeting Transcriber 앱 설정

이 문서의 1, 2 단계를 마쳤다면 **앱 쪽에서는 아무 설정도 필요 없습니다**.

앱이 기동될 때 자동으로 장치 목록을 감지하고 다음 우선순위로 선택합니다:

1. `config.yaml` 의 `recording.preferred_device_name` 이 지정되어 있으면 그 이름 장치 (정확 매칭 → 부분 매칭)
2. **Aggregate Device** (CoreAudio 속성 또는 이름으로 감지)
3. BlackHole (Aggregate 없을 때 폴백)
4. 물리 마이크

`recording.separate_mic_track`의 기본값은 `true`입니다. CoreAudio가 물리 마이크 1채널과
BlackHole 2ch의 구성을 확인하면 앱이 마이크는 왼쪽, BlackHole은 오른쪽에 담아 WAV 하나로
저장합니다. 로컬 전사는 마이크 발화를 `나`로 표시하고, 오른쪽 채널에서만 화자분리를 실행합니다.
두 사람이 동시에 말해도 각 채널의 발화를 같은 시간축에 남깁니다.

녹음 표시줄을 펼치면 `내 마이크`와 `Zoom·시스템`의 실제 입력 음량을 따로 확인할 수
있습니다. 각자 차례로 말해 보면 어느 입력이 들어오는지 쉽게 확인할 수 있습니다.
확인된 Aggregate의 분리 녹음은 기본적으로 macOS의 CoreAudio 입력 큐를 사용합니다.
기존 FFmpeg 녹음 방식이 필요한 경우 `recording.split_track_processing.native_capture: false`로
전환할 수 있습니다. 일반 모노 녹음은 기존 FFmpeg 경로를 사용합니다.

로컬 전사에서는 Zoom·시스템 출력이 스피커를 통해 마이크로 되돌아온 것으로 확인된
성분만 전사용 사본에서 줄입니다. 지연과 파형이 여러 구간에서 일치하는지 확인하고,
동시에 말하는 시간대를 잘라내지 않습니다. 원본 WAV에는 두 채널을 그대로 보존합니다.
누설을 확실히 구별하기 어려운 환경에서는 일부 중복이 남을 수 있으므로 이어폰을 쓰면
예방에 도움이 됩니다.

마이크가 작게 들어오거나 잠깐만 말했을 때는 녹음 전체 평균 대신 신호가 있는 구간의
음량을 보고 전사용 사본을 보정합니다. 두 트랙은 같은 Whisper 모델을 한 번 로드해
차례로 전사합니다. 긴 무음을 건너뛰어 처리할 구간을 줄일 수 있을 때도 발화의 원래
재생 시각을 유지합니다. `recording.split_track_processing`에서 누설 억제와 음량·무음
검사 기준을 조정할 수 있으며 기본값은 `config.yaml`을 참고하세요.

순간적으로 큰 잡음이 들어와도 그 피크 하나가 다른 시각의 작은 발화 전체를
제외하지 않도록, 활성 구간의 대표 음량으로 보정하고 포화 제한은 해당 샘플에만
적용합니다. 보정한 사본은 이미 설치된 Silero CPU 모델로 말소리 여부를 확인합니다.
`speech_verification: true`, `speech_threshold: 0.20`이 기본이며 일반 전사의 VAD
설정과 별개입니다. 모델을 새로 다운로드하거나 음성을 외부로 보내지 않습니다.
검사한 구간 앞뒤 여유와 원래 재생 시각을 유지하고, 확인된 발화를 전체 평균 음량으로
다시 제외하지 않습니다. 이 검사는 잡음 환각을 줄이지만 모든 환경의 인식 정확도를
보장하지는 않습니다.

마이크에서 음성 구간을 감지했는데 로컬 전사 결과가 비거나 음량 오류가 발생하면 앱에서 해당
회의를 알려줍니다. 일부 문장이 전사됐더라도 확인된 음성 구간과 전사 시각을 비교해 빈 시간대를
안내합니다. 짧은 추임새나 문장 안의 단어 누락은 자동 감지가 어려울 수 있습니다.
직접 누락을 발견했다면 분리 전사가 완료된 회의 상세의 **더 보기**에서도 **내 마이크만 다시 전사**를
누를 수 있습니다. 로컬 모델이 원본 WAV의 마이크 채널을 다시 읽고, 새 시간대의 발화만 원래
시각에 **나**로 추가합니다. 기존 내 발화·상대방 발언·직접 수정한 문장을 보존합니다.
추가된 발화가 있으면 검색을 갱신하고 기존 요약을 검토 대상으로 표시합니다.

녹음 중 저장 오류가 발생하거나 캡처 프로세스가 예기치 않게 끝나면 앱은 타이머를 멈추고
중단 안내를 표시합니다. macOS 알림도 요청하며 실제 배너 표시는 시스템 알림 설정을 따릅니다.
분리 녹음에서 입력 프레임 자체가 끊긴 경우에는 `recording.capture_stall_timeout_seconds`
(기본 5초) 뒤에 감지합니다. 조용한 회의도 프레임이 계속 들어오면 정상입니다.
남은 부분 파일 이름은 앱에서 확인할 수 있으며 파일은 데이터 폴더의 `recordings_temp/`에 있습니다.
불완전할 수 있는 부분 파일은 자동 전사 큐로 옮기지 않습니다. 정상 종료한 분리 녹음도
유효 샘플이 있으면 작은 음량만을 이유로 원본을 삭제하지 않습니다.

채널 구성을 확인할 수 없거나 분리 설정을 끈 경우 일반 녹음으로 처리합니다. 예전에 모노로
녹음한 파일과 일반 스테레오 파일도 일반 전사 경로를 사용합니다. OpenAI 전사를 선택하면 기존
단일 오디오 전사 방식을 사용합니다. `recording.multi_track`은 별도 파일을 만드는 이전 옵션입니다.

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
Aggregate 분리 녹음 (CoreAudio): 마이크(L), 시스템 오디오(R)
```

---

## 4. 녹음 결과 검증 (권장)

실제 Zoom 회의 1 건을 녹음한 뒤, 두 채널 모두 소리가 들어갔는지 확인합니다.

```bash
# 방금 녹음한 WAV 경로를 넣습니다.
recording_wav="$HOME/.meeting-transcriber/audio_input/meeting_YYYYMMDD_HHMMSS.wav"
ffprobe -v error -select_streams a:0 -show_entries 'stream=channels:format_tags=comment' "$recording_wav"

# 왼쪽(마이크), 오른쪽(BlackHole) 순서로 평균 음량을 확인합니다.
for channel in 0 1; do
  ffmpeg -hide_banner -i "$recording_wav" -af "pan=mono|c0=c${channel},volumedetect" -f null - 2>&1 \
    | rg mean_volume
done
```

분리 녹음이면 `channels=2`, `TAG:comment=recap-microphone-system-v1`이 표시됩니다.
각자 말을 했는데 해당 채널이 무음이면 Zoom 출력 장치와 Aggregate 구성을 다시 확인합니다.

---

## 5. 알려진 제약·후속 이슈

| 항목 | 상태 | 해결 |
|------|------|------|
| 기존 모노 녹음에서 3채널을 합칠 때 마이크 음량 저하 | 보정 가능 | `recording.aggregate_mic_boost: true`로 마이크 가중치 적용 |
| Aggregate Device 자동 생성 스크립트 | ✅ 해결 | `scripts/create_aggregate_device.swift` + `bash scripts/setup_audio.sh` |
| 물리 마이크 1채널 + BlackHole 2ch 이외의 Aggregate | 일반 녹음 | 오디오 채널 구성이 확인된 경우에만 `나` 자동 표시 |

### 5.1 aggregate_mic_boost 튜닝

Aggregate 가 표준 3채널(내장 마이크 모노 + BlackHole 좌/우) 이 아닌 경우 `pan` 필터가 실패할 수 있다. 녹음 시작 실패 로그가 보이면:

```yaml
# config.yaml
recording:
  aggregate_mic_boost: false  # ← 기본 -ac 1 평균 다운믹스로 폴백
```

실측 후 가중치를 직접 튜닝하고 싶다면 `steps/recorder.py` 의 `pan=mono|c0=0.5*c0+0.25*c1+0.25*c2` 를 수정하면 된다. 합계 1.0 을 유지해야 클리핑이 발생하지 않는다.

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
