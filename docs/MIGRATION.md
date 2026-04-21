# 마이그레이션 가이드

버전/브랜치 간 `config.yaml` 변경 사항 및 주의점을 정리한다.

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
