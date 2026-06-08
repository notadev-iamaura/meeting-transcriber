# C2 — 위키 현황 다이제스트 (집계, LLM 0) 설계

> 상태: 설계 확정 (2026-06-08). 단일 진실 공급원 `docs/plans/2026-06-03-memorable-wiki-system.md` 의 **C2** 구현. C1(다중신호 랭킹)·G1(시맨틱 회상) 과 독립적인 **코어** 기능(모델 로드 0).

## 0. 한 문장

위키에 쌓인 결정/액션을 **LLM 없이 순수 집계**해 "지금 내 미해결 액션 / 최근 결정 / 프로젝트별 현재 상태"를 항상 최신인 작은 현황판(`digest.md`)으로 만든다. 검색·채팅 없이 한눈에 업무 상태를 본다.

## 1. 동기 (계획서 §1 격차 ①③의 *실제* 니즈)

- 격차 ①(자동 망각/압축): 컨텍스트 압박이 없으므로 진짜 니즈는 "압축"이 아니라 **항상 보이는 작은 현황 요약**.
- 격차 ③(메모리 계층화): index/digest 를 "core" 표면으로, 상세는 검색으로. 페이징 불필요.
- → 둘 다 **집계(LLM 0)** 로 충족. "working memory" 질문("내 미해결 액션은?", "이번 주 결정?", "A 프로젝트 상태?")에 즉답.

## 2. 불변식 준수 (계획서 8개)

1. **인용 무결성**: 모든 다이제스트 줄은 원본 인용(`[meeting:id@ts]`)을 그대로 보존. 집계는 원문에서 줄을 *선별·재배치*할 뿐 본문/인용을 생성·변형하지 않는다.
2. **점수만 조정·원문 보존**: 다이제스트는 파생 산출물(`digest.md`). 결정/액션 원장 페이지는 불변.
3. **100% 로컬**: 디스크 frontmatter/본문만 읽음. 외부 호출 0.
4. **코어 모델 로드 0**: 순수 산술/문자열 집계. e5·LLM·임베딩 전부 미사용 — 코어 불변식 핵심.
5. **기존 모듈 재사용**: `WikiStore.read_page`(frontmatter+citations 파싱), `search_index._string_list`(frontmatter 정규화), `models.Citation`.
6. **fail-loud·자동수정 금지**: 깨진 페이지 1건은 경고 후 skip(전체 차단 안 함). 다이제스트 생성 실패는 ingest 를 막지 않음(graceful, index.md 와 동일 정책).
7. **단일 대형모델·RAM·발열**: 모델 0 → 경합 없음.
8. **설정 하드코딩 금지**: `config.yaml` `wiki.digest.*`(최근 N일 등).

## 3. 데이터 소스 (디스크 원장)

| 섹션 | 소스 | 추출 |
|---|---|---|
| 미해결 액션(owner별) | `action_items.md` `## Open (N)` 섹션 | `- [ ] {owner}: {desc}{due} {cit}` 라인 파싱(`_render_open_line` 포맷 결정적) |
| 최근 결정(최근 N일) | `decisions/*.md` | frontmatter `title`·`decision_date`·`status`·`project` + `page.citations` |
| 프로젝트별 현황 | `decisions/*.md` 를 `project` 로 그룹 | 프로젝트별 `decision_date` 최댓값 결정 1건(title·status·date) |

> 정규 필드명은 `search_index._row_to_candidate` 와 동일: `decision_date`(또는 `date`), `status`, `project`(또는 `projects`, list 가능), `title`. `project` 다중값이면 각 프로젝트에 모두 계상.

## 4. 컴포넌트

### 4.1 설정 — `WikiDigestConfig` (config.py, `wiki.digest`)
```yaml
wiki:
  digest:
    enabled: true        # false면 digest.md 미생성
    recent_days: 14      # "최근 결정" 윈도(now 기준)
    max_recent: 50       # 최근 결정 안전 상한(폭주 방지)
    max_per_owner: 50    # owner당 액션 표시 상한
```

### 4.2 `core/wiki/digest.py` — 순수 집계 + 렌더 (모델 0)
- 데이터클래스(frozen): `OpenAction(owner, description, citation, due_date, raw_line)`, `RecentDecision(page_path, title, decision_date, status, project, citations)`, `ProjectStatus(project, last_title, last_date, status, page_path)`, `WikiDigest(open_actions_by_owner, recent_decisions, project_status, total_open_actions, generated_for)`.
- `parse_open_actions(action_items_content) -> list[OpenAction]`: `## Open` 섹션의 모든 `- [ ]` 라인 파싱(다음 `##` 전까지). **누락 0** 보장 — 파싱 불가 라인도 owner="미지정"+raw 보존으로 떨군다(드롭 금지).
- `collect_recent_decisions(store, *, now, recent_days, max_recent)`: `decisions/*.md` 읽어 `decision_date >= now-recent_days` 필터, 날짜 내림차순, 상한 컷.
- `collect_project_status(store)`: 전 결정 페이지를 project 로 그룹 → 프로젝트별 최신 결정.
- `build_digest(store, *, config, now) -> WikiDigest`: 위 3개 조합.
- `render_digest_markdown(digest) -> str`: `digest.md` 본문(frontmatter `type: digest` + 3 섹션). 인용 그대로 노출.

### 4.3 색인 제외 — `digest.md` 를 특수 파일로
- `store.SPECIAL_FILES` + `lint._ALWAYS_VISIBLE_PAGES` 에 `digest.md` 추가 → `all_pages()` 에서 제외(검색/벡터 색인·고아 검사 오염 방지). index.md 와 동일 취급.

### 4.4 컴파일러 연결 — `compiler.py`
- `compile_meeting` step 7b(검색 색인 갱신) 직후 `_regenerate_digest()` 호출: `build_digest` → `render_digest_markdown` → `store.write_page("digest.md", ...)`. `wiki.digest.enabled` 가드, 실패는 경고 후 graceful(ingest 유지).

## 5. 테스트 (로컬·모델/시크릿 없이 결정적)
- `parse_open_actions`: 다중 owner·due·인용 보존·`## Closed` 미포함·깨진 라인 비드롭(누락 0).
- `collect_recent_decisions`: recent_days 경계·정렬·max_recent 컷·깨진 페이지 skip.
- `collect_project_status`: project 다중값·최신 결정 선택·project 없는 결정 제외.
- `build_digest`/`render`: 인용 100% 보존(입력 인용 수 == 출력 인용 수), LLM 호출 0(모델 매니저/llm 미주입으로 구조적 보장 — 의존성 자체가 없음).
- compiler wiring: digest.enabled 시 digest.md 생성, disabled 시 미생성, 생성 실패 graceful.
- 비회귀: `tests/wiki/` 전체, `all_pages()` 에 digest.md 불포함.

## 6. 수락 기준 (계획서 §C2)
- 미해결 액션·최근 결정·프로젝트 상태를 **인용과 함께 정확히 집계(누락 0)**.
- **LLM 호출 0**(구조적: 모듈이 llm/모델 의존성을 import 하지 않음).
- 단위 테스트로 누락 0·인용 보존 증명. `digest.md` 가 검색/고아 검사 오염 안 함.
- 가중치/윈도 전부 config. UI 노출은 C3(별도).

## 7. 비목표 (YAGNI)
- UI/`/app/wiki` 현황 탭·API 엔드포인트 → **C3**.
- owner별 액션을 프로젝트와 교차집계, due 임박 경고, 통계 차트 → 과설계, 제외.
- digest 의 LLM 요약(자연어 브리핑) → 계획서가 명시적으로 배제(LLM 0).
