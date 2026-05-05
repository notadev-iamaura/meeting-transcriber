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
        if (self._destroyed) return;
        var query = els.searchQuery.value.trim();
        if (!query) return;
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
            var sourceLabels = {
                vector: "벡터",
                fts: "키워드",
                both: "복합",
            };
            var source = sourceLabels[item.source] ? item.source : "both";
            sourceTag.className = "result-source-tag " + source;
            sourceTag.textContent = sourceLabels[item.source] || item.source || "복합";
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
