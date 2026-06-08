# C3 — 위키 현황/검색 UI 설계·검증 기록

> 상태: 구현 완료 (2026-06-08). 계획서 §C3 "UI — 현황 화면 + 검색 메타". C2(다이제스트 집계) 위에 스택. **ui-ux producer-reviewer 멀티에이전트 워크플로**(사용자 지시)로 구현하고 메인루프가 3축 게이트를 재검증했다.

## 0. 한 문장

위키에 **"현황(Overview)" 탭**을 신설해 C2 다이제스트(`GET /api/wiki/digest`)를 렌더하고, 검색 결과를 메타(score·status·snippet·citations) 카드로 승격해, **채팅 없이** 업무 현황을 한눈에 본다. 인용은 뷰어 deep link.

## 1. 범위 / 비범위

- **범위**: (1) 백엔드 `GET /api/wiki/digest`(C2 집계 노출, 읽기 전용), (2) WikiView 에 ARIA 탭 바(현황/검색) + 현황 패널(4섹션) + 검색 카드 메타, (3) 인용 → `/app/viewer/{id}?t=초` deep link(기존 위임 패턴 재사용).
- **비범위**: C4(골든셋·실데이터), digest 의 LLM 브리핑/차트(C2 §7 배제), SPA 라우터 변경(탭=뷰 내부 상태), 백엔드 검색/스키마 변경(이미 score·snippet·citations·metadata 반환).

## 2. 구현

### 2.1 백엔드 — `GET /api/wiki/digest` (`api/routers/wiki.py`)
- `core.wiki.digest.build_digest` 집계를 구조화 JSON(`WikiDigestResponse`)으로 노출. owner별 액션 그룹·최근 결정·프로젝트 상태, 각 항목 `citations` 보존. wiki 비활성/부재/실패 시 빈 다이제스트(200, graceful). 모델 로드 0.
- 테스트 2건(`tests/wiki/test_routes.py`): disabled 빈 응답·집계+인용 보존. **기본 CI 게이트 포함**(non-ui).

### 2.2 프론트 — `ui/web/wiki-view.js` + `ui/web/wiki.css`
- **ARIA 탭**: `role=tablist` 안 `현황`(#wikiTabOverview, 기본 활성)·`검색`(#wikiTabSearch). roving tabindex + ←→/Home/End/Enter/Space, 비활성 패널 `hidden`. `?tab` 은 `history.replaceState` 로만(라우터 미변경).
- **현황 패널**: 진입 시 digest 1회 fetch(`_digestLoaded` 가드). 4섹션 카드(미해결 액션 owner별·최근 결정·프로젝트 현황). 전체 빈 → 기존 `.wiki-empty-state` 재사용.
- **검색 카드**: 기존 결과 렌더를 `.wiki-result-card` 로 승격 — score·status 배지·snippet·citations. **status 배지는 dot(상태색)+텍스트(중립 `--text-secondary`)** 로 WCAG 대비 확보(색상 텍스트 회피).
- 디자인 토큰만 사용(하드코딩·신규 토큰 0). 기존 검색/필터/상세/Health/인용 동작 보존.

## 3. 3축 게이트 검증 (메인루프 재검증)
- **behavior** (`tests/ui/behavior/test_wiki_overview.py`, 26건): 탭 구조·키보드·digest 4섹션·인용 deep link·검색 카드 메타·회귀·destroy 정리. **PASS**.
- **a11y** (`tests/ui/a11y/test_wiki_overview.py`, 8건): ARIA 탭 계약·axe scoped 위반 0·키보드 도달·focus-visible. **PASS**.
- **visual** (`tests/ui/visual/test_wiki_overview.py`, 4 baseline): 현황 light/dark/mobile + 검색카드 light. baseline 생성 후 **육안 검증**(카드/배지/다크 단계톤/반응형 정상). **PASS**.
- 전체 위키 UI 83 passed. 기본 게이트(non-ui) 비회귀.

## 4. 멀티에이전트 워크플로 / 적대 리뷰 반영
- 워크플로(8 에이전트): PM 티켓 → Designer A/B(목업·토큰) → QA A/B(Red 시나리오) → Frontend A/B(구현·코드리뷰) → PM 최종.
- 리뷰 발견·반영: **상태 배지 WCAG 대비**(dot+중립텍스트), **다크 단계 톤 분리**(카드 vs 캔버스), **통합테스트 회귀 3건**(새 탭 구조에 맞춰 검색 탭 전환 선행 + `.wiki-result-card` 갱신), **플래키 제거**(`networkidle`→`domcontentloaded`+명시 대기, 실행 254s→48s).
- visual 축은 서브에이전트 브라우저 실행 불안정성 때문에 **메인루프가 직접 baseline 생성·육안 검증**(에이전트는 spec·목업·Red 테스트·구현 담당).

## 5. 알려진 후속 / nit (보류)
- 검색 탭에서 결과 미선택 시 미리보기 영역이 "아직 위키 페이지가 없습니다" 빈상태(기존 동작) — 결과는 있으나 상세 미선택. 빈상태 카피 일관화는 후속.
- 인용 클릭 핸들러가 3곳(트리·미리보기·현황)에 유사 중복 — `_handleCitationClick` 공용 추출은 후속 DRY(기능 영향 0).
- C4 골든셋(실데이터)으로 다이제스트 누락·랭킹 효과 정량화는 데이터 확보 후.
