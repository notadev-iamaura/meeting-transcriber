/* =================================================================
 * Recap WikiView boundary
 *
 * 목적: LLM Wiki 화면을 SPA 라우터 본문에서 분리한다.
 * 공개 API: window.MeetingWikiView
 * ================================================================= */
(function () {
    "use strict";

    function create(deps) {
        deps = deps || {};
        var App = deps.App || window.MeetingApp;
        var Router = deps.Router || (window.SPA && window.SPA.Router);

        if (!App || !Router) {
            throw new Error("MeetingWikiView requires App and Router");
        }

        // =================================================================
        // === WikiView (LLM Wiki Phase 2.F) — /app/wiki ===
        // =================================================================
        //
        // 구조:
        //   ┌──────────┬───────────────────────────────────────────┐
        //   │ 위키 트리 │ 헤더 (검색 + Health 배지)                 │
        //   │ ▾ 결정    │ 페이지 미리보기 (마크다운 + 인용 마커)    │
        //   │ ▾ 인물    │                                           │
        //   │ ▾ ...    │                                           │
        //   └──────────┴───────────────────────────────────────────┘
        //
        // API 의존:
        //   GET /api/wiki/pages                — 전체 페이지 목록
        //   GET /api/wiki/health               — 위키 건강 상태
        //   GET /api/wiki/pages/{type}/{slug}  — 페이지 상세
        //   GET /api/wiki/search?q=...         — 검색
        //
        // 핵심 기능:
        //   - 카테고리 5종 (decisions/people/projects/topics/action_items) 펼침/접힘
        //   - 검색 200ms debounce + ESC 초기화
        //   - Health 배지 클릭 → HEALTH.md 모달 (ESC/외부클릭 닫힘)
        //   - 인용 마커 [meeting:meeting_YYYYMMDD_HHMMSS@HH:MM:SS] → /app/viewer/{id}?t=초
        //
        // =================================================================

        /**
         * 카테고리 메타 — 백엔드 page type → 한국어 라벨 매핑.
         * 백엔드는 단수형(decision/person/...)으로 응답하지만 응답 필드 type 은
         * 라우터마다 차이가 있을 수 있으니 단수/복수형 모두 매칭한다.
         */
        var WIKI_CATEGORIES = [
            // (backendTypes 배열) — 백엔드에서 올 수 있는 type 값 후보들
            { id: "decisions",    label: "결정사항",   types: ["decisions", "decision"] },
            { id: "pending",      label: "검토 필요",   types: ["pending"] },
            { id: "action_items", label: "액션아이템", types: ["action_items", "action_item"] },
            { id: "people",       label: "인물",       types: ["people", "person"] },
            { id: "projects",     label: "프로젝트",   types: ["projects", "project"] },
            { id: "topics",       label: "주제",       types: ["topics", "topic"] },
        ];

        // [meeting:{id}@HH:MM:SS] 형태 인용 마커 정규식.
        // 글로벌 플래그는 매번 새로 생성해 lastIndex 누적을 피한다.
        var WIKI_CITATION_PATTERN = /\[meeting:([A-Za-z0-9_]+)@(\d{2}:\d{2}:\d{2})\]/g;

        /**
         * 결정 상태(status) → 한국어 라벨 + 배지 CSS 변형 매핑.
         * 목업 §3 status 매핑 표 기준. 부재 시 배지를 생략한다(open_questions 응답).
         * 텍스트는 항상 --text-primary 계열, dot 만 상태색 → WCAG AA 보장
         * (디자인 리뷰 blocker 2 해소).
         */
        var WIKI_STATUS_META = {
            decided:    { label: "확정",      variant: "decided" },
            pending:    { label: "검토 필요", variant: "pending" },
            rejected:   { label: "거부",      variant: "rejected" },
            superseded: { label: "대체됨",    variant: "superseded" },
        };

        /**
         * 결정 상태 배지 HTML 을 만든다. status 가 알려진 값이 아니면 빈 문자열
         * (배지 생략) 을 반환한다.
         * @param {string} status - metadata.status (decided/pending/rejected/superseded)
         * @returns {string} 배지 HTML 또는 ""
         */
        function _wikiStatusBadgeHtml(status) {
            if (!status) return "";
            var meta = WIKI_STATUS_META[status];
            if (!meta) return "";
            return '<span class="wiki-status-badge wiki-status-badge--' + meta.variant +
                '"><span class="wiki-status-badge__dot" aria-hidden="true"></span>' +
                App.escapeHtml(meta.label) + "</span>";
        }

        /**
         * 인용 마커 배열을 클릭 가능한 anchor HTML 로 변환한다.
         * 현황 탭의 citations:[ "[meeting:id@HH:MM:SS]" ] 배열을 공백 join 후
         * renderMarkdownWithCitations 와 동일 패턴으로 처리한다 (목업 §5).
         * @param {Array<string>} citations - 인용 마커 문자열 배열
         * @returns {string} anchor 들을 감싼 HTML (없으면 "")
         */
        function _wikiCitationsHtml(citations) {
            if (!citations || !citations.length) return "";
            var anchors = citations
                .map(function (c) { return _wikiCitationMarkerToAnchor(c); })
                .filter(Boolean)
                .join(" ");
            if (!anchors) return "";
            return '<div class="wiki-ov-citations">' + anchors + "</div>";
        }

        /**
         * 단일 인용 마커 문자열을 anchor HTML 로 변환한다.
         * @param {string} marker - "[meeting:{id}@HH:MM:SS]"
         * @returns {string} anchor HTML 또는 "" (형식 불일치 시)
         */
        function _wikiCitationMarkerToAnchor(marker) {
            if (!marker) return "";
            var pattern = new RegExp(WIKI_CITATION_PATTERN.source);
            var m = pattern.exec(marker);
            if (!m) return "";
            var mid = m[1];
            var ts = m[2];
            var seconds = _wikiTimestampToSeconds(ts);
            var midSafe = App.escapeHtml(mid);
            var tsSafe = App.escapeHtml(ts);
            return '<a class="wiki-citation" href="/app/viewer/' + midSafe +
                "?t=" + seconds + '" data-mid="' + midSafe +
                '" data-ts="' + tsSafe + '" data-seconds="' + seconds +
                '" title="' + tsSafe + ' 위치 음성 재생">' + tsSafe + "</a>";
        }

        function _wikiEscapeCssIdent(value) {
            if (window.CSS && typeof window.CSS.escape === "function") {
                return window.CSS.escape(value);
            }
            return String(value).replace(/["\\]/g, "\\$&");
        }

        /**
         * "HH:MM:SS" 형식을 초 단위 정수로 변환한다.
         * @param {string} ts - "HH:MM:SS"
         * @returns {number} 초 단위 (잘못된 입력이면 0)
         */
        function _wikiTimestampToSeconds(ts) {
            if (!ts || typeof ts !== "string") return 0;
            var parts = ts.split(":");
            if (parts.length !== 3) return 0;
            var h = parseInt(parts[0], 10) || 0;
            var m = parseInt(parts[1], 10) || 0;
            var s = parseInt(parts[2], 10) || 0;
            return h * 3600 + m * 60 + s;
        }

        /**
         * 백엔드 type 값을 카테고리 id 로 매핑한다.
         * @param {string} type - 백엔드 응답의 page type
         * @returns {string|null} WIKI_CATEGORIES 의 id (decisions 등) 또는 null
         */
        function _wikiCategoryIdForType(type) {
            if (!type) return null;
            for (var i = 0; i < WIKI_CATEGORIES.length; i++) {
                if (WIKI_CATEGORIES[i].types.indexOf(type) !== -1) {
                    return WIKI_CATEGORIES[i].id;
                }
            }
            return null;
        }

        /**
         * 페이지 path 에서 slug 부분을 추출한다.
         * 예) "decisions/2026-04-15-launch-date.md" → "2026-04-15-launch-date"
         * 백엔드 GET /api/wiki/pages/{type}/{slug:path} 호출 시 slug 로 사용.
         * @param {string} path - 위키 루트 기준 상대 경로
         * @returns {string} slug (확장자 제거됨)
         */
        function _wikiPagePathToSlug(path) {
            if (!path) return "";
            // 첫 / 이후의 부분을 사용 (decisions/foo.md → foo.md)
            var idx = path.indexOf("/");
            var rest = idx >= 0 ? path.substring(idx + 1) : path;
            // 확장자 .md 제거 (있으면)
            return rest.replace(/\.md$/i, "");
        }

        /**
         * 페이지 path 에서 페이지 표시용 fallback title 추출.
         * frontmatter.title 도 첫 H1 도 없을 때 사용.
         * @param {string} path - 상대 경로
         * @returns {string}
         */
        function _wikiPathToFallbackTitle(path) {
            var slug = _wikiPagePathToSlug(path);
            // 폴더 prefix 제거
            var lastSlash = slug.lastIndexOf("/");
            return lastSlash >= 0 ? slug.substring(lastSlash + 1) : slug;
        }

        /**
         * 마크다운 텍스트에서 인용 마커를 제외한 본문을 안전하게 HTML 로 렌더한다.
         * 인용 마커는 placeholder 토큰으로 치환했다가 마지막에 안전한 anchor 로 복원.
         * @param {string} md - raw 마크다운
         * @returns {string} 인용 마커가 클릭 가능한 anchor 로 변환된 HTML
         */
        function renderMarkdownWithCitations(md) {
            if (!md) return "";

            // 1) 인용 마커를 placeholder 토큰으로 치환 (escapeHtml 가 건드리지 않도록)
            //    토큰: \u0000WIKICITE\u0000{idx}\u0000 — null 바이트로 충돌 회피.
            var citations = [];
            var pattern = new RegExp(WIKI_CITATION_PATTERN.source, "g");
            var withPlaceholders = md.replace(pattern, function (full, mid, ts) {
                var idx = citations.length;
                citations.push({ mid: mid, ts: ts });
                return "\u0000WIKICITE\u0000" + idx + "\u0000";
            });

            // 2) 기존 마크다운 렌더러 (escapeHtml 내장) 사용
            var html = App.renderMarkdown(withPlaceholders);

            // 3) placeholder 를 클릭 가능한 anchor 로 복원
            html = html.replace(/\u0000WIKICITE\u0000(\d+)\u0000/g, function (_, idxStr) {
                var idx = parseInt(idxStr, 10);
                var c = citations[idx];
                if (!c) return "";
                var seconds = _wikiTimestampToSeconds(c.ts);
                // mid 와 ts 는 정규식으로 검증된 안전한 값이지만 escape 한번 더.
                var midSafe = App.escapeHtml(c.mid);
                var tsSafe = App.escapeHtml(c.ts);
                return '<a class="wiki-citation" href="/app/viewer/' + midSafe +
                    "?t=" + seconds + '" data-mid="' + midSafe +
                    '" data-ts="' + tsSafe + '" data-seconds="' + seconds +
                    '" title="' + tsSafe + ' 위치 음성 재생">' +
                    tsSafe + "</a>";
            });

            return html;
        }

        /**
         * 위키 뷰 — /app/wiki 진입 시 생성되는 메인 컨트롤러.
         * @constructor
         */
        function WikiView() {
            var self = this;
            self._listeners = [];   // 정리할 이벤트 리스너 [{el, type, fn}]
            self._timers = [];      // 정리할 타이머
            self._abortControllers = []; // 진행 중 fetch 취소용
            self._lifecycleAbortControllers = []; // 목록/헬스 fetch 취소용
            self._els = {};         // 자주 쓰는 DOM 참조
            self._pages = [];       // 전체 페이지 목록 (백엔드 응답)
            self._collapsedCategories = {}; // 카테고리별 접힘 상태
            self._activePagePath = null;    // 현재 선택된 페이지의 path
            self._searchTimer = null;        // 검색 debounce
            self._searchMode = false;        // 검색 결과 표시 중인지
            self._healthData = null;         // 헬스 응답 캐시
            self._destroyed = false;          // destroy 이후 async DOM write 방지
            self._activeTab = "overview";    // 현재 활성 탭 (overview | search)
            self._digestLoaded = false;       // digest fetch 중복 방지

            self._render();
            self._bind();
            self._initTabFromUrl();          // ?tab 쿼리로 초기 탭 복원 (라우터 미변경)
            self._loadPages();
            self._loadHealth();
            self._loadDigest();              // 현황 탭 — digest 1회 호출
        }

        /**
         * 위키 뷰 DOM 을 렌더링한다.
         */
        WikiView.prototype._render = function () {
            var contentEl = Router.getContentEl();
            contentEl.innerHTML = "";

            var html = [
                '<div class="wiki-view" data-component="wiki-overview-search">',
                // 공통 상단 헤더 (검색 input + Health 배지) — 두 탭에서 항상 표시.
                // 현황 탭일 때 검색 input(search-wrapper)만 숨기고 Health 배지는 유지.
                '  <div class="wiki-header">',
                '    <div class="wiki-search-wrapper">',
                '      <span class="wiki-search-icon" aria-hidden="true">',
                '        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">',
                '          <circle cx="6.5" cy="6.5" r="4.5"/>',
                '          <path d="M10 10l4.5 4.5"/>',
                '        </svg>',
                '      </span>',
                '      <input type="search" id="wikiSearchInput" class="wiki-search-input"',
                '             placeholder="위키 검색 (제목/본문)" aria-label="위키 검색"',
                '             autocomplete="off">',
                '    </div>',
                '    <div class="wiki-header-spacer"></div>',
                '    <button type="button" id="wikiHealthBadge" class="wiki-health-badge"',
                '            aria-label="위키 건강 상태 보기">',
                '      <span class="wiki-health-dot wiki-health-dot--no-lint" id="wikiHealthDot"></span>',
                '      <span id="wikiHealthLabel">상태 확인 중…</span>',
                '    </button>',
                '  </div>',
                // ARIA 탭 바 (현황/검색) — 뷰 내부 상태로 두 패널 전환 (목업 §1)
                '  <div class="wiki-tabs" role="tablist" aria-label="위키 보기">',
                '    <button type="button" class="wiki-tab" id="wikiTabOverview" role="tab"',
                '            aria-selected="true" aria-controls="wikiOverviewPanel" tabindex="0">현황</button>',
                '    <button type="button" class="wiki-tab" id="wikiTabSearch" role="tab"',
                '            aria-selected="false" aria-controls="wikiSearchPanel" tabindex="-1">검색</button>',
                '  </div>',
                // 현황(Overview) 패널 — digest 4섹션 (기본 활성)
                '  <section class="wiki-overview" id="wikiOverviewPanel" role="tabpanel"',
                '           aria-labelledby="wikiTabOverview" tabindex="0"></section>',
                // 검색 패널 — 필터/트리/미리보기 (회귀 0, 기본 hidden)
                '  <section class="wiki-search-panel" id="wikiSearchPanel" role="tabpanel"',
                '           aria-labelledby="wikiTabSearch" hidden>',
                '    <div class="wiki-decision-filters" aria-label="결정사항 필터">',
                '      <select id="wikiStatusFilter" class="wiki-filter-control" aria-label="상태">',
                '        <option value="">상태 전체</option>',
                '        <option value="decided">확정</option>',
                '        <option value="pending">검토 필요</option>',
                '        <option value="rejected">거부</option>',
                '        <option value="superseded">대체됨</option>',
                '      </select>',
                '      <input type="search" id="wikiProjectFilter" class="wiki-filter-control" placeholder="프로젝트" aria-label="프로젝트">',
                '      <input type="search" id="wikiPersonFilter" class="wiki-filter-control" placeholder="관련자/담당자" aria-label="관련자 또는 담당자">',
                '      <input type="date" id="wikiDateFromFilter" class="wiki-filter-control" aria-label="시작일">',
                '      <input type="date" id="wikiDateToFilter" class="wiki-filter-control" aria-label="종료일">',
                '      <input type="number" id="wikiConfidenceFilter" class="wiki-filter-control wiki-filter-control--small" min="0" max="10" step="1" placeholder="신뢰도" aria-label="최소 신뢰도">',
                '    </div>',
                // 본문 (트리 + 미리보기)
                '    <div class="wiki-body">',
                '      <nav class="wiki-tree" id="wikiTree" aria-label="위키 페이지 트리"></nav>',
                '      <section class="wiki-preview" id="wikiPreview" aria-live="polite"></section>',
                '    </div>',
                '  </section>',
                '</div>',
            ].join("\n");

            contentEl.innerHTML = html;

            this._els = {
                tabs: document.getElementById("wikiTabOverview") &&
                    document.getElementById("wikiTabOverview").parentNode,
                tabOverview: document.getElementById("wikiTabOverview"),
                tabSearch: document.getElementById("wikiTabSearch"),
                overviewPanel: document.getElementById("wikiOverviewPanel"),
                searchPanel: document.getElementById("wikiSearchPanel"),
                searchInput: document.getElementById("wikiSearchInput"),
                healthBadge: document.getElementById("wikiHealthBadge"),
                healthDot: document.getElementById("wikiHealthDot"),
                healthLabel: document.getElementById("wikiHealthLabel"),
                statusFilter: document.getElementById("wikiStatusFilter"),
                projectFilter: document.getElementById("wikiProjectFilter"),
                personFilter: document.getElementById("wikiPersonFilter"),
                dateFromFilter: document.getElementById("wikiDateFromFilter"),
                dateToFilter: document.getElementById("wikiDateToFilter"),
                confidenceFilter: document.getElementById("wikiConfidenceFilter"),
                tree: document.getElementById("wikiTree"),
                preview: document.getElementById("wikiPreview"),
            };

            // 미리보기 초기 빈 상태
            this._renderPreviewEmpty();

            document.title = "위키 · Recap";
        };

        /**
         * 이벤트 리스너를 바인딩한다.
         */
        WikiView.prototype._bind = function () {
            var self = this;
            var els = self._els;

            // 검색 input — 200ms debounce
            var onSearchInput = function () {
                if (self._searchTimer) {
                    clearTimeout(self._searchTimer);
                    self._searchTimer = null;
                }
                var q = els.searchInput.value.trim();
                self._searchTimer = setTimeout(function () {
                    if (!q) {
                        self._exitSearchMode();
                    } else {
                        self._performSearch(q);
                    }
                }, 200);
            };
            els.searchInput.addEventListener("input", onSearchInput);
            self._listeners.push({ el: els.searchInput, type: "input", fn: onSearchInput });

            var onFilterChange = function () {
                var q = els.searchInput.value.trim();
                if (q || self._hasDecisionFilters()) {
                    self._performSearch(q || "decision");
                } else {
                    self._exitSearchMode();
                }
            };
            [
                els.statusFilter,
                els.projectFilter,
                els.personFilter,
                els.dateFromFilter,
                els.dateToFilter,
                els.confidenceFilter,
            ].forEach(function (el) {
                el.addEventListener("change", onFilterChange);
                el.addEventListener("input", onFilterChange);
                self._listeners.push({ el: el, type: "change", fn: onFilterChange });
                self._listeners.push({ el: el, type: "input", fn: onFilterChange });
            });

            // ESC — 검색 초기화 또는 모달 닫기
            var onSearchKeydown = function (e) {
                if (e.key === "Escape" && els.searchInput.value) {
                    els.searchInput.value = "";
                    self._exitSearchMode();
                }
            };
            els.searchInput.addEventListener("keydown", onSearchKeydown);
            self._listeners.push({ el: els.searchInput, type: "keydown", fn: onSearchKeydown });

            // Health 배지 클릭 → 모달
            var onHealthClick = function () {
                self._openHealthModal();
            };
            els.healthBadge.addEventListener("click", onHealthClick);
            self._listeners.push({ el: els.healthBadge, type: "click", fn: onHealthClick });

            // 트리 위임 클릭 — 인용 마커 / 카테고리 헤더 / 페이지 항목 / 검색 카드
            var onTreeClick = function (e) {
                // 인용 마커(검색 카드 내) — deep link 우선 처리
                var citation = e.target.closest(".wiki-citation");
                if (citation) {
                    e.preventDefault();
                    var cmid = citation.getAttribute("data-mid");
                    var cseconds = citation.getAttribute("data-seconds");
                    if (cmid) {
                        var cpath = "/app/viewer/" + encodeURIComponent(cmid) +
                            (cseconds ? "?t=" + encodeURIComponent(cseconds) : "");
                        Router.navigate(cpath);
                    }
                    return;
                }
                var header = e.target.closest(".wiki-tree__category-header");
                if (header && !header.classList.contains("wiki-tree__category-header--disabled")) {
                    self._toggleCategory(header.getAttribute("data-cat"));
                    return;
                }
                var item = e.target.closest(".wiki-tree__item, .wiki-result-card");
                if (item) {
                    var path = item.getAttribute("data-path");
                    var type = item.getAttribute("data-type");
                    if (path && type) {
                        self._loadPage(path, type);
                    }
                }
            };
            els.tree.addEventListener("click", onTreeClick);
            self._listeners.push({ el: els.tree, type: "click", fn: onTreeClick });

            // 트리 키보드 — 페이지 항목에 Enter/Space 로 선택.
            // 검색 결과 카드 제목/인용은 네이티브 button/anchor 라 기본 동작에 위임.
            var onTreeKeydown = function (e) {
                if (e.key !== "Enter" && e.key !== " ") return;
                // 인용 anchor·검색 카드 제목 버튼은 네이티브 클릭에 위임 — 가로채지 않음.
                if (e.target.closest(".wiki-citation") ||
                    e.target.closest(".wiki-result-card__title")) return;
                var item = e.target.closest(".wiki-tree__item");
                var header = e.target.closest(".wiki-tree__category-header");
                if (item) {
                    e.preventDefault();
                    var path = item.getAttribute("data-path");
                    var type = item.getAttribute("data-type");
                    if (path && type) self._loadPage(path, type);
                } else if (header && !header.classList.contains("wiki-tree__category-header--disabled")) {
                    e.preventDefault();
                    self._toggleCategory(header.getAttribute("data-cat"));
                }
            };
            els.tree.addEventListener("keydown", onTreeKeydown);
            self._listeners.push({ el: els.tree, type: "keydown", fn: onTreeKeydown });

            // 미리보기 위임 클릭 — 인용 마커
            var onPreviewClick = function (e) {
                var citation = e.target.closest(".wiki-citation");
                if (!citation) return;
                e.preventDefault();
                var mid = citation.getAttribute("data-mid");
                var seconds = citation.getAttribute("data-seconds");
                if (mid) {
                    var path = "/app/viewer/" + encodeURIComponent(mid) +
                        (seconds ? "?t=" + encodeURIComponent(seconds) : "");
                    Router.navigate(path);
                }
            };
            els.preview.addEventListener("click", onPreviewClick);
            self._listeners.push({ el: els.preview, type: "click", fn: onPreviewClick });

            // 탭 클릭 — 현황/검색 전환
            var onTabClick = function (e) {
                var tab = e.target.closest(".wiki-tab[role='tab']");
                if (!tab) return;
                var id = tab.id;
                self._activateTab(id === "wikiTabSearch" ? "search" : "overview", true);
            };
            // 탭 키보드 — ← → 이동(roving), Home/End, Enter/Space 활성화
            var onTabKeydown = function (e) {
                var key = e.key;
                var isMove = (key === "ArrowRight" || key === "ArrowLeft" ||
                    key === "Home" || key === "End");
                if (!isMove && key !== "Enter" && key !== " ") return;
                e.preventDefault();
                if (key === "Enter" || key === " ") {
                    // 현재 포커스된 탭 활성화
                    var focused = e.target.closest(".wiki-tab[role='tab']");
                    if (focused) {
                        self._activateTab(
                            focused.id === "wikiTabSearch" ? "search" : "overview", true);
                    }
                    return;
                }
                // 이동 — 두 탭만 있으므로 ← 또는 Home → overview, → 또는 End → search
                var next;
                if (key === "ArrowRight") {
                    next = (self._activeTab === "overview") ? "search" : "overview";
                } else if (key === "ArrowLeft") {
                    next = (self._activeTab === "search") ? "overview" : "search";
                } else if (key === "Home") {
                    next = "overview";
                } else { // End
                    next = "search";
                }
                self._activateTab(next, true);
                // 새 활성 탭으로 포커스 이동 (roving tabindex)
                var target = (next === "search") ? els.tabSearch : els.tabOverview;
                if (target) target.focus();
            };
            els.tabs.addEventListener("click", onTabClick);
            els.tabs.addEventListener("keydown", onTabKeydown);
            self._listeners.push({ el: els.tabs, type: "click", fn: onTabClick });
            self._listeners.push({ el: els.tabs, type: "keydown", fn: onTabKeydown });

            // 현황 패널 위임 클릭 — 인용 마커(deep link) + 카드(페이지 미리보기)
            var onOverviewClick = function (e) {
                var citation = e.target.closest(".wiki-citation");
                if (citation) {
                    e.preventDefault();
                    var mid = citation.getAttribute("data-mid");
                    var seconds = citation.getAttribute("data-seconds");
                    if (mid) {
                        var path = "/app/viewer/" + encodeURIComponent(mid) +
                            (seconds ? "?t=" + encodeURIComponent(seconds) : "");
                        Router.navigate(path);
                    }
                    return;
                }
                // 결정/프로젝트 카드 클릭 → 검색 탭으로 전환 후 해당 페이지 상세 표시
                var card = e.target.closest(".wiki-ov-card[data-path]");
                if (card) {
                    var cpath = card.getAttribute("data-path");
                    var ctype = card.getAttribute("data-type");
                    if (cpath && ctype) {
                        self._activateTab("search", true);
                        self._loadPage(cpath, ctype);
                    }
                }
            };
            els.overviewPanel.addEventListener("click", onOverviewClick);
            self._listeners.push({ el: els.overviewPanel, type: "click", fn: onOverviewClick });
        };

        /**
         * URL ?tab 쿼리로 초기 활성 탭을 복원한다 (라우터 미변경 — design.md §5.2).
         * 잘못된 값/부재 시 기본 'overview'.
         */
        WikiView.prototype._initTabFromUrl = function () {
            var params;
            try {
                params = new URLSearchParams(window.location.search);
            } catch (_) {
                params = null;
            }
            var tab = params ? params.get("tab") : null;
            // 초기 가시성을 명시적으로 적용 (search-wrapper hidden 등).
            // ?tab=search 면 검색 탭, 그 외(기본 포함)는 현황 탭. URL 갱신 없음.
            this._activateTab(tab === "search" ? "search" : "overview", false);
        };

        /**
         * 탭을 활성화하고 패널 가시성·ARIA·roving tabindex 를 갱신한다.
         * @param {string} tab - "overview" | "search"
         * @param {boolean} updateUrl - history.replaceState 로 ?tab 기록 여부
         */
        WikiView.prototype._activateTab = function (tab, updateUrl) {
            if (tab !== "overview" && tab !== "search") return;
            this._activeTab = tab;
            var els = this._els;
            var isOverview = (tab === "overview");

            // 탭 버튼 — aria-selected + roving tabindex
            if (els.tabOverview) {
                els.tabOverview.setAttribute("aria-selected", isOverview ? "true" : "false");
                els.tabOverview.setAttribute("tabindex", isOverview ? "0" : "-1");
            }
            if (els.tabSearch) {
                els.tabSearch.setAttribute("aria-selected", isOverview ? "false" : "true");
                els.tabSearch.setAttribute("tabindex", isOverview ? "-1" : "0");
            }
            // 패널 가시성 — hidden 속성 토글
            if (els.overviewPanel) {
                if (isOverview) els.overviewPanel.removeAttribute("hidden");
                else els.overviewPanel.setAttribute("hidden", "");
            }
            if (els.searchPanel) {
                if (isOverview) els.searchPanel.setAttribute("hidden", "");
                else els.searchPanel.removeAttribute("hidden");
            }
            // 검색 input(search-wrapper)은 검색 탭에서만 보인다 (Health 배지는 항상 표시).
            var searchWrapper = els.searchInput &&
                els.searchInput.closest(".wiki-search-wrapper");
            if (searchWrapper) {
                if (isOverview) searchWrapper.setAttribute("hidden", "");
                else searchWrapper.removeAttribute("hidden");
            }

            if (updateUrl) {
                try {
                    var params = new URLSearchParams(window.location.search);
                    if (isOverview) {
                        params.delete("tab");
                    } else {
                        params.set("tab", "search");
                    }
                    var qs = params.toString();
                    var newUrl = window.location.pathname + (qs ? "?" + qs : "") +
                        window.location.hash;
                    window.history.replaceState(window.history.state, "", newUrl);
                } catch (_) { /* history 미지원 환경 — 무시 */ }
            }
        };

        /**
         * 현황 다이제스트를 백엔드에서 1회 가져와 렌더한다.
         * GET /api/wiki/digest → total_open_actions / open_actions /
         * recent_decisions / project_status 4섹션.
         */
        WikiView.prototype._loadDigest = function () {
            var self = this;
            if (self._digestLoaded) return;
            self._digestLoaded = true;

            self._renderDigestLoading();

            var ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
            if (ctrl) self._lifecycleAbortControllers.push(ctrl);

            App.apiRequest("/wiki/digest", ctrl ? { signal: ctrl.signal } : {})
                .then(function (data) {
                    if (self._destroyed) return;
                    self._renderDigest(data || {});
                })
                .catch(function (err) {
                    if (self._destroyed || (err && err.name === "AbortError")) return;
                    self._renderDigestError(err && err.message);
                });
        };

        /**
         * 현황 패널 로딩 스켈레톤을 렌더한다.
         */
        WikiView.prototype._renderDigestLoading = function () {
            if (!this._els.overviewPanel) return;
            this._els.overviewPanel.innerHTML = [
                '<div class="wiki-overview-inner">',
                '  <div class="wiki-ov-skeleton"></div>',
                '  <div class="wiki-ov-skeleton"></div>',
                '  <div class="wiki-ov-skeleton"></div>',
                "</div>",
            ].join("");
        };

        /**
         * 현황 다이제스트 4섹션을 렌더한다. 전체 빈이면 빈 상태 카드.
         * @param {Object} digest - GET /api/wiki/digest 응답
         */
        WikiView.prototype._renderDigest = function (digest) {
            var panel = this._els.overviewPanel;
            if (!panel) return;

            var openActions = digest.open_actions || [];
            var decisions = digest.recent_decisions || [];
            var projects = digest.project_status || [];
            var totalOpen = (typeof digest.total_open_actions === "number")
                ? digest.total_open_actions : 0;

            // 전체 빈 — 기존 .wiki-empty-state 재사용(검색 빈상태와 카피 일관, 목업 §2)
            if (totalOpen === 0 && openActions.length === 0 &&
                decisions.length === 0 && projects.length === 0) {
                panel.innerHTML = [
                    '<div class="wiki-overview-inner">',
                    '  <div class="wiki-empty-state">',
                    '    <svg class="wiki-empty-state-icon" viewBox="0 0 56 56" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">',
                    '      <path d="M12 50A6 6 0 0 1 18 44H46"/>',
                    '      <path d="M18 6H46v44H18a6 6 0 0 1-6-6V12A6 6 0 0 1 18 6z"/>',
                    '      <path d="M22 18H40M22 26H38M22 34H34"/>',
                    '    </svg>',
                    '    <h2 class="wiki-empty-state-title">아직 정리된 현황이 없습니다</h2>',
                    '    <p class="wiki-empty-state-desc">회의를 처리하면 미해결 액션·결정·프로젝트 현황이 자동으로 모입니다.</p>',
                    "  </div>",
                    "</div>",
                ].join("");
                return;
            }

            var generatedFor = digest.generated_for
                ? App.escapeHtml(digest.generated_for) + " 기준" : "";

            panel.innerHTML = [
                '<div class="wiki-overview-inner">',
                '  <header class="wiki-overview__header">',
                '    <h1 class="wiki-overview__title">현황</h1>',
                generatedFor
                    ? '    <span class="wiki-overview__date">' + generatedFor + "</span>"
                    : "",
                "  </header>",
                '  <p class="wiki-overview__lede">미해결 액션 <strong>' + totalOpen +
                    "</strong>건</p>",
                this._renderOpenActionsSection(openActions),
                this._renderDecisionsSection(decisions),
                this._renderProjectsSection(projects),
                "</div>",
            ].filter(Boolean).join("\n");
        };

        /**
         * ① 미해결 액션 (owner별) 섹션 HTML.
         * @param {Array<Object>} openActions - [{owner, items:[{description,citations,due_date}]}]
         * @returns {string}
         */
        WikiView.prototype._renderOpenActionsSection = function (openActions) {
            var body;
            if (!openActions.length) {
                body = '<p class="wiki-ov-empty">없음</p>';
            } else {
                body = openActions.map(function (group) {
                    var owner = App.escapeHtml(group.owner || "미지정");
                    var items = group.items || [];
                    var itemsHtml = items.map(function (item) {
                        var desc = App.escapeHtml(item.description || "");
                        var due = item.due_date
                            ? '<span class="wiki-ov-item__due">마감 ' +
                                App.escapeHtml(item.due_date) + "</span>"
                            : '<span class="wiki-ov-item__due wiki-ov-item__due--none">마감 없음</span>';
                        var cites = _wikiCitationsHtml(item.citations);
                        return [
                            '<li class="wiki-ov-item">',
                            '  <span class="wiki-ov-item__desc">' + desc + "</span>",
                            due,
                            cites,
                            "</li>",
                        ].join("");
                    }).join("");
                    return [
                        '<div class="wiki-ov-card">',
                        '  <div class="wiki-ov-card__head">',
                        '    <span class="wiki-ov-card__owner">' + owner + "</span>",
                        '    <span class="wiki-ov-count">' + items.length + "건</span>",
                        "  </div>",
                        '  <ul class="wiki-ov-item-list">' + itemsHtml + "</ul>",
                        "</div>",
                    ].join("");
                }).join("");
            }
            return [
                '<section class="wiki-ov-section">',
                '  <h2 class="wiki-ov-section__title">미해결 액션</h2>',
                body,
                "</section>",
            ].join("\n");
        };

        /**
         * ② 최근 결정 섹션 HTML. 카드 클릭 시 해당 페이지 미리보기로 이동.
         * @param {Array<Object>} decisions - recent_decisions
         * @returns {string}
         */
        WikiView.prototype._renderDecisionsSection = function (decisions) {
            var body;
            if (!decisions.length) {
                body = '<p class="wiki-ov-empty">없음</p>';
            } else {
                body = decisions.map(function (d) {
                    var title = App.escapeHtml(d.title ||
                        _wikiPathToFallbackTitle(d.page_path || ""));
                    var badge = _wikiStatusBadgeHtml(d.status);
                    var project = d.project
                        ? '<span class="wiki-ov-card__project">' +
                            App.escapeHtml(d.project) + "</span>"
                        : "";
                    var date = d.decision_date
                        ? '<span class="wiki-ov-card__date">' +
                            App.escapeHtml(d.decision_date) + "</span>"
                        : "";
                    var cites = _wikiCitationsHtml(d.citations);
                    var dataAttrs = d.page_path
                        ? ' data-path="' + App.escapeHtml(d.page_path) +
                            '" data-type="decisions"'
                        : "";
                    return [
                        '<div class="wiki-ov-card wiki-ov-card--clickable"' + dataAttrs + ">",
                        '  <div class="wiki-ov-card__head">',
                        '    <span class="wiki-ov-card__title">' + title + "</span>",
                        badge,
                        "  </div>",
                        '  <div class="wiki-ov-card__meta">' + project + date + "</div>",
                        cites,
                        "</div>",
                    ].filter(Boolean).join("");
                }).join("");
            }
            return [
                '<section class="wiki-ov-section">',
                '  <h2 class="wiki-ov-section__title">최근 결정</h2>',
                body,
                "</section>",
            ].join("\n");
        };

        /**
         * ③ 프로젝트별 현황 섹션 HTML. 카드 클릭 시 page_path 미리보기로 이동.
         * @param {Array<Object>} projects - project_status
         * @returns {string}
         */
        WikiView.prototype._renderProjectsSection = function (projects) {
            var body;
            if (!projects.length) {
                body = '<p class="wiki-ov-empty">없음</p>';
            } else {
                body = projects.map(function (p) {
                    var name = App.escapeHtml(p.project || "미지정");
                    var badge = _wikiStatusBadgeHtml(p.status);
                    var lastTitle = p.last_title
                        ? '<span class="wiki-ov-card__last">' +
                            App.escapeHtml(p.last_title) + "</span>"
                        : "";
                    var date = p.last_date
                        ? '<span class="wiki-ov-card__date">' +
                            App.escapeHtml(p.last_date) + "</span>"
                        : "";
                    var dataAttrs = p.page_path
                        ? ' data-path="' + App.escapeHtml(p.page_path) +
                            '" data-type="decisions"'
                        : "";
                    return [
                        '<div class="wiki-ov-card wiki-ov-card--clickable"' + dataAttrs + ">",
                        '  <div class="wiki-ov-card__head">',
                        '    <span class="wiki-ov-card__project-name">' + name + "</span>",
                        badge,
                        "  </div>",
                        '  <div class="wiki-ov-card__meta">' + lastTitle + date + "</div>",
                        "</div>",
                    ].filter(Boolean).join("");
                }).join("");
            }
            return [
                '<section class="wiki-ov-section">',
                '  <h2 class="wiki-ov-section__title">프로젝트 현황</h2>',
                body,
                "</section>",
            ].join("\n");
        };

        /**
         * 현황 다이제스트 로드 실패 시 에러 카피를 렌더한다.
         * @param {string} message - 에러 메시지
         */
        WikiView.prototype._renderDigestError = function (message) {
            var panel = this._els.overviewPanel;
            if (!panel) return;
            panel.innerHTML = [
                '<div class="wiki-overview-inner">',
                '  <div class="wiki-empty-state">',
                '    <h2 class="wiki-empty-state-title">현황을 불러오지 못했습니다</h2>',
                '    <p class="wiki-empty-state-desc">' +
                    App.escapeHtml(message || "알 수 없는 오류") + "</p>",
                "  </div>",
                "</div>",
            ].join("");
        };

        /**
         * 페이지 목록을 백엔드에서 가져와 트리를 렌더링한다.
         */
        WikiView.prototype._loadPages = function () {
            var self = this;
            // 트리 영역에 로딩 상태 표시
            self._els.tree.innerHTML =
                '<div class="wiki-tree__empty">위키 페이지를 불러오는 중…</div>';

            var ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
            if (ctrl) self._lifecycleAbortControllers.push(ctrl);

            App.apiRequest("/wiki/pages", ctrl ? { signal: ctrl.signal } : {})
                .then(function (data) {
                    if (self._destroyed) return;
                    self._pages = (data && data.pages) || [];
                    if (!self._searchMode) {
                        self._renderTree(self._pages);
                    }
                    // 페이지가 0건이면 미리보기에 빈 상태 표시
                    if (self._pages.length === 0 && !self._activePagePath) {
                        self._renderEmptyWiki();
                    }
                })
                .catch(function (err) {
                    if (self._destroyed || (err && err.name === "AbortError")) return;
                    self._els.tree.innerHTML =
                        '<div class="wiki-tree__empty">' +
                        App.escapeHtml("페이지 목록을 불러오지 못했습니다: " + (err.message || "알 수 없는 오류")) +
                        "</div>";
                });
        };

        /**
         * 페이지 배열을 받아 카테고리별로 그룹핑한 트리를 렌더링한다.
         * @param {Array<Object>} pages - 페이지 목록 (path, type, title, last_updated)
         */
        WikiView.prototype._renderTree = function (pages) {
            var self = this;

            // 카테고리별로 분류
            var grouped = {};
            WIKI_CATEGORIES.forEach(function (cat) { grouped[cat.id] = []; });
            pages.forEach(function (p) {
                var catId = _wikiCategoryIdForType(p.type);
                if (catId && grouped[catId]) {
                    grouped[catId].push(p);
                }
            });

            // 카테고리별 항목 정렬 (last_updated 내림차순, 없으면 path)
            Object.keys(grouped).forEach(function (k) {
                grouped[k].sort(function (a, b) {
                    var au = a.last_updated || "";
                    var bu = b.last_updated || "";
                    if (au !== bu) return au < bu ? 1 : -1;
                    return (a.path || "").localeCompare(b.path || "");
                });
            });

            var html = WIKI_CATEGORIES.map(function (cat) {
                var items = grouped[cat.id] || [];
                var count = items.length;
                var isCollapsed = self._collapsedCategories[cat.id] === true;
                var isEmpty = count === 0;
                var headerCls = "wiki-tree__category-header" +
                    (isEmpty ? " wiki-tree__category-header--disabled" : "");

                var itemsHtml = items.map(function (p) {
                    var rawTitle = p.title || _wikiPathToFallbackTitle(p.path);
                    var isActive = (self._activePagePath === p.path);
                    var meta = p.last_updated ? App.formatDate(p.last_updated) : "";
                    return [
                        '<button type="button" class="wiki-tree__item' +
                            (isActive ? " wiki-tree__item--active" : "") +
                            '" data-path="' + App.escapeHtml(p.path) +
                            '" data-type="' + App.escapeHtml(p.type) +
                            '" tabindex="0">',
                        '  <span class="wiki-tree__item-title">' + App.escapeHtml(rawTitle) + "</span>",
                        meta ? '  <span class="wiki-tree__item-meta">' + App.escapeHtml(meta) + "</span>" : "",
                        "</button>",
                    ].filter(Boolean).join("");
                }).join("");

                return [
                    '<div class="wiki-tree__category' +
                        (isCollapsed ? " wiki-tree__category--collapsed" : "") +
                        '" data-cat="' + App.escapeHtml(cat.id) + '">',
                    '  <button type="button" class="' + headerCls +
                        '" data-cat="' + App.escapeHtml(cat.id) +
                        '" aria-expanded="' + (!isCollapsed) +
                        '"' + (isEmpty ? ' tabindex="-1"' : "") + ">",
                    '    <span class="wiki-tree__caret">\u25BE</span>',
                    '    <span class="wiki-tree__category-name">' + App.escapeHtml(cat.label) + "</span>",
                    '    <span class="wiki-tree__category-count">(' + count + ")</span>",
                    "  </button>",
                    '  <div class="wiki-tree__items">' + itemsHtml + "</div>",
                    "</div>",
                ].join("");
            }).join("");

            self._els.tree.innerHTML = html;
        };

        /**
         * 검색 결과 트리를 렌더링한다 (단일 그룹).
         * @param {Array<Object>} results - 검색 결과
         */
        WikiView.prototype._renderSearchTree = function (results) {
            var self = this;

            if (!results || results.length === 0) {
                self._els.tree.innerHTML =
                    '<div class="wiki-tree__empty">검색 결과가 없습니다.</div>';
                return;
            }

            var itemsHtml = results.map(function (r) {
                var rawTitle = r.title || _wikiPathToFallbackTitle(r.path);
                var isActive = (self._activePagePath === r.path);
                var snippet = r.snippet ? App.escapeHtml(r.snippet) : "";
                // score — 0~1 범위 가정, 소수점 2자리 (tabular-nums)
                var scoreHtml = "";
                if (typeof r.score === "number" && isFinite(r.score)) {
                    scoreHtml = '<span class="wiki-result-card__score" aria-label="관련도 점수">' +
                        r.score.toFixed(2) + "</span>";
                }
                // status 배지 — metadata.status 가 알려진 값일 때만 (부재 시 생략)
                var status = r.metadata && r.metadata.status;
                var badge = _wikiStatusBadgeHtml(status);
                // citations — renderMarkdownWithCitations 동일 패턴 deep link
                var cites = "";
                if (r.citations && r.citations.length) {
                    var anchors = r.citations
                        .map(function (c) { return _wikiCitationMarkerToAnchor(c); })
                        .filter(Boolean)
                        .join(" ");
                    if (anchors) {
                        cites = '<div class="wiki-result-card__citations">' + anchors + "</div>";
                    }
                }
                // 카드 자체는 비상호작용 컨테이너(role 없음) — 인용 anchor 중첩 시
                // nested-interactive 위반 회피. 클릭 타겟은 제목 버튼.
                return [
                    '<div class="wiki-result-card' +
                        (isActive ? " wiki-result-card--active" : "") +
                        '" data-path="' + App.escapeHtml(r.path) +
                        '" data-type="' + App.escapeHtml(r.type) + '" tabindex="-1">',
                    '  <div class="wiki-result-card__head">',
                    '    <button type="button" class="wiki-result-card__title"' +
                        ' data-path="' + App.escapeHtml(r.path) +
                        '" data-type="' + App.escapeHtml(r.type) + '">' +
                        App.escapeHtml(rawTitle) + "</button>",
                    badge,
                    scoreHtml,
                    "  </div>",
                    snippet ? '  <p class="wiki-result-card__snippet">' + snippet + "</p>" : "",
                    cites,
                    "</div>",
                ].filter(Boolean).join("");
            }).join("");

            self._els.tree.innerHTML = [
                '<div class="wiki-search-results" data-cat="search">',
                '  <div class="wiki-search-results__header">',
                '    검색 결과 <span class="wiki-tree__category-count">(' + results.length + ")</span>",
                "  </div>",
                '  <div class="wiki-search-results__list">' + itemsHtml + "</div>",
                "</div>",
            ].join("");
        };

        /**
         * 검색을 수행하고 트리를 검색 결과 모드로 전환한다.
         * @param {string} q - 검색어
         */
        WikiView.prototype._performSearch = function (q) {
            var self = this;
            // 이전 검색 fetch 취소
            self._abortTransientRequests();

            var ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
            if (ctrl) self._abortControllers.push(ctrl);

            self._searchMode = true;

            var params = new URLSearchParams();
            params.set("q", q);
            params.set("limit", "50");
            params.set("page_type", "decision");
            if (self._els.statusFilter.value) params.set("status", self._els.statusFilter.value);
            if (self._els.projectFilter.value.trim()) params.set("project", self._els.projectFilter.value.trim());
            if (self._els.personFilter.value.trim()) {
                params.set("person", self._els.personFilter.value.trim());
            }
            if (self._els.dateFromFilter.value) params.set("date_from", self._els.dateFromFilter.value);
            if (self._els.dateToFilter.value) params.set("date_to", self._els.dateToFilter.value);
            if (self._els.confidenceFilter.value) params.set("min_confidence", self._els.confidenceFilter.value);

            App.apiRequest(
                "/wiki/search?" + params.toString(),
                ctrl ? { signal: ctrl.signal } : {}
            )
                .then(function (data) {
                    if (self._destroyed) return;
                    if (!self._searchMode) return; // 이미 종료된 경우
                    var results = (data && data.results) || [];
                    self._renderSearchTree(results);
                })
                .catch(function (err) {
                    if (self._destroyed || (err && err.name === "AbortError")) return;
                    self._els.tree.innerHTML =
                        '<div class="wiki-tree__empty">' +
                        App.escapeHtml("검색에 실패했습니다: " + (err.message || "알 수 없는 오류")) +
                        "</div>";
                });
        };

        WikiView.prototype._hasDecisionFilters = function () {
            var els = this._els;
            return Boolean(
                els.statusFilter.value ||
                els.projectFilter.value.trim() ||
                els.personFilter.value.trim() ||
                els.dateFromFilter.value ||
                els.dateToFilter.value ||
                els.confidenceFilter.value
            );
        };

        /**
         * 검색 모드를 종료하고 원래 카테고리 트리로 복원한다.
         */
        WikiView.prototype._exitSearchMode = function () {
            this._searchMode = false;
            this._abortTransientRequests();
            this._renderTree(this._pages);
        };

        /**
         * 카테고리의 펼침/접힘 상태를 토글한다.
         * @param {string} catId - 카테고리 id
         */
        WikiView.prototype._toggleCategory = function (catId) {
            if (!catId) return;
            this._collapsedCategories[catId] = !this._collapsedCategories[catId];
            var node = this._els.tree.querySelector(
                '.wiki-tree__category[data-cat="' + _wikiEscapeCssIdent(catId) + '"]'
            );
            if (node) {
                node.classList.toggle(
                    "wiki-tree__category--collapsed",
                    this._collapsedCategories[catId]
                );
                var header = node.querySelector(".wiki-tree__category-header");
                if (header) {
                    header.setAttribute("aria-expanded",
                        !this._collapsedCategories[catId]);
                }
            }
        };

        /**
         * 단일 위키 페이지 상세를 로드해 미리보기에 렌더한다.
         * @param {string} path - 페이지 path (예: "decisions/foo.md")
         * @param {string} type - 백엔드 type (예: "decisions" 또는 "decision")
         */
        WikiView.prototype._loadPage = function (path, type) {
            var self = this;
            if (!path || !type) return;

            self._abortTransientRequests();
            self._activePagePath = path;
            // 트리 active 상태 갱신
            self._updateActiveItem();

            var slug = _wikiPagePathToSlug(path);
            // 백엔드는 page_type 화이트리스트로 단수/복수 모두 허용 — 그대로 전달.
            var endpoint = "/wiki/pages/" + encodeURIComponent(type) +
                "/" + slug.split("/").map(encodeURIComponent).join("/");

            // 로딩 상태
            self._els.preview.innerHTML =
                '<div class="wiki-preview-empty">' +
                '<p class="wiki-preview-empty-desc">불러오는 중…</p></div>';

            var ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
            if (ctrl) self._abortControllers.push(ctrl);

            App.apiRequest(endpoint, ctrl ? { signal: ctrl.signal } : {})
                .then(function (data) {
                    if (self._destroyed) return;
                    self._renderPage(data);
                })
                .catch(function (err) {
                    if (self._destroyed || (err && err.name === "AbortError")) return;
                    self._els.preview.innerHTML =
                        '<div class="wiki-preview-empty">' +
                        '<p class="wiki-preview-empty-title">페이지를 불러오지 못했습니다</p>' +
                        '<p class="wiki-preview-empty-desc">' +
                        App.escapeHtml(err.message || "알 수 없는 오류") +
                        "</p></div>";
                });
        };

        /**
         * 트리에서 active 클래스를 갱신한다.
         */
        WikiView.prototype._updateActiveItem = function () {
            var self = this;
            // 카테고리 트리 항목
            var items = self._els.tree.querySelectorAll(".wiki-tree__item");
            items.forEach(function (item) {
                if (item.getAttribute("data-path") === self._activePagePath) {
                    item.classList.add("wiki-tree__item--active");
                } else {
                    item.classList.remove("wiki-tree__item--active");
                }
            });
            // 검색 결과 카드 (검색 모드)
            var cards = self._els.tree.querySelectorAll(".wiki-result-card");
            cards.forEach(function (card) {
                if (card.getAttribute("data-path") === self._activePagePath) {
                    card.classList.add("wiki-result-card--active");
                } else {
                    card.classList.remove("wiki-result-card--active");
                }
            });
        };

        /**
         * 페이지 상세 응답을 미리보기에 렌더한다.
         * @param {Object} data - WikiPageDetail 응답
         */
        WikiView.prototype._renderPage = function (data) {
            if (!data) {
                this._renderPreviewEmpty();
                return;
            }

            // 제목 결정 — frontmatter.title > 응답 title > 첫 H1 > path slug
            var title = (data.frontmatter && data.frontmatter.title) ||
                data.title ||
                _wikiPathToFallbackTitle(data.path || "");

            // frontmatter 메타 박스 렌더 (title 은 헤더에서 따로 표시하므로 제외)
            var fmRows = [];
            if (data.frontmatter && typeof data.frontmatter === "object") {
                Object.keys(data.frontmatter).forEach(function (k) {
                    if (k === "title") return; // 중복 제거
                    var v = data.frontmatter[k];
                    var vText;
                    if (Array.isArray(v)) {
                        vText = v.join(", ");
                    } else if (v === null || v === undefined) {
                        vText = "";
                    } else if (typeof v === "object") {
                        vText = JSON.stringify(v);
                    } else {
                        vText = String(v);
                    }
                    if (!vText) return;
                    fmRows.push(
                        '<div class="wiki-preview-frontmatter-row">' +
                        '<span class="wiki-preview-frontmatter-key">' +
                        App.escapeHtml(k) + "</span>" +
                        '<span class="wiki-preview-frontmatter-value">' +
                        App.escapeHtml(vText) + "</span></div>"
                    );
                });
            }

            // 본문 마크다운 → HTML (인용 마커 변환 포함)
            var bodyHtml = renderMarkdownWithCitations(data.content || "");

            var html = [
                '<article class="wiki-preview-content">',
                '  <header class="wiki-preview-page-header">',
                '    <h1 class="wiki-preview-page-title">' + App.escapeHtml(title) + "</h1>",
                data.path
                    ? '    <div class="wiki-preview-page-path">' + App.escapeHtml(data.path) + "</div>"
                    : "",
                "  </header>",
                fmRows.length > 0
                    ? '  <div class="wiki-preview-frontmatter">' + fmRows.join("") + "</div>"
                    : "",
                '  <div class="wiki-preview-markdown">' + bodyHtml + "</div>",
                "</article>",
            ].filter(Boolean).join("\n");

            this._els.preview.innerHTML = html;
            this._els.preview.scrollTop = 0;
        };

        /**
         * 미리보기 영역에 "선택해주세요" 빈 상태를 렌더한다.
         */
        WikiView.prototype._renderPreviewEmpty = function () {
            this._els.preview.innerHTML = [
                '<div class="wiki-preview-empty">',
                '  <div class="wiki-preview-empty-icon">\uD83D\uDCD6</div>',
                '  <h2 class="wiki-preview-empty-title">페이지를 선택하세요</h2>',
                '  <p class="wiki-preview-empty-desc">왼쪽 트리에서 위키 페이지를 클릭하면 본문이 표시됩니다.</p>',
                "</div>",
            ].join("\n");
        };

        /**
         * 위키 페이지가 0건일 때 미리보기 영역 빈 상태.
         */
        WikiView.prototype._renderEmptyWiki = function () {
            this._els.preview.innerHTML = [
                '<div class="wiki-empty-state">',
                '  <svg class="wiki-empty-state-icon" viewBox="0 0 56 56" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">',
                '    <path d="M12 50A6 6 0 0 1 18 44H46"/>',
                '    <path d="M18 6H46v44H18a6 6 0 0 1-6-6V12A6 6 0 0 1 18 6z"/>',
                '    <path d="M22 18H40M22 26H38M22 34H34"/>',
                '  </svg>',
                '  <h2 class="wiki-empty-state-title">아직 위키 페이지가 없습니다</h2>',
                '  <p class="wiki-empty-state-desc">회의를 처리하면 결정사항·인물·프로젝트·주제 페이지가 자동으로 만들어집니다.</p>',
                "</div>",
            ].join("\n");
        };

        /**
         * Health 데이터를 백엔드에서 가져와 배지를 갱신한다.
         */
        WikiView.prototype._loadHealth = function () {
            var self = this;
            var ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
            if (ctrl) self._lifecycleAbortControllers.push(ctrl);

            App.apiRequest("/wiki/health", ctrl ? { signal: ctrl.signal } : {})
                .then(function (data) {
                    if (self._destroyed) return;
                    self._healthData = data || { status: "no_lint_yet" };
                    self._renderHealthBadge();
                })
                .catch(function (err) {
                    if (self._destroyed || (err && err.name === "AbortError")) return;
                    self._healthData = { status: "error", raw_markdown: null };
                    self._renderHealthBadge();
                });
        };

        /**
         * 헬스 데이터를 헤더 배지에 반영한다.
         */
        WikiView.prototype._renderHealthBadge = function () {
            var els = this._els;
            var status = (this._healthData && this._healthData.status) || "no_lint_yet";
            var dotClass = "wiki-health-dot";
            var label;
            if (status === "ok") {
                dotClass += " wiki-health-dot--ok";
                label = "위키 정상";
            } else if (status === "warnings") {
                dotClass += " wiki-health-dot--warning";
                label = "경고 있음";
            } else if (status === "error") {
                dotClass += " wiki-health-dot--warning";
                label = "건강 조회 실패";
            } else {
                // no_lint_yet 등
                dotClass += " wiki-health-dot--no-lint";
                label = "아직 점검 안 함";
            }
            els.healthDot.className = dotClass;
            els.healthLabel.textContent = label;
        };

        /**
         * Health 모달을 연다. HEALTH.md 의 raw_markdown 을 안전 렌더.
         */
        WikiView.prototype._openHealthModal = function () {
            var self = this;

            // 기존 모달 있으면 닫기 (재진입 안전)
            self._closeHealthModal();

            var raw = self._healthData && self._healthData.raw_markdown;
            var lastLint = self._healthData && self._healthData.last_lint_at;
            var status = (self._healthData && self._healthData.status) || "no_lint_yet";

            var bodyHtml = raw
                ? '<div class="wiki-preview-markdown">' + App.renderMarkdown(raw) + "</div>"
                : '<div class="wiki-health-modal-empty">' +
                    (status === "no_lint_yet"
                        ? "아직 위키 자동 점검(lint)이 실행되지 않았습니다."
                        : "건강 보고서를 불러오지 못했습니다.") +
                    "</div>";

            var statusLine = lastLint
                ? '<p style="margin:0 0 12px;font-size:12px;color:var(--text-muted);">' +
                    "최근 점검: " + App.escapeHtml(lastLint) + "</p>"
                : "";

            var modal = document.createElement("div");
            modal.className = "wiki-health-modal";
            modal.setAttribute("role", "dialog");
            modal.setAttribute("aria-modal", "true");
            modal.setAttribute("aria-labelledby", "wikiHealthModalTitle");
            modal.innerHTML = [
                '<div class="wiki-health-modal-content">',
                '  <header class="wiki-health-modal-header">',
                '    <h3 class="wiki-health-modal-title" id="wikiHealthModalTitle">위키 건강 보고서</h3>',
                '    <button type="button" class="wiki-health-modal-close" aria-label="닫기">\u2715</button>',
                "  </header>",
                '  <div class="wiki-health-modal-body">',
                statusLine,
                bodyHtml,
                "  </div>",
                "</div>",
            ].join("");

            document.body.appendChild(modal);
            self._healthModal = modal;

            // 닫기 핸들러
            var onModalClick = function (e) {
                if (e.target === modal) {
                    self._closeHealthModal();
                }
            };
            var onCloseBtn = function () { self._closeHealthModal(); };
            var onModalKeydown = function (e) {
                if (e.key === "Escape") {
                    self._closeHealthModal();
                }
            };

            modal.addEventListener("click", onModalClick);
            var closeBtn = modal.querySelector(".wiki-health-modal-close");
            if (closeBtn) closeBtn.addEventListener("click", onCloseBtn);
            document.addEventListener("keydown", onModalKeydown);

            // 정리용 핸들러 저장
            self._healthModalCleanup = function () {
                modal.removeEventListener("click", onModalClick);
                if (closeBtn) closeBtn.removeEventListener("click", onCloseBtn);
                document.removeEventListener("keydown", onModalKeydown);
            };

            // 첫 focusable 로 이동
            if (closeBtn) closeBtn.focus();
        };

        /**
         * Health 모달을 닫는다.
         */
        WikiView.prototype._closeHealthModal = function () {
            if (this._healthModalCleanup) {
                this._healthModalCleanup();
                this._healthModalCleanup = null;
            }
            if (this._healthModal && this._healthModal.parentNode) {
                this._healthModal.parentNode.removeChild(this._healthModal);
            }
            this._healthModal = null;
            // 배지 버튼으로 focus 복귀
            if (!this._destroyed && this._els && this._els.healthBadge) {
                this._els.healthBadge.focus();
            }
        };

        /**
         * 검색/상세 페이지 fetch 를 취소한다.
         */
        WikiView.prototype._abortTransientRequests = function () {
            this._abortControllers.forEach(function (c) {
                try { c.abort(); } catch (_) { /* noop */ }
            });
            this._abortControllers = [];
        };

        /**
         * 진행 중인 모든 fetch 를 취소한다.
         */
        WikiView.prototype._abortAllRequests = function () {
            this._abortTransientRequests();
            this._lifecycleAbortControllers.forEach(function (c) {
                try { c.abort(); } catch (_) { /* noop */ }
            });
            this._lifecycleAbortControllers = [];
        };

        /**
         * 뷰를 정리한다 — 이벤트, 타이머, fetch, 모달 모두 해제.
         */
        WikiView.prototype.destroy = function () {
            this._destroyed = true;

            // 진행 중 fetch 취소
            this._abortAllRequests();

            // 검색 debounce 타이머 해제
            if (this._searchTimer) {
                clearTimeout(this._searchTimer);
                this._searchTimer = null;
            }

            // Health 모달 닫기
            this._closeHealthModal();

            // 이벤트 리스너 해제
            this._listeners.forEach(function (entry) {
                entry.el.removeEventListener(entry.type, entry.fn);
            });
            this._listeners = [];

            // 타이머 해제
            this._timers.forEach(function (t) { clearInterval(t); clearTimeout(t); });
            this._timers = [];

            // 페이지 타이틀 복원
            document.title = "회의록 · Recap";
        };
        return WikiView;
    }

    window.MeetingWikiView = {
        create: create,
    };
})();
