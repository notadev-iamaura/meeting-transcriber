/* =================================================================
 * Recap SettingsView boundary
 *
 * 목적: 설정 셸과 설정 패널들을 SPA 라우터 본문에서 분리한다.
 * 공개 API: window.MeetingSettingsView
 * ================================================================= */
(function () {
    "use strict";

    function create(deps) {
        deps = deps || {};
        var App = deps.App || window.MeetingApp;
        var Router = deps.Router || (window.SPA && window.SPA.Router);
        var errorBanner = deps.errorBanner || { show: function () {} };

        if (!App || !Router) {
            throw new Error("MeetingSettingsView requires App and Router");
        }

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
                '    <button type="button" class="settings-tab" data-tab="reindex" role="tab">검색 인덱스</button>',
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
            } else if (name === "reindex") {
                this._currentPanel = new ReindexSettingsPanel(host);
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


        // =====================================================================
        // ReindexSettingsPanel — RAG 검색 인덱스 백필 패널
        // =====================================================================
        //
        // 기존 회의가 ChromaDB / FTS5 인덱스 없이 completed 처리된 경우를
        // 사용자가 직접 백필할 수 있는 진입점.
        //
        // 카드 1: 현황 요약 (전체 N / 인덱싱됨 M / 누락 K)
        // 카드 2: 일괄 백필 트리거 + WebSocket 진행 상황 progress bar
        // 카드 3: 누락 회의 목록 + 개별 재색인 버튼
        function ReindexSettingsPanel(host) {
            var self = this;
            self._host = host;
            self._status = null;        // 마지막 GET /api/reindex/status 응답
            self._isRunning = false;    // 일괄 백필 진행 중 여부
            self._progress = { processed: 0, total: 0, failed: [] };
            self._currentMeetingId = null;
            self._wsHandler = null;
            self._render();
            self._loadStatus();
            self._bindWebSocket();
        }

        ReindexSettingsPanel.prototype._render = function () {
            this._host.innerHTML = [
                '<div class="reindex-panel">',
                '  <section class="settings-section">',
                '    <h3 class="settings-section-title">인덱싱 현황</h3>',
                '    <p class="settings-section-desc">',
                '      RAG 검색 (벡터 + 키워드) 인덱스에 등록된 회의 수를 표시합니다.',
                '      누락된 회의는 채팅 검색에서 응답 컨텍스트로 사용되지 않습니다.',
                '    </p>',
                '    <div class="reindex-summary" id="reindexSummary" aria-live="polite">',
                '      <div class="reindex-summary-loading">불러오는 중…</div>',
                '    </div>',
                '  </section>',
                '  <section class="settings-section">',
                '    <h3 class="settings-section-title">일괄 백필</h3>',
                '    <p class="settings-section-desc">',
                '      청크가 누락된 모든 회의를 자동으로 재색인합니다. 백그라운드에서',
                '      순차적으로 진행되며 (메모리 보호), 회의록이나 전사문은 변경되지',
                '      않습니다. 창을 닫아도 계속 실행됩니다.',
                '    </p>',
                '    <div class="reindex-batch-controls">',
                '      <button type="button" class="btn btn-primary" id="reindexAllBtn" disabled>',
                '        전체 누락분 백필 시작',
                '      </button>',
                '    </div>',
                '    <div class="reindex-progress" id="reindexProgress" hidden aria-live="polite">',
                '      <div class="reindex-progress-text" id="reindexProgressText"></div>',
                '      <div class="reindex-progress-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100">',
                '        <div class="reindex-progress-bar-fill" id="reindexProgressFill"></div>',
                '      </div>',
                '    </div>',
                '  </section>',
                '  <section class="settings-section">',
                '    <h3 class="settings-section-title">누락 회의 목록</h3>',
                '    <p class="settings-section-desc">',
                '      개별 회의만 즉시 재색인하려면 옆의 "재색인" 버튼을 누르세요.',
                '    </p>',
                '    <div class="reindex-missing-list" id="reindexMissingList" aria-live="polite">',
                '      <div class="reindex-missing-empty">불러오는 중…</div>',
                '    </div>',
                '  </section>',
                '</div>',
            ].join("\n");

            var self = this;
            var btn = document.getElementById("reindexAllBtn");
            if (btn) {
                btn.addEventListener("click", function () {
                    self._startReindexAll();
                });
            }
        };

        ReindexSettingsPanel.prototype._loadStatus = async function () {
            var self = this;
            try {
                var data = await App.apiRequest("/reindex/status");
                self._status = data;
                self._renderSummary(data);
                self._renderMissingList(data.missing_meeting_ids || []);
                var btn = document.getElementById("reindexAllBtn");
                if (btn) {
                    btn.disabled = self._isRunning || data.missing === 0;
                }
            } catch (e) {
                var summary = document.getElementById("reindexSummary");
                if (summary) {
                    summary.innerHTML = '<div class="reindex-summary-error">현황 조회 실패: ' + App.escapeHtml(e.message) + '</div>';
                }
            }
        };

        ReindexSettingsPanel.prototype._renderSummary = function (data) {
            var summary = document.getElementById("reindexSummary");
            if (!summary) return;
            var missingClass = data.missing > 0 ? " reindex-stat-warning" : "";
            summary.innerHTML = [
                '<div class="reindex-stats">',
                '  <div class="reindex-stat">',
                '    <div class="reindex-stat-label">전체 회의</div>',
                '    <div class="reindex-stat-value">' + data.total + '</div>',
                '  </div>',
                '  <div class="reindex-stat">',
                '    <div class="reindex-stat-label">인덱싱됨</div>',
                '    <div class="reindex-stat-value">' + data.indexed + '</div>',
                '  </div>',
                '  <div class="reindex-stat' + missingClass + '">',
                '    <div class="reindex-stat-label">누락</div>',
                '    <div class="reindex-stat-value">' + data.missing + '</div>',
                '  </div>',
                '</div>',
            ].join("\n");
        };

        ReindexSettingsPanel.prototype._renderMissingList = function (ids) {
            var list = document.getElementById("reindexMissingList");
            if (!list) return;
            if (!ids || ids.length === 0) {
                list.innerHTML = '<div class="reindex-missing-empty">누락된 회의가 없습니다 — 모든 회의가 인덱싱되어 있어요.</div>';
                return;
            }
            var self = this;
            var rows = ids.map(function (mid) {
                return [
                    '<div class="reindex-missing-row" data-meeting-id="' + App.escapeHtml(mid) + '">',
                    '  <div class="reindex-missing-id">' + App.escapeHtml(mid) + '</div>',
                    '  <button type="button" class="btn btn-secondary reindex-single-btn" data-meeting-id="' + App.escapeHtml(mid) + '">재색인</button>',
                    '</div>',
                ].join("\n");
            }).join("\n");
            list.innerHTML = rows;

            var btns = list.querySelectorAll(".reindex-single-btn");
            Array.prototype.forEach.call(btns, function (btn) {
                btn.addEventListener("click", function () {
                    var mid = btn.getAttribute("data-meeting-id");
                    self._reindexSingle(mid, btn);
                });
            });
        };

        ReindexSettingsPanel.prototype._reindexSingle = async function (meetingId, btn) {
            if (!meetingId) return;
            if (btn) {
                btn.disabled = true;
                btn.textContent = "처리 중…";
            }
            try {
                await App.apiRequest("/meetings/" + encodeURIComponent(meetingId) + "/reindex", {
                    method: "POST",
                });
                // 성공 시 목록 갱신
                await this._loadStatus();
            } catch (e) {
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = "재시도";
                }
                window.alert("재색인 실패: " + e.message);
            }
        };

        ReindexSettingsPanel.prototype._startReindexAll = async function () {
            var btn = document.getElementById("reindexAllBtn");
            if (btn) btn.disabled = true;
            try {
                var data = await App.apiRequest("/reindex/all", { method: "POST" });
                this._isRunning = true;
                this._progress = { processed: 0, total: data.total || 0, failed: [] };
                this._showProgress();
                this._updateProgress();
            } catch (e) {
                if (btn) btn.disabled = false;
                if (e.status === 409) {
                    window.alert("이미 진행 중인 일괄 백필 작업이 있습니다.");
                } else {
                    window.alert("일괄 백필 시작 실패: " + e.message);
                }
            }
        };

        ReindexSettingsPanel.prototype._showProgress = function () {
            var el = document.getElementById("reindexProgress");
            if (el) el.hidden = false;
        };

        ReindexSettingsPanel.prototype._hideProgress = function () {
            var el = document.getElementById("reindexProgress");
            if (el) el.hidden = true;
        };

        ReindexSettingsPanel.prototype._updateProgress = function () {
            var text = document.getElementById("reindexProgressText");
            var fill = document.getElementById("reindexProgressFill");
            if (!text || !fill) return;
            var p = this._progress;
            var ratio = p.total > 0 ? Math.round((p.processed / p.total) * 100) : 0;
            var failedCount = (p.failed || []).length;
            var failedSuffix = failedCount > 0 ? " (실패 " + failedCount + ")" : "";
            var current = this._currentMeetingId ? " · 현재: " + this._currentMeetingId : "";
            text.textContent = p.processed + " / " + p.total + " 완료 (" + ratio + "%)" + failedSuffix + current;
            fill.style.width = ratio + "%";
            var bar = fill.parentElement;
            if (bar) bar.setAttribute("aria-valuenow", String(ratio));
        };

        ReindexSettingsPanel.prototype._bindWebSocket = function () {
            var self = this;
            self._wsHandler = function (event) {
                var data = (event && event.detail) || {};
                switch (data.phase) {
                    case "all_started":
                        self._isRunning = true;
                        self._progress = {
                            processed: data.processed || 0,
                            total: data.total || 0,
                            failed: [],
                        };
                        self._currentMeetingId = null;
                        self._showProgress();
                        self._updateProgress();
                        break;
                    case "start":
                        self._currentMeetingId = data.meeting_id || null;
                        self._updateProgress();
                        break;
                    case "complete":
                        if (typeof data.processed === "number") {
                            self._progress.processed = data.processed;
                        }
                        if (typeof data.total === "number") {
                            self._progress.total = data.total;
                        }
                        self._currentMeetingId = null;
                        self._updateProgress();
                        break;
                    case "failed":
                        if (data.meeting_id) {
                            self._progress.failed.push(data.meeting_id);
                        }
                        if (typeof data.processed === "number") {
                            self._progress.processed = data.processed;
                        }
                        self._updateProgress();
                        break;
                    case "all_complete":
                        self._isRunning = false;
                        self._currentMeetingId = null;
                        self._updateProgress();
                        // 짧게 보여준 뒤 숨김 + 현황 갱신
                        setTimeout(function () {
                            self._hideProgress();
                            self._loadStatus();
                        }, 1500);
                        break;
                    default:
                        break;
                }
            };
            document.addEventListener("ws:reindex_progress", self._wsHandler);
        };

        ReindexSettingsPanel.prototype.isDirty = function () {
            return false;
        };

        ReindexSettingsPanel.prototype.destroy = function () {
            if (this._wsHandler) {
                document.removeEventListener("ws:reindex_progress", this._wsHandler);
                this._wsHandler = null;
            }
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

        return SettingsView;
    }

    window.MeetingSettingsView = {
        create: create,
    };
})();
