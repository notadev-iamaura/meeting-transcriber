/* =================================================================
 * Recap ViewerView boundary
 *
 * 목적: 회의록 뷰어를 SPA 라우터 본문에서 분리한다.
 * 공개 API: window.MeetingViewerView
 * ================================================================= */
(function () {
    "use strict";

    function create(deps) {
        deps = deps || {};
        var App = deps.App || window.MeetingApp;
        var Router = deps.Router || (window.SPA && window.SPA.Router);
        var ListPanel = deps.ListPanel || window.ListPanel;
        var BulkActionBar = deps.BulkActionBar;
        var Icons = deps.Icons || {};
        var PIPELINE_STEPS = deps.PIPELINE_STEPS || [];
        var errorBanner = deps.errorBanner || { show: function () {} };

        if (!App || !Router || !ListPanel) {
            throw new Error("MeetingViewerView requires App, Router, and ListPanel");
        }

        var TRANSCRIPT_STAGE_RANK = {
            transcribe: 1,
            merge: 2,
            correct: 3,
            corrected: 4,
        };

        // 이 창의 화면 이동은 메모리에서 복원하고, 같은 탭의 새로고침은 sessionStorage로 복원한다.
        var summaryDrafts = new Map();
        var summarySaves = new Map();
        var transcriptDrafts = new Map();
        var transcriptSaves = new Map();
        function meetingDrafts(id) {
            if (!transcriptDrafts.has(id)) {
                var entries = [];
                try {
                    entries = JSON.parse(sessionStorage.getItem("recap:transcript-drafts:" + id)) || [];
                } catch (_) { /* 창 메모리만 사용한다. */ }
                transcriptDrafts.set(id, new Map(Array.isArray(entries) ? entries.filter(function (pair) {
                    return Array.isArray(pair) && typeof pair[0] === "string" && pair[1] &&
                        typeof pair[1].base === "string" && typeof pair[1].text === "string";
                }) : []));
            }
            return transcriptDrafts.get(id);
        }
        function persistUtteranceDraft(id, key, draft) {
            var drafts = meetingDrafts(id);
            if (draft) drafts.set(key, draft);
            else drafts.delete(key);
            var storageKey = "recap:transcript-drafts:" + id;
            try {
                if (drafts.size) sessionStorage.setItem(storageKey, JSON.stringify(Array.from(drafts)));
                else sessionStorage.removeItem(storageKey);
                return true;
            } catch (_) {
                try { sessionStorage.removeItem(storageKey); } catch (_) { /* 메모리로 유지 */ }
                return false;
            }
        }
        function getSummaryDraft(id) {
            if (summaryDrafts.has(id)) return summaryDrafts.get(id);
            try {
                var draft = JSON.parse(sessionStorage.getItem("recap:summary-draft:" + id));
                if (draft && typeof draft.base === "string" && typeof draft.text === "string") {
                    summaryDrafts.set(id, draft);
                    return draft;
                }
            } catch (_) { /* 저장소 사용이 제한된 창에서도 계속 편집할 수 있다. */ }
            return null;
        }
        function setSummaryDraft(id, draft) {
            var key = "recap:summary-draft:" + id;
            if (draft) summaryDrafts.set(id, draft);
            else summaryDrafts.delete(id);
            try {
                if (draft) sessionStorage.setItem(key, JSON.stringify(draft));
                else sessionStorage.removeItem(key);
                return true;
            } catch (_) {
                try { sessionStorage.removeItem(key); } catch (_) { /* 메모리로 유지 */ }
                return false;
            }
        }

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
            self._speakerLabels = {};
            self._transcriptReadVersion = 0;
            self._allUtterances = [];
            self._micEchoCandidates = {};
            self._foldMicEchoes = false;
            self._vocabularyReviews = new Map();
            self._currentQuery = "";
            self._currentMatchIndex = -1;
            self._totalMatches = 0;
            self._searchTimeout = null;
            self._pipelinePollTimer = null;

            // 발화 또는 누락 구간 하나만 재생한다.
            self._audioElement = null;
            self._playingInterval = null;
            self._splitPlayback = false;

            // 실시간 처리 로그 상태 — 단계별 { elapsed, eta, status, anomaly, startedAt }
            // status: "pending" | "running" | "completed" | "failed" | "skipped"
            self._liveLog = {};
            self._liveLogTickTimer = null;
            self._transcriptSourceStage = "corrected";
            self._transcriptReadonly = false;
            self._transcriptSignature = "";
            self._alternateTranscriptionDialog = null;
            self._alternateTranscriptionCleanup = null;
            self._startTranscriptionDialog = null;
            self._startTranscriptionCleanup = null;
            self._audioFolderPending = false;
            self._micTranscription = null;
            self._micStatusPromise = null;
            self._micPollTimer = null;
            self._micRequestPending = false;
            self._micRetryObserved = false;
            self._micRequestError = "";
            self._micStatusEpoch = 0;
            self._summaryLoadVersion = 0;

            // URL 쿼리 파라미터에서 검색어, 타임스탬프 추출
            var urlParams = new URLSearchParams(window.location.search);
            self._initialQuery = urlParams.get("q") || "";
            self._initialTimestamp = parseFloat(urlParams.get("t") || "");

            self._render();
            self._bind();
            if (getSummaryDraft(meetingId) && !urlParams.has("t") && !urlParams.has("q")) {
                document.getElementById("viewerTabSummary").click();
            }
            self._loadData();
            self._lastTitlePending = false;
            var onTitleUpdate = function (event) {
                if (self._destroyed) return;
                var pending = BulkActionBar && BulkActionBar.isTitlePending(self._meetingId);
                if (pending !== self._lastTitlePending && self._lastMeetingData) self._renderActions(self._lastMeetingData);
                self._lastTitlePending = pending;
                if ((event.detail.ids || []).indexOf(self._meetingId) !== -1) self._loadMeetingInfo();
            };
            document.addEventListener("recap:titles-updated", onTitleUpdate);
            self._listeners.push({el: document, type: "recap:titles-updated", fn: onTitleUpdate});
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
                '    <div class="viewer-meeting-heading"><span class="meeting-heading-date" id="viewerMeetingDate"></span><h2 class="viewer-title" id="viewerMeetingTitle"></h2></div>',
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
                '    <span class="viewer-meta-item" id="viewerMetaSpeakers"></span>',
                '    <span class="viewer-meta-item" id="viewerMetaUtterances"></span>',
                '    <span class="viewer-meta-item transcription-provider-meta" id="viewerMetaTranscription" hidden></span>',
                '  </div>',
                '  <div class="transcript-source-status" id="viewerTranscriptSourceStatus"',
                '       role="status" aria-live="polite" style="display:none;"></div>',
                '  <div class="speaker-legend" id="viewerSpeakerLegend"></div>',
                '  <label class="viewer-track-choice" id="viewerTrackChoice" hidden>재생 트랙',
                '    <select id="viewerPlaybackTrack"><option value="mix">함께 듣기</option>',
                '      <option value="mic">내 마이크만</option><option value="system">상대만</option></select>',
                '  </label>',
                '  <div class="viewer-actions" id="viewerActions"></div>',
                '  <details class="viewer-log-panel" id="viewerLogPanel" style="display:none;">',
                '    <summary>처리 로그 <span class="log-total" id="viewerLogTotal"></span></summary>',
                '    <div class="log-table" id="viewerLogTable"></div>',
                '  </details>',
                '</div>',

                '<section class="mic-transcription-notice" id="viewerMicTranscription" hidden',
                '         aria-labelledby="viewerMicTitle" role="status" aria-live="polite">',
                '  <div class="mic-transcription-copy">',
                '    <strong id="viewerMicTitle"></strong>',
                '    <p id="viewerMicMessage"></p>',
                '    <details id="viewerMicIntervals" hidden><summary></summary>',
                '      <ul aria-label="확인이 필요한 마이크 구간"></ul></details>',
                '    <p class="mic-transcription-error" id="viewerMicError" hidden></p>',
                '  </div>',
                '  <button type="button" class="viewer-action-btn" id="viewerMicRetry">내 마이크만 다시 전사</button>',
                '</section>',

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

                '  <div class="mic-echo-review" id="viewerMicEchoReview" hidden>',
                '    <span>상대 발화와 시각·내용이 겹치는 내 마이크 발화</span>',
                '    <button type="button" class="viewer-action-btn" id="viewerMicEchoToggle" aria-controls="viewerTimeline"></button>',
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
                meetingDate: document.getElementById("viewerMeetingDate"),
                metaSpeakers: document.getElementById("viewerMetaSpeakers"),
                metaUtterances: document.getElementById("viewerMetaUtterances"),
                metaTranscription: document.getElementById("viewerMetaTranscription"),
                transcriptSourceStatus: document.getElementById("viewerTranscriptSourceStatus"),
                micNotice: document.getElementById("viewerMicTranscription"),
                micTitle: document.getElementById("viewerMicTitle"),
                micMessage: document.getElementById("viewerMicMessage"),
                micIntervals: document.getElementById("viewerMicIntervals"),
                trackChoice: document.getElementById("viewerTrackChoice"),
                playbackTrack: document.getElementById("viewerPlaybackTrack"),
                micError: document.getElementById("viewerMicError"),
                micRetry: document.getElementById("viewerMicRetry"),
                speakerLegend: document.getElementById("viewerSpeakerLegend"),
                viewerActions: document.getElementById("viewerActions"),
                logPanel: document.getElementById("viewerLogPanel"),
                logTotal: document.getElementById("viewerLogTotal"),
                logTable: document.getElementById("viewerLogTable"),
                tabNav: document.getElementById("viewerTabNav"),
                timeline: document.getElementById("viewerTimeline"),
                micEchoReview: document.getElementById("viewerMicEchoReview"),
                micEchoToggle: document.getElementById("viewerMicEchoToggle"),
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

            var onMicRetry = function () { self._retryMicTranscription(); };
            var onTrackChange = function () {
                if (self._playingInterval) self._playInterval(self._playingInterval);
            };
            els.playbackTrack.addEventListener("change", onTrackChange);
            self._listeners.push({ el: els.playbackTrack, type: "change", fn: onTrackChange });
            els.micRetry.addEventListener("click", onMicRetry);
            self._listeners.push({ el: els.micRetry, type: "click", fn: onMicRetry });
            var onMicProgress = function (event) {
                var detail = event.detail || {};
                if (detail.meeting_id && detail.meeting_id !== self._meetingId) return;
                if (event.type === "ws:mic_transcription_warning") {
                    // 먼저 도착한 상태 조회가 방금 받은 누락 경고를 덮지 못하게 한다.
                    self._micStatusEpoch += 1;
                    self._applyMicTranscription(detail);
                }
                self._loadMicTranscription();
            };
            ["ws:mic_transcription_warning", "ws:pipeline_status", "ws:job_completed", "ws:job_failed", "ws:job_cancelled"].forEach(function (type) {
                document.addEventListener(type, onMicProgress);
                self._listeners.push({ el: document, type: type, fn: onMicProgress });
            });

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

            var onEchoToggle = function () {
                self._foldMicEchoes = !self._foldMicEchoes;
                self._syncMicEchoVisibility();
            };
            els.micEchoToggle.addEventListener("click", onEchoToggle);
            self._listeners.push({el: els.micEchoToggle, type: "click", fn: onEchoToggle});

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
                if (!detail.meeting_id || detail.meeting_id === self._meetingId) {
                    self._loadTranscript({ silent: true });
                }
                if (detail.step === "summarize") {
                    self._loadSummary();
                }
            };
            document.addEventListener("ws:pipeline_status", onPipelineStatus);
            self._listeners.push({ el: document, type: "ws:pipeline_status", fn: onPipelineStatus });
            document.addEventListener("ws:reindex_progress", onPipelineStatus);
            self._listeners.push({ el: document, type: "ws:reindex_progress", fn: onPipelineStatus });

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
        ViewerView.prototype._requestSummarize = async function () {
            var self = this;
            var els = self._els;
            var btn = els.summarizeBtn;
            var originalText = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = Icons.gear + ' 요약 준비 중...';
            btn.classList.add("loading");

            try {
                var url = "/meetings/" + encodeURIComponent(self._meetingId) + "/summarize";
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
                        if (summary.markdown && !self._destroyed) {
                            clearInterval(pollTimer);
                            btn.classList.remove("loading");
                            // _renderSummaryView 를 거쳐야 편집 툴바가 다시 그려지고
                            // _currentSummaryMd 가 올바르게 갱신된다.
                            els.summaryEmpty.style.display = "none";
                            if (els.summarizeBtn) els.summarizeBtn.style.display = "none";
                            self._showLoadedSummary(summary.markdown);
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
        ViewerView.prototype._loadData = async function () {
            this._loadMicTranscription();
            await this._loadMeetingInfo();
            this._loadTranscript();
            this._loadSummary();
        };

        ViewerView.prototype._renderMicTranscription = function () {
            var els = this._els;
            var data = this._micTranscription || {};
            var expired = Boolean(this._lastMeetingData && this._lastMeetingData.audio_expired_at);
            els.trackChoice.hidden = expired || !this._splitPlayback;
            if (expired && this._playingInterval) this._stopUtterancePlayback();
            var status = data.status || "not_applicable";
            var pending = this._micRequestPending || status === "retry_queued" || status === "retrying";
            var failed = ["empty", "low_volume", "partial", "retry_failed"].indexOf(status) !== -1;
            var finished = this._micRetryObserved && (status === "complete" || status === "no_speech");
            var visible = !(this._lastMeetingData && this._lastMeetingData.audio_expired_at) &&
                (pending || failed || finished || Boolean(this._micRequestError));
            var uncovered = Array.isArray(data.uncovered_intervals) ? data.uncovered_intervals.filter(function (interval) {
                return Array.isArray(interval) && interval.length === 2 &&
                    Number.isFinite(interval[0]) && Number.isFinite(interval[1]) &&
                    interval[0] >= 0 && interval[1] > interval[0];
            }) : [];
            var signature = JSON.stringify([status, pending, visible, data.retry_available, data.message, data.no_new_speech, uncovered, this._micRequestError]);
            if (signature === this._micNoticeSignature) return;
            this._micNoticeSignature = signature;
            els.micNotice.hidden = !visible;
            if (!visible) {
                if (this._playingInterval && els.micIntervals.contains(this._playingInterval.btn)) {
                    this._stopUtterancePlayback();
                }
                return;
            }
            els.micNotice.classList.toggle("is-error", (!pending && failed) || Boolean(this._micRequestError));
            els.micNotice.classList.toggle("is-complete", !pending && finished && status === "complete");
            els.micNotice.setAttribute("aria-busy", pending ? "true" : "false");
            els.micTitle.textContent = pending
                ? (status === "retrying" ? "내 마이크를 다시 전사하고 있습니다" : "내 마이크 재전사를 기다리고 있습니다")
                : status === "complete" ? (data.no_new_speech ? "새로 추가할 내 발화가 없습니다" : "내 마이크 전사를 반영했습니다")
                : status === "no_speech" ? "마이크 트랙 확인을 마쳤습니다"
                : status === "retry_failed" ? "내 마이크 재전사를 완료하지 못했습니다"
                : status === "partial" ? "내 마이크 발화 일부가 전사되지 않았을 수 있습니다"
                : "내 마이크 음성이 전사문에 반영되지 않았습니다";
            els.micMessage.textContent = pending
                ? "마이크 트랙만 처리합니다. 기존 전사문은 계속 확인할 수 있습니다."
                : data.message || (status === "complete"
                    ? "추가된 발화와 요약 내용을 확인해 주세요."
                    : "마이크에서 음성을 감지했지만 텍스트를 만들지 못했습니다. 마이크 트랙만 다시 전사할 수 있습니다.");
            els.micError.hidden = !this._micRequestError;
            els.micError.textContent = this._micRequestError;
            els.micIntervals.hidden = pending || status !== "partial" || uncovered.length === 0;
            var self = this;
            var list = els.micIntervals.querySelector("ul");
            if (self._playingInterval && list.contains(self._playingInterval.btn)) self._stopUtterancePlayback();
            els.micIntervals.querySelector("summary").textContent = "의심 구간 " + uncovered.length + "개 듣기";
            list.replaceChildren();
            uncovered.forEach(function (bounds) {
                var item = document.createElement("li");
                var button = document.createElement("button");
                button.type = "button";
                button.className = "viewer-action-btn";
                button.textContent = App.formatTime(bounds[0]) + "–" + App.formatTime(bounds[1]) + " 듣기";
                button.addEventListener("click", function () {
                    self._toggleInterval(bounds[0], bounds[1], item, button);
                });
                item.appendChild(button);
                list.appendChild(item);
            });
            els.micRetry.hidden = !failed && !pending;
            els.micRetry.disabled = pending || data.retry_available !== true;
            els.micRetry.textContent = pending ? "재전사 처리 중…" : "내 마이크만 다시 전사";
        };

        ViewerView.prototype._applyMicTranscription = function (data) {
            if (this._destroyed || !data || typeof data.status !== "string") return;
            var previous = this._micTranscription && this._micTranscription.status;
            var pending = data.status === "retry_queued" || data.status === "retrying";
            if (pending) this._micRetryObserved = true;
            this._micTranscription = data;
            // WebSocket 진행 이벤트에는 원본 검사 결과가 없을 수 있다.
            if (typeof data.split_playback_available === "boolean") {
                this._splitPlayback = data.split_playback_available;
            }
            this._renderMicTranscription();
            if (this._lastMeetingData) this._renderActions(this._lastMeetingData);
            clearTimeout(this._micPollTimer);
            this._micPollTimer = null;
            if (pending) {
                var self = this;
                this._micPollTimer = setTimeout(function () { self._loadMicTranscription(); }, 2000);
            } else if ((previous === "retrying" || previous === "retry_queued") && (data.status === "complete" || data.status === "partial")) {
                this._micStatusEpoch += 1;
                this._loadMeetingInfo();
                this._loadTranscript({ force: true });
                this._loadSummary();
            }
        };

        ViewerView.prototype._loadMicTranscription = function () {
            var self = this;
            if (self._destroyed || self._micRequestPending) return Promise.resolve();
            if (self._micStatusPromise) return self._micStatusPromise;
            var epoch = self._micStatusEpoch;
            self._micStatusPromise = App.apiRequest(
                "/meetings/" + encodeURIComponent(self._meetingId) + "/mic-transcription"
            ).then(function (data) {
                if (self._destroyed || epoch !== self._micStatusEpoch) return;
                self._micRequestError = "";
                self._applyMicTranscription(data);
            }).catch(function () {
                if (self._destroyed || epoch !== self._micStatusEpoch) return;
                var status = self._micTranscription && self._micTranscription.status;
                if (status === "retry_queued" || status === "retrying") {
                    self._micRequestError = "처리 상태를 확인하지 못했습니다. 잠시 후 다시 확인합니다.";
                    self._renderMicTranscription();
                    clearTimeout(self._micPollTimer);
                    self._micPollTimer = setTimeout(function () { self._loadMicTranscription(); }, 2000);
                }
            }).finally(function () { self._micStatusPromise = null; });
            return self._micStatusPromise;
        };

        ViewerView.prototype._retryMicTranscription = async function () {
            var self = this;
            if (self._destroyed || self._micRequestPending || !self._micTranscription || self._micTranscription.retry_available !== true) return;
            self._micRequestPending = true;
            self._micRequestError = "";
            self._micStatusEpoch += 1;
            clearTimeout(self._micPollTimer);
            self._renderMicTranscription();
            if (self._lastMeetingData) self._renderActions(self._lastMeetingData);
            try {
                await App.apiRequest("/meetings/" + encodeURIComponent(self._meetingId) + "/mic-transcription/retry", { method: "POST" });
                if (self._destroyed) return;
                self._micRetryObserved = true;
                self._applyMicTranscription({ status: "retry_queued", retry_available: false });
            } catch (error) {
                if (self._destroyed) return;
                self._micRequestError = "재전사 요청 실패: " + (error.message || "잠시 후 다시 시도해 주세요.");
                if (error.status === 409) self._micRefreshAfterConflict = true;
            } finally {
                self._micRequestPending = false;
                if (!self._destroyed) {
                    self._renderMicTranscription();
                    if (self._lastMeetingData) self._renderActions(self._lastMeetingData);
                    if (!self._micRequestError || self._micRefreshAfterConflict) self._loadMicTranscription();
                    self._micRefreshAfterConflict = false;
                }
            }
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

        ViewerView.prototype._makeSpeakerBadge = function (speaker, displayLabel, className) {
            var badge = document.createElement("span");
            badge.className = className;
            badge.setAttribute("aria-hidden", "true");
            if (speaker && speaker === this._selfSpeakerId) {
                badge.classList.add("is-self");
                badge.innerHTML = Icons.person || "나";
            } else {
                badge.style.setProperty("--speaker-color", this._getSpeakerColor(speaker));
                badge.textContent = /^참석자 [\d?]+$/.test(displayLabel) ? displayLabel.slice(4) : displayLabel.slice(0, 1);
            }
            return badge;
        };

        ViewerView.prototype._openSelfSpeakerDialog = function (trigger, selectedSpeaker, utteranceIndex) {
            var self = this;
            if (!self._canSetSelfSpeaker || self._selfSpeakerDialog) return;
            var previousId = self._selfSpeakerId;
            var revision = self._selfSpeakerRevision;
            var labels = App.buildSpeakerLabels(self._allUtterances);
            var dialog = document.createElement("dialog");
            dialog.className = "transcription-model-dialog self-speaker-dialog";
            dialog.setAttribute("aria-labelledby", "selfSpeakerHeading");
            dialog.setAttribute("aria-describedby", "selfSpeakerHelp");
            dialog.innerHTML =
                '<form class="transcription-model-dialog-content"><header class="transcription-model-dialog-header"><div>' +
                '<h2 id="selfSpeakerHeading"></h2><p id="selfSpeakerHelp">선택한 화자의 모든 발화를 이 회의에서 ‘나’로 표시합니다.</p>' +
                '</div></header><label for="selfSpeakerChoice">내 화자</label>' +
                '<select id="selfSpeakerChoice" class="transcription-model-select"></select>' +
                '<p role="alert" hidden></p><footer class="transcription-model-dialog-actions">' +
                '<button type="button" class="transcription-dialog-cancel">취소</button>' +
                '<button type="submit" class="transcription-dialog-submit"></button></footer></form>';
            var form = dialog.querySelector("form");
            var select = dialog.querySelector("select");
            var cancel = dialog.querySelector("button[type=button]");
            var submit = dialog.querySelector("button[type=submit]");
            var error = dialog.querySelector("[role=alert]");
            dialog.querySelector("h2").textContent = previousId ? "내 화자 변경" : "내 화자 지정";
            var emptyChoice = new Option(previousId ? "지정 해제" : "화자 선택", "");
            emptyChoice.disabled = !previousId;
            select.add(emptyChoice);
            Object.keys(labels).forEach(function (raw) {
                if (raw !== "UNKNOWN") select.add(new Option(labels[raw] + (raw === previousId ? " (현재 나)" : ""), raw));
            });
            select.value = selectedSpeaker || previousId || "";
            function updateSubmit() {
                submit.textContent = select.value || !previousId ? "나로 지정" : "지정 해제";
                submit.disabled = (select.value || null) === previousId;
            }
            updateSubmit();
            select.addEventListener("change", updateSubmit);
            cancel.addEventListener("click", function () { dialog.close(); });
            dialog.addEventListener("cancel", function (event) {
                if (select.disabled) event.preventDefault();
            });
            dialog.addEventListener("close", function () {
                dialog.remove();
                self._selfSpeakerDialog = null;
                if (self._destroyed) return;
                var returnTo = trigger;
                if (!returnTo.isConnected && utteranceIndex !== undefined) {
                    var row = self._els.timeline.children[utteranceIndex];
                    returnTo = row && row.querySelector("button.utterance-speaker");
                }
                if (!returnTo || !returnTo.isConnected) returnTo = self._els.speakerLegend.querySelector("button");
                if (returnTo) returnTo.focus({preventScroll: true});
            });
            form.addEventListener("submit", async function (event) {
                event.preventDefault();
                if (submit.disabled) return;
                select.disabled = cancel.disabled = submit.disabled = true;
                error.hidden = true;
                try {
                    var data = await App.apiRequest("/meetings/" + encodeURIComponent(self._meetingId) + "/self-speaker", {
                        method: "PUT", headers: {"Content-Type": "application/json"},
                        body: JSON.stringify({speaker_id: select.value || null,
                            expected_self_speaker_id: previousId, expected_revision: revision}),
                    });
                    if (self._destroyed) return;
                    self._transcriptReadVersion++;
                    self._applyTranscriptResponse(data, {silent: true});
                    dialog.close();
                } catch (e) {
                    if (self._destroyed) return;
                    if (e.status === 409 || e.status === 412) {
                        dialog.close();
                        errorBanner.show("화자 정보가 변경되었습니다. 최신 전사문에서 다시 선택해 주세요.");
                        await self._loadTranscript({silent: true});
                        return;
                    }
                    error.textContent = e.message || "저장하지 못했습니다. 다시 시도해 주세요.";
                    error.hidden = false;
                    select.disabled = cancel.disabled = false;
                    updateSubmit();
                    submit.focus();
                }
            });
            self._els.speakerLegend.parentNode.appendChild(dialog);
            self._selfSpeakerDialog = dialog;
            dialog.showModal();
            select.focus();
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
                var displayLabel = App.speakerLabel(speaker, self._speakerLabels);
                var label = document.createElement("span");
                label.textContent = displayLabel;
                chip.appendChild(self._makeSpeakerBadge(speaker, displayLabel, "speaker-badge"));
                chip.appendChild(label);
                if (speaker === self._selfSpeakerId) {
                    chip.classList.add("is-self");
                    var sourceText = {microphone: "내 마이크", manual: "직접 지정"}[self._selfSpeakerSource];
                    if (sourceText) {
                        var source = document.createElement("span");
                        source.className = "speaker-source";
                        source.textContent = sourceText;
                        chip.appendChild(source);
                    }
                }
                legendEl.appendChild(chip);
            });
            if (self._canSetSelfSpeaker) {
                var trigger = document.createElement("button");
                trigger.type = "button";
                trigger.className = "viewer-action-btn self-speaker-trigger";
                trigger.setAttribute("aria-haspopup", "dialog");
                trigger.textContent = self._selfSpeakerId ? "내 화자 변경" : "내 화자 지정";
                trigger.addEventListener("click", function () { self._openSelfSpeakerDialog(trigger); });
                legendEl.appendChild(trigger);
            }
        };

        /** 화자만 바뀌어도 발화 편집기, 재생 버튼과 스크롤 위치는 그대로 둔다. */
        ViewerView.prototype._renderRowSpeaker = function (row, speaker, index) {
            var self = this;
            var label = App.speakerLabel(speaker, self._speakerLabels);
            row.classList.toggle("is-self", Boolean(speaker && speaker === self._selfSpeakerId));
            var badge = self._makeSpeakerBadge(speaker, label, "utterance-badge");
            var oldBadge = row.querySelector(".utterance-badge");
            if (oldBadge) oldBadge.replaceWith(badge);
            else row.prepend(badge);

            var canChoose = self._canSetSelfSpeaker && speaker && speaker.trim() && speaker !== "UNKNOWN";
            var name = document.createElement(canChoose ? "button" : "span");
            name.className = "utterance-speaker";
            name.textContent = label;
            if (canChoose) {
                name.type = "button";
                name.title = self._selfSpeakerId ? "내 화자 변경" : "내 화자 지정";
                name.setAttribute("aria-label", label + " · " + name.title);
                name.setAttribute("aria-haspopup", "dialog");
                name.addEventListener("click", function () { self._openSelfSpeakerDialog(name, speaker, index); });
            }
            var oldName = row.querySelector(".utterance-speaker");
            if (oldName) oldName.replaceWith(name);
            else row.querySelector(".utterance-header").prepend(name);
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
            if (self._playingInterval && els.timeline.contains(self._playingInterval.btn)) {
                self._stopUtterancePlayback();
            }
            els.timeline.innerHTML = "";
            var matchCount = 0;
            var draftRows = new Map();
            var drafts = meetingDrafts(self._meetingId);

            utterances.forEach(function (u, utteranceIndex) {
                var el = document.createElement("div");
                el.className = "utterance";

                // 우측 콘텐츠 영역
                var content = document.createElement("div");
                content.className = "utterance-content";

                // 헤더 (화자명 + 타임스탬프)
                var header = document.createElement("div");
                header.className = "utterance-header";

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

                header.appendChild(timeEl);
                if (!self._lastMeetingData || !self._lastMeetingData.audio_expired_at) header.appendChild(playBtn);
                var remoteIndex = self._micEchoCandidates[utteranceIndex];
                var remote = utterances[remoteIndex];
                if (Number.isInteger(remoteIndex) && remote) {
                    el.classList.add("mic-echo-candidate");
                    var label = document.createElement("span");
                    label.className = "mic-echo-label";
                    label.textContent = "중복 후보 · " + (self._speakerLabels[remote.speaker] || remote.speaker)
                        + " " + App.formatTime(remote.start);
                    header.appendChild(label);
                }

                // 텍스트
                var textEl = document.createElement("div");
                textEl.className = "utterance-text";
                var anchor = JSON.stringify([u.speaker, u.start, u.end]);
                var occurrence = draftRows.get(anchor) || 0;
                draftRows.set(anchor, occurrence + 1);
                textEl.dataset.draftKey = anchor + "/" + occurrence;
                textEl.title = self._canEditTranscript()
                    ? "더블클릭 또는 Enter로 편집"
                    : "읽기 전용 전사문";

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
                if (self._canEditTranscript()) {
                    textEl.tabIndex = 0;
                    textEl.addEventListener("dblclick", function () {
                        self._beginEditUtterance(utteranceIndex, textEl);
                    });
                    textEl.addEventListener("keydown", function (event) {
                        if (event.target === textEl && event.key === "Enter") {
                            event.preventDefault();
                            self._beginEditUtterance(utteranceIndex, textEl);
                        }
                    });
                }

                // ▶ 버튼 → 해당 발화 구간만 재생 (toggle)
                (function (idx, container, btn) {
                    btn.addEventListener("click", function (e) {
                        e.stopPropagation();
                        self._toggleUtterancePlayback(idx, container, btn);
                    });
                })(utteranceIndex, el, playBtn);

                content.appendChild(header);
                content.appendChild(textEl);
                var review = self._vocabularyReviews.get(utteranceIndex);
                if (review) {
                    if (review.text === u.text && review.start === u.start && review.speaker === u.speaker) {
                        content.appendChild(review.element);
                    } else {
                        self._vocabularyReviews.delete(utteranceIndex);
                    }
                }

                el.appendChild(content);
                self._renderRowSpeaker(el, u.speaker, utteranceIndex);
                els.timeline.appendChild(el);
                var draft = drafts.get(textEl.dataset.draftKey);
                if (draft) self._beginEditUtterance(utteranceIndex, textEl, draft);
            });

            self._syncMicEchoVisibility();
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

        /** 같은 시각·같은 버튼을 다시 선택하면 정지한다. */
        ViewerView.prototype._toggleInterval = function (start, end, el, btn) {
            if (this._playingInterval && this._playingInterval.btn === btn) {
                this._stopUtterancePlayback();
                return;
            }
            if (!Number.isFinite(start) || !Number.isFinite(end) || !(end > start)) return;
            this._playInterval({ start: Math.max(0, start), end: end, el: el, btn: btn,
                label: btn.getAttribute("aria-label"), caption: btn.textContent });
        };

        /** 직접 녹음은 서버가 고른 구간 WAV, 일반 파일은 하나의 회의 음원에서 재생한다. */
        ViewerView.prototype._playInterval = function (interval) {
            var self = this;
            self._stopUtterancePlayback();
            if (self._lastMeetingData && self._lastMeetingData.audio_expired_at) return;
            var audio = document.createElement("audio");
            var split = self._splitPlayback;
            var url = "/api/meetings/" + encodeURIComponent(self._meetingId) + "/audio";
            if (split) {
                url += "/clip?" + new URLSearchParams({ track: self._els.playbackTrack.value,
                    start: String(interval.start), end: String(interval.end) }).toString();
            }
            self._audioElement = audio;
            self._playingInterval = interval;
            interval.btn.classList.add("playing");
            interval.el.classList.add("playing");
            interval.btn.setAttribute("aria-label", "재생 정지");
            if (!interval.label) interval.btn.textContent = interval.caption.replace(/듣기$/, "정지");
            var stop = function () {
                if (self._audioElement === audio) self._stopUtterancePlayback();
            };
            var fail = function () {
                if (self._audioElement !== audio) return;
                stop();
                errorBanner.show("이 구간을 재생할 수 없습니다. 음성 파일의 보관 상태를 확인해 주세요.");
            };
            audio.addEventListener("ended", stop);
            audio.addEventListener("error", fail);
            if (!split) {
                audio.addEventListener("loadedmetadata", function () {
                    if (self._audioElement === audio) audio.currentTime = interval.start;
                }, { once: true });
                audio.addEventListener("timeupdate", function () {
                    if (audio.currentTime >= interval.end) stop();
                });
            }
            audio.src = url;
            audio.hidden = true;
            document.body.appendChild(audio);
            try {
                if (!split) audio.currentTime = interval.start;
                var playing = audio.play();
                if (playing) playing.catch(fail);
            } catch (_) { fail(); }
        };

        ViewerView.prototype._toggleUtterancePlayback = function (index, el, btn) {
            var row = this._allUtterances[index];
            if (row) this._toggleInterval(Math.max(0, row.start - 0.2), row.end, el, btn);
        };

        ViewerView.prototype._stopUtterancePlayback = function () {
            var interval = this._playingInterval;
            this._playingInterval = null;
            var audio = this._audioElement;
            this._audioElement = null;
            if (audio) {
                audio.pause();
                audio.removeAttribute("src");
                audio.load();
                audio.remove();
            }
            if (!interval) return;
            interval.el.classList.remove("playing");
            interval.btn.classList.remove("playing");
            if (interval.label) interval.btn.setAttribute("aria-label", interval.label);
            else {
                interval.btn.removeAttribute("aria-label");
                interval.btn.textContent = interval.caption;
            }
        };

        /**
         * 특정 발화를 인라인 편집 모드로 전환한다 (더블클릭 핸들러).
         * @param {number} index - self._allUtterances 배열 내 인덱스
         * @param {HTMLElement} textEl - 현재 텍스트를 보여주는 element
         */
        ViewerView.prototype._beginEditUtterance = function (index, textEl, draft) {
            var self = this;
            if (!self._canEditTranscript()) {
                return;
            }
            if (!self._allUtterances || index < 0 || index >= self._allUtterances.length) return;
            if (textEl.classList.contains("editing")) return;
            var openEditor = self._els.timeline.querySelector(".utterance-textarea");
            if (!draft && openEditor) {
                openEditor.focus();
                return;
            }

            var originalText = self._allUtterances[index].text;
            var key = textEl.dataset.draftKey;
            var id = self._meetingId;
            var base = draft ? draft.base : originalText;
            textEl.removeAttribute("tabindex");
            textEl.classList.add("editing");
            textEl.innerHTML = "";

            var textarea = document.createElement("textarea");
            textarea.className = "utterance-textarea";
            textarea.setAttribute("aria-label", "발화 편집");
            textarea.maxLength = 10000;
            textarea.value = draft ? draft.text : originalText;
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
            var draftStatus = document.createElement("span");
            draftStatus.className = "summary-draft-status";
            draftStatus.setAttribute("role", "status");
            actions.prepend(draftStatus);
            function remember() {
                var changed = textarea.value !== originalText;
                var stored = persistUtteranceDraft(id, key, changed ? {
                    base: base, text: textarea.value, selection: [textarea.selectionStart, textarea.selectionEnd]
                } : null);
                draftStatus.textContent = changed ? (stored ? "이 창에 초안 보관됨" : "화면 이동 시 초안 유지") : "";
            }
            textarea.addEventListener("input", remember);
            if (draft) {
                draftStatus.textContent = "편집 초안 복원됨";
                if (Array.isArray(draft.selection)) textarea.setSelectionRange(draft.selection[0], draft.selection[1]);
            }
            var saveError = document.createElement("p");
            saveError.className = "utterance-edit-error";
            saveError.setAttribute("role", "alert");
            saveError.hidden = true;
            textEl.appendChild(saveError);

            if (draft && draft.error) {
                saveError.textContent = draft.error;
                saveError.hidden = false;
            }
            if (draft && draft.needsRefresh && draft.failedBase === originalText) {
                var refreshBtn = document.createElement("button");
                refreshBtn.type = "button";
                refreshBtn.className = "btn-icon";
                refreshBtn.textContent = "최신 전사문 보기";
                refreshBtn.addEventListener("click", function () { self._loadTranscript(); });
                actions.appendChild(refreshBtn);
                saveBtn.disabled = true;
            } else if (draft && originalText !== base && originalText !== draft.text) {
                saveError.textContent = "원문이 변경됐습니다. 현재 저장된 문장: " + originalText;
                saveError.hidden = false;
                saveBtn.textContent = "현재 문장을 초안으로 바꾸기";
            }
            function markSaving() {
                saveBtn.disabled = cancelBtn.disabled = textarea.readOnly = true;
                saveBtn.textContent = "저장 중…";
                saveError.hidden = true;
            }
            if (transcriptSaves.has(id)) markSaving();
            var restore = function () {
                if (cancelBtn.disabled) return;
                persistUtteranceDraft(id, key, null);
                self._renderTimeline(self._allUtterances, self._currentQuery || "");
                var row = self._els.timeline.children[index];
                if (row && !self._destroyed) row.querySelector(".utterance-text").focus({preventScroll: true});
            };
            cancelBtn.addEventListener("click", restore);
            saveBtn.addEventListener("click", function () {
                if (saveBtn.disabled || transcriptSaves.has(id)) return;
                var next = textarea.value;
                if (next === originalText) {
                    restore();
                    return;
                }
                markSaving();
                var request = self._saveTranscript(index, originalText, next).then(function (data) {
                    persistUtteranceDraft(id, key, null);
                    return data;
                }).catch(function (err) {
                    var latest = meetingDrafts(id).get(key);
                    persistUtteranceDraft(id, key, Object.assign({}, latest, {
                        base: base, text: next, failedBase: originalText,
                        needsRefresh: err.status === 409 || err.status === 412,
                        error: "전사문 저장 실패 · " + err.message
                    }));
                    return null;
                }).finally(function () {
                    transcriptSaves.delete(id);
                });
                transcriptSaves.set(id, request);
                request.then(function (data) {
                    if (self._destroyed) return;
                    var active = document.activeElement;
                    var returnFocus = active === document.body || self._els.timeline.contains(active);
                    if (!data) {
                        self._renderTimeline(self._allUtterances, self._currentQuery);
                        var editorRow = self._els.timeline.children[index];
                        var editor = editorRow && editorRow.querySelector("textarea");
                        if (returnFocus && editor) editor.focus({preventScroll: true});
                        return;
                    }
                    self._transcriptReadVersion++;
                    self._applyTranscriptResponse(data, {force: true, silent: true});
                    var savedRow = self._els.timeline.children[index];
                    if (!savedRow) return;
                    if (data.vocabulary_suggestions && data.vocabulary_suggestions.length) {
                        self._showVocabularyReview(index, savedRow, data.vocabulary_suggestions, returnFocus);
                    } else if (returnFocus) {
                        savedRow.querySelector(".utterance-text").focus({preventScroll: true});
                    }
                });
            });
            textarea.addEventListener("keydown", function (e) {
                if (e.isComposing) return;
                if (e.key === "Escape") {
                    e.preventDefault();
                    restore();
                } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                    e.preventDefault();
                    saveBtn.click();
                }
            });

            if (!draft) setTimeout(function () {
                if (textarea.isConnected) {
                    textarea.focus();
                    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
                }
            }, 0);
        };

        /**
         * 편집을 시작했을 때의 문장과 새 문장을 보낸다. 저장 전까지 화면의 원문은 유지한다.
         */
        ViewerView.prototype._saveTranscript = async function (index, before, after) {
            if (!this._canEditTranscript()) throw new Error("처리 중이거나 읽기 전용인 전사문은 수정할 수 없습니다.");
            var utterances = this._allUtterances.map(function (u, i) {
                return {
                    text: i === index ? after : u.text,
                    original_text: u.original_text || u.text,
                    speaker: u.speaker,
                    start: u.start,
                    end: u.end,
                    was_corrected: i === index || !!u.was_corrected,
                };
            });
            return App.apiRequest("/meetings/" + encodeURIComponent(this._meetingId) + "/transcript", {
                method: "PUT", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({utterances: utterances, vocabulary_edit: {utterance_index: index, before_text: before}}),
            });
        };

        /** 저장한 발화의 변경 후보를 검토하고 선택한 표현만 용어집에 등록한다. */
        ViewerView.prototype._showVocabularyReview = function (index, row, suggestions, returnFocus) {
            var self = this;
            var form = document.createElement("form");
            form.className = "vocabulary-review";
            form.setAttribute("aria-label", "용어집에 추가할 표현");
            form.innerHTML =
                '<div class="vocabulary-review-title"><strong>용어집에 추가할 표현</strong><span>전사문 저장됨</span></div>' +
                '<div class="vocabulary-review-fields"></div>' +
                '<p class="utterance-edit-error" role="alert" hidden></p>' +
                '<div class="vocabulary-review-actions"><button type="button" class="viewer-action-btn">건너뛰기</button>' +
                '<button type="submit" class="viewer-action-btn">용어집에 추가</button></div>';
            var fields = form.querySelector(".vocabulary-review-fields");
            var error = form.querySelector('[role="alert"]');
            var skip = form.querySelector('button[type="button"]');
            var submit = form.querySelector('button[type="submit"]');
            var choices = suggestions.map(function (suggestion, i) {
                var field = document.createElement("div");
                field.className = "vocabulary-review-pair";
                field.innerHTML =
                    '<input type="checkbox" checked>' +
                    '<label>기존 표기<input type="text" maxlength="100" required></label>' +
                    '<span aria-hidden="true">→</span>' +
                    '<label>정답 표기<input type="text" maxlength="100" required></label>';
                var inputs = field.querySelectorAll("input");
                var check = inputs[0], alias = inputs[1], term = inputs[2];
                check.setAttribute("aria-label", "표현 " + (i + 1) + " 선택");
                alias.setAttribute("aria-label", "기존 표기 " + (i + 1));
                term.setAttribute("aria-label", "정답 표기 " + (i + 1));
                alias.value = suggestion.alias;
                term.value = suggestion.term;
                fields.appendChild(field);
                return {check: check, alias: alias, term: term};
            });
            function updateControls(saving) {
                choices.forEach(function (choice) {
                    choice.check.disabled = saving;
                    choice.alias.disabled = choice.term.disabled = saving || !choice.check.checked;
                });
                skip.disabled = saving;
                submit.disabled = saving || !choices.some(function (choice) { return choice.check.checked; });
                submit.textContent = saving ? "추가 중…" : "용어집에 추가";
            }
            form.addEventListener("change", function () { updateControls(false); });
            skip.addEventListener("click", function () {
                var text = form.parentElement.querySelector(".utterance-text");
                self._vocabularyReviews.delete(index);
                form.remove();
                text.focus({preventScroll: true});
            });
            form.addEventListener("submit", async function (event) {
                event.preventDefault();
                if (submit.disabled) return;
                var corrections = choices.filter(function (choice) { return choice.check.checked; }).map(function (choice) {
                    return {alias: choice.alias.value.trim(), term: choice.term.value.trim()};
                });
                if (corrections.some(function (pair) { return !pair.alias || !pair.term || pair.alias === pair.term; })) {
                    error.textContent = "기존 표기와 정답 표기를 서로 다르게 입력해 주세요.";
                    error.hidden = false;
                    return;
                }
                updateControls(true);
                error.hidden = true;
                try {
                    var result = await App.apiPost("/vocabulary/corrections", {corrections: corrections});
                    if (self._destroyed || !form.isConnected) return;
                    self._vocabularyReviews.delete(index);
                    var status = document.createElement("p");
                    status.setAttribute("role", "status");
                    status.className = "vocabulary-review-result";
                    status.tabIndex = -1;
                    form.replaceWith(status);
                    var messages = [];
                    if (result.added) messages.push("용어집에 " + result.added + "개 추가됨");
                    if (result.existing) messages.push("이미 등록된 표현 " + result.existing + "개");
                    status.textContent = messages.join(" · ");
                    status.focus({preventScroll: true});
                } catch (err) {
                    if (self._destroyed || !form.isConnected) return;
                    error.textContent = "용어집 등록 실패 · " + err.message;
                    error.hidden = false;
                    updateControls(false);
                    submit.focus({preventScroll: true});
                }
            });
            var utterance = self._allUtterances[index];
            self._vocabularyReviews.set(index, {text: utterance.text, start: utterance.start, speaker: utterance.speaker, element: form});
            row.querySelector(".utterance-content").appendChild(form);
            if (returnFocus !== false) choices[0].check.focus({preventScroll: true});
        };

        /**
         * "모두 바꾸기" 모달을 연다.
         */
        ViewerView.prototype._openReplaceModal = function () {
            var self = this;
            if (!self._canEditTranscript()) {
                errorBanner.show("처리 중이거나 읽기 전용인 전사문은 모두 바꾸기를 사용할 수 없습니다.");
                return;
            }
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

        ViewerView.prototype._syncMicEchoVisibility = function () {
            var els = this._els;
            var count = Object.keys(this._micEchoCandidates).length;
            var searching = Boolean(this._currentQuery);
            var folded = this._foldMicEchoes && !searching;
            els.micEchoReview.hidden = !count;
            els.timeline.classList.toggle("fold-mic-echoes", folded);
            els.micEchoToggle.setAttribute("aria-expanded", String(!folded));
            els.micEchoToggle.disabled = searching;
            els.micEchoToggle.textContent = searching ? "검색 중 전체 표시" :
                "중복 후보 " + count + "개 " + (folded ? "펼치기" : "접기");
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
                if (self._foldMicEchoes && closestEl.classList.contains("mic-echo-candidate")) {
                    self._foldMicEchoes = false;
                    self._syncMicEchoVisibility();
                }
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
                if (self._destroyed) return;
                var previousStatus = self._lastMeetingData && self._lastMeetingData.status;
                var active = ["queued", "transcribing", "diarizing", "merging", "embedding"].indexOf(data.status) !== -1;
                if (self._meetingInfoPollTimer) clearTimeout(self._meetingInfoPollTimer);
                self._meetingInfoPollTimer = null;
                if (active) {
                    self._meetingInfoPollTimer = setTimeout(function () { self._loadMeetingInfo(); }, 3000);
                    self._timers.push(self._meetingInfoPollTimer);
                }
                if (previousStatus && previousStatus !== data.status && (data.status === "completed" || data.status === "failed")) {
                    self._loadTranscript({ silent: true });
                    if (data.status === "completed") self._loadSummary();
                }

                els.meetingInfo.style.display = "block";
                self._lastMeetingData = data;
                App.safeText(els.meetingDate, App.meetingDateLabel(data));
                if (!els.meetingTitle.classList.contains("editing")) self._renderMeetingTitle(data);

                els.meetingStatus.className = "viewer-status " + data.status;
                App.safeText(els.meetingStatus, App.getStatusLabel(data));

                var audioLabel = App.getFileName(data.audio_path);
                if (data.audio_expired_at) {
                    audioLabel = "음성 보관 기간 45일 만료" + (data.audio_retention_pending ? " · 정리 대기" : "");
                }
                els.metaFile.innerHTML = Icons.mic + ' <span>' + App.escapeHtml(audioLabel) + '</span>';
                if (data.audio_expired_at) {
                    self._renderMicTranscription();
                    els.timeline.querySelectorAll(".utterance-play-btn").forEach(function (btn) { btn.remove(); });
                }
                if (data.stt_provider) {
                    var processingLocation = data.stt_provider === "openai"
                        ? "OpenAI 서버"
                        : "이 Mac";
                    var modelName = String(data.stt_model || "").split("/").pop();
                    els.metaTranscription.textContent = modelName
                        ? processingLocation + " · " + modelName
                        : processingLocation + "에서 전사";
                    els.metaTranscription.hidden = false;
                } else {
                    els.metaTranscription.hidden = true;
                    els.metaTranscription.textContent = "";
                }

                // 액션 버튼 렌더링 (전사 시작, 재시도, 요약 생성, 삭제)
                // _loadTranscript 완료 후 다시 호출되어 복사/다운로드 버튼이 갱신됨
                self._lastMeetingData = data;
                self._renderTranscriptSourceStatus();
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
            titleSpan.title = displayTitle + " · 클릭하여 제목 편집";
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
                self._renderMeetingTitle(self._lastMeetingData || data);
            };
            var doSave = async function () {
                if (saved || saving) return;
                var next = input.value.trim();
                // 값이 기존과 동일하면 저장 스킵
                if (next === currentTitle) {
                    saved = true;
                    self._renderMeetingTitle(self._lastMeetingData || data);
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

            input.focus();
            input.select();
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
                if (self._destroyed || !self._els.meetingTitle.isConnected) return false;
                self._lastMeetingData = data;
                self._renderMeetingTitle(data);
                if (typeof ListPanel !== "undefined" && ListPanel.loadMeetings) {
                    ListPanel.loadMeetings();
                }
                return true;
            } catch (e) {
                if (self._destroyed) return false;
                errorBanner.show("제목 저장 실패: " + (e.message || String(e)));
                return false;
            }
        };

        /** 원본 녹음 파일이 있는 폴더를 Finder에서 연다. */
        ViewerView.prototype._openAudioFolder = async function () {
            var self = this;
            if (self._destroyed || self._audioFolderPending) return;
            self._audioFolderPending = true;
            self._renderActions(self._lastMeetingData);
            try {
                var result = await App.apiRequest(
                    "/meetings/" + encodeURIComponent(self._meetingId) + "/open-audio-folder",
                    { method: "POST" }
                );
                if (!result.opened && !self._destroyed) {
                    errorBanner.show("자동으로 폴더를 열 수 없습니다. 파일 탐색기에서 다음 경로를 직접 열어 주세요: " + result.path);
                }
            } catch (e) {
                if (!self._destroyed) errorBanner.show("녹음 폴더 열기 실패: " + e.message);
            } finally {
                self._audioFolderPending = false;
                if (!self._destroyed) self._renderActions(self._lastMeetingData);
            }
        };

        /** 로컬 AI 제목 작업을 접수하며 화면 이동 뒤에도 서버에서 계속 처리한다. */
        ViewerView.prototype._enqueueTitle = async function () {
            var self = this;
            if (self._destroyed || !BulkActionBar || BulkActionBar.isTitlePending(self._meetingId)) return;
            try {
                await BulkActionBar.enqueueTitle([self._meetingId]);
            } catch (_) {
                // shell의 작업 상태에서 오류와 재시도 안내를 유지한다.
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
            actionsEl.setAttribute("role", "toolbar");
            actionsEl.setAttribute("aria-label", "회의 작업");
            // 전사·마이크 응답이 늦게 도착해도 사용자가 연 메뉴와 트리거를 유지한다.
            var more = actionsEl.querySelector(".viewer-more-actions");
            if (!more) {
                more = document.createElement("details");
                more.className = "viewer-more-actions";
                more.innerHTML = '<summary>더 보기</summary><div class="viewer-more-panel"></div>';
                more.addEventListener("keydown", function (event) {
                    if (event.key === "Escape") {
                        event.preventDefault(); event.stopPropagation();
                        more.open = false; more.querySelector("summary").focus();
                    }
                });
                actionsEl.appendChild(more);
            }
            var morePanel = more.querySelector(".viewer-more-panel");
            var focusedAction = morePanel.contains(document.activeElement) ? document.activeElement : null;
            actionsEl.querySelectorAll(".viewer-failure-notice, .viewer-action-group").forEach(function (el) {
                el.remove();
            });

            // 부분 전사문이 있어도 실패 원인과 복구 안내를 숨기지 않는다.
            if (data.status === "failed" && !data.audio_expired_at) {
                var failure = document.createElement("div");
                failure.className = "viewer-failure-notice";
                failure.setAttribute("role", "alert");
                var failureTitle = document.createElement("strong");
                failureTitle.textContent = App.getStatusLabel(data);
                failure.appendChild(failureTitle);
                var failureReason = document.createElement("p");
                failureReason.textContent = data.error_message || "오류 내용을 불러오지 못했습니다.";
                failure.appendChild(failureReason);
                var recovery = document.createElement("p");
                var openaiFailure = (!data.failed_step || data.failed_step === "transcribe") && /OpenAI/i.test(data.error_message || "");
                recovery.textContent = openaiFailure
                    ? "이 Mac에서 로컬로 다시 전사할까요? 이 회의만 로컬 모델로 처리하며 " +
                      "음성을 외부로 다시 보내지 않습니다. 기본 모델 설정은 바뀌지 않습니다. " +
                      "기존 전사·요약은 교체됩니다. 로컬 모델 준비가 필요하고 처리 시간이 길어질 수 있습니다. " +
                      "‘실패한 단계부터 다시 시도’는 기존 OpenAI 모델을 사용합니다."
                    : "기존 결과를 유지하려면 ‘실패한 단계부터 다시 시도’를 선택하세요. " +
                      "계속 실패하면 준비 상태에서 모델과 실행 환경을 확인하세요.";
                failure.appendChild(recovery);
                if (data.transcript_available) {
                    recovery.textContent = "전사문은 저장되어 아래에서 바로 확인할 수 있습니다. ‘" +
                        (data.retry_label || "실패한 단계부터 다시 시도") +
                        "’는 저장된 전사 결과를 유지하고 이어서 처리합니다. 모델 로딩 오류가 계속되면 설정에서 교정·요약 모델을 확인하세요.";
                    var transcriptLink = document.createElement("button");
                    transcriptLink.className = "viewer-action-btn";
                    transcriptLink.textContent = "저장된 전사문 보기";
                    transcriptLink.addEventListener("click", function () {
                        var tab = document.querySelector('.tab-btn[data-tab="transcript"]');
                        if (tab) tab.click();
                        var transcript = document.querySelector(".utterance-text");
                        if (transcript) transcript.scrollIntoView({ block: "center" });
                    });
                    failure.appendChild(transcriptLink);
                }
                if (openaiFailure) {
                    var localBtn = document.createElement("button");
                    localBtn.className = "viewer-action-btn local-fallback";
                    localBtn.textContent = "네, 이 회의만 로컬로 전사";
                    localBtn.addEventListener("click", function () {
                        self._reTranscribeMeeting(data.meeting_id, localBtn, "local");
                    });
                    failure.appendChild(localBtn);
                }
                var recoveryLink = document.createElement("a");
                recoveryLink.className = "viewer-action-btn";
                var modelSettings = openaiFailure || data.failed_step === "correct" || data.failed_step === "summarize";
                recoveryLink.href = modelSettings ? "/app/settings" : "/app/setup";
                recoveryLink.textContent = modelSettings ? "모델 설정 열기" : "준비 상태 확인";
                recoveryLink.addEventListener("click", function (event) {
                    event.preventDefault();
                    Router.navigate(modelSettings ? "/app/settings" : "/app/setup");
                });
                failure.appendChild(recoveryLink);
                actionsEl.insertBefore(failure, more);
            }

            function makeGroup(className, label) {
                var group = document.createElement("div");
                group.className = "viewer-action-group " + className;
                group.setAttribute("role", "group");
                group.setAttribute("aria-label", label);
                return group;
            }

            var primaryGroup = makeGroup("primary", "주요 작업");
            var secondaryGroup = makeGroup("secondary", "보조 작업");
            var dangerGroup = makeGroup("danger", "위험 작업");
            actionsEl.insertBefore(primaryGroup, more);
            morePanel.appendChild(secondaryGroup);
            morePanel.appendChild(dangerGroup);
            var micState = self._micTranscription;
            if (!data.audio_expired_at && micState && micState.retry_available === true && !self._micRequestPending &&
                    (micState.status === "complete" || micState.status === "unknown")) {
                var micRetryBtn = document.createElement("button");
                micRetryBtn.type = "button";
                micRetryBtn.className = "viewer-action-btn mic-track-retry";
                micRetryBtn.textContent = "내 마이크만 다시 전사";
                micRetryBtn.title = "내 발화가 빠졌다고 느낄 때 마이크 트랙만 다시 확인합니다. 기존 발화와 편집은 유지합니다.";
                micRetryBtn.addEventListener("click", function () { self._retryMicTranscription(); });
                secondaryGroup.appendChild(micRetryBtn);
            }
            var folderBtn = document.createElement("button");
            folderBtn.type = "button";
            folderBtn.className = "viewer-action-btn open-audio-folder";
            folderBtn.textContent = self._audioFolderPending ? "폴더 여는 중…" : "녹음 폴더 열기";
            folderBtn.disabled = self._audioFolderPending || !data.audio_path;
            folderBtn.title = "원본 녹음 파일이 있는 폴더를 Finder에서 엽니다.";
            folderBtn.addEventListener("click", function () { self._openAudioFolder(); });
            if (!data.audio_expired_at) primaryGroup.appendChild(folderBtn);

            var aiTitleBtn = document.createElement("button");
            aiTitleBtn.type = "button";
            aiTitleBtn.className = "viewer-action-btn ai-title";
            var titlePending = BulkActionBar && BulkActionBar.isTitlePending(self._meetingId);
            aiTitleBtn.textContent = titlePending ? "제목 정리 중…" : "제목 자동 정리";
            var titleBlockedByWork = ["queued", "recording", "transcribing", "diarizing", "merging", "embedding", "processing"].indexOf(data.status) !== -1;
            aiTitleBtn.disabled = titlePending || titleBlockedByWork || !self._allUtterances.length;
            aiTitleBtn.title = titlePending ? "제목 정리 중입니다. 화면을 이동해도 계속되며 작업 내역에서 중단할 수 있습니다." : aiTitleBtn.disabled
                ? (titleBlockedByWork ? "전사 작업 완료 후 제목을 만들 수 있습니다." : "전사문이 있어야 제목을 만들 수 있습니다.")
                : "이 Mac의 AI가 녹음 날짜와 주요 내용을 담은 제목으로 정리합니다. 작업 내역에서 되돌릴 수 있습니다.";
            aiTitleBtn.addEventListener("click", function () { self._enqueueTitle(); });
            primaryGroup.appendChild(aiTitleBtn);

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
                dangerGroup.appendChild(cancelBtn);
            }

            // 녹음 완료 시 전사 시작 버튼
            if (data.status === "recorded" && !data.audio_expired_at) {
                var transcribeBtn = document.createElement("button");
                transcribeBtn.className = "viewer-action-btn transcribe";
                transcribeBtn.innerHTML = Icons.play + ' 전사 시작';
                transcribeBtn.setAttribute("aria-haspopup", "dialog");
                transcribeBtn.title = "이 회의에 사용할 전사 모델을 선택합니다.";
                transcribeBtn.addEventListener("click", function () {
                    self._openStartTranscriptionDialog(transcribeBtn);
                });
                primaryGroup.appendChild(transcribeBtn);
            }

            // 실패 시 이어서 재시도 버튼
            if (data.status === "failed" && !data.audio_expired_at) {
                var retryBtn = document.createElement("button");
                retryBtn.className = "viewer-action-btn retry";
                retryBtn.textContent = "\u21BB " + (data.retry_label || "실패한 단계부터 다시 시도");
                retryBtn.title =
                    "기존 결과와 진행 기록을 유지하고, 실패한 지점부터 다시 처리합니다.";
                retryBtn.setAttribute(
                    "aria-label",
                    data.retry_label || "기존 결과를 유지하고 실패한 단계부터 다시 시도"
                );
                retryBtn.addEventListener("click", function () {
                    self._retryMeeting(data.meeting_id);
                });
                primaryGroup.appendChild(retryBtn);
            }

            // 재전사 버튼 (완료/실패 회의를 처음부터 다시 전사)
            if (!data.audio_expired_at && (data.status === "completed" || data.status === "failed")) {
                var reBtn = document.createElement("button");
                reBtn.className = "viewer-action-btn retranscribe";
                reBtn.innerHTML = "↻ 처음부터 다시 전사";
                reBtn.title =
                    "기존 전사문, 요약, 진행 기록을 삭제하고 오디오부터 새로 처리합니다.";
                reBtn.setAttribute(
                    "aria-label",
                    "기존 전사문과 요약을 삭제하고 처음부터 다시 전사"
                );
                reBtn.addEventListener("click", function () {
                    self._reTranscribeMeeting(data.meeting_id, reBtn);
                });
                dangerGroup.appendChild(reBtn);
            }

            // 기존 결과를 보존하고 로컬/OpenAI 전사 결과를 별도 A/B 작업으로 비교한다.
            if (data.status === "completed" && !data.audio_expired_at) {
                var alternateModelBtn = document.createElement("button");
                alternateModelBtn.className = "viewer-action-btn alternate-transcription";
                alternateModelBtn.textContent = "다른 모델로 텍스트 변환하기…";
                alternateModelBtn.title =
                    "기존 회의록은 유지하고 로컬 모델과 OpenAI 모델의 전사 결과를 비교합니다.";
                alternateModelBtn.setAttribute("aria-haspopup", "dialog");
                alternateModelBtn.addEventListener("click", function () {
                    self._openAlternateTranscriptionDialog(alternateModelBtn);
                });
                secondaryGroup.appendChild(alternateModelBtn);
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
                primaryGroup.appendChild(summarizeBtn);
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
                secondaryGroup.appendChild(abTestBtn);
            }

            // 전사문 복사/다운로드는 draft/처리 중 상태에서도 허용.
            // 모두 바꾸기는 편집 가능한 최종/보정 전사에서만 허용한다.
            if (self._allUtterances && self._allUtterances.length > 0) {
                var copyBtn = document.createElement("button");
                copyBtn.className = "viewer-action-btn copy";
                copyBtn.innerHTML = Icons.copy + ' 전사문 복사';
                copyBtn.setAttribute("aria-label", "전사문을 클립보드로 복사");
                copyBtn.addEventListener("click", function () {
                    self._copyTranscript(copyBtn);
                });
                secondaryGroup.appendChild(copyBtn);

                var downloadBtn = document.createElement("button");
                downloadBtn.className = "viewer-action-btn download-txt";
                downloadBtn.innerHTML = Icons.doc + ' .txt 다운로드';
                downloadBtn.setAttribute("aria-label", "전사문을 텍스트 파일로 다운로드");
                downloadBtn.addEventListener("click", function () {
                    self._downloadTranscript();
                });
                secondaryGroup.appendChild(downloadBtn);

                if (self._canEditTranscript()) {
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
                    secondaryGroup.appendChild(replaceBtn);
                }
            }

            // 삭제 버튼 (완료/실패/녹음완료 시)
            if (data.status === "completed" || data.status === "failed" || data.status === "recorded") {
                var deleteBtn = document.createElement("button");
                deleteBtn.className = "viewer-action-btn delete";
                deleteBtn.textContent = "\u2715 삭제";
                deleteBtn.addEventListener("click", function () {
                    self._deleteMeeting(data.meeting_id);
                });
                dangerGroup.appendChild(deleteBtn);
            }

            [primaryGroup, secondaryGroup, dangerGroup].forEach(function (group) {
                if (group.children.length === 0) {
                    group.remove();
                }
            });
            if (focusedAction) {
                var nextFocus = Array.from(morePanel.querySelectorAll("button")).find(function (button) {
                    return button.textContent === focusedAction.textContent;
                });
                (nextFocus || more.querySelector("summary")).focus({ preventScroll: true });
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

            var sourceLabels = {
                transcribe: "전사 초안",
                merge: "화자 병합본",
                correct: "보정본",
                corrected: "최종 전사문",
            };
            var speakerCountText = self._transcriptSourceStage === "transcribe"
                ? "화자 구분 전"
                : Object.keys(self._speakerLabels).filter(function (s) { return s !== "UNKNOWN"; }).length + "명";

            // 헤더 + 본문
            var header = [
                "회의 ID: " + self._meetingId,
                "추출 일시: " + new Date().toISOString(),
                "전사 상태: " + (sourceLabels[self._transcriptSourceStage] || "전사문"),
                "화자 수: " + speakerCountText,
                "발화 수: " + self._allUtterances.length + "건",
                "─────────────────────────────────────────",
                "",
            ].join("\n");

            var lines = self._allUtterances.map(function (u) {
                var label = App.speakerLabel(u.speaker, self._speakerLabels);
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
         * 녹음 완료된 개별 회의에만 적용할 전사 모델 선택 대화상자를 연다.
         * 전역 기본 설정은 변경하지 않으며 OpenAI는 파일마다 동의를 다시 받는다.
         * @param {HTMLElement} trigger 닫힌 뒤 포커스를 되돌릴 버튼
         */
        ViewerView.prototype._openStartTranscriptionDialog = function (trigger) {
            var self = this;
            if (self._startTranscriptionCleanup) {
                self._startTranscriptionCleanup(false);
            }

            var abortController = new AbortController();
            var dialog = document.createElement("dialog");
            dialog.className = "transcription-model-dialog start-transcription-dialog";
            dialog.setAttribute("role", "dialog");
            dialog.setAttribute("aria-modal", "true");
            dialog.setAttribute("aria-labelledby", "startTranscriptionTitle");
            dialog.setAttribute("aria-describedby", "startTranscriptionDescription");
            dialog.innerHTML = [
                '<div class="transcription-model-dialog-content">',
                '  <header class="transcription-model-dialog-header">',
                '    <div>',
                '      <h2 id="startTranscriptionTitle">이 회의 전사 시작</h2>',
                '      <p id="startTranscriptionDescription">이번 회의에서 사용할 모델을 선택하세요. 이 선택은 이 회의에만 적용되며 기본 전사 모델은 바뀌지 않습니다.</p>',
                '    </div>',
                '    <button type="button" class="transcription-dialog-close" aria-label="닫기">&#x2715;</button>',
                '  </header>',
                '  <div class="transcription-model-choice-list" id="meetingTranscriptionChoices" role="radiogroup" aria-label="이번 회의 전사 모델">',
                '    <div class="transcription-model-status">모델 목록을 불러오는 중…</div>',
                '  </div>',
                '  <div class="external-upload-warning" id="meetingExternalUploadWarning" role="note" hidden>',
                '    <strong>외부 전송 안내</strong>',
                '    <span>OpenAI 모델을 선택하면 이 회의의 음성 파일이 OpenAI 서버로 업로드됩니다.</span>',
                '  </div>',
                '  <label class="external-upload-consent" id="meetingExternalUploadConsentLabel" hidden>',
                '    <input type="checkbox" id="meetingExternalUploadConsent">',
                '    <span>이 음성 파일을 OpenAI 서버로 전송하는 데 동의합니다.</span>',
                '  </label>',
                '  <div class="transcription-missing-key" id="meetingMissingOpenAIKey" hidden>',
                '    <span>OpenAI API 키를 등록하면 이 회의에서 OpenAI 전사를 선택할 수 있습니다.</span>',
                '    <button type="button" class="transcription-settings-link">설정에서 키 등록</button>',
                '  </div>',
                '  <div class="transcription-model-status" id="meetingTranscriptionStatus" role="status" aria-live="polite">모델 목록을 불러오는 중…</div>',
                '  <footer class="transcription-model-dialog-actions">',
                '    <button type="button" class="transcription-dialog-cancel">취소</button>',
                '    <button type="button" class="transcription-dialog-submit" disabled>이 모델로 전사 시작</button>',
                '  </footer>',
                '</div>',
            ].join("\n");

            document.body.appendChild(dialog);
            self._startTranscriptionDialog = dialog;

            var closeBtn = dialog.querySelector(".transcription-dialog-close");
            var cancelBtn = dialog.querySelector(".transcription-dialog-cancel");
            var submitBtn = dialog.querySelector(".transcription-dialog-submit");
            var settingsBtn = dialog.querySelector(".transcription-settings-link");
            var choiceList = dialog.querySelector("#meetingTranscriptionChoices");
            var warningEl = dialog.querySelector("#meetingExternalUploadWarning");
            var consentLabel = dialog.querySelector("#meetingExternalUploadConsentLabel");
            var consentInput = dialog.querySelector("#meetingExternalUploadConsent");
            var missingKeyEl = dialog.querySelector("#meetingMissingOpenAIKey");
            var statusEl = dialog.querySelector("#meetingTranscriptionStatus");
            var catalog = null;
            var requestInFlight = false;

            function displayModelLabel(model) {
                var label = String((model && (model.label || model.id)) || "");
                return label
                    .replace(/^이 Mac에서 처리\s*·\s*/, "")
                    .replace(/^OpenAI 서버에서 처리\s*·\s*/, "")
                    .replace(/\s*·\s*외부 전송$/, "");
            }

            function getSelectedModel() {
                if (!catalog) return null;
                var selected = choiceList.querySelector(
                    'input[name="meetingTranscriptionModel"]:checked'
                );
                if (!selected) return null;
                return (catalog.models || []).find(function (model) {
                    return model.id === selected.value;
                }) || null;
            }

            function setDialogDismissalBlocked(blocked) {
                closeBtn.disabled = blocked;
                cancelBtn.disabled = blocked;
                settingsBtn.disabled = blocked;
                choiceList.querySelectorAll('input[name="meetingTranscriptionModel"]').forEach(
                    function (input) {
                        input.disabled = blocked || input.dataset.available !== "true";
                    }
                );
                consentInput.disabled = blocked;
                if (blocked) {
                    dialog.setAttribute("aria-busy", "true");
                } else {
                    dialog.removeAttribute("aria-busy");
                }
            }

            function updateGate() {
                var selectedModel = getSelectedModel();
                choiceList.querySelectorAll(".transcription-model-choice").forEach(
                    function (choice) {
                        var input = choice.querySelector("input");
                        choice.classList.toggle("selected", !!(input && input.checked));
                    }
                );
                var keyConfigured = !!(
                    catalog && catalog.openai_key && catalog.openai_key.configured
                );
                var external = !!(selectedModel && selectedModel.external_upload);
                warningEl.hidden = !external;
                consentLabel.hidden = !external;
                missingKeyEl.hidden = keyConfigured;
                if (!external) consentInput.checked = false;

                var ready = !!(
                    !requestInFlight &&
                    selectedModel &&
                    selectedModel.available &&
                    (!external || (keyConfigured && consentInput.checked))
                );
                submitBtn.disabled = !ready;

                if (!selectedModel) {
                    statusEl.textContent = "사용할 수 있는 전사 모델을 선택해 주세요.";
                    statusEl.className = "transcription-model-status warning";
                } else if (!selectedModel.available) {
                    statusEl.textContent =
                        selectedModel.unavailable_reason || "선택한 모델을 사용할 수 없습니다.";
                    statusEl.className = "transcription-model-status error";
                } else if (external && !keyConfigured) {
                    statusEl.textContent = "OpenAI API 키를 먼저 등록해 주세요.";
                    statusEl.className = "transcription-model-status warning";
                } else if (external && !consentInput.checked) {
                    statusEl.textContent = "외부 전송 안내를 확인하고 동의해 주세요.";
                    statusEl.className = "transcription-model-status warning";
                } else if (external) {
                    statusEl.textContent = "이 회의만 OpenAI 서버에서 전사합니다.";
                    statusEl.className = "transcription-model-status success";
                } else {
                    statusEl.textContent = "이 회의는 이 Mac에서 전사하며 외부로 전송되지 않습니다.";
                    statusEl.className = "transcription-model-status success";
                }
            }

            var cleanup = function (restoreFocus) {
                if (self._startTranscriptionDialog !== dialog) return;
                abortController.abort();
                dialog.removeEventListener("close", onClose);
                dialog.removeEventListener("cancel", onDialogCancel);
                dialog.removeEventListener("click", onBackdropClick);
                closeBtn.removeEventListener("click", onDismiss);
                cancelBtn.removeEventListener("click", onDismiss);
                settingsBtn.removeEventListener("click", onOpenSettings);
                choiceList.removeEventListener("change", onChoiceChange);
                consentInput.removeEventListener("change", updateGate);
                submitBtn.removeEventListener("click", onSubmit);
                if (dialog.parentNode) dialog.parentNode.removeChild(dialog);
                self._startTranscriptionDialog = null;
                self._startTranscriptionCleanup = null;
                if (restoreFocus) {
                    var focusTarget = trigger && trigger.isConnected ? trigger : null;
                    if (!focusTarget && self._els && self._els.viewerActions) {
                        focusTarget = self._els.viewerActions.querySelector(".transcribe");
                    }
                    if (focusTarget) focusTarget.focus();
                }
            };

            var onClose = function () { cleanup(true); };
            var onDialogCancel = function (event) {
                if (requestInFlight) event.preventDefault();
            };
            var onDismiss = function () {
                if (requestInFlight) return;
                dialog.close();
            };
            var onBackdropClick = function (event) {
                if (!requestInFlight && event.target === dialog) dialog.close();
            };
            var onOpenSettings = function () {
                if (requestInFlight) return;
                cleanup(false);
                Router.navigate("/app/settings?focus=openai");
            };
            var onChoiceChange = function () {
                consentInput.checked = false;
                updateGate();
            };
            var onSubmit = async function () {
                var selectedModel = getSelectedModel();
                var external = !!(selectedModel && selectedModel.external_upload);
                if (
                    requestInFlight ||
                    !selectedModel ||
                    !selectedModel.available ||
                    (external && (
                        !(catalog.openai_key && catalog.openai_key.configured) ||
                        !consentInput.checked
                    ))
                ) {
                    updateGate();
                    return;
                }

                requestInFlight = true;
                setDialogDismissalBlocked(true);
                submitBtn.disabled = true;
                submitBtn.textContent = "전사 요청 중…";
                statusEl.textContent = "선택한 모델로 이 회의를 작업 큐에 넣고 있습니다…";
                statusEl.className = "transcription-model-status";

                try {
                    await self._transcribeMeeting(self._meetingId, {
                        model_id: selectedModel.id,
                        external_upload_confirmed: external,
                    }, abortController.signal);
                    if (self._startTranscriptionDialog !== dialog) return;
                    requestInFlight = false;
                    setDialogDismissalBlocked(false);
                    cleanup(false);
                } catch (err) {
                    if (err && err.name === "AbortError") return;
                    if (self._startTranscriptionDialog !== dialog) return;
                    requestInFlight = false;
                    setDialogDismissalBlocked(false);
                    submitBtn.textContent = "이 모델로 전사 시작";
                    updateGate();
                    statusEl.textContent =
                        "전사 시작 실패: " + (err.message || String(err));
                    statusEl.className = "transcription-model-status error";
                }
            };

            dialog.addEventListener("close", onClose);
            dialog.addEventListener("cancel", onDialogCancel);
            dialog.addEventListener("click", onBackdropClick);
            closeBtn.addEventListener("click", onDismiss);
            cancelBtn.addEventListener("click", onDismiss);
            settingsBtn.addEventListener("click", onOpenSettings);
            choiceList.addEventListener("change", onChoiceChange);
            consentInput.addEventListener("change", updateGate);
            submitBtn.addEventListener("click", onSubmit);
            self._startTranscriptionCleanup = cleanup;

            if (typeof dialog.showModal === "function") {
                dialog.showModal();
            } else {
                dialog.setAttribute("open", "");
            }
            closeBtn.focus();

            App.apiRequest("/transcription-models", { signal: abortController.signal })
                .then(function (data) {
                    if (self._startTranscriptionDialog !== dialog) return;
                    catalog = data || { models: [], openai_key: { configured: false } };
                    var models = Array.isArray(catalog.models) ? catalog.models : [];
                    choiceList.innerHTML = "";

                    models.forEach(function (model) {
                        var choice = document.createElement("label");
                        choice.className = "transcription-model-choice " + model.provider;
                        if (!model.available) {
                            choice.classList.add("unavailable");
                            choice.setAttribute("aria-disabled", "true");
                        }

                        var input = document.createElement("input");
                        input.type = "radio";
                        input.name = "meetingTranscriptionModel";
                        input.value = model.id;
                        input.dataset.available = model.available ? "true" : "false";
                        input.disabled = !model.available;

                        var content = document.createElement("span");
                        content.className = "transcription-model-choice-content";
                        var badge = document.createElement("span");
                        badge.className = "processing-location-badge " +
                            (model.external_upload ? "external" : "local");
                        badge.textContent = model.external_upload
                            ? "OpenAI 서버 · 외부 전송"
                            : "이 Mac에서 처리";
                        var name = document.createElement("strong");
                        name.textContent = displayModelLabel(model);
                        var detail = document.createElement("span");
                        detail.className = "transcription-model-choice-detail";
                        detail.textContent = model.available
                            ? (model.external_upload
                                ? "이 회의의 오디오를 OpenAI에 전송해 전사합니다."
                                : "오디오가 외부로 전송되지 않습니다.")
                            : (model.unavailable_reason || "현재 사용할 수 없습니다.");
                        content.appendChild(badge);
                        content.appendChild(name);
                        content.appendChild(detail);
                        choice.appendChild(input);
                        choice.appendChild(content);
                        choiceList.appendChild(choice);
                    });

                    var meetingProvider = self._lastMeetingData &&
                        self._lastMeetingData.stt_provider;
                    var desiredId = meetingProvider === "openai"
                        ? "openai:gpt-4o-transcribe-diarize"
                        : (meetingProvider === "local" ? "local" : catalog.default_model_id);
                    var desiredInput = Array.from(
                        choiceList.querySelectorAll('input[name="meetingTranscriptionModel"]')
                    ).find(function (input) {
                        return input.value === desiredId && !input.disabled;
                    });
                    var fallbackInput = choiceList.querySelector(
                        'input[name="meetingTranscriptionModel"][data-available="true"]'
                    );
                    if (desiredInput || fallbackInput) {
                        (desiredInput || fallbackInput).checked = true;
                    }
                    updateGate();
                })
                .catch(function (err) {
                    if (err && err.name === "AbortError") return;
                    if (self._startTranscriptionDialog !== dialog) return;
                    catalog = { models: [], openai_key: { configured: false } };
                    choiceList.innerHTML = "";
                    statusEl.textContent =
                        "전사 모델 목록을 불러오지 못했습니다: " +
                        (err.message || String(err));
                    statusEl.className = "transcription-model-status error";
                    submitBtn.disabled = true;
                });
        };

        /**
         * 녹음 완료된 회의의 전사를 시작한다.
         * @param {string} meetingId - 전사할 회의 ID
         * @param {Object} payload - 공개 model_id와 외부 전송 동의
         * @param {AbortSignal} signal - dialog/view 수명에 묶인 요청 취소 신호
         */
        ViewerView.prototype._transcribeMeeting = async function (meetingId, payload, signal) {
            await App.apiRequest(
                "/meetings/" + encodeURIComponent(meetingId) + "/transcribe",
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload || {}),
                    signal: signal,
                }
            );
            // abort 직전 응답이 완료되는 경합에서도 폐기된 뷰를 다시 로드하지 않는다.
            if (signal && signal.aborted) return;
            this._loadMeetingInfo();
            ListPanel.loadMeetings();
            // 홈 대시보드 카드(미전사/처리 대기 카운트) 즉시 갱신.
            document.dispatchEvent(new CustomEvent("recap:dashboard-refresh"));
        };

        /**
         * 실패한 회의를 기존 진행 기록을 유지한 채 다시 큐에 넣는다.
         * @param {string} meetingId - 재시도할 회의 ID
         */
        ViewerView.prototype._retryMeeting = async function (meetingId) {
            try {
                await App.apiPost("/meetings/" + encodeURIComponent(meetingId) + "/retry", {});
                this._loadMeetingInfo();
                ListPanel.loadMeetings();
                // failed → queued 전이로 처리 대기 카운트가 변하므로 카드도 즉시 갱신.
                document.dispatchEvent(new CustomEvent("recap:dashboard-refresh"));
            } catch (e) {
                errorBanner.show("실패한 단계부터 다시 시도 실패: " + e.message);
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
                // queued/처리중 → recorded 전이로 미전사 카운트가 변하므로 카드도 즉시 갱신.
                document.dispatchEvent(new CustomEvent("recap:dashboard-refresh"));
                setTimeout(function () {
                    if (typeof ListPanel !== "undefined" && ListPanel.loadMeetings) {
                        ListPanel.loadMeetings();
                    }
                    document.dispatchEvent(new CustomEvent("recap:dashboard-refresh"));
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
        ViewerView.prototype._reTranscribeMeeting = async function (meetingId, btn, modelId) {
            var confirmation = modelId === "local"
                ? "이 회의를 이 Mac의 로컬 모델로 처음부터 전사할까요?\n" +
                  "기존 전사문·요약·진행 기록은 교체되며 원본 오디오는 유지됩니다.\n" +
                  "음성을 외부로 다시 보내지 않고, 전역 기본 모델 설정도 바꾸지 않습니다."
                :
                "기존 전사문, 요약, 진행 기록을 삭제하고\n" +
                "오디오부터 처음부터 다시 처리합니다.\n\n" +
                "일시적인 오류라면 '실패한 단계부터 다시 시도'를 먼저 선택하세요.\n" +
                "계속하시겠습니까?";
            if (!confirm(confirmation)) {
                return;
            }
            var originalText = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = "처음부터 다시 전사 요청 중...";
            try {
                await App.apiPost(
                    "/meetings/" + encodeURIComponent(meetingId) + "/re-transcribe",
                    modelId === "local" ? { model_id: "local" } : {}
                );
                this._loadMeetingInfo();
                ListPanel.loadMeetings();
            } catch (e) {
                errorBanner.show("처음부터 다시 전사 실패: " + e.message);
                btn.disabled = false;
                btn.innerHTML = originalText;
            }
        };

        /**
         * 현재 회의의 로컬 전사와 OpenAI 전사를 비교하는 비파괴 A/B 대화상자를 연다.
         * @param {HTMLElement} trigger 닫힌 뒤 포커스를 되돌릴 버튼
         */
        ViewerView.prototype._openAlternateTranscriptionDialog = function (trigger) {
            var self = this;
            if (self._alternateTranscriptionCleanup) {
                self._alternateTranscriptionCleanup(false);
            }

            var abortController = new AbortController();
            var dialog = document.createElement("dialog");
            dialog.className = "transcription-model-dialog";
            dialog.setAttribute("role", "dialog");
            dialog.setAttribute("aria-modal", "true");
            dialog.setAttribute("aria-labelledby", "alternateTranscriptionTitle");
            dialog.setAttribute("aria-describedby", "alternateTranscriptionDescription");
            dialog.innerHTML = [
                '<div class="transcription-model-dialog-content">',
                '  <header class="transcription-model-dialog-header">',
                '    <div>',
                '      <h2 id="alternateTranscriptionTitle">다른 모델로 텍스트 변환하기</h2>',
                '      <p id="alternateTranscriptionDescription">기존 회의록을 바꾸지 않고 두 전사 결과를 별도 A/B 테스트로 만듭니다.</p>',
                '    </div>',
                '    <button type="button" class="transcription-dialog-close" aria-label="닫기">&#x2715;</button>',
                '  </header>',
                '  <div class="transcription-comparison-grid">',
                '    <section class="transcription-model-option local" aria-labelledby="alternateLocalTitle">',
                '      <div class="processing-location-badge local">A · 이 Mac에서 처리</div>',
                '      <h3 id="alternateLocalTitle">로컬 기준 모델</h3>',
                '      <p class="transcription-model-name" id="alternateLocalModel">불러오는 중…</p>',
                '      <p class="transcription-model-detail">음성 파일이 외부로 전송되지 않습니다.</p>',
                '    </section>',
                '    <section class="transcription-model-option external" aria-labelledby="alternateOpenAITitle">',
                '      <div class="processing-location-badge external">B · OpenAI 서버</div>',
                '      <label id="alternateOpenAITitle" for="alternateOpenAIModel">OpenAI 전사 모델</label>',
                '      <select id="alternateOpenAIModel" class="transcription-model-select" aria-describedby="alternateTranscriptionStatus" disabled>',
                '        <option value="">불러오는 중…</option>',
                '      </select>',
                '      <p class="transcription-model-detail">선택한 모델로 처리하기 위해 음성 파일을 외부로 전송합니다.</p>',
                '    </section>',
                '  </div>',
                '  <div class="external-upload-warning" role="note">',
                '    <strong>외부 전송 안내</strong>',
                '    <span>OpenAI 모델을 실행하면 이 회의의 음성 파일이 OpenAI 서버로 업로드됩니다. 기존 로컬 전사문과 회의록은 그대로 유지됩니다.</span>',
                '  </div>',
                '  <label class="external-upload-consent">',
                '    <input type="checkbox" id="alternateExternalUploadConsent">',
                '    <span>이 음성 파일을 OpenAI 서버로 전송하는 데 동의합니다.</span>',
                '  </label>',
                '  <div class="transcription-missing-key" id="alternateMissingKey" hidden>',
                '    <span>OpenAI API 키를 먼저 등록해야 합니다.</span>',
                '    <button type="button" class="transcription-settings-link">설정에서 키 등록</button>',
                '  </div>',
                '  <div class="transcription-model-status" id="alternateTranscriptionStatus" role="status" aria-live="polite">모델 목록을 불러오는 중…</div>',
                '  <footer class="transcription-model-dialog-actions">',
                '    <button type="button" class="transcription-dialog-cancel">취소</button>',
                '    <button type="button" class="transcription-dialog-submit" disabled>비교 전사 시작</button>',
                '  </footer>',
                '</div>',
            ].join("\n");

            document.body.appendChild(dialog);
            self._alternateTranscriptionDialog = dialog;

            var closeBtn = dialog.querySelector(".transcription-dialog-close");
            var cancelBtn = dialog.querySelector(".transcription-dialog-cancel");
            var submitBtn = dialog.querySelector(".transcription-dialog-submit");
            var settingsBtn = dialog.querySelector(".transcription-settings-link");
            var localModelEl = dialog.querySelector("#alternateLocalModel");
            var openAISelect = dialog.querySelector("#alternateOpenAIModel");
            var consentInput = dialog.querySelector("#alternateExternalUploadConsent");
            var missingKeyEl = dialog.querySelector("#alternateMissingKey");
            var statusEl = dialog.querySelector("#alternateTranscriptionStatus");
            var catalog = null;
            var localModel = null;
            var requestInFlight = false;

            var cleanup = function (restoreFocus) {
                if (self._alternateTranscriptionDialog !== dialog) return;
                abortController.abort();
                dialog.removeEventListener("close", onClose);
                dialog.removeEventListener("cancel", onDialogCancel);
                dialog.removeEventListener("click", onBackdropClick);
                closeBtn.removeEventListener("click", onDismiss);
                cancelBtn.removeEventListener("click", onDismiss);
                settingsBtn.removeEventListener("click", onOpenSettings);
                openAISelect.removeEventListener("change", updateGate);
                consentInput.removeEventListener("change", updateGate);
                submitBtn.removeEventListener("click", onSubmit);
                if (dialog.parentNode) dialog.parentNode.removeChild(dialog);
                self._alternateTranscriptionDialog = null;
                self._alternateTranscriptionCleanup = null;
                if (restoreFocus) {
                    var focusTarget = trigger && trigger.isConnected ? trigger : null;
                    if (!focusTarget && self._els && self._els.viewerActions) {
                        focusTarget = self._els.viewerActions.querySelector(
                            ".alternate-transcription"
                        );
                    }
                    if (focusTarget) focusTarget.focus();
                }
            };

            function setDialogDismissalBlocked(blocked) {
                closeBtn.disabled = blocked;
                cancelBtn.disabled = blocked;
                settingsBtn.disabled = blocked;
                if (blocked) {
                    dialog.setAttribute("aria-busy", "true");
                } else {
                    dialog.removeAttribute("aria-busy");
                }
            }

            var onClose = function () { cleanup(true); };
            var onDialogCancel = function (event) {
                if (requestInFlight) event.preventDefault();
            };
            var onDismiss = function () {
                if (requestInFlight) return;
                dialog.close();
            };
            var onBackdropClick = function (event) {
                if (!requestInFlight && event.target === dialog) dialog.close();
            };
            var onOpenSettings = function () {
                if (requestInFlight) return;
                cleanup(false);
                Router.navigate("/app/settings?focus=openai");
            };

            function getSelectedOpenAIModel() {
                if (!catalog) return null;
                return (catalog.models || []).find(function (model) {
                    return model.provider === "openai" && model.id === openAISelect.value;
                }) || null;
            }

            function getDisplayModelLabel(model) {
                var label = String((model && (model.label || model.id)) || "");
                return label
                    .replace(/^이 Mac에서 처리\s*·\s*/, "")
                    .replace(/^OpenAI 서버에서 처리\s*·\s*/, "")
                    .replace(/\s*·\s*외부 전송$/, "");
            }

            function updateGate() {
                var openAIKey = catalog && catalog.openai_key;
                var selectedOpenAIModel = getSelectedOpenAIModel();
                var keyConfigured = !!(openAIKey && openAIKey.configured);
                var ready = !!(
                    !requestInFlight &&
                    localModel &&
                    localModel.available &&
                    selectedOpenAIModel &&
                    selectedOpenAIModel.available &&
                    keyConfigured &&
                    consentInput.checked
                );
                submitBtn.disabled = !ready;
                missingKeyEl.hidden = keyConfigured;

                if (!localModel) {
                    statusEl.textContent = "비교할 로컬 모델을 찾을 수 없습니다.";
                    statusEl.className = "transcription-model-status error";
                } else if (!localModel.available) {
                    statusEl.textContent =
                        localModel.unavailable_reason || "로컬 기준 모델을 사용할 수 없습니다.";
                    statusEl.className = "transcription-model-status error";
                } else if (!keyConfigured) {
                    statusEl.textContent = "OpenAI API 키를 등록하면 비교 전사를 시작할 수 있습니다.";
                    statusEl.className = "transcription-model-status warning";
                } else if (!selectedOpenAIModel) {
                    statusEl.textContent = "사용할 수 있는 OpenAI 전사 모델을 선택해 주세요.";
                    statusEl.className = "transcription-model-status warning";
                } else if (!selectedOpenAIModel.available) {
                    statusEl.textContent =
                        selectedOpenAIModel.unavailable_reason || "선택한 OpenAI 모델을 사용할 수 없습니다.";
                    statusEl.className = "transcription-model-status error";
                } else if (!consentInput.checked) {
                    statusEl.textContent = "외부 전송 안내를 확인하고 동의해 주세요.";
                    statusEl.className = "transcription-model-status warning";
                } else {
                    statusEl.textContent = "원본은 유지되며 비교 결과는 별도 A/B 테스트에 저장됩니다.";
                    statusEl.className = "transcription-model-status success";
                }
            }

            var onSubmit = async function () {
                var selectedOpenAIModel = getSelectedOpenAIModel();
                if (
                    requestInFlight ||
                    !localModel ||
                    !selectedOpenAIModel ||
                    !consentInput.checked ||
                    !(catalog.openai_key && catalog.openai_key.configured)
                ) {
                    updateGate();
                    return;
                }

                requestInFlight = true;
                setDialogDismissalBlocked(true);
                openAISelect.disabled = true;
                consentInput.disabled = true;
                submitBtn.disabled = true;
                submitBtn.textContent = "비교 작업 생성 중…";
                statusEl.textContent = "두 전사 모델의 비교 작업을 만들고 있습니다…";
                statusEl.className = "transcription-model-status";

                var payload = {
                    source_meeting_id: self._meetingId,
                    variant_a: {
                        label: getDisplayModelLabel(localModel),
                        model_id: localModel.id,
                        backend: "mlx",
                    },
                    variant_b: {
                        label: getDisplayModelLabel(selectedOpenAIModel),
                        model_id: selectedOpenAIModel.id,
                        backend: "openai",
                    },
                    allow_diarize_rerun: false,
                    external_upload_confirmed: true,
                };

                try {
                    var result = await App.apiRequest("/ab-tests/stt", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(payload),
                        signal: abortController.signal,
                    });
                    if (self._alternateTranscriptionDialog !== dialog) return;
                    if (!result || !result.test_id) {
                        throw new Error("A/B 테스트 ID를 받지 못했습니다.");
                    }
                    var testId = result.test_id;
                    requestInFlight = false;
                    setDialogDismissalBlocked(false);
                    cleanup(false);
                    Router.navigate("/app/ab-test/" + encodeURIComponent(testId));
                } catch (err) {
                    if (err && err.name === "AbortError") return;
                    if (self._alternateTranscriptionDialog !== dialog) return;
                    requestInFlight = false;
                    setDialogDismissalBlocked(false);
                    openAISelect.disabled = false;
                    consentInput.disabled = false;
                    submitBtn.textContent = "비교 전사 시작";
                    updateGate();
                    statusEl.textContent = "비교 전사 시작 실패: " + (err.message || String(err));
                    statusEl.className = "transcription-model-status error";
                }
            };

            dialog.addEventListener("close", onClose);
            dialog.addEventListener("cancel", onDialogCancel);
            dialog.addEventListener("click", onBackdropClick);
            closeBtn.addEventListener("click", onDismiss);
            cancelBtn.addEventListener("click", onDismiss);
            settingsBtn.addEventListener("click", onOpenSettings);
            openAISelect.addEventListener("change", updateGate);
            consentInput.addEventListener("change", updateGate);
            submitBtn.addEventListener("click", onSubmit);
            self._alternateTranscriptionCleanup = cleanup;

            if (typeof dialog.showModal === "function") {
                dialog.showModal();
            } else {
                dialog.setAttribute("open", "");
            }
            closeBtn.focus();

            App.apiRequest("/transcription-models", { signal: abortController.signal })
                .then(function (data) {
                    if (self._alternateTranscriptionDialog !== dialog) return;
                    catalog = data || { models: [], openai_key: { configured: false } };
                    var models = Array.isArray(catalog.models) ? catalog.models : [];
                    localModel = models.find(function (model) {
                        return model.provider === "local" && model.is_default;
                    }) || models.find(function (model) {
                        return model.provider === "local" && model.available;
                    }) || models.find(function (model) {
                        return model.provider === "local";
                    }) || null;
                    localModelEl.textContent = localModel
                        ? getDisplayModelLabel(localModel)
                        : "사용 가능한 로컬 모델 없음";

                    openAISelect.innerHTML = "";
                    var placeholder = document.createElement("option");
                    placeholder.value = "";
                    placeholder.textContent = "OpenAI 모델 선택";
                    placeholder.disabled = true;
                    placeholder.selected = true;
                    openAISelect.appendChild(placeholder);

                    var firstAvailableId = "";
                    var firstOpenAIId = "";
                    models.filter(function (model) {
                        return model.provider === "openai";
                    }).forEach(function (model) {
                        var option = document.createElement("option");
                        option.value = model.id;
                        option.textContent = getDisplayModelLabel(model);
                        if (!firstOpenAIId) firstOpenAIId = model.id;
                        if (!model.available) {
                            option.textContent += " · 사용 불가";
                            option.disabled = true;
                        } else if (!firstAvailableId) {
                            firstAvailableId = model.id;
                        }
                        openAISelect.appendChild(option);
                    });
                    if (firstAvailableId || firstOpenAIId) {
                        openAISelect.value = firstAvailableId || firstOpenAIId;
                    }
                    openAISelect.disabled = !firstAvailableId;
                    updateGate();
                    if (firstAvailableId) openAISelect.focus();
                })
                .catch(function (err) {
                    if (err && err.name === "AbortError") return;
                    if (self._alternateTranscriptionDialog !== dialog) return;
                    localModelEl.textContent = "모델 목록 조회 실패";
                    openAISelect.disabled = true;
                    statusEl.textContent = "모델 목록 조회 실패: " + (err.message || String(err));
                    statusEl.className = "transcription-model-status error";
                });
        };

        /**
         * 단계 진행 이벤트(`step_progress`)를 처리해 라이브 로그 상태를 갱신한다.
         *
         * detail 스키마:
         *   {
         *     meeting_id, job_id, step,
         *     phase: "start" | "complete",
         *     input_size, eta_seconds, elapsed_seconds, anomaly, success
         *   }
         */
        ViewerView.prototype._handleStepProgress = function (detail) {
            var step = detail.step;
            if (!step) return;
            var entry = this._liveLog[step] || {};

            if (detail.phase === "start") {
                entry.status = "running";
                entry.startedAt = Date.now();
                entry.eta = step === "wiki_compile" ? 0 : Number(detail.eta_seconds) || 0;
                entry.anomaly = "normal";
                entry.elapsed = 0;
                entry.inputSize = Number(detail.input_size) || 0;
                // 이전에 완료됐다가 재시도되는 경우도 있으므로 성공 플래그 초기화
            } else if (detail.phase === "complete") {
                entry.status = detail.success === false ? "failed" : "completed";
                entry.elapsed = Number(detail.elapsed_seconds) || entry.elapsed || 0;
                entry.eta = step === "wiki_compile" ? 0 : Number(detail.eta_seconds) || entry.eta || 0;
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
                var hasElapsed = entry.elapsed != null || Boolean(entry.startedAt);
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
                var hasEstimate = key !== "wiki_compile" && entry.eta > 0;
                var anomaly = key === "wiki_compile" ? "normal" : entry.anomaly || "normal";
                if (entry.status === "running" && hasEstimate) {
                    var ratio = elapsed / entry.eta;
                    if (ratio >= 2.5) anomaly = "danger";
                    else if (ratio >= 1.5) anomaly = "warning";
                    else anomaly = "normal";
                }

                // 진행률 바 (ETA가 있을 때만)
                var progressBar = "";
                if (hasEstimate && entry.status === "running") {
                    var pct = Math.min(100, Math.round((elapsed / entry.eta) * 100));
                    progressBar =
                        '<div class="log-progress-bar"><div class="log-progress-fill" ' +
                        'style="width:' + pct + '%"></div></div>';
                }

                // ETA 텍스트
                var etaText = "";
                if (hasEstimate) {
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
                    '    <div class="log-elapsed">' + (hasElapsed ? self._formatElapsed(elapsed) :
                        (entry.status === "failed" ? "경과 미확정" : "진행 중")) + '</div>',
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
                    if (existing && existing.status === "running" &&
                        (s.step !== "wiki_compile" || data.current_step === "wiki_compile")) return;
                    self._liveLog[s.step] = {
                        status: s.success ? "completed" : "failed",
                        elapsed: s.elapsed_known === false ? null : Number(s.elapsed_seconds) || 0,
                        eta: existing ? existing.eta : 0,
                        anomaly: "normal",
                    };
                });
                (data.skipped_steps || []).forEach(function (k) {
                    self._liveLog[k] = { status: "skipped", elapsed: 0, eta: 0, anomaly: "normal" };
                });
                if (data.status === "running" && data.current_step === "wiki_compile") {
                    var wikiEntry = self._liveLog.wiki_compile;
                    if (!wikiEntry || wikiEntry.status !== "running") {
                        // 재진입 시 시작 이벤트 시각을 알 수 없으므로 경과 시간은 추정하지 않는다.
                        self._liveLog.wiki_compile = { status: "running", elapsed: null, eta: 0, anomaly: "normal" };
                    }
                }
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
                setSummaryDraft(meetingId, null);
                meetingDrafts(meetingId).clear();
                persistUtteranceDraft(meetingId, "", null);
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
            await this._requestSummarize();
        };

        ViewerView.prototype._normalizeTranscriptStage = function (stage) {
            if (TRANSCRIPT_STAGE_RANK[stage]) return stage;
            return "corrected";
        };

        ViewerView.prototype._isProcessingStatus = function (status) {
            return {
                queued: true,
                transcribing: true,
                diarizing: true,
                merging: true,
                embedding: true,
            }[status] === true;
        };

        ViewerView.prototype._canEditTranscript = function () {
            var status = this._lastMeetingData && this._lastMeetingData.status;
            return status === "completed" && !this._transcriptReadonly;
        };

        ViewerView.prototype._buildTranscriptSignature = function (stage, readonly, utterances) {
            return [
                stage,
                readonly ? "ro" : "rw",
                utterances.length,
                utterances.map(function (u) {
                    return [
                        u.speaker || "",
                        Number(u.start) || 0,
                        Number(u.end) || 0,
                        u.text || "",
                    ].join("~");
                }).join("^"),
            ].join("|");
        };

        ViewerView.prototype._renderTranscriptSourceStatus = function () {
            var self = this;
            var el = self._els.transcriptSourceStatus;
            if (!el) return;

            var stage = self._transcriptSourceStage;
            var status = self._lastMeetingData && self._lastMeetingData.status;
            var processingLabels = {
                queued: "대기",
                transcribing: "전사",
                diarizing: "화자 구분",
                merging: "병합",
                embedding: "검색 준비",
            };
            var stageLabels = {
                transcribe: "전사 초안",
                merge: "화자 병합본",
                correct: "보정본",
                corrected: "",
            };
            var parts = [];
            var searchLabels = {
                pending: "수정본 검색 반영 대기 중",
                running: "수정본 검색 반영 작업 중",
                failed: "검색 반영 실패 · 설정에서 재색인해 주세요",
                unavailable: "검색 상태 확인 필요",
            };
            if (searchLabels[self._searchStatus]) parts.push(searchLabels[self._searchStatus]);
            if (self._summaryNeedsReview) parts.push("전사문 수정 후 요약 확인 필요");
            if (stageLabels[stage]) {
                parts.push(stageLabels[stage]);
            }
            if (stage === "transcribe") {
                parts.push("화자 구분 전");
            } else if (self._transcriptReadonly) {
                parts.push("읽기 전용");
            }
            if (self._lastMeetingData && self._lastMeetingData.current_step === "wiki_compile") {
                parts.push("Wiki 정리 중");
            } else if (processingLabels[status]) {
                parts.push(processingLabels[status] + " 진행 중");
            }

            el.className = "transcript-source-status stage-" + App.escapeHtml(stage);
            if (parts.length === 0) {
                el.style.display = "none";
                el.textContent = "";
                return;
            }
            el.style.display = "inline-flex";
            el.textContent = parts.join(" · ");
        };

        ViewerView.prototype._applyTranscriptResponse = function (data, options) {
            var self = this;
            var els = self._els;
            options = options || {};
            // 재시도 접수 이전 응답이 새 진행 상태를 덮어쓰지 않도록 전용 상태 조회를 우선한다.
            if (data.mic_transcription && !self._micRequestPending && !self._micRetryObserved) {
                self._applyMicTranscription(data.mic_transcription);
            }

            var stage = self._normalizeTranscriptStage(data.source_stage || "corrected");
            var readonly = typeof data.readonly === "boolean" ? data.readonly : false;
            var utterances = data.utterances || [];
            var currentRank = TRANSCRIPT_STAGE_RANK[self._transcriptSourceStage] || 0;
            var incomingRank = TRANSCRIPT_STAGE_RANK[stage] || 0;

            if (
                !options.force &&
                self._allUtterances &&
                self._allUtterances.length > 0 &&
                incomingRank < currentRank
            ) {
                return true;
            }

            var candidates = data.mic_echo_candidates || {};
            var signature = self._buildTranscriptSignature(stage, readonly, utterances) + JSON.stringify(candidates);
            var speakerSignature = JSON.stringify([
                data.self_speaker_revision, data.self_speaker_id, data.self_speaker_source, data.can_set_self_speaker
            ]);
            var sameTranscript = signature === self._transcriptSignature;
            var speakersChanged = speakerSignature !== self._speakerSignature;
            var onlySpeakersChanged = sameTranscript && speakersChanged;
            self._searchStatus = data.search_status || "legacy";
            self._summaryNeedsReview = Boolean(data.summary_needs_review);
            self._renderTranscriptSourceStatus();
            if (options.silent && sameTranscript && !speakersChanged) {
                return true;
            }

            self._transcriptSourceStage = stage;
            self._micEchoCandidates = candidates;
            self._transcriptReadonly = readonly;
            self._transcriptSignature = signature;
            self._speakerSignature = speakerSignature;
            if (!onlySpeakersChanged) self._allUtterances = utterances;
            self._speakerLabels = Object.assign(
                App.buildSpeakerLabels(self._allUtterances),
                data.speaker_labels && typeof data.speaker_labels === "object" ? data.speaker_labels : {}
            );
            // 구 버전 응답에서는 원문 자체가 '나'인 경우만 본인으로 표시한다.
            var legacySelf = data.self_speaker_id === undefined && utterances.some(function (u) { return u.speaker === "나"; });
            self._selfSpeakerId = legacySelf ? "나" : data.self_speaker_id || null;
            self._selfSpeakerRevision = data.self_speaker_revision;
            self._selfSpeakerSource = data.self_speaker_source;
            self._canSetSelfSpeaker = data.can_set_self_speaker === true;
            if (self._selfSpeakerId) self._speakerLabels[self._selfSpeakerId] = "나";

            if (self._allUtterances.length === 0) {
                return false;
            }

            els.transcriptEmpty.style.display = "none";
            if (els.pipelineProgress) {
                els.pipelineProgress.style.display = "none";
            }

            self._buildSpeakerColorMap(data.speakers || []);
            self._renderSpeakerLegend(data.speakers || []);
            if (onlySpeakersChanged && els.timeline.children.length === utterances.length) {
                utterances.forEach(function (u, index) {
                    self._renderRowSpeaker(els.timeline.children[index], u.speaker, index);
                });
                return true;
            }

            if (stage === "transcribe") {
                els.metaSpeakers.innerHTML = Icons.person + ' <span>화자 구분 전</span>';
            } else {
                els.metaSpeakers.innerHTML =
                    Icons.person +
                    ' <span>화자 ' +
                    App.escapeHtml(String(data.num_speakers || 0)) +
                    '명</span>';
            }
            els.metaUtterances.innerHTML =
                Icons.chat +
                ' <span>발화 ' +
                App.escapeHtml(String(data.total_utterances || self._allUtterances.length || 0)) +
                '건</span>';

            self._renderTranscriptSourceStatus();

            if (self._lastMeetingData) {
                self._renderActions(self._lastMeetingData);
            }

            els.tabNav.style.display = "flex";
            els.searchBar.style.display = "flex";

            if (self._initialQuery && !self._currentQuery) {
                els.searchInput.value = self._initialQuery;
                self._currentQuery = self._initialQuery;
            }

            self._renderTimeline(self._allUtterances, self._currentQuery);

            if (!options.silent && !isNaN(self._initialTimestamp) && self._initialTimestamp >= 0) {
                self._scrollToTimestamp(self._initialTimestamp);
            }

            if (
                self._lastMeetingData &&
                self._isProcessingStatus(self._lastMeetingData.status) &&
                !self._pipelinePollTimer
            ) {
                self._startPipelinePolling();
            }

            return true;
        };

        /**
         * 전사문을 로드한다.
         */
        ViewerView.prototype._loadTranscript = async function (options) {
            var self = this;
            var els = self._els;
            var micEpoch = self._micStatusEpoch;
            var readVersion = self._transcriptReadVersion;
            options = options || {};
            var showLoading = !options.silent;
            if (showLoading) {
                els.transcriptLoading.classList.add("visible");
            }
            if (!options.silent) {
                els.transcriptEmpty.style.display = "none";
            }

            try {
                // 방금 저장한 회의를 다시 열면 이전 뷰의 PUT이 끝난 뒤 파일을 읽는다.
                if (transcriptSaves.has(self._meetingId)) await transcriptSaves.get(self._meetingId);
                if (self._destroyed) return true;
                var data = await App.apiRequest(
                    "/meetings/" + encodeURIComponent(self._meetingId) + "/transcript"
                );
                // 재전사 완료 전에 시작한 응답이 복구된 발화를 다시 숨기지 않는다.
                if (self._destroyed || micEpoch !== self._micStatusEpoch || readVersion !== self._transcriptReadVersion) return true;

                if (!self._applyTranscriptResponse(data, options)) {
                    els.transcriptEmpty.style.display = "block";
                    self._handleMissingTranscript(self._lastMeetingData);
                    return false;
                }
                return true;

            } catch (e) {
                if (self._destroyed || micEpoch !== self._micStatusEpoch || readVersion !== self._transcriptReadVersion) return true;
                if (e.status === 404) {
                    if (!options.silent) {
                        els.transcriptEmpty.style.display = "block";
                        self._handleMissingTranscript(self._lastMeetingData);
                    }
                } else {
                    if (!options.silent) {
                        errorBanner.show("전사문 로드 실패: " + e.message);
                    }
                }
                return false;
            } finally {
                if (showLoading) {
                    els.transcriptLoading.classList.remove("visible");
                }
            }
        };

        /**
         * 전사문이 없을 때 회의 상태에 맞는 빈 화면을 렌더링한다.
         * @param {Object|null} meeting - /meetings/{id} 응답
         */
        ViewerView.prototype._handleMissingTranscript = function (meeting) {
            var self = this;
            var els = self._els;
            var status = meeting && meeting.status ? meeting.status : "";
            var emptyText = document.getElementById("viewerEmptyText");
            var emptySub = document.getElementById("viewerEmptySub");

            function setEmpty(title, subtitle) {
                self._stopPipelinePolling();
                if (emptyText) App.safeText(emptyText, title);
                if (emptySub) App.safeText(emptySub, subtitle);
                els.pipelineProgress.style.display = "none";
                els.pipelineStatus.classList.remove("error");
            }

            if (meeting && meeting.audio_expired_at) {
                setEmpty("저장된 전사문이 없습니다", "녹음 보관 기간 45일이 지나 새로 전사할 수 없습니다.");
                return;
            }
            if (status === "recorded") {
                setEmpty(
                    "전사 시작 대기 중",
                    "아직 전사문이 없습니다. 전사를 시작하면 진행 상태가 여기에 표시됩니다."
                );
                return;
            }

            if (status === "failed") {
                setEmpty(
                    App.getStatusLabel(meeting),
                    meeting.error_message || "오류 내용을 불러오지 못했습니다."
                );
                return;
            }

            if (status === "completed") {
                setEmpty(
                    "전사문을 찾을 수 없습니다",
                    "회의는 완료 상태지만 전사 결과 파일이 없습니다. 처음부터 다시 전사해 주세요."
                );
                return;
            }

            self._startPipelinePolling();
        };

        /**
         * 파이프라인 폴링 타이머를 정리한다.
         */
        ViewerView.prototype._stopPipelinePolling = function () {
            if (this._pipelinePollTimer) {
                clearInterval(this._pipelinePollTimer);
                this._pipelinePollTimer = null;
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

            self._stopPipelinePolling();

            // Wiki는 실제 진행이 관찰된 회의에만 추가한다.
            var pipelineSteps = [
                { key: "convert", label: "오디오 준비" },
                { key: "transcribe", label: "전사" },
                { key: "diarize", label: "화자 구분" },
                { key: "merge", label: "전사 병합" },
                { key: "correct", label: "AI 교정" },
                { key: "summarize", label: "요약" },
                { key: "chunk", label: "검색 준비" },
                { key: "embed", label: "검색 준비" },
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
                merging: "diarize",
                embedding: "embed",
            };

            function renderProgress(currentStep, completedSteps) {
                var visibleSteps = pipelineSteps;
                if (currentStep === "wiki_compile" ||
                    (completedSteps && completedSteps.indexOf("wiki_compile") >= 0) ||
                    self._liveLog.wiki_compile) {
                    visibleSteps = pipelineSteps.concat([{ key: "wiki_compile", label: "Wiki 정리" }]);
                }
                var html = "";
                for (var i = 0; i < visibleSteps.length; i++) {
                    var step = visibleSteps[i];
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
                    if (i < visibleSteps.length - 1) {
                        html += '<div class="pipeline-step-line ' + (state === "done" ? "done" : "") + '"></div>';
                    }
                }
                els.pipelineSteps.innerHTML = html;
            }

            // 초기 렌더링
            els.pipelineProgress.style.display = "block";
            els.pipelineStatus.classList.remove("error");
            renderProgress("", []);
            App.safeText(els.pipelineStatus, "상태 확인 중...");

            // 빈 상태 텍스트 업데이트
            App.safeText(document.getElementById("viewerEmptyText"), "파이프라인 처리 중");
            var subEl = document.getElementById("viewerEmptySub");
            if (subEl) subEl.innerHTML = "완료되면 전사문, 요약, 검색 준비 상태가 자동으로 표시됩니다.";

            async function poll() {
                try {
                    var meeting = await App.apiRequest(
                        "/meetings/" + encodeURIComponent(self._meetingId)
                    );
                    var status = meeting.status || "";

                    // 완료 시: 전사문 다시 로드
                    if (status === "completed") {
                        self._stopPipelinePolling();
                        els.pipelineProgress.style.display = "none";
                        els.transcriptEmpty.style.display = "none";
                        self._loadTranscript({ force: true });
                        self._loadSummary();
                        self._loadMeetingInfo();
                        return;
                    }

                    // 실패 시: 에러 표시
                    if (status === "failed") {
                        self._stopPipelinePolling();
                        App.safeText(els.pipelineStatus, "처리 실패: " + (meeting.error_message || "알 수 없는 오류"));
                        els.pipelineStatus.classList.add("error");
                        App.safeText(document.getElementById("viewerEmptyText"), "전사 처리 실패");
                        if (subEl) App.safeText(subEl, meeting.error_message || "알 수 없는 오류");
                        self._loadMeetingInfo();
                        return;
                    }

                    if (!processingStatuses[status]) {
                        self._stopPipelinePolling();
                        self._handleMissingTranscript(meeting);
                        return;
                    }

                    self._lastMeetingData = meeting;
                    self._renderTranscriptSourceStatus();
                    await self._loadTranscript({ silent: true });

                    // 처리 중: 단계 업데이트
                    var currentStep = meeting.current_step || statusToStep[status] || "";
                    var completedSteps = meeting.completed_steps || [];

                    renderProgress(currentStep, completedSteps);
                    var stepLabel = "";
                    for (var j = 0; j < pipelineSteps.length; j++) {
                        if (pipelineSteps[j].key === currentStep) {
                            stepLabel = pipelineSteps[j].label;
                            break;
                        }
                    }
                    App.safeText(els.pipelineStatus, meeting.status_label ||
                        (currentStep === "wiki_compile" ? "Wiki 정리 중" :
                            (stepLabel ? stepLabel + " 진행 중..." : App.getStatusLabel(meeting))));

                } catch (e) {
                    // 네트워크 오류 등 → 무시하고 계속 폴링
                }
            }

            poll();
            var pollTimer = setInterval(poll, 3000);
            self._pipelinePollTimer = pollTimer;
            self._timers.push(pollTimer);
        };

        /**
         * 회의록을 로드한다.
         */
        ViewerView.prototype._loadSummary = async function () {
            var self = this;
            var els = self._els;
            var version = ++self._summaryLoadVersion;
            els.summaryLoading.classList.add("visible");
            els.summaryEmpty.style.display = "none";

            try {
                // 이동 직전에 누른 저장이 끝난 뒤 최신 본문과 이 창의 초안을 함께 읽는다.
                if (summarySaves.has(self._meetingId)) await summarySaves.get(self._meetingId);
                if (self._destroyed || version !== self._summaryLoadVersion) return;
                var data = await App.apiRequest(
                    "/meetings/" + encodeURIComponent(self._meetingId) + "/summary"
                );
                if (self._destroyed || version !== self._summaryLoadVersion) return;
                if (!data.markdown && !getSummaryDraft(self._meetingId) && !self._refreshSummaryEditor) {
                    els.summaryEmpty.style.display = "block";
                    els.summarizeBtn.style.display = "inline-block";
                    return;
                }

                self._showLoadedSummary(data.markdown || "");

                // 요약 생성 버튼 숨기기
                els.summarizeBtn.style.display = "none";

                // 탭 표시
                els.tabNav.style.display = "flex";

            } catch (e) {
                if (self._destroyed || version !== self._summaryLoadVersion) return;
                if (getSummaryDraft(self._meetingId) || self._refreshSummaryEditor) {
                    self._showLoadedSummary(null);
                    els.tabNav.style.display = "flex";
                } else if (e.status === 404) {
                    els.summaryEmpty.style.display = "block";
                    els.summarizeBtn.style.display = "inline-block";
                } else {
                    console.warn("회의록 로드 실패:", e.message);
                    els.summaryEmpty.style.display = "block";
                }
            } finally {
                if (version === self._summaryLoadVersion) els.summaryLoading.classList.remove("visible");
            }
        };

        ViewerView.prototype._showLoadedSummary = function (markdown) {
            this._currentSummaryMd = markdown;
            if (this._refreshSummaryEditor) {
                this._refreshSummaryEditor();
                return;
            }
            var draft = getSummaryDraft(this._meetingId);
            if (draft) this._beginEditSummary(draft);
            else this._renderSummaryView(markdown);
        };

        /**
         * 요약 뷰 모드 (마크다운 렌더링 + 편집 버튼) 를 그린다.
         * @param {string} markdown
         */
        ViewerView.prototype._renderSummaryView = function (markdown, message) {
            var self = this;
            var els = self._els;

            els.summaryContent.innerHTML = "";

            // 기존 산출물의 파괴적 force 재생성은 same-UID namespace 경쟁 때문에
            // 서버가 fail-closed하므로 UI에도 안전한 편집 동작만 노출한다.
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
            if (message) {
                var status = document.createElement("span");
                status.className = "summary-draft-status";
                status.setAttribute("role", "status");
                status.textContent = message;
                toolbar.appendChild(status);
            }

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
        ViewerView.prototype._beginEditSummary = function (draft) {
            var self = this;
            var els = self._els;
            var id = self._meetingId;
            var base = draft ? draft.base : self._currentSummaryMd;
            var storageAvailable = true;
            var saving = false;
            var saveError = "";

            els.summaryContent.innerHTML = "";

            var editor = document.createElement("div");
            editor.className = "summary-editor";

            var textarea = document.createElement("textarea");
            textarea.className = "summary-textarea";
            textarea.setAttribute("aria-label", "회의 요약 편집");
            textarea.value = draft ? draft.text : base;
            textarea.spellcheck = false;
            editor.appendChild(textarea);

            var latest = document.createElement("details");
            var latestLabel = document.createElement("summary");
            latestLabel.textContent = "현재 저장된 요약 확인";
            var latestText = document.createElement("pre");
            latest.append(latestLabel, latestText);
            editor.appendChild(latest);

            var status = document.createElement("p");
            status.className = "summary-draft-status";
            status.setAttribute("role", "status");
            editor.appendChild(status);

            var toolbar = document.createElement("div");
            toolbar.className = "summary-edit-actions";

            var cancelBtn = document.createElement("button");
            cancelBtn.type = "button";
            cancelBtn.className = "btn-secondary";
            cancelBtn.textContent = "취소";
            cancelBtn.addEventListener("click", function () {
                if (saving) return;
                setSummaryDraft(id, null);
                self._refreshSummaryEditor = null;
                if (self._currentSummaryMd == null) {
                    els.summaryContent.replaceChildren();
                    self._loadSummary();
                } else self._renderSummaryView(self._currentSummaryMd);
            });
            toolbar.appendChild(cancelBtn);

            var saveBtn = document.createElement("button");
            saveBtn.type = "button";
            saveBtn.className = "btn-summary-save";
            function refresh() {
                var conflict = self._currentSummaryMd != null && self._currentSummaryMd !== base;
                latest.hidden = !conflict;
                if (conflict) latestText.textContent = self._currentSummaryMd;
                textarea.readOnly = saving;
                saveBtn.disabled = saving || !textarea.value.length;
                cancelBtn.disabled = saving;
                saveBtn.textContent = saving ? "저장 중…" : conflict ? "확인한 요약을 초안으로 교체" : "저장";
                var message = saveError;
                if (!message && saving) message = "회의 요약을 저장하고 있습니다.";
                if (!message && conflict) message = "다른 곳에서 요약을 수정했습니다. 현재 저장된 요약을 확인한 뒤 초안을 적용하세요.";
                if (!message && self._currentSummaryMd == null) message = "저장된 요약을 불러오지 못했습니다. 초안을 보관하고 있습니다.";
                if (!message && getSummaryDraft(id)) message = storageAvailable
                    ? "이 창에 초안을 보관했습니다. 돌아오면 이어서 편집할 수 있습니다."
                    : "화면을 옮겨도 초안을 유지합니다. 새로고침 전에 저장하세요.";
                if (status.textContent !== message) status.textContent = message;
                status.hidden = !message;
            }
            self._refreshSummaryEditor = refresh;
            textarea.addEventListener("input", function () {
                saveError = "";
                var unchanged = textarea.value === base && base === self._currentSummaryMd;
                storageAvailable = setSummaryDraft(id, unchanged ? null : {base: base, text: textarea.value});
                refresh();
            });
            function save() {
                if (saveBtn.disabled || summarySaves.has(id)) return;
                var text = textarea.value;
                if (text === base && text === self._currentSummaryMd) {
                    cancelBtn.click();
                    return;
                }
                var expected = self._currentSummaryMd == null ? base : self._currentSummaryMd;
                saving = true;
                saveError = "";
                refresh();
                var request = App.apiRequest("/meetings/" + encodeURIComponent(id) + "/summary", {
                    method: "PUT",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({markdown: text, expected_markdown: expected}),
                }).then(function (data) {
                    setSummaryDraft(id, null);
                    if (self._destroyed) return;
                    self._summaryLoadVersion++;
                    els.summaryLoading.classList.remove("visible");
                    self._refreshSummaryEditor = null;
                    self._currentSummaryMd = data.markdown;
                    self._renderSummaryView(data.markdown, "요약을 저장했습니다.");
                }).catch(function (e) {
                    if (self._destroyed) return;
                    saveError = e.status === 412 ? "" : "저장 실패 · " + e.message;
                    if (e.status === 412) self._loadSummary();
                }).finally(function () {
                    summarySaves.delete(id);
                    saving = false;
                    if (!self._destroyed && self._refreshSummaryEditor === refresh) refresh();
                });
                summarySaves.set(id, request);
            }
            saveBtn.addEventListener("click", save);
            textarea.addEventListener("keydown", function (event) {
                if ((event.metaKey || event.ctrlKey) && event.key === "Enter" && !event.isComposing) {
                    event.preventDefault();
                    save();
                }
            });
            toolbar.appendChild(saveBtn);

            editor.appendChild(toolbar);
            els.summaryContent.appendChild(editor);

            refresh();
            if (!draft) textarea.focus();
        };

        /**
         * 뷰를 정리한다.
         */
        ViewerView.prototype.destroy = function () {
            this._destroyed = true;
            if (this._selfSpeakerDialog) this._selfSpeakerDialog.close();
            clearTimeout(this._micPollTimer);
            if (this._startTranscriptionCleanup) {
                this._startTranscriptionCleanup(false);
            }

            // 외부 전사 대화상자의 네트워크 요청·리스너·DOM을 함께 정리한다.
            if (this._alternateTranscriptionCleanup) {
                this._alternateTranscriptionCleanup(false);
            }

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
            this._stopUtterancePlayback();

            // 페이지 타이틀 복원
            document.title = "회의록 · Recap";
        };

        return ViewerView;
    }

    window.MeetingViewerView = {
        create: create,
    };
})();
