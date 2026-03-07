/* =================================================================
 * 회의 전사 시스템 — SPA 모듈 (spa.js)
 *
 * 목적: 3개의 독립 HTML 페이지(index, viewer, chat)를
 *       단일 페이지 애플리케이션(SPA)으로 통합한다.
 *       History API 기반 클라이언트 라우터, 사이드바 회의 목록,
 *       HomeView / ViewerView / ChatView 를 제공한다.
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
        failed: 6,
        completed: 7,
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
                // /app/chat
                pattern: /^\/app\/chat$/,
                handler: function () {
                    return new ChatView();
                },
            },
            {
                // /app (홈) — 기본 라우트
                pattern: /^\/app\/?$/,
                handler: function () {
                    return new HomeView();
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

            // 경로 매칭
            for (var i = 0; i < routes.length; i++) {
                var match = pathname.match(routes[i].pattern);
                if (match) {
                    _currentView = routes[i].handler(match);
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
                // navigate()와 동일하게 전체 경로 전달 (resolve() 내부에서 쿼리 분리)
                var fullPath = window.location.pathname + window.location.search;
                resolve(fullPath);
                Sidebar.setActiveFromPath(window.location.pathname);
            });

            // 현재 경로에 맞는 뷰 렌더링
            var path = window.location.pathname;

            // /static/index.html 또는 루트 경로 → /app 으로 리다이렉트
            if (path === "/" || path === "/static/index.html" || path === "/static/" || path === "/index.html") {
                history.replaceState(null, "", "/app");
                path = "/app";
            }

            resolve(path);
            Sidebar.setActiveFromPath(path);
        }

        /**
         * 지정 경로로 내비게이션한다.
         * @param {string} path - 이동할 경로
         */
        function navigate(path) {
            // 현재 URL과 동일하면 무시 (경로 + 쿼리 스트링 모두 비교)
            var current = window.location.pathname + window.location.search;
            if (current === path) return;
            history.pushState(null, "", path);
            resolve(path);
            Sidebar.setActiveFromPath(path);
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
    // === Sidebar (사이드바 회의 목록) ===
    // =================================================================

    var Sidebar = (function () {
        var _meetings = [];           // 전체 회의 목록 데이터
        var _activeId = null;         // 현재 활성화된 회의 ID
        var _listEl = null;           // #meeting-list 엘리먼트
        var _searchEl = null;         // #sidebar-search 엘리먼트
        var _statusDot = null;        // #statusDot 엘리먼트
        var _statusText = null;       // #statusText 엘리먼트
        var _statusTimer = null;      // 상태 폴링 타이머
        var _meetingsTimer = null;    // 회의 목록 폴링 타이머
        var _searchTimeout = null;    // 검색 디바운스 타이머

        /**
         * 사이드바를 초기화한다.
         */
        function init() {
            _listEl = document.getElementById("meeting-list");
            _searchEl = document.getElementById("sidebar-search");
            _statusDot = document.getElementById("statusDot");
            _statusText = document.getElementById("statusText");

            // 검색 입력 디바운스
            _searchEl.addEventListener("input", function () {
                clearTimeout(_searchTimeout);
                _searchTimeout = setTimeout(function () {
                    filter(_searchEl.value.trim());
                }, 250);
            });

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
         * 회의 목록을 API에서 가져와 사이드바에 렌더링한다.
         */
        async function loadMeetings() {
            try {
                var data = await App.apiRequest("/meetings");
                _meetings = data.meetings || [];
                render(_meetings);
            } catch (e) {
                // 조용히 처리 (사이드바 로드 실패는 치명적이지 않음)
            }
        }

        /**
         * 회의 목록을 렌더링한다.
         * @param {Array} meetings - 회의 목록
         */
        function render(meetings) {
            _listEl.innerHTML = "";

            if (meetings.length === 0) {
                var empty = document.createElement("li");
                empty.className = "meeting-list-empty";
                empty.textContent = "회의가 없습니다";
                _listEl.appendChild(empty);
                return;
            }

            // 최신순 정렬
            var sorted = meetings.slice().sort(function (a, b) {
                return (b.created_at || "").localeCompare(a.created_at || "");
            });

            sorted.forEach(function (meeting) {
                var li = document.createElement("li");
                li.className = "meeting-list-item";
                if (meeting.meeting_id === _activeId) {
                    li.classList.add("active");
                }

                // 상태 아이콘
                var statusIcon = document.createElement("span");
                statusIcon.className = "meeting-list-status " + meeting.status;
                statusIcon.textContent = App.getStatusLabel(meeting.status);

                // 회의 ID
                var titleEl = document.createElement("span");
                titleEl.className = "meeting-list-title";
                titleEl.textContent = meeting.meeting_id;

                // 날짜
                var dateEl = document.createElement("span");
                dateEl.className = "meeting-list-date";
                dateEl.textContent = App.formatDate(meeting.created_at);

                li.appendChild(titleEl);
                li.appendChild(statusIcon);
                li.appendChild(dateEl);

                // 클릭 → ViewerView로 이동
                li.addEventListener("click", function () {
                    Router.navigate("/app/viewer/" + encodeURIComponent(meeting.meeting_id));
                });

                _listEl.appendChild(li);
            });
        }

        /**
         * 활성 항목을 설정한다 (하이라이트).
         * @param {string} meetingId - 활성화할 회의 ID (null이면 해제)
         */
        function setActive(meetingId) {
            _activeId = meetingId;
            // 기존 active 클래스 제거 후 새로 부여
            var items = _listEl.querySelectorAll(".meeting-list-item");
            items.forEach(function (item) {
                var titleEl = item.querySelector(".meeting-list-title");
                if (titleEl && titleEl.textContent === meetingId) {
                    item.classList.add("active");
                } else {
                    item.classList.remove("active");
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
         * 검색어로 회의 목록을 필터링한다.
         * @param {string} query - 검색어
         */
        function filter(query) {
            if (!query) {
                render(_meetings);
                return;
            }
            var lower = query.toLowerCase();
            var filtered = _meetings.filter(function (m) {
                return (m.meeting_id || "").toLowerCase().indexOf(lower) >= 0;
            });
            render(filtered);
        }

        /**
         * 현재 회의 목록 데이터를 반환한다.
         * @returns {Array}
         */
        function getMeetings() {
            return _meetings;
        }

        /**
         * 사이드바 타이머를 정리한다.
         */
        function destroy() {
            // 사이드바 타이머 정리
            if (_statusTimer) { clearInterval(_statusTimer); _statusTimer = null; }
            if (_meetingsTimer) { clearInterval(_meetingsTimer); _meetingsTimer = null; }
            if (_searchTimeout) { clearTimeout(_searchTimeout); _searchTimeout = null; }
        }

        return {
            init: init,
            loadMeetings: loadMeetings,
            setActive: setActive,
            setActiveFromPath: setActiveFromPath,
            filter: filter,
            getMeetings: getMeetings,
            destroy: destroy,
        };
    })();


    // =================================================================
    // === HomeView (기존 index.html 로직 이식) ===
    // =================================================================

    /**
     * 홈 뷰: 검색 폼, 검색 결과, 회의 목록 카드, 파이프라인 진행 표시.
     * @constructor
     */
    function HomeView() {
        var self = this;
        self._timers = [];          // 정리해야 할 타이머 목록
        self._listeners = [];       // 정리해야 할 이벤트 리스너 목록
        self._allMeetings = [];     // 전체 회의 데이터 (정렬용)
        self._els = {};             // DOM 참조

        self._render();
        self._bind();
        self._loadData();
    }

    /**
     * 홈 뷰 DOM을 생성한다.
     */
    HomeView.prototype._render = function () {
        var contentEl = Router.getContentEl();
        contentEl.innerHTML = "";

        var html = [
            // 검색 섹션
            '<section class="search-section">',
            '  <h2 class="search-title">회의 내용 검색</h2>',
            '  <form class="search-form" id="homeSearchForm">',
            '    <div class="search-input-row">',
            '      <div class="search-input-wrapper">',
            '        <span class="search-icon">&#x1F50D;</span>',
            '        <input type="text" class="search-input" id="homeSearchQuery"',
            '               placeholder="검색어를 입력하세요 (예: 프로젝트 일정, 결정사항...)"',
            '               autocomplete="off" />',
            '      </div>',
            '      <button type="submit" class="search-btn" id="homeSearchBtn">검색</button>',
            '    </div>',
            '    <div class="filter-row">',
            '      <div class="filter-group">',
            '        <span class="filter-label">날짜</span>',
            '        <input type="date" class="filter-input" id="homeFilterDate" />',
            '      </div>',
            '      <div class="filter-group">',
            '        <span class="filter-label">화자</span>',
            '        <input type="text" class="filter-input" id="homeFilterSpeaker" placeholder="예: SPEAKER_00" />',
            '      </div>',
            '      <button type="button" class="filter-clear-btn" id="homeFilterClearBtn" aria-label="검색 필터 초기화">',
            '        필터 초기화',
            '      </button>',
            '    </div>',
            '  </form>',
            '</section>',

            // 검색 결과
            '<section class="search-results" id="homeSearchResults">',
            '  <div class="search-results-header">',
            '    <div>',
            '      <h2 class="section-title">검색 결과</h2>',
            '      <span class="search-stats" id="homeSearchStats"></span>',
            '    </div>',
            '    <button class="search-close-btn" id="homeSearchCloseBtn" aria-label="검색 결과 닫기">결과 닫기</button>',
            '  </div>',
            '  <div id="homeSearchResultsList"></div>',
            '  <div class="loading-overlay" id="homeSearchLoading" role="status" aria-live="polite">',
            '    <span class="loading-spinner" aria-hidden="true"></span>',
            '    <span>검색 중...</span>',
            '  </div>',
            '  <div class="empty-state" id="homeSearchEmpty" style="display:none;">',
            '    <div class="empty-state-icon">&#x1F50D;</div>',
            '    <div class="empty-state-text" id="homeSearchEmptyText">검색 결과가 없습니다</div>',
            '    <div class="empty-state-sub" id="homeSearchEmptySub">다른 검색어나 필터를 시도해보세요</div>',
            '  </div>',
            '</section>',

            // 회의 목록
            '<section>',
            '  <div class="section-header">',
            '    <h2 class="section-title">회의 목록</h2>',
            '    <div class="sort-controls">',
            '      <select class="sort-select" id="homeSortSelect" aria-label="회의 목록 정렬">',
            '        <option value="newest">최신순</option>',
            '        <option value="oldest">오래된순</option>',
            '        <option value="status">상태별</option>',
            '        <option value="name">이름순</option>',
            '      </select>',
            '      <span class="section-count" id="homeMeetingsCount"></span>',
            '    </div>',
            '  </div>',
            '  <div class="meetings-grid" id="homeMeetingsGrid"></div>',
            '  <div class="loading-overlay" id="homeMeetingsLoading" role="status" aria-live="polite">',
            '    <span class="loading-spinner" aria-hidden="true"></span>',
            '    <span>회의 목록 불러오는 중...</span>',
            '  </div>',
            '  <div class="empty-state" id="homeMeetingsEmpty" style="display:none;">',
            '    <div class="empty-state-icon">&#x1F4CB;</div>',
            '    <div class="empty-state-text">아직 등록된 회의가 없습니다</div>',
            '    <div class="empty-state-sub">',
            '      Zoom 회의를 녹음하거나, 오디오 파일(.m4a, .wav)을<br>',
            '      감시 폴더에 추가하면 자동으로 전사가 시작됩니다.',
            '    </div>',
            '  </div>',
            '</section>',
        ].join("\n");

        contentEl.innerHTML = html;

        // DOM 참조 캐싱
        this._els = {
            searchForm: document.getElementById("homeSearchForm"),
            searchQuery: document.getElementById("homeSearchQuery"),
            searchBtn: document.getElementById("homeSearchBtn"),
            filterDate: document.getElementById("homeFilterDate"),
            filterSpeaker: document.getElementById("homeFilterSpeaker"),
            filterClearBtn: document.getElementById("homeFilterClearBtn"),
            searchResults: document.getElementById("homeSearchResults"),
            searchResultsList: document.getElementById("homeSearchResultsList"),
            searchStats: document.getElementById("homeSearchStats"),
            searchLoading: document.getElementById("homeSearchLoading"),
            searchEmpty: document.getElementById("homeSearchEmpty"),
            searchCloseBtn: document.getElementById("homeSearchCloseBtn"),
            meetingsGrid: document.getElementById("homeMeetingsGrid"),
            meetingsCount: document.getElementById("homeMeetingsCount"),
            meetingsLoading: document.getElementById("homeMeetingsLoading"),
            meetingsEmpty: document.getElementById("homeMeetingsEmpty"),
            sortSelect: document.getElementById("homeSortSelect"),
        };
    };

    /**
     * 이벤트 리스너를 바인딩한다.
     */
    HomeView.prototype._bind = function () {
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

        // 검색 결과 닫기
        var onSearchClose = function () {
            els.searchResults.classList.remove("visible");
            els.searchResultsList.innerHTML = "";
        };
        els.searchCloseBtn.addEventListener("click", onSearchClose);
        self._listeners.push({ el: els.searchCloseBtn, type: "click", fn: onSearchClose });

        // 정렬 드롭다운 변경
        var onSortChange = function () {
            if (self._allMeetings.length > 0) {
                self._renderMeetings(self._sortMeetings(self._allMeetings));
            }
        };
        els.sortSelect.addEventListener("change", onSortChange);
        self._listeners.push({ el: els.sortSelect, type: "change", fn: onSortChange });

        // WebSocket 이벤트: 파이프라인 상태
        var onPipelineStatus = function (e) {
            var detail = e.detail || {};
            var meetingId = detail.meeting_id;
            var step = detail.step || detail.current_step || detail.status || "";
            if (meetingId && step) {
                self._updatePipelineProgress(meetingId, step);
            }
        };
        document.addEventListener("ws:pipeline_status", onPipelineStatus);
        self._listeners.push({ el: document, type: "ws:pipeline_status", fn: onPipelineStatus });

        // WebSocket 이벤트: 작업 완료
        var onJobCompleted = function (e) {
            self._fetchMeetings();

            // 해당 회의의 진행 표시를 완료 상태로 전환
            var detail = e.detail || {};
            var meetingId = detail.meeting_id;
            if (meetingId) {
                var progressEl = document.querySelector(
                    '.pipeline-progress[data-meeting-id="' + meetingId + '"]'
                );
                if (progressEl) {
                    var dots = progressEl.querySelectorAll(".step-dot");
                    dots.forEach(function (dot) {
                        dot.classList.remove("active");
                        dot.classList.add("completed");
                    });
                    var fillEl = progressEl.querySelector(".progress-fill");
                    if (fillEl) fillEl.style.width = "100%";
                    setTimeout(function () {
                        progressEl.style.display = "none";
                    }, 2000);
                }
            }
        };
        document.addEventListener("ws:job_completed", onJobCompleted);
        self._listeners.push({ el: document, type: "ws:job_completed", fn: onJobCompleted });

        // WebSocket 이벤트: 작업 추가
        var onJobAdded = function () {
            self._fetchMeetings();
        };
        document.addEventListener("ws:job_added", onJobAdded);
        self._listeners.push({ el: document, type: "ws:job_added", fn: onJobAdded });

        // WebSocket 이벤트: 작업 실패
        var onJobFailed = function (e) {
            var detail = e.detail || {};
            var meetingId = detail.meeting_id;
            var errMsg = detail.error || detail.error_message || "알 수 없는 오류";
            errorBanner.show(
                "작업 실패" +
                (meetingId ? " (" + meetingId + ")" : "") +
                ": " + errMsg
            );

            if (meetingId) {
                var progressEl = document.querySelector(
                    '.pipeline-progress[data-meeting-id="' + meetingId + '"]'
                );
                if (progressEl) {
                    var activeDot = progressEl.querySelector(".step-dot.active");
                    if (activeDot) {
                        activeDot.style.backgroundColor = "var(--error)";
                    }
                }
            }
            self._fetchMeetings();
        };
        document.addEventListener("ws:job_failed", onJobFailed);
        self._listeners.push({ el: document, type: "ws:job_failed", fn: onJobFailed });
    };

    /**
     * 초기 데이터를 로드한다.
     */
    HomeView.prototype._loadData = function () {
        this._fetchMeetings();
    };

    /**
     * 회의 목록을 정렬한다.
     * @param {Array} meetings - 회의 목록
     * @returns {Array} 정렬된 배열
     */
    HomeView.prototype._sortMeetings = function (meetings) {
        var sortBy = this._els.sortSelect.value;
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
        } else if (sortBy === "name") {
            sorted.sort(function (a, b) {
                return (a.meeting_id || "").localeCompare(b.meeting_id || "");
            });
        }

        return sorted;
    };

    /**
     * 회의 목록을 API에서 가져와 렌더링한다.
     */
    HomeView.prototype._fetchMeetings = async function () {
        var self = this;
        var els = self._els;

        // 스켈레톤 로딩
        if (self._allMeetings.length === 0) {
            els.meetingsGrid.innerHTML = "";
            els.meetingsGrid.appendChild(App.createSkeletonCards(3));
        }
        els.meetingsLoading.classList.add("visible");
        els.meetingsEmpty.style.display = "none";

        try {
            var data = await App.apiRequest("/meetings");
            self._allMeetings = data.meetings || [];
            self._renderMeetings(self._sortMeetings(self._allMeetings));
            App.safeText(els.meetingsCount, "총 " + (data.total || 0) + "건");
        } catch (e) {
            errorBanner.show("회의 목록을 불러올 수 없습니다: " + e.message);
        } finally {
            els.meetingsLoading.classList.remove("visible");
        }
    };

    /**
     * 회의 목록을 카드 형태로 렌더링한다.
     * @param {Array} meetings - 회의 아이템 배열
     */
    HomeView.prototype._renderMeetings = function (meetings) {
        var self = this;
        var els = self._els;
        els.meetingsGrid.innerHTML = "";

        if (meetings.length === 0) {
            els.meetingsEmpty.style.display = "block";
            return;
        }

        els.meetingsEmpty.style.display = "none";

        meetings.forEach(function (meeting) {
            var card = document.createElement("div");
            card.className = "meeting-card";
            card.setAttribute("role", "button");
            card.setAttribute("tabindex", "0");
            card.setAttribute("aria-label",
                meeting.meeting_id + " 회의 — " +
                App.getStatusLabel(meeting.status));

            // 카드 헤더: ID + 상태 배지
            var header = document.createElement("div");
            header.className = "meeting-card-header";

            var idEl = document.createElement("span");
            idEl.className = "meeting-id";
            idEl.textContent = meeting.meeting_id;

            var statusEl = document.createElement("span");
            statusEl.className = "meeting-status " + meeting.status;
            statusEl.textContent = App.getStatusLabel(meeting.status);

            header.appendChild(idEl);
            header.appendChild(statusEl);

            // 메타 정보
            var meta = document.createElement("div");
            meta.className = "meeting-meta";

            // 파일명
            var fileItem = document.createElement("div");
            fileItem.className = "meeting-meta-item";
            var fileIcon = document.createElement("span");
            fileIcon.className = "meeting-meta-icon";
            fileIcon.textContent = "\uD83C\uDFA4";
            var fileText = document.createElement("span");
            fileText.textContent = App.getFileName(meeting.audio_path);
            fileItem.appendChild(fileIcon);
            fileItem.appendChild(fileText);
            meta.appendChild(fileItem);

            // 생성일
            if (meeting.created_at) {
                var dateItem = document.createElement("div");
                dateItem.className = "meeting-meta-item";
                var dateIcon = document.createElement("span");
                dateIcon.className = "meeting-meta-icon";
                dateIcon.textContent = "\uD83D\uDCC5";
                var dateText = document.createElement("span");
                dateText.textContent = App.formatDate(meeting.created_at);
                dateItem.appendChild(dateIcon);
                dateItem.appendChild(dateText);
                meta.appendChild(dateItem);
            }

            // 재시도 횟수
            if (meeting.retry_count > 0) {
                var retryItem = document.createElement("div");
                retryItem.className = "meeting-meta-item";
                var retryIcon = document.createElement("span");
                retryIcon.className = "meeting-meta-icon";
                retryIcon.textContent = "\u21BB";
                var retryText = document.createElement("span");
                retryText.textContent = "재시도 " + meeting.retry_count + "회";
                retryItem.appendChild(retryIcon);
                retryItem.appendChild(retryText);
                meta.appendChild(retryItem);
            }

            card.appendChild(header);
            card.appendChild(meta);

            // 에러 메시지
            if (meeting.error_message) {
                var errorEl = document.createElement("div");
                errorEl.className = "meeting-error";
                errorEl.textContent = meeting.error_message;
                card.appendChild(errorEl);
            }

            // 파이프라인 진행 표시
            var isProcessing = (
                meeting.status !== "completed" &&
                meeting.status !== "failed" &&
                meeting.status !== "queued"
            );

            var progressEl = document.createElement("div");
            progressEl.className = "pipeline-progress";
            progressEl.setAttribute("data-meeting-id", meeting.meeting_id);

            var stepsEl = document.createElement("div");
            stepsEl.className = "progress-steps";

            PIPELINE_STEPS.forEach(function (stepDef) {
                var dot = document.createElement("span");
                dot.className = "step-dot";
                dot.setAttribute("data-step", stepDef.key);
                dot.textContent = stepDef.label;
                stepsEl.appendChild(dot);
            });

            var barEl = document.createElement("div");
            barEl.className = "progress-bar";
            var fillEl = document.createElement("div");
            fillEl.className = "progress-fill";
            barEl.appendChild(fillEl);

            progressEl.appendChild(stepsEl);
            progressEl.appendChild(barEl);

            if (!isProcessing) {
                progressEl.style.display = "none";
            }

            card.appendChild(progressEl);

            // 액션 버튼 (실패 시 재시도, 완료/실패 시 삭제)
            if (meeting.status === "failed" || meeting.status === "completed") {
                var actionsEl = document.createElement("div");
                actionsEl.className = "meeting-card-actions";

                if (meeting.status === "failed") {
                    var retryBtn = document.createElement("button");
                    retryBtn.className = "meeting-card-action retry";
                    retryBtn.textContent = "\u21BB 재시도";
                    retryBtn.setAttribute("aria-label", meeting.meeting_id + " 재시도");
                    retryBtn.addEventListener("click", function (e) {
                        e.stopPropagation();
                        self._retryMeeting(meeting.meeting_id);
                    });
                    actionsEl.appendChild(retryBtn);
                }

                var deleteBtn = document.createElement("button");
                deleteBtn.className = "meeting-card-action delete";
                deleteBtn.textContent = "\u2715 삭제";
                deleteBtn.setAttribute("aria-label", meeting.meeting_id + " 삭제");
                deleteBtn.addEventListener("click", function (e) {
                    e.stopPropagation();
                    self._deleteMeeting(meeting.meeting_id);
                });
                actionsEl.appendChild(deleteBtn);

                card.appendChild(actionsEl);
            }

            // 클릭 → ViewerView로 이동 (SPA 내비게이션)
            card.addEventListener("click", function () {
                Router.navigate("/app/viewer/" + encodeURIComponent(meeting.meeting_id));
            });

            // 키보드 접근성 (Enter/Space)
            card.addEventListener("keydown", function (e) {
                if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    card.click();
                }
            });

            els.meetingsGrid.appendChild(card);
        });
    };

    /**
     * 검색을 수행한다.
     */
    HomeView.prototype._performSearch = async function () {
        var self = this;
        var els = self._els;
        var query = els.searchQuery.value.trim();
        if (!query) return;

        els.searchResults.classList.add("visible");
        els.searchLoading.classList.add("visible");
        els.searchResultsList.innerHTML = "";
        els.searchEmpty.style.display = "none";
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
        } finally {
            els.searchLoading.classList.remove("visible");
            els.searchBtn.disabled = false;
        }
    };

    /**
     * 검색 결과를 렌더링한다.
     * @param {Object} data - SearchResponse
     */
    HomeView.prototype._renderSearchResults = function (data) {
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
            var emptyText = document.getElementById("homeSearchEmptyText");
            var emptySub = document.getElementById("homeSearchEmptySub");
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
            dateItem.textContent = "\uD83D\uDCC5 " + (item.date || "-");
            meta.appendChild(dateItem);

            if (item.speakers && item.speakers.length > 0) {
                var speakerItem = document.createElement("span");
                speakerItem.className = "result-meta-item";
                speakerItem.textContent = "\uD83D\uDC64 " + item.speakers.join(", ");
                meta.appendChild(speakerItem);
            }

            var timeItem = document.createElement("span");
            timeItem.className = "result-meta-item";
            timeItem.textContent = "\u23F1 " + App.formatTime(item.start_time) +
                " ~ " + App.formatTime(item.end_time);
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

            // 클릭 → ViewerView로 이동 (SPA 내비게이션)
            el.addEventListener("click", function () {
                var viewerPath = "/app/viewer/" + encodeURIComponent(item.meeting_id);
                // 검색어와 타임스탬프를 쿼리 파라미터로 전달
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
     * 파이프라인 진행 표시를 업데이트한다.
     * @param {string} meetingId - 회의 ID
     * @param {string} currentStep - 현재 단계
     */
    HomeView.prototype._updatePipelineProgress = function (meetingId, currentStep) {
        var progressEl = document.querySelector(
            '.pipeline-progress[data-meeting-id="' + meetingId + '"]'
        );
        if (!progressEl) return;

        progressEl.style.display = "block";

        var currentIdx = -1;
        PIPELINE_STEPS.forEach(function (stepDef, idx) {
            if (stepDef.key === currentStep) {
                currentIdx = idx;
            }
        });

        var dots = progressEl.querySelectorAll(".step-dot");
        dots.forEach(function (dot, idx) {
            dot.classList.remove("active", "completed");
            if (idx < currentIdx) {
                dot.classList.add("completed");
            } else if (idx === currentIdx) {
                dot.classList.add("active");
            }
        });

        var fillEl = progressEl.querySelector(".progress-fill");
        if (fillEl && currentIdx >= 0) {
            var pct = Math.round(((currentIdx + 1) / PIPELINE_STEPS.length) * 100);
            fillEl.style.width = pct + "%";
        }
    };

    /**
     * 실패한 회의를 재시도한다.
     * @param {string} meetingId - 재시도할 회의 ID
     */
    HomeView.prototype._retryMeeting = async function (meetingId) {
        try {
            await App.apiPost("/meetings/" + encodeURIComponent(meetingId) + "/retry", {});
            this._fetchMeetings();
        } catch (e) {
            errorBanner.show("재시도 실패: " + e.message);
        }
    };

    /**
     * 회의를 삭제한다.
     * @param {string} meetingId - 삭제할 회의 ID
     */
    HomeView.prototype._deleteMeeting = async function (meetingId) {
        if (!confirm("'" + meetingId + "' 회의를 삭제하시겠습니까?\n삭제된 데이터는 복구할 수 없습니다.")) {
            return;
        }
        try {
            await App.apiDelete("/meetings/" + encodeURIComponent(meetingId));
            this._fetchMeetings();
        } catch (e) {
            errorBanner.show("삭제 실패: " + e.message);
        }
    };

    /**
     * 뷰를 정리한다. (이벤트 리스너, 타이머 해제)
     */
    HomeView.prototype.destroy = function () {
        // 이벤트 리스너 해제
        this._listeners.forEach(function (entry) {
            entry.el.removeEventListener(entry.type, entry.fn);
        });
        this._listeners = [];

        // 타이머 해제
        this._timers.forEach(function (t) { clearInterval(t); clearTimeout(t); });
        this._timers = [];
    };


    // =================================================================
    // === ViewerView (기존 viewer.html 로직 이식) ===
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
            // 뒤로가기 링크
            '<a href="#" class="back-link" id="viewerBackLink">',
            '  <span>&#x2190;</span> 회의 목록으로 돌아가기',
            '</a>',

            // 회의 정보
            '<div class="meeting-info" id="viewerMeetingInfo" style="display:none;">',
            '  <div class="meeting-info-header">',
            '    <span class="meeting-info-title" id="viewerMeetingTitle"></span>',
            '    <span class="meeting-info-status" id="viewerMeetingStatus"></span>',
            '  </div>',
            '  <div class="meeting-meta-row">',
            '    <span class="meeting-meta-item" id="viewerMetaFile"></span>',
            '    <span class="meeting-meta-item" id="viewerMetaDate"></span>',
            '    <span class="meeting-meta-item" id="viewerMetaSpeakers"></span>',
            '    <span class="meeting-meta-item" id="viewerMetaUtterances"></span>',
            '  </div>',
            '  <div class="speaker-legend" id="viewerSpeakerLegend"></div>',
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
            '    <div class="empty-state-icon">&#x1F4DD;</div>',
            '    <div class="empty-state-text">전사문이 아직 생성되지 않았습니다</div>',
            '    <div class="empty-state-sub">',
            '      파이프라인이 처리 중이라면 잠시 기다려 주세요.<br>',
            '      완료되면 전사문이 자동으로 표시됩니다.',
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
            '    <div class="empty-state-icon">&#x1F4CB;</div>',
            '    <div class="empty-state-text">회의록이 아직 생성되지 않았습니다</div>',
            '    <div class="empty-state-sub">',
            '      AI 요약 단계까지 파이프라인이 진행되면<br>',
            '      자동으로 회의록이 생성됩니다.',
            '    </div>',
            '  </div>',
            '</div>',
        ].join("\n");

        contentEl.innerHTML = html;

        // DOM 참조 캐싱
        this._els = {
            backLink: document.getElementById("viewerBackLink"),
            meetingInfo: document.getElementById("viewerMeetingInfo"),
            meetingTitle: document.getElementById("viewerMeetingTitle"),
            meetingStatus: document.getElementById("viewerMeetingStatus"),
            metaFile: document.getElementById("viewerMetaFile"),
            metaDate: document.getElementById("viewerMetaDate"),
            metaSpeakers: document.getElementById("viewerMetaSpeakers"),
            metaUtterances: document.getElementById("viewerMetaUtterances"),
            speakerLegend: document.getElementById("viewerSpeakerLegend"),
            tabNav: document.getElementById("viewerTabNav"),
            timeline: document.getElementById("viewerTimeline"),
            transcriptLoading: document.getElementById("viewerTranscriptLoading"),
            transcriptEmpty: document.getElementById("viewerTranscriptEmpty"),
            summaryContent: document.getElementById("viewerSummaryContent"),
            summaryLoading: document.getElementById("viewerSummaryLoading"),
            summaryEmpty: document.getElementById("viewerSummaryEmpty"),
            searchBar: document.getElementById("viewerSearchBar"),
            searchInput: document.getElementById("viewerSearchInput"),
            searchInfo: document.getElementById("viewerSearchInfo"),
            searchClear: document.getElementById("viewerSearchClear"),
            searchPrev: document.getElementById("viewerSearchPrev"),
            searchNext: document.getElementById("viewerSearchNext"),
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

        // 뒤로가기 링크 (SPA 내비게이션)
        var onBack = function (e) {
            e.preventDefault();
            Router.navigate("/app");
        };
        els.backLink.addEventListener("click", onBack);
        self._listeners.push({ el: els.backLink, type: "click", fn: onBack });

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

        var onPipelineStatus = function () {
            self._loadMeetingInfo();
        };
        document.addEventListener("ws:pipeline_status", onPipelineStatus);
        self._listeners.push({ el: document, type: "ws:pipeline_status", fn: onPipelineStatus });
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

        speakers.forEach(function (speaker) {
            var chip = document.createElement("span");
            chip.className = "speaker-chip";

            var dot = document.createElement("span");
            dot.className = "speaker-dot";
            dot.style.backgroundColor = self._getSpeakerColor(speaker);

            var label = document.createElement("span");
            label.textContent = speaker;

            chip.appendChild(dot);
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

        utterances.forEach(function (u) {
            var el = document.createElement("div");
            el.className = "utterance";
            var color = self._getSpeakerColor(u.speaker);
            el.style.borderLeftColor = color;

            // 시간
            var timeEl = document.createElement("span");
            timeEl.className = "utterance-time";
            timeEl.textContent = App.formatTime(u.start);

            // 화자
            var speakerEl = document.createElement("span");
            speakerEl.className = "utterance-speaker";
            speakerEl.textContent = u.speaker;
            speakerEl.style.color = color;

            // 텍스트 (검색어 하이라이팅 적용)
            var textEl = document.createElement("span");
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

            el.appendChild(timeEl);
            el.appendChild(speakerEl);
            el.appendChild(textEl);
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

            els.meetingStatus.className = "meeting-info-status " + data.status;
            App.safeText(els.meetingStatus, App.getStatusLabel(data.status));

            App.safeText(els.metaFile, "\uD83C\uDFA4 " + App.getFileName(data.audio_path));
            App.safeText(els.metaDate, "\uD83D\uDCC5 " + App.formatDate(data.created_at));
        } catch (e) {
            if (e.status === 404) {
                errorBanner.show("회의를 찾을 수 없습니다: " + self._meetingId);
            } else {
                errorBanner.show("회의 정보 로드 실패: " + e.message);
            }
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
                return;
            }

            // 화자 색상 맵 + 범례
            self._buildSpeakerColorMap(data.speakers || []);
            self._renderSpeakerLegend(data.speakers || []);

            // 메타 정보 업데이트
            App.safeText(els.metaSpeakers,
                "\uD83D\uDC64 화자 " + (data.num_speakers || 0) + "명");
            App.safeText(els.metaUtterances,
                "\uD83D\uDCDD 발화 " + (data.total_utterances || 0) + "건");

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
            } else {
                errorBanner.show("전사문 로드 실패: " + e.message);
            }
        } finally {
            els.transcriptLoading.classList.remove("visible");
        }
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
                return;
            }

            // 마크다운 렌더링
            els.summaryContent.innerHTML = App.renderMarkdown(data.markdown);

            // 탭 표시
            els.tabNav.style.display = "flex";

        } catch (e) {
            if (e.status === 404) {
                els.summaryEmpty.style.display = "block";
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
        document.title = "회의 전사 시스템";
    };


    // =================================================================
    // === ChatView (기존 chat.html 로직 이식) ===
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
            '<div class="welcome-icon">&#x1F4AC;</div>' +
            '<div class="welcome-title">AI 회의 어시스턴트</div>' +
            '<div class="welcome-desc">' +
                '회의 내용에 대해 자유롭게 질문하세요.<br>' +
                '관련 회의 내용을 검색하여 AI가 답변합니다.' +
            '</div>' +
            '<div class="welcome-tips">' +
                '<div class="welcome-tip">&#x1F4A1; "지난 회의에서 결정된 일정이 뭐야?"</div>' +
                '<div class="welcome-tip">&#x1F4A1; "프로젝트 진행 상황을 요약해줘"</div>' +
                '<div class="welcome-tip">&#x1F4A1; "다음 마일스톤까지 해야 할 일은?"</div>' +
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
            '    <select class="controls-select" id="chatMeetingFilter">',
            '      <option value="">전체 회의</option>',
            '    </select>',
            '    <div class="controls-right">',
            '      <button class="btn-small" id="chatBtnClearChat">대화 초기화</button>',
            '    </div>',
            '  </div>',

            // 메시지 영역 (환영 메시지는 _createWelcomeHtml() 공용 메서드 사용)
            '  <div class="messages-area" id="chatMessagesArea">',
            self._createWelcomeHtml(),
            '  </div>',

            // 타이핑 인디케이터
            '  <div class="typing-indicator" id="chatTypingIndicator">',
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
        document.title = "AI Chat — 회의 전사 시스템";
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
                    completed: "\u2705",
                    recording: "\uD83D\uDD34",
                    transcribing: "\u2699\uFE0F",
                    diarizing: "\u2699\uFE0F",
                    merging: "\u2699\uFE0F",
                    embedding: "\u2699\uFE0F",
                    queued: "\u23F3",
                    failed: "\u274C",
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
        avatar.textContent = "\uD83D\uDC64";

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
            refsTitle.textContent = "\uD83D\uDCCE 참조 출처 (" + data.references.length + "건)";
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
        copyBtn.textContent = "\uD83D\uDCCB 복사";
        copyBtn.setAttribute("aria-label", "답변 복사");
        copyBtn.addEventListener("click", function () {
            var textToCopy = data.answer || bubble.textContent;
            App.copyToClipboard(textToCopy).then(function (ok) {
                if (ok) {
                    copyBtn.textContent = "\u2713 복사됨";
                    copyBtn.classList.add("copied");
                    setTimeout(function () {
                        copyBtn.textContent = "\uD83D\uDCCB 복사";
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

        // 환영 메시지 복원 (_createWelcomeHtml() 공용 메서드 사용)
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

        // 페이지 타이틀 복원
        document.title = "회의 전사 시스템";
    };


    // =================================================================
    // === 공개 API ===
    // =================================================================

    window.SPA = {
        Router: Router,
        Sidebar: Sidebar,
        HomeView: HomeView,
        ViewerView: ViewerView,
        ChatView: ChatView,
    };


    // =================================================================
    // === 초기화 ===
    // =================================================================

    // WebSocket 연결
    App.connectWebSocket();

    // 사이드바 초기화
    Sidebar.init();

    // 라우터 초기화 (현재 경로에 맞는 뷰 렌더링)
    Router.init();

})();
