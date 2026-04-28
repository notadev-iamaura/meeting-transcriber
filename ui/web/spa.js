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
         * @param {string} path - URL 경로
         */
        function setActiveFromPath(path) {
            var pathname = path.split("?")[0];

            _buttons.forEach(function (btn) {
                var route = btn.getAttribute("data-route");
                btn.classList.remove("active");

                // /app 라우트: /app 또는 /app/viewer/* 경로에서 활성화
                if (route === "/app") {
                    if (pathname === "/app" || pathname === "/app/" || pathname.indexOf("/app/viewer/") === 0) {
                        btn.classList.add("active");
                    }
                } else if (route === pathname) {
                    btn.classList.add("active");
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

            // list-panel chat-mode 처리 (채팅/설정 뷰에서는 CSS로 숨김)
            var listPanel = document.getElementById("list-panel");
            if (listPanel) {
                if (pathname === "/app/chat" || pathname.indexOf("/app/settings") === 0 || pathname.indexOf("/app/ab-test") === 0) {
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

    var ListPanel = (function () {
        var _meetings = [];           // 전체 회의 목록 데이터
        var _activeId = null;         // 현재 활성화된 회의 ID
        var _listEl = null;           // #listContent 엘리먼트
        var _searchEl = null;         // #listSearchInput 엘리먼트
        var _sortEl = null;           // #listSortSelect 엘리먼트
        var _countEl = null;          // #listCount 엘리먼트
        var _statusDot = null;        // #statusDot 엘리먼트
        var _statusText = null;       // #statusText 엘리먼트
        var _statusTimer = null;      // 상태 폴링 타이머
        var _meetingsTimer = null;    // 회의 목록 폴링 타이머
        var _searchTimeout = null;    // 검색 디바운스 타이머

        /**
         * 리스트 패널을 초기화한다.
         */
        function init() {
            _listEl = document.getElementById("listContent");
            _searchEl = document.getElementById("listSearchInput");
            _sortEl = document.getElementById("listSortSelect");
            _countEl = document.getElementById("listCount");
            _statusDot = document.getElementById("statusDot");
            _statusText = document.getElementById("statusText");

            // 검색 입력 디바운스
            if (_searchEl) {
                _searchEl.addEventListener("input", function () {
                    clearTimeout(_searchTimeout);
                    _searchTimeout = setTimeout(function () {
                        _applyFilterAndSort();
                    }, 250);
                });
            }

            // 정렬 변경
            if (_sortEl) {
                _sortEl.addEventListener("change", function () {
                    _applyFilterAndSort();
                });
            }

            // WebSocket 이벤트 리스닝 — 회의 목록 자동 갱신
            document.addEventListener("ws:job_completed", function () {
                loadMeetings();
            });
            document.addEventListener("ws:job_added", function () {
                loadMeetings();
            });

            // WebSocket 연결 상태 표시
            document.addEventListener("ws:connection", function (e) {
                if (e.detail.connected) {
                    _statusDot.className = "status-dot connected";
                    App.safeText(_statusText, "연결됨");
                } else {
                    _statusDot.className = "status-dot disconnected";
                    App.safeText(_statusText, "연결 끊김 — 재연결 중...");
                }
            });

            // 녹음 이벤트 처리 (플로팅 바)
            // ──────────────────────────────────────────────
            // 녹음 경과시간 표시 전략:
            //   - 백엔드는 10초마다 `ws:recording_duration` 을 쏜다 (싱크 포인트)
            //   - 프론트는 싱크 포인트를 기준선으로 잡고, 로컬 1초 타이머로
            //     부드럽게 증가시킨다. 다음 싱크가 오면 드리프트를 보정한다.
            //   - 탭 전환(SPA 뷰 이동)에는 영향받지 않지만, 브라우저 탭
            //     자체가 백그라운드로 가면 setInterval 이 throttle 되므로
            //     포커스 복귀 시 다음 싱크 포인트(최대 10초)에 정확히 보정된다.
            // ──────────────────────────────────────────────
            var _recTickTimer = null;
            var _recBaseSeconds = 0;          // 마지막 싱크 시점의 서버 초
            var _recBaseWallClock = 0;        // 그 싱크를 받은 로컬 타임스탬프 (ms)
            function _formatRecDuration(sec) {
                var s0 = Math.floor(sec);
                if (s0 < 0) s0 = 0;
                var m = Math.floor(s0 / 60);
                var s = s0 % 60;
                var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
                return pad(m) + ":" + pad(s);
            }
            // Overlay 타이머용 HH:MM:SS 포맷 (레퍼런스 RecordingOverlay 스펙)
            function _formatRecDurationLong(sec) {
                var s0 = Math.floor(sec);
                if (s0 < 0) s0 = 0;
                var h = Math.floor(s0 / 3600);
                var m = Math.floor((s0 % 3600) / 60);
                var s = s0 % 60;
                var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
                return pad(h) + ":" + pad(m) + ":" + pad(s);
            }
            function _renderRecDuration() {
                var elapsedMs = Date.now() - _recBaseWallClock;
                var cur = _recBaseSeconds + elapsedMs / 1000;
                var pillDur = document.getElementById("recordingDuration");
                if (pillDur) App.safeText(pillDur, _formatRecDuration(cur));
                var overlayTimer = document.getElementById("recordingOverlayTimer");
                if (overlayTimer) App.safeText(overlayTimer, _formatRecDurationLong(cur));
                var overlayMeta = document.getElementById("recordingOverlayMetaDuration");
                if (overlayMeta) App.safeText(overlayMeta, _formatRecDurationLong(cur));
            }
            function _startRecTicker(initialSeconds) {
                _recBaseSeconds = initialSeconds || 0;
                _recBaseWallClock = Date.now();
                _renderRecDuration();
                if (_recTickTimer) clearInterval(_recTickTimer);
                _recTickTimer = setInterval(_renderRecDuration, 1000);
            }
            function _stopRecTicker() {
                if (_recTickTimer) {
                    clearInterval(_recTickTimer);
                    _recTickTimer = null;
                }
                _recBaseSeconds = 0;
                _recBaseWallClock = 0;
            }

            // 녹음 HUD 상태 제어 — overlay(기본) ↔ pill(최소화) 배타 표시.
            // _recOverlayShown 이 true 면 pill 은 숨김, false 면 pill 표시.
            function _showRecHUD(mode) {
                // mode: "overlay" | "pill" | "none"
                var pill = document.getElementById("recordingStatus");
                var overlay = document.getElementById("recordingOverlay");
                if (mode === "overlay") {
                    if (pill) pill.classList.remove("visible");
                    if (overlay) {
                        overlay.classList.add("visible");
                        overlay.setAttribute("aria-hidden", "false");
                    }
                } else if (mode === "pill") {
                    if (overlay) {
                        overlay.classList.remove("visible");
                        overlay.setAttribute("aria-hidden", "true");
                    }
                    if (pill) pill.classList.add("visible");
                } else {
                    if (pill) pill.classList.remove("visible");
                    if (overlay) {
                        overlay.classList.remove("visible");
                        overlay.setAttribute("aria-hidden", "true");
                    }
                }
            }

            // 48-bar waveform 마운트 — 레퍼런스 RecordingOverlay.jsx 기준.
            // 백엔드 실시간 audio-level 스트림이 없어 CSS keyframe 기반 시각 신호.
            (function mountRecordingWaveBars() {
                var wave = document.querySelector("#recordingOverlay .recording-wave");
                if (!wave || wave.childElementCount > 0) return;
                for (var i = 0; i < 48; i++) {
                    var span = document.createElement("span");
                    // 각 bar 에 다른 animation-delay 로 자연스러운 파도 효과
                    span.style.animationDelay = (-(Math.random() * 1.2)).toFixed(2) + "s";
                    wave.appendChild(span);
                }
            })();

            document.addEventListener("ws:recording_started", function () {
                // 새 녹음 → overlay 기본 표시
                _showRecHUD("overlay");
                _startRecTicker(0);
                // 버튼 disabled 초기화
                var recStopBtn = document.getElementById("recordingStopBtn");
                if (recStopBtn) recStopBtn.disabled = false;
                var overlayStop = document.getElementById("recordingOverlayStopBtn");
                if (overlayStop) overlayStop.disabled = false;
                var overlayCancel = document.getElementById("recordingOverlayCancelBtn");
                if (overlayCancel) overlayCancel.disabled = false;
                loadMeetings();
            });
            document.addEventListener("ws:recording_stopped", function () {
                _showRecHUD("none");
                _stopRecTicker();
                loadMeetings();
            });
            document.addEventListener("ws:recording_duration", function (e) {
                // 백엔드 싱크 포인트: 기준선을 갱신하고 즉시 렌더
                var detail = e.detail || {};
                var seconds = detail.duration_seconds || 0;
                _recBaseSeconds = seconds;
                _recBaseWallClock = Date.now();
                // 싱크가 왔는데 타이머가 없다면(복원 누락 등) 새로 시작
                if (!_recTickTimer) {
                    var overlay = document.getElementById("recordingOverlay");
                    var pill = document.getElementById("recordingStatus");
                    // 현재 어느 쪽이 표시 중인지 확인 — overlay 우선, 없으면 pill
                    var mode = (overlay && overlay.classList.contains("visible")) ? "overlay"
                             : (pill && pill.classList.contains("visible")) ? "pill"
                             : "overlay";
                    _showRecHUD(mode);
                    _recTickTimer = setInterval(_renderRecDuration, 1000);
                }
                _renderRecDuration();
            });
            document.addEventListener("ws:recording_error", function (e) {
                var detail = e.detail || {};
                _showRecHUD("none");
                _stopRecTicker();
                var msg = detail.error || detail.message || "녹음 중 오류가 발생했습니다";
                errorBanner.show(msg);
            });

            // Overlay ↔ pill 전환 버튼들
            (function bindRecOverlayControls() {
                var expandBtn = document.getElementById("recordingExpandBtn");
                if (expandBtn) {
                    expandBtn.addEventListener("click", function () {
                        _showRecHUD("overlay");
                    });
                }

                async function sendStop() {
                    try {
                        await App.apiRequest("/recording/stop", { method: "POST" });
                        _showRecHUD("none");
                        _stopRecTicker();
                    } catch (err) {
                        errorBanner.show("녹음 정지 실패: " + (err.message || "알 수 없는 오류"));
                        var ob = document.getElementById("recordingOverlayStopBtn");
                        var oc = document.getElementById("recordingOverlayCancelBtn");
                        if (ob) ob.disabled = false;
                        if (oc) oc.disabled = false;
                    }
                }

                // Overlay 내부 버튼들 — data-recording-action 속성으로 delegate
                var overlay = document.getElementById("recordingOverlay");
                if (overlay) {
                    overlay.addEventListener("click", function (e) {
                        var t = e.target.closest("[data-recording-action]");
                        if (!t) return;
                        var action = t.getAttribute("data-recording-action");
                        if (action === "minimize") {
                            _showRecHUD("pill");
                        } else if (action === "cancel" || action === "stop") {
                            var stopBtn = document.getElementById("recordingOverlayStopBtn");
                            var cancelBtn = document.getElementById("recordingOverlayCancelBtn");
                            if (stopBtn) stopBtn.disabled = true;
                            if (cancelBtn) cancelBtn.disabled = true;
                            sendStop();
                        }
                    });
                }
            })();

            // 범용 안내 모달 (#infoModal) — 아직 구현되지 않은 기능 안내 등에 재사용.
            // 사용: showInfoModal("제목", "본문")
            function showInfoModal(title, message) {
                var modal = document.getElementById("infoModal");
                if (!modal) return;
                var t = document.getElementById("infoModalTitle");
                var m = document.getElementById("infoModalMessage");
                if (t) App.safeText(t, title || "안내");
                if (m) App.safeText(m, message || "");
                modal.classList.remove("hidden");
                var closeBtn = document.getElementById("infoModalClose");
                if (closeBtn) closeBtn.focus();
            }
            function hideInfoModal() {
                var modal = document.getElementById("infoModal");
                if (!modal) return;
                modal.classList.add("hidden");
            }
            // 닫기 버튼 + 배경 클릭 + ESC 키로 닫기
            var _infoModalEl = document.getElementById("infoModal");
            if (_infoModalEl) {
                var _infoCloseBtn = document.getElementById("infoModalClose");
                if (_infoCloseBtn) _infoCloseBtn.addEventListener("click", hideInfoModal);
                _infoModalEl.addEventListener("click", function (e) {
                    // 컨테이너(= overlay)를 직접 눌렀을 때만 — 내부 모달 콘텐츠 클릭은 무시
                    if (e.target === _infoModalEl) hideInfoModal();
                });
                document.addEventListener("keydown", function (e) {
                    if (e.key === "Escape" && !_infoModalEl.classList.contains("hidden")) {
                        hideInfoModal();
                    }
                });
            }

            // 가져오기 모달 (#importModal) — 레퍼런스 ImportPanel.jsx 기준 dropzone + 큐.
            // 실제 업로드 엔드포인트는 아직 없음 → 선택된 파일 큐만 시각화하고
            // "파이프라인 시작" 버튼은 안내 메시지를 노출한다.
            var _importModalEl = document.getElementById("importModal");
            var _importDropzone = document.getElementById("importDropzone");
            var _importFileInput = document.getElementById("importFileInput");
            var _importQueue = document.getElementById("importQueue");
            var _importQueueList = document.getElementById("importQueueList");
            var _importNotice = document.getElementById("importNotice");
            var _importCancelBtn = document.getElementById("importModalCancel");
            var _importStartBtn = document.getElementById("importModalStart");
            var _importFiles = [];

            function _importFormatSize(bytes) {
                if (!bytes || bytes < 0) return "—";
                if (bytes < 1024) return bytes + " B";
                if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
                if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
                return (bytes / 1024 / 1024 / 1024).toFixed(2) + " GB";
            }

            function _renderImportQueue() {
                if (!_importQueueList) return;
                _importQueueList.innerHTML = "";
                _importFiles.forEach(function (f, idx) {
                    var row = document.createElement("div");
                    row.className = "import-queue-item";

                    var name = document.createElement("span");
                    name.className = "import-queue-item-name";
                    name.textContent = f.name;
                    name.setAttribute("title", f.name);

                    var meta = document.createElement("span");
                    meta.className = "import-queue-item-meta";
                    meta.textContent = _importFormatSize(f.size);

                    var remove = document.createElement("button");
                    remove.type = "button";
                    remove.className = "import-queue-item-remove";
                    remove.setAttribute("aria-label", "제거");
                    remove.innerHTML = '<svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="5" y1="5" x2="15" y2="15"></line><line x1="15" y1="5" x2="5" y2="15"></line></svg>';
                    remove.addEventListener("click", function () {
                        _importFiles.splice(idx, 1);
                        _renderImportQueue();
                    });

                    row.appendChild(name);
                    row.appendChild(meta);
                    row.appendChild(remove);
                    _importQueueList.appendChild(row);
                });

                if (_importQueue) _importQueue.hidden = _importFiles.length === 0;
                if (_importStartBtn) _importStartBtn.disabled = _importFiles.length === 0;
                if (_importNotice) _importNotice.hidden = _importFiles.length === 0;
            }

            function _addImportFiles(fileList) {
                if (!fileList) return;
                for (var i = 0; i < fileList.length; i++) {
                    _importFiles.push(fileList[i]);
                }
                _renderImportQueue();
            }

            function _openImportModal() {
                if (!_importModalEl) return;
                _importModalEl.classList.remove("hidden");
                if (_importDropzone) _importDropzone.focus();
            }

            function _closeImportModal() {
                if (!_importModalEl) return;
                _importModalEl.classList.add("hidden");
            }

            var _importBtn = document.getElementById("importBtn");
            if (_importBtn) {
                _importBtn.addEventListener("click", _openImportModal);
            }

            if (_importDropzone && _importFileInput) {
                _importDropzone.addEventListener("click", function () {
                    _importFileInput.click();
                });
                _importDropzone.addEventListener("keydown", function (e) {
                    if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        _importFileInput.click();
                    }
                });
                _importDropzone.addEventListener("dragover", function (e) {
                    e.preventDefault();
                    _importDropzone.classList.add("dragging");
                });
                _importDropzone.addEventListener("dragleave", function () {
                    _importDropzone.classList.remove("dragging");
                });
                _importDropzone.addEventListener("drop", function (e) {
                    e.preventDefault();
                    _importDropzone.classList.remove("dragging");
                    _addImportFiles(e.dataTransfer && e.dataTransfer.files);
                });
                _importFileInput.addEventListener("change", function () {
                    _addImportFiles(_importFileInput.files);
                    _importFileInput.value = ""; // 같은 파일 재선택 허용
                });
            }

            if (_importCancelBtn) {
                _importCancelBtn.addEventListener("click", function () {
                    _importFiles = [];
                    _renderImportQueue();
                    _closeImportModal();
                });
            }
            if (_importStartBtn) {
                _importStartBtn.addEventListener("click", function () {
                    _closeImportModal();
                    showInfoModal(
                        "가져오기",
                        "업로드 API 는 아직 준비 중입니다.\n" +
                        "현재는 ~/.meeting-transcriber/audio_input 폴더에 파일을 직접 복사해 주세요. " +
                        "파이프라인이 자동으로 감지해 처리합니다."
                    );
                    _importFiles = [];
                    _renderImportQueue();
                });
            }
            // importModal 배경 클릭으로 닫기 + ESC 로 닫기 (infoModal 과 동일 패턴)
            if (_importModalEl) {
                _importModalEl.addEventListener("click", function (e) {
                    if (e.target === _importModalEl) _closeImportModal();
                });
                document.addEventListener("keydown", function (e) {
                    if (e.key === "Escape" && !_importModalEl.classList.contains("hidden")) {
                        _closeImportModal();
                    }
                });
            }

            // 녹음 HUD 의 즉시 정지 버튼: 클릭 시 /api/recording/stop 호출.
            // POST 성공 시 pill 을 즉시 숨김 — WebSocket 이 끊긴 상태에서도 UI 가 멈추지 않도록.
            // 백엔드의 ws:recording_stopped 가 뒤이어 도착해도 기존 핸들러의 동작(hide+ticker stop)은 멱등.
            var _recStopBtn = document.getElementById("recordingStopBtn");
            if (_recStopBtn) {
                _recStopBtn.addEventListener("click", async function () {
                    if (_recStopBtn.disabled) return;
                    _recStopBtn.disabled = true;
                    try {
                        await App.apiRequest("/recording/stop", { method: "POST" });
                        var recStatus = document.getElementById("recordingStatus");
                        if (recStatus) recStatus.classList.remove("visible");
                        _stopRecTicker();
                    } catch (err) {
                        errorBanner.show("녹음 정지 실패: " + (err.message || "알 수 없는 오류"));
                        _recStopBtn.disabled = false;
                    }
                });
            }

            // 초기 데이터 로드
            loadMeetings();
            fetchStatus();

            // 새로고침/재방문 시 진행 중인 녹음 상태 복원
            // (백엔드는 녹음을 계속하지만 ws:recording_started 이벤트는 다시 안 오므로
            //  프론트 플로팅 바가 사라지는 UX 결함을 방지)
            App.apiRequest("/recording/status")
                .then(function (rec) {
                    if (rec && rec.is_recording) {
                        // 복원 시엔 최소화(pill) 모드로 표시해 작업 중이던 뷰를 가리지 않는다.
                        // 사용자가 pill 의 확장 버튼을 눌러 overlay 로 볼 수 있음.
                        _showRecHUD("pill");
                        _startRecTicker(rec.duration_seconds || 0);
                        // 새로고침으로 새 DOM 이 로드된 상태라 버튼은 기본 활성이지만,
                        // 브라우저 form state 자동 복원에 대비해 명시적으로 리셋.
                        var recStopBtn = document.getElementById("recordingStopBtn");
                        if (recStopBtn) recStopBtn.disabled = false;
                        var overlayStop = document.getElementById("recordingOverlayStopBtn");
                        if (overlayStop) overlayStop.disabled = false;
                        var overlayCancel = document.getElementById("recordingOverlayCancelBtn");
                        if (overlayCancel) overlayCancel.disabled = false;
                    }
                })
                .catch(function () {
                    // recorder 미초기화 등은 무시
                });

            // 주기적 폴링 (WebSocket 폴백)
            _statusTimer = setInterval(fetchStatus, STATUS_POLL_INTERVAL);
            _meetingsTimer = setInterval(loadMeetings, MEETINGS_POLL_INTERVAL);
        }

        /**
         * 시스템 상태를 폴링한다.
         */
        async function fetchStatus() {
            try {
                var data = await App.apiRequest("/status");
                _statusDot.className = "status-dot connected";
                var activeCount = data.active_jobs || 0;
                if (activeCount > 0) {
                    App.safeText(_statusText, "처리 중 " + activeCount + "건");
                } else {
                    App.safeText(_statusText, "대기 중");
                }
            } catch (e) {
                _statusDot.className = "status-dot error";
                App.safeText(_statusText, "서버 미연결");
            }
        }

        /**
         * 회의 목록을 API에서 가져와 렌더링한다.
         *
         * 초기 로딩 (목록이 비어있을 때) 만 스켈레톤 카드 4 개를 표시한다.
         * 폴링 (이미 데이터가 렌더링된 상태) 에서는 깜빡임 방지를 위해
         * 스켈레톤을 표시하지 않는다. mockup §3.3 표 (회의 목록 = 카드형 × 4).
         * render() 진입 시 _listEl.innerHTML="" 으로 자동 cleanup.
         */
        async function loadMeetings() {
            // 최초 로딩 시점 (목록 비어있고 _meetings 도 비어있음) 에만 스켈레톤 노출
            if (_listEl && _meetings.length === 0 && _listEl.children.length === 0) {
                _listEl.appendChild(App.createSkeletonCards(4));
            }
            try {
                var data = await App.apiRequest("/meetings");
                _meetings = data.meetings || [];
                _applyFilterAndSort();
            } catch (e) {
                // 조용히 처리 (리스트 로드 실패는 치명적이지 않음)
                // 실패 시 스켈레톤이 남아있을 수 있으므로 정리
                if (_listEl) {
                    var skeletons = _listEl.querySelectorAll(".skeleton-card");
                    if (skeletons.length > 0) {
                        _listEl.innerHTML = "";
                    }
                }
            }
        }

        /**
         * 현재 검색어와 정렬 기준으로 필터링 및 정렬 후 렌더링한다.
         */
        function _applyFilterAndSort() {
            var query = _searchEl ? _searchEl.value.trim() : "";
            var filtered = _meetings;

            // 검색 필터
            if (query) {
                var lower = query.toLowerCase();
                filtered = _meetings.filter(function (m) {
                    var idMatch = (m.meeting_id || "").toLowerCase().indexOf(lower) >= 0;
                    var summaryMatch = (m.summary_preview || "").toLowerCase().indexOf(lower) >= 0;
                    return idMatch || summaryMatch;
                });
            }

            // 정렬
            var sortBy = _sortEl ? _sortEl.value : "newest";
            filtered = _sortMeetings(filtered, sortBy);

            // 카운트 업데이트
            if (_countEl) {
                App.safeText(_countEl, filtered.length + "/" + _meetings.length);
            }

            // Progressive Disclosure: 회의가 하나도 없을 때 검색/정렬/카운트 UI 숨김
            // (§5.3 Progressive Onboarding — 빈 상태에서 사용자를 압도하지 않음)
            var listPanelEl = document.getElementById("list-panel");
            if (listPanelEl) {
                listPanelEl.classList.toggle("list-panel-empty", _meetings.length === 0);
            }

            render(filtered);
        }

        /**
         * 회의 목록을 정렬한다.
         * @param {Array} meetings - 회의 목록
         * @param {string} sortBy - 정렬 기준
         * @returns {Array} 정렬된 배열
         */
        function _sortMeetings(meetings, sortBy) {
            var sorted = meetings.slice();

            if (sortBy === "newest") {
                sorted.sort(function (a, b) {
                    return (b.created_at || "").localeCompare(a.created_at || "");
                });
            } else if (sortBy === "oldest") {
                sorted.sort(function (a, b) {
                    return (a.created_at || "").localeCompare(b.created_at || "");
                });
            } else if (sortBy === "status") {
                sorted.sort(function (a, b) {
                    var oa = STATUS_SORT_ORDER[a.status] != null ? STATUS_SORT_ORDER[a.status] : 99;
                    var ob = STATUS_SORT_ORDER[b.status] != null ? STATUS_SORT_ORDER[b.status] : 99;
                    if (oa !== ob) return oa - ob;
                    return (b.created_at || "").localeCompare(a.created_at || "");
                });
            }

            return sorted;
        }

        /**
         * meeting_id에서 날짜 기반 제목을 추출한다.
         * 예: "meeting_20260310_193619" → "2026-03-10 19:36"
         * @param {string} meetingId - 회의 ID
         * @param {string} createdAt - 생성일 (폴백)
         * @returns {string} 날짜 기반 제목
         */
        function _extractTitle(meetingId, createdAt) {
            // meeting_YYYYMMDD_HHMMSS 패턴 매칭
            var match = (meetingId || "").match(/(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/);
            if (match) {
                return match[1] + "-" + match[2] + "-" + match[3] + " " +
                       match[4] + ":" + match[5];
            }
            // 폴백: created_at 날짜 사용
            if (createdAt) {
                return App.formatDate(createdAt);
            }
            return meetingId || "-";
        }

        /**
         * 회의 목록을 렌더링한다.
         * @param {Array} meetings - 회의 목록
         */
        function render(meetings) {
            _listEl.innerHTML = "";

            if (meetings.length === 0) {
                // 빈 상태 (mockup §5.1) — fixture 의 마크업 인터페이스와 일치
                var empty = document.createElement("div");
                empty.className = "empty-state-container";
                empty.setAttribute("data-empty", "meeting-list");
                empty.innerHTML =
                    '<div class="empty-state" role="status" aria-live="polite">' +
                    '  <svg class="empty-state-icon" width="48" height="48" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
                    '    <circle cx="24" cy="24" r="20"/>' +
                    '    <line x1="14" y1="20" x2="14" y2="28"/>' +
                    '    <line x1="19" y1="16" x2="19" y2="32"/>' +
                    '    <line x1="24" y1="14" x2="24" y2="34"/>' +
                    '    <line x1="29" y1="16" x2="29" y2="32"/>' +
                    '    <line x1="34" y1="20" x2="34" y2="28"/>' +
                    '  </svg>' +
                    '  <h2 class="empty-state-title">아직 회의가 없어요</h2>' +
                    '  <p class="empty-state-description">첫 회의를 녹음하면 자동으로 전사·요약돼요.</p>' +
                    '  <button class="empty-state-cta" type="button" data-action="start-recording">녹음 시작</button>' +
                    '</div>';
                _listEl.appendChild(empty);
                // CTA → /api/recording/start 호출 (메뉴바 _on_start_recording 과 동일 엔드포인트)
                var cta = empty.querySelector('[data-action="start-recording"]');
                if (cta) {
                    cta.addEventListener("click", function () {
                        App.apiRequest("/recording/start", { method: "POST" }).catch(function (err) {
                            console.error("녹음 시작 실패:", err);
                        });
                    });
                }
                return;
            }

            meetings.forEach(function (meeting) {
                var item = document.createElement("div");
                item.className = "meeting-item";
                item.setAttribute("data-meeting-id", meeting.meeting_id);
                item.setAttribute("role", "option");
                item.setAttribute("tabindex", "0");
                item.setAttribute("aria-label",
                    _extractTitle(meeting.meeting_id, meeting.created_at) +
                    " — " + App.getStatusLabel(meeting.status));
                if (meeting.meeting_id === _activeId) {
                    item.classList.add("active");
                    item.setAttribute("aria-selected", "true");
                } else {
                    item.setAttribute("aria-selected", "false");
                }

                // 처리 중인 항목: pulse 애니메이션
                var isProcessing = (
                    meeting.status !== "completed" &&
                    meeting.status !== "failed" &&
                    meeting.status !== "recorded" &&
                    meeting.status !== "queued"
                );
                if (isProcessing) {
                    item.classList.add("processing");
                }

                // 상태 도트
                var statusDot = document.createElement("span");
                statusDot.className = "meeting-item-dot";
                if (meeting.status === "completed") {
                    statusDot.classList.add("completed");
                } else if (meeting.status === "failed") {
                    statusDot.classList.add("failed");
                } else if (isProcessing) {
                    statusDot.classList.add("processing");
                } else if (meeting.status === "recorded") {
                    statusDot.classList.add("recorded");
                } else {
                    statusDot.classList.add("queued");
                }

                // 텍스트 컨테이너
                var textContainer = document.createElement("div");
                textContainer.className = "meeting-item-text";

                // 제목: 사용자 정의 title 우선, 없으면 날짜 기반 폴백
                var titleEl = document.createElement("div");
                titleEl.className = "meeting-item-title";
                titleEl.textContent = App.extractMeetingTitle(meeting);

                // 요약 프리뷰 1줄 — summary 가 있으면 우선, 없으면 상태 라벨
                // 전체 요약을 native tooltip 으로 노출해 한 줄 잘림 보완.
                var previewEl = document.createElement("div");
                previewEl.className = "meeting-item-preview";
                if (meeting.summary_preview) {
                    previewEl.textContent = meeting.summary_preview;
                    item.setAttribute("title", meeting.summary_preview);
                } else {
                    previewEl.textContent = App.getStatusLabel(meeting.status);
                }

                textContainer.appendChild(titleEl);
                textContainer.appendChild(previewEl);

                item.appendChild(statusDot);
                item.appendChild(textContainer);

                // 클릭 → ViewerView로 이동
                item.addEventListener("click", function () {
                    Router.navigate("/app/viewer/" + encodeURIComponent(meeting.meeting_id));
                });

                // 키보드 접근성 (Enter/Space)
                item.addEventListener("keydown", function (e) {
                    if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        item.click();
                    }
                });

                _listEl.appendChild(item);
            });
        }

        /**
         * 활성 항목을 설정한다 (하이라이트).
         * @param {string} meetingId - 활성화할 회의 ID (null이면 해제)
         */
        function setActive(meetingId) {
            _activeId = meetingId;
            var items = _listEl.querySelectorAll(".meeting-item");
            items.forEach(function (item) {
                var itemId = item.getAttribute("data-meeting-id");
                if (itemId === meetingId) {
                    item.classList.add("active");
                    item.setAttribute("aria-selected", "true");
                } else {
                    item.classList.remove("active");
                    item.setAttribute("aria-selected", "false");
                }
            });
        }

        /**
         * URL 경로에서 활성 회의를 추출하여 설정한다.
         * @param {string} path - URL 경로
         */
        function setActiveFromPath(path) {
            var match = path.match(/^\/app\/viewer\/(.+)$/);
            if (match) {
                setActive(decodeURIComponent(match[1]));
            } else {
                setActive(null);
            }
        }

        /**
         * 현재 회의 목록 데이터를 반환한다.
         * @returns {Array}
         */
        function getMeetings() {
            return _meetings;
        }

        /**
         * 리스트 패널 타이머를 정리한다.
         */
        function destroy() {
            if (_statusTimer) { clearInterval(_statusTimer); _statusTimer = null; }
            if (_meetingsTimer) { clearInterval(_meetingsTimer); _meetingsTimer = null; }
            if (_searchTimeout) { clearTimeout(_searchTimeout); _searchTimeout = null; }
        }

        return {
            init: init,
            loadMeetings: loadMeetings,
            setActive: setActive,
            setActiveFromPath: setActiveFromPath,
            getMeetings: getMeetings,
            destroy: destroy,
        };
    })();


    // =================================================================
    // === EmptyView (회의 미선택 초기 상태) ===
    // =================================================================

    /**
     * 회의 목록에서 아무것도 선택하지 않은 초기 상태.
     * 시스템 리소스 모니터와 일괄 요약 기능을 포함한다.
     * @constructor
     */
    function EmptyView() {
        // 리소스 모니터는 GlobalResourceBar 가 모든 탭에서 표시하므로 EmptyView 자체에서는 렌더하지 않음.
        this._render();
    }

    /**
     * EmptyView DOM을 생성한다.
     * 리소스 모니터 + 일괄 요약 버튼 + 안내 메시지를 표시한다.
     */
    EmptyView.prototype._render = function () {
        var contentEl = Router.getContentEl();
        contentEl.innerHTML = "";

        var html = [
            // 메인 안내 영역
            '<div class="empty-view">',
            '  <div class="empty-view-icon">' + Icons.clipboard + '</div>',
            '  <h2 class="empty-view-title">회의를 선택하세요</h2>',
            '  <p class="empty-view-desc">왼쪽 목록에서 회의를 선택하면 전사 내용을 볼 수 있습니다.</p>',
            '  <div style="margin-top:16px;">',
            '    <button class="batch-summarize-btn" id="batch-summarize-btn">일괄 요약 생성</button>',
            '  </div>',
            '  <div class="empty-view-shortcuts">',
            '    <div class="empty-view-shortcut">\u2318K 검색</div>',
            '  </div>',
            '</div>',
        ].join("\n");

        contentEl.innerHTML = html;
        document.title = "회의록 · Recap";

        // 일괄 요약 버튼 이벤트
        var self = this;
        var batchBtn = document.getElementById("batch-summarize-btn");
        if (batchBtn) {
            batchBtn.addEventListener("click", function () {
                self._batchSummarize(batchBtn);
            });
        }
    };

    /**
     * 일괄 요약 생성을 실행한다.
     * POST /api/meetings/summarize-batch
     * @param {HTMLButtonElement} btn - 버튼 요소 (비활성화용)
     */
    EmptyView.prototype._batchSummarize = function (btn) {
        btn.disabled = true;
        btn.textContent = "요약 실행 중...";
        App.apiPost("/meetings/summarize-batch", {})
            .then(function (data) {
                var total = data.total || 0;
                btn.textContent = total > 0
                    ? total + "건 요약 시작됨"
                    : "요약 대상 없음";
                setTimeout(function () {
                    btn.disabled = false;
                    btn.textContent = "일괄 요약 생성";
                }, 3000);
            })
            .catch(function () {
                btn.textContent = "요약 실패";
                setTimeout(function () {
                    btn.disabled = false;
                    btn.textContent = "일괄 요약 생성";
                }, 3000);
            });
    };

    /**
     * 뷰를 정리한다. (리소스 모니터는 GlobalResourceBar 가 관리)
     */
    EmptyView.prototype.destroy = function () {};


    // =================================================================
    // === SearchView (검색 전용 뷰, /app/search) ===
    // =================================================================

    /**
     * 검색 뷰: 검색 폼, 필터, 검색 결과 목록.
     * @constructor
     */
    function SearchView() {
        var self = this;
        self._listeners = [];
        self._timers = [];
        self._els = {};

        self._render();
        self._bind();
    }

    /**
     * 검색 뷰 DOM을 생성한다.
     */
    SearchView.prototype._render = function () {
        var contentEl = Router.getContentEl();
        contentEl.innerHTML = "";

        var html = [
            '<div class="search-view">',

            // 검색 헤더
            '  <div class="search-view-header">',
            '    <h2 class="search-view-title">회의 내용 검색</h2>',
            '  </div>',

            // 검색 폼
            '  <form class="search-form" id="searchForm">',
            '    <div class="search-input-row">',
            '      <div class="search-input-wrapper">',
            '        <span class="search-icon"><svg class="icon" width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="6.5" cy="6.5" r="4.5" stroke="currentColor" stroke-width="1.5"/><path d="M10 10l4.5 4.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg></span>',
            '        <input type="text" class="search-input" id="searchQuery"',
            '               placeholder="검색어를 입력하세요 (예: 프로젝트 일정, 결정사항...)"',
            '               aria-label="회의 내용 검색"',
            '               autocomplete="off" />',
            '      </div>',
            '      <button type="submit" class="search-btn" id="searchBtn">검색</button>',
            '    </div>',
            '    <div class="filter-row">',
            '      <div class="filter-group">',
            '        <span class="filter-label">날짜</span>',
            '        <input type="date" class="filter-input" id="searchFilterDate" />',
            '      </div>',
            '      <div class="filter-group">',
            '        <span class="filter-label">화자</span>',
            '        <input type="text" class="filter-input" id="searchFilterSpeaker" placeholder="예: SPEAKER_00" />',
            '      </div>',
            '      <button type="button" class="filter-clear-btn" id="searchFilterClearBtn" aria-label="검색 필터 초기화">',
            '        필터 초기화',
            '      </button>',
            '    </div>',
            '  </form>',

            // 검색 결과
            '  <section class="search-results" id="searchResults">',
            '    <div class="search-results-header">',
            '      <div>',
            '        <span class="search-stats" id="searchStats"></span>',
            '      </div>',
            '    </div>',
            '    <div id="searchResultsList"></div>',
            // 스켈레톤 로딩 (mockup §3.3 — 카드형 × 3, sr-only 텍스트 "검색 중…")
            // 기존 id="searchLoading" 보존 → els.searchLoading.style.display 로직 유지.
            '    <div class="skeleton-container" id="searchLoading" role="status" aria-live="polite" style="display:none;">',
            '      <span class="sr-only">검색 중…</span>',
            '      <div class="skeleton-card" aria-hidden="true">',
            '        <div class="skeleton-line short"></div>',
            '        <div class="skeleton-line medium"></div>',
            '        <div class="skeleton-line"></div>',
            '      </div>',
            '      <div class="skeleton-card" aria-hidden="true">',
            '        <div class="skeleton-line short"></div>',
            '        <div class="skeleton-line medium"></div>',
            '        <div class="skeleton-line"></div>',
            '      </div>',
            '      <div class="skeleton-card" aria-hidden="true">',
            '        <div class="skeleton-line short"></div>',
            '        <div class="skeleton-line medium"></div>',
            '        <div class="skeleton-line"></div>',
            '      </div>',
            '    </div>',
            // 검색 빈 상태 (mockup §5.2) — fixture 마크업 인터페이스와 일치
            '    <div class="empty-state-container" id="searchEmpty" data-empty="search" style="display:none;">',
            '      <div class="empty-state" role="status" aria-live="polite">',
            '        <svg class="empty-state-icon" width="48" height="48" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">',
            '          <circle cx="20" cy="20" r="12"/>',
            '          <line x1="29" y1="29" x2="40" y2="40"/>',
            '        </svg>',
            '        <h2 class="empty-state-title" id="searchEmptyText">검색 결과가 없어요</h2>',
            '        <p class="empty-state-description" id="searchEmptySub">다른 키워드로 다시 검색해 보세요. 띄어쓰기·맞춤법을 한번 더 확인해 주세요.</p>',
            '      </div>',
            '    </div>',
            '  </section>',

            // 검색 안내 (결과 없을 때 기본 표시)
            '  <div class="search-view-hint" id="searchHint">',
            '    <p>회의 전사 내용을 벡터 검색 + 키워드 검색으로 찾습니다.</p>',
            '    <p>검색어를 입력하고 Enter 또는 검색 버튼을 누르세요.</p>',
            '  </div>',

            '</div>',
        ].join("\n");

        contentEl.innerHTML = html;

        // DOM 참조 캐싱
        this._els = {
            searchForm: document.getElementById("searchForm"),
            searchQuery: document.getElementById("searchQuery"),
            searchBtn: document.getElementById("searchBtn"),
            filterDate: document.getElementById("searchFilterDate"),
            filterSpeaker: document.getElementById("searchFilterSpeaker"),
            filterClearBtn: document.getElementById("searchFilterClearBtn"),
            searchResults: document.getElementById("searchResults"),
            searchResultsList: document.getElementById("searchResultsList"),
            searchStats: document.getElementById("searchStats"),
            searchLoading: document.getElementById("searchLoading"),
            searchEmpty: document.getElementById("searchEmpty"),
            searchHint: document.getElementById("searchHint"),
        };

        document.title = "검색 · Recap";
    };

    /**
     * 이벤트 리스너를 바인딩한다.
     */
    SearchView.prototype._bind = function () {
        var self = this;
        var els = self._els;

        // 검색 폼 제출
        var onSubmit = function (e) {
            e.preventDefault();
            self._performSearch();
        };
        els.searchForm.addEventListener("submit", onSubmit);
        self._listeners.push({ el: els.searchForm, type: "submit", fn: onSubmit });

        // 필터 초기화
        var onFilterClear = function () {
            els.filterDate.value = "";
            els.filterSpeaker.value = "";
        };
        els.filterClearBtn.addEventListener("click", onFilterClear);
        self._listeners.push({ el: els.filterClearBtn, type: "click", fn: onFilterClear });

        // 입력 필드에 포커스
        els.searchQuery.focus();
    };

    /**
     * 검색을 수행한다.
     */
    SearchView.prototype._performSearch = async function () {
        var self = this;
        var els = self._els;
        var query = els.searchQuery.value.trim();
        if (!query) return;

        els.searchResults.classList.add("visible");
        els.searchLoading.classList.add("visible");
        els.searchResultsList.innerHTML = "";
        els.searchEmpty.style.display = "none";
        els.searchHint.style.display = "none";
        els.searchBtn.disabled = true;

        try {
            var body = { query: query };
            var dateVal = els.filterDate.value;
            if (dateVal) body.date_filter = dateVal;
            var speakerVal = els.filterSpeaker.value.trim();
            if (speakerVal) body.speaker_filter = speakerVal;

            var data = await App.apiPost("/search", body);
            self._renderSearchResults(data);
        } catch (e) {
            if (e.status === 503) {
                errorBanner.show("검색 엔진이 아직 초기화되지 않았습니다.");
            } else {
                errorBanner.show("검색 실패: " + e.message);
            }
            els.searchResults.classList.remove("visible");
            els.searchHint.style.display = "block";
        } finally {
            els.searchLoading.classList.remove("visible");
            els.searchBtn.disabled = false;
        }
    };

    /**
     * 검색 결과를 렌더링한다.
     * @param {Object} data - SearchResponse
     */
    SearchView.prototype._renderSearchResults = function (data) {
        var self = this;
        var els = self._els;
        var results = data.results || [];
        els.searchResultsList.innerHTML = "";

        // 통계
        App.safeText(
            els.searchStats,
            results.length + "건 검색됨 (벡터: " +
                (data.vector_count || 0) + ", FTS: " +
                (data.fts_count || 0) + ")"
        );

        if (results.length === 0) {
            var emptyText = document.getElementById("searchEmptyText");
            var emptySub = document.getElementById("searchEmptySub");
            var query = els.searchQuery.value.trim();

            App.safeText(emptyText, "'" + query + "'에 대한 검색 결과가 없습니다");

            var hasFilter = els.filterDate.value || els.filterSpeaker.value.trim();
            if (hasFilter) {
                App.safeText(emptySub,
                    "날짜/화자 필터를 해제하거나 다른 검색어를 시도해 보세요");
            } else {
                App.safeText(emptySub,
                    "다른 키워드를 사용하거나, 채팅에서 자연어로 질문해 보세요");
            }

            els.searchEmpty.style.display = "block";
            return;
        }

        els.searchEmpty.style.display = "none";

        results.forEach(function (item) {
            var el = document.createElement("div");
            el.className = "result-item";
            el.setAttribute("role", "button");
            el.setAttribute("tabindex", "0");
            el.setAttribute("aria-label",
                item.meeting_id + " 검색 결과 — " +
                App.formatTime(item.start_time) + " ~ " +
                App.formatTime(item.end_time));

            // 헤더: 회의 ID + 점수
            var header = document.createElement("div");
            header.className = "result-header";

            var meetingId = document.createElement("span");
            meetingId.className = "result-meeting-id";
            meetingId.textContent = item.meeting_id;

            var score = document.createElement("span");
            score.className = "result-score";
            score.textContent = "점수 " + item.score.toFixed(4);

            header.appendChild(meetingId);
            header.appendChild(score);

            // 본문 텍스트
            var text = document.createElement("div");
            text.className = "result-text";
            text.textContent = item.text;

            // 메타 정보
            var meta = document.createElement("div");
            meta.className = "result-meta";

            var dateItem = document.createElement("span");
            dateItem.className = "result-meta-item";
            dateItem.innerHTML = Icons.calendar + ' <span>' + App.escapeHtml(item.date || "-") + '</span>';
            meta.appendChild(dateItem);

            if (item.speakers && item.speakers.length > 0) {
                var speakerItem = document.createElement("span");
                speakerItem.className = "result-meta-item";
                speakerItem.innerHTML = Icons.person + ' <span>' + App.escapeHtml(item.speakers.join(", ")) + '</span>';
                meta.appendChild(speakerItem);
            }

            var timeItem = document.createElement("span");
            timeItem.className = "result-meta-item";
            timeItem.innerHTML = Icons.clock + ' <span>' + App.escapeHtml(App.formatTime(item.start_time) +
                " ~ " + App.formatTime(item.end_time)) + '</span>';
            meta.appendChild(timeItem);

            // 검색 소스 태그
            var sourceTag = document.createElement("span");
            sourceTag.className = "result-source-tag " + (item.source || "both");
            var sourceLabels = {
                vector: "벡터",
                fts: "키워드",
                both: "복합",
            };
            sourceTag.textContent = sourceLabels[item.source] || item.source;
            meta.appendChild(sourceTag);

            el.appendChild(header);
            el.appendChild(text);
            el.appendChild(meta);

            // 클릭 → ViewerView로 이동 (검색어 + 타임스탬프 전달)
            el.addEventListener("click", function () {
                var viewerPath = "/app/viewer/" + encodeURIComponent(item.meeting_id);
                var params = [];
                var q = els.searchQuery.value.trim();
                if (q) params.push("q=" + encodeURIComponent(q));
                if (item.start_time != null) params.push("t=" + encodeURIComponent(item.start_time));
                if (params.length > 0) viewerPath += "?" + params.join("&");
                Router.navigate(viewerPath);
            });

            el.addEventListener("keydown", function (e) {
                if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    el.click();
                }
            });

            els.searchResultsList.appendChild(el);
        });
    };

    /**
     * 뷰를 정리한다.
     */
    SearchView.prototype.destroy = function () {
        this._listeners.forEach(function (entry) {
            entry.el.removeEventListener(entry.type, entry.fn);
        });
        this._listeners = [];
        this._timers.forEach(function (t) { clearInterval(t); clearTimeout(t); });
        this._timers = [];
    };


    // =================================================================
    // === ViewerView (전사문 뷰어) ===
    // =================================================================

    /**
     * 전사문 뷰어 뷰: 회의 정보, 전사 타임라인, 요약 탭.
     * @constructor
     * @param {string} meetingId - 회의 ID
     */
    function ViewerView(meetingId) {
        var self = this;
        self._meetingId = meetingId;
        self._listeners = [];
        self._timers = [];
        self._els = {};

        // 상태 변수
        self._speakerColorMap = {};
        self._allUtterances = [];
        self._currentQuery = "";
        self._currentMatchIndex = -1;
        self._totalMatches = 0;
        self._searchTimeout = null;

        // 발화 음성 재생 상태 (lazy 초기화 — 첫 재생 시 audio element 생성)
        self._audioElement = null;
        self._currentPlayingIdx = -1;   // 재생 중 발화의 _allUtterances 인덱스
        self._currentPlayingEnd = 0;    // 재생 정지 기준 시각 (초)
        self._currentPlayingEl = null;  // .utterance DOM (.playing 토글 대상)
        self._currentPlayingBtn = null; // ▶ 버튼 DOM

        // 실시간 처리 로그 상태 — 단계별 { elapsed, eta, status, anomaly, startedAt }
        // status: "pending" | "running" | "completed" | "failed" | "skipped"
        self._liveLog = {};
        self._liveLogTickTimer = null;

        // URL 쿼리 파라미터에서 검색어, 타임스탬프 추출
        var urlParams = new URLSearchParams(window.location.search);
        self._initialQuery = urlParams.get("q") || "";
        self._initialTimestamp = parseFloat(urlParams.get("t") || "");

        self._render();
        self._bind();
        self._loadData();
    }

    /**
     * 뷰어 뷰 DOM을 생성한다.
     */
    ViewerView.prototype._render = function () {
        var contentEl = Router.getContentEl();
        contentEl.innerHTML = "";

        var html = [
            // 회의 정보 헤더
            '<div class="viewer-header" id="viewerMeetingInfo" style="display:none;">',
            '  <div class="viewer-header-top">',
            '    <h2 class="viewer-title" id="viewerMeetingTitle"></h2>',
            '    <span class="viewer-status" id="viewerMeetingStatus"></span>',
            '    <button type="button" class="density-toggle" id="viewerDensityToggle"',
            '            aria-label="타임라인 밀도 전환" title="밀도 전환 (조밀/편안)"',
            '            aria-pressed="false">',
            '      <svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">',
            '        <line x1="3" y1="5" x2="17" y2="5"></line>',
            '        <line x1="3" y1="10" x2="17" y2="10"></line>',
            '        <line x1="3" y1="15" x2="17" y2="15"></line>',
            '      </svg>',
            '    </button>',
            '  </div>',
            '  <div class="viewer-meta">',
            '    <span class="viewer-meta-item" id="viewerMetaFile"></span>',
            '    <span class="viewer-meta-item" id="viewerMetaDate"></span>',
            '    <span class="viewer-meta-item" id="viewerMetaSpeakers"></span>',
            '    <span class="viewer-meta-item" id="viewerMetaUtterances"></span>',
            '  </div>',
            '  <div class="speaker-legend" id="viewerSpeakerLegend"></div>',
            '  <div class="viewer-actions" id="viewerActions"></div>',
            '  <details class="viewer-log-panel" id="viewerLogPanel" style="display:none;">',
            '    <summary>처리 로그 <span class="log-total" id="viewerLogTotal"></span></summary>',
            '    <div class="log-table" id="viewerLogTable"></div>',
            '  </details>',
            '</div>',

            // 탭 네비게이션
            '<div class="tabs" id="viewerTabNav" style="display:none;" role="tablist" aria-label="회의 내용 탭">',
            '  <button class="tab-btn active" data-tab="transcript"',
            '          role="tab" id="viewerTabTranscript"',
            '          aria-selected="true"',
            '          aria-controls="viewerPanelTranscript">전사문</button>',
            '  <button class="tab-btn" data-tab="summary"',
            '          role="tab" id="viewerTabSummary"',
            '          aria-selected="false"',
            '          aria-controls="viewerPanelSummary">요약</button>',
            '</div>',

            // 전사문 탭 패널
            '<div class="tab-panel active" id="viewerPanelTranscript"',
            '     role="tabpanel" aria-labelledby="viewerTabTranscript">',

            '  <div class="search-bar" id="viewerSearchBar" style="display:none;">',
            '    <input type="text" class="search-bar-input" id="viewerSearchInput"',
            '           placeholder="전사문 내 검색... (Ctrl+F)"',
            '           autocomplete="off" aria-label="전사문 내 검색" />',
            '    <span class="search-bar-info" id="viewerSearchInfo" role="status" aria-live="polite"></span>',
            '    <button class="search-bar-clear" id="viewerSearchPrev" aria-label="이전 결과" style="display:none;">&#x25B2;</button>',
            '    <button class="search-bar-clear" id="viewerSearchNext" aria-label="다음 결과" style="display:none;">&#x25BC;</button>',
            '    <button class="search-bar-clear" id="viewerSearchClear">초기화</button>',
            '  </div>',

            '  <div class="timeline" id="viewerTimeline"></div>',

            // 스켈레톤 로딩 (mockup §3.3 — 라인형 × 5, sr-only 텍스트 "전사 불러오는 중…")
            // 기존 id="viewerTranscriptLoading" 보존 → 기존 show/hide 로직 유지.
            '  <div class="skeleton-container" id="viewerTranscriptLoading" role="status" aria-live="polite" style="display:none;">',
            '    <span class="sr-only">전사 불러오는 중…</span>',
            '    <div data-skeleton="lines" aria-hidden="true">',
            '      <div class="skeleton-line"></div>',
            '      <div class="skeleton-line"></div>',
            '      <div class="skeleton-line medium"></div>',
            '      <div class="skeleton-line"></div>',
            '      <div class="skeleton-line short"></div>',
            '    </div>',
            '  </div>',

            '  <div class="empty-state" id="viewerTranscriptEmpty" style="display:none;">',
            '    <div class="empty-state-icon"><svg class="icon icon-lg" width="48" height="48" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M12 4h16l12 12v28a4 4 0 0 1-4 4H12a4 4 0 0 1-4-4V8a4 4 0 0 1 4-4Z" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M28 4v12h12M16 24h16M16 32h10" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg></div>',
            '    <div class="empty-state-text" id="viewerEmptyText">전사문이 아직 생성되지 않았습니다</div>',
            '    <div class="empty-state-sub" id="viewerEmptySub">',
            '      파이프라인이 처리 중이라면 잠시 기다려 주세요.<br>',
            '      완료되면 전사문이 자동으로 표시됩니다.',
            '    </div>',
            '    <div class="pipeline-progress" id="viewerPipelineProgress" style="display:none;">',
            '      <div class="pipeline-steps" id="viewerPipelineSteps"></div>',
            '      <div class="pipeline-status-text" id="viewerPipelineStatus"></div>',
            '    </div>',
            '  </div>',
            '</div>',

            // 회의록 탭 패널
            '<div class="tab-panel" id="viewerPanelSummary"',
            '     role="tabpanel" aria-labelledby="viewerTabSummary">',
            '  <div class="summary-content" id="viewerSummaryContent"></div>',

            // 스켈레톤 로딩 (mockup §3.3 — 라인형 × 5, sr-only 텍스트 "요약 불러오는 중…")
            // 기존 id="viewerSummaryLoading" 보존 → 기존 show/hide 로직 유지.
            '  <div class="skeleton-container" id="viewerSummaryLoading" role="status" aria-live="polite" style="display:none;">',
            '    <span class="sr-only">요약 불러오는 중…</span>',
            '    <div data-skeleton="lines" aria-hidden="true">',
            '      <div class="skeleton-line"></div>',
            '      <div class="skeleton-line"></div>',
            '      <div class="skeleton-line medium"></div>',
            '      <div class="skeleton-line"></div>',
            '      <div class="skeleton-line short"></div>',
            '    </div>',
            '  </div>',

            '  <div class="empty-state" id="viewerSummaryEmpty" style="display:none;">',
            '    <div class="empty-state-icon"><svg class="icon icon-lg" width="48" height="48" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="10" y="6" width="28" height="36" rx="4" stroke="currentColor" stroke-width="2.5"/><path d="M18 6v-1a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v1M16 18h16M16 26h10" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg></div>',
            '    <div class="empty-state-text">회의록이 아직 생성되지 않았습니다</div>',
            '    <div class="empty-state-sub">',
            '      전사가 완료된 후 아래 버튼을 눌러 요약을 생성할 수 있습니다.',
            '    </div>',
            '    <button class="btn-summarize" id="viewerSummarizeBtn" style="display:none;">',
            '      ' + Icons.doc + ' 요약 생성',
            '    </button>',
            '  </div>',
            '</div>',
        ].join("\n");

        contentEl.innerHTML = html;

        // DOM 참조 캐싱
        this._els = {
            meetingInfo: document.getElementById("viewerMeetingInfo"),
            meetingTitle: document.getElementById("viewerMeetingTitle"),
            meetingStatus: document.getElementById("viewerMeetingStatus"),
            metaFile: document.getElementById("viewerMetaFile"),
            metaDate: document.getElementById("viewerMetaDate"),
            metaSpeakers: document.getElementById("viewerMetaSpeakers"),
            metaUtterances: document.getElementById("viewerMetaUtterances"),
            speakerLegend: document.getElementById("viewerSpeakerLegend"),
            viewerActions: document.getElementById("viewerActions"),
            logPanel: document.getElementById("viewerLogPanel"),
            logTotal: document.getElementById("viewerLogTotal"),
            logTable: document.getElementById("viewerLogTable"),
            tabNav: document.getElementById("viewerTabNav"),
            timeline: document.getElementById("viewerTimeline"),
            transcriptLoading: document.getElementById("viewerTranscriptLoading"),
            transcriptEmpty: document.getElementById("viewerTranscriptEmpty"),
            pipelineProgress: document.getElementById("viewerPipelineProgress"),
            pipelineSteps: document.getElementById("viewerPipelineSteps"),
            pipelineStatus: document.getElementById("viewerPipelineStatus"),
            summaryContent: document.getElementById("viewerSummaryContent"),
            summaryLoading: document.getElementById("viewerSummaryLoading"),
            summaryEmpty: document.getElementById("viewerSummaryEmpty"),
            searchBar: document.getElementById("viewerSearchBar"),
            searchInput: document.getElementById("viewerSearchInput"),
            searchInfo: document.getElementById("viewerSearchInfo"),
            searchClear: document.getElementById("viewerSearchClear"),
            searchPrev: document.getElementById("viewerSearchPrev"),
            searchNext: document.getElementById("viewerSearchNext"),
            summarizeBtn: document.getElementById("viewerSummarizeBtn"),
            densityToggle: document.getElementById("viewerDensityToggle"),
        };

        // Density 토글 — localStorage 에 'viewer-density' 키로 저장, 기본은 Comfortable.
        // 저장값이 'compact' 면 .timeline 에 .density-compact 클래스를 붙여 레퍼런스
        // Viewer.jsx 수치(26px 배지 / 13px 본문 / lh 1.6) 를 적용한다.
        // IIFE 는 자기 함수 스코프라 외부 this 접근 불가 → 로컬 변수로 캡처.
        var els = this._els;
        (function initDensityToggle() {
            var btn = els.densityToggle;
            var tl = els.timeline;
            if (!btn || !tl) return;
            var saved = (function () {
                try { return localStorage.getItem("viewer-density") || "comfortable"; }
                catch (e) { return "comfortable"; }
            })();
            function apply(mode) {
                var compact = mode === "compact";
                tl.classList.toggle("density-compact", compact);
                btn.setAttribute("aria-pressed", compact ? "true" : "false");
                btn.title = compact ? "밀도: 조밀 (클릭 → 편안)" : "밀도: 편안 (클릭 → 조밀)";
            }
            apply(saved);
            btn.addEventListener("click", function () {
                var now = tl.classList.contains("density-compact") ? "comfortable" : "compact";
                try { localStorage.setItem("viewer-density", now); } catch (e) { /* private mode */ }
                apply(now);
            });
        })();

        // 페이지 타이틀 업데이트
        document.title = this._meetingId + " · 전사문 · Recap";
    };

    /**
     * 이벤트 리스너를 바인딩한다.
     */
    ViewerView.prototype._bind = function () {
        var self = this;
        var els = self._els;

        // 탭 전환
        var tabBtns = document.querySelectorAll("#viewerTabNav .tab-btn");
        tabBtns.forEach(function (btn) {
            var onTabClick = function () {
                // 모든 탭 비활성화
                tabBtns.forEach(function (b) {
                    b.classList.remove("active");
                    b.setAttribute("aria-selected", "false");
                });
                btn.classList.add("active");
                btn.setAttribute("aria-selected", "true");

                // 모든 패널 비활성화
                var panels = Router.getContentEl().querySelectorAll(".tab-panel");
                panels.forEach(function (panel) {
                    panel.classList.remove("active");
                });

                var tabName = btn.getAttribute("data-tab");
                if (tabName === "transcript") {
                    document.getElementById("viewerPanelTranscript").classList.add("active");
                } else if (tabName === "summary") {
                    document.getElementById("viewerPanelSummary").classList.add("active");
                }
            };
            btn.addEventListener("click", onTabClick);
            self._listeners.push({ el: btn, type: "click", fn: onTabClick });

            // 키보드 탐색: 좌우 화살표 (ARIA 탭 패턴)
            var onTabKeydown = function (e) {
                var tabs = Array.from(tabBtns);
                var idx = tabs.indexOf(btn);
                var nextIdx = -1;

                if (e.key === "ArrowRight") {
                    nextIdx = (idx + 1) % tabs.length;
                } else if (e.key === "ArrowLeft") {
                    nextIdx = (idx - 1 + tabs.length) % tabs.length;
                }

                if (nextIdx >= 0) {
                    e.preventDefault();
                    tabs[nextIdx].focus();
                    tabs[nextIdx].click();
                }
            };
            btn.addEventListener("keydown", onTabKeydown);
            self._listeners.push({ el: btn, type: "keydown", fn: onTabKeydown });
        });

        // 인라인 검색 (디바운스 300ms)
        var onSearchInput = function () {
            clearTimeout(self._searchTimeout);
            self._searchTimeout = setTimeout(function () {
                self._currentQuery = els.searchInput.value.trim();
                self._renderTimeline(self._allUtterances, self._currentQuery);
            }, 300);
        };
        els.searchInput.addEventListener("input", onSearchInput);
        self._listeners.push({ el: els.searchInput, type: "input", fn: onSearchInput });

        // 검색 초기화
        var onSearchClear = function () {
            els.searchInput.value = "";
            self._currentQuery = "";
            self._renderTimeline(self._allUtterances, "");
        };
        els.searchClear.addEventListener("click", onSearchClear);
        self._listeners.push({ el: els.searchClear, type: "click", fn: onSearchClear });

        // 이전/다음 검색 결과 네비게이션
        var onSearchPrev = function () {
            self._navigateSearchResult(-1);
        };
        els.searchPrev.addEventListener("click", onSearchPrev);
        self._listeners.push({ el: els.searchPrev, type: "click", fn: onSearchPrev });

        var onSearchNext = function () {
            self._navigateSearchResult(1);
        };
        els.searchNext.addEventListener("click", onSearchNext);
        self._listeners.push({ el: els.searchNext, type: "click", fn: onSearchNext });

        // Ctrl+F / Cmd+F → 인라인 검색창 포커스
        var onKeydown = function (e) {
            if ((e.ctrlKey || e.metaKey) && e.key === "f") {
                if (els.searchBar.style.display !== "none") {
                    e.preventDefault();
                    els.searchInput.focus();
                    els.searchInput.select();
                }
            }
            // ESC → 검색 초기화
            if (e.key === "Escape" && document.activeElement === els.searchInput) {
                els.searchInput.value = "";
                self._currentQuery = "";
                self._renderTimeline(self._allUtterances, "");
                els.searchInput.blur();
            }
            // n/p 단축키 → 다음/이전 검색 결과 (검색창에 포커스가 없을 때)
            if (document.activeElement !== els.searchInput) {
                if (e.key === "n") {
                    self._navigateSearchResult(1);
                } else if (e.key === "p") {
                    self._navigateSearchResult(-1);
                }
            }
        };
        document.addEventListener("keydown", onKeydown);
        self._listeners.push({ el: document, type: "keydown", fn: onKeydown });

        // WebSocket 이벤트: 파이프라인 완료 시 데이터 자동 갱신
        var onJobCompleted = function () {
            self._loadMeetingInfo();
            self._loadTranscript();
            self._loadSummary();
        };
        document.addEventListener("ws:job_completed", onJobCompleted);
        self._listeners.push({ el: document, type: "ws:job_completed", fn: onJobCompleted });

        var onPipelineStatus = function (e) {
            self._loadMeetingInfo();
            // 요약 완료 시 요약 데이터 갱신
            var detail = e.detail || {};
            if (detail.step === "summarize") {
                self._loadSummary();
            }
        };
        document.addEventListener("ws:pipeline_status", onPipelineStatus);
        self._listeners.push({ el: document, type: "ws:pipeline_status", fn: onPipelineStatus });

        // 단계별 실시간 진행/ETA/이상 탐지 이벤트
        var onStepProgress = function (e) {
            var detail = e.detail || {};
            if (detail.meeting_id && detail.meeting_id !== self._meetingId) return;
            self._handleStepProgress(detail);
        };
        document.addEventListener("ws:step_progress", onStepProgress);
        self._listeners.push({ el: document, type: "ws:step_progress", fn: onStepProgress });

        // 요약 생성 버튼 클릭
        var onSummarize = function () {
            self._requestSummarize();
        };
        els.summarizeBtn.addEventListener("click", onSummarize);
        self._listeners.push({ el: els.summarizeBtn, type: "click", fn: onSummarize });
    };

    /**
     * 온디맨드 요약 생성을 요청한다.
     */
    ViewerView.prototype._requestSummarize = async function (force) {
        var self = this;
        var els = self._els;
        // force=true면 재생성 버튼에서 호출, false면 최초 생성 버튼에서 호출
        var btn = force ? els.summaryContent.querySelector(".btn-regenerate") : els.summarizeBtn;
        if (!btn) btn = els.summarizeBtn;
        var originalText = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = Icons.gear + ' 요약 준비 중...';
        btn.classList.add("loading");

        try {
            var url = "/meetings/" + encodeURIComponent(self._meetingId) + "/summarize";
            if (force) url += "?force=true";
            await App.apiPost(url, {});

            // 2초 간격 폴링: 진행 단계 감지 + 완료 감지 (최대 5분)
            var pollCount = 0;
            var maxPolls = 150;
            var dots = 0;
            var stepLabels = {
                correct: "LLM 보정",
                summarize: "요약 생성",
            };

            var pollTimer = setInterval(async function () {
                pollCount++;
                dots = (dots + 1) % 4;

                if (pollCount > maxPolls) {
                    clearInterval(pollTimer);
                    btn.disabled = false;
                    btn.innerHTML = originalText;
                    btn.classList.remove("loading");
                    errorBanner.show("요약 생성 시간이 초과되었습니다.");
                    return;
                }

                try {
                    // 요약 완료 확인
                    var summary = await App.apiRequest(
                        "/meetings/" + encodeURIComponent(self._meetingId) + "/summary"
                    );
                    if (summary.markdown) {
                        clearInterval(pollTimer);
                        btn.classList.remove("loading");
                        // _renderSummaryView 를 거쳐야 편집/재생성 툴바가 다시 그려지고
                        // _currentSummaryMd / _summaryDirty 가 올바르게 갱신된다.
                        // (이전 버그: innerHTML 직접 덮어쓰기 → 툴바 사라지고 dirty 추적 깨짐)
                        self._currentSummaryMd = summary.markdown;
                        self._summaryDirty = false;
                        els.summaryEmpty.style.display = "none";
                        if (els.summarizeBtn) els.summarizeBtn.style.display = "none";
                        self._renderSummaryView(summary.markdown);
                        // 처리 로그도 갱신 (요약 단계 elapsed 가 추가됨)
                        self._loadPipelineLog();
                        return;
                    }
                } catch (ignore) {
                    // 404 = 아직 생성 중 → 진행 상태 확인
                }

                // 회의 상태에서 현재 단계 확인
                try {
                    var meeting = await App.apiRequest(
                        "/meetings/" + encodeURIComponent(self._meetingId)
                    );
                    var status = meeting.status || "";
                    var label = stepLabels[status] || "처리";
                    btn.innerHTML = Icons.gear + ' ' + label + ' 중' + '.'.repeat(dots + 1);
                } catch (ignore) {
                    btn.innerHTML = Icons.gear + ' 처리 중' + '.'.repeat(dots + 1);
                }
            }, 2000);

            self._timers.push(pollTimer);

        } catch (e) {
            errorBanner.show("요약 요청 실패: " + e.message);
            btn.disabled = false;
            btn.innerHTML = originalText;
            btn.classList.remove("loading");
        }
    };

    /**
     * 초기 데이터를 로드한다.
     */
    ViewerView.prototype._loadData = function () {
        this._loadMeetingInfo();
        this._loadTranscript();
        this._loadSummary();
    };

    /**
     * 화자 목록으로 색상 맵을 생성한다.
     * @param {Array} speakers - 화자 배열
     */
    ViewerView.prototype._buildSpeakerColorMap = function (speakers) {
        this._speakerColorMap = {};
        var self = this;
        speakers.forEach(function (s, i) {
            self._speakerColorMap[s] = App.SPEAKER_COLORS[i % App.SPEAKER_COLORS.length];
        });
    };

    /**
     * 화자 색상을 반환한다.
     * @param {string} speaker - 화자
     * @returns {string} CSS 색상
     */
    ViewerView.prototype._getSpeakerColor = function (speaker) {
        return this._speakerColorMap[speaker] || "var(--text-secondary)";
    };

    /**
     * 화자 범례를 렌더링한다.
     * @param {Array} speakers - 화자 배열
     */
    ViewerView.prototype._renderSpeakerLegend = function (speakers) {
        var self = this;
        var legendEl = self._els.speakerLegend;
        legendEl.innerHTML = "";
        if (!speakers || speakers.length === 0) return;

        // 화자별 번호 매핑
        var speakerNumbers = {};
        var count = 0;
        speakers.forEach(function (s) {
            count++;
            speakerNumbers[s] = count;
        });

        speakers.forEach(function (speaker) {
            var chip = document.createElement("span");
            chip.className = "speaker-chip";

            var badge = document.createElement("span");
            badge.className = "speaker-badge";
            badge.style.backgroundColor = self._getSpeakerColor(speaker);
            badge.textContent = speakerNumbers[speaker] || "?";

            var label = document.createElement("span");
            label.textContent = speaker === "UNKNOWN" ? "참석자 ?" : "참석자 " + (speakerNumbers[speaker] || "?");

            chip.appendChild(badge);
            chip.appendChild(label);
            legendEl.appendChild(chip);
        });
    };

    /**
     * 전사문 발화 목록을 타임라인으로 렌더링한다.
     * @param {Array} utterances - 발화 목록
     * @param {string} query - 검색어 (하이라이팅용)
     */
    ViewerView.prototype._renderTimeline = function (utterances, query) {
        var self = this;
        var els = self._els;
        // 재렌더 직전 재생 상태를 정리 (이전 DOM 참조가 끊어지므로)
        self._stopUtterancePlayback();
        els.timeline.innerHTML = "";
        var matchCount = 0;

        // 화자별 번호 매핑 (SPEAKER_00 → 1, SPEAKER_01 → 2, ...)
        var speakerNumbers = {};
        var speakerCount = 0;

        utterances.forEach(function (u) {
            if (!(u.speaker in speakerNumbers)) {
                speakerCount++;
                speakerNumbers[u.speaker] = speakerCount;
            }
        });

        // 화자 표시명 변환 (SPEAKER_00 → 참석자 1)
        function getSpeakerLabel(speaker) {
            var num = speakerNumbers[speaker];
            if (speaker === "UNKNOWN") return "참석자 ?";
            return "참석자 " + (num || "?");
        }

        utterances.forEach(function (u) {
            var el = document.createElement("div");
            el.className = "utterance";
            var color = self._getSpeakerColor(u.speaker);
            var num = speakerNumbers[u.speaker] || "?";

            // 좌측 번호 배지 (원형)
            var badge = document.createElement("div");
            badge.className = "utterance-badge";
            badge.style.backgroundColor = color;
            badge.textContent = num;

            // 우측 콘텐츠 영역
            var content = document.createElement("div");
            content.className = "utterance-content";

            // 헤더 (화자명 + 타임스탬프)
            var header = document.createElement("div");
            header.className = "utterance-header";

            var speakerEl = document.createElement("span");
            speakerEl.className = "utterance-speaker";
            speakerEl.textContent = getSpeakerLabel(u.speaker);
            speakerEl.style.color = color;

            var timeEl = document.createElement("span");
            timeEl.className = "utterance-time";
            timeEl.textContent = App.formatTime(u.start);

            // ▶ 발화 음성 재생 버튼 (이 발화 시간 구간만 재생)
            var playBtn = document.createElement("button");
            playBtn.type = "button";
            playBtn.className = "utterance-play-btn";
            playBtn.setAttribute("aria-label", "이 발화 음성 재생");
            playBtn.title = "이 발화의 음성 재생 (다시 누르면 정지)";
            // 재생 ▶ / 정지 ■ 둘 다 그려두고 CSS 로 .playing 시 표시 토글
            playBtn.innerHTML =
                '<svg class="play-icon" width="11" height="11" viewBox="0 0 12 12" fill="none" aria-hidden="true">' +
                '<path d="M3 1.5v9l7.5-4.5L3 1.5z" fill="currentColor"/></svg>' +
                '<svg class="stop-icon" width="11" height="11" viewBox="0 0 12 12" fill="none" aria-hidden="true">' +
                '<rect x="2.5" y="2.5" width="7" height="7" rx="1" fill="currentColor"/></svg>';

            header.appendChild(speakerEl);
            header.appendChild(timeEl);
            header.appendChild(playBtn);

            // 텍스트
            var textEl = document.createElement("div");
            textEl.className = "utterance-text";
            textEl.title = "더블클릭하여 편집";

            if (query) {
                var htmlContent = App.highlightText(u.text, query);
                textEl.innerHTML = htmlContent;

                if (u.text.toLowerCase().indexOf(query.toLowerCase()) >= 0) {
                    el.classList.add("highlighted");
                    matchCount++;
                }
            } else {
                textEl.textContent = u.text;
            }

            // 더블클릭 → 인라인 편집 모드
            // (단일 클릭은 텍스트 선택과 충돌하므로 더블클릭 사용)
            var utteranceIndex = utterances.indexOf(u);
            textEl.addEventListener("dblclick", function () {
                self._beginEditUtterance(utteranceIndex, textEl);
            });

            // ▶ 버튼 → 해당 발화 구간만 재생 (toggle)
            (function (idx, container, btn) {
                btn.addEventListener("click", function (e) {
                    e.stopPropagation();
                    self._toggleUtterancePlayback(idx, container, btn);
                });
            })(utteranceIndex, el, playBtn);

            content.appendChild(header);
            content.appendChild(textEl);

            el.appendChild(badge);
            el.appendChild(content);
            els.timeline.appendChild(el);
        });

        // 검색 정보 업데이트
        self._totalMatches = matchCount;
        if (query) {
            if (matchCount > 0) {
                self._currentMatchIndex = 0;
                App.safeText(els.searchInfo, "1 / " + matchCount + "건 일치");
                els.searchPrev.style.display = "inline-block";
                els.searchNext.style.display = "inline-block";
            } else {
                self._currentMatchIndex = -1;
                App.safeText(els.searchInfo, "결과 없음");
                els.searchPrev.style.display = "none";
                els.searchNext.style.display = "none";
            }

            // 첫 번째 하이라이트로 스크롤
            var firstHighlight = els.timeline.querySelector(".highlighted");
            if (firstHighlight) {
                firstHighlight.scrollIntoView({ behavior: "smooth", block: "center" });
            }
        } else {
            self._currentMatchIndex = -1;
            self._totalMatches = 0;
            App.safeText(els.searchInfo, "");
            els.searchPrev.style.display = "none";
            els.searchNext.style.display = "none";
        }
    };

    /**
     * 발화 음성 재생용 audio element 를 lazy 하게 만든다.
     *
     * 한 ViewerView 인스턴스에서 단일 audio 만 사용한다 (여러 발화가
     * 같은 audio 를 공유하고 currentTime 만 옮긴다). 첫 ▶ 클릭에서만
     * 메타데이터 로드가 일어나도록 preload="metadata".
     *
     * @returns {HTMLAudioElement}
     */
    ViewerView.prototype._ensureAudioPlayer = function () {
        var self = this;
        if (self._audioElement) return self._audioElement;

        var audio = document.createElement("audio");
        audio.preload = "metadata";
        audio.src = "/api/meetings/" + encodeURIComponent(self._meetingId) + "/audio";
        audio.style.display = "none";

        // 발화 종료 시각에 도달하면 자동 정지 (구간 재생)
        audio.addEventListener("timeupdate", function () {
            if (self._currentPlayingIdx < 0) return;
            if (audio.currentTime >= self._currentPlayingEnd) {
                try { audio.pause(); } catch (e) { /* no-op */ }
                self._stopUtterancePlayback();
            }
        });

        // 파일 끝까지 재생된 경우
        audio.addEventListener("ended", function () {
            self._stopUtterancePlayback();
        });

        // 404·네트워크·코덱 오류 → 사용자 알림
        audio.addEventListener("error", function () {
            // 처음 한 번만 알리고 정지 (각 발화마다 알림 폭탄 방지)
            if (self._currentPlayingIdx >= 0) {
                errorBanner.show(
                    "이 회의의 음성 파일을 재생할 수 없어요 " +
                    "(보존 기간이 지났거나 파일이 손상되었을 수 있어요)."
                );
            }
            self._stopUtterancePlayback();
        });

        document.body.appendChild(audio);
        self._audioElement = audio;
        return audio;
    };

    /**
     * 특정 발화의 음성 구간을 재생/정지 토글한다 (▶ 버튼 핸들러).
     *
     * 같은 발화에서 다시 누르면 정지, 다른 발화를 누르면 그 발화로 점프.
     * Whisper 시작 시각이 살짝 늦은 경우가 잦아 0.2초 백오프하여 seek 한다.
     *
     * @param {number} index - self._allUtterances 인덱스
     * @param {HTMLElement} utteranceEl - .utterance 컨테이너 (.playing 클래스 토글용)
     * @param {HTMLElement} btnEl - ▶ 버튼 (.playing 클래스 토글용)
     */
    ViewerView.prototype._toggleUtterancePlayback = function (index, utteranceEl, btnEl) {
        var self = this;
        if (!self._allUtterances || index < 0 || index >= self._allUtterances.length) return;
        var u = self._allUtterances[index];
        if (typeof u.start !== "number" || typeof u.end !== "number") return;

        var audio = self._ensureAudioPlayer();

        // 같은 발화 재생 중 → 정지 (토글)
        if (self._currentPlayingIdx === index && !audio.paused) {
            try { audio.pause(); } catch (e) { /* no-op */ }
            self._stopUtterancePlayback();
            return;
        }

        // 이전 발화 재생 중이면 정리 후 새 발화로 전환
        self._stopUtterancePlayback();

        self._currentPlayingIdx = index;
        self._currentPlayingEnd = u.end;
        self._currentPlayingEl = utteranceEl;
        self._currentPlayingBtn = btnEl;

        utteranceEl.classList.add("playing");
        btnEl.classList.add("playing");
        btnEl.setAttribute("aria-label", "재생 정지");

        // Whisper 타임스탬프 보정 (시작 시각에서 0.2초 앞으로 백오프)
        var seekTo = Math.max(0, (u.start || 0) - 0.2);

        var startPlayback = function () {
            try {
                audio.currentTime = seekTo;
                var playPromise = audio.play();
                if (playPromise && typeof playPromise.catch === "function") {
                    playPromise.catch(function (err) {
                        self._stopUtterancePlayback();
                        errorBanner.show(
                            "음성 재생에 실패했어요: " + (err && err.message ? err.message : err)
                        );
                    });
                }
            } catch (err) {
                self._stopUtterancePlayback();
            }
        };

        // 메타데이터(duration) 로드 전이면 로드 완료 후 재생 시작
        if (audio.readyState < 1) {
            var onLoaded = function () {
                audio.removeEventListener("loadedmetadata", onLoaded);
                // 사이에 사용자가 다른 버튼을 누르거나 정지했는지 확인
                if (self._currentPlayingIdx === index) startPlayback();
            };
            audio.addEventListener("loadedmetadata", onLoaded);
        } else {
            startPlayback();
        }
    };

    /**
     * 현재 발화 재생 상태를 모두 정리한다 (UI 클래스 + 내부 인덱스).
     * audio 요소 자체는 유지 (다음 재생에 재사용).
     */
    ViewerView.prototype._stopUtterancePlayback = function () {
        var self = this;
        if (self._currentPlayingEl) {
            self._currentPlayingEl.classList.remove("playing");
        }
        if (self._currentPlayingBtn) {
            self._currentPlayingBtn.classList.remove("playing");
            self._currentPlayingBtn.setAttribute("aria-label", "이 발화 음성 재생");
        }
        self._currentPlayingIdx = -1;
        self._currentPlayingEnd = 0;
        self._currentPlayingEl = null;
        self._currentPlayingBtn = null;
    };

    /**
     * 특정 발화를 인라인 편집 모드로 전환한다 (더블클릭 핸들러).
     * @param {number} index - self._allUtterances 배열 내 인덱스
     * @param {HTMLElement} textEl - 현재 텍스트를 보여주는 element
     */
    ViewerView.prototype._beginEditUtterance = function (index, textEl) {
        var self = this;
        if (!self._allUtterances || index < 0 || index >= self._allUtterances.length) return;
        if (textEl.classList.contains("editing")) return;

        var originalText = self._allUtterances[index].text;
        textEl.classList.add("editing");
        textEl.innerHTML = "";

        var textarea = document.createElement("textarea");
        textarea.className = "utterance-textarea";
        textarea.value = originalText;
        textarea.rows = Math.max(2, Math.ceil(originalText.length / 60));
        textEl.appendChild(textarea);

        var actions = document.createElement("div");
        actions.className = "utterance-edit-actions";
        var cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "btn-icon";
        cancelBtn.textContent = "취소";
        var saveBtn = document.createElement("button");
        saveBtn.type = "button";
        saveBtn.className = "btn-icon btn-icon-primary";
        saveBtn.textContent = "저장";
        actions.appendChild(cancelBtn);
        actions.appendChild(saveBtn);
        textEl.appendChild(actions);

        var done = false;
        var restore = function () {
            if (done) return;
            done = true;
            self._renderTimeline(self._allUtterances, self._currentQuery || "");
        };
        cancelBtn.addEventListener("click", restore);
        saveBtn.addEventListener("click", function () {
            if (done) return;
            var next = textarea.value;
            if (next === originalText) {
                restore();
                return;
            }
            done = true;
            self._allUtterances[index] = Object.assign({}, self._allUtterances[index], {
                text: next,
                was_corrected: true,
            });
            self._saveTranscript();
        });
        textarea.addEventListener("keydown", function (e) {
            if (e.key === "Escape") {
                e.preventDefault();
                restore();
            } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                saveBtn.click();
            }
        });

        setTimeout(function () {
            textarea.focus();
            textarea.setSelectionRange(textarea.value.length, textarea.value.length);
        }, 0);
    };

    /**
     * 현재 self._allUtterances 를 서버에 PUT 한다 (인라인 편집 후 또는 bulk replace 후).
     */
    ViewerView.prototype._saveTranscript = async function () {
        var self = this;
        try {
            var payload = {
                utterances: self._allUtterances.map(function (u) {
                    return {
                        text: u.text,
                        original_text: u.original_text || u.text,
                        speaker: u.speaker,
                        start: u.start,
                        end: u.end,
                        was_corrected: !!u.was_corrected,
                    };
                }),
            };
            var resp = await fetch(
                "/api/meetings/" +
                    encodeURIComponent(self._meetingId) +
                    "/transcript",
                {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                }
            );
            if (!resp.ok) {
                var err = await resp.json().catch(function () { return {}; });
                throw new Error(err.detail || "HTTP " + resp.status);
            }
            var data = await resp.json();
            self._allUtterances = data.utterances || [];
            self._renderTimeline(self._allUtterances, self._currentQuery || "");
        } catch (e) {
            errorBanner.show("전사문 저장 실패: " + (e.message || e));
            // 실패 시 서버에서 재로드
            self._loadTranscript();
        }
    };

    /**
     * "모두 바꾸기" 모달을 연다.
     */
    ViewerView.prototype._openReplaceModal = function () {
        var self = this;
        // 기존 모달 제거
        var existing = document.getElementById("transcriptReplaceModal");
        if (existing) existing.remove();

        var overlay = document.createElement("div");
        overlay.id = "transcriptReplaceModal";
        overlay.className = "modal-overlay";
        overlay.innerHTML = [
            '<div class="modal-content">',
            '  <h3 class="modal-title">전사문 모두 바꾸기</h3>',
            '  <div class="modal-field">',
            '    <label class="modal-label" for="replaceFind">찾을 말 (오인식된 표기)</label>',
            '    <input type="text" class="modal-input" id="replaceFind" placeholder="예: 파이선" />',
            '  </div>',
            '  <div class="modal-field">',
            '    <label class="modal-label" for="replaceReplace">바꿀 말 (정답)</label>',
            '    <input type="text" class="modal-input" id="replaceReplace" placeholder="예: FastAPI" />',
            '  </div>',
            '  <div class="modal-field">',
            '    <label class="checkbox-label">',
            '      <input type="checkbox" id="replaceAddVocab" checked />',
            '      <span>용어집에도 추가 (다음부터 보정에 자동 반영)</span>',
            '    </label>',
            '  </div>',
            '  <div class="modal-error" id="replaceError"></div>',
            '  <div class="modal-actions">',
            '    <button type="button" class="btn-secondary" id="replaceCancelBtn">취소</button>',
            '    <button type="button" class="settings-save-btn" id="replaceApplyBtn">적용</button>',
            '  </div>',
            '</div>',
        ].join("");
        document.body.appendChild(overlay);

        var findInput = document.getElementById("replaceFind");
        var replaceInput = document.getElementById("replaceReplace");
        var addVocabCb = document.getElementById("replaceAddVocab");
        var errorEl = document.getElementById("replaceError");
        var applyBtn = document.getElementById("replaceApplyBtn");
        var cancelBtn = document.getElementById("replaceCancelBtn");

        // close 안에서 escHandler 도 함께 해제 — 어떤 경로로 닫혀도 leak 없음
        var escHandler;  // 아래에서 정의 후 close 가 참조
        var close = function () {
            overlay.remove();
            if (escHandler) {
                document.removeEventListener("keydown", escHandler);
                escHandler = null;
            }
        };
        cancelBtn.addEventListener("click", close);
        overlay.addEventListener("click", function (e) {
            if (e.target === overlay) close();
        });
        escHandler = function (e) {
            if (e.key === "Escape") close();
        };
        document.addEventListener("keydown", escHandler);

        // Enter 키 → 적용 (find/replace input 에서)
        var enterHandler = function (e) {
            if (e.isComposing || e.shiftKey) return;
            if (e.key === "Enter") {
                e.preventDefault();
                applyBtn.click();
            }
        };
        findInput.addEventListener("keydown", enterHandler);
        replaceInput.addEventListener("keydown", enterHandler);

        applyBtn.addEventListener("click", async function () {
            var find = findInput.value.trim();
            var replace = replaceInput.value.trim();
            if (!find || !replace) {
                errorEl.textContent = "찾을 말과 바꿀 말을 모두 입력해 주세요.";
                return;
            }
            if (find === replace) {
                errorEl.textContent = "찾을 말과 바꿀 말이 같아요.";
                return;
            }
            errorEl.textContent = "";
            applyBtn.disabled = true;
            applyBtn.textContent = "적용 중…";
            try {
                var resp = await fetch(
                    "/api/meetings/" +
                        encodeURIComponent(self._meetingId) +
                        "/transcript/replace",
                    {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            find: find,
                            replace: replace,
                            add_to_vocabulary: addVocabCb.checked,
                        }),
                    }
                );
                if (!resp.ok) {
                    var err = await resp.json().catch(function () { return {}; });
                    throw new Error(err.detail || "HTTP " + resp.status);
                }
                var data = await resp.json();
                close();  // escHandler 도 close() 가 함께 해제
                // 성공 메시지
                var msg = data.changes + "건을 바꿨어요 (" + data.updated_utterances + "개 발화)";
                if (data.vocabulary_action === "term_created") {
                    msg += " · 용어집에 '" + replace + "' 신규 등록";
                } else if (data.vocabulary_action === "alias_added") {
                    msg += " · '" + replace + "' 용어에 별칭 추가";
                } else if (data.vocabulary_action === "alias_already_exists") {
                    msg += " · 용어집에 이미 등록되어 있음";
                } else if (data.vocabulary_action === "failed") {
                    msg += " · 용어집 등록은 실패했어요";
                }
                // 잠깐 alert 대신 에러배너에 info 성격으로 표시 (toast 구현이 없으면)
                errorBanner.show(msg);
                // 전사문 재로드
                self._loadTranscript();
            } catch (e) {
                errorEl.textContent = e.message || String(e);
                applyBtn.disabled = false;
                applyBtn.textContent = "적용";
            }
        });

        // 포커스
        setTimeout(function () { findInput.focus(); }, 0);
    };

    /**
     * 검색 결과 이전/다음 항목으로 스크롤한다.
     * @param {number} direction - 이동 방향 (-1: 이전, 1: 다음)
     */
    ViewerView.prototype._navigateSearchResult = function (direction) {
        var self = this;
        var els = self._els;
        var highlights = els.timeline.querySelectorAll(".highlighted");
        if (highlights.length === 0) return;

        self._currentMatchIndex += direction;
        if (self._currentMatchIndex < 0) self._currentMatchIndex = highlights.length - 1;
        if (self._currentMatchIndex >= highlights.length) self._currentMatchIndex = 0;

        highlights[self._currentMatchIndex].scrollIntoView({ behavior: "smooth", block: "center" });
        App.safeText(els.searchInfo,
            (self._currentMatchIndex + 1) + " / " + highlights.length + "건 일치");
    };

    /**
     * 지정 타임스탬프에 가장 가까운 발화로 스크롤한다.
     * @param {number} targetTime - 타겟 시간 (초)
     */
    ViewerView.prototype._scrollToTimestamp = function (targetTime) {
        var self = this;
        var els = self._els;
        var utteranceEls = els.timeline.querySelectorAll(".utterance");
        var closestEl = null;
        var closestDiff = Infinity;

        self._allUtterances.forEach(function (u, idx) {
            var diff = Math.abs((u.start || 0) - targetTime);
            if (diff < closestDiff) {
                closestDiff = diff;
                closestEl = utteranceEls[idx] || null;
            }
        });

        if (closestEl) {
            closestEl.classList.add("highlighted");
            closestEl.scrollIntoView({ behavior: "smooth", block: "center" });
        }
    };

    /**
     * 회의 메타 정보를 로드한다.
     */
    ViewerView.prototype._loadMeetingInfo = async function () {
        var self = this;
        var els = self._els;
        try {
            var data = await App.apiRequest("/meetings/" + encodeURIComponent(self._meetingId));

            els.meetingInfo.style.display = "block";
            // 사용자 정의 title 우선, 없으면 타임스탬프 폴백
            self._lastMeetingData = data;
            self._renderMeetingTitle(data);

            els.meetingStatus.className = "viewer-status " + data.status;
            App.safeText(els.meetingStatus, App.getStatusLabel(data.status));

            els.metaFile.innerHTML = Icons.mic + ' <span>' + App.escapeHtml(App.getFileName(data.audio_path)) + '</span>';
            els.metaDate.innerHTML = Icons.calendar + ' <span>' + App.escapeHtml(App.formatDate(data.created_at)) + '</span>';

            // 액션 버튼 렌더링 (전사 시작, 재시도, 요약 생성, 삭제)
            // _loadTranscript 완료 후 다시 호출되어 복사/다운로드 버튼이 갱신됨
            self._lastMeetingData = data;
            self._renderActions(data);

            // 처리 로그 (단계별 소요시간) 로드 — completed/failed 일 때만 의미가 있음
            self._loadPipelineLog();

        } catch (e) {
            if (e.status === 404) {
                errorBanner.show("회의를 찾을 수 없습니다: " + self._meetingId);
            } else {
                errorBanner.show("회의 정보 로드 실패: " + e.message);
            }
        }
    };

    /**
     * 제목 표시 + 편집 버튼을 렌더링한다.
     * 클릭 또는 편집 버튼 → 인라인 input, Enter/Blur 저장, Esc 취소.
     * @param {Object} data - /meetings/{id} 응답
     */
    ViewerView.prototype._renderMeetingTitle = function (data) {
        var self = this;
        var els = self._els;
        var titleEl = els.meetingTitle;
        if (!titleEl) return;

        titleEl.innerHTML = "";
        titleEl.classList.remove("editing");

        var displayTitle = App.extractMeetingTitle(data);
        var titleSpan = document.createElement("span");
        titleSpan.className = "viewer-title-text";
        titleSpan.textContent = displayTitle;
        titleSpan.title = "클릭하여 제목 편집";
        titleSpan.addEventListener("click", function () {
            self._beginEditTitle(data);
        });
        titleEl.appendChild(titleSpan);

        var editBtn = document.createElement("button");
        editBtn.type = "button";
        editBtn.className = "viewer-title-edit-btn";
        editBtn.setAttribute("aria-label", "제목 편집");
        editBtn.title = "제목 편집";
        editBtn.textContent = "✎";
        editBtn.addEventListener("click", function () {
            self._beginEditTitle(data);
        });
        titleEl.appendChild(editBtn);

        // 페이지 타이틀도 갱신
        document.title = displayTitle + " · 전사문 · Recap";
    };

    /**
     * 제목 인라인 편집 모드로 전환한다.
     * @param {Object} data - 현재 회의 데이터
     */
    ViewerView.prototype._beginEditTitle = function (data) {
        var self = this;
        var titleEl = self._els.meetingTitle;
        if (!titleEl || titleEl.classList.contains("editing")) return;

        titleEl.classList.add("editing");
        titleEl.innerHTML = "";

        var currentTitle = (data && data.title && data.title.trim()) || "";
        var placeholder = App.extractMeetingTitle(data);

        var input = document.createElement("input");
        input.type = "text";
        input.className = "viewer-title-input";
        input.value = currentTitle;
        input.placeholder = placeholder;
        input.maxLength = 200;
        input.setAttribute("aria-label", "회의 제목");
        titleEl.appendChild(input);

        var hint = document.createElement("span");
        hint.className = "viewer-title-hint";
        hint.textContent = "Enter로 저장 · Esc로 취소";
        titleEl.appendChild(hint);

        var saved = false;
        var saving = false;  // 저장 in-flight 가드
        var cancelEdit = function () {
            if (saved || saving) return;
            saved = true;
            self._renderMeetingTitle(data);
        };
        var doSave = async function () {
            if (saved || saving) return;
            var next = input.value.trim();
            // 값이 기존과 동일하면 저장 스킵
            if (next === currentTitle) {
                saved = true;
                self._renderMeetingTitle(data);
                return;
            }
            saving = true;
            input.disabled = true;
            hint.textContent = "저장 중…";
            var ok = await self._saveTitle(next);
            saving = false;
            if (ok) {
                saved = true;
                // _renderMeetingTitle 은 _saveTitle 안에서 이미 호출됨
            } else {
                // 실패 — 편집 모드 유지하고 사용자가 재시도할 수 있도록 input 복원
                input.disabled = false;
                hint.textContent = "저장 실패. Enter 로 재시도, Esc 로 취소";
                input.focus();
            }
        };

        input.addEventListener("keydown", function (e) {
            if (e.isComposing) return;  // IME 조합 중에는 무시
            if (e.key === "Enter") {
                e.preventDefault();
                doSave();
            } else if (e.key === "Escape") {
                e.preventDefault();
                cancelEdit();
            }
        });
        // blur 시 자동 저장 — 단, 저장 중이면 race 방지
        input.addEventListener("blur", function () {
            // 약간의 지연 후 doSave (다른 이벤트 핸들러 완료 대기)
            setTimeout(function () {
                if (!saved && !saving && input.isConnected) {
                    doSave();
                }
            }, 100);
        });

        setTimeout(function () {
            input.focus();
            input.select();
        }, 0);
    };

    /**
     * PATCH /api/meetings/{id} 로 제목을 저장한다.
     *
     * 실패 시 사용자가 입력한 텍스트를 잃지 않도록 편집 모드를 유지하고
     * 인라인 에러를 표시한다 (이전에는 _renderMeetingTitle 로 원본 복원해서
     * 사용자 입력이 사라지는 데이터 손실 버그가 있었음).
     *
     * @param {string} title - 새 제목 (빈 문자열이면 초기화)
     * @returns {Promise<boolean>} 성공 여부 (호출자가 편집 모드를 닫을지 결정)
     */
    ViewerView.prototype._saveTitle = async function (title) {
        var self = this;
        // 뷰가 이미 destroy 되었으면 (사용자가 다른 회의로 이동) 조용히 abort
        if (!self._els || !self._els.meetingTitle || !self._els.meetingTitle.isConnected) {
            return false;
        }
        try {
            var resp = await fetch(
                "/api/meetings/" + encodeURIComponent(self._meetingId),
                {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ title: title }),
                }
            );
            if (!resp.ok) {
                var err = await resp.json().catch(function () { return {}; });
                throw new Error(err.detail || "HTTP " + resp.status);
            }
            var data = await resp.json();
            self._lastMeetingData = data;
            self._renderMeetingTitle(data);
            if (typeof ListPanel !== "undefined" && ListPanel.loadMeetings) {
                ListPanel.loadMeetings();
            }
            return true;
        } catch (e) {
            errorBanner.show("제목 저장 실패: " + (e.message || String(e)));
            return false;
        }
    };

    /**
     * 회의 상태에 따른 액션 버튼을 렌더링한다.
     * @param {Object} data - 회의 데이터
     */
    ViewerView.prototype._renderActions = function (data) {
        var self = this;
        var els = self._els;
        var actionsEl = els.viewerActions;
        actionsEl.innerHTML = "";

        // 진행 중(queued/transcribing/diarizing/merging/embedding) 시 취소 버튼
        var inProgressStates = {
            queued: true, transcribing: true, diarizing: true,
            merging: true, embedding: true,
        };
        if (inProgressStates[data.status]) {
            var cancelBtn = document.createElement("button");
            cancelBtn.className = "viewer-action-btn delete";
            cancelBtn.innerHTML = "✕ 전사 취소";
            cancelBtn.title =
                "전사 작업을 취소하고 녹음 완료 상태로 되돌립니다. " +
                "실행 중인 단계가 끝난 직후에 중지됩니다.";
            cancelBtn.addEventListener("click", function () {
                self._cancelMeeting(data.meeting_id, cancelBtn);
            });
            actionsEl.appendChild(cancelBtn);
        }

        // 녹음 완료 시 전사 시작 버튼
        if (data.status === "recorded") {
            var transcribeBtn = document.createElement("button");
            transcribeBtn.className = "viewer-action-btn transcribe";
            transcribeBtn.innerHTML = Icons.play + ' 전사 시작';
            transcribeBtn.addEventListener("click", function () {
                self._transcribeMeeting(data.meeting_id, transcribeBtn);
            });
            actionsEl.appendChild(transcribeBtn);
        }

        // 실패 시 재시도 버튼
        if (data.status === "failed") {
            var retryBtn = document.createElement("button");
            retryBtn.className = "viewer-action-btn retry";
            retryBtn.textContent = "\u21BB 재시도";
            retryBtn.addEventListener("click", function () {
                self._retryMeeting(data.meeting_id);
            });
            actionsEl.appendChild(retryBtn);
        }

        // 재전사 버튼 (완료/실패 회의를 처음부터 다시 전사)
        if (data.status === "completed" || data.status === "failed") {
            var reBtn = document.createElement("button");
            reBtn.className = "viewer-action-btn retry";
            reBtn.innerHTML = "↻ 재전사";
            reBtn.title = "기존 전사 결과를 폐기하고 처음부터 다시 전사합니다";
            reBtn.addEventListener("click", function () {
                self._reTranscribeMeeting(data.meeting_id, reBtn);
            });
            actionsEl.appendChild(reBtn);
        }

        // 요약 생성 버튼 (completed + skipped_steps에 summarize 포함 시)
        var skippedSteps = data.skipped_steps || [];
        var hasSummarizeSkipped = skippedSteps.indexOf("summarize") >= 0;
        if (data.status === "completed" && hasSummarizeSkipped) {
            var summarizeBtn = document.createElement("button");
            summarizeBtn.className = "viewer-action-btn summarize";
            summarizeBtn.innerHTML = Icons.doc + ' 요약 생성';
            summarizeBtn.addEventListener("click", function () {
                self._summarizeMeeting(data.meeting_id, summarizeBtn);
            });
            actionsEl.appendChild(summarizeBtn);
        }

        // A/B 테스트 버튼 (완료된 회의에서만)
        if (data.status === "completed") {
            var abTestBtn = document.createElement("button");
            abTestBtn.className = "viewer-action-btn";
            abTestBtn.innerHTML = '&#x1F9EA; A/B 테스트';
            abTestBtn.title = "이 회의를 서로 다른 모델로 A/B 테스트합니다";
            abTestBtn.addEventListener("click", function () {
                Router.navigate("/app/ab-test/new?source=" + encodeURIComponent(data.meeting_id));
            });
            actionsEl.appendChild(abTestBtn);
        }

        // 전사문 복사/다운로드 + 모두 바꾸기 버튼 (완료된 회의이며 전사문 로드된 경우)
        if (data.status === "completed" && self._allUtterances && self._allUtterances.length > 0) {
            var copyBtn = document.createElement("button");
            copyBtn.className = "viewer-action-btn copy";
            copyBtn.innerHTML = Icons.copy + ' 전사문 복사';
            copyBtn.setAttribute("aria-label", "전사문을 클립보드로 복사");
            copyBtn.addEventListener("click", function () {
                self._copyTranscript(copyBtn);
            });
            actionsEl.appendChild(copyBtn);

            var downloadBtn = document.createElement("button");
            downloadBtn.className = "viewer-action-btn download-txt";
            downloadBtn.innerHTML = Icons.doc + ' .txt 다운로드';
            downloadBtn.setAttribute("aria-label", "전사문을 텍스트 파일로 다운로드");
            downloadBtn.addEventListener("click", function () {
                self._downloadTranscript();
            });
            actionsEl.appendChild(downloadBtn);

            // 모두 바꾸기 (find/replace + 용어집 자동 등록)
            var replaceBtn = document.createElement("button");
            replaceBtn.className = "viewer-action-btn replace";
            replaceBtn.innerHTML = "↻ 모두 바꾸기";
            replaceBtn.setAttribute(
                "aria-label",
                "전사문에서 특정 패턴을 찾아 모두 치환하고 용어집에 추가"
            );
            replaceBtn.title =
                "오인식 패턴을 한 번에 치환하고 용어집에도 자동 등록해요 (발화 더블클릭으로 개별 편집도 가능)";
            replaceBtn.addEventListener("click", function () {
                self._openReplaceModal();
            });
            actionsEl.appendChild(replaceBtn);
        }

        // 삭제 버튼 (완료/실패/녹음완료 시)
        if (data.status === "completed" || data.status === "failed" || data.status === "recorded") {
            var deleteBtn = document.createElement("button");
            deleteBtn.className = "viewer-action-btn delete";
            deleteBtn.textContent = "\u2715 삭제";
            deleteBtn.addEventListener("click", function () {
                self._deleteMeeting(data.meeting_id);
            });
            actionsEl.appendChild(deleteBtn);
        }
    };

    /**
     * 전사문을 일반 텍스트 형식으로 빌드한다.
     * 형식: "[참석자 N] HH:MM:SS  텍스트" 줄 단위.
     * @returns {string} 다운로드/복사용 plain text
     */
    ViewerView.prototype._buildTranscriptText = function () {
        var self = this;
        if (!self._allUtterances || self._allUtterances.length === 0) {
            return "";
        }

        // 화자 → 참석자 N 매핑 (전사 뷰와 동일한 번호 부여)
        var speakerNumbers = {};
        var count = 0;
        self._allUtterances.forEach(function (u) {
            if (!(u.speaker in speakerNumbers)) {
                count++;
                speakerNumbers[u.speaker] = count;
            }
        });

        function getLabel(speaker) {
            if (speaker === "UNKNOWN") return "참석자 ?";
            return "참석자 " + (speakerNumbers[speaker] || "?");
        }

        // 헤더 + 본문
        var header = [
            "회의 ID: " + self._meetingId,
            "추출 일시: " + new Date().toISOString(),
            "화자 수: " + count + "명",
            "발화 수: " + self._allUtterances.length + "건",
            "─────────────────────────────────────────",
            "",
        ].join("\n");

        var lines = self._allUtterances.map(function (u) {
            var label = getLabel(u.speaker);
            var time = App.formatTime(u.start);
            return "[" + label + "] " + time + "  " + (u.text || "");
        });

        return header + lines.join("\n") + "\n";
    };

    /**
     * 전사문을 클립보드로 복사한다.
     * 복사 후 버튼에 일시적으로 "복사됨" 표시.
     * @param {HTMLElement} btn - 클릭된 복사 버튼
     */
    ViewerView.prototype._copyTranscript = function (btn) {
        var self = this;
        var text = self._buildTranscriptText();
        if (!text) {
            errorBanner.show("복사할 전사문이 없습니다.");
            return;
        }

        var originalHtml = btn.innerHTML;
        App.copyToClipboard(text).then(function (ok) {
            if (ok) {
                btn.innerHTML = Icons.check + ' 복사됨';
                btn.classList.add("copied");
                setTimeout(function () {
                    btn.innerHTML = originalHtml;
                    btn.classList.remove("copied");
                }, 2000);
            } else {
                errorBanner.show("클립보드 복사에 실패했습니다.");
            }
        });
    };

    /**
     * 전사문을 .txt 파일로 다운로드한다.
     * 파일명: {meeting_id}_transcript.txt
     */
    ViewerView.prototype._downloadTranscript = function () {
        var self = this;
        var text = self._buildTranscriptText();
        if (!text) {
            errorBanner.show("다운로드할 전사문이 없습니다.");
            return;
        }

        try {
            var blob = new Blob([text], { type: "text/plain;charset=utf-8" });
            var url = URL.createObjectURL(blob);
            var a = document.createElement("a");
            a.href = url;
            a.download = self._meetingId + "_transcript.txt";
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            // 메모리 해제
            setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
        } catch (e) {
            errorBanner.show("파일 다운로드 실패: " + e.message);
        }
    };

    /**
     * 녹음 완료된 회의의 전사를 시작한다.
     * @param {string} meetingId - 전사할 회의 ID
     * @param {HTMLElement} btn - 클릭된 버튼 요소
     */
    ViewerView.prototype._transcribeMeeting = async function (meetingId, btn) {
        var originalText = btn.textContent;
        btn.disabled = true;
        btn.textContent = "요청 중...";

        try {
            await App.apiPost("/meetings/" + encodeURIComponent(meetingId) + "/transcribe", {});
            this._loadMeetingInfo();
            ListPanel.loadMeetings();
        } catch (e) {
            errorBanner.show("전사 시작 실패: " + e.message);
            btn.disabled = false;
            btn.textContent = originalText;
        }
    };

    /**
     * 실패한 회의를 재시도한다.
     * @param {string} meetingId - 재시도할 회의 ID
     */
    ViewerView.prototype._retryMeeting = async function (meetingId) {
        try {
            await App.apiPost("/meetings/" + encodeURIComponent(meetingId) + "/retry", {});
            this._loadMeetingInfo();
            ListPanel.loadMeetings();
        } catch (e) {
            errorBanner.show("재시도 실패: " + e.message);
        }
    };

    /**
     * 진행 중인 전사를 취소한다.
     * queued: 즉시 recorded 로 전환.
     * processing: 다음 단계 경계에서 중지 (백그라운드 폴링이 갱신).
     * @param {string} meetingId
     * @param {HTMLElement} btn
     */
    ViewerView.prototype._cancelMeeting = async function (meetingId, btn) {
        if (!confirm(
            "진행 중인 전사를 취소하시겠습니까?\n" +
            "현재 실행 중인 단계가 끝난 직후 중지되고\n" +
            "녹음 완료 상태로 되돌아갑니다."
        )) {
            return;
        }
        var originalText = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = "취소 요청 중...";
        try {
            await App.apiPost(
                "/meetings/" + encodeURIComponent(meetingId) + "/cancel",
                {}
            );
            // 즉시 한 번 갱신하고, 백그라운드 폴링이 처리되도록 약간의 딜레이 후 한 번 더
            this._loadMeetingInfo();
            ListPanel.loadMeetings();
            setTimeout(function () {
                if (typeof ListPanel !== "undefined" && ListPanel.loadMeetings) {
                    ListPanel.loadMeetings();
                }
            }, 3000);
        } catch (e) {
            errorBanner.show("취소 실패: " + e.message);
            btn.disabled = false;
            btn.innerHTML = originalText;
        }
    };

    /**
     * 기존 전사 결과를 폐기하고 처음부터 다시 전사한다.
     * 사용자 확인 후 POST /meetings/{id}/re-transcribe 호출.
     * @param {string} meetingId
     * @param {HTMLElement} btn
     */
    ViewerView.prototype._reTranscribeMeeting = async function (meetingId, btn) {
        if (!confirm(
            "기존 전사 결과(전사문/요약/체크포인트)를 모두 폐기하고\n" +
            "처음부터 다시 전사합니다. 계속하시겠습니까?"
        )) {
            return;
        }
        var originalText = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = "재전사 요청 중...";
        try {
            await App.apiPost(
                "/meetings/" + encodeURIComponent(meetingId) + "/re-transcribe",
                {}
            );
            this._loadMeetingInfo();
            ListPanel.loadMeetings();
        } catch (e) {
            errorBanner.show("재전사 실패: " + e.message);
            btn.disabled = false;
            btn.innerHTML = originalText;
        }
    };

    /**
     * 단계 진행 이벤트(`step_progress`)를 처리해 라이브 로그 상태를 갱신한다.
     *
     * detail 스키마:
     *   {
     *     meeting_id, job_id, step,
     *     phase: "start" | "complete",
     *     input_size, eta_seconds, elapsed_seconds, anomaly
     *   }
     */
    ViewerView.prototype._handleStepProgress = function (detail) {
        var step = detail.step;
        if (!step) return;
        var entry = this._liveLog[step] || {};

        if (detail.phase === "start") {
            entry.status = "running";
            entry.startedAt = Date.now();
            entry.eta = Number(detail.eta_seconds) || 0;
            entry.anomaly = "normal";
            entry.elapsed = 0;
            entry.inputSize = Number(detail.input_size) || 0;
            // 이전에 완료됐다가 재시도되는 경우도 있으므로 성공 플래그 초기화
        } else if (detail.phase === "complete") {
            entry.status = "completed";
            entry.elapsed = Number(detail.elapsed_seconds) || entry.elapsed || 0;
            entry.eta = Number(detail.eta_seconds) || entry.eta || 0;
            entry.anomaly = detail.anomaly || "normal";
        }
        this._liveLog[step] = entry;
        this._ensureLiveLogTicker();
        this._renderLiveLog();
    };

    /**
     * 라이브 로그 1초 틱을 시작한다 (실행 중인 단계가 있을 때만).
     */
    ViewerView.prototype._ensureLiveLogTicker = function () {
        var self = this;
        if (self._liveLogTickTimer) return;
        self._liveLogTickTimer = setInterval(function () {
            var hasRunning = false;
            Object.keys(self._liveLog).forEach(function (k) {
                if (self._liveLog[k].status === "running") hasRunning = true;
            });
            if (!hasRunning) {
                clearInterval(self._liveLogTickTimer);
                self._liveLogTickTimer = null;
                return;
            }
            self._renderLiveLog();
        }, 1000);
        self._timers.push(self._liveLogTickTimer);
    };

    /**
     * 라이브 로그 상태를 처리 로그 패널에 렌더링한다.
     *
     * 진행 중인 단계는 경과 시간을 Date.now() 기반으로 재계산하고,
     * elapsed/eta 비율을 진행률 바로 표시한다. 예상 대비 초과 시 색상 경고.
     */
    ViewerView.prototype._renderLiveLog = function () {
        var self = this;
        var els = self._els;
        if (!els.logPanel || !els.logTable || !els.logTotal) return;

        var stepKeys = PIPELINE_STEPS.map(function (s) { return s.key; });
        var labelMap = {};
        PIPELINE_STEPS.forEach(function (s) { labelMap[s.key] = s.label; });

        // 표시할 단계가 하나도 없으면 패널 숨김
        var hasAnyEntry = stepKeys.some(function (k) { return self._liveLog[k]; });
        if (!hasAnyEntry) {
            // 라이브 데이터가 없으면 REST 기반 폴백(_loadPipelineLog)에 맡김
            return;
        }

        var totalElapsed = 0;
        var rows = stepKeys.map(function (key) {
            var entry = self._liveLog[key];
            if (!entry) return "";

            // 진행 중이면 wall-clock 기반으로 현재 경과 시간 재계산
            var elapsed = entry.elapsed || 0;
            if (entry.status === "running" && entry.startedAt) {
                elapsed = (Date.now() - entry.startedAt) / 1000;
            }

            // 상태/색상 분류
            var statusClass, statusText;
            if (entry.status === "completed") {
                statusClass = "success";
                statusText = "✓";
                totalElapsed += elapsed;
            } else if (entry.status === "failed") {
                statusClass = "failed";
                statusText = "✗";
            } else if (entry.status === "skipped") {
                statusClass = "skipped";
                statusText = "skip";
            } else if (entry.status === "running") {
                statusClass = "running";
                statusText = "…";
                totalElapsed += elapsed;
            } else {
                statusClass = "pending";
                statusText = "·";
            }

            // 예상 대비 경과 비율 → 이상 탐지 색상 (실시간 판단)
            var anomaly = entry.anomaly || "normal";
            if (entry.status === "running" && entry.eta > 0) {
                var ratio = elapsed / entry.eta;
                if (ratio >= 2.5) anomaly = "danger";
                else if (ratio >= 1.5) anomaly = "warning";
                else anomaly = "normal";
            }

            // 진행률 바 (ETA가 있을 때만)
            var progressBar = "";
            if (entry.eta > 0 && entry.status === "running") {
                var pct = Math.min(100, Math.round((elapsed / entry.eta) * 100));
                progressBar =
                    '<div class="log-progress-bar"><div class="log-progress-fill" ' +
                    'style="width:' + pct + '%"></div></div>';
            }

            // ETA 텍스트
            var etaText = "";
            if (entry.eta > 0) {
                if (entry.status === "running") {
                    var remain = Math.max(0, entry.eta - elapsed);
                    etaText = '<span class="log-eta">남은 ~' + self._formatElapsed(remain) + '</span>';
                } else if (entry.status === "completed") {
                    etaText = '<span class="log-eta">예상 ' + self._formatElapsed(entry.eta) + '</span>';
                }
            }

            return [
                '<div class="log-row anomaly-' + anomaly + ' status-' + statusClass + '">',
                '  <div class="log-step">' + App.escapeHtml(labelMap[key] || key) + '</div>',
                '  <div class="log-progress">',
                     progressBar,
                '    <div class="log-elapsed">' + self._formatElapsed(elapsed) + '</div>',
                     etaText,
                '  </div>',
                '  <div class="log-status ' + statusClass + '">' + statusText + '</div>',
                '</div>',
            ].join("");
        }).filter(function (r) { return r; });

        els.logTable.innerHTML = rows.join("");
        els.logTotal.textContent = "(총 " + self._formatElapsed(totalElapsed) + ")";
        els.logPanel.style.display = "block";
        // 실행 중일 때는 패널 자동 펼침
        var anyRunning = stepKeys.some(function (k) {
            return self._liveLog[k] && self._liveLog[k].status === "running";
        });
        if (anyRunning && !els.logPanel.hasAttribute("open")) {
            els.logPanel.setAttribute("open", "");
        }
    };

    /**
     * 파이프라인 처리 로그 (단계별 소요시간) 를 로드하여 표시한다.
     * GET /meetings/{id}/pipeline-state
     */
    ViewerView.prototype._loadPipelineLog = async function () {
        var self = this;
        var els = self._els;
        if (!els.logPanel || !els.logTable || !els.logTotal) return;
        try {
            var data = await App.apiRequest(
                "/meetings/" + encodeURIComponent(self._meetingId) + "/pipeline-state"
            );
            var steps = data.step_results || [];
            if (!steps.length) {
                // 라이브 데이터가 이미 있다면 패널은 유지
                if (Object.keys(self._liveLog).length === 0) {
                    els.logPanel.style.display = "none";
                }
                return;
            }

            // REST 로 받은 완료된 단계들을 라이브 상태에도 반영 (재진입 복원)
            steps.forEach(function (s) {
                var existing = self._liveLog[s.step];
                // 라이브 상태가 running 이면 덮어쓰지 않음 (최신이 우선)
                if (existing && existing.status === "running") return;
                self._liveLog[s.step] = {
                    status: s.success ? "completed" : "failed",
                    elapsed: Number(s.elapsed_seconds) || 0,
                    eta: existing ? existing.eta : 0,
                    anomaly: "normal",
                };
            });
            (data.skipped_steps || []).forEach(function (k) {
                self._liveLog[k] = { status: "skipped", elapsed: 0, eta: 0, anomaly: "normal" };
            });
            // 라이브 렌더러가 있으면 그걸로 통일
            self._renderLiveLog();
            return;
            var total = data.total_elapsed_seconds || 0;
            els.logTotal.textContent = "(총 " + self._formatElapsed(total) + ")";

            // 단계 라벨 매핑 (PIPELINE_STEPS 기반)
            var labelMap = {};
            PIPELINE_STEPS.forEach(function (s) { labelMap[s.key] = s.label; });

            var skipped = data.skipped_steps || [];
            var rows = steps.map(function (s) {
                var label = labelMap[s.step] || s.step;
                var statusClass = s.success ? "success" : "failed";
                var statusText = s.success ? "✓" : "✗";
                if (skipped.indexOf(s.step) >= 0) {
                    statusClass = "skipped";
                    statusText = "skip";
                }
                return [
                    '<div class="log-row">',
                    '  <div class="log-step">' + App.escapeHtml(label) + '</div>',
                    '  <div class="log-elapsed">' + self._formatElapsed(s.elapsed_seconds || 0) + '</div>',
                    '  <div class="log-status ' + statusClass + '">' + statusText + '</div>',
                    '</div>',
                ].join("");
            });
            els.logTable.innerHTML = rows.join("");
            els.logPanel.style.display = "block";
        } catch (e) {
            // pipeline_state.json 이 없는 회의(녹음만 완료, 미전사 등)는 정상 — 패널 숨김
            els.logPanel.style.display = "none";
        }
    };

    /**
     * 초 단위 시간을 사람이 읽기 좋은 형식으로 변환한다.
     * @param {number} sec - 초
     * @returns {string} 예: "12.3초", "1분 5초", "1시간 23분"
     */
    ViewerView.prototype._formatElapsed = function (sec) {
        sec = Math.round(sec);
        if (sec < 60) return sec + "초";
        if (sec < 3600) {
            var m = Math.floor(sec / 60);
            var s = sec % 60;
            return s ? (m + "분 " + s + "초") : (m + "분");
        }
        var h = Math.floor(sec / 3600);
        var mm = Math.floor((sec % 3600) / 60);
        return mm ? (h + "시간 " + mm + "분") : (h + "시간");
    };

    /**
     * 회의를 삭제한다.
     * @param {string} meetingId - 삭제할 회의 ID
     */
    ViewerView.prototype._deleteMeeting = async function (meetingId) {
        if (!confirm("'" + meetingId + "' 회의를 삭제하시겠습니까?\n삭제된 데이터는 복구할 수 없습니다.")) {
            return;
        }
        try {
            await App.apiDelete("/meetings/" + encodeURIComponent(meetingId));
            ListPanel.loadMeetings();
            Router.navigate("/app");
        } catch (e) {
            errorBanner.show("삭제 실패: " + e.message);
        }
    };

    /**
     * 온디맨드 요약을 요청한다.
     * @param {string} meetingId - 회의 ID
     * @param {HTMLElement} btn - 클릭된 버튼 요소
     */
    ViewerView.prototype._summarizeMeeting = async function (meetingId, btn) {
        // 액션바 버튼에서 호출됨. 실제 폴링/갱신 로직은 _requestSummarize 와 통일.
        // 폴링 도중 사용자가 요약 탭을 보도록 자동 전환.
        var els = this._els;
        if (els.tabNav) {
            els.tabNav.style.display = "flex";
            var sumTab = document.getElementById("viewerTabSummary");
            if (sumTab) sumTab.click();
        }
        // 액션 버튼은 _loadMeetingInfo 가 다시 렌더하므로 별도 복원 불필요.
        // _requestSummarize 가 summarizeBtn 또는 toolbar 의 재생성 버튼을 사용한다.
        await this._requestSummarize(false);
    };

    /**
     * 전사문을 로드한다.
     */
    ViewerView.prototype._loadTranscript = async function () {
        var self = this;
        var els = self._els;
        els.transcriptLoading.classList.add("visible");
        els.transcriptEmpty.style.display = "none";

        try {
            var data = await App.apiRequest(
                "/meetings/" + encodeURIComponent(self._meetingId) + "/transcript"
            );

            self._allUtterances = data.utterances || [];

            if (self._allUtterances.length === 0) {
                els.transcriptEmpty.style.display = "block";
                // 처리 중 상태면 파이프라인 진행 표시 + 폴링 시작
                self._startPipelinePolling();
                return;
            }

            // 화자 색상 맵 + 범례
            self._buildSpeakerColorMap(data.speakers || []);
            self._renderSpeakerLegend(data.speakers || []);

            // 메타 정보 업데이트
            els.metaSpeakers.innerHTML = Icons.person + ' <span>화자 ' + App.escapeHtml(String(data.num_speakers || 0)) + '명</span>';
            els.metaUtterances.innerHTML = Icons.chat + ' <span>발화 ' + App.escapeHtml(String(data.total_utterances || 0)) + '건</span>';

            // 전사문 로드 후 액션 버튼 재렌더 (복사/다운로드 버튼이 _allUtterances 길이에 의존)
            if (self._lastMeetingData) {
                self._renderActions(self._lastMeetingData);
            }

            // 탭과 검색바 표시
            els.tabNav.style.display = "flex";
            els.searchBar.style.display = "flex";

            // 초기 검색어 적용 (URL에서 전달된 경우)
            if (self._initialQuery) {
                els.searchInput.value = self._initialQuery;
                self._currentQuery = self._initialQuery;
            }

            // 타임라인 렌더링
            self._renderTimeline(self._allUtterances, self._currentQuery);

            // URL 타임스탬프로 해당 발화 위치로 스크롤
            if (!isNaN(self._initialTimestamp) && self._initialTimestamp >= 0) {
                self._scrollToTimestamp(self._initialTimestamp);
            }

        } catch (e) {
            if (e.status === 404) {
                els.transcriptEmpty.style.display = "block";
                self._startPipelinePolling();
            } else {
                errorBanner.show("전사문 로드 실패: " + e.message);
            }
        } finally {
            els.transcriptLoading.classList.remove("visible");
        }
    };

    /**
     * 파이프라인 진행 상태 폴링을 시작한다.
     * 처리 중인 회의일 때 3초마다 상태를 확인하고 프로그레스 바를 업데이트한다.
     * 전사 완료 시 자동으로 전사문을 다시 로드한다.
     */
    ViewerView.prototype._startPipelinePolling = function () {
        var self = this;
        var els = self._els;

        // 파이프라인 6단계 정의
        var pipelineSteps = [
            { key: "convert", label: "변환" },
            { key: "transcribe", label: "전사" },
            { key: "diarize", label: "화자분리" },
            { key: "merge", label: "병합" },
            { key: "correct", label: "보정" },
            { key: "summarize", label: "요약" },
        ];

        // 진행 중 상태 목록
        var processingStatuses = {
            queued: true, transcribing: true, diarizing: true,
            merging: true, embedding: true, recording: true,
        };

        // 상태 → 파이프라인 단계 매핑
        var statusToStep = {
            transcribing: "transcribe",
            diarizing: "diarize",
            merging: "merge",
            embedding: "correct",
        };

        function renderProgress(currentStep, completedSteps) {
            var html = "";
            for (var i = 0; i < pipelineSteps.length; i++) {
                var step = pipelineSteps[i];
                var state = "pending";
                if (completedSteps && completedSteps.indexOf(step.key) >= 0) {
                    state = "done";
                } else if (currentStep === step.key) {
                    state = "active";
                }
                html += '<div class="pipeline-step ' + state + '">';
                html += '<div class="pipeline-step-dot"></div>';
                html += '<div class="pipeline-step-label">' + App.escapeHtml(step.label) + '</div>';
                html += '</div>';
                if (i < pipelineSteps.length - 1) {
                    html += '<div class="pipeline-step-line ' + (state === "done" ? "done" : "") + '"></div>';
                }
            }
            els.pipelineSteps.innerHTML = html;
        }

        // 초기 렌더링
        els.pipelineProgress.style.display = "block";
        renderProgress("", []);
        App.safeText(els.pipelineStatus, "상태 확인 중...");

        // 빈 상태 텍스트 업데이트
        App.safeText(document.getElementById("viewerEmptyText"), "파이프라인 처리 중");
        var subEl = document.getElementById("viewerEmptySub");
        if (subEl) subEl.innerHTML = "완료되면 전사문이 자동으로 표시됩니다.";

        // 3초 간격 폴링
        var pollTimer = setInterval(async function () {
            try {
                var meeting = await App.apiRequest(
                    "/meetings/" + encodeURIComponent(self._meetingId)
                );
                var status = meeting.status || "";

                // 완료 시: 전사문 다시 로드
                if (status === "completed") {
                    clearInterval(pollTimer);
                    els.pipelineProgress.style.display = "none";
                    els.transcriptEmpty.style.display = "none";
                    self._loadTranscript();
                    self._loadSummary();
                    self._loadMeetingInfo();
                    return;
                }

                // 실패 시: 에러 표시
                if (status === "failed") {
                    clearInterval(pollTimer);
                    App.safeText(els.pipelineStatus, "처리 실패: " + (meeting.error_message || "알 수 없는 오류"));
                    els.pipelineStatus.classList.add("error");
                    return;
                }

                // 처리 중: 단계 업데이트
                var currentStep = statusToStep[status] || "";
                // completed_steps는 API에 없으므로 현재 단계 이전을 완료로 추정
                var completedSteps = [];
                if (currentStep) {
                    for (var i = 0; i < pipelineSteps.length; i++) {
                        if (pipelineSteps[i].key === currentStep) break;
                        completedSteps.push(pipelineSteps[i].key);
                    }
                }

                renderProgress(currentStep, completedSteps);
                var stepLabel = "";
                for (var j = 0; j < pipelineSteps.length; j++) {
                    if (pipelineSteps[j].key === currentStep) {
                        stepLabel = pipelineSteps[j].label;
                        break;
                    }
                }
                App.safeText(els.pipelineStatus, stepLabel ? stepLabel + " 진행 중..." : App.getStatusLabel(status));

            } catch (e) {
                // 네트워크 오류 등 → 무시하고 계속 폴링
            }
        }, 3000);

        self._timers.push(pollTimer);
    };

    /**
     * 회의록을 로드한다.
     */
    ViewerView.prototype._loadSummary = async function () {
        var self = this;
        var els = self._els;
        els.summaryLoading.classList.add("visible");
        els.summaryEmpty.style.display = "none";

        try {
            var data = await App.apiRequest(
                "/meetings/" + encodeURIComponent(self._meetingId) + "/summary"
            );

            if (!data.markdown) {
                els.summaryEmpty.style.display = "block";
                els.summarizeBtn.style.display = "inline-block";
                return;
            }

            // 현재 마크다운 보관 (편집 시 기준값)
            // dirty 플래그도 리셋 — 서버의 진실을 받았으므로 깨끗한 상태
            self._currentSummaryMd = data.markdown;
            self._summaryDirty = false;
            self._renderSummaryView(data.markdown);

            // 요약 생성 버튼 숨기기
            els.summarizeBtn.style.display = "none";

            // 탭 표시
            els.tabNav.style.display = "flex";

        } catch (e) {
            if (e.status === 404) {
                els.summaryEmpty.style.display = "block";
                els.summarizeBtn.style.display = "inline-block";
            } else {
                console.warn("회의록 로드 실패:", e.message);
                els.summaryEmpty.style.display = "block";
            }
        } finally {
            els.summaryLoading.classList.remove("visible");
        }
    };

    /**
     * 요약 뷰 모드 (마크다운 렌더링 + 편집/재생성 버튼) 를 그린다.
     * @param {string} markdown
     */
    ViewerView.prototype._renderSummaryView = function (markdown) {
        var self = this;
        var els = self._els;

        els.summaryContent.innerHTML = "";

        // 툴바: [편집] [재생성]
        var toolbar = document.createElement("div");
        toolbar.className = "summary-toolbar";

        var editBtn = document.createElement("button");
        editBtn.type = "button";
        editBtn.className = "btn-secondary";
        editBtn.innerHTML = "✎ 편집";
        editBtn.addEventListener("click", function () {
            self._beginEditSummary();
        });
        toolbar.appendChild(editBtn);

        var regenerateBtn = document.createElement("button");
        regenerateBtn.type = "button";
        regenerateBtn.className = "btn-regenerate";
        regenerateBtn.innerHTML = Icons.gear + " 요약 재생성";
        regenerateBtn.addEventListener("click", function () {
            if (self._summaryDirty) {
                if (!window.confirm(
                    "직접 편집한 내용이 있어요. 재생성하면 편집본이 사라지고 " +
                    "보정 결과로 덮어쓰여요 (.bak 에 백업). 계속할까요?"
                )) return;
            }
            self._requestSummarize(true);
        });
        toolbar.appendChild(regenerateBtn);

        els.summaryContent.appendChild(toolbar);

        // 렌더된 마크다운
        var rendered = document.createElement("div");
        rendered.className = "summary-rendered";
        rendered.innerHTML = App.renderMarkdown(markdown);
        els.summaryContent.appendChild(rendered);
    };

    /**
     * 요약 편집 모드로 전환한다 (textarea + 저장/취소).
     */
    ViewerView.prototype._beginEditSummary = function () {
        var self = this;
        var els = self._els;
        var markdown = self._currentSummaryMd || "";

        els.summaryContent.innerHTML = "";

        var editor = document.createElement("div");
        editor.className = "summary-editor";

        var textarea = document.createElement("textarea");
        textarea.className = "summary-textarea";
        textarea.value = markdown;
        textarea.spellcheck = false;
        editor.appendChild(textarea);

        var toolbar = document.createElement("div");
        toolbar.className = "summary-edit-actions";

        var cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "btn-secondary";
        cancelBtn.textContent = "취소";
        cancelBtn.addEventListener("click", function () {
            self._renderSummaryView(self._currentSummaryMd || "");
        });
        toolbar.appendChild(cancelBtn);

        var saveBtn = document.createElement("button");
        saveBtn.type = "button";
        saveBtn.className = "btn-regenerate";
        saveBtn.textContent = "저장";
        saveBtn.addEventListener("click", async function () {
            // 변경 없으면 저장 스킵 + dirty 마킹도 안 함 (회귀 방지)
            if (textarea.value === (self._currentSummaryMd || "")) {
                self._renderSummaryView(self._currentSummaryMd || "");
                return;
            }
            saveBtn.disabled = true;
            saveBtn.textContent = "저장 중…";
            try {
                var resp = await fetch(
                    "/api/meetings/" +
                        encodeURIComponent(self._meetingId) +
                        "/summary",
                    {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ markdown: textarea.value }),
                    }
                );
                if (!resp.ok) {
                    var err = await resp.json().catch(function () { return {}; });
                    throw new Error(err.detail || "HTTP " + resp.status);
                }
                var data = await resp.json();
                self._currentSummaryMd = data.markdown;
                // 사용자가 직접 편집해 저장한 상태 — 재생성 시 confirm 필요
                self._summaryDirty = true;
                self._renderSummaryView(data.markdown);
            } catch (e) {
                errorBanner.show("회의록 저장 실패: " + (e.message || e));
                saveBtn.disabled = false;
                saveBtn.textContent = "저장";
            }
        });
        toolbar.appendChild(saveBtn);

        editor.appendChild(toolbar);
        els.summaryContent.appendChild(editor);

        setTimeout(function () { textarea.focus(); }, 0);
    };

    /**
     * 뷰를 정리한다.
     */
    ViewerView.prototype.destroy = function () {
        // 이벤트 리스너 해제
        this._listeners.forEach(function (entry) {
            entry.el.removeEventListener(entry.type, entry.fn);
        });
        this._listeners = [];

        // 타이머 해제
        clearTimeout(this._searchTimeout);
        this._timers.forEach(function (t) { clearInterval(t); clearTimeout(t); });
        this._timers = [];

        // 발화 음성 재생용 audio element 정리 (다른 뷰로 이동 시 leak 방지)
        if (this._audioElement) {
            try { this._audioElement.pause(); } catch (e) { /* no-op */ }
            this._audioElement.src = "";
            if (this._audioElement.parentNode) {
                this._audioElement.parentNode.removeChild(this._audioElement);
            }
            this._audioElement = null;
        }
        this._currentPlayingIdx = -1;
        this._currentPlayingEl = null;
        this._currentPlayingBtn = null;

        // 페이지 타이틀 복원
        document.title = "회의록 · Recap";
    };


    // =================================================================
    // === ChatView (AI 채팅) ===
    // =================================================================

    /**
     * AI 채팅 뷰: RAG 기반 질문/답변, 참조 카드, 세션 관리.
     * @constructor
     */
    function ChatView() {
        var self = this;
        self._listeners = [];
        self._timers = [];
        self._els = {};
        self._isSending = false;
        self._messageCount = 0;
        self._currentAbortController = null;

        // 세션 ID 생성 (대화 컨텍스트 유지)
        self._sessionId = self._generateSessionId();

        self._render();
        self._bind();
        self._loadMeetingList();
    }

    /**
     * 세션 ID를 생성한다.
     * @returns {string} UUID v4 형식 세션 ID
     */
    ChatView.prototype._generateSessionId = function () {
        if (typeof crypto !== "undefined" && crypto.randomUUID) {
            return crypto.randomUUID();
        }
        // 폴백: 간단한 UUID v4
        return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
            var r = (Math.random() * 16) | 0;
            var v = c === "x" ? r : (r & 0x3) | 0x8;
            return v.toString(16);
        });
    };

    /**
     * 환영 메시지 HTML을 생성한다. (_render 및 _clearChat 공용)
     * @returns {string} 환영 메시지 HTML 문자열
     */
    ChatView.prototype._createWelcomeHtml = function () {
        // 채팅 빈 상태 (mockup §5.3) — empty-state 패턴 + 기존 welcome-tips 보존
        // Hidden AI 원칙(design.md §5.1)에 따라 'AI' 단어 사용 금지
        return '<div class="empty-state-container" id="chatWelcomeMessage" data-empty="chat">' +
            '<div class="empty-state" role="status">' +
            '<svg class="empty-state-icon" width="48" height="48" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
            '<path d="M6 12h22a4 4 0 014 4v12a4 4 0 01-4 4h-12l-6 6v-6H6a4 4 0 01-4-4V16a4 4 0 014-4z" transform="translate(2 0)"/>' +
            '<path d="M16 18h22a4 4 0 014 4v12a4 4 0 01-4 4h-2v6l-6-6h-14a4 4 0 01-4-4V22a4 4 0 014-4z" transform="translate(0 4)" opacity="0.6"/>' +
            '</svg>' +
            '<h2 class="empty-state-title">대화를 시작해 보세요</h2>' +
            '<p class="empty-state-description">회의 내용에 대해 무엇이든 물어보세요. 화자별 요약·결정사항·다음 액션 등을 정리해 드려요.</p>' +
            '</div>' +
            '<div class="welcome-tips">' +
                '<div class="welcome-tip"><span class="tip-arrow">&rarr;</span> "지난 회의에서 결정된 일정이 뭐야?"</div>' +
                '<div class="welcome-tip"><span class="tip-arrow">&rarr;</span> "프로젝트 진행 상황을 요약해줘"</div>' +
                '<div class="welcome-tip"><span class="tip-arrow">&rarr;</span> "다음 마일스톤까지 해야 할 일은?"</div>' +
            '</div>' +
        '</div>';
    };

    /**
     * 채팅 뷰 DOM을 생성한다.
     */
    ChatView.prototype._render = function () {
        var contentEl = Router.getContentEl();
        contentEl.innerHTML = "";

        var html = [
            '<div class="chat-layout">',

            // 제어 바
            '  <div class="controls-bar">',
            '    <span class="controls-label">검색 범위:</span>',
            '    <select class="controls-select" id="chatMeetingFilter" aria-label="검색 범위 회의 선택">',
            '      <option value="">전체 회의</option>',
            '    </select>',
            '    <div class="controls-right">',
            '      <button class="btn-small" id="chatBtnClearChat">대화 초기화</button>',
            '    </div>',
            '  </div>',

            // 메시지 영역
            '  <div class="messages-area" id="chatMessagesArea" role="log" aria-live="polite" aria-label="채팅 메시지">',
            this._createWelcomeHtml(),
            '  </div>',

            // 타이핑 인디케이터
            '  <div class="typing-indicator" id="chatTypingIndicator" role="status" aria-live="polite">',
            '    <div class="typing-dots">',
            '      <span class="typing-dot"></span>',
            '      <span class="typing-dot"></span>',
            '      <span class="typing-dot"></span>',
            '    </div>',
            '    <span class="typing-text">답변을 생성하고 있어요…</span>',
            '  </div>',

            // 입력 영역
            '  <div class="input-area">',
            '    <div class="input-row">',
            '      <div class="input-wrapper">',
            '        <textarea class="chat-input" id="chatInput"',
            '                  placeholder="회의 내용에 대해 질문하세요..."',
            '                  aria-label="회의 내용 질문 입력"',
            '                  rows="1"></textarea>',
            '      </div>',
            '      <button class="send-btn" id="chatSendBtn" disabled aria-label="메시지 전송">전송</button>',
            '      <button class="btn-cancel-send" id="chatCancelBtn" aria-label="응답 생성 취소">취소</button>',
            '    </div>',
            '    <div class="input-hint">Enter로 전송, Shift+Enter로 줄바꿈</div>',
            '  </div>',

            '</div>',
        ].join("\n");

        contentEl.innerHTML = html;

        // DOM 참조 캐싱
        this._els = {
            meetingFilter: document.getElementById("chatMeetingFilter"),
            btnClearChat: document.getElementById("chatBtnClearChat"),
            messagesArea: document.getElementById("chatMessagesArea"),
            welcomeMessage: document.getElementById("chatWelcomeMessage"),
            typingIndicator: document.getElementById("chatTypingIndicator"),
            chatInput: document.getElementById("chatInput"),
            sendBtn: document.getElementById("chatSendBtn"),
            cancelBtn: document.getElementById("chatCancelBtn"),
        };

        // 페이지 타이틀 업데이트
        document.title = "채팅 · Recap";
    };

    /**
     * 이벤트 리스너를 바인딩한다.
     */
    ChatView.prototype._bind = function () {
        var self = this;
        var els = self._els;

        // 입력 필드 값 변경 → 전송 버튼 활성화 제어 + 자동 높이 조정
        var onInput = function () {
            els.sendBtn.disabled = self._isSending || !els.chatInput.value.trim();
            els.chatInput.style.height = "auto";
            els.chatInput.style.height = Math.min(els.chatInput.scrollHeight, 120) + "px";
        };
        els.chatInput.addEventListener("input", onInput);
        self._listeners.push({ el: els.chatInput, type: "input", fn: onInput });

        // 키보드 이벤트: Enter 전송, Shift+Enter 줄바꿈
        // 한국어 IME composing 처리
        var onKeydown = function (e) {
            // IME 조합 중이면 무시 (한국어 입력 시 Enter가 조합 확정에 사용됨)
            if (e.isComposing || e.keyCode === 229) return;

            if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                if (!self._isSending && els.chatInput.value.trim()) {
                    self._sendMessage();
                }
            }
        };
        els.chatInput.addEventListener("keydown", onKeydown);
        self._listeners.push({ el: els.chatInput, type: "keydown", fn: onKeydown });

        // 전송 버튼 클릭
        var onSend = function () {
            if (!self._isSending && els.chatInput.value.trim()) {
                self._sendMessage();
            }
        };
        els.sendBtn.addEventListener("click", onSend);
        self._listeners.push({ el: els.sendBtn, type: "click", fn: onSend });

        // 취소 버튼 클릭
        var onCancel = function () {
            self._cancelSending();
        };
        els.cancelBtn.addEventListener("click", onCancel);
        self._listeners.push({ el: els.cancelBtn, type: "click", fn: onCancel });

        // 대화 초기화
        var onClear = function () {
            self._clearChat();
        };
        els.btnClearChat.addEventListener("click", onClear);
        self._listeners.push({ el: els.btnClearChat, type: "click", fn: onClear });

        // WebSocket 이벤트: 새 회의 추가/완료 시 드롭다운 자동 갱신
        var onJobCompleted = function () {
            self._refreshMeetingFilter();
        };
        document.addEventListener("ws:job_completed", onJobCompleted);
        self._listeners.push({ el: document, type: "ws:job_completed", fn: onJobCompleted });

        var onJobAdded = function () {
            self._refreshMeetingFilter();
        };
        document.addEventListener("ws:job_added", onJobAdded);
        self._listeners.push({ el: document, type: "ws:job_added", fn: onJobAdded });

        // 입력에 포커스
        els.chatInput.focus();
    };

    /**
     * 회의 목록을 드롭다운에 로드한다.
     */
    ChatView.prototype._loadMeetingList = async function () {
        var self = this;
        var els = self._els;
        try {
            var data = await App.apiRequest("/meetings");
            var meetings = data.meetings || [];

            meetings.forEach(function (meeting) {
                var option = document.createElement("option");
                option.value = meeting.meeting_id;

                var statusLabel = {
                    completed: "\u2713",
                    recorded: "\u25CF",
                    recording: "\u25CF",
                    transcribing: "\u2022",
                    diarizing: "\u2022",
                    merging: "\u2022",
                    embedding: "\u2022",
                    queued: "\u2022",
                    failed: "\u2717",
                };
                var icon = statusLabel[meeting.status] || "";
                option.textContent = icon + " " + meeting.meeting_id;
                els.meetingFilter.appendChild(option);
            });
        } catch (e) {
            console.warn("회의 목록 로드 실패:", e.message);
        }
    };

    /**
     * 회의 필터 드롭다운을 갱신한다.
     */
    ChatView.prototype._refreshMeetingFilter = function () {
        var self = this;
        var els = self._els;
        var currentValue = els.meetingFilter.value;
        els.meetingFilter.innerHTML = '<option value="">전체 회의</option>';
        self._loadMeetingList().then(function () {
            if (currentValue) {
                els.meetingFilter.value = currentValue;
            }
        });
    };

    /**
     * 환영 메시지를 숨긴다.
     */
    ChatView.prototype._hideWelcome = function () {
        if (this._els.welcomeMessage) {
            this._els.welcomeMessage.style.display = "none";
        }
    };

    /**
     * 사용자 메시지를 추가한다.
     * @param {string} text - 메시지 텍스트
     */
    ChatView.prototype._addUserMessage = function (text) {
        this._hideWelcome();
        this._messageCount++;

        var msg = document.createElement("div");
        msg.className = "message user";

        var avatar = document.createElement("div");
        avatar.className = "message-avatar";
        avatar.innerHTML = Icons.person;

        var body = document.createElement("div");
        body.className = "message-body";

        var bubble = document.createElement("div");
        bubble.className = "message-bubble";
        bubble.textContent = text;

        body.appendChild(bubble);
        msg.appendChild(avatar);
        msg.appendChild(body);
        this._els.messagesArea.appendChild(msg);

        this._scrollToBottom();
    };

    /**
     * AI 답변 메시지를 추가한다.
     * @param {Object} data - 응답 데이터
     */
    ChatView.prototype._addAssistantMessage = function (data) {
        var self = this;
        self._messageCount++;

        var msg = document.createElement("div");
        msg.className = "message assistant";

        // 아바타
        var avatar = document.createElement("div");
        avatar.className = "message-avatar";
        avatar.textContent = "\uD83E\uDD16";

        // 메시지 본체
        var body = document.createElement("div");
        body.className = "message-body";

        // 답변 버블
        var bubble = document.createElement("div");
        bubble.className = "message-bubble";
        bubble.innerHTML = App.renderMarkdown(data.answer);

        body.appendChild(bubble);

        // LLM 미사용 경고
        if (!data.llm_used && data.error_message) {
            var notice = document.createElement("div");
            notice.className = "llm-fallback-notice";
            notice.textContent = "\u26A0 응답을 받지 못했어요: " + data.error_message;
            body.appendChild(notice);
        }

        // 참조 출처
        if (data.references && data.references.length > 0) {
            var refsSection = document.createElement("div");
            refsSection.className = "references";

            var refsTitle = document.createElement("div");
            refsTitle.className = "references-title";
            refsTitle.innerHTML = Icons.clip + ' 참조 출처 (' + data.references.length + '건)';
            refsSection.appendChild(refsTitle);

            data.references.forEach(function (ref, index) {
                var card = document.createElement("a");
                card.className = "ref-card";
                // SPA 내비게이션으로 뷰어 이동
                card.href = "/app/viewer/" + encodeURIComponent(ref.meeting_id);
                card.addEventListener("click", function (e) {
                    e.preventDefault();
                    Router.navigate("/app/viewer/" + encodeURIComponent(ref.meeting_id));
                });

                // 인덱스
                var indexEl = document.createElement("span");
                indexEl.className = "ref-card-index";
                indexEl.textContent = "[" + (index + 1) + "]";

                // 본체
                var bodyEl = document.createElement("div");
                bodyEl.className = "ref-card-body";

                // REFERENCE 오버라인 (레퍼런스 Chat.jsx 기준 citation 문서화)
                var overline = document.createElement("div");
                overline.className = "overline ref-card-overline";
                overline.textContent = "REFERENCE";
                bodyEl.appendChild(overline);

                // 메타 정보
                var meta = document.createElement("div");
                meta.className = "ref-card-meta";

                var meetingSpan = document.createElement("span");
                meetingSpan.textContent = ref.meeting_id;

                var dateSpan = document.createElement("span");
                dateSpan.textContent = ref.date || "";

                var speakersSpan = document.createElement("span");
                speakersSpan.textContent = (ref.speakers || []).join(", ");

                var timeSpan = document.createElement("span");
                timeSpan.textContent = App.formatTime(ref.start_time) + "~" + App.formatTime(ref.end_time);

                meta.appendChild(meetingSpan);
                if (ref.date) meta.appendChild(dateSpan);
                if (ref.speakers && ref.speakers.length) meta.appendChild(speakersSpan);
                meta.appendChild(timeSpan);

                // 미리보기
                var preview = document.createElement("div");
                preview.className = "ref-card-preview";
                preview.textContent = ref.text_preview || "";

                bodyEl.appendChild(meta);
                bodyEl.appendChild(preview);

                // 점수
                var scoreEl = document.createElement("span");
                scoreEl.className = "ref-card-score";
                scoreEl.textContent = (ref.score * 100).toFixed(0) + "%";

                card.appendChild(indexEl);
                card.appendChild(bodyEl);
                card.appendChild(scoreEl);
                refsSection.appendChild(card);
            });

            body.appendChild(refsSection);
        }

        // 복사 버튼
        var actions = document.createElement("div");
        actions.className = "message-actions";
        var copyBtn = document.createElement("button");
        copyBtn.className = "btn-copy";
        copyBtn.innerHTML = Icons.copy + ' 복사';
        copyBtn.setAttribute("aria-label", "답변 복사");
        copyBtn.addEventListener("click", function () {
            var textToCopy = data.answer || bubble.textContent;
            App.copyToClipboard(textToCopy).then(function (ok) {
                if (ok) {
                    copyBtn.innerHTML = Icons.check + ' 복사됨';
                    copyBtn.classList.add("copied");
                    setTimeout(function () {
                        copyBtn.innerHTML = Icons.copy + ' 복사';
                        copyBtn.classList.remove("copied");
                    }, 2000);
                }
            });
        });
        actions.appendChild(copyBtn);
        body.appendChild(actions);

        msg.appendChild(avatar);
        msg.appendChild(body);
        self._els.messagesArea.appendChild(msg);

        self._scrollToBottom();
    };

    /**
     * 메시지 영역을 맨 아래로 스크롤한다.
     */
    ChatView.prototype._scrollToBottom = function () {
        var messagesArea = this._els.messagesArea;
        requestAnimationFrame(function () {
            messagesArea.scrollTop = messagesArea.scrollHeight;
        });
    };

    /**
     * 전송 상태를 설정한다.
     * @param {boolean} sending - 전송 중 여부
     */
    ChatView.prototype._setSending = function (sending) {
        var els = this._els;
        this._isSending = sending;
        els.sendBtn.disabled = sending || !els.chatInput.value.trim();
        els.chatInput.disabled = sending;
        els.typingIndicator.classList.toggle("visible", sending);

        if (sending) {
            els.sendBtn.style.display = "none";
            els.cancelBtn.classList.add("visible");
            this._scrollToBottom();
        } else {
            els.sendBtn.style.display = "";
            els.cancelBtn.classList.remove("visible");
        }
    };

    /**
     * 채팅 메시지를 전송한다.
     */
    ChatView.prototype._sendMessage = async function () {
        var self = this;
        var els = self._els;
        var query = els.chatInput.value.trim();
        if (!query) return;

        // 입력 초기화
        els.chatInput.value = "";
        els.chatInput.style.height = "auto";
        errorBanner.hide();

        // 사용자 메시지 표시
        self._addUserMessage(query);

        // 전송 상태
        self._setSending(true);

        // AbortController 생성
        self._currentAbortController = new AbortController();

        try {
            var meetingIdFilter = els.meetingFilter.value || null;

            var result = await App.apiRequest("/chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    query: query,
                    session_id: self._sessionId,
                    meeting_id_filter: meetingIdFilter,
                    date_filter: null,
                    speaker_filter: null,
                }),
                signal: self._currentAbortController.signal,
            });

            // 빈 응답 처리
            if (!result || (!result.answer && (!result.references || result.references.length === 0))) {
                result = result || {};
                result.answer = result.answer || "관련 회의 내용을 찾을 수 없습니다. 다른 키워드로 질문해 보세요.";
            }

            // AI 답변 표시
            self._addAssistantMessage(result);

        } catch (e) {
            // 사용자가 직접 취소한 경우
            if (e.name === "AbortError") return;

            if (e.status === 503) {
                errorBanner.show("아직 답변 준비가 덜 됐어요. 잠시 후 다시 시도해 주세요.");
            } else if (e.status === 400) {
                errorBanner.show("입력 내용을 확인해 주세요: " + e.message);
            } else if (e.status === 0) {
                errorBanner.show("서버에 연결할 수 없습니다. 네트워크 상태를 확인해 주세요.");
            } else {
                errorBanner.show("답변 생성에 실패했습니다. 잠시 후 다시 시도해 주세요.");
            }
        } finally {
            self._currentAbortController = null;
            self._setSending(false);
            els.chatInput.focus();
        }
    };

    /**
     * 진행 중인 AI 응답 요청을 취소한다.
     */
    ChatView.prototype._cancelSending = function () {
        if (this._currentAbortController) {
            this._currentAbortController.abort();
            this._currentAbortController = null;
        }
        this._setSending(false);
        this._els.chatInput.focus();
    };

    /**
     * 대화를 초기화한다.
     */
    ChatView.prototype._clearChat = function () {
        var self = this;
        var els = self._els;

        // 새 세션 ID 생성
        self._sessionId = self._generateSessionId();

        // 메시지 영역 초기화
        els.messagesArea.innerHTML = "";
        self._messageCount = 0;

        // 환영 메시지 복원
        els.messagesArea.innerHTML = self._createWelcomeHtml();
        els.welcomeMessage = document.getElementById("chatWelcomeMessage");

        errorBanner.hide();
        els.chatInput.focus();
    };

    /**
     * 뷰를 정리한다.
     */
    ChatView.prototype.destroy = function () {
        // 진행 중인 요청 취소
        if (this._currentAbortController) {
            this._currentAbortController.abort();
            this._currentAbortController = null;
        }

        // 이벤트 리스너 해제
        this._listeners.forEach(function (entry) {
            entry.el.removeEventListener(entry.type, entry.fn);
        });
        this._listeners = [];

        // 타이머 해제
        this._timers.forEach(function (t) { clearInterval(t); clearTimeout(t); });
        this._timers = [];

        // chat-mode 클래스 제거 (리스트 패널 복원)
        var listPanel = document.getElementById("list-panel");
        if (listPanel) listPanel.classList.remove("chat-mode");

        // 페이지 타이틀 복원
        document.title = "회의록 · Recap";
    };


    // =================================================================
    // === SettingsView — 설정 셸 + 3개 탭 (일반 / 프롬프트 / 용어집) ===
    // =================================================================
    //
    // 구조:
    //   SettingsView (셸)
    //     ├─ 헤더 + segmented control (탭)
    //     └─ 활성 패널 렌더링 영역
    //          ├─ GeneralPanel      (기존 config.yaml 조회/수정)
    //          ├─ PromptsPanel      (/api/prompts)
    //          └─ VocabularyPanel   (/api/vocabulary)
    //
    // 각 패널은 독립된 생명주기: _render / _bind / isDirty / destroy.
    // 탭 전환 시 현재 패널이 dirty 이면 confirm 으로 이탈 확인.
    // 페이지 레벨 이탈은 Router.navigate의 canLeave 훅에서 처리.
    // =================================================================

    /**
     * 설정 셸 뷰. 탭으로 하위 패널을 스위칭한다.
     * @param {{initialTab: string}} opts
     * @constructor
     */
    function SettingsView(opts) {
        var self = this;
        self._opts = opts || { initialTab: "general" };
        self._currentPanel = null;
        self._currentTab = null;
        self._render();
        self._showTab(self._opts.initialTab || "general");
        document.title = "설정 · Recap";

        // 브라우저 탭 닫기/새로고침 가드: 편집 중이면 네이티브 경고 표시
        self._beforeUnloadHandler = function (e) {
            if (
                self._currentPanel &&
                typeof self._currentPanel.isDirty === "function" &&
                self._currentPanel.isDirty()
            ) {
                e.preventDefault();
                // 최신 브라우저는 커스텀 메시지 무시하고 기본 경고만 표시
                e.returnValue = "";
                return "";
            }
        };
        window.addEventListener("beforeunload", self._beforeUnloadHandler);
    }

    SettingsView.prototype._render = function () {
        var contentEl = Router.getContentEl();
        contentEl.innerHTML = [
            '<div class="settings-view">',
            '  <div class="settings-header">',
            '    <h2 class="settings-title">설정</h2>',
            '  </div>',
            '  <div class="settings-tabs" role="tablist" aria-label="설정 카테고리">',
            '    <button type="button" class="settings-tab" data-tab="general" role="tab">일반</button>',
            '    <button type="button" class="settings-tab" data-tab="prompts" role="tab">프롬프트</button>',
            '    <button type="button" class="settings-tab" data-tab="vocabulary" role="tab">용어집</button>',
            '  </div>',
            '  <div class="settings-panel-host" id="settingsPanelHost" role="tabpanel"></div>',
            '</div>',
        ].join("\n");

        var self = this;
        var tabs = contentEl.querySelectorAll(".settings-tab");
        Array.prototype.forEach.call(tabs, function (btn) {
            btn.addEventListener("click", function () {
                self._showTab(btn.getAttribute("data-tab"));
            });
        });
    };

    SettingsView.prototype._showTab = function (name) {
        if (this._currentTab === name) return;

        // 현재 패널이 dirty이면 이탈 확인
        if (
            this._currentPanel &&
            typeof this._currentPanel.isDirty === "function" &&
            this._currentPanel.isDirty()
        ) {
            var ok = window.confirm(
                "저장하지 않은 변경사항이 있어요. 이동하시겠어요?"
            );
            if (!ok) return;
        }

        // 기존 패널 정리
        if (this._currentPanel && typeof this._currentPanel.destroy === "function") {
            this._currentPanel.destroy();
        }
        this._currentPanel = null;

        // 탭 버튼 활성 상태
        var tabs = document.querySelectorAll(".settings-tab");
        Array.prototype.forEach.call(tabs, function (btn) {
            var isActive = btn.getAttribute("data-tab") === name;
            btn.classList.toggle("active", isActive);
            btn.setAttribute("aria-selected", isActive ? "true" : "false");
        });

        // URL 동기화 (general 은 기본 경로)
        var targetPath = name === "general" ? "/app/settings" : "/app/settings/" + name;
        if (window.location.pathname !== targetPath) {
            history.replaceState(null, "", targetPath);
        }

        var host = document.getElementById("settingsPanelHost");
        host.innerHTML = "";

        // 패널 생성
        if (name === "general") {
            this._currentPanel = new GeneralSettingsPanel(host);
        } else if (name === "prompts") {
            this._currentPanel = new PromptsSettingsPanel(host);
        } else if (name === "vocabulary") {
            this._currentPanel = new VocabularySettingsPanel(host);
        } else {
            this._currentPanel = new GeneralSettingsPanel(host);
            name = "general";
        }
        this._currentTab = name;
    };

    SettingsView.prototype.canLeave = function () {
        if (
            this._currentPanel &&
            typeof this._currentPanel.isDirty === "function" &&
            this._currentPanel.isDirty()
        ) {
            return window.confirm(
                "저장하지 않은 변경사항이 있어요. 떠나시겠어요?"
            );
        }
        return true;
    };

    SettingsView.prototype.destroy = function () {
        if (this._currentPanel && typeof this._currentPanel.destroy === "function") {
            this._currentPanel.destroy();
        }
        this._currentPanel = null;
        if (this._beforeUnloadHandler) {
            window.removeEventListener("beforeunload", this._beforeUnloadHandler);
            this._beforeUnloadHandler = null;
        }
        var listPanel = document.getElementById("list-panel");
        if (listPanel) listPanel.classList.remove("chat-mode");
        document.title = "회의록 · Recap";
    };


    // =================================================================
    // === AbTestListView (A/B 테스트 목록, /app/ab-test) ===
    // =================================================================

    /**
     * A/B 테스트 목록 뷰.
     * @constructor
     */
    function AbTestListView() {
        var self = this;
        self._listeners = [];
        self._timers = [];
        self._render();
        self._loadTests();
        document.title = "A/B 테스트 · Recap";
    }

    /**
     * 목록 뷰 DOM을 생성한다.
     */
    AbTestListView.prototype._render = function () {
        var contentEl = Router.getContentEl();
        contentEl.innerHTML = [
            '<div class="ab-test-view">',
            '  <div class="ab-test-view-header">',
            '    <h2 class="ab-test-view-title">A/B 모델 테스트</h2>',
            '    <button class="ab-test-new-btn" id="abNewBtn">+ 새 테스트</button>',
            '  </div>',
            '  <div class="ab-test-list" id="abTestList"></div>',
            '</div>',
        ].join("\n");

        var self = this;
        var newBtn = document.getElementById("abNewBtn");
        if (newBtn) {
            var onNew = function () { Router.navigate("/app/ab-test/new"); };
            newBtn.addEventListener("click", onNew);
            self._listeners.push({ el: newBtn, type: "click", fn: onNew });
        }
    };

    /**
     * 테스트 목록을 API에서 로드한다.
     */
    AbTestListView.prototype._loadTests = async function () {
        var self = this;
        var listEl = document.getElementById("abTestList");
        if (!listEl) return;

        try {
            var data = await App.apiRequest("/ab-tests");
            var tests = data.tests || [];
            self._renderList(tests, listEl);
        } catch (e) {
            listEl.innerHTML = '<div class="ab-test-empty"><div class="ab-test-empty-text">목록을 불러올 수 없습니다</div></div>';
        }
    };

    /**
     * 테스트 목록을 렌더링한다.
     * @param {Array} tests - 테스트 목록
     * @param {HTMLElement} listEl - 목록 컨테이너
     */
    AbTestListView.prototype._renderList = function (tests, listEl) {
        var self = this;
        listEl.innerHTML = "";

        if (tests.length === 0) {
            listEl.innerHTML = [
                '<div class="ab-test-empty">',
                '  <div class="ab-test-empty-icon">&#x1F9EA;</div>',
                '  <div class="ab-test-empty-text">아직 A/B 테스트가 없습니다</div>',
                '  <div class="ab-test-empty-sub">설정 → 고급 기능에서 시작하세요.</div>',
                '</div>',
            ].join("\n");
            return;
        }

        tests.forEach(function (test) {
            var card = document.createElement("div");
            card.className = "ab-test-card";

            var typeClass = (test.test_type || "llm").toLowerCase();
            var typeLabel = typeClass === "stt" ? "STT" : "LLM";
            var statusClass = (test.status || "pending").replace(/ /g, "_");

            // 한국어 상태 라벨
            var statusLabels = {
                pending: "대기 중",
                running: "진행 중",
                completed: "완료",
                failed: "실패",
                cancelled: "취소됨",
                partial_failed: "부분 실패",
            };
            var statusText = statusLabels[statusClass] || statusClass;

            var variantALabel = (test.variant_a && test.variant_a.label) || "모델 A";
            var variantBLabel = (test.variant_b && test.variant_b.label) || "모델 B";
            var createdAt = test.started_at || test.created_at || "";
            var dateStr = createdAt ? App.formatDate(createdAt) : "";

            card.innerHTML = [
                '<span class="ab-test-type-badge ' + App.escapeHtml(typeClass) + '">' + App.escapeHtml(typeLabel) + '</span>',
                '<div class="ab-test-card-body">',
                '  <div class="ab-test-card-title">' + App.escapeHtml(variantALabel) + ' vs ' + App.escapeHtml(variantBLabel) + '</div>',
                '  <div class="ab-test-card-meta">',
                '    소스: ' + App.escapeHtml(test.source_meeting_id || "-"),
                dateStr ? (' · ' + App.escapeHtml(dateStr)) : '',
                '  </div>',
                '</div>',
                '<span class="ab-test-status ' + App.escapeHtml(statusClass) + '">' + App.escapeHtml(statusText) + '</span>',
                '<button class="ab-test-card-delete" data-test-id="' + App.escapeHtml(test.test_id) + '">삭제</button>',
            ].join("");

            // 카드 클릭 → 결과 뷰
            var onCardClick = function (e) {
                if (e.target.classList.contains("ab-test-card-delete")) return;
                Router.navigate("/app/ab-test/" + encodeURIComponent(test.test_id));
            };
            card.addEventListener("click", onCardClick);
            self._listeners.push({ el: card, type: "click", fn: onCardClick });

            // 삭제 버튼
            var delBtn = card.querySelector(".ab-test-card-delete");
            if (delBtn) {
                var onDel = function (e) {
                    e.stopPropagation();
                    self._deleteTest(test.test_id);
                };
                delBtn.addEventListener("click", onDel);
                self._listeners.push({ el: delBtn, type: "click", fn: onDel });
            }

            listEl.appendChild(card);
        });
    };

    /**
     * 테스트를 삭제한다.
     * @param {string} testId - 테스트 ID
     */
    AbTestListView.prototype._deleteTest = async function (testId) {
        if (!window.confirm("이 A/B 테스트를 삭제하시겠습니까?")) return;
        try {
            await App.apiDelete("/ab-tests/" + encodeURIComponent(testId));
            this._loadTests();
        } catch (e) {
            errorBanner.show("삭제 실패: " + (e.message || "알 수 없는 오류"));
        }
    };

    /**
     * 뷰 정리.
     */
    AbTestListView.prototype.destroy = function () {
        var i;
        for (i = 0; i < this._listeners.length; i++) {
            var l = this._listeners[i];
            l.el.removeEventListener(l.type, l.fn);
        }
        this._listeners = [];
        for (i = 0; i < this._timers.length; i++) {
            clearInterval(this._timers[i]);
        }
        this._timers = [];
        var listPanel = document.getElementById("list-panel");
        if (listPanel) listPanel.classList.remove("chat-mode");
    };


    // =================================================================
    // === AbTestNewView (A/B 테스트 생성, /app/ab-test/new) ===
    // =================================================================

    /**
     * A/B 테스트 생성 폼 뷰.
     * @constructor
     */
    function AbTestNewView() {
        var self = this;
        self._listeners = [];
        self._timers = [];
        self._testType = "llm";    // "llm" 또는 "stt"
        self._meetings = [];       // 전체 회의 목록 (필터 전)
        self._sttModels = [];

        // LLM 프리셋 목록 — API 에서 로드 (available 필드로 로컬 보유 여부 판별)
        self._llmPresets = [];

        // URL 쿼리 파라미터 파싱
        var params = new URLSearchParams(window.location.search);
        self._sourceParam = params.get("source") || "";
        var typeParam = params.get("type");
        if (typeParam === "stt") self._testType = "stt";

        self._render();
        self._bind();
        self._loadData();
        document.title = "새 A/B 테스트 · Recap";
    }

    /**
     * 생성 폼 DOM을 생성한다.
     */
    AbTestNewView.prototype._render = function () {
        var contentEl = Router.getContentEl();
        contentEl.innerHTML = [
            '<div class="ab-new-form">',
            '  <h2>새 A/B 테스트</h2>',

            // 유형 선택 탭
            '  <div class="ab-type-tabs">',
            '    <button class="ab-type-tab active" data-type="llm" id="abTypeLlm">LLM 비교</button>',
            '    <button class="ab-type-tab" data-type="stt" id="abTypeStt">STT 비교</button>',
            '  </div>',

            // 소스 회의 선택
            '  <div class="ab-form-section">',
            '    <label class="ab-form-label">소스 회의</label>',
            '    <select class="ab-form-select" id="abSourceMeeting">',
            '      <option value="">회의를 선택하세요...</option>',
            '    </select>',
            '  </div>',

            // 모델 A
            '  <div class="ab-model-selector">',
            '    <div class="ab-model-selector-title">모델 A</div>',
            '    <select class="ab-form-select" id="abModelASelect"></select>',
            '    <input class="ab-form-input" id="abModelACustom" placeholder="HuggingFace repo ID 입력" style="display:none;margin-top:6px;" />',
            '  </div>',

            // 모델 B
            '  <div class="ab-model-selector">',
            '    <div class="ab-model-selector-title">모델 B</div>',
            '    <select class="ab-form-select" id="abModelBSelect"></select>',
            '    <input class="ab-form-input" id="abModelBCustom" placeholder="HuggingFace repo ID 입력" style="display:none;margin-top:6px;" />',
            '  </div>',

            // LLM 옵션
            '  <div id="abLlmOptions">',
            '    <div class="ab-form-section">',
            '      <label class="ab-form-label">LLM 범위</label>',
            '      <div class="ab-form-checkbox-row"><input type="checkbox" id="abScopeCorrect" checked /><span>교정</span></div>',
            '      <div class="ab-form-checkbox-row"><input type="checkbox" id="abScopeSummarize" checked /><span>요약</span></div>',
            '    </div>',
            '  </div>',

            // STT 옵션
            '  <div id="abSttOptions" style="display:none;">',
            '    <div class="ab-form-section">',
            '      <div class="ab-form-checkbox-row"><input type="checkbox" id="abAllowDiarize" checked /><span>화자분리 자동 실행 (미전사 회의는 필수)</span></div>',
            '      <div class="ab-form-hint">체크포인트가 없을 때만 필요합니다</div>',
            '    </div>',
            '  </div>',

            // 경고 메시지
            '  <div class="ab-form-warning" id="abFormWarning" style="display:none;"></div>',

            // 액션 버튼
            '  <div class="ab-form-actions">',
            '    <button class="ab-form-submit" id="abSubmitBtn" disabled>테스트 시작</button>',
            '    <button class="ab-form-cancel" id="abCancelBtn">취소</button>',
            '  </div>',
            '</div>',
        ].join("\n");
    };

    /**
     * 이벤트 바인딩.
     */
    AbTestNewView.prototype._bind = function () {
        var self = this;

        // 유형 탭 전환
        var typeTabs = document.querySelectorAll(".ab-type-tab");
        Array.prototype.forEach.call(typeTabs, function (tab) {
            var onTab = function () {
                self._testType = tab.getAttribute("data-type");
                Array.prototype.forEach.call(typeTabs, function (t) {
                    t.classList.toggle("active", t === tab);
                });
                self._updateTypeUI();
                // 탭 전환 시 소스 드롭다운도 유형에 맞게 재구성
                self._updateSourceDropdown();
                self._populateModelSelects();
                self._validate();
            };
            tab.addEventListener("click", onTab);
            self._listeners.push({ el: tab, type: "click", fn: onTab });
        });

        // 모델 선택 변경
        var selectors = ["abModelASelect", "abModelBSelect"];
        selectors.forEach(function (id) {
            var sel = document.getElementById(id);
            if (!sel) return;
            var customId = id.replace("Select", "Custom");
            var onChange = function () {
                var customEl = document.getElementById(customId);
                if (customEl) {
                    customEl.style.display = sel.value === "__custom__" ? "block" : "none";
                }
                self._validate();
            };
            sel.addEventListener("change", onChange);
            self._listeners.push({ el: sel, type: "change", fn: onChange });
        });

        // 커스텀 입력 변경 시 유효성 검증
        var customInputs = ["abModelACustom", "abModelBCustom"];
        customInputs.forEach(function (id) {
            var inp = document.getElementById(id);
            if (!inp) return;
            var onInput = function () { self._validate(); };
            inp.addEventListener("input", onInput);
            self._listeners.push({ el: inp, type: "input", fn: onInput });
        });

        // 소스 회의 변경
        var srcSel = document.getElementById("abSourceMeeting");
        if (srcSel) {
            var onSrc = function () { self._validate(); };
            srcSel.addEventListener("change", onSrc);
            self._listeners.push({ el: srcSel, type: "change", fn: onSrc });
        }

        // 제출
        var submitBtn = document.getElementById("abSubmitBtn");
        if (submitBtn) {
            var onSubmit = function () { self._submit(); };
            submitBtn.addEventListener("click", onSubmit);
            self._listeners.push({ el: submitBtn, type: "click", fn: onSubmit });
        }

        // 취소
        var cancelBtn = document.getElementById("abCancelBtn");
        if (cancelBtn) {
            var onCancel = function () { Router.navigate("/app/ab-test"); };
            cancelBtn.addEventListener("click", onCancel);
            self._listeners.push({ el: cancelBtn, type: "click", fn: onCancel });
        }
    };

    /**
     * 회의 목록 + STT 모델 목록을 로드한다.
     */
    AbTestNewView.prototype._loadData = async function () {
        var self = this;

        try {
            var data = await App.apiRequest("/meetings");
            // 전체 회의 목록을 저장해두고 필터링은 _updateSourceDropdown 에서 수행
            self._meetings = data.meetings || [];
        } catch (e) {
            self._meetings = [];
        }

        try {
            var sttData = await App.apiRequest("/stt-models");
            self._sttModels = sttData || [];
            // sttData 가 배열이 아닌 경우(객체 래핑) 처리
            if (!Array.isArray(self._sttModels) && self._sttModels.models) {
                self._sttModels = self._sttModels.models;
            }
        } catch (e) {
            self._sttModels = [];
        }

        // LLM 프리셋 목록 (로컬 보유 여부 포함)
        try {
            var llmData = await App.apiRequest("/llm-models/available");
            self._llmPresets = (Array.isArray(llmData) ? llmData : []).map(function (m) {
                return { label: m.label, id: m.model_id, available: m.available };
            });
        } catch (e) {
            // API 실패 시 폴백: 빈 목록 (사용자 정의 입력만 가능)
            self._llmPresets = [];
        }

        // 유형 UI 업데이트 + 소스 드롭다운 + 모델 셀렉트 채우기
        if (self._testType === "stt") {
            var sttTab = document.getElementById("abTypeStt");
            var llmTab = document.getElementById("abTypeLlm");
            if (sttTab) sttTab.classList.add("active");
            if (llmTab) llmTab.classList.remove("active");
        }
        self._updateTypeUI();
        self._updateSourceDropdown();
        self._populateModelSelects();
        self._validate();
    };

    /**
     * 현재 테스트 유형에 맞게 소스 회의 드롭다운을 다시 채운다.
     *
     * - LLM 비교: merge.json 이 필요하므로 status === "completed" 인 회의만 표시
     * - STT 비교: input.wav 만 있으면 되므로 "recorded" 이상(recorded, transcribing,
     *             completed) 인 회의도 표시
     */
    AbTestNewView.prototype._updateSourceDropdown = function () {
        var self = this;
        var srcSel = document.getElementById("abSourceMeeting");
        if (!srcSel) return;

        // 현재 선택값을 보존해두고 재구성 후 복원한다
        var prevValue = srcSel.value;

        // 기존 옵션 초기화 (placeholder 포함 전체 재구성)
        srcSel.innerHTML = "";
        var placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "회의 선택...";
        srcSel.appendChild(placeholder);

        // 유형별 필터: LLM 은 completed 만, STT 는 wav 파일이 있는 상태(recorded 이상)
        var filtered = self._meetings.filter(function (m) {
            if (self._testType === "llm") {
                return m.status === "completed";
            } else {
                // recorded / transcribing / completed 모두 허용
                return m.status === "completed" ||
                       m.status === "recorded" ||
                       m.status === "transcribing";
            }
        });

        filtered.forEach(function (m) {
            var opt = document.createElement("option");
            opt.value = m.meeting_id;
            opt.textContent = App.extractMeetingTitle ? App.extractMeetingTitle(m) : m.meeting_id;
            srcSel.appendChild(opt);
        });

        // 이전 선택값 또는 URL source 파라미터 복원
        var restoreValue = prevValue || self._sourceParam;
        if (restoreValue) {
            srcSel.value = restoreValue;
        }
    };

    /**
     * 유형에 따라 옵션 영역을 토글한다.
     */
    AbTestNewView.prototype._updateTypeUI = function () {
        var llmOpts = document.getElementById("abLlmOptions");
        var sttOpts = document.getElementById("abSttOptions");
        if (llmOpts) llmOpts.style.display = this._testType === "llm" ? "block" : "none";
        if (sttOpts) sttOpts.style.display = this._testType === "stt" ? "block" : "none";
    };

    /**
     * 현재 유형에 맞게 모델 드롭다운을 채운다.
     */
    AbTestNewView.prototype._populateModelSelects = function () {
        var self = this;
        var selectIds = ["abModelASelect", "abModelBSelect"];

        selectIds.forEach(function (id) {
            var sel = document.getElementById(id);
            if (!sel) return;
            sel.innerHTML = "";

            var placeholder = document.createElement("option");
            placeholder.value = "";
            placeholder.textContent = "모델 선택...";
            sel.appendChild(placeholder);

            if (self._testType === "llm") {
                self._llmPresets.forEach(function (p) {
                    var opt = document.createElement("option");
                    opt.value = p.id;
                    if (p.available === false) {
                        opt.textContent = p.label + " (다운로드 필요)";
                        opt.disabled = true;
                        opt.style.color = "var(--text-muted)";
                    } else {
                        opt.textContent = p.label;
                    }
                    sel.appendChild(opt);
                });
                var customOpt = document.createElement("option");
                customOpt.value = "__custom__";
                customOpt.textContent = "사용자 정의...";
                sel.appendChild(customOpt);
            } else {
                // STT 모델
                self._sttModels.forEach(function (m) {
                    var opt = document.createElement("option");
                    opt.value = m.id || m.model_id || "";
                    opt.textContent = m.label || m.id || "";
                    sel.appendChild(opt);
                });
            }

            // 커스텀 필드 숨김 리셋
            var customId = id.replace("Select", "Custom");
            var customEl = document.getElementById(customId);
            if (customEl) customEl.style.display = "none";
        });
    };

    /**
     * 폼 유효성 검증. 제출 버튼 활성화/비활성화.
     */
    AbTestNewView.prototype._validate = function () {
        var submitBtn = document.getElementById("abSubmitBtn");
        var warningEl = document.getElementById("abFormWarning");
        if (!submitBtn || !warningEl) return;

        var srcSel = document.getElementById("abSourceMeeting");
        var source = srcSel ? srcSel.value : "";
        var modelA = this._getModelId("A");
        var modelB = this._getModelId("B");

        var warning = "";
        var valid = true;

        if (!source) { valid = false; }
        if (!modelA || !modelB) { valid = false; }
        if (modelA && modelB && modelA === modelB) {
            warning = "서로 다른 모델을 선택하세요";
            valid = false;
        }

        warningEl.textContent = warning;
        warningEl.style.display = warning ? "block" : "none";
        submitBtn.disabled = !valid;
    };

    /**
     * 선택된 모델 ID를 반환한다.
     * @param {string} variant - "A" 또는 "B"
     * @returns {string}
     */
    AbTestNewView.prototype._getModelId = function (variant) {
        var suffix = variant === "A" ? "A" : "B";
        var sel = document.getElementById("abModel" + suffix + "Select");
        if (!sel) return "";
        var val = sel.value;
        if (val === "__custom__") {
            var custom = document.getElementById("abModel" + suffix + "Custom");
            return custom ? custom.value.trim() : "";
        }
        return val;
    };

    /**
     * 선택된 모델 라벨을 반환한다.
     * @param {string} variant - "A" 또는 "B"
     * @returns {string}
     */
    AbTestNewView.prototype._getModelLabel = function (variant) {
        var suffix = variant === "A" ? "A" : "B";
        var sel = document.getElementById("abModel" + suffix + "Select");
        if (!sel) return "";
        if (sel.value === "__custom__") {
            return this._getModelId(variant);
        }
        var selectedOption = sel.options[sel.selectedIndex];
        return selectedOption ? selectedOption.textContent : sel.value;
    };

    /**
     * 테스트를 시작한다.
     */
    AbTestNewView.prototype._submit = async function () {
        var self = this;
        var submitBtn = document.getElementById("abSubmitBtn");
        if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = "시작 중..."; }

        var srcSel = document.getElementById("abSourceMeeting");
        var source = srcSel ? srcSel.value : "";
        var modelAId = self._getModelId("A");
        var modelBId = self._getModelId("B");
        var modelALabel = self._getModelLabel("A");
        var modelBLabel = self._getModelLabel("B");

        var endpoint, body;

        if (self._testType === "llm") {
            var scopeCorrect = document.getElementById("abScopeCorrect");
            var scopeSummarize = document.getElementById("abScopeSummarize");
            endpoint = "/ab-tests/llm";
            body = {
                source_meeting_id: source,
                variant_a: { label: modelALabel, model_id: modelAId, backend: "mlx" },
                variant_b: { label: modelBLabel, model_id: modelBId, backend: "mlx" },
                scope: {
                    correct: scopeCorrect ? scopeCorrect.checked : true,
                    summarize: scopeSummarize ? scopeSummarize.checked : true,
                },
            };
        } else {
            var allowDiarize = document.getElementById("abAllowDiarize");
            endpoint = "/ab-tests/stt";
            body = {
                source_meeting_id: source,
                variant_a: { label: modelALabel, model_id: modelAId },
                variant_b: { label: modelBLabel, model_id: modelBId },
                allow_diarize_rerun: allowDiarize ? allowDiarize.checked : false,
            };
        }

        try {
            var result = await App.apiPost(endpoint, body);
            var testId = result.test_id;
            if (testId) {
                Router.navigate("/app/ab-test/" + encodeURIComponent(testId));
            } else {
                Router.navigate("/app/ab-test");
            }
        } catch (e) {
            errorBanner.show("테스트 시작 실패: " + (e.message || "알 수 없는 오류"));
            if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = "테스트 시작"; }
        }
    };

    /**
     * 뷰 정리.
     */
    AbTestNewView.prototype.destroy = function () {
        var i;
        for (i = 0; i < this._listeners.length; i++) {
            var l = this._listeners[i];
            l.el.removeEventListener(l.type, l.fn);
        }
        this._listeners = [];
        for (i = 0; i < this._timers.length; i++) {
            clearInterval(this._timers[i]);
        }
        this._timers = [];
        var listPanel = document.getElementById("list-panel");
        if (listPanel) listPanel.classList.remove("chat-mode");
    };


    // =================================================================
    // === AbTestResultView (A/B 테스트 결과, /app/ab-test/:testId) ===
    // =================================================================

    /**
     * A/B 테스트 결과 뷰 (진행 상태 + 비교).
     * @param {string} testId - 테스트 ID
     * @constructor
     */
    function AbTestResultView(testId) {
        var self = this;
        self._testId = testId;
        self._listeners = [];
        self._timers = [];
        self._data = null;
        self._activeTab = "correct"; // "correct" | "summary" | "transcript"
        self._elapsedBase = 0;       // 서버 경과 시간 (초)
        self._elapsedWall = 0;       // 로컬 싱크 시점 (ms)
        self._elapsedTimer = null;

        self._render();
        self._loadData();
        self._bindWs();
        document.title = "A/B 테스트 · Recap";
    }

    /**
     * 결과 뷰 DOM 을 생성한다.
     */
    AbTestResultView.prototype._render = function () {
        var contentEl = Router.getContentEl();
        contentEl.innerHTML = [
            '<div class="ab-result-view">',
            '  <div class="ab-result-header" id="abResultHeader"></div>',
            '  <div id="abProgressSection"></div>',
            '  <div id="abCompareSection"></div>',
            '</div>',
        ].join("\n");
    };

    /**
     * 테스트 데이터를 로드한다.
     */
    AbTestResultView.prototype._loadData = async function () {
        var self = this;
        // 서버가 pending metadata 를 쓰기 전에 GET 이 도착할 경우를 대비해
        // 최대 3회까지 500ms 간격으로 재시도한다 (백그라운드 태스크 race condition 방어).
        var maxRetries = 3;
        var retryDelay = 500; // ms
        var attempt = 0;
        var lastError = null;
        while (attempt <= maxRetries) {
            try {
                var data = await App.apiRequest("/ab-tests/" + encodeURIComponent(self._testId));
                self._data = data;
                self._renderHeader(data);
                var status = (data.metadata && data.metadata.status) || data.status || "";
                if (status === "running" || status === "pending") {
                    self._renderProgress(data);
                    self._startPolling();
                } else {
                    self._renderCompare(data);
                }
                return;
            } catch (e) {
                lastError = e;
                // 404 이면 재시도, 그 외 에러는 즉시 중단
                var isNotFound = e && (e.status === 404 || (e.message && e.message.indexOf("404") !== -1));
                if (!isNotFound || attempt >= maxRetries) {
                    break;
                }
                attempt++;
                await new Promise(function (resolve) { setTimeout(resolve, retryDelay); });
            }
        }
        // 재시도 소진 또는 404 외 에러
        var headerEl = document.getElementById("abResultHeader");
        if (headerEl) {
            headerEl.innerHTML = '<div class="ab-test-empty"><div class="ab-test-empty-text">테스트를 찾을 수 없습니다</div></div>';
        }
    };

    /**
     * 헤더 메타데이터를 렌더링한다.
     * @param {Object} d - 테스트 데이터
     */
    AbTestResultView.prototype._renderHeader = function (d) {
        var headerEl = document.getElementById("abResultHeader");
        if (!headerEl) return;

        var meta = d.metadata || d;
        var typeLabel = (meta.test_type || "llm") === "stt" ? "STT" : "LLM";
        var typeClass = (meta.test_type || "llm").toLowerCase();
        var va = meta.variant_a || {};
        var vb = meta.variant_b || {};
        var sourceId = meta.source_meeting_id || "-";

        headerEl.innerHTML = [
            '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">',
            '  <a href="javascript:void(0)" id="abBackLink" style="color:var(--accent);font-size:13px;">← 목록</a>',
            '</div>',
            '<h2 class="ab-result-title">',
            '  <span class="ab-test-type-badge ' + App.escapeHtml(typeClass) + '">' + App.escapeHtml(typeLabel) + '</span> ',
            App.escapeHtml(va.label || "A") + ' vs ' + App.escapeHtml(vb.label || "B"),
            '</h2>',
            '<div class="ab-result-meta">',
            '  <span>소스: <a href="/app/viewer/' + App.escapeHtml(encodeURIComponent(sourceId)) + '">' + App.escapeHtml(sourceId) + '</a></span>',
            '  <span>상태: <span class="ab-test-status ' + App.escapeHtml(meta.status || "pending") + '">' + App.escapeHtml(meta.status || "pending") + '</span></span>',
            '</div>',
        ].join("\n");

        var self = this;
        var backLink = document.getElementById("abBackLink");
        if (backLink) {
            var onBack = function () { Router.navigate("/app/ab-test"); };
            backLink.addEventListener("click", onBack);
            self._listeners.push({ el: backLink, type: "click", fn: onBack });
        }
    };

    /**
     * 진행 상태를 렌더링한다.
     * @param {Object} d - 테스트 데이터
     */
    AbTestResultView.prototype._renderProgress = function (d) {
        var self = this;
        var section = document.getElementById("abProgressSection");
        if (!section) return;

        var meta = d.metadata || d;
        var currentVariant = meta.current_variant || "-";
        var currentStep = meta.current_step || "-";
        var pct = meta.progress_pct || 0;

        // 단계 한국어 라벨
        var stepLabels = {
            transcribe: "전사 중",
            correct: "교정 중",
            summarize: "요약 중",
            merge: "병합 중",
            diarize: "화자분리 중",
        };
        var stepText = stepLabels[currentStep] || currentStep;

        section.innerHTML = [
            '<div class="ab-progress-section">',
            '  <div class="ab-progress-variant">',
            '    <div class="ab-progress-variant-label">Variant A: ' + App.escapeHtml((meta.variant_a || {}).label || "A") + '</div>',
            '    <div class="ab-progress-bar"><div class="ab-progress-bar-fill" id="abProgressA" style="width:' + (currentVariant === "B" ? "100" : pct) + '%;"></div></div>',
            '    <div class="ab-progress-step-text" id="abStepA">' + (currentVariant === "A" ? App.escapeHtml(stepText) : (currentVariant === "B" ? "완료" : "대기 중")) + '</div>',
            '  </div>',
            '  <div class="ab-progress-variant">',
            '    <div class="ab-progress-variant-label">Variant B: ' + App.escapeHtml((meta.variant_b || {}).label || "B") + '</div>',
            '    <div class="ab-progress-bar"><div class="ab-progress-bar-fill" id="abProgressB" style="width:' + (currentVariant === "B" ? pct : "0") + '%;"></div></div>',
            '    <div class="ab-progress-step-text" id="abStepB">' + (currentVariant === "B" ? App.escapeHtml(stepText) : "대기 중") + '</div>',
            '  </div>',
            '  <div class="ab-progress-elapsed" id="abElapsed"></div>',
            '  <div class="ab-progress-actions">',
            '    <button class="ab-form-cancel" id="abCancelTestBtn">취소</button>',
            '  </div>',
            '</div>',
        ].join("\n");

        // 경과 시간 카운터
        if (meta.started_at) {
            var startMs = new Date(meta.started_at).getTime();
            self._elapsedBase = Math.floor((Date.now() - startMs) / 1000);
            self._elapsedWall = Date.now();
            self._renderElapsed();
            if (self._elapsedTimer) clearInterval(self._elapsedTimer);
            self._elapsedTimer = setInterval(function () { self._renderElapsed(); }, 1000);
            self._timers.push(self._elapsedTimer);
        }

        // 취소 버튼
        var cancelBtn = document.getElementById("abCancelTestBtn");
        if (cancelBtn) {
            var onCancel = function () {
                self._cancelTest();
            };
            cancelBtn.addEventListener("click", onCancel);
            self._listeners.push({ el: cancelBtn, type: "click", fn: onCancel });
        }
    };

    /**
     * 경과 시간 표시를 업데이트한다.
     */
    AbTestResultView.prototype._renderElapsed = function () {
        var el = document.getElementById("abElapsed");
        if (!el) return;
        var elapsedMs = Date.now() - this._elapsedWall;
        var sec = this._elapsedBase + Math.floor(elapsedMs / 1000);
        var m = Math.floor(sec / 60);
        var s = sec % 60;
        var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
        el.textContent = "경과 시간: " + pad(m) + ":" + pad(s);
    };

    /**
     * 3초 간격 폴링을 시작한다.
     */
    AbTestResultView.prototype._startPolling = function () {
        var self = this;
        var timer = setInterval(function () {
            self._pollStatus();
        }, 3000);
        self._timers.push(timer);
    };

    /**
     * 상태를 폴링한다.
     */
    AbTestResultView.prototype._pollStatus = async function () {
        var self = this;
        try {
            var data = await App.apiRequest("/ab-tests/" + encodeURIComponent(self._testId));
            self._data = data;
            var meta = data.metadata || data;

            if (meta.status === "completed" || meta.status === "failed" || meta.status === "cancelled" || meta.status === "partial_failed") {
                // 폴링 중단 + 비교 뷰 렌더
                self._stopTimers();
                self._renderHeader(data);
                var progressSection = document.getElementById("abProgressSection");
                if (progressSection) progressSection.innerHTML = "";
                self._renderCompare(data);
                return;
            }

            // 진행 중 — UI 업데이트
            self._updateProgress(meta);
        } catch (e) {
            // 무시
        }
    };

    /**
     * 진행 UI 를 업데이트한다.
     * @param {Object} meta - 메타데이터
     */
    AbTestResultView.prototype._updateProgress = function (meta) {
        var currentVariant = meta.current_variant || "-";
        var currentStep = meta.current_step || "-";
        var pct = meta.progress_pct || 0;

        var stepLabels = {
            transcribe: "전사 중", correct: "교정 중", summarize: "요약 중",
            merge: "병합 중", diarize: "화자분리 중",
        };
        var stepText = stepLabels[currentStep] || currentStep;

        var progressA = document.getElementById("abProgressA");
        var progressB = document.getElementById("abProgressB");
        var stepA = document.getElementById("abStepA");
        var stepB = document.getElementById("abStepB");

        if (progressA) {
            progressA.style.width = (currentVariant === "B" ? "100" : pct) + "%";
            if (currentVariant === "B") progressA.classList.add("done");
        }
        if (progressB) {
            progressB.style.width = (currentVariant === "B" ? pct : "0") + "%";
        }
        if (stepA) {
            stepA.textContent = currentVariant === "A" ? stepText : (currentVariant === "B" ? "완료" : "대기 중");
        }
        if (stepB) {
            stepB.textContent = currentVariant === "B" ? stepText : "대기 중";
        }
    };

    /**
     * WebSocket step_progress 이벤트를 구독한다.
     */
    AbTestResultView.prototype._bindWs = function () {
        var self = this;
        var onStepProgress = function (e) {
            var detail = e.detail || {};
            // A/B 테스트 이벤트가 아니면 무시
            if (detail.ab_test_id !== self._testId) return;
            var meta = self._data ? (self._data.metadata || self._data) : {};
            if (detail.variant) meta.current_variant = detail.variant;
            if (detail.step) meta.current_step = detail.step;
            if (detail.progress_pct != null) meta.progress_pct = detail.progress_pct;
            self._updateProgress(meta);
        };
        document.addEventListener("ws:step_progress", onStepProgress);
        self._listeners.push({ el: document, type: "ws:step_progress", fn: onStepProgress });
    };

    /**
     * 테스트를 취소한다.
     */
    AbTestResultView.prototype._cancelTest = async function () {
        try {
            await App.apiPost("/ab-tests/" + encodeURIComponent(this._testId) + "/cancel", {});
            this._pollStatus();
        } catch (e) {
            errorBanner.show("취소 실패: " + (e.message || ""));
        }
    };

    /**
     * 비교 뷰를 렌더링한다.
     * @param {Object} data - 전체 테스트 데이터
     */
    AbTestResultView.prototype._renderCompare = function (data) {
        var self = this;
        var section = document.getElementById("abCompareSection");
        if (!section) return;

        var meta = data.metadata || data;
        var testType = meta.test_type || "llm";

        // 탭 결정
        // STT/LLM 모두: "최종 결과" (diff 없이 깔끔하게) + "상세 비교" (diff 하이라이트)
        var tabs = [];
        if (testType === "llm") {
            tabs.push({ key: "clean", label: "최종 결과" });
            tabs.push({ key: "diff", label: "상세 비교" });
            tabs.push({ key: "summary", label: "요약" });
        } else {
            tabs.push({ key: "clean", label: "최종 결과" });
            tabs.push({ key: "diff", label: "상세 비교" });
        }

        self._activeTab = tabs[0].key;

        // 탭 HTML
        var tabsHtml = '<div class="ab-compare-tabs">';
        tabs.forEach(function (t) {
            tabsHtml += '<button class="ab-compare-tab' + (t.key === self._activeTab ? ' active' : '') + '" data-tab="' + t.key + '">' + App.escapeHtml(t.label) + '</button>';
        });
        tabsHtml += '</div>';

        section.innerHTML = tabsHtml + '<div id="abCompareContent"></div><div id="abMetricsSection"></div>';

        // 탭 클릭
        var tabBtns = section.querySelectorAll(".ab-compare-tab");
        Array.prototype.forEach.call(tabBtns, function (btn) {
            var onTab = function () {
                self._activeTab = btn.getAttribute("data-tab");
                Array.prototype.forEach.call(tabBtns, function (b) {
                    b.classList.toggle("active", b === btn);
                });
                self._renderCompareContent(data);
            };
            btn.addEventListener("click", onTab);
            self._listeners.push({ el: btn, type: "click", fn: onTab });
        });

        self._renderCompareContent(data);
        self._renderMetrics(data);
    };

    /**
     * 비교 콘텐츠를 탭에 따라 렌더링한다.
     * @param {Object} data - 전체 테스트 데이터
     */
    AbTestResultView.prototype._renderCompareContent = function (data) {
        var self = this;
        var container = document.getElementById("abCompareContent");
        if (!container) return;

        var meta = data.metadata || data;
        var va = data.variant_a || {};
        var vb = data.variant_b || {};
        var vaLabel = (meta.variant_a || {}).label || "A";
        var vbLabel = (meta.variant_b || {}).label || "B";

        if (self._activeTab === "clean") {
            self._renderCleanCompare(container, data, vaLabel, vbLabel);
        } else if (self._activeTab === "diff") {
            self._renderDiffCompare(container, data, vaLabel, vbLabel);
        } else if (self._activeTab === "summary") {
            self._renderSummaryCompare(container, vaLabel, vbLabel);
        }
    };

    // ================================================================
    // "최종 결과" 탭 — diff 없이, 좌우 최종 전사/교정문을 깔끔하게 나란히
    // ================================================================
    AbTestResultView.prototype._renderCleanCompare = function (container, data, vaLabel, vbLabel) {
        var meta = data.metadata || data;
        var va = data.variant_a || {};
        var vb = data.variant_b || {};
        var isLlm = meta.test_type === "llm";

        // LLM: correct.utterances, STT: transcribe.utterances
        var uttA = isLlm
            ? ((va.correct && va.correct.utterances) || [])
            : ((va.transcribe && (va.transcribe.utterances || va.transcribe.segments)) || []);
        var uttB = isLlm
            ? ((vb.correct && vb.correct.utterances) || [])
            : ((vb.transcribe && (vb.transcribe.utterances || vb.transcribe.segments)) || []);

        // 타임스탬프 기반 매칭 (±3초 허용)
        var paired = _pairByTimestamp(uttA, uttB, 3.0);

        // 통계
        var stats = { matched: 0, aOnly: 0, bOnly: 0 };
        paired.forEach(function (p) {
            if (p.a && p.b) stats.matched++;
            else if (p.a) stats.aOnly++;
            else stats.bOnly++;
        });

        var statsHtml = '<div class="ab-stats-bar">' +
            '<span class="ab-stat">공통 ' + stats.matched + '개</span>' +
            '<span class="ab-stat ab-stat-a">' + vaLabel + '만 ' + stats.aOnly + '개</span>' +
            '<span class="ab-stat ab-stat-b">' + vbLabel + '만 ' + stats.bOnly + '개</span>' +
            '</div>';

        // 행 렌더링 — diff 없이 순수 텍스트만
        var rows = [];
        paired.forEach(function (pair) {
            var a = pair.a || {};
            var b = pair.b || {};
            var textA = a.text || "";
            var textB = b.text || "";
            var timeA = a.start != null ? _fmtTime(a.start) : "";
            var timeB = b.start != null ? _fmtTime(b.start) : "";
            var speakerA = a.speaker || "";
            var speakerB = b.speaker || "";

            // 한쪽만 있는 발화: 빈 칸 + 회색 "발화 없음" 표시
            var cellA, cellB;
            if (!pair.a) {
                cellA = '<div class="ab-utterance-cell ab-cell-empty"><span class="ab-empty-label">발화 없음</span></div>';
            } else {
                cellA = '<div class="ab-utterance-cell">' +
                    '<div class="ab-utterance-meta"><span class="ab-utterance-time">' + App.escapeHtml(timeA) + '</span>' +
                    (speakerA ? ' <span class="ab-utterance-speaker">' + App.escapeHtml(speakerA) + '</span>' : '') + '</div>' +
                    '<div class="ab-utterance-text">' + App.escapeHtml(textA) + '</div></div>';
            }
            if (!pair.b) {
                cellB = '<div class="ab-utterance-cell ab-cell-empty"><span class="ab-empty-label">발화 없음</span></div>';
            } else {
                cellB = '<div class="ab-utterance-cell">' +
                    '<div class="ab-utterance-meta"><span class="ab-utterance-time">' + App.escapeHtml(timeB) + '</span>' +
                    (speakerB ? ' <span class="ab-utterance-speaker">' + App.escapeHtml(speakerB) + '</span>' : '') + '</div>' +
                    '<div class="ab-utterance-text">' + App.escapeHtml(textB) + '</div></div>';
            }

            // 양쪽 텍스트가 동일하면 배경 없음, 다르면 연한 노란색
            var rowClass = "ab-utterance-row";
            if (pair.a && pair.b && textA !== textB) rowClass += " ab-row-differ";
            if (!pair.a || !pair.b) rowClass += " ab-row-missing";

            rows.push('<div class="' + rowClass + '">' + cellA + cellB + '</div>');
        });

        container.innerHTML = [
            '<div class="ab-compare-header-bar">',
            '  <div class="ab-compare-header">' + App.escapeHtml(vaLabel) + ' (' + uttA.length + '개)</div>',
            '  <div class="ab-compare-header">' + App.escapeHtml(vbLabel) + ' (' + uttB.length + '개)</div>',
            '</div>',
            statsHtml,
            '<div class="ab-utterance-list">',
            rows.join(""),
            '</div>',
        ].join("\n");
    };

    // ================================================================
    // "상세 비교" 탭 — 매칭된 발화만, 단어 수준 diff 표시
    // ================================================================
    AbTestResultView.prototype._renderDiffCompare = function (container, data, vaLabel, vbLabel) {
        var meta = data.metadata || data;
        var va = data.variant_a || {};
        var vb = data.variant_b || {};
        var isLlm = meta.test_type === "llm";

        var uttA = isLlm
            ? ((va.correct && va.correct.utterances) || [])
            : ((va.transcribe && (va.transcribe.utterances || va.transcribe.segments)) || []);
        var uttB = isLlm
            ? ((vb.correct && vb.correct.utterances) || [])
            : ((vb.transcribe && (vb.transcribe.utterances || vb.transcribe.segments)) || []);

        var paired = _pairByTimestamp(uttA, uttB, 3.0);

        // 매칭됐고 텍스트가 다른 쌍만 추출
        var diffPairs = [];
        var identicalCount = 0;
        paired.forEach(function (pair) {
            if (!pair.a || !pair.b) return; // 한쪽만 있는 건 "최종 결과" 탭에서 확인
            var textA = pair.a.text || "";
            var textB = pair.b.text || "";
            if (textA === textB) {
                identicalCount++;
                return;
            }
            diffPairs.push(pair);
        });

        var stats = { matched: paired.filter(function (p) { return p.a && p.b; }).length, different: diffPairs.length, identical: identicalCount };

        var statsHtml = '<div class="ab-stats-bar">' +
            '<span class="ab-stat">매칭 ' + stats.matched + '개</span>' +
            '<span class="ab-stat ab-stat-identical">동일 ' + stats.identical + '개</span>' +
            '<span class="ab-stat ab-stat-diff">차이 ' + stats.different + '개</span>' +
            '</div>';

        if (diffPairs.length === 0) {
            container.innerHTML = statsHtml +
                '<div class="ab-empty-diff">매칭된 발화 중 텍스트 차이가 없습니다.</div>';
            return;
        }

        var rows = [];
        diffPairs.forEach(function (pair, idx) {
            var a = pair.a;
            var b = pair.b;
            var textA = a.text || "";
            var textB = b.text || "";
            var timeA = a.start != null ? _fmtTime(a.start) : "";
            var timeB = b.start != null ? _fmtTime(b.start) : "";

            var diffs = _diffWords(textA, textB);

            rows.push(
                '<div class="ab-diff-row">' +
                '  <div class="ab-diff-num">' + (idx + 1) + '</div>' +
                '  <div class="ab-diff-pair">' +
                '    <div class="ab-diff-cell ab-diff-cell-a">' +
                '      <span class="ab-utterance-time">' + App.escapeHtml(timeA) + '</span> ' +
                       _renderDiffA(diffs) +
                '    </div>' +
                '    <div class="ab-diff-cell ab-diff-cell-b">' +
                '      <span class="ab-utterance-time">' + App.escapeHtml(timeB) + '</span> ' +
                       _renderDiffB(diffs) +
                '    </div>' +
                '  </div>' +
                '</div>'
            );
        });

        container.innerHTML = [
            '<div class="ab-compare-header-bar">',
            '  <div class="ab-compare-header">' + App.escapeHtml(vaLabel) + '</div>',
            '  <div class="ab-compare-header">' + App.escapeHtml(vbLabel) + '</div>',
            '</div>',
            statsHtml,
            '<div class="ab-diff-list">',
            rows.join(""),
            '</div>',
        ].join("\n");
    };

    /**
     * 요약 비교 렌더링. 양쪽 요약 마크다운을 로드한다.
     */
    AbTestResultView.prototype._renderSummaryCompare = async function (container, vaLabel, vbLabel) {
        var self = this;
        container.innerHTML = [
            '<div class="ab-compare-container">',
            '  <div class="ab-compare-panel">',
            '    <div class="ab-compare-header">' + App.escapeHtml(vaLabel) + '</div>',
            '    <div class="ab-compare-body" id="abSummaryA">불러오는 중...</div>',
            '  </div>',
            '  <div class="ab-compare-panel">',
            '    <div class="ab-compare-header">' + App.escapeHtml(vbLabel) + '</div>',
            '    <div class="ab-compare-body" id="abSummaryB">불러오는 중...</div>',
            '  </div>',
            '</div>',
        ].join("\n");

        var baseUrl = "/ab-tests/" + encodeURIComponent(self._testId) + "/variant/";
        var summaryA = "";
        var summaryB = "";

        try {
            var respA = await App.apiRequest(baseUrl + "a/summary");
            summaryA = typeof respA === "string" ? respA : (respA.summary || respA.text || JSON.stringify(respA));
        } catch (e) { summaryA = "(로드 실패)"; }

        try {
            var respB = await App.apiRequest(baseUrl + "b/summary");
            summaryB = typeof respB === "string" ? respB : (respB.summary || respB.text || JSON.stringify(respB));
        } catch (e) { summaryB = "(로드 실패)"; }

        var elA = document.getElementById("abSummaryA");
        var elB = document.getElementById("abSummaryB");

        if (elA) elA.innerHTML = _highlightForbidden(App.renderMarkdown(summaryA));
        if (elB) elB.innerHTML = _highlightForbidden(App.renderMarkdown(summaryB));

        // 동기 스크롤
        if (elA && elB) {
            var syncing = false;
            var onScrollA = function () {
                if (syncing) return;
                syncing = true;
                elB.scrollTop = elA.scrollTop;
                syncing = false;
            };
            var onScrollB = function () {
                if (syncing) return;
                syncing = true;
                elA.scrollTop = elB.scrollTop;
                syncing = false;
            };
            elA.addEventListener("scroll", onScrollA);
            elB.addEventListener("scroll", onScrollB);
            self._listeners.push({ el: elA, type: "scroll", fn: onScrollA });
            self._listeners.push({ el: elB, type: "scroll", fn: onScrollB });
        }
    };

    // _renderTranscriptCompare 삭제됨 → _renderCleanCompare / _renderDiffCompare 로 통합

    /**
     * 메트릭 비교 카드를 렌더링한다.
     * @param {Object} data - 전체 테스트 데이터
     */
    AbTestResultView.prototype._renderMetrics = function (data) {
        var section = document.getElementById("abMetricsSection");
        if (!section) return;

        var va = data.variant_a || {};
        var vb = data.variant_b || {};
        var metricsA = va.metrics || {};
        var metricsB = vb.metrics || {};
        var meta = data.metadata || data;

        var elapsed_a = metricsA.elapsed_seconds ? (metricsA.elapsed_seconds.total || 0) : 0;
        var elapsed_b = metricsB.elapsed_seconds ? (metricsB.elapsed_seconds.total || 0) : 0;
        var chars_a = metricsA.char_count ? (metricsA.char_count.correct || metricsA.char_count.transcribe || 0) : 0;
        var chars_b = metricsB.char_count ? (metricsB.char_count.correct || metricsB.char_count.transcribe || 0) : 0;
        var forbidden_a = metricsA.forbidden_patterns ? (metricsA.forbidden_patterns.total || 0) : 0;
        var forbidden_b = metricsB.forbidden_patterns ? (metricsB.forbidden_patterns.total || 0) : 0;

        // 승패 판정 (단순 점수: 처리시간 짧을수록 +1, 금지 패턴 적을수록 +1)
        var scoreA = 0;
        var scoreB = 0;
        if (elapsed_a < elapsed_b) scoreA++;
        else if (elapsed_b < elapsed_a) scoreB++;
        if (forbidden_a < forbidden_b) scoreA++;
        else if (forbidden_b < forbidden_a) scoreB++;

        var judgeText = scoreA > scoreB ? "참고 스코어: A 우세 (" + scoreA + " vs " + scoreB + ")"
            : scoreB > scoreA ? "참고 스코어: B 우세 (" + scoreB + " vs " + scoreA + ")"
            : "참고 스코어: 무승부 (" + scoreA + " vs " + scoreB + ")";

        section.innerHTML = [
            '<div class="ab-metrics-grid">',
            _metricCard("처리시간", elapsed_a.toFixed(1) + "s", elapsed_b.toFixed(1) + "s", elapsed_a <= elapsed_b ? "A" : "B"),
            _metricCard("글자수", chars_a.toLocaleString(), chars_b.toLocaleString(), null),
            _metricCard("금지 패턴 수",
                forbidden_a.toString(), forbidden_b.toString(),
                forbidden_a <= forbidden_b ? "A" : "B",
                forbidden_a > 0 || forbidden_b > 0),
            '</div>',
            '<div class="ab-judge-note">' + App.escapeHtml(judgeText) + '<br><span style="font-size:11px;">자동 판정은 참고용입니다</span></div>',
            (meta.status === "partial_failed" ? '<div class="ab-form-warning" style="margin-top:12px;">일부 variant 실행이 실패했습니다. 결과가 불완전할 수 있습니다.</div>' : ''),
        ].join("\n");
    };

    /**
     * 타이머를 모두 중단한다.
     */
    AbTestResultView.prototype._stopTimers = function () {
        for (var i = 0; i < this._timers.length; i++) {
            clearInterval(this._timers[i]);
        }
        this._timers = [];
        if (this._elapsedTimer) {
            clearInterval(this._elapsedTimer);
            this._elapsedTimer = null;
        }
    };

    /**
     * 뷰 정리.
     */
    AbTestResultView.prototype.destroy = function () {
        this._stopTimers();
        var i;
        for (i = 0; i < this._listeners.length; i++) {
            var l = this._listeners[i];
            l.el.removeEventListener(l.type, l.fn);
        }
        this._listeners = [];
        var listPanel = document.getElementById("list-panel");
        if (listPanel) listPanel.classList.remove("chat-mode");
    };


    // =================================================================
    // === A/B 테스트 유틸리티 함수 (diff, 금지 패턴, 타임스탬프 매칭) ===
    // =================================================================

    /**
     * 두 문자열의 단어 배열 LCS diff를 수행한다.
     * @param {string} a - 원본 텍스트 (A)
     * @param {string} b - 비교 텍스트 (B)
     * @returns {Array} [{type: "equal"|"added"|"removed", text: "..."}]
     */
    function _diffWords(a, b) {
        var wordsA = (a || "").split(/\s+/).filter(function (w) { return w.length > 0; });
        var wordsB = (b || "").split(/\s+/).filter(function (w) { return w.length > 0; });

        if (wordsA.length === 0 && wordsB.length === 0) return [];
        if (wordsA.length === 0) {
            return wordsB.map(function (w) { return { type: "added", text: w }; });
        }
        if (wordsB.length === 0) {
            return wordsA.map(function (w) { return { type: "removed", text: w }; });
        }

        // LCS DP 테이블 구축
        var n = wordsA.length;
        var m = wordsB.length;
        var dp = [];
        var i, j;
        for (i = 0; i <= n; i++) {
            dp[i] = [];
            for (j = 0; j <= m; j++) {
                dp[i][j] = 0;
            }
        }
        for (i = 1; i <= n; i++) {
            for (j = 1; j <= m; j++) {
                if (wordsA[i - 1] === wordsB[j - 1]) {
                    dp[i][j] = dp[i - 1][j - 1] + 1;
                } else {
                    dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
                }
            }
        }

        // 역추적으로 diff 생성
        var result = [];
        i = n;
        j = m;
        while (i > 0 || j > 0) {
            if (i > 0 && j > 0 && wordsA[i - 1] === wordsB[j - 1]) {
                result.unshift({ type: "equal", text: wordsA[i - 1] });
                i--;
                j--;
            } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
                result.unshift({ type: "added", text: wordsB[j - 1] });
                j--;
            } else {
                result.unshift({ type: "removed", text: wordsA[i - 1] });
                i--;
            }
        }

        return result;
    }

    /**
     * diff 결과를 A측 HTML로 렌더링한다 (removed는 빨간 취소선, added는 무시).
     * @param {Array} diffs - diff 결과
     * @returns {string} HTML
     */
    function _renderDiffA(diffs) {
        var parts = [];
        diffs.forEach(function (d) {
            if (d.type === "equal") {
                parts.push(App.escapeHtml(d.text));
            } else if (d.type === "removed") {
                parts.push('<span class="ab-diff-removed">' + App.escapeHtml(d.text) + '</span>');
            }
            // added 는 A 측에 표시하지 않음
        });
        return parts.join(" ");
    }

    /**
     * diff 결과를 B측 HTML로 렌더링한다 (added는 초록 밑줄, removed는 무시).
     * @param {Array} diffs - diff 결과
     * @returns {string} HTML
     */
    function _renderDiffB(diffs) {
        var parts = [];
        diffs.forEach(function (d) {
            if (d.type === "equal") {
                parts.push(App.escapeHtml(d.text));
            } else if (d.type === "added") {
                parts.push('<span class="ab-diff-added">' + App.escapeHtml(d.text) + '</span>');
            }
            // removed 는 B 측에 표시하지 않음
        });
        return parts.join(" ");
    }

    /**
     * 금지 패턴(SPEAKER_XX, UNKNOWN, 한글(English))을 하이라이트한다.
     * @param {string} html - 렌더링된 HTML
     * @returns {string} 하이라이트된 HTML
     */
    function _highlightForbidden(html) {
        // SPEAKER_XX 패턴
        html = html.replace(/(SPEAKER_\d+)/g, '<mark class="forbidden-pattern">$1</mark>');
        // UNKNOWN 패턴
        html = html.replace(/(UNKNOWN)/g, '<mark class="forbidden-pattern">$1</mark>');
        // 한글(English) 패턴 — 한글 뒤에 괄호 안 영어
        html = html.replace(/([\uAC00-\uD7A3]+)\(([A-Za-z]+)\)/g,
            '<mark class="forbidden-pattern">$1($2)</mark>');
        return html;
    }

    /**
     * 타임스탬프 기반 발화 매칭 (STT 비교용).
     * 가까운 시간끼리 쌍을 만든다.
     * @param {Array} uttA - A 발화 리스트
     * @param {Array} uttB - B 발화 리스트
     * @returns {Array} [{a: ..., b: ...}]
     */
    /**
     * 타임스탬프 기반 양방향 발화 매칭.
     *
     * A 와 B 를 시간순으로 병합하되, tolerance 초 이내의 발화를 같은 쌍으로 묶는다.
     * 한쪽에만 있는 발화도 시간순 위치에 삽입되어 UI 에서 자연스러운 정렬을 보장.
     *
     * @param {Array} uttA - A 발화 리스트
     * @param {Array} uttB - B 발화 리스트
     * @param {number} [tolerance=3.0] - 매칭 허용 시간 차이(초)
     * @returns {Array<{a: object|null, b: object|null}>}
     */
    function _pairByTimestamp(uttA, uttB, tolerance) {
        if (tolerance == null) tolerance = 3.0;

        var result = [];
        var usedB = {};  // 이미 매칭된 B 인덱스
        var idxB = 0;

        // 1단계: A 기준으로 B 에서 가장 가까운 매칭 찾기
        uttA.forEach(function (a) {
            var startA = a.start || 0;
            var bestIdx = -1;
            var bestDist = Infinity;

            for (var j = Math.max(0, idxB - 2); j < uttB.length; j++) {
                if (usedB[j]) continue;
                var dist = Math.abs((uttB[j].start || 0) - startA);
                if (dist < bestDist) {
                    bestDist = dist;
                    bestIdx = j;
                }
                // 정렬 가정: 너무 멀어지면 중단
                if ((uttB[j].start || 0) - startA > tolerance * 3) break;
            }

            if (bestIdx >= 0 && bestDist <= tolerance) {
                usedB[bestIdx] = true;
                result.push({ a: a, b: uttB[bestIdx], time: startA });
                idxB = bestIdx + 1;
            } else {
                result.push({ a: a, b: null, time: startA });
            }
        });

        // 2단계: B 에서 미매칭된 발화를 시간순 위치에 삽입
        for (var k = 0; k < uttB.length; k++) {
            if (usedB[k]) continue;
            result.push({ a: null, b: uttB[k], time: uttB[k].start || 0 });
        }

        // 3단계: 시간순 정렬
        result.sort(function (x, y) { return (x.time || 0) - (y.time || 0); });

        return result;
    }

    /**
     * 초를 mm:ss 형식으로 변환한다.
     * @param {number} sec - 초
     * @returns {string}
     */
    function _fmtTime(sec) {
        var s = Math.floor(sec || 0);
        var m = Math.floor(s / 60);
        var r = s % 60;
        var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
        return pad(m) + ":" + pad(r);
    }

    /**
     * 메트릭 카드 HTML을 생성한다.
     * @param {string} title - 카드 제목
     * @param {string} valA - A 값
     * @param {string} valB - B 값
     * @param {string|null} winner - "A", "B", 또는 null
     * @param {boolean} isWarn - 경고 스타일 여부
     * @returns {string} HTML
     */
    function _metricCard(title, valA, valB, winner, isWarn) {
        var classA = winner === "A" ? " winner" : "";
        var classB = winner === "B" ? " winner" : "";
        if (isWarn) {
            if (parseInt(valA, 10) > 0) classA = " warn";
            if (parseInt(valB, 10) > 0) classB = " warn";
            if (parseInt(valA, 10) === 0) classA = " good";
            if (parseInt(valB, 10) === 0) classB = " good";
        }
        return [
            '<div class="ab-metric-card">',
            '  <div class="ab-metric-card-title">' + App.escapeHtml(title) + '</div>',
            '  <div class="ab-metric-card-values">',
            '    <div><div class="ab-metric-value' + classA + '">' + App.escapeHtml(valA) + '</div><div class="ab-metric-label">A</div></div>',
            '    <div><div class="ab-metric-value' + classB + '">' + App.escapeHtml(valB) + '</div><div class="ab-metric-label">B</div></div>',
            '  </div>',
            '</div>',
        ].join("");
    }


    // =================================================================
    // === GeneralSettingsPanel (기존 /api/settings) ===
    // =================================================================

    /**
     * 일반 설정 패널: LLM 모델, 백엔드, 온도, STT 언어 등 config.yaml 기반 설정.
     * 기존 SettingsView의 본문을 그대로 옮겨온 것.
     * @param {HTMLElement} host
     * @constructor
     */
    function GeneralSettingsPanel(host) {
        var self = this;
        self._host = host;
        self._els = {};
        self._listeners = [];
        self._render();
        self._bind();
        self._loadSettings();
    }

    GeneralSettingsPanel.prototype._render = function () {
        this._host.innerHTML = [
            '<div class="settings-content">',
            '  <section class="settings-section">',
            '    <h3 class="settings-section-title">LLM 모델</h3>',
            '    <div class="settings-group">',
            '      <div class="setting-row">',
            '        <label class="setting-label" for="settingsModel">모델</label>',
            '        <div class="setting-control-with-help">',
            '          <select class="setting-select" id="settingsModel"></select>',
            '          <span class="setting-help" id="settingsModelHelp" tabindex="0" role="button" aria-label="현재 선택된 모델 설명 보기"',
            '                data-tooltip="모델을 선택하면 설명이 여기에 표시됩니다.">?</span>',
            '        </div>',
            '      </div>',
            '      <div class="setting-row">',
            '        <label class="setting-label" for="settingsBackend">백엔드</label>',
            '        <select class="setting-select" id="settingsBackend">',
            '          <option value="mlx">MLX (기본, in-process)</option>',
            '          <option value="ollama">Ollama (외부 서버)</option>',
            '        </select>',
            '      </div>',
            '      <div class="setting-row">',
            '        <label class="setting-label" for="settingsTemp">Temperature</label>',
            '        <div class="setting-slider-group">',
            '          <input type="range" class="setting-slider" id="settingsTemp" min="0" max="2" step="0.1">',
            '          <span class="setting-slider-value" id="settingsTempValue">0.3</span>',
            '        </div>',
            '      </div>',
            '    </div>',
            '  </section>',
            '  <section class="settings-section">',
            '    <h3 class="settings-section-title">파이프라인</h3>',
            '    <div class="settings-group">',
            '      <div class="setting-row">',
            '        <label class="setting-label" for="settingsSkipLlm">LLM 보정/요약 스킵</label>',
            '        <label class="setting-toggle">',
            '          <input type="checkbox" id="settingsSkipLlm">',
            '          <span class="toggle-track"><span class="toggle-thumb"></span></span>',
            '        </label>',
            '      </div>',
            '    </div>',
            '  </section>',
            '  <section class="settings-section">',
            '    <h3 class="settings-section-title">음성 인식</h3>',
            '    <div class="settings-group">',
            '      <div class="setting-row">',
            '        <label class="setting-label" for="settingsLang">전사 언어</label>',
            '        <select class="setting-select" id="settingsLang">',
            '          <option value="ko">한국어</option>',
            '          <option value="en">English</option>',
            '          <option value="ja">日本語</option>',
            '          <option value="zh">中文</option>',
            '        </select>',
            '      </div>',
            '    </div>',
            '  </section>',
            '  <section class="settings-section">',
            '    <h3 class="settings-section-title">환각(Hallucination) 필터</h3>',
            '    <p class="settings-section-desc">Whisper 가 무음/잡음 구간에서 생성하는 가짜 텍스트를 제거해요. 너무 공격적이면 실제 발화도 삭제되니 0.9 권장.</p>',
            '    <div class="settings-group">',
            '      <div class="setting-row">',
            '        <label class="setting-label" for="settingsHfEnabled">필터 사용</label>',
            '        <label class="setting-toggle">',
            '          <input type="checkbox" id="settingsHfEnabled">',
            '          <span class="toggle-track"><span class="toggle-thumb"></span></span>',
            '        </label>',
            '      </div>',
            '      <div class="setting-row">',
            '        <label class="setting-label" for="settingsHfNoSpeech">무음 임계값 (no_speech)</label>',
            '        <div class="setting-slider-group">',
            '          <input type="range" class="setting-slider" id="settingsHfNoSpeech" min="0" max="1" step="0.05">',
            '          <span class="setting-slider-value" id="settingsHfNoSpeechValue">0.9</span>',
            '        </div>',
            '      </div>',
            '      <div class="setting-row">',
            '        <label class="setting-label" for="settingsHfCompRatio">압축비 임계값</label>',
            '        <div class="setting-slider-group">',
            '          <input type="range" class="setting-slider" id="settingsHfCompRatio" min="1" max="5" step="0.1">',
            '          <span class="setting-slider-value" id="settingsHfCompRatioValue">2.4</span>',
            '        </div>',
            '      </div>',
            '      <div class="setting-row">',
            '        <label class="setting-label" for="settingsHfRepetition">반복 감지 임계값</label>',
            '        <div class="setting-slider-group">',
            '          <input type="range" class="setting-slider" id="settingsHfRepetition" min="2" max="10" step="1">',
            '          <span class="setting-slider-value" id="settingsHfRepetitionValue">3</span>',
            '        </div>',
            '      </div>',
            '    </div>',
            '  </section>',
            '  <section class="settings-section">',
            '    <h3 class="settings-section-title">음성 인식 모델 (STT)</h3>',
            '    <p class="settings-section-desc">한국어 회의 전사에 사용할 모델을 선택하세요. 다운로드 완료 후 활성화하면 다음 전사부터 적용돼요.</p>',
            '    <div class="stt-models" id="settingsSttModels" aria-live="polite">',
            '      <div class="stt-models-loading">불러오는 중…</div>',
            '    </div>',
            '    <div class="stt-models-status" id="settingsSttStatus" role="status" aria-live="polite"></div>',
            '  </section>',
            '  <div class="settings-actions">',
            '    <button class="settings-save-btn" id="settingsSaveBtn">변경사항 저장</button>',
            '    <span class="settings-save-status" id="settingsSaveStatus"></span>',
            '  </div>',
            '  <section class="settings-advanced">',
            '    <h3 class="settings-advanced-title">고급 기능</h3>',
            '    <div class="settings-advanced-item" id="settingsAbTestLink" tabindex="0" role="button">',
            '      <div class="settings-advanced-item-text">',
            '        <span class="settings-advanced-item-label">A/B 모델 테스트</span>',
            '        <span class="settings-advanced-item-desc">동일 회의를 서로 다른 모델로 처리하여 결과를 비교합니다.</span>',
            '      </div>',
            '      <span class="settings-advanced-item-arrow">&#x203A;</span>',
            '    </div>',
            '  </section>',
            '</div>',
        ].join("\n");

        this._els = {
            model: document.getElementById("settingsModel"),
            modelHelp: document.getElementById("settingsModelHelp"),
            backend: document.getElementById("settingsBackend"),
            temp: document.getElementById("settingsTemp"),
            tempValue: document.getElementById("settingsTempValue"),
            skipLlm: document.getElementById("settingsSkipLlm"),
            lang: document.getElementById("settingsLang"),
            saveBtn: document.getElementById("settingsSaveBtn"),
            saveStatus: document.getElementById("settingsSaveStatus"),
            sttModels: document.getElementById("settingsSttModels"),
            sttStatus: document.getElementById("settingsSttStatus"),
            hfEnabled: document.getElementById("settingsHfEnabled"),
            hfNoSpeech: document.getElementById("settingsHfNoSpeech"),
            hfNoSpeechValue: document.getElementById("settingsHfNoSpeechValue"),
            hfCompRatio: document.getElementById("settingsHfCompRatio"),
            hfCompRatioValue: document.getElementById("settingsHfCompRatioValue"),
            hfRepetition: document.getElementById("settingsHfRepetition"),
            hfRepetitionValue: document.getElementById("settingsHfRepetitionValue"),
        };
        // 모델별 description 캐시 (툴팁 갱신용)
        this._modelDescriptions = {};
        // STT 모델 폴링 타이머 (다운로드 중일 때 3초 간격 상태 갱신)
        this._sttPollTimers = [];
        // 다운로드 중인 모델 id (있으면 다른 카드의 다운로드 버튼 비활성화)
        this._sttDownloadingId = null;
    };

    GeneralSettingsPanel.prototype._bind = function () {
        var self = this;
        var els = self._els;
        var onTempInput = function () {
            els.tempValue.textContent = els.temp.value;
        };
        els.temp.addEventListener("input", onTempInput);
        self._listeners.push({ el: els.temp, type: "input", fn: onTempInput });

        // 환각 필터 슬라이더 라이브 값 표시
        var onHfNoSpeech = function () { els.hfNoSpeechValue.textContent = els.hfNoSpeech.value; };
        var onHfCompRatio = function () { els.hfCompRatioValue.textContent = els.hfCompRatio.value; };
        var onHfRepetition = function () { els.hfRepetitionValue.textContent = els.hfRepetition.value; };
        els.hfNoSpeech.addEventListener("input", onHfNoSpeech);
        els.hfCompRatio.addEventListener("input", onHfCompRatio);
        els.hfRepetition.addEventListener("input", onHfRepetition);
        self._listeners.push({ el: els.hfNoSpeech, type: "input", fn: onHfNoSpeech });
        self._listeners.push({ el: els.hfCompRatio, type: "input", fn: onHfCompRatio });
        self._listeners.push({ el: els.hfRepetition, type: "input", fn: onHfRepetition });

        // LLM 모델 변경 시 (?) 툴팁 description 갱신
        var onModelChange = function () {
            self._updateModelHelp();
        };
        els.model.addEventListener("change", onModelChange);
        self._listeners.push({ el: els.model, type: "change", fn: onModelChange });

        var onSave = function () {
            self._saveSettings();
        };
        els.saveBtn.addEventListener("click", onSave);
        self._listeners.push({ el: els.saveBtn, type: "click", fn: onSave });

        // STT 모델 목록 로드
        self._loadSttModels();

        // 고급 기능 — A/B 테스트 링크
        var abTestLink = document.getElementById("settingsAbTestLink");
        if (abTestLink) {
            var onAbTest = function () { Router.navigate("/app/ab-test"); };
            abTestLink.addEventListener("click", onAbTest);
            self._listeners.push({ el: abTestLink, type: "click", fn: onAbTest });
            // 키보드 접근성
            abTestLink.addEventListener("keydown", function (e) {
                if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onAbTest(); }
            });
        }
    };

    GeneralSettingsPanel.prototype._loadSettings = async function () {
        var self = this;
        var els = this._els;
        try {
            var data = await App.apiRequest("/settings");
            if (data.available_models && data.available_models.length > 0) {
                // 모델 description 캐시 (툴팁 갱신용)
                self._modelDescriptions = {};
                data.available_models.forEach(function (m) {
                    self._modelDescriptions[m.id] = m.description || "";
                });

                els.model.innerHTML = "";
                data.available_models.forEach(function (m) {
                    var opt = document.createElement("option");
                    opt.value = m.id;
                    opt.textContent = m.label + " (" + m.size + ")";
                    els.model.appendChild(opt);
                });
            }
            if (data.llm_mlx_model_name) els.model.value = data.llm_mlx_model_name;
            self._updateModelHelp();
            if (data.llm_backend) els.backend.value = data.llm_backend;
            if (data.llm_temperature !== undefined && data.llm_temperature !== null) {
                els.temp.value = data.llm_temperature;
                els.tempValue.textContent = data.llm_temperature;
            }
            els.skipLlm.checked = !!data.llm_skip_steps;
            if (data.stt_language) els.lang.value = data.stt_language;
            // 환각 필터
            els.hfEnabled.checked = !!data.hf_enabled;
            if (data.hf_no_speech_threshold !== undefined && data.hf_no_speech_threshold !== null) {
                els.hfNoSpeech.value = data.hf_no_speech_threshold;
                els.hfNoSpeechValue.textContent = data.hf_no_speech_threshold;
            }
            if (data.hf_compression_ratio_threshold !== undefined && data.hf_compression_ratio_threshold !== null) {
                els.hfCompRatio.value = data.hf_compression_ratio_threshold;
                els.hfCompRatioValue.textContent = data.hf_compression_ratio_threshold;
            }
            if (data.hf_repetition_threshold !== undefined && data.hf_repetition_threshold !== null) {
                els.hfRepetition.value = data.hf_repetition_threshold;
                els.hfRepetitionValue.textContent = data.hf_repetition_threshold;
            }
        } catch (err) {
            errorBanner.show("설정 불러오기 실패: " + (err.message || err));
        }
    };

    /**
     * 현재 선택된 LLM 모델의 설명을 (?) 툴팁에 반영한다.
     */
    GeneralSettingsPanel.prototype._updateModelHelp = function () {
        var els = this._els;
        if (!els.modelHelp || !this._modelDescriptions) return;
        var currentId = els.model.value;
        var desc = this._modelDescriptions[currentId];
        if (desc) {
            els.modelHelp.setAttribute("data-tooltip", desc);
        } else {
            els.modelHelp.setAttribute("data-tooltip", "선택된 모델의 설명이 없습니다.");
        }
    };

    GeneralSettingsPanel.prototype._saveSettings = async function () {
        var els = this._els;
        els.saveBtn.disabled = true;
        els.saveBtn.textContent = "저장 중…";
        els.saveStatus.textContent = "";

        var payload = {
            llm_mlx_model_name: els.model.value,
            llm_backend: els.backend.value,
            llm_temperature: parseFloat(els.temp.value),
            llm_skip_steps: els.skipLlm.checked,
            stt_language: els.lang.value,
            hf_enabled: els.hfEnabled.checked,
            hf_no_speech_threshold: parseFloat(els.hfNoSpeech.value),
            hf_compression_ratio_threshold: parseFloat(els.hfCompRatio.value),
            hf_repetition_threshold: parseInt(els.hfRepetition.value, 10),
        };

        try {
            var resp = await fetch("/api/settings", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (!resp.ok) {
                var errData = await resp.json().catch(function () { return {}; });
                throw new Error(errData.detail || "HTTP " + resp.status);
            }
            els.saveStatus.textContent = "저장 완료";
            els.saveStatus.className = "settings-save-status success";
            setTimeout(function () {
                els.saveStatus.textContent = "";
                els.saveStatus.className = "settings-save-status";
            }, 3000);
        } catch (err) {
            els.saveStatus.textContent = "저장 실패: " + App.escapeHtml(err.message || String(err));
            els.saveStatus.className = "settings-save-status error";
        } finally {
            els.saveBtn.disabled = false;
            els.saveBtn.textContent = "변경사항 저장";
        }
    };

    GeneralSettingsPanel.prototype.isDirty = function () {
        // 일반 설정은 명시적 저장 버튼 패턴이라 dirty 추적을 하지 않는다.
        return false;
    };

    GeneralSettingsPanel.prototype.destroy = function () {
        this._listeners.forEach(function (e) {
            e.el.removeEventListener(e.type, e.fn);
        });
        this._listeners = [];
        // STT 폴링 타이머 정리 (메모리 누수 방지)
        if (this._sttPollTimers) {
            this._sttPollTimers.forEach(function (t) { clearInterval(t); });
            this._sttPollTimers = [];
        }
        this._sttDownloadingId = null;
    };

    // =================================================================
    // === STT 모델 관리 메서드 (Phase 6) ===
    // =================================================================

    /**
     * STT 모델 목록을 API에서 가져와 카드로 렌더링한다.
     * 폴링 루프에서도 호출되며, 실패 시 에러 배너 + 컨테이너에 에러 표시.
     */
    GeneralSettingsPanel.prototype._loadSttModels = async function () {
        var self = this;
        if (!self._els || !self._els.sttModels) return;
        try {
            var data = await App.apiRequest("/stt-models");
            self._renderSttModels((data && data.models) || []);
        } catch (err) {
            // API 미구현 상태(404)에서도 전체 설정 페이지가 무너지지 않도록 graceful degradation
            var msg = (err && err.message) ? err.message : String(err);
            self._els.sttModels.innerHTML = "";
            var errDiv = document.createElement("div");
            errDiv.className = "stt-models-error";
            errDiv.textContent = "STT 모델 목록을 불러오지 못했어요: " + msg;
            self._els.sttModels.appendChild(errDiv);
            // 재시도 버튼
            var retryBtn = document.createElement("button");
            retryBtn.type = "button";
            retryBtn.className = "stt-action-btn";
            retryBtn.textContent = "다시 시도";
            retryBtn.setAttribute("aria-label", "STT 모델 목록 다시 불러오기");
            retryBtn.addEventListener("click", function () { self._loadSttModels(); });
            self._els.sttModels.appendChild(retryBtn);
        }
    };

    /**
     * 모델 배열을 카드 UI로 렌더링한다.
     * 각 카드: 헤더(이름+배지), 설명, 메트릭, 액션(다운로드/활성화/진행률).
     * @param {Array} models /api/stt-models 응답의 models 배열
     */
    GeneralSettingsPanel.prototype._renderSttModels = function (models) {
        var self = this;
        var container = self._els.sttModels;
        container.innerHTML = "";

        if (!models || models.length === 0) {
            var empty = document.createElement("div");
            empty.className = "stt-models-empty";
            empty.textContent = "사용할 수 있는 STT 모델이 없어요.";
            container.appendChild(empty);
            return;
        }

        // 다운로드 중인 모델이 있는지 스캔 (다른 카드의 다운로드 버튼 비활성화용)
        var downloadingId = null;
        models.forEach(function (m) {
            if (m.status === "downloading" || m.status === "quantizing") {
                downloadingId = m.id;
            }
        });
        self._sttDownloadingId = downloadingId;

        models.forEach(function (m) {
            var card = document.createElement("div");
            card.className = "stt-model-card";
            if (m.is_active) card.classList.add("active");
            if (m.is_recommended) card.classList.add("recommended");

            // 헤더: 이름 + 배지 (추천/활성)
            var header = document.createElement("div");
            header.className = "stt-model-header";

            var name = document.createElement("div");
            name.className = "stt-model-name";
            name.textContent = m.label || m.id;
            header.appendChild(name);

            // (?) 도움말 — description을 한 줄 요약으로 표시
            if (m.description) {
                var help = document.createElement("span");
                help.className = "setting-help";
                help.tabIndex = 0;
                help.setAttribute("role", "button");
                help.setAttribute("aria-label", (m.label || m.id) + " 모델 설명 보기");
                help.setAttribute("data-tooltip", m.description);
                help.textContent = "?";
                header.appendChild(help);
            }

            if (m.is_recommended) {
                var rec = document.createElement("span");
                rec.className = "stt-model-badge recommended";
                rec.textContent = "추천";
                header.appendChild(rec);
            }
            if (m.is_active) {
                var act = document.createElement("span");
                act.className = "stt-model-badge active";
                act.textContent = "활성";
                header.appendChild(act);
            }

            // 설명 (카드 본문) — 헤더 (?) 와 동일 내용을 더 자세히 노출
            var desc = document.createElement("div");
            desc.className = "stt-model-desc";
            desc.textContent = m.description || "";

            // 메트릭: CER / WER / 크기 / 메모리 (XSS 방지: textContent 기반 조립)
            // 각 라벨에 비개발자용 1줄 설명 툴팁 첨부
            var metrics = document.createElement("div");
            metrics.className = "stt-model-metrics";
            var metricPairs = [
                {
                    label: "CER",
                    value: (m.cer_percent != null ? m.cer_percent + "%" : "-"),
                    tooltip: "글자 오류율 — 100글자 중 몇 글자가 틀렸는지. 낮을수록 정확합니다.",
                },
                {
                    label: "WER",
                    value: (m.wer_percent != null ? m.wer_percent + "%" : "-"),
                    tooltip: "단어 오류율 — 100단어 중 몇 단어가 틀렸는지. 낮을수록 정확합니다.",
                },
                {
                    label: "크기",
                    value: (m.expected_size_mb != null ? m.expected_size_mb + "MB" : "-"),
                    tooltip: "디스크에 저장되는 모델 파일 크기입니다.",
                },
                {
                    label: "RAM",
                    value: (m.memory_gb != null ? m.memory_gb + "GB" : "-"),
                    tooltip: "전사 실행 중 사용하는 메모리 사용량입니다. 16GB 맥북에서 다른 앱과 함께 쓸 때 참고하세요.",
                },
            ];
            metricPairs.forEach(function (pair) {
                var span = document.createElement("span");
                span.className = "metric";
                span.setAttribute("data-tooltip", pair.tooltip);
                var strong = document.createElement("strong");
                strong.textContent = pair.label;
                span.appendChild(strong);
                span.appendChild(document.createTextNode(" " + pair.value));
                metrics.appendChild(span);
            });

            // 액션 영역
            var actions = document.createElement("div");
            actions.className = "stt-model-actions";

            if (m.status === "not_downloaded" || m.status === "error") {
                var dlBtn = document.createElement("button");
                dlBtn.type = "button";
                dlBtn.className = "stt-action-btn download";
                dlBtn.textContent = m.status === "error" ? "다시 다운로드" : "다운로드";
                dlBtn.setAttribute("aria-label", (m.label || m.id) + " 모델 다운로드");
                // 다른 모델이 다운로드 중이면 비활성화
                if (downloadingId && downloadingId !== m.id) {
                    dlBtn.disabled = true;
                    dlBtn.title = "다른 모델을 다운로드 중이에요";
                }
                dlBtn.addEventListener("click", function () {
                    self._downloadModel(m.id);
                });
                actions.appendChild(dlBtn);
            } else if (m.status === "downloading" || m.status === "quantizing") {
                var progress = document.createElement("div");
                progress.className = "stt-model-progress";
                var pct = (m.download_progress && typeof m.download_progress.progress_percent === "number")
                    ? m.download_progress.progress_percent
                    : (typeof m.download_progress === "number" ? m.download_progress : 0);
                var stepLabel = m.status === "downloading" ? "다운로드 중" : "양자화 중";
                var pText = document.createElement("div");
                pText.className = "progress-text";
                pText.textContent = stepLabel + " " + pct + "%";
                var pBar = document.createElement("div");
                pBar.className = "progress-bar";
                pBar.setAttribute("role", "progressbar");
                pBar.setAttribute("aria-valuenow", String(pct));
                pBar.setAttribute("aria-valuemin", "0");
                pBar.setAttribute("aria-valuemax", "100");
                var pFill = document.createElement("div");
                pFill.className = "progress-fill";
                pFill.style.width = pct + "%";
                pBar.appendChild(pFill);
                progress.appendChild(pText);
                progress.appendChild(pBar);
                actions.appendChild(progress);
            } else if (m.status === "ready" && !m.is_active) {
                var actBtn = document.createElement("button");
                actBtn.type = "button";
                actBtn.className = "stt-action-btn activate";
                actBtn.textContent = "활성화";
                actBtn.setAttribute("aria-label", (m.label || m.id) + " 모델 활성화");
                actBtn.addEventListener("click", function () {
                    self._activateModel(m.id);
                });
                actions.appendChild(actBtn);
            } else if (m.is_active) {
                // 이미 활성 — 별도 버튼 없음 (헤더 배지로 표시)
                var activeLabel = document.createElement("span");
                activeLabel.className = "stt-active-label";
                activeLabel.textContent = "현재 사용 중";
                actions.appendChild(activeLabel);
            }

            card.appendChild(header);
            card.appendChild(desc);
            card.appendChild(metrics);
            card.appendChild(actions);

            // 수동 다운로드 펼침 섹션 — 네트워크 이슈로 자동 다운로드가 안 되는 사용자용
            // 다운로드 중이거나 이미 ready 상태가 아닐 때만 노출
            if (m.status === "not_downloaded" || m.status === "error") {
                var manualSection = self._buildManualDownloadSection(m);
                if (manualSection) {
                    card.appendChild(manualSection);
                }
            }

            // 에러 메시지 (있으면 카드 하단에)
            if (m.error_message) {
                var errEl = document.createElement("div");
                errEl.className = "stt-model-error";
                errEl.setAttribute("role", "alert");
                errEl.textContent = "오류: " + m.error_message;
                card.appendChild(errEl);
            }

            container.appendChild(card);
        });
    };

    /**
     * 수동 다운로드 펼침 섹션을 생성한다.
     * HF 직접 URL 목록 + 복사 버튼 + 폴더 경로 입력 + 가져오기 버튼.
     * needs_quantization=True 모델은 supported=false 로 안내만 표시.
     * @param {Object} m 모델 정보
     * @returns {HTMLElement|null}
     */
    GeneralSettingsPanel.prototype._buildManualDownloadSection = function (m) {
        var self = this;
        var details = document.createElement("details");
        details.className = "stt-manual-download";

        var summary = document.createElement("summary");
        summary.textContent = "브라우저로 직접 받기";
        details.appendChild(summary);

        var body = document.createElement("div");
        body.className = "stt-manual-body";
        body.innerHTML = '<div class="stt-manual-loading">정보 불러오는 중…</div>';
        details.appendChild(body);

        // details가 열릴 때 처음 한 번만 데이터 로드 (lazy)
        var loaded = false;
        details.addEventListener("toggle", function () {
            if (!details.open || loaded) return;
            loaded = true;
            self._loadManualDownloadInfo(m.id, body);
        });

        return details;
    };

    /**
     * /api/stt-models/{id}/manual-download-info 를 호출해 UI 에 렌더링한다.
     * @param {string} modelId
     * @param {HTMLElement} bodyEl 렌더 대상 컨테이너
     */
    GeneralSettingsPanel.prototype._loadManualDownloadInfo = async function (
        modelId,
        bodyEl
    ) {
        var self = this;
        try {
            var info = await App.apiRequest(
                "/stt-models/" + encodeURIComponent(modelId) + "/manual-download-info"
            );
            bodyEl.innerHTML = "";

            if (!info.supported) {
                var msg = document.createElement("p");
                msg.className = "stt-manual-unsupported";
                msg.textContent = info.instructions || "이 모델은 수동 다운로드를 지원하지 않아요.";
                bodyEl.appendChild(msg);
                return;
            }

            // 옵션 1: 앱이 URL 로 대신 받기 (가장 간단한 경로)
            // huggingface_hub 를 건너뛰고 urllib 스트리밍으로 HF 직접 URL 다운로드.
            // 기업 프록시·SSL MITM·ISP 필터 환경에서 자주 성공한다.
            var directGroup = document.createElement("div");
            directGroup.className = "stt-direct-download-group";

            var directTitle = document.createElement("div");
            directTitle.className = "stt-direct-title";
            directTitle.textContent = "옵션 1 · 앱이 URL 로 대신 받기 (추천)";
            directGroup.appendChild(directTitle);

            var directDesc = document.createElement("p");
            directDesc.className = "stt-direct-desc";
            directDesc.textContent =
                "앱이 HuggingFace 직접 URL로 파일을 받아 자동으로 배치해요. " +
                "브라우저 왕복이나 폴더 경로 입력 없이 한 번 클릭으로 끝나요.";
            directGroup.appendChild(directDesc);

            var directBtn = document.createElement("button");
            directBtn.type = "button";
            directBtn.className = "stt-action-btn download";
            directBtn.textContent = "앱이 URL로 받기";
            directBtn.addEventListener("click", function () {
                self._downloadModelDirect(modelId);
            });
            directGroup.appendChild(directBtn);

            bodyEl.appendChild(directGroup);

            // 옵션 2: 브라우저로 직접 받아서 폴더 가져오기
            var manualTitle = document.createElement("div");
            manualTitle.className = "stt-direct-title";
            manualTitle.style.marginTop = "18px";
            manualTitle.textContent = "옵션 2 · 브라우저로 직접 받기";
            bodyEl.appendChild(manualTitle);

            // 안내 문구
            var help = document.createElement("p");
            help.className = "stt-manual-instructions";
            help.style.whiteSpace = "pre-line";
            help.textContent = info.instructions;
            bodyEl.appendChild(help);

            // URL 목록
            var urlList = document.createElement("ul");
            urlList.className = "stt-manual-urls";
            info.files.forEach(function (f) {
                var li = document.createElement("li");

                var nameSpan = document.createElement("code");
                nameSpan.className = "stt-manual-file-name";
                nameSpan.textContent = f.name;
                li.appendChild(nameSpan);

                var link = document.createElement("a");
                link.href = f.url;
                link.target = "_blank";
                link.rel = "noopener noreferrer";
                link.className = "stt-manual-link";
                link.textContent = "브라우저로 열기 ↗";
                li.appendChild(link);

                var copyBtn = document.createElement("button");
                copyBtn.type = "button";
                copyBtn.className = "btn-icon";
                copyBtn.textContent = "URL 복사";
                copyBtn.addEventListener("click", function () {
                    navigator.clipboard.writeText(f.url).then(function () {
                        copyBtn.textContent = "복사됨 ✓";
                        setTimeout(function () {
                            copyBtn.textContent = "URL 복사";
                        }, 1500);
                    });
                });
                li.appendChild(copyBtn);

                urlList.appendChild(li);
            });
            bodyEl.appendChild(urlList);

            // 타겟 디렉토리 표시
            var targetInfo = document.createElement("div");
            targetInfo.className = "stt-manual-target";
            targetInfo.innerHTML =
                '<strong>복사될 위치:</strong> <code>' +
                App.escapeHtml(info.target_directory) +
                "</code>";
            bodyEl.appendChild(targetInfo);

            // 폴더 경로 입력 + 가져오기 버튼
            var importGroup = document.createElement("div");
            importGroup.className = "stt-manual-import-group";

            var label = document.createElement("label");
            label.className = "stt-manual-label";
            label.textContent = "다운로드한 폴더 경로";
            importGroup.appendChild(label);

            var pathInput = document.createElement("input");
            pathInput.type = "text";
            pathInput.className = "stt-manual-path-input";
            pathInput.placeholder = "예: /Users/yourname/Downloads/" + modelId;
            importGroup.appendChild(pathInput);

            var importBtn = document.createElement("button");
            importBtn.type = "button";
            importBtn.className = "stt-action-btn activate";
            importBtn.textContent = "가져오기";
            importBtn.addEventListener("click", function () {
                self._importManualModel(modelId, pathInput.value, bodyEl);
            });
            importGroup.appendChild(importBtn);

            bodyEl.appendChild(importGroup);
        } catch (err) {
            bodyEl.innerHTML = "";
            var errEl = document.createElement("div");
            errEl.className = "stt-manual-error";
            errEl.textContent = "정보를 불러오지 못했어요: " + (err.message || err);
            bodyEl.appendChild(errEl);
        }
    };

    /**
     * 수동 가져오기 엔드포인트 호출.
     * @param {string} modelId
     * @param {string} sourceDir 사용자 폴더 경로
     * @param {HTMLElement} bodyEl 결과 표시 컨테이너
     */
    GeneralSettingsPanel.prototype._importManualModel = async function (
        modelId,
        sourceDir,
        bodyEl
    ) {
        var self = this;
        sourceDir = (sourceDir || "").trim();
        if (!sourceDir) {
            self._showSttStatus("폴더 경로를 입력해 주세요.", "error");
            return;
        }

        self._showSttStatus("가져오는 중…", "info");
        try {
            var resp = await fetch(
                "/api/stt-models/" + encodeURIComponent(modelId) + "/import-manual",
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ source_dir: sourceDir }),
                }
            );
            if (!resp.ok) {
                var errData = await resp.json().catch(function () { return {}; });
                throw new Error(errData.detail || "HTTP " + resp.status);
            }
            var data = await resp.json();
            self._showSttStatus(data.message || "가져오기 완료", "success");
            // 모델 목록 리로드 → 상태가 ready 로 바뀌어 '활성화' 버튼 노출됨
            self._loadSttModels();
        } catch (err) {
            self._showSttStatus(
                "가져오기 실패: " + (err.message || String(err)),
                "error"
            );
        }
    };

    /**
     * 인라인 상태 메시지를 표시한다 (토스트 대체).
     * @param {string} msg 표시할 메시지
     * @param {string} kind "info" | "success" | "error"
     */
    GeneralSettingsPanel.prototype._showSttStatus = function (msg, kind) {
        var el = this._els && this._els.sttStatus;
        if (!el) return;
        el.textContent = msg;
        el.className = "stt-models-status " + (kind || "info");
        // success/info는 3초 후 자동 숨김, error는 유지
        if (kind !== "error") {
            var self = this;
            setTimeout(function () {
                if (self._els && self._els.sttStatus && self._els.sttStatus.textContent === msg) {
                    self._els.sttStatus.textContent = "";
                    self._els.sttStatus.className = "stt-models-status";
                }
            }, 3000);
        }
    };

    /**
     * 특정 모델 다운로드를 시작하고, 3초 간격으로 상태를 폴링한다.
     * ready 또는 error 상태가 되면 폴링을 중지한다.
     * @param {string} modelId 모델 id
     */
    /**
     * HF 직접 URL 로 다운로드한다 (huggingface_hub 건너뜀).
     * 수동 다운로드 섹션의 "앱이 URL로 받기" 버튼에서 호출한다.
     * @param {string} modelId 모델 id
     */
    GeneralSettingsPanel.prototype._downloadModelDirect = function (modelId) {
        return this._downloadModel(modelId, { direct: true });
    };

    GeneralSettingsPanel.prototype._downloadModel = async function (modelId, opts) {
        var self = this;
        opts = opts || {};
        var direct = opts.direct === true;
        var endpoint = direct
            ? "/stt-models/" + modelId + "/download-direct"
            : "/stt-models/" + modelId + "/download";
        var startMsg = direct
            ? "URL 로 직접 다운로드를 시작해요…"
            : "다운로드를 시작합니다…";
        try {
            self._showSttStatus(startMsg, "info");
            await App.apiPost(endpoint, {});
            // 즉시 한 번 새로고침하여 진행률 바가 바로 표시되도록 함
            await self._loadSttModels();

            // 3초 간격 폴링 시작
            var pollTimer = setInterval(async function () {
                try {
                    var data = await App.apiRequest("/stt-models");
                    self._renderSttModels((data && data.models) || []);
                    // 해당 모델의 현재 상태 확인
                    var target = null;
                    (data.models || []).forEach(function (m) {
                        if (m.id === modelId) target = m;
                    });
                    if (!target) {
                        clearInterval(pollTimer);
                        return;
                    }
                    if (target.status === "ready") {
                        clearInterval(pollTimer);
                        self._showSttStatus("다운로드 완료: " + (target.label || modelId), "success");
                    } else if (target.status === "error" || target.error_message) {
                        clearInterval(pollTimer);
                        self._showSttStatus(
                            "다운로드 실패: " + (target.error_message || "알 수 없는 오류"),
                            "error"
                        );
                    }
                } catch (pollErr) {
                    // 폴링 중 네트워크 일시 오류는 그냥 다음 주기로 넘김
                }
            }, 3000);
            self._sttPollTimers.push(pollTimer);
        } catch (err) {
            var msg = (err && err.message) ? err.message : String(err);
            self._showSttStatus("다운로드 시작 실패: " + msg, "error");
        }
    };

    /**
     * 특정 모델을 활성화한다. 성공 시 즉시 목록을 새로고침한다.
     * @param {string} modelId 모델 id
     */
    GeneralSettingsPanel.prototype._activateModel = async function (modelId) {
        var self = this;
        try {
            var resp = await App.apiPost("/stt-models/" + modelId + "/activate", {});
            await self._loadSttModels();
            var msg = (resp && resp.message)
                ? resp.message
                : "활성 모델이 변경되었어요. 다음 전사부터 적용돼요.";
            self._showSttStatus(msg, "success");
        } catch (err) {
            var m = (err && err.message) ? err.message : String(err);
            self._showSttStatus("활성화 실패: " + m, "error");
        }
    };


    // =================================================================
    // === PromptsSettingsPanel — 3개 프롬프트 편집 (보정/요약/채팅) ===
    // =================================================================

    /**
     * 프롬프트 편집 패널.
     * Segmented control 로 3개 프롬프트 탭 전환, textarea + 변경 감지 dot +
     * 글자수 카운터 + 저장/되돌리기 버튼 구성.
     * @param {HTMLElement} host
     * @constructor
     */
    function PromptsSettingsPanel(host) {
        var self = this;
        self._host = host;
        self._els = {};
        self._listeners = [];
        // 프롬프트 3종 현재 저장된 값
        self._initial = { corrector: "", summarizer: "", chat: "" };
        // 현재 편집 값 (dirty 비교용)
        self._current = { corrector: "", summarizer: "", chat: "" };
        // 현재 활성 탭
        self._activeKind = "corrector";
        // 기본값 (reset 버튼용)
        self._defaults = null;
        // 키보드 핸들러 (destroy에서 해제)
        self._keydownHandler = null;
        self._render();
        self._bind();
        self._load();
    }

    PromptsSettingsPanel.prototype._LABELS = {
        corrector: {
            title: "보정 프롬프트",
            desc: "STT 결과의 오타·문법·오인식을 교정할 때 사용해요. '[번호]' 출력 포맷 지시를 반드시 포함해야 해요.",
        },
        summarizer: {
            title: "요약 프롬프트",
            desc: "회의 전사문을 마크다운 회의록으로 요약할 때 사용해요.",
        },
        chat: {
            title: "채팅 프롬프트",
            desc: "회의록을 검색해 답변하는 채팅에 사용해요.",
        },
    };

    PromptsSettingsPanel.prototype._render = function () {
        this._host.innerHTML = [
            '<div class="settings-content prompts-panel">',
            '  <div class="prompt-subtabs" role="tablist" aria-label="프롬프트 종류">',
            '    <button type="button" class="prompt-subtab active" data-kind="corrector" role="tab" aria-selected="true">보정</button>',
            '    <button type="button" class="prompt-subtab" data-kind="summarizer" role="tab" aria-selected="false">요약</button>',
            '    <button type="button" class="prompt-subtab" data-kind="chat" role="tab" aria-selected="false">채팅</button>',
            '  </div>',
            '  <section class="settings-section prompt-editor-card">',
            '    <div class="prompt-editor-header">',
            '      <div>',
            '        <h3 class="settings-section-title" id="promptTitle">보정 프롬프트</h3>',
            '        <p class="prompt-editor-desc" id="promptDesc"></p>',
            '      </div>',
            '      <span class="prompt-dirty-indicator" id="promptDirtyDot" aria-live="polite"></span>',
            '    </div>',
            '    <textarea class="prompt-textarea" id="promptTextarea" spellcheck="false" aria-labelledby="promptTitle"></textarea>',
            '    <div class="prompt-meta">',
            '      <span id="promptCounter">0 / 8,000자</span>',
            '      <span id="promptSaveStatus" class="settings-save-status"></span>',
            '    </div>',
            '  </section>',
            '  <div class="settings-actions prompt-actions">',
            '    <button type="button" class="btn-text-destructive" id="promptResetBtn">기본값으로 되돌리기</button>',
            '    <div class="prompt-actions-right">',
            '      <button type="button" class="btn-secondary" id="promptRevertBtn" disabled>되돌리기</button>',
            '      <button type="button" class="settings-save-btn" id="promptSaveBtn" disabled>저장</button>',
            '    </div>',
            '  </div>',
            '</div>',
        ].join("\n");

        this._els = {
            subtabs: this._host.querySelectorAll(".prompt-subtab"),
            title: document.getElementById("promptTitle"),
            desc: document.getElementById("promptDesc"),
            textarea: document.getElementById("promptTextarea"),
            counter: document.getElementById("promptCounter"),
            dirtyDot: document.getElementById("promptDirtyDot"),
            saveStatus: document.getElementById("promptSaveStatus"),
            saveBtn: document.getElementById("promptSaveBtn"),
            revertBtn: document.getElementById("promptRevertBtn"),
            resetBtn: document.getElementById("promptResetBtn"),
        };
    };

    PromptsSettingsPanel.prototype._bind = function () {
        var self = this;
        var els = self._els;

        // 서브탭 클릭
        Array.prototype.forEach.call(els.subtabs, function (btn) {
            var onClick = function () {
                self._switchKind(btn.getAttribute("data-kind"));
            };
            btn.addEventListener("click", onClick);
            self._listeners.push({ el: btn, type: "click", fn: onClick });
        });

        // textarea 입력
        var onInput = function () {
            self._current[self._activeKind] = els.textarea.value;
            self._updateMeta();
        };
        els.textarea.addEventListener("input", onInput);
        self._listeners.push({ el: els.textarea, type: "input", fn: onInput });

        // 저장 버튼
        var onSave = function () {
            self._save();
        };
        els.saveBtn.addEventListener("click", onSave);
        self._listeners.push({ el: els.saveBtn, type: "click", fn: onSave });

        // 되돌리기 (편집 되돌림)
        var onRevert = function () {
            self._current[self._activeKind] = self._initial[self._activeKind];
            els.textarea.value = self._initial[self._activeKind];
            self._updateMeta();
        };
        els.revertBtn.addEventListener("click", onRevert);
        self._listeners.push({ el: els.revertBtn, type: "click", fn: onRevert });

        // 기본값으로 되돌리기
        var onReset = function () {
            self._resetCurrent();
        };
        els.resetBtn.addEventListener("click", onReset);
        self._listeners.push({ el: els.resetBtn, type: "click", fn: onReset });

        // Cmd+S / Ctrl+S 단축키
        self._keydownHandler = function (e) {
            if ((e.metaKey || e.ctrlKey) && e.key === "s") {
                if (self.isDirty()) {
                    e.preventDefault();
                    self._save();
                }
            }
        };
        document.addEventListener("keydown", self._keydownHandler);
    };

    PromptsSettingsPanel.prototype._load = async function () {
        var self = this;
        try {
            var data = await App.apiRequest("/prompts");
            var p = data.prompts || data;
            self._initial.corrector = p.corrector.system_prompt;
            self._initial.summarizer = p.summarizer.system_prompt;
            self._initial.chat = p.chat.system_prompt;
            self._current = {
                corrector: self._initial.corrector,
                summarizer: self._initial.summarizer,
                chat: self._initial.chat,
            };
            self._showKind(self._activeKind);
        } catch (err) {
            errorBanner.show("프롬프트 불러오기 실패: " + (err.message || err));
        }
    };

    PromptsSettingsPanel.prototype._switchKind = function (kind) {
        if (this._activeKind === kind) return;
        this._activeKind = kind;
        // 서브탭 활성 상태 업데이트
        Array.prototype.forEach.call(this._els.subtabs, function (btn) {
            var isActive = btn.getAttribute("data-kind") === kind;
            btn.classList.toggle("active", isActive);
            btn.setAttribute("aria-selected", isActive ? "true" : "false");
        });
        this._showKind(kind);
    };

    PromptsSettingsPanel.prototype._showKind = function (kind) {
        var els = this._els;
        var label = this._LABELS[kind];
        els.title.textContent = label.title;
        els.desc.textContent = label.desc;
        els.textarea.value = this._current[kind] || "";
        this._updateMeta();
    };

    PromptsSettingsPanel.prototype._updateMeta = function () {
        var els = this._els;
        var text = els.textarea.value || "";
        var len = text.length;
        var max = 8000;
        var isOver = len > max;
        els.counter.textContent = len.toLocaleString() + " / " + max.toLocaleString() + "자";
        els.counter.classList.toggle("over-limit", isOver);

        var dirty = this._current[this._activeKind] !== this._initial[this._activeKind];
        els.dirtyDot.classList.toggle("active", dirty);
        els.dirtyDot.textContent = dirty ? "· 변경됨" : "";

        // 저장 버튼: dirty 이고 길이 제한 안 넘고 최소 길이 이상일 때만 활성
        var canSave = dirty && !isOver && len >= 20;
        els.saveBtn.disabled = !canSave;
        els.revertBtn.disabled = !dirty;
    };

    PromptsSettingsPanel.prototype._save = async function () {
        var self = this;
        var els = self._els;
        var kind = self._activeKind;

        els.saveBtn.disabled = true;
        els.saveBtn.textContent = "저장 중…";
        els.saveStatus.textContent = "";
        els.saveStatus.className = "settings-save-status";

        var body = {};
        body[kind] = { system_prompt: self._current[kind] };

        try {
            var resp = await fetch("/api/prompts", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            if (!resp.ok) {
                var errData = await resp.json().catch(function () { return {}; });
                var msg = errData.detail || "HTTP " + resp.status;
                if (typeof msg !== "string") msg = JSON.stringify(msg);
                throw new Error(msg);
            }
            var data = await resp.json();
            var p = data.prompts || data;
            self._initial.corrector = p.corrector.system_prompt;
            self._initial.summarizer = p.summarizer.system_prompt;
            self._initial.chat = p.chat.system_prompt;
            // 활성 탭의 current 는 유지 (사용자가 계속 편집 가능)
            self._current[kind] = self._initial[kind];
            els.textarea.value = self._initial[kind];
            self._updateMeta();

            els.saveStatus.textContent = "저장되었어요";
            els.saveStatus.className = "settings-save-status success";
            setTimeout(function () {
                els.saveStatus.textContent = "";
                els.saveStatus.className = "settings-save-status";
            }, 2000);
        } catch (err) {
            els.saveStatus.textContent = "저장 실패: " + (err.message || String(err));
            els.saveStatus.className = "settings-save-status error";
        } finally {
            els.saveBtn.textContent = "저장";
            self._updateMeta();
        }
    };

    PromptsSettingsPanel.prototype._resetCurrent = async function () {
        var ok = window.confirm(
            "기본 프롬프트로 되돌릴까요? 현재 저장된 프롬프트 3개(보정/요약/채팅)가 모두 기본값으로 복원돼요."
        );
        if (!ok) return;

        var self = this;
        var els = self._els;
        els.resetBtn.disabled = true;
        try {
            var resp = await fetch("/api/prompts/reset", { method: "POST" });
            if (!resp.ok) {
                var errData = await resp.json().catch(function () { return {}; });
                throw new Error(errData.detail || "HTTP " + resp.status);
            }
            var data = await resp.json();
            var p = data.prompts || data;
            self._initial.corrector = p.corrector.system_prompt;
            self._initial.summarizer = p.summarizer.system_prompt;
            self._initial.chat = p.chat.system_prompt;
            self._current = {
                corrector: self._initial.corrector,
                summarizer: self._initial.summarizer,
                chat: self._initial.chat,
            };
            self._showKind(self._activeKind);
            els.saveStatus.textContent = "기본값으로 복원되었어요";
            els.saveStatus.className = "settings-save-status success";
            setTimeout(function () {
                els.saveStatus.textContent = "";
                els.saveStatus.className = "settings-save-status";
            }, 2000);
        } catch (err) {
            els.saveStatus.textContent = "복원 실패: " + (err.message || String(err));
            els.saveStatus.className = "settings-save-status error";
        } finally {
            els.resetBtn.disabled = false;
        }
    };

    PromptsSettingsPanel.prototype.isDirty = function () {
        return (
            this._current.corrector !== this._initial.corrector ||
            this._current.summarizer !== this._initial.summarizer ||
            this._current.chat !== this._initial.chat
        );
    };

    PromptsSettingsPanel.prototype.destroy = function () {
        this._listeners.forEach(function (e) {
            e.el.removeEventListener(e.type, e.fn);
        });
        this._listeners = [];
        if (this._keydownHandler) {
            document.removeEventListener("keydown", this._keydownHandler);
            this._keydownHandler = null;
        }
    };


    // =================================================================
    // === VocabularySettingsPanel — 용어집 CRUD ===
    // =================================================================

    /**
     * 용어집 관리 패널.
     * 카드 리스트 + 검색 + 인라인 편집 모달 + 기본값 복원.
     * @param {HTMLElement} host
     * @constructor
     */
    function VocabularySettingsPanel(host) {
        var self = this;
        self._host = host;
        self._els = {};
        self._listeners = [];
        self._terms = [];
        self._filterQuery = "";
        self._editingId = null;
        self._render();
        self._bind();
        self._load();
    }

    VocabularySettingsPanel.prototype._render = function () {
        this._host.innerHTML = [
            '<div class="settings-content vocabulary-panel">',
            '  <section class="settings-section">',
            '    <div class="vocab-header">',
            '      <div>',
            '        <h3 class="settings-section-title">고유명사 용어집</h3>',
            '        <p class="vocab-desc">STT가 자주 잘못 인식하는 이름이나 전문용어를 등록하면 보정 단계에서 자동으로 교정해요.</p>',
            '      </div>',
            '      <button type="button" class="settings-save-btn" id="vocabAddBtn">＋ 용어 추가</button>',
            '    </div>',
            '    <div class="vocab-toolbar">',
            '      <input type="text" class="vocab-search" id="vocabSearch" placeholder="용어·별칭·메모 검색" />',
            '      <span class="vocab-count" id="vocabCount">0개 항목</span>',
            '    </div>',
            '    <div class="vocab-list" id="vocabList"></div>',
            '  </section>',
            '</div>',
            '<div class="modal-overlay hidden" id="vocabModal" role="dialog" aria-modal="true" aria-labelledby="vocabModalTitle">',
            '  <div class="modal-content">',
            '    <h3 class="modal-title" id="vocabModalTitle">용어 추가</h3>',
            '    <div class="modal-field">',
            '      <label class="modal-label" for="vocabFieldTerm">정답 표기 *</label>',
            '      <input type="text" class="modal-input" id="vocabFieldTerm" maxlength="100" placeholder="예: FastAPI" />',
            '    </div>',
            '    <div class="modal-field">',
            '      <label class="modal-label" for="vocabFieldAliases">별칭 (쉼표로 구분)</label>',
            '      <input type="text" class="modal-input" id="vocabFieldAliases" placeholder="예: 패스트api, 패스트에이피아이" />',
            '      <p class="modal-hint">STT가 잘못 들을 법한 표기를 쉼표로 구분해 입력해요.</p>',
            '    </div>',
            '    <div class="modal-field">',
            '      <label class="modal-label" for="vocabFieldNote">메모 (선택)</label>',
            '      <input type="text" class="modal-input" id="vocabFieldNote" maxlength="500" placeholder="예: 디자이너 이름" />',
            '    </div>',
            '    <div class="modal-error" id="vocabModalError"></div>',
            '    <div class="modal-actions">',
            '      <button type="button" class="btn-secondary" id="vocabModalCancel">취소</button>',
            '      <button type="button" class="settings-save-btn" id="vocabModalSave">저장</button>',
            '    </div>',
            '  </div>',
            '</div>',
        ].join("\n");

        this._els = {
            addBtn: document.getElementById("vocabAddBtn"),
            search: document.getElementById("vocabSearch"),
            count: document.getElementById("vocabCount"),
            list: document.getElementById("vocabList"),
            modal: document.getElementById("vocabModal"),
            modalTitle: document.getElementById("vocabModalTitle"),
            fieldTerm: document.getElementById("vocabFieldTerm"),
            fieldAliases: document.getElementById("vocabFieldAliases"),
            fieldNote: document.getElementById("vocabFieldNote"),
            modalError: document.getElementById("vocabModalError"),
            modalSave: document.getElementById("vocabModalSave"),
            modalCancel: document.getElementById("vocabModalCancel"),
        };
    };

    VocabularySettingsPanel.prototype._bind = function () {
        var self = this;
        var els = self._els;

        var onAdd = function () { self._openModal(null); };
        els.addBtn.addEventListener("click", onAdd);
        self._listeners.push({ el: els.addBtn, type: "click", fn: onAdd });

        var onSearch = function () {
            self._filterQuery = els.search.value.trim().toLowerCase();
            self._renderList();
        };
        els.search.addEventListener("input", onSearch);
        self._listeners.push({ el: els.search, type: "input", fn: onSearch });

        var onModalSave = function () { self._submitModal(); };
        els.modalSave.addEventListener("click", onModalSave);
        self._listeners.push({ el: els.modalSave, type: "click", fn: onModalSave });

        var onModalCancel = function () { self._closeModal(); };
        els.modalCancel.addEventListener("click", onModalCancel);
        self._listeners.push({ el: els.modalCancel, type: "click", fn: onModalCancel });

        // 모달 오버레이 클릭으로 닫기
        var onOverlayClick = function (e) {
            if (e.target === els.modal) self._closeModal();
        };
        els.modal.addEventListener("click", onOverlayClick);
        self._listeners.push({ el: els.modal, type: "click", fn: onOverlayClick });

        // Esc 로 모달 닫기
        self._escHandler = function (e) {
            if (e.key === "Escape" && !els.modal.classList.contains("hidden")) {
                self._closeModal();
            }
        };
        document.addEventListener("keydown", self._escHandler);

        // 카드 버튼 이벤트 위임 (편집/삭제) — 리스트 레벨에 한 번만 부착.
        // 재렌더 시에도 동일 핸들러가 재사용되어 리스너 누수가 없다.
        var onListClick = function (e) {
            var target = e.target;
            if (!target || typeof target.getAttribute !== "function") return;
            var action = target.getAttribute("data-action");
            if (!action) return;
            var id = target.getAttribute("data-id");
            if (!id) return;
            if (action === "edit") {
                var term = self._terms.find(function (t) { return t.id === id; });
                if (term) self._openModal(term);
            } else if (action === "delete") {
                self._deleteTerm(id);
            }
        };
        els.list.addEventListener("click", onListClick);
        self._listeners.push({ el: els.list, type: "click", fn: onListClick });
    };

    VocabularySettingsPanel.prototype._load = async function () {
        var self = this;
        try {
            var data = await App.apiRequest("/vocabulary");
            self._terms = data.terms || [];
            self._renderList();
        } catch (err) {
            errorBanner.show("용어집 불러오기 실패: " + (err.message || err));
        }
    };

    VocabularySettingsPanel.prototype._renderList = function () {
        var els = this._els;
        var query = this._filterQuery;
        var filtered = this._terms;
        if (query) {
            filtered = this._terms.filter(function (t) {
                var hay = (t.term || "") + " " + (t.aliases || []).join(" ") + " " + (t.note || "");
                return hay.toLowerCase().indexOf(query) !== -1;
            });
        }

        els.count.textContent = filtered.length.toLocaleString() + "개 항목";

        if (filtered.length === 0) {
            if (this._terms.length === 0) {
                els.list.innerHTML = [
                    '<div class="vocab-empty">',
                    '  <div class="vocab-empty-title">아직 등록된 용어가 없어요</div>',
                    '  <div class="vocab-empty-desc">자주 잘못 인식되는 이름·전문용어를 추가하면 자동으로 교정해 드려요.</div>',
                    '</div>',
                ].join("\n");
            } else {
                els.list.innerHTML = '<div class="vocab-empty"><div class="vocab-empty-desc">검색 결과가 없어요.</div></div>';
            }
            return;
        }

        var self = this;
        var html = filtered.map(function (t) {
            var aliases = (t.aliases || [])
                .map(function (a) {
                    return '<span class="vocab-chip">' + App.escapeHtml(a) + "</span>";
                })
                .join(" ");
            var note = t.note
                ? '<div class="vocab-note">' + App.escapeHtml(t.note) + "</div>"
                : "";
            return [
                '<div class="vocab-card" data-id="' + App.escapeHtml(t.id) + '">',
                '  <div class="vocab-card-main">',
                '    <div class="vocab-term">' + App.escapeHtml(t.term) + "</div>",
                aliases ? '<div class="vocab-aliases">' + aliases + "</div>" : "",
                note,
                "  </div>",
                '  <div class="vocab-card-actions">',
                '    <button type="button" class="btn-icon" data-action="edit" data-id="' + App.escapeHtml(t.id) + '" aria-label="편집">편집</button>',
                '    <button type="button" class="btn-icon btn-icon-destructive" data-action="delete" data-id="' + App.escapeHtml(t.id) + '" aria-label="삭제">삭제</button>',
                "  </div>",
                "</div>",
            ].join("");
        }).join("");
        els.list.innerHTML = html;
        // 이벤트 위임은 _bind()에서 els.list에 한 번만 부착했으므로
        // 재렌더할 때 리스너를 다시 붙일 필요가 없다.
    };

    VocabularySettingsPanel.prototype._openModal = function (term) {
        var els = this._els;
        this._editingId = term ? term.id : null;
        els.modalTitle.textContent = term ? "용어 편집" : "용어 추가";
        els.fieldTerm.value = term ? term.term : "";
        els.fieldAliases.value = term && term.aliases ? term.aliases.join(", ") : "";
        els.fieldNote.value = term && term.note ? term.note : "";
        els.modalError.textContent = "";
        els.modal.classList.remove("hidden");
        setTimeout(function () { els.fieldTerm.focus(); }, 0);
    };

    VocabularySettingsPanel.prototype._closeModal = function () {
        this._els.modal.classList.add("hidden");
        this._editingId = null;
    };

    VocabularySettingsPanel.prototype._submitModal = async function () {
        var self = this;
        var els = self._els;
        var term = els.fieldTerm.value.trim();
        var aliasesRaw = els.fieldAliases.value || "";
        var aliases = aliasesRaw
            .split(",")
            .map(function (s) { return s.trim(); })
            .filter(function (s) { return s.length > 0; });
        var note = els.fieldNote.value.trim() || null;

        if (!term) {
            els.modalError.textContent = "정답 표기를 입력해 주세요.";
            return;
        }

        els.modalError.textContent = "";
        els.modalSave.disabled = true;
        els.modalSave.textContent = "저장 중…";

        try {
            var resp;
            if (self._editingId) {
                resp = await fetch(
                    "/api/vocabulary/terms/" + encodeURIComponent(self._editingId),
                    {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ term: term, aliases: aliases, note: note }),
                    }
                );
            } else {
                resp = await fetch("/api/vocabulary/terms", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ term: term, aliases: aliases, note: note }),
                });
            }
            if (!resp.ok) {
                var errData = await resp.json().catch(function () { return {}; });
                throw new Error(errData.detail || "HTTP " + resp.status);
            }
            self._closeModal();
            await self._load();
        } catch (err) {
            els.modalError.textContent = err.message || String(err);
        } finally {
            els.modalSave.disabled = false;
            els.modalSave.textContent = "저장";
        }
    };

    VocabularySettingsPanel.prototype._deleteTerm = async function (id) {
        var term = this._terms.find(function (t) { return t.id === id; });
        var name = term ? term.term : id;
        if (!window.confirm('"' + name + '" 용어를 삭제할까요?')) return;
        try {
            var resp = await fetch("/api/vocabulary/terms/" + encodeURIComponent(id), {
                method: "DELETE",
            });
            if (!resp.ok && resp.status !== 204) {
                var errData = await resp.json().catch(function () { return {}; });
                throw new Error(errData.detail || "HTTP " + resp.status);
            }
            await this._load();
        } catch (err) {
            errorBanner.show("삭제 실패: " + (err.message || err));
        }
    };

    VocabularySettingsPanel.prototype.isDirty = function () {
        // CRUD 기반이라 뷰 레벨 dirty는 없음 (모달 편집 중에도 저장 전엔 persist 안 됨)
        return false;
    };

    VocabularySettingsPanel.prototype.destroy = function () {
        this._listeners.forEach(function (e) {
            e.el.removeEventListener(e.type, e.fn);
        });
        this._listeners = [];
        if (this._escHandler) {
            document.removeEventListener("keydown", this._escHandler);
            this._escHandler = null;
        }
    };


    // =================================================================
    // === 글로벌 리소스 모니터 (모든 뷰 공통 상단) ===
    // =================================================================

    /**
     * 모든 탭에서 항상 표시되는 RAM/CPU/모델 상태 바.
     * body 우측 상단에 fixed 로 부착되며, 5초마다 /api/system/resources 폴링.
     * 단일 인스턴스로 충분하므로 IIFE 가 아닌 모듈 객체로 노출한다.
     */
    var GlobalResourceBar = (function () {
        var _timer = null;
        var _el = null;

        function _ensureDom() {
            if (_el) return _el;
            _el = document.createElement("div");
            _el.id = "globalResourceBar";
            _el.className = "global-resource-bar";
            _el.setAttribute("role", "status");
            _el.setAttribute("aria-live", "polite");
            _el.innerHTML = [
                '<div class="grb-item">',
                '  <span class="grb-label">RAM</span>',
                '  <div class="grb-bar-bg"><div class="grb-bar-fill" id="grb-ram-bar"></div></div>',
                '  <span class="grb-value" id="grb-ram-text">--</span>',
                '</div>',
                '<div class="grb-item">',
                '  <span class="grb-label">CPU</span>',
                '  <div class="grb-bar-bg"><div class="grb-bar-fill" id="grb-cpu-bar"></div></div>',
                '  <span class="grb-value" id="grb-cpu-text">--</span>',
                '</div>',
                '<div class="grb-model" id="grb-model-text" title="현재 로드된 모델"></div>',
            ].join("");
            document.body.appendChild(_el);
            return _el;
        }

        function _refresh() {
            App.apiRequest("/system/resources")
                .then(function (data) {
                    var ramBar = document.getElementById("grb-ram-bar");
                    var ramText = document.getElementById("grb-ram-text");
                    if (ramBar && ramText) {
                        // 표시 텍스트(used/total)와 막대를 일치시키기 위해
                        // psutil 의 ram_percent (macOS 에서는 wired+inactive 포함으로 과대표시)
                        // 대신 used/total 비율을 직접 계산한다.
                        var ramPct = 0;
                        if (data.ram_total_gb > 0) {
                            ramPct = Math.round(
                                (data.ram_used_gb / data.ram_total_gb) * 100
                            );
                        }
                        ramBar.style.width = ramPct + "%";
                        ramBar.className = "grb-bar-fill" +
                            (ramPct > 85 ? " danger" : ramPct > 70 ? " warning" : "");
                        ramText.textContent = data.ram_used_gb + "/" + data.ram_total_gb + "G";
                    }
                    var cpuBar = document.getElementById("grb-cpu-bar");
                    var cpuText = document.getElementById("grb-cpu-text");
                    if (cpuBar && cpuText) {
                        var cpuPct = data.cpu_percent || 0;
                        cpuBar.style.width = cpuPct + "%";
                        cpuBar.className = "grb-bar-fill" +
                            (cpuPct > 85 ? " danger" : cpuPct > 70 ? " warning" : "");
                        cpuText.textContent = cpuPct + "%";
                    }
                    var modelText = document.getElementById("grb-model-text");
                    if (modelText) {
                        modelText.textContent = data.loaded_model || "";
                    }
                })
                .catch(function () {
                    // 서버 미시작 등은 무시
                });
        }

        function start() {
            _ensureDom();
            _refresh();
            if (_timer) clearInterval(_timer);
            _timer = setInterval(_refresh, 5000);
        }

        function stop() {
            if (_timer) {
                clearInterval(_timer);
                _timer = null;
            }
        }

        return { start: start, stop: stop, refresh: _refresh };
    })();


    // =================================================================
    // === 키보드 단축키 (글로벌) ===
    // =================================================================

    // 편집 컨텍스트 판별: 텍스트 입력 중에는 전역 단축키가 키 입력을 가로채지 않도록.
    // 단, Command Palette 자체 입력창은 예외(팔레트 내부 동작은 자체 핸들러가 처리).
    function isEditingContext(target) {
        if (!target) return false;
        var inPalette =
            target.closest && target.closest(".command-palette-overlay");
        if (inPalette) return false;
        var tag = (target.tagName || "").toUpperCase();
        return (
            tag === "INPUT" || tag === "TEXTAREA" || target.isContentEditable
        );
    }

    // 통합 전역 키핸들러 (WS-3 소유).
    //   ⌘K  → Command Palette 열기
    //   ⌘,  → 설정 이동 (macOS 관례상 편집 중에도 허용)
    //   ⌘1  → /app (회의록)
    //   ⌘2  → /app/search (검색)
    //   ⌘3  → /app/chat (채팅)
    document.addEventListener("keydown", function (e) {
        if (!(e.metaKey || e.ctrlKey)) return;

        // Cmd+K → Command Palette
        if (e.key === "k") {
            if (isEditingContext(e.target)) return;
            e.preventDefault();
            commandPalette.open();
            return;
        }

        // Cmd+, → 설정 (macOS 표준 단축키 — 편집 중에도 허용)
        if (e.key === ",") {
            e.preventDefault();
            Router.navigate("/app/settings");
            return;
        }

        // Cmd+1/2/3 → 주요 라우트 빠른 전환 (편집 중에는 숫자 입력 보호)
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
    // === CommandPalette — ⌘K 명령 팔레트 (신규) ===
    // =================================================================
    //
    // macOS Spotlight / Raycast 스타일 전역 명령 팔레트.
    // 카테고리: 회의(최신 5건) / 액션(네비게이션·테마) /
    //           STT 모델(활성화 가능) / 도움말.
    //
    // 키보드:
    //   ⌘K     → 열기 (전역 keydown 핸들러 연결됨)
    //   ↑↓     → 항목 선택 (순환)
    //   Enter  → 실행
    //   Esc    → 닫기
    //   외부클릭 → 닫기

    function CommandPalette() {
        this._isOpen = false;
        this._overlayEl = null;
        this._inputEl = null;
        this._resultsEl = null;
        this._items = [];
        this._filteredItems = [];
        this._selectedIdx = 0;
        this._recentActions = [];
        this._meetingsCache = null;
        this._sttModelsCache = null;
        this._boundKeydown = null;
        this._loadRecent();
    }

    var CMDK_STORAGE_KEY = "cmdk-recent-actions";
    var CMDK_RECENT_LIMIT = 20;

    /**
     * LocalStorage 에서 최근 액션 ID 목록 로드.
     */
    CommandPalette.prototype._loadRecent = function () {
        try {
            var raw = localStorage.getItem(CMDK_STORAGE_KEY);
            this._recentActions = raw ? JSON.parse(raw) : [];
            if (!Array.isArray(this._recentActions)) {
                this._recentActions = [];
            }
        } catch (err) {
            this._recentActions = [];
        }
    };

    /**
     * 최근 사용 액션 ID 를 앞쪽에 추가 + 저장.
     */
    CommandPalette.prototype._pushRecent = function (itemId) {
        if (!itemId) return;
        var next = [itemId];
        for (var i = 0; i < this._recentActions.length; i++) {
            if (this._recentActions[i] !== itemId) {
                next.push(this._recentActions[i]);
            }
        }
        this._recentActions = next.slice(0, CMDK_RECENT_LIMIT);
        try {
            localStorage.setItem(
                CMDK_STORAGE_KEY,
                JSON.stringify(this._recentActions)
            );
        } catch (err) {
            // 저장 실패 무시
        }
    };

    /**
     * 간단한 fuzzy 매칭 점수: 완전일치(100) > 접두(50) > 부분(10) > 불일치(0).
     */
    function cmdkFuzzyMatch(query, text) {
        if (!query) return 1;
        if (!text) return 0;
        var q = String(query).toLowerCase();
        var t = String(text).toLowerCase();
        if (t === q) return 100;
        if (t.indexOf(q) === 0) return 50;
        if (t.indexOf(q) >= 0) return 10;
        return 0;
    }

    /**
     * 팔레트 열기. 최초 호출 시 DOM 생성, 매 호출마다 항목 갱신.
     */
    CommandPalette.prototype.open = function () {
        if (this._isOpen) return;
        var self = this;

        // 접근성: 팔레트 열기 전 포커스 요소 저장(닫힘 후 복원)
        this._previousFocus =
            document.activeElement && document.activeElement !== document.body
                ? document.activeElement
                : null;

        if (!this._overlayEl) {
            this._createDom();
        }

        this._items = this._buildStaticItems();
        this._filter("");
        this._selectedIdx = 0;

        this._overlayEl.style.display = "flex";
        document.body.classList.add("command-palette-open");
        this._isOpen = true;
        this._inputEl.value = "";
        this._render();

        setTimeout(function () {
            if (self._inputEl) self._inputEl.focus();
        }, 0);

        this._boundKeydown = function (e) {
            self._handleKeydown(e);
        };
        document.addEventListener("keydown", this._boundKeydown, true);

        this._loadAsyncItems();
    };

    /**
     * 팔레트 닫기.
     */
    CommandPalette.prototype.close = function () {
        if (!this._isOpen) return;
        this._isOpen = false;
        if (this._overlayEl) {
            this._overlayEl.style.display = "none";
        }
        document.body.classList.remove("command-palette-open");
        if (this._boundKeydown) {
            document.removeEventListener("keydown", this._boundKeydown, true);
            this._boundKeydown = null;
        }
        // 접근성: 이전 포커스 복원 (호출 요소로)
        if (this._previousFocus && typeof this._previousFocus.focus === "function") {
            try {
                this._previousFocus.focus();
            } catch (err) {
                // 포커스 복원 실패 무시
            }
        }
        this._previousFocus = null;
    };

    /**
     * 모달 DOM 생성 (최초 1회).
     */
    CommandPalette.prototype._createDom = function () {
        var self = this;

        var overlay = document.createElement("div");
        overlay.className = "command-palette-overlay";
        overlay.style.display = "none";

        var modal = document.createElement("div");
        modal.className = "command-palette";
        modal.setAttribute("role", "dialog");
        modal.setAttribute("aria-modal", "true");
        modal.setAttribute("aria-label", "명령 팔레트");

        var inputWrap = document.createElement("div");
        inputWrap.className = "command-palette-input-wrap";
        var input = document.createElement("input");
        input.type = "text";
        input.className = "command-palette-input";
        input.setAttribute("placeholder", "명령 검색…");
        input.setAttribute("aria-label", "명령 검색");
        input.setAttribute("autocomplete", "off");
        input.setAttribute("spellcheck", "false");
        inputWrap.appendChild(input);

        var results = document.createElement("div");
        results.className = "command-palette-results";
        results.setAttribute("role", "listbox");
        results.setAttribute("aria-label", "명령 결과");

        modal.appendChild(inputWrap);
        modal.appendChild(results);
        overlay.appendChild(modal);
        document.body.appendChild(overlay);

        this._overlayEl = overlay;
        this._inputEl = input;
        this._resultsEl = results;

        // 입력 → 즉시 필터링
        input.addEventListener("input", function () {
            self._filter(input.value);
            self._selectedIdx = 0;
            self._render();
        });

        // 모달 내부 클릭은 전파 차단 (외부 클릭 닫기와 분리)
        modal.addEventListener("click", function (e) {
            e.stopPropagation();
        });

        // 외부 클릭 → 닫기
        overlay.addEventListener("click", function () {
            self.close();
        });

        // 결과 클릭 → 해당 항목 실행
        results.addEventListener("click", function (e) {
            var target = e.target;
            while (target && target !== results) {
                if (
                    target.classList &&
                    target.classList.contains("command-palette-item")
                ) {
                    var idx = parseInt(target.getAttribute("data-idx"), 10);
                    if (!isNaN(idx) && self._filteredItems[idx]) {
                        self._executeItem(self._filteredItems[idx]);
                    }
                    return;
                }
                target = target.parentNode;
            }
        });
    };

    /**
     * 정적 항목(액션/도움말) 빌드. 비동기 항목은 _loadAsyncItems() 가 이후 주입.
     */
    CommandPalette.prototype._buildStaticItems = function () {
        var items = [];

        items.push({
            id: "action:goto-settings",
            category: "액션",
            title: "설정 열기",
            subtitle: "일반 / 프롬프트 / 용어집",
            run: function () {
                Router.navigate("/app/settings");
            },
        });
        items.push({
            id: "action:goto-chat",
            category: "액션",
            title: "채팅 열기",
            subtitle: "회의록 기반 대화",
            run: function () {
                Router.navigate("/app/chat");
            },
        });
        items.push({
            id: "action:goto-search",
            category: "액션",
            title: "검색 열기",
            subtitle: "전체 회의 하이브리드 검색",
            run: function () {
                Router.navigate("/app/search");
            },
        });
        items.push({
            id: "action:toggle-theme",
            category: "액션",
            title: "다크 모드 전환",
            subtitle: "라이트 ↔ 다크 테마 토글",
            run: function () {
                var root = document.documentElement;
                var current = root.getAttribute("data-theme");
                var next;
                if (current === "dark") {
                    next = "light";
                } else if (current === "light") {
                    next = "dark";
                } else {
                    next = window.matchMedia("(prefers-color-scheme: dark)")
                        .matches
                        ? "light"
                        : "dark";
                }
                root.setAttribute("data-theme", next);
                try {
                    localStorage.setItem("theme", next);
                } catch (err) {
                    // 저장 실패 무시
                }
            },
        });
        items.push({
            id: "action:goto-home",
            category: "액션",
            title: "홈으로 이동",
            subtitle: "회의 목록 대시보드",
            run: function () {
                Router.navigate("/app");
            },
        });

        items.push({
            id: "help:shortcuts",
            category: "도움말",
            title: "키보드 단축키",
            subtitle: "⌘K 명령 팔레트 · ⌘F 찾기 · ⌘S 저장",
            run: function () {
                alert(
                    "키보드 단축키\n\n" +
                        "⌘K  명령 팔레트 열기\n" +
                        "⌘F  뷰어 내 찾기\n" +
                        "⌘S  저장"
                );
            },
        });

        return items;
    };

    /**
     * /api/meetings, /api/stt-models 를 병렬 호출해 항목 주입.
     */
    CommandPalette.prototype._loadAsyncItems = function () {
        var self = this;

        App.apiRequest("/meetings")
            .then(function (data) {
                var meetings = (data && data.meetings) || [];
                self._meetingsCache = meetings;
                var added = [];
                for (var i = 0; i < Math.min(meetings.length, 5); i++) {
                    var m = meetings[i];
                    if (!m || !m.id) continue;
                    var title =
                        m.title ||
                        (App.getFileName
                            ? App.getFileName(m.audio_file || "")
                            : "") ||
                        m.id;
                    added.push({
                        id: "meeting:" + m.id,
                        category: "회의",
                        title: String(title),
                        subtitle: m.created_at
                            ? App.formatDate
                                ? App.formatDate(m.created_at)
                                : String(m.created_at)
                            : "",
                        run: (function (mid) {
                            return function () {
                                Router.navigate(
                                    "/app/viewer/" + encodeURIComponent(mid)
                                );
                            };
                        })(m.id),
                    });
                }
                self._mergeDynamicItems("회의", added);
                self._filter(self._inputEl ? self._inputEl.value : "");
                self._render();
            })
            .catch(function () {
                // 실패 시 정적 항목만 유지
            });

        App.apiRequest("/stt-models")
            .then(function (data) {
                var models = (data && data.models) || [];
                self._sttModelsCache = models;
                var added = [];
                for (var i = 0; i < models.length; i++) {
                    var m = models[i];
                    if (!m || !m.id) continue;
                    var st = m.status || "";
                    if (
                        st &&
                        st !== "ready" &&
                        st !== "active" &&
                        st !== "downloaded"
                    ) {
                        continue;
                    }
                    added.push({
                        id: "stt:" + m.id,
                        category: "STT 모델",
                        title: "모델 활성화: " + (m.name || m.id),
                        subtitle: m.description || m.id,
                        run: (function (modelId) {
                            return function () {
                                App.apiPost(
                                    "/stt-models/" +
                                        encodeURIComponent(modelId) +
                                        "/activate",
                                    {}
                                ).catch(function () {});
                                Router.navigate("/app/settings/general");
                            };
                        })(m.id),
                    });
                }
                self._mergeDynamicItems("STT 모델", added);
                self._filter(self._inputEl ? self._inputEl.value : "");
                self._render();
            })
            .catch(function () {
                // 실패 시 정적 항목만 유지
            });
    };

    /**
     * 특정 카테고리의 기존 항목을 제거하고 새 항목으로 교체 (중복 방지).
     */
    CommandPalette.prototype._mergeDynamicItems = function (category, added) {
        var base = [];
        for (var i = 0; i < this._items.length; i++) {
            if (this._items[i].category !== category) {
                base.push(this._items[i]);
            }
        }
        this._items = base.concat(added);
    };

    /**
     * 쿼리로 필터링 + 최근 사용 가중치로 정렬.
     */
    CommandPalette.prototype._filter = function (query) {
        var q = (query || "").trim();
        var scored = [];
        for (var i = 0; i < this._items.length; i++) {
            var item = this._items[i];
            var haystack =
                (item.title || "") +
                " " +
                (item.subtitle || "") +
                " " +
                (item.category || "");
            var score = cmdkFuzzyMatch(q, haystack);
            if (score > 0 || !q) {
                var recentIdx = this._recentActions.indexOf(item.id);
                var recentBoost =
                    recentIdx >= 0 ? CMDK_RECENT_LIMIT - recentIdx : 0;
                scored.push({ item: item, score: score + recentBoost });
            }
        }
        scored.sort(function (a, b) {
            return b.score - a.score;
        });
        this._filteredItems = scored.map(function (s) {
            return s.item;
        });
    };

    /**
     * 카테고리별로 그룹화해 렌더. XSS 방지: textContent 만 사용.
     */
    CommandPalette.prototype._render = function () {
        if (!this._resultsEl) return;
        var results = this._resultsEl;
        while (results.firstChild) {
            results.removeChild(results.firstChild);
        }

        if (this._filteredItems.length === 0) {
            var empty = document.createElement("div");
            empty.className = "command-palette-empty";
            empty.textContent = "결과가 없습니다";
            results.appendChild(empty);
            return;
        }

        var groups = {};
        var groupOrder = [];
        for (var i = 0; i < this._filteredItems.length; i++) {
            var it = this._filteredItems[i];
            var cat = it.category || "기타";
            if (!groups[cat]) {
                groups[cat] = [];
                groupOrder.push(cat);
            }
            groups[cat].push({ item: it, idx: i });
        }

        for (var g = 0; g < groupOrder.length; g++) {
            var catName = groupOrder[g];
            var groupEl = document.createElement("div");
            groupEl.className = "command-palette-group";
            groupEl.setAttribute("role", "group");
            groupEl.setAttribute("aria-label", catName);

            var header = document.createElement("div");
            header.className = "command-palette-category";
            header.textContent = catName;
            groupEl.appendChild(header);

            for (var k = 0; k < groups[catName].length; k++) {
                var entry = groups[catName][k];
                var itemEl = document.createElement("div");
                itemEl.className = "command-palette-item";
                itemEl.setAttribute("role", "option");
                itemEl.setAttribute("data-idx", String(entry.idx));
                if (entry.idx === this._selectedIdx) {
                    itemEl.classList.add("selected");
                    itemEl.setAttribute("aria-selected", "true");
                } else {
                    itemEl.setAttribute("aria-selected", "false");
                }

                var titleEl = document.createElement("div");
                titleEl.className = "command-palette-item-title";
                titleEl.textContent = entry.item.title || "";
                itemEl.appendChild(titleEl);

                if (entry.item.subtitle) {
                    var subEl = document.createElement("div");
                    subEl.className = "command-palette-item-subtitle";
                    subEl.textContent = entry.item.subtitle;
                    itemEl.appendChild(subEl);
                }

                groupEl.appendChild(itemEl);
            }

            results.appendChild(groupEl);
        }

        var selectedEl = results.querySelector(
            ".command-palette-item.selected"
        );
        if (selectedEl && selectedEl.scrollIntoView) {
            selectedEl.scrollIntoView({ block: "nearest" });
        }
    };

    /**
     * 팔레트 내부 키보드 조작 (document capture phase).
     */
    CommandPalette.prototype._handleKeydown = function (e) {
        if (!this._isOpen) return;
        if (e.key === "Escape") {
            e.preventDefault();
            e.stopPropagation();
            this.close();
            return;
        }
        if (e.key === "ArrowDown") {
            e.preventDefault();
            if (this._filteredItems.length > 0) {
                this._selectedIdx =
                    (this._selectedIdx + 1) % this._filteredItems.length;
                this._render();
            }
            return;
        }
        if (e.key === "ArrowUp") {
            e.preventDefault();
            if (this._filteredItems.length > 0) {
                this._selectedIdx =
                    (this._selectedIdx - 1 + this._filteredItems.length) %
                    this._filteredItems.length;
                this._render();
            }
            return;
        }
        if (e.key === "Enter") {
            e.preventDefault();
            var item = this._filteredItems[this._selectedIdx];
            if (item) this._executeItem(item);
            return;
        }
    };

    /**
     * 항목 실행: 최근 목록 갱신 → 닫기 → run().
     */
    CommandPalette.prototype._executeItem = function (item) {
        if (!item || typeof item.run !== "function") return;
        this._pushRecent(item.id);
        this.close();
        try {
            item.run();
        } catch (err) {
            // 실행 실패 무시
        }
    };

    // 싱글턴 인스턴스 (전역 ⌘K 핸들러에서 사용)
    var commandPalette = new CommandPalette();


    // =================================================================
    // === 공개 API ===
    // =================================================================

    window.SPA = {
        Router: Router,
        NavBar: NavBar,
        ListPanel: ListPanel,
        EmptyView: EmptyView,
        SearchView: SearchView,
        ViewerView: ViewerView,
        ChatView: ChatView,
        SettingsView: SettingsView,
        CommandPalette: commandPalette,
    };


    // =================================================================
    // === 초기화 ===
    // =================================================================

    // WebSocket 연결
    App.connectWebSocket();

    // 네비게이션 바 초기화
    NavBar.init();

    // 리스트 패널 초기화
    ListPanel.init();

    // 라우터 초기화 (현재 경로에 맞는 뷰 렌더링)
    Router.init();

    // 글로벌 리소스 모니터 시작 (모든 탭 공통 상단 표시)
    GlobalResourceBar.start();

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
