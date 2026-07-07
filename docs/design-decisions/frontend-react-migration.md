# 프론트엔드 전환 결정 — React/TypeScript 스트랭글러 경계

**상태**: 준비 단계
**최종 업데이트**: 2026-07-07

---

## 결정 요약

기존 순수 JS SPA는 즉시 제거하지 않는다. React + TypeScript 전환은 뷰 단위
island로 진행하고, 기존 `ui/web` 셸은 전환 기간 동안 라우팅과 legacy view를
계속 담당한다.

현재 허용된 `window.*` 전역 노출은 legacy module factory와 호환 어댑터뿐이다.
이 목록은 `tests/harness/test_frontend_boundaries.py`의
`_ALLOWED_WINDOW_GLOBALS_BY_FILE`에서 고정한다. 신규 프론트엔드 코드는 이 목록을
늘리지 않는다.
`window["Name"] = ...` 형태도 같은 목록으로 집계하며, `Object.assign(window, ...)`,
`Object.defineProperty(window, ...)`, `Object.defineProperties(window, ...)`처럼
allowlist를 우회하는 전역 변경은 허용하지 않는다.

## 신규 코드 규약

- 새 화면 또는 전환 중인 화면은 React + TypeScript 쪽에서 module import/export로
  연결한다. `window.*`를 새 상태 저장소나 service locator로 쓰지 않는다.
- 서버 상태는 React query 계층(TanStack Query 또는 파일럿에서 확정한 동급 계층)에
  모으고, 클라이언트 UI 상태는 React 내장 상태부터 사용한다.
- 기존 `window.Meeting*` factory는 legacy adapter로만 유지한다. React island가
  붙는 시점에는 SPA router가 mount/unmount adapter를 호출하고, island 내부는
  전역 상태를 읽거나 쓰지 않는다.
- `window.ListPanel`과 `window.SPA`는 Playwright/외부 핸들러 호환용 legacy 예외다.
  새 예외가 필요하면 문서와 allowlist 테스트를 함께 갱신해야 한다.

## Build Output Serving

FastAPI는 React/Vite 의존성을 추가하지 않아도 향후 build artifact가 공존할 수
있도록 선택적 정적 경로를 제공한다. `ui/web-dist` 디렉토리가 존재하면 서버가
`/app-assets`에 no-cache 정적 파일로 마운트한다. 디렉토리가 없으면 라우트를 만들지
않으며 기존 `/static` legacy asset과 `/app` SPA catch-all 동작은 그대로 유지한다.

React/Vite scaffold는 `ui/web-src`를 별도 root로 사용하고, dedicated
`ui/web-src/index.html`에서만 `src/main.tsx`를 로드한다. Vite 설정은
`base: "/app-assets/"`, `build.outDir: "../web-dist"`를 사용해 산출물을
`ui/web-dist`에 만든다. `/app-assets`는 `/app/{path}` catch-all 밖에 있으므로 React
island asset 요청이 legacy index.html로 잘못 떨어지지 않는다.

이 scaffold는 아직 asset-only 단계다. legacy `ui/web/index.html`에는 React asset을
주입하지 않고, 기존 `/app` 라우팅과 순수 JS SPA 동작도 바꾸지 않는다. `ui/web-dist`는
generated artifact로 `.gitignore`에 두며, launcher source bundle과 validator exclusion
목록에서도 제외한다.

## 다음 파일럿 기준

첫 React 파일럿은 `search-view`가 대상이다. 이유는 파일 크기가 작고 독립적이며,
검색 API 실패/빈 결과/필터 전달 같은 수용 기준이 명확하기 때문이다.

파일럿이 실제 view 전환을 시작할 때는 다음을 함께 추가한다.

- 컴포넌트 테스트(Vitest 또는 동급)
- 기존 Playwright/UI 회귀 테스트 재사용
- FastAPI 정적 서빙의 legacy `ui/web` + `/app-assets` build output 공존 규칙

## 검증

전역 노출면 변경 여부는 아래 명령으로 확인한다.

```bash
pytest tests/harness/test_frontend_boundaries.py -q
```

서버의 build output 공존 계약은 아래 명령으로 확인한다.

```bash
pytest tests/test_server.py -q
```
