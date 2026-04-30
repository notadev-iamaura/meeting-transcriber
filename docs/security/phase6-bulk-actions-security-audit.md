# Phase 6 보안 감사 보고서 — bulk-actions

- 감사 일자: 2026-04-29
- 감사 범위: bulk-actions Phase 6 신규 추가분 전용
- 위협 모델: 로컬 단독 실행 (127.0.0.1:8765), 동일 머신 다른 프로세스 포함

---

## 1. 감사 축별 결과 요약

| 축 | 상태 | 비고 |
|---|---|---|
| 1. 입력 검증 | WARN | meeting_ids 길이 무제한 — DoS 가능 |
| 2. Path 보안 | WARN | audio_path 베이스 디렉토리 경계 검증 없음 |
| 3. 출력 안전 | WARN | 500 detail 에 내부 예외 문자열 노출 |
| 4. DoS / 자원 사용 | WARN | meeting_ids 무제한 + scope=all 무제한 |
| 5. CSRF / Origin | PASS | CORS 화이트리스트 적용 확인 |
| 6. 프론트 XSS | PASS | safeText / escapeHtml 일관 적용 |
| 7. 로깅 안전 | PASS | 민감 정보 미포함, 경로만 기록 |

---

## 2. 발견된 취약점 상세

---

### [MEDIUM-01] meeting_ids 배열 길이 무제한 — 소프트 DoS

- **심각도**: Medium
- **분류**: 입력 검증 / 자원 소진
- **위치**: `/Users/youngouksong/projects/meeting-transcriber/api/routes.py` 라인 3195

**문제 설명**

`BatchActionRequest.meeting_ids` 필드에 `max_length` 제약이 없다.

```python
# routes.py:3195 — 현재 코드
meeting_ids: list[str] = Field(default_factory=list)
```

동일 머신의 악성 스크립트(또는 결함 있는 클라이언트)가 수백만 개의 ID를 담은 배열을 전송하면:

1. 각 ID마다 `_validate_meeting_id()` 정규식 매칭 실행 (CPU)
2. `dict.fromkeys()` 로 중복 제거 (메모리)
3. 각 ID마다 `_classify_meeting_for_batch()` 내에서 파일 시스템 접근 2회 (I/O)

scope="selected" + 100만 개 ID 전송 시 응답 지연 수십 초 이상 예상. 기존 전사 파이프라인과 공유하는 이벤트 루프이므로 실제 전사 작업이 지연될 수 있다.

**비교**: 같은 파일 라인 2955의 `SummarizeBatchRequest.meeting_ids` 역시 동일한 문제를 가지나 이번 티켓 범위 밖.

**수정 권고**

```python
# routes.py:3195 수정 제안
meeting_ids: list[str] = Field(default_factory=list, max_length=500)
```

500이 현실적인 상한이지만 운영 환경에 따라 100~1000 범위에서 조정 가능. 초과 시 Pydantic 이 자동으로 422를 반환하므로 엔드포인트 로직 변경 불필요.

---

### [MEDIUM-02] audio_path 베이스 디렉토리 경계 검증 없음

- **심각도**: Medium
- **분류**: Path 보안 / 정보 노출
- **위치**: `/Users/youngouksong/projects/meeting-transcriber/api/routes.py` 라인 3158~3179

**문제 설명**

`_resolve_audio_path()` 는 `JobQueue` 에서 조회한 `audio_path` 를 검증 없이 `Path` 로 변환한다.

```python
# routes.py:3178
path = Path(job.audio_path)
return path if path.exists() else None
```

`audio_path` 는 `JobQueue` 의 SQLite DB에서 조회되므로 사용자가 직접 위조할 수 없다. 그러나 두 가지 상황에서 경계 밖 경로가 참조될 수 있다:

1. **DB 손상 또는 수동 편집**: SQLite 파일을 직접 편집하면 임의 경로(예: `/etc/passwd`) 를 주입할 수 있다. `path.exists()` 가 `True` 를 반환하면 해당 파일이 `pipeline.run()` 에 오디오로 전달된다.
2. **심링크 공격**: `~/.meeting-transcriber/audio_input/` 아래에 시스템 파일을 가리키는 심링크를 생성한 뒤 해당 경로로 업로드하면 파이프라인이 임의 파일을 처리한다.

현재 영향은 제한적이다. `pipeline.run()` 이 WAV 파서를 통과하므로 `/etc/passwd` 를 처리해도 파싱 실패로 종료될 가능성이 높다. 그러나 이론적으로 정보가 파이프라인 로그에 기록되거나 STT 모델에 전달될 위험이 있다.

**수정 권고**

```python
# routes.py:_resolve_audio_path 내 추가 검증 제안
path = Path(job.audio_path).resolve()
# 허용 베이스 디렉토리 목록 (config 에서 가져옴 또는 하드코딩)
# 엔드포인트에서 config 참조가 가능하므로 queue 헬퍼에 config 를 전달하거나
# 상위 batch_action 에서 검증하는 것이 현실적
allowed_base = Path("~/.meeting-transcriber").expanduser().resolve()
if not str(path).startswith(str(allowed_base)):
    logger.warning(f"audio_path 가 허용 경계 밖: {path}")
    return None
return path if path.exists() else None
```

실용적 대안으로, `_resolve_audio_path` 에 `base_dir: Path` 매개변수를 추가하고 `batch_action` 에서 `config.paths.resolved_base_dir` 을 전달한다.

---

### [LOW-01] 500 오류 detail 에 내부 예외 문자열 노출

- **심각도**: Low
- **분류**: 정보 노출 / OWASP A05
- **위치**: `/Users/youngouksong/projects/meeting-transcriber/api/routes.py` 라인 3273, 3301

**문제 설명**

두 곳에서 내부 예외 `{e}` 를 HTTP 응답 `detail` 필드에 그대로 포함한다.

```python
# routes.py:3273
detail=f"회의 목록 조회 중 오류가 발생했습니다: {e}",

# routes.py:3301
detail=f"체크포인트 디렉토리 스캔 실패: {e}",
```

로컬 전용 앱이므로 외부 노출 위험은 낮다. 그러나 예외 문자열에는 내부 파일 경로, SQLite 잠금 상태, DB 스키마 관련 정보 등이 포함될 수 있다. 동일 머신의 다른 앱(또는 피싱 웹페이지)이 이 정보를 수집할 경우 내부 디렉토리 구조가 노출된다.

이 패턴은 기존 코드 전체에 광범위하게 존재하므로(out-of-scope 주석), 이번 티켓 신규 추가 라인 기준으로만 기록한다.

**수정 권고**

```python
# routes.py:3269~3274 수정 제안
except Exception as e:
    logger.exception(f"일괄 처리: 회의 목록 조회 실패: {e}")
    raise HTTPException(
        status_code=500,
        detail="회의 목록 조회 중 오류가 발생했습니다. 로그를 확인하세요.",
    ) from e

# routes.py:3297~3302 수정 제안
except OSError as e:
    logger.exception(f"일괄 처리: 체크포인트 디렉토리 스캔 실패: {e}")
    raise HTTPException(
        status_code=500,
        detail="체크포인트 디렉토리 스캔 중 오류가 발생했습니다. 로그를 확인하세요.",
    ) from e
```

---

### [LOW-02] scope=all 시 체크포인트 디렉토리 전체 스캔 — 대용량 처리 보호 없음

- **심각도**: Low
- **분류**: DoS / 자원 소진
- **위치**: `/Users/youngouksong/projects/meeting-transcriber/api/routes.py` 라인 3291~3296

**문제 설명**

`scope="all"` 경로에서 `checkpoints_dir.iterdir()` 로 전체 디렉토리를 스캔한다. 회의가 1만 개 이상 쌓인 경우, 각 항목마다 `_classify_meeting_for_batch()` 에서 파일 시스템 접근이 2회 발생한다 (총 2만 회 I/O). 이는 `asyncio` 이벤트 루프를 블로킹하지는 않지만 — `iterdir()` 와 `is_file()` 은 동기 호출로 이벤트 루프에서 직접 실행된다 — 응답 시간이 수 분에 달할 수 있다.

또한 이 경로에는 상한 없이 백그라운드 태스크가 큐잉되므로, scope=all 요청을 반복 전송하면 `running_tasks` set 이 무한정 증가한다.

**수정 권고**

1. `scope="all"` 처리 시 `asyncio.to_thread()` 를 사용해 파일 시스템 스캔을 별도 스레드로 위임한다.
2. 스캔 결과에 상한 (예: `max_candidates = 2000`) 을 두고 초과 시 경고 로그를 남긴다.
3. 동일 `action+scope` 조합의 중복 요청을 짧은 시간(예: 10초) 내에 차단하는 간단한 쿨다운을 적용한다.

---

## 3. PASS 항목 상세 근거

### CSRF / Origin — PASS

`/api/server.py` 라인 360~374 에서 `CORSMiddleware` 가 `allow_origins` 를 `["http://127.0.0.1:{port}", "http://localhost:{port}", ...]` 로 제한한다. `POST /api/meetings/batch` 는 기존 다른 POST 엔드포인트와 동일한 미들웨어 보호를 받으며, 별도의 약화 조건이 없다.

단, CORS 는 브라우저 정책이다. 같은 머신에서 실행되는 비브라우저 프로세스(curl, Python requests 등)는 Origin 헤더를 보내지 않아도 접근 가능하다. 이는 로컬 전용 앱의 구조적 한계이며, 이번 티켓 신규 코드의 문제가 아니다.

### 프론트엔드 XSS — PASS

**회의 제목 렌더링**: `spa.js` 라인 1166에서 `titleEl.textContent = App.extractMeetingTitle(meeting)` 으로 `textContent` 를 사용해 HTML 삽입 불가.

**토스트 메시지**: `BulkActionBar._showToast()` (라인 1664)와 `_showAlert()` (라인 1694) 모두 `App.safeText(el, text)` 를 호출한다. `safeText` 는 `app.js` 라인 104~106에서 `el.textContent = text` 로 구현되어 있어 HTML 인젝션이 불가능하다.

**data-meeting-id 속성**: `spa.js` 라인 1097에서 `item.setAttribute("data-meeting-id", meeting.meeting_id)` 로 설정된다. `setAttribute` 는 값을 속성 문자열로 처리하므로 XSS 벡터가 되지 않는다. 또한 서버에서 수신한 `meeting_id` 는 이미 `_validate_meeting_id` 정규식을 통과한 값이다.

**서버 응답 echo**: `BatchActionResponse.message` 는 서버 내부에서 정적으로 생성된 문자열(`"{queued}건 처리, {skipped}건 건너뜀"`)이며 사용자 입력을 직접 포함하지 않는다.

### 로깅 안전 — PASS

신규 추가 로깅 라인 (`routes.py` 3047, 3174, 3330, 3378~3404, 3416~3418) 에서 기록하는 정보는 `meeting_id`, `action`, `classification` 으로 제한된다. 회의 제목, 전사 내용, 사용자 입력 자유 문자열은 포함되지 않는다. `audio_path` 는 WARNING 레벨에서 기록되나 이는 실패 원인 진단에 필요한 정보이며 외부 노출 경로가 없다.

---

## 4. 추가 관찰 (이번 티켓 범위 내, 수정 불필요 수준)

### [INFO-01] 백그라운드 태스크 동시 실행 누적

`batch_action` 은 매 요청마다 새 `asyncio.Task` 를 생성한다. 기존 태스크가 완료되지 않은 상태에서 추가 요청이 오면 두 태스크가 동시에 `pipeline.run()` 을 호출할 수 있다. `PipelineManager._llm_lock` 이 LLM 단계를 직렬화하므로 메모리 충돌은 방지되지만, 전사(STT) 단계는 잠금 없이 중첩 실행된다.

이는 이번 티켓의 설계 범위 내 결정으로 보이며(코드 주석 `routes.py:3071~3072` 참조), 현재 사용 패턴(한 명의 로컬 사용자)에서 실질적 위협은 낮다. 향후 동시 요청이 문제가 되면 `app.state` 에 배치 진행 중 플래그를 두어 중복 요청을 405로 반환하는 방법을 고려할 수 있다.

### [INFO-02] `assert` 구문 (production 환경 주의)

`routes.py` 라인 3375~3377에 `assert audio_path is not None` 구문이 있다. Python 을 `-O` (최적화) 플래그로 실행하면 assert 가 제거된다. 현재 프로젝트 실행 방법에서 `-O` 를 사용하지 않으므로 실질적 위험은 없으나, 방어 프로그래밍 관점에서 `assert` 대신 명시적 예외를 권장한다.

```python
# 현재 (routes.py:3375)
assert audio_path is not None, f"audio_path 사전 검증 누락 — meeting_id={mid}"

# 권장
if audio_path is None:
    raise RuntimeError(f"audio_path 사전 검증 누락 — meeting_id={mid}")
```

---

## 5. 최종 판정

**판정: 수정 권고 (배포 전 Medium 항목 2건 검토 필요)**

| 심각도 | 건수 |
|---|---|
| Critical | 0 |
| High | 0 |
| Medium | 2 |
| Low | 2 |
| Info | 2 |

**즉시 처리 필요 (Medium)**

1. `MEDIUM-01`: `BatchActionRequest.meeting_ids` 에 `max_length=500` 추가 — 1줄 수정
2. `MEDIUM-02`: `_resolve_audio_path()` 반환 전 베이스 디렉토리 경계 검증 추가

**배포 후 단기 처리 권고 (Low)**

3. `LOW-01`: 500 detail 에서 내부 예외 문자열 제거 → "로그를 확인하세요"로 대체
4. `LOW-02`: scope=all 스캔에 상한 및 비동기 처리 적용

Critical 및 High 취약점은 발견되지 않았다. 로컬 전용 아키텍처를 고려하면 현재 수준의 위협 노출도는 수용 가능한 범위이나, Medium-01(DoS)과 Medium-02(경계 검증)는 코드 변경 비용이 낮고 방어 효과가 명확하므로 배포 전 수정을 권장한다.
