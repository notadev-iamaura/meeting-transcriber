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
        // 이 창의 마지막 검색만 메모리에 보관한다. 결과 본문을 브라우저 저장소에 쓰지 않는다.
        var lastSearch = null;

        if (!App || !Router) {
            throw new Error("MeetingSearchView requires App and Router");
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
        self._speakerScope = null;
        self._response = null;
        self._pendingBody = null;
        self._selectedIndex = -1;

        self._render();
        self._bind();
        if (lastSearch) self._restoreSearch(lastSearch);
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
            '        <input type="text" class="filter-input" id="searchFilterSpeaker" placeholder="예: 참석자 1, 나" aria-label="검색 화자 필터" aria-describedby="searchSpeakerScope" />',
            '      </div>',
            '      <button type="button" class="filter-clear-btn" id="searchFilterClearBtn" aria-label="검색 필터 초기화">',
            '        필터 초기화',
            '      </button>',
            '      <span class="search-speaker-scope" id="searchSpeakerScope" role="status"></span>',
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
            speakerScope: document.getElementById("searchSpeakerScope"),
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
            self._setSpeakerScope(null);
            if (els.searchQuery.value.trim() && !els.searchQuery.disabled) {
                self._performSearch();
            } else {
                self._searchSeq++;
                self._response = null;
                self._pendingBody = null;
                els.searchLoading.classList.remove("visible");
                els.searchBtn.disabled = els.searchQuery.disabled;
                els.searchResults.classList.remove("visible");
                els.searchEmpty.style.display = "none";
                els.searchHint.style.display = "block";
            }
        };
        els.filterClearBtn.addEventListener("click", onFilterClear);
        self._listeners.push({ el: els.filterClearBtn, type: "click", fn: onFilterClear });
        var onSpeakerInput = function () { self._setSpeakerScope(null); };
        els.filterSpeaker.addEventListener("input", onSpeakerInput);
        self._listeners.push({ el: els.filterSpeaker, type: "input", fn: onSpeakerInput });

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

    SearchView.prototype._setSpeakerScope = function (scope) {
        this._speakerScope = scope;
        this._els.speakerScope.textContent = scope ? "이 회의에서만 · " + scope.heading : "";
    };

    SearchView.prototype._restoreSearch = function (saved) {
        var els = this._els;
        els.searchQuery.value = saved.query;
        els.filterDate.value = saved.date;
        els.filterSpeaker.value = saved.speaker;
        this._setSpeakerScope(saved.scope);
        if (saved.pendingBody) {
            this._performSearch(saved.pendingBody);
            return;
        }
        if (!saved.response) return;
        els.searchResults.classList.add("visible");
        els.searchHint.style.display = "none";
        this._showResponse(saved.response);
        this._selectedIndex = saved.selectedIndex;
        var row = els.searchResultsList.children[saved.selectedIndex];
        if (row) row.querySelector(".result-open").focus({ preventScroll: true });
        Router.getContentEl().scrollTop = saved.scrollTop;
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
        els.searchBtn.disabled = disabled || !!this._pendingBody;
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
        this._searchSeq++;
        this._pendingBody = null;
        this._response = null;
        els.searchLoading.classList.remove("visible");
        els.searchResultsList.innerHTML = "";
        els.searchEmpty.style.display = "none";
        els.searchHint.style.display = "block";
    };

    /**
     * 검색을 수행한다.
     */
    SearchView.prototype._performSearch = async function (savedBody) {
        var self = this;
        var els = self._els;
        if (self._destroyed) return;
        var query = savedBody ? savedBody.query : els.searchQuery.value.trim();
        if (!query) return;
        var readinessState = self._getReadinessState();
        if (readinessState !== "ready" && readinessState !== "unknown") {
            self._syncSearchAvailability();
            return;
        }
        var seq = self._searchSeq + 1;
        self._searchSeq = seq;
        self._selectedIndex = -1;
        self._response = null;
        var body = savedBody || { query: query };
        if (!savedBody) {
            if (els.filterDate.value) body.date_filter = els.filterDate.value;
            var speaker = els.filterSpeaker.value.trim();
            if (self._speakerScope) {
                body.meeting_id_filter = self._speakerScope.meetingId;
                body.speaker_filter = self._speakerScope.raw;
            } else if (speaker) {
                if (/^SPEAKER_\d+$/i.test(speaker) || speaker === "UNKNOWN") body.speaker_filter = speaker;
                else body.speaker_label_filter = speaker;
            }
        }
        self._pendingBody = body;

        els.searchResults.classList.add("visible");
        els.searchLoading.classList.add("visible");
        els.searchResultsList.innerHTML = "";
        els.searchEmpty.style.display = "none";
        els.searchHint.style.display = "none";
        els.searchBtn.disabled = true;

        try {
            var data = await App.apiPost("/search", body);
            if (self._destroyed || seq !== self._searchSeq) return;
            self._showResponse(data);
            var sourceErrors = data.source_errors || {};
            if (Object.keys(sourceErrors).length) {
                errorBanner.show(Object.values(sourceErrors).join(" "));
            }
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
            self._pendingBody = null;
            els.searchLoading.classList.remove("visible");
            els.searchBtn.disabled = false;
        }
    };

    SearchView.prototype._showResponse = function (data) {
        this._response = data;
        this._renderSearchResults(data);
        if (Object.keys(data.source_errors || {}).length) {
            App.safeText(this._els.searchStats, "검색 일부 실패 · 확인된 결과 " + data.results.length + "건");
            if (!data.results.length) {
                App.safeText(document.getElementById("searchEmptyText"), "검색을 완료하지 못했습니다");
                App.safeText(document.getElementById("searchEmptySub"), "설정의 검색 인덱스에서 상태를 확인하고 복구해 주세요.");
            }
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

        var meetings = deps.ListPanel && deps.ListPanel.getMeetings ? deps.ListPanel.getMeetings() : [];
        results.forEach(function (item, index) {
            var el = document.createElement("div");
            el.className = "result-item";
            el.setAttribute("tabindex", "-1");

            // 회의 일시와 제목
            var header = document.createElement("div");
            header.className = "result-header";

            var openResult = document.createElement("button");
            openResult.type = "button";
            openResult.className = "result-open";
            var meetingId = document.createElement("span");
            meetingId.className = "result-meeting-id";
            meetingId.id = "searchResultMeeting" + index;
            App.renderReferenceHeading(meetingId, item, meetings);
            openResult.setAttribute("aria-labelledby", meetingId.id + " searchResultTime" + index);
            openResult.appendChild(meetingId);
            header.appendChild(openResult);

            // 본문 텍스트
            var text = document.createElement("div");
            text.className = "result-text";
            text.textContent = item.text;

            // 메타 정보
            var meta = document.createElement("div");
            meta.className = "result-meta";

            if (item.speakers && item.speakers.length > 0) {
                var speakerItem = document.createElement("span");
                speakerItem.className = "result-meta-item";
                speakerItem.innerHTML = Icons.person || "";
                item.speakers.forEach(function (speaker, i) {
                    if (i) speakerItem.appendChild(document.createTextNode(", "));
                    var label = App.speakerLabel(speaker, item.speaker_labels);
                    var chip = document.createElement("button");
                    chip.type = "button";
                    chip.className = "result-speaker-filter";
                    chip.textContent = label;
                    chip.setAttribute("aria-label", "이 회의의 " + label + " 발화로 검색");
                    chip.title = chip.getAttribute("aria-label");
                    chip.addEventListener("click", function (event) {
                        event.stopPropagation();
                        els.filterSpeaker.value = label;
                        self._setSpeakerScope({ meetingId: item.meeting_id, raw: speaker, heading: meetingId.textContent });
                        els.filterSpeaker.focus();
                        self._performSearch();
                    });
                    speakerItem.appendChild(chip);
                });
                meta.appendChild(speakerItem);
            }

            var timeItem = document.createElement("span");
            timeItem.className = "result-meta-item";
            timeItem.id = "searchResultTime" + index;
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
                self._selectedIndex = index;
                var viewerPath = "/app/viewer/" + encodeURIComponent(item.meeting_id);
                var params = [];
                var q = data.query || els.searchQuery.value.trim();
                if (q) params.push("q=" + encodeURIComponent(q));
                if (item.start_time != null) params.push("t=" + encodeURIComponent(item.start_time));
                if (params.length > 0) viewerPath += "?" + params.join("&");
                Router.navigate(viewerPath);
            });

            el.addEventListener("keydown", function (e) {
                if (e.target === el && (e.key === "Enter" || e.key === " ")) {
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
        var els = this._els;
        lastSearch = {
            query: els.searchQuery.value, date: els.filterDate.value, speaker: els.filterSpeaker.value,
            scope: this._speakerScope, response: this._response, pendingBody: this._pendingBody,
            selectedIndex: this._selectedIndex, scrollTop: Router.getContentEl().scrollTop,
        };
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
