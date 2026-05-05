/* =================================================================
 * Recap A/B Test view boundary
 *
 * 목적: A/B 테스트 목록/생성/결과 화면을 SPA 라우터 본문에서 분리한다.
 * 공개 API: window.MeetingAbTestView
 * ================================================================= */
(function () {
    "use strict";

    function create(deps) {
        deps = deps || {};
        var App = deps.App || window.MeetingApp;
        var Router = deps.Router || (window.SPA && window.SPA.Router);
        var errorBanner = deps.errorBanner || { show: function () {} };

        if (!App || !Router) {
            throw new Error("MeetingAbTestView requires App and Router");
        }

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
            self._destroyed = false;
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
                if (self._destroyed) return;
                var tests = data.tests || [];
                self._renderList(tests, listEl);
            } catch (e) {
                if (self._destroyed) return;
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
                if (this._destroyed) return;
                this._loadTests();
            } catch (e) {
                if (this._destroyed) return;
                errorBanner.show("삭제 실패: " + (e.message || "알 수 없는 오류"));
            }
        };

        /**
         * 뷰 정리.
         */
        AbTestListView.prototype.destroy = function () {
            this._destroyed = true;
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
            self._destroyed = false;
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
                if (self._destroyed) return;
                // 전체 회의 목록을 저장해두고 필터링은 _updateSourceDropdown 에서 수행
                self._meetings = data.meetings || [];
            } catch (e) {
                if (self._destroyed) return;
                self._meetings = [];
            }

            try {
                var sttData = await App.apiRequest("/stt-models");
                if (self._destroyed) return;
                self._sttModels = sttData || [];
                // sttData 가 배열이 아닌 경우(객체 래핑) 처리
                if (!Array.isArray(self._sttModels) && self._sttModels.models) {
                    self._sttModels = self._sttModels.models;
                }
            } catch (e) {
                if (self._destroyed) return;
                self._sttModels = [];
            }

            // LLM 프리셋 목록 (로컬 보유 여부 포함)
            try {
                var llmData = await App.apiRequest("/llm-models/available");
                if (self._destroyed) return;
                self._llmPresets = (Array.isArray(llmData) ? llmData : []).map(function (m) {
                    return { label: m.label, id: m.model_id, available: m.available };
                });
            } catch (e) {
                if (self._destroyed) return;
                // API 실패 시 폴백: 빈 목록 (사용자 정의 입력만 가능)
                self._llmPresets = [];
            }

            if (self._destroyed) return;

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
                if (self._destroyed) return;
                var testId = result.test_id;
                if (testId) {
                    Router.navigate("/app/ab-test/" + encodeURIComponent(testId));
                } else {
                    Router.navigate("/app/ab-test");
                }
            } catch (e) {
                if (self._destroyed) return;
                errorBanner.show("테스트 시작 실패: " + (e.message || "알 수 없는 오류"));
                if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = "테스트 시작"; }
            }
        };

        /**
         * 뷰 정리.
         */
        AbTestNewView.prototype.destroy = function () {
            this._destroyed = true;
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
            self._destroyed = false;
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
                    if (self._destroyed) return;
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
                    if (self._destroyed) return;
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
                if (self._destroyed) return;
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
                if (self._destroyed) return;
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
                if (self._destroyed) return;
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
                if (this._destroyed) return;
                this._pollStatus();
            } catch (e) {
                if (this._destroyed) return;
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
                if (self._destroyed) return;
                summaryA = typeof respA === "string" ? respA : (respA.summary || respA.text || JSON.stringify(respA));
            } catch (e) { summaryA = "(로드 실패)"; }

            try {
                var respB = await App.apiRequest(baseUrl + "b/summary");
                if (self._destroyed) return;
                summaryB = typeof respB === "string" ? respB : (respB.summary || respB.text || JSON.stringify(respB));
            } catch (e) { summaryB = "(로드 실패)"; }

            if (self._destroyed) return;

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
            this._destroyed = true;
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

        return {
            ListView: AbTestListView,
            NewView: AbTestNewView,
            ResultView: AbTestResultView,
        };
    }

    window.MeetingAbTestView = {
        create: create,
    };
})();
