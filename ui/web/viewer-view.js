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
        var Icons = deps.Icons || {};
        var PIPELINE_STEPS = deps.PIPELINE_STEPS || [];
        var errorBanner = deps.errorBanner || { show: function () {} };

        if (!App || !Router || !ListPanel) {
            throw new Error("MeetingViewerView requires App, Router, and ListPanel");
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
            self._allUtterances = [];
            self._currentQuery = "";
            self._currentMatchIndex = -1;
            self._totalMatches = 0;
            self._searchTimeout = null;
            self._pipelinePollTimer = null;

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
        ViewerView.prototype._loadData = async function () {
            await this._loadMeetingInfo();
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

            // 실패 시 이어서 재시도 버튼
            if (data.status === "failed") {
                var retryBtn = document.createElement("button");
                retryBtn.className = "viewer-action-btn retry";
                retryBtn.textContent = "\u21BB 실패한 단계부터 다시 시도";
                retryBtn.title =
                    "기존 결과와 진행 기록을 유지하고, 실패한 지점부터 다시 처리합니다.";
                retryBtn.setAttribute(
                    "aria-label",
                    "기존 결과를 유지하고 실패한 단계부터 다시 시도"
                );
                retryBtn.addEventListener("click", function () {
                    self._retryMeeting(data.meeting_id);
                });
                actionsEl.appendChild(retryBtn);
            }

            // 재전사 버튼 (완료/실패 회의를 처음부터 다시 전사)
            if (data.status === "completed" || data.status === "failed") {
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
                // 홈 대시보드 카드(미전사/처리 대기 카운트) 즉시 갱신.
                document.dispatchEvent(new CustomEvent("recap:dashboard-refresh"));
            } catch (e) {
                errorBanner.show("전사 시작 실패: " + e.message);
                btn.disabled = false;
                btn.textContent = originalText;
            }
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
        ViewerView.prototype._reTranscribeMeeting = async function (meetingId, btn) {
            if (!confirm(
                "기존 전사문, 요약, 진행 기록을 삭제하고\n" +
                "오디오부터 처음부터 다시 처리합니다.\n\n" +
                "일시적인 오류라면 '실패한 단계부터 다시 시도'를 먼저 선택하세요.\n" +
                "계속하시겠습니까?"
            )) {
                return;
            }
            var originalText = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = "처음부터 다시 전사 요청 중...";
            try {
                await App.apiPost(
                    "/meetings/" + encodeURIComponent(meetingId) + "/re-transcribe",
                    {}
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
                    self._handleMissingTranscript(self._lastMeetingData);
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
                    self._handleMissingTranscript(self._lastMeetingData);
                } else {
                    errorBanner.show("전사문 로드 실패: " + e.message);
                }
            } finally {
                els.transcriptLoading.classList.remove("visible");
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

            if (status === "recorded") {
                setEmpty(
                    "전사 시작 대기 중",
                    "아직 전사문이 없습니다. 전사를 시작하면 진행 상태가 여기에 표시됩니다."
                );
                return;
            }

            if (status === "failed") {
                setEmpty(
                    "전사 처리 실패",
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
            els.pipelineStatus.classList.remove("error");
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
                        self._pipelinePollTimer = null;
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
                        self._pipelinePollTimer = null;
                        App.safeText(els.pipelineStatus, "처리 실패: " + (meeting.error_message || "알 수 없는 오류"));
                        els.pipelineStatus.classList.add("error");
                        App.safeText(document.getElementById("viewerEmptyText"), "전사 처리 실패");
                        if (subEl) App.safeText(subEl, meeting.error_message || "알 수 없는 오류");
                        self._loadMeetingInfo();
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

            self._pipelinePollTimer = pollTimer;
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

        return ViewerView;
    }

    window.MeetingViewerView = {
        create: create,
    };
})();
