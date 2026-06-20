/* =================================================================
 * Recap SearchView boundary
 *
 * 목적: 검색 화면을 SPA 라우터 본문에서 분리한다.
 * 공개 API: window.MeetingSearchView
 * ================================================================= */
(function () {
    "use strict";

    function create(deps) {
        deps = deps || {};
        var App = deps.App || window.MeetingApp;
        var Router = deps.Router || (window.SPA && window.SPA.Router);
        var Icons = deps.Icons || {};
        var errorBanner = deps.errorBanner || { show: function () {} };

        if (!App || !Router) {
            throw new Error("MeetingSearchView requires App and Router");
        }

        function _displaySpeakerLabel(label) {
            var raw = String(label || "");
            var match = raw.match(/^SPEAKER_(\d+)$/i);
            if (!match) return raw;
            return "화자 " + (Number(match[1]) + 1);
        }

        function _normalizeIndexStatus(status) {
            if (!status || typeof status !== "object") return null;
            var total = Number(status.total);
            var indexed = Number(status.indexed);
            if (!isFinite(total) || !isFinite(indexed)) return null;
            var missing = Number(status.missing);
            return {
                total: total,
                indexed: indexed,
                missing: isFinite(missing) ? missing : 0,
                missingMeetingIds: Array.isArray(status.missing_meeting_ids)
                    ? status.missing_meeting_ids.slice()
                    : [],
            };
        }

        function _defaultSearchHintHtml() {
            return [
                '<p>회의 전사문, 요약, 결정사항에서 의미가 비슷한 표현과 정확한 키워드를 함께 찾습니다.</p>',
                '<div class="search-suggestion-row" aria-label="추천 검색어">',
                '  <button type="button" class="suggestion-chip" data-query="결정사항">결정사항</button>',
                '  <button type="button" class="suggestion-chip" data-query="다음 액션">다음 액션</button>',
                '  <button type="button" class="suggestion-chip" data-query="일정">일정</button>',
                '</div>',
            ].join("");
        }

        function _emptyMeetingsHintHtml() {
            return [
                '<p>검색할 회의가 아직 없습니다. 녹음하거나 오디오를 가져오면 전사문과 결정사항을 찾을 수 있습니다.</p>',
                '<div class="search-suggestion-row" aria-label="첫 회의 작업">',
                '  <button type="button" class="suggestion-chip" data-search-empty-action="record">녹음 시작</button>',
                '  <button type="button" class="suggestion-chip" data-search-empty-action="import">오디오 가져오기</button>',
                '</div>',
            ].join("");
        }

        function _notReadyHintHtml(state) {
            if (state === "no_completed") {
                return [
                    '<p>검색하려면 전사가 완료된 회의가 필요합니다. 진행 중인 전사가 끝나면 검색할 수 있습니다.</p>',
                    '<div class="search-suggestion-row" aria-label="검색 준비 작업">',
                    '  <button type="button" class="suggestion-chip" data-search-empty-action="go-home">홈으로 이동</button>',
                    '</div>',
                ].join("");
            }
            return [
                '<p>회의는 있지만 검색 인덱스가 아직 준비되지 않았습니다. 누락분을 복구한 뒤 다시 검색해 주세요.</p>',
                '<div class="search-suggestion-row" aria-label="검색 준비 작업">',
                '  <button type="button" class="suggestion-chip" data-search-empty-action="go-reindex">검색 인덱스 복구</button>',
                '</div>',
            ].join("");
        }

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
        self._destroyed = false;
        self._searchSeq = 0;
        self._hasMeetings = null;
        self._indexStatus = null;

        self._render();
        self._bind();
        self._loadMeetingState();
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
            '    <div class="filter-row" aria-label="검색 필터">',
            '      <div class="filter-group">',
            '        <label class="filter-label" for="searchFilterDate">날짜</label>',
            '        <input type="date" class="filter-input" id="searchFilterDate" aria-label="검색 날짜 필터" />',
            '      </div>',
            '      <div class="filter-group">',
            '        <label class="filter-label" for="searchFilterSpeaker">화자</label>',
            '        <input type="text" class="filter-input" id="searchFilterSpeaker" placeholder="예: 화자 1 또는 참석자 이름" aria-label="검색 화자 필터" />',
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
            _defaultSearchHintHtml(),
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
            if (els.searchQuery.value.trim() && !els.searchQuery.disabled) {
                self._performSearch();
            } else {
                els.searchResults.classList.remove("visible");
                els.searchEmpty.style.display = "none";
                els.searchHint.style.display = "block";
            }
        };
        els.filterClearBtn.addEventListener("click", onFilterClear);
        self._listeners.push({ el: els.filterClearBtn, type: "click", fn: onFilterClear });

        var onSuggestionClick = function (e) {
            var emptyAction = e.target.closest("[data-search-empty-action]");
            if (emptyAction) {
                var action = emptyAction.getAttribute("data-search-empty-action");
                if (action === "import") {
                    var modal = document.getElementById("importModal");
                    if (modal) {
                        modal.classList.remove("hidden");
                        var dz = document.getElementById("importDropzone");
                        if (dz) dz.focus();
                    }
                } else if (action === "record") {
                    App.apiRequest("/recording/start", { method: "POST" }).catch(function (err) {
                        errorBanner.show("녹음 시작 실패: " + (err.message || "오디오 입력 상태를 확인해 주세요."));
                    });
                } else if (action === "go-reindex") {
                    Router.navigate("/app/settings/reindex");
                } else if (action === "go-home") {
                    Router.navigate("/app");
                }
                return;
            }
            var chip = e.target.closest(".suggestion-chip");
            if (!chip) return;
            els.searchQuery.value = chip.getAttribute("data-query") || chip.textContent.trim();
            self._performSearch();
        };
        els.searchHint.addEventListener("click", onSuggestionClick);
        self._listeners.push({ el: els.searchHint, type: "click", fn: onSuggestionClick });

        // 입력 필드에 포커스
        els.searchQuery.focus();
    };

    SearchView.prototype._loadMeetingState = function () {
        var self = this;
        App.apiRequest("/meetings")
            .then(function (data) {
                if (self._destroyed) return;
                var meetings = data && data.meetings ? data.meetings : [];
                self._hasMeetings = meetings.length > 0;
                if (!self._hasMeetings) {
                    self._indexStatus = { total: 0, indexed: 0, missing: 0, missingMeetingIds: [] };
                    self._syncSearchAvailability();
                    return;
                }
                App.apiRequest("/reindex/status")
                    .then(function (status) {
                        if (self._destroyed) return;
                        self._indexStatus = _normalizeIndexStatus(status);
                        self._syncSearchAvailability();
                    })
                    .catch(function () {
                        if (self._destroyed) return;
                        self._indexStatus = null;
                        self._syncSearchAvailability();
                    });
            })
            .catch(function () {
                if (self._destroyed) return;
                self._hasMeetings = null;
                self._indexStatus = null;
                self._syncSearchAvailability();
            });
    };

    SearchView.prototype._getReadinessState = function () {
        if (this._hasMeetings === false) return "no_meetings";
        if (this._hasMeetings !== true) return "unknown";
        if (!this._indexStatus) return "ready";
        if (this._indexStatus.total === 0) return "no_completed";
        if (this._indexStatus.indexed === 0) return "no_index";
        return "ready";
    };

    SearchView.prototype._syncSearchAvailability = function () {
        var els = this._els;
        var state = this._getReadinessState();
        var disabled = state === "no_meetings" || state === "no_completed" || state === "no_index";
        els.searchQuery.disabled = disabled;
        els.searchBtn.disabled = disabled;
        els.filterDate.disabled = disabled;
        els.filterSpeaker.disabled = disabled;
        els.filterClearBtn.disabled = disabled;

        if (state === "no_meetings") {
            els.searchQuery.placeholder = "회의를 추가하면 검색할 수 있습니다.";
            els.searchHint.innerHTML = _emptyMeetingsHintHtml();
            els.searchHint.setAttribute("data-state", state);
        } else if (state === "no_completed" || state === "no_index") {
            els.searchQuery.placeholder = state === "no_completed"
                ? "전사가 완료되면 검색할 수 있습니다."
                : "검색 인덱스를 준비하면 검색할 수 있습니다.";
            els.searchHint.innerHTML = _notReadyHintHtml(state);
            els.searchHint.setAttribute("data-state", state);
        } else {
            els.searchQuery.placeholder = "검색어를 입력하세요 (예: 프로젝트 일정, 결정사항...)";
            if (els.searchHint.getAttribute("data-state") !== "default" &&
                    !els.searchResults.classList.contains("visible")) {
                els.searchHint.innerHTML = _defaultSearchHintHtml();
            }
            els.searchHint.setAttribute("data-state", "default");
            return;
        }

        els.searchResults.classList.remove("visible");
        els.searchLoading.classList.remove("visible");
        els.searchResultsList.innerHTML = "";
        els.searchEmpty.style.display = "none";
        els.searchHint.style.display = "block";
    };

    /**
     * 검색을 수행한다.
     */
    SearchView.prototype._performSearch = async function () {
        var self = this;
        var els = self._els;
        if (self._destroyed) return;
        var query = els.searchQuery.value.trim();
        if (!query) return;
        var readinessState = self._getReadinessState();
        if (readinessState !== "ready" && readinessState !== "unknown") {
            self._syncSearchAvailability();
            return;
        }
        var seq = self._searchSeq + 1;
        self._searchSeq = seq;

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
            if (self._destroyed || seq !== self._searchSeq) return;
            self._renderSearchResults(data);
        } catch (e) {
            if (self._destroyed || seq !== self._searchSeq) return;
            if (e.status === 503) {
                errorBanner.show("검색 엔진이 아직 초기화되지 않았습니다.");
            } else {
                errorBanner.show("검색 실패: " + e.message);
            }
            els.searchResults.classList.remove("visible");
            els.searchHint.style.display = "block";
        } finally {
            if (self._destroyed || seq !== self._searchSeq) return;
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
        if (self._destroyed) return;
        var els = self._els;
        var results = data.results || [];
        els.searchResultsList.innerHTML = "";

        // 통계 — 기본 화면은 업무 언어만 노출하고, 알고리즘 세부는 결과 카드 안에 접어둔다.
        App.safeText(els.searchStats, "관련 결과 " + results.length + "건");

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
                    "다른 키워드를 사용하거나 검색 범위를 넓혀 보세요");
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

            // 헤더: 회의 ID
            var header = document.createElement("div");
            header.className = "result-header";

            var meetingId = document.createElement("span");
            meetingId.className = "result-meeting-id";
            meetingId.textContent = item.meeting_id;

            header.appendChild(meetingId);

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
                var speakerLabels = item.speakers.map(_displaySpeakerLabel);
                speakerItem.innerHTML = Icons.person + ' <span>' + App.escapeHtml(speakerLabels.join(", ")) + '</span>';
                meta.appendChild(speakerItem);
            }

            var timeItem = document.createElement("span");
            timeItem.className = "result-meta-item";
            timeItem.innerHTML = Icons.clock + ' <span>' + App.escapeHtml(App.formatTime(item.start_time) +
                " ~ " + App.formatTime(item.end_time)) + '</span>';
            meta.appendChild(timeItem);

            var sourceLabels = {
                vector: "의미 기반",
                fts: "키워드",
                both: "복합",
            };
            var source = sourceLabels[item.source] ? item.source : "both";

            el.appendChild(header);
            el.appendChild(text);
            el.appendChild(meta);

            var details = document.createElement("details");
            details.className = "search-result-details";
            details.addEventListener("click", function (e) {
                e.stopPropagation();
            });
            details.addEventListener("keydown", function (e) {
                e.stopPropagation();
            });
            var summary = document.createElement("summary");
            summary.textContent = "검색 세부 정보";
            var debug = document.createElement("div");
            debug.className = "search-result-debug";
            debug.textContent =
                "방식 " + (sourceLabels[item.source] || item.source || "복합") +
                " · 점수 " + item.score.toFixed(4) +
                " · 청크 " + item.chunk_index;
            details.appendChild(summary);
            details.appendChild(debug);
            el.appendChild(details);

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
        this._destroyed = true;
        this._searchSeq += 1;
        this._listeners.forEach(function (entry) {
            entry.el.removeEventListener(entry.type, entry.fn);
        });
        this._listeners = [];
        this._timers.forEach(function (t) { clearInterval(t); clearTimeout(t); });
        this._timers = [];
    };


        return SearchView;
    }

    window.MeetingSearchView = {
        create: create,
    };
})();
