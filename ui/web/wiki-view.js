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
        //   - 인용 마커 [meeting:abc12345@HH:MM:SS] → /app/viewer/{id}?t=초
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
            { id: "action_items", label: "액션아이템", types: ["action_items", "action_item"] },
            { id: "people",       label: "인물",       types: ["people", "person"] },
            { id: "projects",     label: "프로젝트",   types: ["projects", "project"] },
            { id: "topics",       label: "주제",       types: ["topics", "topic"] },
        ];

        // [meeting:8자리hex@HH:MM:SS] 형태 인용 마커 정규식.
        // 글로벌 플래그는 매번 새로 생성해 lastIndex 누적을 피한다.
        var WIKI_CITATION_PATTERN = /\[meeting:([a-f0-9]{8})@(\d{2}:\d{2}:\d{2})\]/g;

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

            self._render();
            self._bind();
            self._loadPages();
            self._loadHealth();
        }

        /**
         * 위키 뷰 DOM 을 렌더링한다.
         */
        WikiView.prototype._render = function () {
            var contentEl = Router.getContentEl();
            contentEl.innerHTML = "";

            var html = [
                '<div class="wiki-view">',
                // 상단 헤더 (검색 + Health)
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
                // 본문 (트리 + 미리보기)
                '  <div class="wiki-body">',
                '    <nav class="wiki-tree" id="wikiTree" aria-label="위키 페이지 트리"></nav>',
                '    <section class="wiki-preview" id="wikiPreview" aria-live="polite"></section>',
                '  </div>',
                '</div>',
            ].join("\n");

            contentEl.innerHTML = html;

            this._els = {
                searchInput: document.getElementById("wikiSearchInput"),
                healthBadge: document.getElementById("wikiHealthBadge"),
                healthDot: document.getElementById("wikiHealthDot"),
                healthLabel: document.getElementById("wikiHealthLabel"),
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

            // 트리 위임 클릭 — 카테고리 헤더 또는 페이지 항목
            var onTreeClick = function (e) {
                var header = e.target.closest(".wiki-tree__category-header");
                if (header && !header.classList.contains("wiki-tree__category-header--disabled")) {
                    self._toggleCategory(header.getAttribute("data-cat"));
                    return;
                }
                var item = e.target.closest(".wiki-tree__item");
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

            // 트리 키보드 — 페이지 항목에 Enter/Space 로 선택
            var onTreeKeydown = function (e) {
                if (e.key !== "Enter" && e.key !== " ") return;
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
                return [
                    '<button type="button" class="wiki-tree__item' +
                        (isActive ? " wiki-tree__item--active" : "") +
                        '" data-path="' + App.escapeHtml(r.path) +
                        '" data-type="' + App.escapeHtml(r.type) +
                        '" tabindex="0">',
                    '  <span class="wiki-tree__item-title">' + App.escapeHtml(rawTitle) + "</span>",
                    snippet ? '  <span class="wiki-tree__item-meta">' + snippet + "</span>" : "",
                    "</button>",
                ].filter(Boolean).join("");
            }).join("");

            self._els.tree.innerHTML = [
                '<div class="wiki-tree__category" data-cat="search">',
                '  <button type="button" class="wiki-tree__category-header"',
                '          data-cat="search" aria-expanded="true" tabindex="-1">',
                '    <span class="wiki-tree__caret">\u25BE</span>',
                '    <span class="wiki-tree__category-name">검색 결과</span>',
                '    <span class="wiki-tree__category-count">(' + results.length + ")</span>",
                "  </button>",
                '  <div class="wiki-tree__items">' + itemsHtml + "</div>",
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

            App.apiRequest(
                "/wiki/search?q=" + encodeURIComponent(q) + "&limit=20",
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
            var items = self._els.tree.querySelectorAll(".wiki-tree__item");
            items.forEach(function (item) {
                if (item.getAttribute("data-path") === self._activePagePath) {
                    item.classList.add("wiki-tree__item--active");
                } else {
                    item.classList.remove("wiki-tree__item--active");
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
