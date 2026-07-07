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

            var brandHomeLink = document.getElementById("brandHomeLink");
            if (brandHomeLink) {
                brandHomeLink.addEventListener("click", function (event) {
                    event.preventDefault();
                    Router.navigate("/app");
                });
            }

            var globalHomeButton = document.getElementById("globalHomeButton");
            if (globalHomeButton) {
                globalHomeButton.addEventListener("click", function () {
                    Router.navigate("/app");
                });
            }
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
                // /app/setup — 최초 설정 준비 상태
                pattern: /^\/app\/setup\/?$/,
                handler: function () {
                    return new SetupView();
                },
            },
            {
                // /app/settings 및 /app/settings/{tab}
                pattern: /^\/app\/settings(?:\/(general|prompts|vocabulary|reindex|wiki-backfill))?$/,
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
            document.body.setAttribute("data-route", pathname);

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
                    pathname === "/app/setup" ||
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
            if (current === path) {
                if (MobileDrawer && MobileDrawer.isOpen && MobileDrawer.isOpen()) {
                    MobileDrawer.close({ restoreFocus: false });
                }
                return;
            }
            // 편집 중 이탈 가드
            if (_currentView && typeof _currentView.canLeave === "function") {
                if (_currentView.canLeave() === false) {
                    return;
                }
            }
            history.pushState(null, "", path);
            _currentPath = path;
            resolve(path);
            if (MobileDrawer && MobileDrawer.isOpen && MobileDrawer.isOpen()) {
                MobileDrawer.close({ restoreFocus: false });
            }
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
    // === SetupView (최초 설정 준비 상태)
    // =================================================================

    function SetupView() {
        this._destroyed = false;
        this._controller = null;
        this._contentEl = Router.getContentEl();
        this._onActionClick = null;
        this._renderShell();
        this._bindEvents();
        this._loadReadiness();
    }

    SetupView.prototype.destroy = function () {
        this._destroyed = true;
        if (this._controller) {
            this._controller.abort();
            this._controller = null;
        }
        if (this._onActionClick) {
            this._contentEl.removeEventListener("click", this._onActionClick);
            this._onActionClick = null;
        }
    };

    SetupView.prototype._renderShell = function () {
        this._contentEl.innerHTML = [
            '<div class="setup-view">',
            '  <header class="setup-header">',
            '    <div>',
            '      <div class="overline">첫 실행 준비</div>',
            '      <h2 class="setup-title">녹음과 전사를 시작하기 전 확인</h2>',
            '      <p class="setup-subtitle">로컬 환경 상태만 확인합니다. 설치, 권한 변경, 모델 다운로드는 실행하지 않습니다.</p>',
            '    </div>',
            '    <div class="setup-actions">',
            '      <button type="button" class="btn-secondary" id="setupRefreshBtn">새로고침</button>',
            '      <button type="button" class="settings-save-btn" id="setupSettingsBtn">설정 열기</button>',
            '    </div>',
            '  </header>',
            '  <section class="setup-summary is-loading" id="setupSummary" role="status" aria-live="polite">',
            '    <div class="setup-summary-dot is-loading" aria-hidden="true"></div>',
            '    <div>',
            '      <div class="setup-summary-title">준비 상태 확인 중</div>',
            '      <div class="setup-summary-sub">데이터 디렉토리, 오디오 장치, 모델 상태를 확인합니다.</div>',
            '    </div>',
            '  </section>',
            '  <section class="setup-capabilities" id="setupCapabilities" aria-label="기능 준비 상태"></section>',
            '  <section class="setup-checks" id="setupChecks" aria-label="설정 점검 항목">',
            '    <div class="setup-skeleton"></div>',
            '    <div class="setup-skeleton"></div>',
            '    <div class="setup-skeleton"></div>',
            '  </section>',
            '</div>',
        ].join("\n");
    };

    SetupView.prototype._bindEvents = function () {
        var self = this;
        var refreshBtn = document.getElementById("setupRefreshBtn");
        var settingsBtn = document.getElementById("setupSettingsBtn");
        if (refreshBtn) {
            refreshBtn.addEventListener("click", function () {
                self._loadReadiness();
            });
        }
        if (settingsBtn) {
            settingsBtn.addEventListener("click", function () {
                Router.navigate("/app/settings");
            });
        }
        this._onActionClick = function (event) {
            var rawTarget = event.target;
            if (!rawTarget || typeof rawTarget.closest !== "function") return;
            var target = rawTarget.closest("[data-setup-route]");
            if (!target || !self._contentEl.contains(target)) return;
            var route = setupSafeRoute(target.getAttribute("data-setup-route"));
            if (!route) return;
            event.preventDefault();
            Router.navigate(route);
        };
        this._contentEl.addEventListener("click", this._onActionClick);
    };

    SetupView.prototype._loadReadiness = async function () {
        var self = this;
        if (this._controller) {
            this._controller.abort();
        }
        this._controller = new AbortController();
        this._setLoading();
        try {
            var data = await App.apiRequest("/setup/readiness", {
                signal: this._controller.signal,
            });
            if (self._destroyed) return;
            self._renderReadiness(data);
        } catch (err) {
            if (err && err.name === "AbortError") return;
            if (self._destroyed) return;
            self._renderError(err);
        }
    };

    SetupView.prototype._setLoading = function () {
        var summary = document.getElementById("setupSummary");
        var checks = document.getElementById("setupChecks");
        var capabilities = document.getElementById("setupCapabilities");
        if (summary) {
            summary.className = "setup-summary is-loading";
            summary.innerHTML = [
                '<div class="setup-summary-dot is-loading" aria-hidden="true"></div>',
                '<div>',
                '  <div class="setup-summary-title">준비 상태 확인 중</div>',
                '  <div class="setup-summary-sub">로컬 상태를 읽고 있습니다.</div>',
                '</div>',
            ].join("\n");
        }
        if (capabilities) {
            capabilities.innerHTML = "";
        }
        if (checks) {
            checks.innerHTML = [
                '<div class="setup-skeleton"></div>',
                '<div class="setup-skeleton"></div>',
                '<div class="setup-skeleton"></div>',
            ].join("\n");
        }
    };

    SetupView.prototype._renderReadiness = function (data) {
        this._renderSummary(data);
        this._renderCapabilities(data.capabilities || {});
        this._renderChecks(data.checks || []);
    };

    SetupView.prototype._renderSummary = function (data) {
        var summary = document.getElementById("setupSummary");
        if (!summary) return;
        var ready = Boolean(data && data.ready);
        var configured = Boolean(data && data.configured);
        var title = ready ? "첫 회의 처리 준비 완료" : "확인이 필요한 항목이 있습니다";
        var subtitle = ready
            ? "녹음과 전사에 필요한 기본 조건이 준비되었습니다."
            : (
                configured
                    ? "일부 기능이 아직 완전한 회의 캡처 상태가 아닙니다."
                    : "필수 설정 또는 로컬 도구를 먼저 확인해야 합니다."
            );
        summary.className = "setup-summary " + (ready ? "is-ready" : "is-action-required");
        summary.innerHTML = [
            '<div class="setup-summary-dot ' + (ready ? "is-ready" : "is-action-required") + '" aria-hidden="true"></div>',
            '<div>',
            '  <div class="setup-summary-title">' + App.escapeHtml(title) + '</div>',
            '  <div class="setup-summary-sub">' + App.escapeHtml(subtitle) + '</div>',
            '</div>',
        ].join("\n");
    };

    SetupView.prototype._renderCapabilities = function (capabilities) {
        var host = document.getElementById("setupCapabilities");
        if (!host) return;
        var items = [
            ["recording_usable", "녹음"],
            ["full_meeting_capture_ready", "전체 회의 캡처"],
            ["stt_model_ready", "음성 인식 모델"],
        ];
        host.innerHTML = items.map(function (item) {
            var key = item[0];
            var label = item[1];
            var ready = Boolean(capabilities[key]);
            return [
                '<div class="setup-capability ' + (ready ? "is-ready" : "is-pending") + '">',
                '  <span class="setup-capability-dot" aria-hidden="true"></span>',
                '  <span>' + App.escapeHtml(label) + '</span>',
                '  <strong>' + (ready ? "준비됨" : "확인 필요") + '</strong>',
                '</div>',
            ].join("\n");
        }).join("");
    };

    SetupView.prototype._renderChecks = function (checks) {
        var host = document.getElementById("setupChecks");
        if (!host) return;
        if (!checks.length) {
            host.innerHTML = '<div class="setup-empty">표시할 점검 항목이 없습니다.</div>';
            return;
        }
        host.innerHTML = checks.map(function (check) {
            return [
                '<article class="setup-check setup-check--' + App.escapeHtml(check.status || "unknown") + '">',
                '  <div class="setup-check-main">',
                '    <div class="setup-check-icon" aria-hidden="true">' + setupStatusGlyph(check.status) + '</div>',
                '    <div class="setup-check-copy">',
                '      <div class="setup-check-title-row">',
                '        <h3 class="setup-check-title">' + App.escapeHtml(setupCheckLabel(check.id)) + '</h3>',
                '        <span class="setup-check-badge">' + App.escapeHtml(setupStatusLabel(check.status)) + '</span>',
                '      </div>',
                '      <p class="setup-check-message">' + App.escapeHtml(check.message || "") + '</p>',
                setupActionHint(check.action_hint),
                setupActionList(check.actions),
                setupDetails(check),
                '    </div>',
                '  </div>',
                '</article>',
            ].join("\n");
        }).join("");
    };

    SetupView.prototype._renderError = function (err) {
        var summary = document.getElementById("setupSummary");
        var checks = document.getElementById("setupChecks");
        var message = err && err.message ? err.message : "알 수 없는 오류";
        if (summary) {
            summary.className = "setup-summary is-action-required";
            summary.innerHTML = [
                '<div class="setup-summary-dot is-action-required" aria-hidden="true"></div>',
                '<div>',
                '  <div class="setup-summary-title">준비 상태를 불러오지 못했습니다</div>',
                '  <div class="setup-summary-sub">' + App.escapeHtml(message) + '</div>',
                '</div>',
            ].join("\n");
        }
        if (checks) {
            checks.innerHTML = '<div class="setup-empty">새로고침을 눌러 다시 확인하세요.</div>';
        }
    };

    function setupCheckLabel(id) {
        var labels = {
            base_dir: "데이터 디렉토리",
            python_runtime: "Python 런타임",
            ffmpeg: "ffmpeg",
            hf_token_env: "HuggingFace 토큰",
            audio_devices: "오디오 장치",
            stt_model: "음성 인식 모델",
        };
        return labels[id] || id || "점검 항목";
    }

    function setupStatusLabel(status) {
        var labels = {
            pass: "정상",
            warn: "주의",
            fail: "필요",
            unknown: "확인 불가",
        };
        return labels[status] || "확인 불가";
    }

    function setupStatusGlyph(status) {
        if (status === "pass") return "✓";
        if (status === "warn") return "!";
        if (status === "fail") return "×";
        return "?";
    }

    function setupActionHint(hint) {
        if (!hint) return "";
        return '<p class="setup-check-action">' + App.escapeHtml(hint) + '</p>';
    }

    function setupActionList(actions) {
        if (!Array.isArray(actions) || !actions.length) return "";
        var rendered = actions.map(setupActionMarkup).filter(Boolean).join("");
        if (!rendered) return "";
        return '<div class="setup-check-actions-list">' + rendered + '</div>';
    }

    function setupActionMarkup(action) {
        if (!action || !action.kind) return "";
        var label = App.escapeHtml(action.label || "다음 단계");
        var description = action.description
            ? '<span class="setup-action-description">' + App.escapeHtml(action.description) + '</span>'
            : "";

        if (action.kind === "external_link") {
            var href = setupSafeExternalHref(action.value);
            if (!href) return "";
            return [
                '<a class="setup-action-link" href="' + App.escapeHtml(href) + '" target="_blank" rel="noopener noreferrer">',
                '  <span>' + label + '</span>',
                description,
                '</a>',
            ].join("");
        }

        if (action.kind === "route") {
            var route = setupSafeRoute(action.value);
            if (!route) return "";
            return [
                '<a class="setup-action-link" href="' + App.escapeHtml(route) + '" data-setup-route="' + App.escapeHtml(route) + '">',
                '  <span>' + label + '</span>',
                description,
                '</a>',
            ].join("");
        }

        if (action.kind === "command") {
            var command = setupSafeCommand(action.value);
            if (!command) return "";
            return [
                '<div class="setup-action-command">',
                '  <span class="setup-action-label">' + label + '</span>',
                description,
                '  <code>' + App.escapeHtml(command) + '</code>',
                '</div>',
            ].join("");
        }

        return "";
    }

    function setupSafeExternalHref(value) {
        if (typeof value !== "string" || value.indexOf("https://huggingface.co/") !== 0) {
            return "";
        }
        try {
            var parsed = new URL(value);
            if (parsed.protocol !== "https:" || parsed.hostname !== "huggingface.co") {
                return "";
            }
            return parsed.href;
        } catch (err) {
            return "";
        }
    }

    function setupSafeRoute(value) {
        return value === "/app/settings" ? value : "";
    }

    function setupSafeCommand(value) {
        if (typeof value !== "string") return "";
        if (!value.trim()) return "";
        return value;
    }

    function setupDetails(check) {
        var details = check && check.details ? check.details : {};
        var parts = [];
        if (check.id === "base_dir" && details.path) {
            parts.push("경로 " + details.path);
            if (details.actual_mode && details.expected_mode) {
                parts.push("권한 " + details.actual_mode + " / 권장 " + details.expected_mode);
            }
        } else if (check.id === "ffmpeg" && details.path) {
            parts.push(details.path);
        } else if (check.id === "python_runtime") {
            if (details.runtime_scope === "launcher_handoff") {
                parts.push("런처 전달");
            } else if (details.runtime_scope === "server_reconstructed") {
                parts.push("서버 기준 재구성");
            }
            if (details.python_source) parts.push("선택 " + details.python_source);
            if (details.python_executable) parts.push("후보 " + details.python_executable);
            if (details.running_python) parts.push("실행 중 " + details.running_python);
            if (details.selected_matches_running_python === false) {
                parts.push("실행 Python과 후보 다름");
            }
            if (details.selected_is_file === false) {
                parts.push("후보 파일 아님");
            } else if (details.selected_is_executable === false) {
                parts.push("실행 권한 없음");
            }
        } else if (check.id === "hf_token_env") {
            parts.push(details.configured ? "설정됨" : "미설정");
            if (
                details.environment_variables_present &&
                details.environment_variables_present.length
            ) {
                parts.push("환경변수 " + details.environment_variables_present.join(", "));
            }
        } else if (check.id === "audio_devices") {
            parts.push(details.has_blackhole ? "BlackHole 감지" : "BlackHole 없음");
            parts.push(details.has_aggregate ? "Aggregate 감지" : "Aggregate 없음");
            if (details.selected_mode) parts.push("모드 " + details.selected_mode);
        } else if (check.id === "stt_model") {
            if (details.active_model_id) parts.push(details.active_model_id);
            if (details.model_status) parts.push(details.model_status);
        }
        if (!parts.length) return "";
        return '<div class="setup-check-details">' + parts.map(function (part) {
            return '<span>' + App.escapeHtml(part) + '</span>';
        }).join("") + '</div>';
    }


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

    var ThemeControllerModule = window.MeetingThemeController;
    var ThemeController = (
        ThemeControllerModule && typeof ThemeControllerModule.create === "function"
    )
        ? ThemeControllerModule.create({})
        : {
            init: function () {},
            restore: function () {},
            toggle: function () {},
        };

    var CommandPaletteModule = window.MeetingCommandPalette;
    var commandPaletteDeps = {
        App: App,
        Router: Router,
    };
    if (
        ThemeControllerModule &&
        typeof ThemeControllerModule.create === "function"
    ) {
        commandPaletteDeps.toggleTheme = ThemeController.toggle;
    }
    var commandPalette = (
        CommandPaletteModule && typeof CommandPaletteModule.create === "function"
    )
        ? CommandPaletteModule.create(commandPaletteDeps)
        : { open: function () {} };

    var MobileDrawerModule = window.MeetingMobileDrawer;
    var MobileDrawer = (
        MobileDrawerModule && typeof MobileDrawerModule.create === "function"
    )
        ? MobileDrawerModule.create({})
        : {
            init: function () {},
            open: function () {},
            close: function () {},
            isOpen: function () { return false; },
        };

    var ShortcutControllerModule = window.MeetingShortcutController;
    var ShortcutController = (
        ShortcutControllerModule &&
        typeof ShortcutControllerModule.create === "function"
    )
        ? ShortcutControllerModule.create({
            Router: Router,
            CommandPalette: commandPalette,
            isEditingContext:
                CommandPaletteModule &&
                typeof CommandPaletteModule.isEditingContext === "function"
                    ? CommandPaletteModule.isEditingContext
                    : null,
        })
        : {
            start: function () {},
            stop: function () {},
        };


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
        MobileDrawer: MobileDrawer,
        ThemeController: ThemeController,
        ShortcutController: ShortcutController,
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

    // 글로벌 셸 컨트롤러 초기화
    ThemeController.init();
    MobileDrawer.init();
    ShortcutController.start();

})();
