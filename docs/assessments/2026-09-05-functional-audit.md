# Meeting Transcriber 기능 오류 감사 — 2026-09-05

담당: 기능 오류 서브에이전트. 현재 소스에 근거해 사용자에게 잘못된 결과를 주는 기능 경계를 우선 조사했다. 저장소 코드는 변경하지 않았다. 시작/종료 `git status --short`가 모두 빈 출력이다. `docs/STATUS.md`에 이미 해결됐다고 기록된 큐 backlog, diarization timeout, 모델 취소 수명주기, 회의 mutation 직렬화를 새 결함으로 재보고하지 않았다.

현재 가장 선명한 결함은 **전사 편집 결과와 검색/AI 답변의 불일치**다. 전사→교정→요약→검색의 개별 단계 자체보다, 이후 편집·필터·복구가 각 단계의 결과를 일관되게 유지하지 못하는 경계에 확정 근거가 모였다.

## 검증 수준과 제약

- `/tmp/mt-functional-repro-20260905.py`는 실제 소스의 함수 AST를 추출하여 실행한다. 테스트용 문장·파일·메모리 SQLite FTS만 사용한다. 저장소 import 및 사용자 데이터 접근은 없다.
- 의존성 경계(FastAPI/Pydantic 응답, 원자 파일 쓰기, 모델, Chroma, Wiki synthesis 등)는 단순 객체/함수로 대체했다. 따라서 이는 실제 코드 제어 흐름에 대한 **격리 재현**이지 API E2E, 실제 모델 평가, 실제 Chroma 실행 결과는 아니다.
- `python3 /tmp/mt-functional-repro-20260905.py`: exit 0, 아래 5개 assertion 그룹 모두 성공.
- `.venv/bin/python`: 없음. 시스템 Python의 `import chromadb`: `ModuleNotFoundError`. 패키지 설치 없이 진행했으므로 기존 pytest와 실모델/마이크 녹음은 미실행.
- Chroma 외부 계약은 공식 문서 https://docs.trychroma.com/docs/querying-collections/metadata-filtering 및 https://docs.trychroma.com/reference/where-filter 로 확인했다.

## F1 — [P1] 전사 수정 후 검색 및 RAG가 수정 전 텍스트를 계속 사용

**근거**

- `/Users/youngouksong/projects/meeting-transcriber/api/routers/meeting_detail.py:3189`: 발화 목록을 교체하고 :3197에서 전사 JSON을 저장한다. :3203의 invalidation은 `_json_cache`뿐이다. 함수 종료까지 chunk/embed/FTS/Chroma 갱신이나 durable dirty marker가 없다.
- 같은 파일 :3321 이후의 모두 바꾸기 경로도 JSON 변경 후 `_json_cache.invalidate(target)`만 수행한다.
- `/Users/youngouksong/projects/meeting-transcriber/ui/web/viewer-view.js:951`: `_saveTranscript()`는 PUT 성공 뒤 화면 response만 적용하며 재색인 호출이 없다. 프로젝트 프론트 `reindex` 호출 검색에서도 편집 후 경로는 발견되지 않았다.
- `/Users/youngouksong/projects/meeting-transcriber/search/hybrid_search.py:832`: 검색은 저장된 벡터/FTS 인덱스를 읽는다. 전사 JSON을 다시 읽는 경로가 아니다.

**재현**: 이미 색인된 회의의 “예산은 100만원”을 “예산은 200만원”으로 수정한다. 격리 실행에서 실제 `update_transcript()` 응답과 JSON은 200만원으로 바뀌었지만 메모리 FTS의 100만원 hit=1, 200만원 hit=0이 유지됐다. 테스트의 SQLite는 독립 저장소이며 handler의 외부 갱신 호출이 없음을 검증한다.

**영향**: 사용자가 고쳤다고 믿는 금액·담당자·일정이 검색과 AI 채팅에서는 계속 예전 값으로 답해진다. 요약도 자동 갱신되지 않으므로 수정 후 파생 문서가 오래됐음을 알려줄 필요가 있다. 현재 UI의 “전체 누락분 백필”은 벡터가 이미 존재하면 대상을 잡지 않아 일반 사용자의 복구 동선도 불분명하다.

**해결 방향**: 전사 저장 transaction/회의 lease에서 source revision과 검색 dirty marker를 내구성 있게 남기고 순차 재색인을 등록한다. 검색에서는 오래된 revision을 구별하고 재색인 대기·실패를 명시한다. 회의록은 사용자 수동 편집을 덮지 않도록 ‘전사 수정 이후 갱신 필요’ 상태를 먼저 제공한다.

**확신**: 높음. 코드 추적 + 실제 handler 격리 실행. 루트 에이전트 독립 정적 검토에서도 확인.

## F2 — [P2] Wiki 채팅 분기는 사용자의 회의·날짜·화자 필터를 무시

**근거**

- `/Users/youngouksong/projects/meeting-transcriber/api/routers/search_chat.py:501` 부근: hybrid.respond에 meeting_id_filter/date_filter/speaker_filter를 모두 전달한다.
- `/Users/youngouksong/projects/meeting-transcriber/core/wiki/chat_integration.py:258`: WIKI는 `_synthesize_from_wiki(query, verdict)`만 호출해 kwargs를 버린다.
- 같은 파일 :283~284: BOTH도 RAG에는 kwargs를 넘기지만 Wiki에는 넘기지 않는다.
- 같은 파일 :318 이후 `_synthesize_from_wiki()` 시그니처에는 필터 자체가 없고 전역 WikiStore 검색을 수행한다. WikiSearchIndex/semantic 검색에는 page_types=["decision"]만 제한한다.

**재현**: Wiki 라우터 활성 상태에서 특정 회의와 날짜를 지정한 결정 질문이 WIKI로 분기된다. 격리 실행에서 실제 `_handle_wiki()`에 회의·날짜·화자 필터를 전달했지만 synthesis 호출 인자는 query/verdict 두 개뿐이며, 다른 회의 citation이 반환되어도 그대로 사용자 응답으로 전달됐다.

**영향**: “이 회의에서 결정한 내용”이라는 질문에 다른 고객·프로젝트·시점의 결정이 섞인다. 사용자 한 계정의 로컬 데이터 내부 범위 오류이며, 이를 다중 사용자 권한 유출로 확대 해석하지 않는다.

**해결 방향**: Wiki의 문서와 citation에 필터를 적용할 때 의미를 보존할 수 있을 때만 WIKI/BOTH 허용. 그 전에는 범위 필터가 있는 질문을 RAG로 보내는 것이 명확한 복구다. 필터 적용된 Wiki 검색의 citation 검증도 회귀 테스트에 포함한다.

**확신**: 높음. 실제 분기 함수 격리 실행. 단, `config.py:1046`에서 router_enabled 기본 False이므로 선택 활성 기능에서 발생하며 기본 채팅 전부가 영향을 받는다는 주장은 하지 않는다.

## F3 — [P2] 일반 파일명 회의를 재색인할 때 검색 인덱스 날짜가 오늘로 변함

**근거**

- `/Users/youngouksong/projects/meeting-transcriber/core/watcher.py:1260`: 회의 ID는 입력 파일의 stem이다. weekly-sync.wav 같은 timestamp 없는 파일명도 정상 허용한다.
- `/Users/youngouksong/projects/meeting-transcriber/core/pipeline.py:2099`: 최초 색인 날짜는 ID의 timestamp → 오디오 mtime → 현재 시각 순서다.
- `/Users/youngouksong/projects/meeting-transcriber/core/reindex_recovery.py:154`: 재색인은 ID timestamp → 현재 시각만 사용하며 오디오 mtime/기존 날짜는 무시한다. :160에서 이 날짜로 청크를 다시 만들고 저장한다.

**재현**: synthetic weekly-sync.wav mtime을 2026-01-02로 설정. 실제 `_derive_meeting_date` 결과는 2026-01-02. 동일 ID와 audio_path로 실제 `_reindex_meeting_artifacts_locked`를 실행하되 모델/저장을 stub하면 Chunker에 전달되는 날짜가 2026-09-05로 바뀐다.

**영향**: 과거 날짜로 찾던 회의가 재색인 뒤 결과에서 사라지고, 오늘 회의 결과에 과거 회의가 들어온다. 검색 citation의 날짜도 잘못된다. timestamp 형식 ID의 앱 녹음은 해당 조건에 걸리지 않는다.

**해결 방향**: 회의 날짜를 최초 등록 시 DB/안정 메타데이터에 저장하고 모든 재색인에서 같은 값을 사용한다. 우선은 최초 파이프라인과 공통 날짜 함수로 통일하고 기존 청크 날짜/원본 오디오를 존중한다.

**확신**: 높음. 두 실제 함수의 격리 비교, 루트 독립 정적 확인.

## F4 — [P2] 검색 복구는 벡터 1개만 있으면 FTS 누락·부분 색인을 정상으로 판정

**근거**

- `/Users/youngouksong/projects/meeting-transcriber/api/routers/reindex.py:171`: `_count_chunks_for_meeting()`은 Chroma IDs 개수만 센다.
- 같은 파일 :222~225: 하나라도 있으면 indexed로 집계한다. FTS, 예상 청크 수, source revision은 보지 않는다.
- 같은 파일 :509: 전체 누락분 백필도 Chroma count==0만 대상으로 등록한다.
- `/Users/youngouksong/projects/meeting-transcriber/ui/web/settings-view.js:296` 이후 단건 재색인 버튼은 missing_meeting_ids 목록에만 제공한다.

**재현**: 실제 get_index_status를 completed meeting 1개, 살아 있는 vector ID 1개, FTS가 없는 synthetic 환경에서 실행하면 indexed=1/missing=0을 반환한다. 구현 자체가 FTS 저장소를 조회하지 않는다.

**영향**: FTS 테이블 삭제/손상, 과거 데이터 누락, 부분 저장 뒤 검색의 키워드 부분이 깨져도 설정 화면은 정상이라고 표시하고 전체 복구가 회의를 제외한다. F1의 수정 후 오래된 색인도 같은 진단으로 놓친다. 단건 API를 직접 호출하면 복구 가능하므로 영구 복구 불가능하다고 주장하지 않는다.

**해결 방향**: 완료된 source revision 기준 양쪽 인덱스의 존재와 기대 청크 집합/세대를 비교한다. 최소한 FTS 존재·회의 청크 수를 함께 검사하고 degraded/incomplete 상태를 노출해 복구 대상으로 포함한다. 전체 회의의 N번 개별 get 대신 일괄 조회도 병행할 수 있다.

**확신**: 높음. 실제 상태 함수 격리 실행과 일괄 선택 로직 추적. 실제 Chroma 파손을 조성하지 않았다.

## F5 — [P2] 화자 벡터 필터가 저장한 metadata 타입과 맞지 않음

**근거**

- `/Users/youngouksong/projects/meeting-transcriber/steps/embedder.py:457`: speakers는 `",".join(c.speakers)`인 scalar CSV 문자열로 저장된다.
- `/Users/youngouksong/projects/meeting-transcriber/search/hybrid_search.py:354`: 화자 필터는 `{"speakers": {"$contains": speaker_filter}}`로 생성된다.
- 같은 파일 :401~403: 벡터 쿼리 예외는 로그만 남기고 빈 결과로 바뀐다.
- Chroma 공식 metadata 문서에서 `$contains`는 **배열 metadata의 원소 포함** 연산이다. 문서 본문 substring 검색은 별개 `where_document` 계약이다. 공식 Cookbook은 배열 metadata가 1.5.0 이상에서 지원된다고 명시한다(https://cookbook.chromadb.dev/core/filters/).
- `pyproject.toml:46` 허용 범위는 `chromadb>=0.4.0,<2.0`으로 오래된 버전도 포함한다.

**재현/검증**: 실제 `_search_vector()`를 query capture collection에 실행하여 where가 위 형태인 것을 검증. 실제 writer의 scalar CSV 저장도 소스 확인. 설치된 Chroma가 없으므로 엔진이 어떤 문구로 예외를 반환하는지/몇 건을 반환하는지는 실행하지 않았다. 최신 공식 계약상 문자열에 배열 필터를 쓰는 불일치는 확실하고, 구버전은 해당 연산자를 지원하지 않을 수 있다.

**영향**: 화자 지정 검색/채팅에서 의미 기반 검색이 누락되거나 실패하고, FTS에 정확한 단어가 있는 경우만 살아남아 사용자가 누락을 알아차리기 어렵다.

**해결 방향**: 현재 지원 버전을 유지하려면 화자별 scalar boolean metadata와 `$eq` 같은 지원되는 조건으로 저장/질의 계약을 통일한다. 배열 metadata 채택은 최소 버전 및 기존 CSV 인덱스 migration을 함께 결정한다. 실제 지원 버전의 Chroma에 넣고 화자별 의미 검색을 검사하는 integration test가 필요하다.

**확신**: 높음(저장·질의 계약 불일치), 중간(현재 사용자 설치 버전의 구체적 런타임 증상). 엔진 실행으로 재현했다는 주장은 하지 않는다.

## 루트 발견에 대한 독립 교차 검토

`api/websocket.py:382`의 엔드포인트와 :201의 ConnectionManager.connect는 Origin/Host 검증 없이 accept·등록한다. :224의 broadcast_event는 등록된 모든 소켓에 같은 이벤트를 전송한다. `api/server.py`의 middleware 검색에서는 CORSMiddleware만 확인되며 WebSocket endpoint의 별도 검증은 없다. `steps/recorder.py:879`의 recording_stopped 이벤트 payload에 file_path가 실제 들어간다. 따라서 루트의 ‘서버 측 cross-origin WebSocket 차단 부재’ 판정에 독립 동의한다. 실제 브라우저의 로컬 네트워크/혼합 콘텐츠 정책까지 우회했다는 의미로 해석하지 않는다.

## 후속 검증 우선순위

1. 실제 Chroma+SQLite를 사용하는 synthetic 회의로 전사 편집→검색 질의→날짜 필터→재색인 회귀 시나리오를 하나의 통합 테스트로 만든다.
2. Wiki router enabled의 WIKI/BOTH 모두에 명시적 범위 필터를 붙여 다른 회의 citation이 없는지 검사한다.
3. FTS-only 유실/부분 벡터/오래된 revision의 복구 UI를 검증한다.
4. 한국어 실제 회의 STT·화자·교정 정확도는 이 코드 감사로 점수화할 수 없다. 독립 정답 음성, hardware/native runtime과 사용자 데이터 사용 동의를 갖춘 별도 품질 평가가 필요하다.
