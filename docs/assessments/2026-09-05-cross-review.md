# 통합 검토자의 교차 검증

평가 기준: 2026-09-05, main, `e3e4b61f2d5730e257fa46b79a06bcf1e09d575e`.
시작 시 Git 변경 없음. 제품 코드 수정, 실제 회의 읽기, 모델 실행, 서버 실행,
외부 음성 전송, 의존성 설치, 게시 작업을 수행하지 않았다.

## 추가 결함: WebSocket 연결에 출처 검증이 없다

- 우선순위: P1, 외부 배포/프라이버시 홍보 확대 전 수정.
- 코드: `api/websocket.py:168`, `api/websocket.py:382`.
- 전역 미들웨어: `api/server.py:514`, `api/server.py:567`.
- 노출 가능한 실제 이벤트 필드: `steps/recorder.py:879`의 회의 ID, 파일 절대경로,
  오디오 장치와 길이. `core/orchestrator.py:1033`은 작업 실패 메시지도 방송한다.
- 원인: `/ws/events`가 Origin/Host/세션 토큰을 검사하지 않고 ConnectionManager에
  연결을 넘긴다. manager는 연결 수만 확인한 뒤 accept한다.
- 기존 OpenAI의 `require_loopback_server`는 선택된 HTTP 기능용이며 이 경로에는 적용되지 않는다.
- Starlette 공식 CORS 구현은 HTTP가 아닌 scope를 그대로 다음 앱에 전달한다.
  [공식 구현](https://github.com/Kludex/starlette/blob/main/starlette/middleware/cors.py)
  (2026-09-05 조회). CORS를 WebSocket 접근 통제로 볼 수 없다.
- 재현: `/tmp/mt-boundary-audit-20260905.py`. 실제 websocket 모듈의 FastAPI
  import/type/decorator만 stub으로 대체하고, 나머지 구현은 그대로 실행했다.
  `Origin: https://untrusted.example` 연결이 수락됐고 합성 `recording_stopped`의
  `/synthetic/private.wav`가 수신됐다.
- 관측 결과: `hostile_origin_accepted=true`, `events_received=[system_status,
  recording_stopped]`, `synthetic_path_received=true`.
- 검증 한계: 실제 브라우저에서 외부 사이트→localhost 네트워크 연결이 성공하는지는
  확인하지 않았다. 브라우저의 로컬 네트워크 권한, 혼합 콘텐츠 정책 등도 영향을 준다.
  모든 브라우저에서 즉시 악용 가능하거나 오디오 본문이 유출됐다고 주장하지 않는다.
- 개선: accept 전에 예상 Origin/Host를 정확히 제한하고, Origin 없는 네이티브
  클라이언트는 명시적 세션 인증 경로로 구분한다. 이벤트의 경로·에러 정보 최소화.
  HTTP mutation 경로도 공통 출처/CSRF 경계 검토 대상으로 묶는다.
- 후속 검증: 허용 Origin, 거부 Origin, Origin 없음, Host 변조, 정상 네이티브
  연결을 실제 ASGI 테스트에서 검증하고 지원 브라우저별 교차 출처 연결을 별도 확인한다.

## 다른 분석 결과의 독립 확인

1. 전사 편집: `api/routers/meeting_detail.py:3191` 부근의 파일 저장 이후
   `_json_cache.invalidate(target)`만 수행한다. 검색·chunk·embed의 revision
   무효화 또는 재색인 등록이 없다는 점을 직접 확인했다.
2. Wiki 범위: `core/wiki/chat_integration.py:253`, `:283`에서 kwargs는 RAG에만
   전달되고 `_synthesize_from_wiki(query, verdict)`에는 전달되지 않는다.
   해당 함수가 Wiki 전체 decision 검색을 실행하므로 회의/날짜/화자 범위가 유실된다.
   다중 사용자 간 권한 침해라고 확대 해석하지 않고 현재 단일 사용자 내 범위 오류로 판정한다.
3. 재색인 날짜: `core/reindex_recovery.py:146`의 이름 패턴 미일치 fallback은
   오늘 날짜이며 `core/pipeline.py:2099`의 원래 오디오 mtime fallback과 다르다.
4. 백필 상태: `api/routers/reindex.py:133`의 indexed 정의가 Chroma 청크 1개 이상이며,
   조회 helper도 Chroma만 읽는다. FTS 및 원문 revision 건강성을 보장하는 지표가 아니다.
5. 화자 검색: `steps/embedder.py:457`의 CSV 문자열과
   `search/hybrid_search.py:354`의 `$contains` 조합을 확인했다.
   실제 설치 버전 Chroma에서의 동작 검증은 의존성 부재로 실행하지 못했다.

## 이번 실행의 검증 기록

| 검사 | 실제 결과 | 해석 |
|---|---|---|
| `git status --short` | clean | 기존 사용자 변경 없음 |
| `.venv/bin/python -m pytest -m harness tests/harness -q` | exit 127 | 가상환경 없음. 테스트 실패 판정이 아니라 미실행 |
| `.venv/bin/python -m pytest tests/test_quality_evals.py tests/test_stt_quality_metrics.py -q` | exit 127 | 동일 |
| Python 3.11/3.12 모듈 가용성 검사 | pytest/FastAPI/Pydantic 등 없음 | 설치하거나 임의 우회하지 않음 |
| 추적 Python 전체 `ast.parse(..., feature_version=(3,11))` | 355개 통과 | 구문 검사만, import/실행 보장 아님 |
| 추적 `ui/web/*.js`에 `node --check` | 17개 통과 | 구문 검사만, 브라우저 동작 보장 아님 |
| `python3.12 /tmp/mt-boundary-audit-20260905.py` | exit 0, 경계 누락 재현 | 실제 모듈 + FastAPI 경계 stub, 네트워크 미사용 |

`docs/STATUS.md`의 3915 passed 등은 과거 기록이며 이번 결과로 재사용하지 않았다.
실제 음성 품질, Metal/pyannote 성능, 메모리 피크, 8시간 연속 녹음,
깨끗한 Mac 설치, 브라우저 E2E, 유저 전환/매출은 이번에 실측하지 않았다.


---

# 개선 담당자의 독립 교차 검토

2026-09-05. `/tmp/mt-cross-review-20260905.md`, `/tmp/mt-functional-audit-20260905.md`와 관련 실제 코드를 읽기 전용으로 대조했다. 저장소 수정 및 사용자 데이터 접근 없음.

## 주장과 우선순위

1. **F1 전사 수정 후 오래된 검색: P1 유지.** `api/routers/meeting_detail.py:3197` 저장 뒤 `:3203`은 JSON 캐시만 무효화한다. 사용자가 고친 숫자·일정의 일관성 문제라 우선순위가 충분하다. 단, 이미 색인된 회의를 편집하는 조건이며 모든 신규 전사가 틀렸다는 의미는 아니다. 요약 자동 갱신 부재는 별도 제품 정책 제약과 구분한다.
2. **F2 Wiki 필터: P2 유지, 조건 표시.** `core/wiki/chat_integration.py:258/284`에서 범위 kwargs 유실을 재확인했다. `config.py:1046`의 router_enabled는 기본 False이며 선택 활성 시에만 영향. ‘기본 AI 채팅이 회의 필터를 항상 무시’ 또는 다중 사용자 권한 침해라는 표현은 과장이다.
3. **F3 날짜: P2 유지, 제목 수정 권장.** 정확한 제목은 ‘일반 파일명 회의를 재색인하면 **검색 인덱스의 날짜**가 오늘로 변경될 수 있음’. `core/reindex_recovery.py:154`와 `core/pipeline.py:2099`의 불일치는 확실하지만 원본 오디오·DB 생성일·모든 화면의 회의 날짜가 바뀐다는 증거는 없다. timestamp ID는 예외이며 최초 색인과 재색인이 다른 날일 때 차이가 드러난다.
4. **F4 백필 누락: P2 유지.** `api/routers/reindex.py:119`의 API 자체 정의는 Chroma 청크 1개 이상이다. 따라서 API가 자신이 정의한 숫자를 잘못 계산하는 버그보다는 사용자에게 제공하는 **전체 검색 건강성 진단 및 복구 범위 부족**이다. FTS-only 유실·부분 색인에는 실질적인 기능 결함이다. 단건 재색인 API로 복구 가능하므로 영구 불능이나 모든 검색 중단으로 표현하지 않는다.
5. **F5 화자 검색: P2 유지, 증거 등급 구분.** `steps/embedder.py:457`의 scalar CSV와 `search/hybrid_search.py:354`의 contains 조합을 재확인했다. 실제 Chroma가 설치되어 있지 않으므로 현재 기기에서 발생한 예외 문구/결과 건수/사용자 피해를 확정하지 않는다. ‘저장·질의 계약 불일치, 런타임 재현은 추가 필요’가 맞다.
6. **WebSocket: 서버 측 경계 누락 판정 유지.** stub 재현은 서버 endpoint가 hostile Origin을 거절하지 않는다는 증거다. 실제 브라우저의 로컬 네트워크/혼합 콘텐츠 제약을 통과한 공격, 음성 본문 유출, 현재 진행 중 침해가 입증된 것은 아니다. 프라이버시를 제품 가치로 홍보하므로 배포 전 P1 우선순위는 합리적이다.

## metadata/date 영속성과 기존 보존 안전성

- **날짜를 보존하는 방향은 적절하지만, 기존 파생 인덱스를 무조건 원장으로 승격하면 안 된다.** 이미 재색인되어 잘못된 오늘 날짜가 있을 수 있다. `core/job_queue.py:1722`도 created_at은 등록 시각이며 회의 시각과 다름을 명시한다. 새 canonical `meeting_date`와 값의 출처/결정 시점을 별도로 기록하고, 기존 회의에서 근거가 충돌하면 보존·미확정 표시 또는 사용자 확인을 택한다.
- 최초 입력 시 검증한 source mtime 또는 명시적인 회의 시각을 저장해 이후 매번 mtime을 다시 해석하지 않는 편이 안정적이다. 파일 복사·교체로 mtime이 달라질 수 있고 mtime 자체가 실제 회의일의 확정 증거도 아니므로 provenance가 필요하다.
- 현재 `_derive_meeting_date`는 `lstat`와 일반 파일 검사를 한다(`core/pipeline.py:2125`). 공통 함수로 옮길 때도 lexical/no-follow 검사를 약화하거나 `resolve()/stat()`로 symlink를 따라가면 안 된다. 재색인의 source read→index/checkpoint 게시를 감싸는 `core/reindex_recovery.py:92`의 회의 lease를 그대로 유지한다.
- **‘DB transaction 안에서 파일 저장+dirty marker’는 하나의 원자적 commit이 아니다.** 파일·SQLite·Chroma는 서로 다른 저장소다. 먼저 내구성 있는 intent/dirty 상태를 남기고 원문을 게시하며, crash 후 재조정할 수 있는 절차가 필요하다. 반대로 원문 저장 뒤 dirty 기록 전에 종료되면 현재의 stale 문제를 다시 만든다. 보수적으로 dirty만 남는 경우는 자동 복구 가능하게 설계한다.
- 원문의 revision/hash와 두 인덱스의 generation을 연결하고, 두 저장소가 모두 완료한 뒤에만 current 상태로 표시한다. 반영 중·실패 시 이전 결과를 최신처럼 보여주지 않는다. 신규 marker는 기존 no-replace/pinned writer 규칙 및 회의 coordinator 계약을 따르고 기존 `reindex_required.json`을 무조건 덮어쓰거나 삭제하지 않는다.
- 화자 metadata schema 변경은 기존 인덱스 전체 삭제를 즉시 실행하는 식으로 해결하지 않는다. 현재 지원 버전 범위의 reader/writer 계약과 migration 계획을 먼저 정하고, source 산출물과 기존 사용자가 수정한 요약을 보존한 채 새 generation을 검증한다. production 의존성 최소 버전 변경은 별도 승인 범위다.

## 교차 검토 결론

새 결함을 취소할 근거는 찾지 못했다. F1은 가장 직접적인 사용자 신뢰 훼손으로 P1, 나머지 F2~F5는 조건부 P2가 적절하다. 날짜 제목과 F4의 ‘API 산식 오류 vs 진단 범위’ 구분을 조정하면 보고서가 더 정확하다. 수정 작업은 별도 실행·회귀 검증이 필요하며 이번 교차 검토는 정적 코드 확인이다.
