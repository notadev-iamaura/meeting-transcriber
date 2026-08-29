# 마이그레이션 가이드

버전/브랜치 간 `config.yaml` 변경 사항 및 주의점을 정리한다.

---

## 새벽 자동 처리 backlog 누락 수정 (2026-08-30)

자동 전사가 파이프라인 직접 실행에서 JobProcessor 큐 등록으로 바뀐 뒤에도
기본 1건 상한이 남아 있었습니다. 최신 순 선택과 48시간 대상 window 때문에
오래된 `recorded` 회의가 영구히 자동 전사되지 않을 수 있어 기본을 다음처럼
변경했습니다.

```yaml
auto_processing:
  max_items_per_run: 0                # 누락분 전체를 순차 큐에 등록
  run_on_startup_if_missed: true       # 당일 예약 시각을 놓친 경우 1회 catch-up
```

- `auto_processing.enabled` 자체는 계속 명시적 opt-in입니다.
- 기존 별도 설정 파일에 `max_items_per_run: 1` 또는
  `run_on_startup_if_missed: false`가 명시되어 있으면 자동으로 덮어쓰지 않습니다.
  누락분 전체 처리가 필요하면 설정 화면에서 **1회 처리 상한=0**,
  **놓친 실행 따라잡기=켬**을 확인하세요.
- 여러 건을 한꺼번에 병렬 전사하는 변경은 아닙니다. JobProcessor가 기존
  서멀 배치/쿨다운 규칙으로 순차 처리합니다.
- 같은 회의의 전사·지연 요약·검색 재색인·재전사·삭제·수동 편집은 회의별로 직렬화됩니다.
  `POST /api/meetings/{id}/summarize`, `POST /api/meetings/{id}/reindex`와
  회의록·전사문 편집 API는 DB 작업이 `completed`가 아니면 `409`를 반환합니다.

---

## 짧은·손상 오디오 차단 강화 (2026-08-18)

전사할 가치가 낮은 짧은 파일과 길이·볼륨을 정상 측정할 수 없는 손상 파일이
STT 단계에서 `오류`를 만들지 않도록 오디오 품질 게이트를 fail-closed로 변경했다.

| 항목 | 기존 | 변경 후 |
|---|---|---|
| 전사 큐 최소 실제 재생 시간 | 5초 | 30초 |
| 앱 녹음 최소 경과 시간 | 5초 | 30초 |
| 품질 측정 `ERROR` | 큐 등록 허용 | 큐/STT 차단; 파일 결함이 확정된 경우만 격리 |

```yaml
audio_quality:
  enabled: true
  min_duration_seconds: 30.0
  decode_timeout_base_seconds: 60.0
  decode_timeout_factor: 0.25
  decode_timeout_cap_seconds: 900.0

recording:
  min_duration_seconds: 30    # 앱 자체 녹음 경과시간, 미달 시 임시 파일 파기

watcher:
  file_ready_timeout_seconds: 30  # growing/open-writer readiness 최대 대기
```

- `audio_quality.min_duration_seconds`는 16 kHz mono full-decode sample count와 성공한
  ffprobe duration 중 더 짧은 값을 기준으로 쓴다. 잘린 파일은 decode가,
  AAC 등의 encoder padding은 probe가 보수적으로 막는다. ffprobe 길이를 확정할 수
  없으면 decoded-short/저볼륨처럼 파일 결함을 증명할 수 있는 경우만 거부하고,
  정상으로 추측해 ACCEPT하지 않는다. ffmpeg progress time은 판정에 쓰지 않는다.
- OGG/Vorbis처럼 컨테이너가 입력 경계를 granule 단위로 양자화하는 형식은 원래 생성
  명령의 소수점이 아니라 저장된 미디어에 표현된 effective duration을 기준으로 한다.
- full-decode timeout은 duration hint가 있으면
  `min(cap, max(base, base + duration × factor))`, hint가 없으면 `cap`을 쓴다.
- 동일 프로세스에서 identity와 gate 설정이 모두 같은 파일의 ACCEPT 결과만 bounded LRU로
  재사용한다. 비수락·예외·변경된 파일은 cache하지 않는다.
- `recording.min_duration_seconds`는 앱 자체 녹음을 더 일찍 정리하는 경과시간 기준이다.
- `REJECT`와 `MEDIA_INVALID`로 확정된 파일만
  `~/.meeting-transcriber/audio_quarantine/`으로 이동한다. `SOURCE_BUSY`,
  `INFRA_UNAVAILABLE`, `SECURITY_BLOCKED`는 원본을 보존하고 재시도/운영자 확인
  대상으로 남긴다.
- 입력·격리·체크포인트·출력 경로의 symlink는 지원하지 않으며 target을 읽거나 이동하지
  않는다. writable 파일은 `watcher.file_ready_timeout_seconds` 안에서 readiness를
  확인하고, 계속 열려 있으면 원본을 보존한 채 한 번 지연 재검사한다.
- `/api/uploads`는 입력 디렉터리를 no-follow로 열고 임의 이름의 0600 temp inode를
  완전히 기록·fsync한 뒤 same-directory hardlink로 무덮어쓰기 publish한다. 기존 파일이나
  symlink를 덮어쓰지 않으며, API가 직접 queue에 넣지 않고 watcher가 최종 admission을 맡는다.
- 이미 큐에 있거나 재시도되는 파일도 `PipelineManager`와 `Transcriber`의 최종 gate에서
  다시 검사하여 STT 모델 실행 전에 차단한다. 레거시 queued/failed row는 원래 실행 의도를
  DB hold payload에 보존한 뒤 재감사한다.
- 레거시 row의 확정된 미디어 거부는 DB에 source identity와 예약 quarantine 경로를 먼저
  journal로 남긴 뒤 exact move와 CAS delete를 수행한다. 중간에 앱이 종료되면 startup이
  source-only/quarantine-only 상태를 멱등 복구하며, 양쪽 존재·양쪽 없음·identity 불일치는
  추측하지 않고 파일과 row를 보존한다.
- retry/force/re-transcribe/STT A/B/batch API는 상태나 산출물을 바꾸기 전에 같은 gate를
  실행한다. 응답은 `MEDIA_INVALID=422`, `SOURCE_BUSY=409`,
  `INFRA_UNAVAILABLE=503`, `SECURITY_BLOCKED=400`으로 구분한다.
- 재전사는 `claimed → staging → purging → committing` 상태를 DB에 남기며, 실패·앱 종료 시
  기존 산출물 rollback 또는 commit 재개를 수행한다. admission 실패 시 기존 산출물과
  검색 인덱스는 변경하지 않는다. 산출물 stage/rollback/cleanup과 checkpoint state 읽기,
  reindex recovery marker 쓰기는 pinned directory/file descriptor 안에서 수행한다.
- `audio_quality.enabled: false`는 duration/volume full-decode 정책만 우회한다. no-follow
  경로, 일반 파일, source identity, writer readiness 검사는 계속 적용된다.
- 설정 변경은 앱 재시작 후 반영된다.

---

## Phase 1 크래시 방지 (2026-04-21 병합, PR #5)

> 2026-04-21 MLX Metal SIGSEGV 크래시 방지를 위한 Defense-in-Depth 적용.
> 자세한 배경: `docs/superpowers/plans/2026-04-21-phase1-crash-prevention.md`

### 🚨 파괴 변경 (Breaking Changes)

#### 1. `pipeline.retry_max_count` 제약 축소

| 항목 | 기존 | 변경 후 |
|---|---|---|
| 기본값 | `3` | `1` |
| 최소값 (`ge`) | `0` | `1` |
| 최대값 (`le`) | `10` | `5` |

**영향:**
- `config.yaml` 에 `retry_max_count: 0` 또는 `retry_max_count: 6` 이상으로 수동 설정한 사용자는 앱 기동 시 Pydantic `ValidationError` 발생.
- 기본값 변경으로 타임아웃 발생 시 재시도 없이 즉시 실패 처리.

**근거:**
- 타임아웃 후 재시도가 MLX Metal 상태 오염된 채 모델을 재로드하여
  SIGSEGV 크래시의 즉발 트리거였음 (2026-04-21 인시던트).
- `NonRetryableError` 계열은 어차피 재시도하지 않으므로 기본값 1 은
  "Retryable 오류에 한해 최대 1회 시도" 의미.

**마이그레이션:**

```yaml
# 기존 (Phase 1 이전)
pipeline:
  retry_max_count: 3

# 변경 후 권장 (Phase 1 병합 버전)
pipeline:
  retry_max_count: 1    # 또는 생략하여 기본값 사용
```

`retry_max_count: 0` 으로 재시도를 완전히 비활성화하고 싶었던 경우,
Phase 1 기본값 `1` 이 이미 거의 동일한 효과 (Retryable 오류 1회 시도 후
실패 확정). 더 많은 재시도가 필요하면 `2~5` 범위 내에서 조정.

#### 2. `DELETE /api/meetings/{id}` 동작 변경

**기존:** DB 레코드만 삭제, 오디오 파일은 `audio_input/` 에 잔존.

**변경 후:** DB 레코드 삭제 + 오디오 파일을 `audio_quarantine/` 으로 이동.

**영향:**
- 삭제한 회의의 오디오 파일이 입력 폴더에서 사라짐 (UI 상 동일).
- `audio_quarantine/` 에 누적되므로 주기적 정리가 필요할 수 있음.
- 파일 시스템 레벨 복구는 `audio_quarantine/{파일명}` 에서 가능.

**마이그레이션:**
- 별도 조치 불필요. 기존 기능에 비해 더 안전한 동작 (watcher 재감지 루프 차단).
- 격리 폴더를 백업에 포함하고 싶으면 `.time-machine` / `.spotlight`
  제외 설정 확인 (기본적으로 제외되어 있음).

#### 3. `Transcriber.transcribe()` 시그니처

`timeout_override: int | None = None` keyword-only 파라미터 추가.

**영향:** 기존 호출처는 keyword 미지정 → 하위 호환. 시그니처 변경 자체는 파괴적이지 않음.

### ✨ 신규 설정 (기본값으로 자동 활성화)

```yaml
audio_quality:
  enabled: true               # 신규: 큐잉 전 품질 검증
  min_mean_volume_db: -40.0   # 저볼륨 차단 임계
  min_duration_seconds: 5.0

paths:
  audio_quarantine_subdir: "audio_quarantine"  # 신규 서브디렉토리

watcher:
  excluded_subdirs:           # 신규: 감시 제외 경로
    - "audio_quarantine"

pipeline:
  dynamic_timeout_enabled: true      # 신규: 길이 비례 타임아웃
  dynamic_timeout_multiplier: 3.0
  dynamic_timeout_min_seconds: 600   # 10 분 하한
  dynamic_timeout_max_seconds: 10800 # 3 시간 상한
```

**영향:** 전부 자동 활성화. 기존 설정에 덮어써지는 항목은 없으므로 기존
`config.yaml` 을 그대로 사용해도 Phase 1 방어막이 작동한다.

### 🛠 launchd 재등록 권장

`scripts/setup_launchagent.sh` 의 `KeepAlive` 가 `false` → `dict(Crashed=true)` 로 변경.
기존에 LaunchAgent 를 등록해두었다면 재등록해야 새 설정이 반영된다:

```bash
launchctl unload ~/Library/LaunchAgents/com.meeting-transcriber.plist
bash scripts/setup_launchagent.sh
```

### 📊 관찰성 (P2)

Phase 1 Follow-up 에서 `core/audio_quality.py` 에 카운터가 추가되었다.
외부 스크립트/엔드포인트에서 `get_validation_stats()` 로 조회 가능:

```python
from core.audio_quality import get_validation_stats, reset_validation_stats

stats = get_validation_stats()
# {"accept": 120, "reject": 3, "error": 1}
```

ERROR 가 지속적으로 증가하면 ffmpeg 미설치/환경 문제를 의심해야 한다.
