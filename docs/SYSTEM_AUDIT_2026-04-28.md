# 시스템 미흡 사항 감사 보고서

> **작성일**: 2026-04-28
> **범위**: meeting-transcriber 전체 시스템 (UI/UX, 코드 품질, 미구현 기능, 아키텍처)
> **목적**: 보안 취약점이 아닌 디자인·UX·코드 품질·미구현 항목 식별

---

## 한 줄 요약

핵심 엔진(전사·요약·검색)은 튼튼하지만, **"문 손잡이"(UI 마감)와 "약속한 부가 기능"이 마무리 안 된 상태**.

---

## 🎯 우선순위 Top 5 (즉시 개선 권장)

| # | 항목 | 영향도 | 비고 |
|---|------|--------|------|
| 1 | 파일 업로드 API 구현 | 🔴 높음 | UI는 있는데 백엔드 없음 — 가장 큰 갭 |
| 2 | Command Palette(⌘K) 실제 연결 | 🔴 높음 | 단축키 안내만 있고 동작 안 함 |
| 3 | 빈 상태/스켈레톤/다크모드 톤 | 🟡 중간 | design.md 가이드라인 충실 적용 |
| 4 | `BaseStep` ABC + CheckpointManager 분리 | 🟡 중간 | 파이프라인 유지보수성 |
| 5 | `api/routes.py` 도메인별 분할 | 🟢 낮음 | 4,349줄 → search/chat/settings/upload |

---

## 🚪 1. UI/UX 미흡 (영향도: 높음)

집의 문은 만들었는데 손잡이가 없는 상태와 같다.

### 1.1 Command Palette (⌘K) 미완성
- **위치**: `ui/web/spa.js:7616~8194`
- **증상**: `index.html:16,27,43`의 `<kbd>` 단축키 힌트가 표시되지만 실제로 누르면 동작하지 않음
- **원인**: 모듈 코드는 작성되었으나 SPA 라우터/메인 UI에 통합 미완료
- **해결**: ⌘K 키 이벤트 바인딩 + 팔레트 모듈 활성화

### 1.2 빈 상태(Empty State) 패턴 미적용
- **위치**: `ui/web/spa.js:950~952`
- **증상**: 회의가 없을 때 단순히 "회의 없음" 텍스트만 표시
- **기준 위반**: `docs/design.md §3.7` (48px 아이콘 + 제목 + 설명 + CTA 버튼)
- **해결**: 아이콘·안내 문구·"회의 시작" CTA 추가

### 1.3 스켈레톤 shimmer 애니메이션 누락
- **위치**: `ui/web/app.js:585~605`
- **증상**: 로딩 중 회색 박스만 표시되고 반짝이는 애니메이션 없음
- **기준 위반**: `docs/design.md §3.8`
- **해결**: CSS `@keyframes shimmer` 정의 추가

### 1.4 다크모드 톤 격차 부족
- **위치**: `ui/web/style.css:149~230`
- **증상**: 어두운 색은 적용됐지만 깊이감이 부족해 영역 구분이 흐림
- **기준 위반**: `docs/design.md §1.1` (`#1C1C1E`/`#2C2C2E`/`#3A3A3C` 단계별 톤)
- **해결**: 배경 레이어별 색상 값 재조정

### 1.5 ARIA 동기화 누락 (접근성)
- **위치**: `ui/web/index.html:90`
- **증상**: `role="listbox"`로 선언했으나 `aria-selected`/`aria-current` 속성이 동적으로 업데이트되지 않음
- **영향**: 스크린 리더 사용자가 현재 선택된 항목을 인식할 수 없음
- **해결**: `spa.js`의 선택 변경 핸들러에서 ARIA 속성 갱신

### 1.6 `:focus-visible` 부분 적용
- **위치**: `ui/web/style.css:515`
- **증상**: 일부 요소에만 키보드 포커스 표시
- **해결**: 모든 인터랙티브 요소(버튼/입력/링크)에 일관된 `:focus-visible` 스타일 적용

### 1.7 모바일 반응형 불완전
- **위치**: `ui/web/style.css:3717~3758`
- **증상**: 768px 이하에서 List Panel이 숨겨지지만 다시 여는 진입로(햄버거 메뉴 등) 없음
- **해결**: 모바일 토글 버튼 추가 또는 Sheet/Drawer 패턴 적용

---

## 🚧 2. 미구현 / 반쪽 구현 기능 (영향도: 높음)

약속한 기능 중 일부가 누락되거나 절반만 구현되어 있다.

### 2.1 파일 업로드 API (가장 큰 갭) 🔴
- **약속 위치**: `README.md:362`, `ui/web/spa.js:630~751`
- **현재 상태**: UI에서 파일 선택 → 파일 큐 표시까지만 동작
- **누락**: 실제 `POST /api/upload` 엔드포인트 부재
- **현재 우회**: 사용자에게 `~/.meeting-transcriber/audio_input/` 폴더에 수동 복사 안내(`spa.js:751`)
- **해결**: 멀티파트 업로드 엔드포인트 + 진행률 WebSocket 연동

### 2.2 Archive 정책 (외장 디스크 이동)
- **약속 위치**: `config.yaml:166`, `security/lifecycle.py:59`
- **현재 상태**: `ColdAction` enum만 정의, 구현 없음(`lifecycle.py:63`)
- **현재 동작**: `delete_audio`만 작동
- **해결**: 외장 디스크 마운트 감지 + rsync 기반 이동 로직

### 2.3 데이터 라이프사이클 자동 스케줄러
- **약속 위치**: `config.yaml:163~166` (Hot 30일 / Warm 90일 / Cold)
- **현재 상태**: 설정값만 있고 자동 실행 없음
- **현재 동작**: 관리자가 수동으로 `lifecycle` 함수 호출해야 작동
- **해결**: APScheduler 또는 launchd 기반 일일 스케줄러

### 2.4 CPU 온도 모니터링 ⚠️
- **약속 위치**: `core/thermal_manager.py:74~79`
- **현재 상태**: Apple Silicon 권한 제약으로 온도 직접 읽기 불가
- **실제 작동**: 배치 카운터(2건 후 3분 쿨다운)만 작동
- **해결**: `powermetrics`(sudo 필요) 대신 SMC 라이브러리 또는 부하 기반 추정

### 2.5 Zoom 오디오 소스 자동 전환
- **약속 위치**: `README.md:322~334`
- **현재 상태**: Zoom 회의 감지·녹음은 동작
- **누락**: BlackHole/마이크 자동 전환은 설정값 의존(수동 모드)
- **해결**: Zoom 프로세스 시작 시 CoreAudio API로 입력 장치 동적 전환

---

## 🔧 3. 코드 품질 미흡 (영향도: 중간)

### 3.1 거대 파일 (책임 과다)
부엌에 냉장고·세탁기·TV·침대를 다 넣어둔 상태와 같다.

| 파일 | 줄 수 | 권장 분할 |
|------|-------|----------|
| `api/routes.py` | 4,349 | search / chat / settings / upload / models |
| `core/pipeline.py` | 1,720 | orchestrator / checkpoint_manager / resource_guard |
| `steps/recorder.py` | 1,229 | recorder / audio_source / format_detector |
| `core/ab_test_runner.py` | 1,095 | runner / metrics / reporter |
| `core/job_queue.py` | 1,071 | queue / persistence / scheduler |

### 3.2 타입 힌트 `Any` 남발
- **위치**:
  - `core/orchestrator.py:40~46` — `job_queue: Any, pipeline: Any, thermal_manager: Any`
  - `api/routes.py:33~51` — `task: asyncio.Task[Any]` 등
- **권장**: `typing.Protocol` 도입으로 구체적 인터페이스 정의

### 3.3 빈 except 블록 (조용한 실패)
- **위치**:
  - `api/routes.py:3295, 3305, 3307` — 주석만 있는 빈 블록
  - `api/websocket.py:280` — 명시적 처리 없음
  - `core/orchestrator.py:280~282` — 예외 무시
- **권장**: 최소한 `logger.debug()` 추가, 의도된 무시는 주석 명시

### 3.4 `print()` 사용
- **위치**: `main.py:383`
- **문제**: logger 일관성 깨짐
- **해결**: `logger.error()` 또는 `logger.info()`로 교체

### 3.5 하드코딩 매직 넘버
| 위치 | 상수 |
|------|------|
| `core/user_settings.py:50~58` | `_PROMPT_MIN_LEN=20`, `_VOCAB_MAX_TERMS=500` |
| `core/ollama_client.py:34` | `_CONNECTION_CACHE_TTL_SECONDS=300.0` |
| `api/routes.py:64` | `_JsonFileCache` 최대 크기 64 |

→ `config.yaml`로 이동 권장

---

## 🏗️ 4. 아키텍처 결함 (영향도: 중장기)

### 4.1 Step 인터페이스 비일관 🔴
- **문제**: 8개 스텝(transcriber, diarizer, merger, corrector...)이 각각 다른 결과 타입(`TranscriptResult`, `DiarizationResult`, `MergedResult` 등) 반환
- **누락**: 추상 베이스 클래스(ABC) 부재
- **해결**:
  ```python
  # core/base_step.py (제안)
  class BaseStep(ABC):
      @abstractmethod
      async def run(self, input: StepInput) -> StepOutput: ...
  ```

### 4.2 `main.py` 책임 과다
- **위치**: `main.py` (434줄)
- **담당**: 설정 로드 + 로깅 + 디렉토리 초기화 + 시그널 처리 + 스레드 관리 + 단일 인스턴스 락
- **해결**: `Bootstrap` / `AppLifecycle` 클래스로 분리, `orchestrator.py`와 역할 명확화

### 4.3 체크포인트 로직 분산
- **위치**: `core/pipeline.py:547~572`
- **문제**: JSON 저장/로드 로직이 파이프라인 내부에 산재
- **위험**: `_rebuild_state_from_checkpoints()` 부분 실패 시 상태 불일치
- **해결**: `core/checkpoint_manager.py` 분리 (저장/로드/검증/복구 단일 책임)

### 4.4 비동기/동기 혼재
- **위치**:
  - `main.py:242~294` — threading 기반 FastAPI(`_ServerThread`)
  - `core/orchestrator.py:151` — `asyncio.to_thread()` 호출
- **문제**: 두 모델 혼재로 디버깅 복잡도 증가
- **해결**: rumps(메인 스레드 강제) 제약을 명시한 후, 나머지는 단일 asyncio 루프로 통일

### 4.5 예외 계층 일관성 부족
- **현재**: `PipelineError`, `PipelineStepError`, `InvalidInputError` 혼재
- **위치**: `core/pipeline.py:587~622`
- **문제**: API 라우터에서 HTTP 응답 매핑이 명시적이지 않음
- **해결**: 예외 계층 정의 + FastAPI `exception_handler` 등록

### 4.6 ResourceGuard의 제한적 활용
- **위치**: `core/pipeline.py:117~214`
- **현재**: LLM 단계 선택적 스킵만 가능
- **개선**: 단계별 graceful degradation 정책 (예: 화자분리 실패 시 단일 화자 가정)

---

## 📊 영향도 매트릭스

| 영역 | 사용자 체감 | 개발자 부담 | 우선순위 |
|------|------------|------------|----------|
| 파일 업로드 API | 🔴🔴🔴 | 🟡🟡 | **즉시** |
| ⌘K 단축키 | 🔴🔴 | 🟢 | **즉시** |
| 빈 상태/다크모드 | 🟡🟡 | 🟢 | 1차 스프린트 |
| 라이프사이클 자동화 | 🟡 | 🟡🟡 | 2차 스프린트 |
| BaseStep ABC | 🟢 | 🔴🔴 | 리팩토링 시즌 |
| 거대 파일 분할 | 🟢 | 🔴🔴 | 리팩토링 시즌 |

---

## 📚 참고 문서

- `docs/design.md` — 디자인 토큰 및 컴포넌트 패턴
- `README.md` — 약속된 기능 목록
- `CLAUDE.md` — 아키텍처 핵심 규칙
- `config.yaml` — 설정 항목 단일 진실 공급원

---

## 다음 단계 제안

1. **즉시**: 우선순위 Top 2(파일 업로드 API, ⌘K) 이슈 등록
2. **1차 스프린트**: UI 마감(빈 상태/스켈레톤/다크모드/ARIA) 일괄 처리
3. **2차 스프린트**: 미구현 기능(Archive, 자동 스케줄러) 완성
4. **리팩토링 시즌**: `BaseStep` ABC 도입 → 거대 파일 분할 → 예외 계층 정리

---

*본 보고서는 2026-04-28 시점 코드 기준이며, 코드 변경 시 file_path:line_number 갱신이 필요합니다.*
