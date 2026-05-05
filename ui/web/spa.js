/* =================================================================
 * 회의 전사 시스템 — SPA 모듈 (spa.js)
 *
 * 목적: 3-column 레이아웃(nav-bar + list-panel + content) 기반
 *       단일 페이지 애플리케이션(SPA)을 구현한다.
 *       History API 기반 클라이언트 라우터, 네비게이션 바,
 *       리스트 패널, EmptyView / ViewerView / SearchView / ChatView 를 제공한다.
 *
 * 의존성: MeetingApp (app.js) — apiRequest, apiPost, apiDelete,
 *         formatDate, formatTime, escapeHtml, safeText, getFileName,
 *         getStatusLabel, renderMarkdown, highlightText,
 *         connectWebSocket, initErrorBanner, createSkeletonCards,
 *         SPEAKER_COLORS, PIPELINE_STEPS, copyToClipboard
 * ================================================================= */
(function () {
    "use strict";

    var App = window.MeetingApp;

    // === SVG 아이콘 (macOS SF Symbols 스타일, 16x16, stroke-width 1.5) ===
    var Icons = {
        // 마이크 아이콘 (오디오 파일)
        mic: '<svg class="icon" width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M8 1.5a2 2 0 0 0-2 2v4a2 2 0 0 0 4 0v-4a2 2 0 0 0-2-2Z" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M4 6.5v1a4 4 0 0 0 8 0v-1M8 11.5v3M6 14.5h4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
        // 달력 아이콘 (날짜)
        calendar: '<svg class="icon" width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="1.5" y="2.5" width="13" height="12" rx="2" stroke="currentColor" stroke-width="1.5"/><path d="M1.5 6.5h13M5 1v3M11 1v3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
        // 사람 아이콘 (화자)
        person: '<svg class="icon" width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="8" cy="5" r="2.5" stroke="currentColor" stroke-width="1.5"/><path d="M3 14.5c0-2.76 2.24-5 5-5s5 2.24 5 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
        // 말풍선 아이콘 (발화)
        chat: '<svg class="icon" width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M2.5 2.5h11a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1h-6l-3 2.5v-2.5h-2a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1Z" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
        // 메모 아이콘 (전사/기록)
        doc: '<svg class="icon" width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M4 1.5h5.5L13 5v9a1.5 1.5 0 0 1-1.5 1.5h-7A1.5 1.5 0 0 1 3 14V3a1.5 1.5 0 0 1 1-1.5Z" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M9.5 1.5V5H13M5.5 8.5h5M5.5 11h3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
        // 시계 아이콘 (타임스탬프)
        clock: '<svg class="icon" width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="8" cy="8" r="6.5" stroke="currentColor" stroke-width="1.5"/><path d="M8 4v4l2.5 2.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
        // 클립 아이콘 (참조/첨부)
        clip: '<svg class="icon" width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M7 14.5a3.5 3.5 0 0 1-3.5-3.5V5a2.5 2.5 0 0 1 5 0v6a1.5 1.5 0 0 1-3 0V5.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
        // 복사 아이콘
        copy: '<svg class="icon" width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="5.5" y="5.5" width="9" height="9" rx="1.5" stroke="currentColor" stroke-width="1.5"/><path d="M3.5 10.5h-1a1.5 1.5 0 0 1-1.5-1.5v-7a1.5 1.5 0 0 1 1.5-1.5h7a1.5 1.5 0 0 1 1.5 1.5v1" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
        // 체크 아이콘 (완료/복사됨)
        check: '<svg class="icon" width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M3 8.5l3.5 3.5L13 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
        // 클립보드/목록 아이콘 (빈 상태)
        clipboard: '<svg class="icon icon-lg" width="48" height="48" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="10" y="6" width="28" height="36" rx="4" stroke="currentColor" stroke-width="2"/><path d="M18 6v-1a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v1M16 18h16M16 26h10M16 34h12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
        // 녹음 도트 아이콘
        recordDot: '<svg class="icon" width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="8" cy="8" r="4" fill="#FF3B30"/></svg>',
        // 재생 아이콘
        play: '<svg class="icon" width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M4 2.5l9 5.5-9 5.5V2.5Z" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
        // 기어 아이콘 (처리 중)
        gear: '<svg class="icon" width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="8" cy="8" r="2" stroke="currentColor" stroke-width="1.5"/><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3.05 3.05l1.41 1.41M11.54 11.54l1.41 1.41M3.05 12.95l1.41-1.41M11.54 4.46l1.41-1.41" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
        // 모래시계 아이콘 (대기 중)
        hourglass: '<svg class="icon" width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M4 1.5h8M4 14.5h8M4.5 1.5v3.5L8 8l-3.5 3v3.5M11.5 1.5v3.5L8 8l3.5 3v3.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
        // X 아이콘 (실패)
        xCircle: '<svg class="icon" width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="8" cy="8" r="6.5" stroke="currentColor" stroke-width="1.5"/><path d="M5.5 5.5l5 5M10.5 5.5l-5 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
    };

    // === 상수 ===
    var STATUS_POLL_INTERVAL = 5000;     // 상태 폴링 간격 (ms)
    var MEETINGS_POLL_INTERVAL = 15000;  // 회의 목록 갱신 간격 (ms)
    var AUTO_HIDE_DELAY = 8000;          // 에러 배너 자동 숨김 (ms)

    // app.js에서 정의된 파이프라인 단계를 재사용 (중복 방지)
    var PIPELINE_STEPS = (typeof App !== "undefined" && App.PIPELINE_STEPS) ? App.PIPELINE_STEPS : [
        { key: "convert",    label: "변환" },
        { key: "transcribe", label: "전사" },
        { key: "diarize",    label: "화자" },
        { key: "merge",      label: "병합" },
        { key: "correct",    label: "보정" },
        { key: "summarize",  label: "요약" },
    ];

    // 상태별 정렬 우선순위 (처리 중 > 대기 > 실패 > 완료)
    var STATUS_SORT_ORDER = {
        recording: 0,
        transcribing: 1,
        diarizing: 2,
        merging: 3,
        embedding: 4,
        queued: 5,
        recorded: 6,
        failed: 7,
        completed: 8,
    };

    // =================================================================
    // === 에러 배너 (글로벌) ===
    // =================================================================

    var errorBanner = App.initErrorBanner("errorBanner", "errorMessage", "errorClose");
    var _originalShow = errorBanner.show;
    var _autoHideTimer = null;

    /**
     * 에러 배너를 표시한다. 8초 후 자동 숨김.
     * @param {string} text - 에러 메시지
     */
    errorBanner.show = function (text) {
        if (_autoHideTimer) { clearTimeout(_autoHideTimer); _autoHideTimer = null; }
        _originalShow(text);
        _autoHideTimer = setTimeout(function () {
            errorBanner.hide();
            _autoHideTimer = null;
        }, AUTO_HIDE_DELAY);
    };


    // =================================================================
    // === NavBar (네비게이션 바 제어) ===
    // =================================================================

    var NavBar = (function () {
        var _buttons = [];

        /**
         * 네비게이션 바를 초기화한다.
         * nav-btn 클릭 시 라우터 내비게이션을 수행한다.
         */
        function init() {
            _buttons = Array.from(document.querySelectorAll("#nav-bar .nav-btn"));

            _buttons.forEach(function (btn) {
                btn.addEventListener("click", function () {
                    var route = btn.getAttribute("data-route");
                    if (route) {
                        Router.navigate(route);
                    }
                });
            });
            // 전역 키보드 단축키(⌘,/⌘1/⌘2/⌘3/⌘K)는 WS-3 Command Palette 모듈이 소유.
        }

        /**
         * 현재 경로에 맞는 네비게이션 버튼을 활성화한다.
         *
         * 활성 버튼에는 시각용 `.active` 클래스와 ARIA 의 `aria-current="page"` 를
         * 동시에 부여한다 (T-301 mockup §1.1). 비활성 버튼에서는
         * `removeAttribute("aria-current")` 로 속성 자체를 제거한다 — `"false"`
         * 로 두면 macOS VoiceOver 가 "current page" 로 오발화하는 회귀 가능성이
         * 있어 mockup §1.1 의 정규화 규칙을 그대로 따른다.
         *
         * @param {string} path - URL 경로
         */
        function setActiveFromPath(path) {
            var pathname = path.split("?")[0];

            _buttons.forEach(function (btn) {
                var route = btn.getAttribute("data-route");
                btn.classList.remove("active");
                btn.removeAttribute("aria-current");

                // /app 라우트: /app 또는 /app/viewer/* 경로에서 활성화
                if (route === "/app") {
                    if (pathname === "/app" || pathname === "/app/" || pathname.indexOf("/app/viewer/") === 0) {
                        btn.classList.add("active");
                        btn.setAttribute("aria-current", "page");
                    }
                } else if (route === pathname) {
                    btn.classList.add("active");
                    btn.setAttribute("aria-current", "page");
                }
            });
        }

        return {
            init: init,
            setActiveFromPath: setActiveFromPath,
        };
    })();


    // =================================================================
    // === Router (History API 기반 클라이언트 라우터) ===
    // =================================================================

    var Router = (function () {
        var _currentView = null;  // 현재 활성 뷰 인스턴스
        var _contentEl = null;    // #content 엘리먼트
        var _currentPath = "/app"; // 현재 활성 경로 (popstate 가드용)

        /**
         * 라우트 정의 목록.
         * 각 라우트는 pattern(정규식)과 handler(뷰 생성 함수)로 구성.
         */
        var routes = [
            {
                // /app/viewer/{meetingId} (쿼리 파라미터 제외)
                pattern: /^\/app\/viewer\/([^?]+)/,
                handler: function (match) {
                    return new ViewerView(decodeURIComponent(match[1]));
                },
            },
            {
                // /app/search
                pattern: /^\/app\/search$/,
                handler: function () {
                    return new SearchView();
                },
            },
            {
                // /app/chat
                pattern: /^\/app\/chat$/,
                handler: function () {
                    return new ChatView();
                },
            },
            {
                // /app/wiki — LLM Wiki Phase 2.F
                pattern: /^\/app\/wiki\/?$/,
                handler: function () {
                    return new WikiView();
                },
            },
            {
                // /app/settings 및 /app/settings/{tab} (tab: general|prompts|vocabulary)
                pattern: /^\/app\/settings(?:\/(general|prompts|vocabulary))?$/,
                handler: function (match) {
                    return new SettingsView({ initialTab: match[1] || "general" });
                },
            },
            {
                // /app/ab-test/new — A/B 테스트 생성
                pattern: /^\/app\/ab-test\/new/,
                handler: function () {
                    return new AbTestNewView();
                },
            },
            {
                // /app/ab-test/{testId} — A/B 테스트 결과
                pattern: /^\/app\/ab-test\/([^/?]+)/,
                handler: function (match) {
                    return new AbTestResultView(decodeURIComponent(match[1]));
                },
            },
            {
                // /app/ab-test — A/B 테스트 목록
                pattern: /^\/app\/ab-test\/?$/,
                handler: function () {
                    return new AbTestListView();
                },
            },
            {
                // /app (홈) — 기본 라우트 → EmptyView
                pattern: /^\/app\/?$/,
                handler: function () {
                    return new EmptyView();
                },
            },
        ];

        /**
         * 주어진 경로에 대응하는 뷰를 렌더링한다.
         * @param {string} path - URL 경로
         */
        function resolve(path) {
            // 쿼리 문자열 분리 (순수 경로만 매칭에 사용)
            var pathname = path.split("?")[0];

            // 이전 뷰가 있으면 정리 (이벤트 리스너, 타이머 해제)
            if (_currentView && typeof _currentView.destroy === "function") {
                _currentView.destroy();
            }
            _currentView = null;

            // 콘텐츠 영역 초기화
            _contentEl.innerHTML = "";

            // list-panel chat-mode 처리 (채팅/설정/위키 뷰에서는 CSS로 숨김)
            var listPanel = document.getElementById("list-panel");
            if (listPanel) {
                if (
                    pathname === "/app/chat" ||
                    pathname === "/app/wiki" ||
                    pathname.indexOf("/app/wiki/") === 0 ||
                    pathname.indexOf("/app/settings") === 0 ||
                    pathname.indexOf("/app/ab-test") === 0
                ) {
                    listPanel.classList.add("chat-mode");
                } else {
                    listPanel.classList.remove("chat-mode");
                }
            }

            // 경로 매칭
            for (var i = 0; i < routes.length; i++) {
                var match = pathname.match(routes[i].pattern);
                if (match) {
                    _currentView = routes[i].handler(match);
                    // 네비게이션 바 활성 상태 업데이트
                    NavBar.setActiveFromPath(path);
                    // 리스트 패널 활성 항목 업데이트
                    ListPanel.setActiveFromPath(pathname);
                    return;
                }
            }

            // 매칭 안 되면 홈으로 리다이렉트
            navigate("/app");
        }

        /**
         * 라우터를 초기화한다.
         * popstate 이벤트를 바인딩하고 현재 경로를 해석한다.
         */
        function init() {
            _contentEl = document.getElementById("content");

            // 뒤로가기/앞으로가기 처리 — 편집 중이면 canLeave 가드로 차단
            window.addEventListener("popstate", function () {
                var fullPath = window.location.pathname + window.location.search;
                if (
                    _currentView &&
                    typeof _currentView.canLeave === "function" &&
                    _currentView.canLeave() === false
                ) {
                    // URL 을 이전 위치로 되돌림 (사용자 편집 보존)
                    history.pushState(null, "", _currentPath);
                    return;
                }
                _currentPath = fullPath;
                resolve(fullPath);
            });

            // 현재 경로에 맞는 뷰 렌더링
            var path = window.location.pathname;

            // /static/index.html 또는 루트 경로 → /app 으로 리다이렉트
            if (path === "/" || path === "/static/index.html" || path === "/static/" || path === "/index.html") {
                history.replaceState(null, "", "/app");
                path = "/app";
            }

            _currentPath = path + window.location.search;
            resolve(path);
        }

        /**
         * 지정 경로로 내비게이션한다.
         * 현재 뷰가 canLeave()를 노출하고 false를 반환하면 이동을 취소한다.
         * @param {string} path - 이동할 경로
         */
        function navigate(path) {
            // 현재 URL과 동일하면 무시 (경로 + 쿼리 스트링 모두 비교)
            var current = window.location.pathname + window.location.search;
            if (current === path) return;
            // 편집 중 이탈 가드
            if (_currentView && typeof _currentView.canLeave === "function") {
                if (_currentView.canLeave() === false) {
                    return;
                }
            }
            history.pushState(null, "", path);
            _currentPath = path;
            resolve(path);
        }

        /**
         * 현재 콘텐츠 영역 엘리먼트를 반환한다.
         * @returns {HTMLElement}
         */
        function getContentEl() {
            return _contentEl;
        }

        return {
            init: init,
            navigate: navigate,
            getContentEl: getContentEl,
        };
    })();


    // =================================================================
    // === ListPanel (리스트 패널 — 회의 목록) ===
    // =================================================================

    var ListPanelModule = window.MeetingListPanel;
    var ListPanel = (
        ListPanelModule && typeof ListPanelModule.create === "function"
    )
        ? ListPanelModule.create({
            App: App,
            Router: Router,
            errorBanner: errorBanner,
            STATUS_SORT_ORDER: STATUS_SORT_ORDER,
            STATUS_POLL_INTERVAL: STATUS_POLL_INTERVAL,
            MEETINGS_POLL_INTERVAL: MEETINGS_POLL_INTERVAL,
        })
        : {
            init: function () {},
            loadMeetings: function () {},
            setActive: function () {},
            setActiveFromPath: function () {},
            getMeetings: function () { return []; },
            destroy: function () {},
            clearSelection: function () {},
            getSelectedIds: function () { return []; },
        };


    // =================================================================
    // === BulkActionBar (컨텍스트 액션 바, bulk-actions §B) ===
    // =================================================================

    var BulkActionBarModule = window.MeetingBulkActionBar;
    var BulkActionBar = (
        BulkActionBarModule && typeof BulkActionBarModule.create === "function"
    )
        ? BulkActionBarModule.create({
            App: App,
            ListPanel: ListPanel,
        })
        : {
            init: function BulkActionBarUnavailable() {
                throw new Error("MeetingBulkActionBar module is not loaded");
            },
            showBulkToast: function () {},
        };


    var EmptyViewModule = window.MeetingEmptyView;
    var EmptyView = (
        EmptyViewModule && typeof EmptyViewModule.create === "function"
    )
        ? EmptyViewModule.create({
            App: App,
            Router: Router,
            Icons: Icons,
            showBulkToast: BulkActionBar.showBulkToast,
        })
        : function EmptyViewUnavailable() {
            throw new Error("MeetingEmptyView module is not loaded");
        };

    var SearchViewModule = window.MeetingSearchView;
    var SearchView = (
        SearchViewModule && typeof SearchViewModule.create === "function"
    )
        ? SearchViewModule.create({
            App: App,
            Router: Router,
            Icons: Icons,
            errorBanner: errorBanner,
        })
        : function SearchViewUnavailable() {
            throw new Error("MeetingSearchView module is not loaded");
        };

    var ViewerViewModule = window.MeetingViewerView;
    var ViewerView = (
        ViewerViewModule && typeof ViewerViewModule.create === "function"
    )
        ? ViewerViewModule.create({
            App: App,
            Router: Router,
            ListPanel: ListPanel,
            Icons: Icons,
            PIPELINE_STEPS: PIPELINE_STEPS,
            errorBanner: errorBanner,
        })
        : function ViewerViewUnavailable() {
            throw new Error("MeetingViewerView module is not loaded");
        };


    var ChatViewModule = window.MeetingChatView;
    var ChatView = (
        ChatViewModule && typeof ChatViewModule.create === "function"
    )
        ? ChatViewModule.create({
            App: App,
            Router: Router,
            Icons: Icons,
            errorBanner: errorBanner,
        })
        : function ChatViewUnavailable() {
            throw new Error("MeetingChatView module is not loaded");
        };


    var WikiViewModule = window.MeetingWikiView;
    var WikiView = (
        WikiViewModule && typeof WikiViewModule.create === "function"
    )
        ? WikiViewModule.create({
            App: App,
            Router: Router,
        })
        : function WikiViewUnavailable() {
            throw new Error("MeetingWikiView module is not loaded");
        };


    var SettingsViewModule = window.MeetingSettingsView;
    var SettingsView = (
        SettingsViewModule && typeof SettingsViewModule.create === "function"
    )
        ? SettingsViewModule.create({
            App: App,
            Router: Router,
            errorBanner: errorBanner,
        })
        : function SettingsViewUnavailable() {
            throw new Error("MeetingSettingsView module is not loaded");
        };


    var AbTestViewModule = window.MeetingAbTestView;
    var AbTestViews = (
        AbTestViewModule && typeof AbTestViewModule.create === "function"
    )
        ? AbTestViewModule.create({
            App: App,
            Router: Router,
            errorBanner: errorBanner,
        })
        : {
            ListView: function AbTestListViewUnavailable() {
                throw new Error("MeetingAbTestView module is not loaded");
            },
            NewView: function AbTestNewViewUnavailable() {
                throw new Error("MeetingAbTestView module is not loaded");
            },
            ResultView: function AbTestResultViewUnavailable() {
                throw new Error("MeetingAbTestView module is not loaded");
            },
        };
    var AbTestListView = AbTestViews.ListView;
    var AbTestNewView = AbTestViews.NewView;
    var AbTestResultView = AbTestViews.ResultView;


    var GlobalResourceBarModule = window.MeetingGlobalResourceBar;
    var GlobalResourceBar = (
        GlobalResourceBarModule && typeof GlobalResourceBarModule.create === "function"
    )
        ? GlobalResourceBarModule.create({
            App: App,
            intervalMs: 5000,
        })
        : {
            start: function () {},
            stop: function () {},
            refresh: function () {},
        };


    // =================================================================
    // === 키보드 단축키 (글로벌) ===
    // =================================================================

    var CommandPaletteModule = window.MeetingCommandPalette;
    var commandPalette = (
        CommandPaletteModule && typeof CommandPaletteModule.create === "function"
    )
        ? CommandPaletteModule.create({ App: App, Router: Router })
        : { open: function () {} };

    function isEditingContext(target) {
        if (
            CommandPaletteModule &&
            typeof CommandPaletteModule.isEditingContext === "function"
        ) {
            return CommandPaletteModule.isEditingContext(target);
        }
        if (!target) return false;
        var tag = (target.tagName || "").toUpperCase();
        return (
            tag === "INPUT" || tag === "TEXTAREA" || target.isContentEditable
        );
    }

    document.addEventListener("keydown", function (e) {
        if (!(e.metaKey || e.ctrlKey)) return;

        if (e.key === "k") {
            if (isEditingContext(e.target)) return;
            e.preventDefault();
            commandPalette.open();
            return;
        }

        if (e.key === ",") {
            e.preventDefault();
            Router.navigate("/app/settings");
            return;
        }

        if (e.key === "1" || e.key === "2" || e.key === "3") {
            if (isEditingContext(e.target)) return;
            e.preventDefault();
            if (e.key === "1") Router.navigate("/app");
            else if (e.key === "2") Router.navigate("/app/search");
            else Router.navigate("/app/chat");
            return;
        }
    });


    // =================================================================
    // === 공개 API ===
    // =================================================================

    window.SPA = {
        Router: Router,
        NavBar: NavBar,
        ListPanel: ListPanel,
        BulkActionBar: BulkActionBar,
        EmptyView: EmptyView,
        SearchView: SearchView,
        ViewerView: ViewerView,
        ChatView: ChatView,
        WikiView: WikiView,
        SettingsView: SettingsView,
        CommandPalette: commandPalette,
    };

    // 전역 노출 — Playwright 시나리오 / 외부 핸들러가 ListPanel.clearSelection 등 사용
    window.ListPanel = ListPanel;


    // =================================================================
    // === 초기화 ===
    // =================================================================

    // WebSocket 연결
    App.connectWebSocket();

    // 네비게이션 바 초기화
    NavBar.init();

    // 리스트 패널 초기화
    ListPanel.init();

    // 컨텍스트 액션 바 초기화 (bulk-actions §B)
    BulkActionBar.init();

    // 라우터 초기화 (현재 경로에 맞는 뷰 렌더링)
    Router.init();

    // 글로벌 리소스 모니터 시작 (모든 탭 공통 상단 표시)
    GlobalResourceBar.start();

    // 모바일 drawer 토글 초기화 (T-302)
    // - 768px 이하에서 햄버거 클릭 → 사이드바 drawer 열림/닫힘
    // - SSOT: 햄버거 버튼의 aria-expanded (사이드바에는 ARIA 상태 부여 금지)
    // - 시각 토글: 사이드바의 .is-open 클래스
    // - ESC 키 + 백드롭 클릭으로 닫힘 (WCAG 2.1.2 No Keyboard Trap)
    // - 닫힘 시 햄버거 버튼으로 focus 복귀 (mockup §6.3)
    (function initMobileDrawer() {
        var toggleBtn = document.getElementById("mobile-menu-toggle");
        var panel = document.getElementById("list-panel");
        var backdrop = document.getElementById("drawer-backdrop");
        if (!toggleBtn || !panel || !backdrop) {
            return;
        }

        // drawer 안 첫 focusable 요소 — focus trap 시작점
        function firstFocusable() {
            return panel.querySelector(
                'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
            );
        }

        function openDrawer() {
            toggleBtn.setAttribute("aria-expanded", "true");
            toggleBtn.setAttribute("aria-label", "메뉴 닫기");
            panel.classList.add("is-open");
            backdrop.classList.add("visible");
            document.body.style.overflow = "hidden";
            // focus 이동 — drawer 안 첫 focusable 항목으로
            var first = firstFocusable();
            if (first) {
                first.focus();
            }
        }

        function closeDrawer() {
            toggleBtn.setAttribute("aria-expanded", "false");
            toggleBtn.setAttribute("aria-label", "메뉴 열기");
            panel.classList.remove("is-open");
            backdrop.classList.remove("visible");
            document.body.style.overflow = "";
            // focus 복귀 — 햄버거 버튼으로
            toggleBtn.focus();
        }

        toggleBtn.addEventListener("click", function () {
            if (toggleBtn.getAttribute("aria-expanded") === "true") {
                closeDrawer();
            } else {
                openDrawer();
            }
        });

        backdrop.addEventListener("click", closeDrawer);

        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape" && toggleBtn.getAttribute("aria-expanded") === "true") {
                closeDrawer();
            }
        });
    })();

    // 테마 토글 초기화
    (function initThemeToggle() {
        var btn = document.getElementById("themeToggle");
        if (!btn) return;

        // 저장된 테마 복원
        var saved = localStorage.getItem("theme");
        if (saved === "dark" || saved === "light") {
            document.documentElement.setAttribute("data-theme", saved);
        }

        btn.addEventListener("click", function () {
            var current = document.documentElement.getAttribute("data-theme");
            var isDark;

            if (current === "dark") {
                // 다크 → 라이트
                document.documentElement.setAttribute("data-theme", "light");
                localStorage.setItem("theme", "light");
                isDark = false;
            } else if (current === "light") {
                // 라이트 → 다크
                document.documentElement.setAttribute("data-theme", "dark");
                localStorage.setItem("theme", "dark");
                isDark = true;
            } else {
                // 시스템 기본 → 반대로 전환
                isDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
                var newTheme = isDark ? "light" : "dark";
                document.documentElement.setAttribute("data-theme", newTheme);
                localStorage.setItem("theme", newTheme);
            }
        });
    })();

})();
