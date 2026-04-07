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
                if (pathname === "/app/chat" || pathname.indexOf("/app/settings") === 0) {
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

            // 뒤로가기/앞으로가기 처리
            window.addEventListener("popstate", function () {
                var fullPath = window.location.pathname + window.location.search;
                resolve(fullPath);
            });

            // 현재 경로에 맞는 뷰 렌더링
            var path = window.location.pathname;

            // /static/index.html 또는 루트 경로 → /app 으로 리다이렉트
            if (path === "/" || path === "/static/index.html" || path === "/static/" || path === "/index.html") {
                history.replaceState(null, "", "/app");
                path = "/app";
            }

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
            document.addEventListener("ws:recording_started", function () {
                var recStatus = document.getElementById("recordingStatus");
                if (recStatus) recStatus.classList.add("visible");
                var recDuration = document.getElementById("recordingDuration");
                if (recDuration) App.safeText(recDuration, "00:00");
                loadMeetings();
            });
            document.addEventListener("ws:recording_stopped", function () {
                var recStatus = document.getElementById("recordingStatus");
                if (recStatus) recStatus.classList.remove("visible");
                loadMeetings();
            });
            document.addEventListener("ws:recording_duration", function (e) {
                var detail = e.detail || {};
                var seconds = detail.duration_seconds || 0;
                var recDuration = document.getElementById("recordingDuration");
                if (recDuration) {
                    var sec = Math.floor(seconds);
                    var m = Math.floor(sec / 60);
                    var s = sec % 60;
                    var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
                    App.safeText(recDuration, pad(m) + ":" + pad(s));
                }
            });
            document.addEventListener("ws:recording_error", function (e) {
                var detail = e.detail || {};
                var recStatus = document.getElementById("recordingStatus");
                if (recStatus) recStatus.classList.remove("visible");
                var msg = detail.error || detail.message || "녹음 중 오류가 발생했습니다";
                errorBanner.show(msg);
            });

            // 초기 데이터 로드
            loadMeetings();
            fetchStatus();

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
         */
        async function loadMeetings() {
            try {
                var data = await App.apiRequest("/meetings");
                _meetings = data.meetings || [];
                _applyFilterAndSort();
            } catch (e) {
                // 조용히 처리 (리스트 로드 실패는 치명적이지 않음)
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
                var empty = document.createElement("div");
                empty.className = "list-empty";
                empty.textContent = "회의 없음";
                _listEl.appendChild(empty);
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

                // 제목: 날짜 기반
                var titleEl = document.createElement("div");
                titleEl.className = "meeting-item-title";
                titleEl.textContent = _extractTitle(meeting.meeting_id, meeting.created_at);

                // 요약 프리뷰 1줄
                var previewEl = document.createElement("div");
                previewEl.className = "meeting-item-preview";
                if (meeting.summary_preview) {
                    previewEl.textContent = meeting.summary_preview;
                } else if (meeting.status === "completed") {
                    previewEl.textContent = App.getStatusLabel(meeting.status);
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
        this._resourceTimer = null;
        this._render();
        this._loadResources();
        // 5초마다 리소스 갱신
        var self = this;
        this._resourceTimer = setInterval(function () {
            self._loadResources();
        }, 5000);
    }

    /**
     * EmptyView DOM을 생성한다.
     * 리소스 모니터 + 일괄 요약 버튼 + 안내 메시지를 표시한다.
     */
    EmptyView.prototype._render = function () {
        var contentEl = Router.getContentEl();
        contentEl.innerHTML = "";

        var html = [
            // 리소스 모니터 섹션
            '<div class="resource-monitor">',
            '  <div class="section-title">시스템 상태</div>',
            '  <div class="resource-bars">',
            '    <div class="resource-item">',
            '      <div class="resource-label">',
            '        <span>RAM</span>',
            '        <span class="resource-value" id="res-ram-text">--</span>',
            '      </div>',
            '      <div class="resource-bar-bg">',
            '        <div class="resource-bar-fill" id="res-ram-bar" style="width:0%"></div>',
            '      </div>',
            '    </div>',
            '    <div class="resource-item">',
            '      <div class="resource-label">',
            '        <span>CPU</span>',
            '        <span class="resource-value" id="res-cpu-text">--</span>',
            '      </div>',
            '      <div class="resource-bar-bg">',
            '        <div class="resource-bar-fill" id="res-cpu-bar" style="width:0%"></div>',
            '      </div>',
            '    </div>',
            '  </div>',
            '  <div class="resource-model" id="res-model-text"></div>',
            '</div>',
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
        document.title = "회의록";

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
     * 시스템 리소스 정보를 조회하여 UI에 반영한다.
     * GET /api/system/resources
     */
    EmptyView.prototype._loadResources = function () {
        App.apiRequest("/system/resources")
            .then(function (data) {
                // RAM 바
                var ramBar = document.getElementById("res-ram-bar");
                var ramText = document.getElementById("res-ram-text");
                if (ramBar && ramText) {
                    var ramPct = data.ram_percent || 0;
                    ramBar.style.width = ramPct + "%";
                    ramBar.className = "resource-bar-fill" +
                        (ramPct > 85 ? " danger" : ramPct > 70 ? " warning" : "");
                    ramText.textContent =
                        data.ram_used_gb + " / " + data.ram_total_gb + " GB (" + ramPct + "%)";
                }
                // CPU 바
                var cpuBar = document.getElementById("res-cpu-bar");
                var cpuText = document.getElementById("res-cpu-text");
                if (cpuBar && cpuText) {
                    var cpuPct = data.cpu_percent || 0;
                    cpuBar.style.width = cpuPct + "%";
                    cpuBar.className = "resource-bar-fill" +
                        (cpuPct > 85 ? " danger" : cpuPct > 70 ? " warning" : "");
                    cpuText.textContent = cpuPct + "%";
                }
                // 로드된 모델
                var modelText = document.getElementById("res-model-text");
                if (modelText) {
                    modelText.textContent = data.loaded_model
                        ? "로드된 모델: " + data.loaded_model
                        : "";
                }
            })
            .catch(function () {
                // 리소스 조회 실패 시 무시 (서버 미시작 등)
            });
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
     * 뷰를 정리한다. 리소스 모니터 타이머를 해제한다.
     */
    EmptyView.prototype.destroy = function () {
        if (this._resourceTimer) {
            clearInterval(this._resourceTimer);
            this._resourceTimer = null;
        }
    };


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
            '    <div class="loading-overlay" id="searchLoading" role="status" aria-live="polite">',
            '      <span class="loading-spinner" aria-hidden="true"></span>',
            '      <span>검색 중...</span>',
            '    </div>',
            '    <div class="empty-state" id="searchEmpty" style="display:none;">',
            '      <div class="empty-state-icon"><svg class="icon icon-lg" width="48" height="48" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg"><circle cx="20" cy="20" r="14" stroke="currentColor" stroke-width="3"/><path d="M30 30l12 12" stroke="currentColor" stroke-width="3" stroke-linecap="round"/></svg></div>',
            '      <div class="empty-state-text" id="searchEmptyText">검색 결과가 없습니다</div>',
            '      <div class="empty-state-sub" id="searchEmptySub">다른 검색어나 필터를 시도해보세요</div>',
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

        document.title = "검색 — 회의록";
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
                    "다른 키워드를 사용하거나, AI Chat에서 자연어로 질문해 보세요");
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
            '  </div>',
            '  <div class="viewer-meta">',
            '    <span class="viewer-meta-item" id="viewerMetaFile"></span>',
            '    <span class="viewer-meta-item" id="viewerMetaDate"></span>',
            '    <span class="viewer-meta-item" id="viewerMetaSpeakers"></span>',
            '    <span class="viewer-meta-item" id="viewerMetaUtterances"></span>',
            '  </div>',
            '  <div class="speaker-legend" id="viewerSpeakerLegend"></div>',
            '  <div class="viewer-actions" id="viewerActions"></div>',
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
            '          aria-controls="viewerPanelSummary">회의록 (AI 요약)</button>',
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

            '  <div class="loading-overlay" id="viewerTranscriptLoading" role="status" aria-live="polite">',
            '    <span class="loading-spinner" aria-hidden="true"></span>',
            '    <span>전사문 불러오는 중...</span>',
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

            '  <div class="loading-overlay" id="viewerSummaryLoading" role="status" aria-live="polite">',
            '    <span class="loading-spinner" aria-hidden="true"></span>',
            '    <span>회의록 불러오는 중...</span>',
            '  </div>',

            '  <div class="empty-state" id="viewerSummaryEmpty" style="display:none;">',
            '    <div class="empty-state-icon"><svg class="icon icon-lg" width="48" height="48" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg"><rect x="10" y="6" width="28" height="36" rx="4" stroke="currentColor" stroke-width="2.5"/><path d="M18 6v-1a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v1M16 18h16M16 26h10" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg></div>',
            '    <div class="empty-state-text">회의록이 아직 생성되지 않았습니다</div>',
            '    <div class="empty-state-sub">',
            '      전사가 완료된 후 아래 버튼을 눌러 AI 요약을 생성할 수 있습니다.',
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
        };

        // 페이지 타이틀 업데이트
        document.title = this._meetingId + " — 전사문 뷰어";
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
                        els.summaryContent.innerHTML = App.renderMarkdown(summary.markdown);
                        els.summaryEmpty.style.display = "none";
                        btn.style.display = "none";
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

            header.appendChild(speakerEl);
            header.appendChild(timeEl);

            // 텍스트
            var textEl = document.createElement("div");
            textEl.className = "utterance-text";

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
            App.safeText(els.meetingTitle, data.meeting_id);

            els.meetingStatus.className = "viewer-status " + data.status;
            App.safeText(els.meetingStatus, App.getStatusLabel(data.status));

            els.metaFile.innerHTML = Icons.mic + ' <span>' + App.escapeHtml(App.getFileName(data.audio_path)) + '</span>';
            els.metaDate.innerHTML = Icons.calendar + ' <span>' + App.escapeHtml(App.formatDate(data.created_at)) + '</span>';

            // 액션 버튼 렌더링 (전사 시작, 재시도, 요약 생성, 삭제)
            // _loadTranscript 완료 후 다시 호출되어 복사/다운로드 버튼이 갱신됨
            self._lastMeetingData = data;
            self._renderActions(data);

        } catch (e) {
            if (e.status === 404) {
                errorBanner.show("회의를 찾을 수 없습니다: " + self._meetingId);
            } else {
                errorBanner.show("회의 정보 로드 실패: " + e.message);
            }
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

        // 전사문 복사/다운로드 버튼 (완료된 회의이며 전사문이 로드된 경우)
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
        var originalText = btn.textContent;
        btn.disabled = true;
        btn.textContent = "요약 중...";
        btn.classList.add("loading");

        try {
            await App.apiPost("/meetings/" + encodeURIComponent(meetingId) + "/summarize", {});
        } catch (e) {
            errorBanner.show("요약 요청 실패: " + e.message);
            btn.disabled = false;
            btn.textContent = originalText;
            btn.classList.remove("loading");
        }
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

            // 마크다운 렌더링 + 재생성 버튼
            els.summaryContent.innerHTML = App.renderMarkdown(data.markdown);

            // 재생성 버튼 추가
            var regenerateDiv = document.createElement("div");
            regenerateDiv.className = "summary-regenerate";
            var regenerateBtn = document.createElement("button");
            regenerateBtn.className = "btn-regenerate";
            regenerateBtn.innerHTML = Icons.gear + ' 요약 재생성';
            regenerateBtn.addEventListener("click", function () {
                self._requestSummarize(true);
            });
            regenerateDiv.appendChild(regenerateBtn);
            els.summaryContent.appendChild(regenerateDiv);

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

        // 페이지 타이틀 복원
        document.title = "회의록";
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
        return '<div class="welcome-message" id="chatWelcomeMessage">' +
            '<div class="welcome-icon"><svg class="icon icon-lg" width="48" height="48" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M6 8h36a2 2 0 0 1 2 2v20a2 2 0 0 1-2 2H18l-8 6V32H6a2 2 0 0 1-2-2V10a2 2 0 0 1 2-2Z" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/><circle cx="16" cy="20" r="2" fill="currentColor"/><circle cx="24" cy="20" r="2" fill="currentColor"/><circle cx="32" cy="20" r="2" fill="currentColor"/></svg></div>' +
            '<div class="welcome-title">AI 회의 어시스턴트</div>' +
            '<div class="welcome-desc">' +
                '회의 내용에 대해 자유롭게 질문하세요.<br>' +
                '관련 회의 내용을 검색하여 AI가 답변합니다.' +
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
            '    <span class="typing-text">AI가 답변을 생성하고 있습니다...</span>',
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
        document.title = "AI Chat — 회의록";
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
            notice.textContent = "\u26A0 AI 모델 응답 불가: " + data.error_message;
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
                errorBanner.show("AI 엔진이 아직 준비되지 않았습니다. 잠시 후 다시 시도해 주세요.");
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
        document.title = "회의록";
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
        document.title = "설정 — 회의록";

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
        document.title = "회의록";
    };


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
                rec.textContent = "⭐ 추천";
                header.appendChild(rec);
            }
            if (m.is_active) {
                var act = document.createElement("span");
                act.className = "stt-model-badge active";
                act.textContent = "● 활성";
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
    GeneralSettingsPanel.prototype._downloadModel = async function (modelId) {
        var self = this;
        try {
            self._showSttStatus("다운로드를 시작합니다…", "info");
            await App.apiPost("/stt-models/" + modelId + "/download", {});
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
            desc: "회의록을 검색해 답변하는 AI 채팅에 사용해요.",
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
                    '  <div class="vocab-empty-desc">자주 잘못 인식되는 이름·전문용어를 추가하면 AI가 자동으로 교정해 드려요.</div>',
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
    // === 키보드 단축키 (글로벌) ===
    // =================================================================

    document.addEventListener("keydown", function (e) {
        // Cmd+K → 검색 뷰로 이동
        if ((e.metaKey || e.ctrlKey) && e.key === "k") {
            e.preventDefault();
            Router.navigate("/app/search");
            // 검색 입력에 포커스
            setTimeout(function () {
                var searchInput = document.getElementById("searchQuery");
                if (searchInput) searchInput.focus();
            }, 100);
        }
    });


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
