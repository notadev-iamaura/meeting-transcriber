/* =================================================================
 * Recap EmptyView boundary
 *
 * 목적: 홈/빈 화면을 SPA 라우터 본문에서 분리한다.
 * 공개 API: window.MeetingEmptyView
 * ================================================================= */
(function () {
    "use strict";

    function create(deps) {
        deps = deps || {};
        var App = deps.App || window.MeetingApp;
        var Router = deps.Router || (window.SPA && window.SPA.Router);
        var Icons = deps.Icons || {};
        var showBulkToast = deps.showBulkToast || function () {};

        if (!App || !Router) {
            throw new Error("MeetingEmptyView requires App and Router");
        }

    // =================================================================
    // === Home Dropdowns ([전체 일괄 ▾] / [최근 24시간 ▾], bulk-actions §C) ===
    // =================================================================

    /**
     * EmptyView 가 새로 렌더된 후 호출되어 두 드롭다운을 마운트.
     * - 트리거: aria-expanded 토글, chevron 회전 (CSS), 메뉴 표시
     * - 메뉴 항목: scope/action 선택 → preview 확인 모달 → POST /api/meetings/batch
     * - 키보드: Enter/Space=열기, ↑↓=항목 이동, Enter=선택, Esc=닫기
     * - 외부 클릭 → 닫기 (메뉴 위에서 클릭은 메뉴 내부 핸들러가 처리)
     */
    function _mountHomeDropdowns(owner) {
        var wrappers = document.querySelectorAll(".home-action-dropdown-wrapper");
        if (wrappers.length === 0) return;

        // 메뉴 옵션 단일 진실 — 두 드롭다운 동일.
        // data-option 'both' 가 기본 (aria-checked='true').
        var MENU_OPTIONS = [
            { option: "both", label: "전사+요약 시작", checked: true },
            { option: "transcribe", label: "전사만 시작", checked: false },
            { option: "summarize", label: "요약만 시작", checked: false },
        ];

        var ACTION_LABELS = {
            both: "전사 + 요약",
            transcribe: "전사",
            summarize: "요약",
        };

        var SCOPE_LABELS = {
            "all-bulk": "전체 회의",
            "recent-24h": "최근 24시간",
        };
        var confirmSeq = 0;

        function buildPayload(wrapper, option) {
            var trigger = wrapper.querySelector(".home-action-btn--dropdown");
            if (!trigger) return null;
            var dropdownId = trigger.getAttribute("data-dropdown");
            var apiAction = (option === "both") ? "full" : option;
            var payload = { action: apiAction };
            if (dropdownId === "all-bulk") {
                payload.scope = "all";
            } else if (dropdownId === "recent-24h") {
                payload.scope = "recent";
                payload.hours = 24;
            } else {
                return null;
            }
            return {
                dropdownId: dropdownId,
                option: option,
                payload: payload,
                actionLabel: ACTION_LABELS[option] || option,
                scopeLabel: SCOPE_LABELS[dropdownId] || dropdownId,
            };
        }

        function _populateMenu(menu) {
            // lazy-render — 닫힐 때 비우고 열 때 채움. Playwright strict mode 매칭 우회.
            menu.innerHTML = "";
            MENU_OPTIONS.forEach(function (opt) {
                var btn = document.createElement("button");
                btn.type = "button";
                btn.className = "home-action-dropdown-item";
                btn.setAttribute("role", "menuitemradio");
                btn.setAttribute("aria-checked", opt.checked ? "true" : "false");
                btn.setAttribute("data-option", opt.option);
                btn.setAttribute("tabindex", "-1");
                App.safeText(btn, opt.label);
                menu.appendChild(btn);
            });
        }

        function closeMenu(wrapper) {
            var trigger = wrapper.querySelector(".home-action-btn--dropdown");
            var menu = wrapper.querySelector(".home-action-dropdown");
            if (!trigger || !menu) return;
            trigger.setAttribute("aria-expanded", "false");
            menu.classList.remove("is-open");
            menu.hidden = true;
            // 닫을 때 메뉴 항목 제거 (strict mode 매칭 회피)
            menu.innerHTML = "";
        }

        function closeAll() {
            wrappers.forEach(function (w) { closeMenu(w); });
        }

        function openMenu(wrapper) {
            if (owner && owner._destroyed) return;
            closeAll();
            var trigger = wrapper.querySelector(".home-action-btn--dropdown");
            var menu = wrapper.querySelector(".home-action-dropdown");
            if (!trigger || !menu) return;
            // 열기 직전 항목 채움 (lazy)
            _populateMenu(menu);
            menu.hidden = false;
            // 다음 frame 에 is-open 추가 → fade transition
            requestAnimationFrame(function () {
                if (owner && owner._destroyed) return;
                menu.classList.add("is-open");
            });
            trigger.setAttribute("aria-expanded", "true");
            // 첫 항목으로 포커스 이동
            var first = menu.querySelector("[role='menuitemradio']");
            if (first) first.focus();
        }

        function ensureConfirmDialog() {
            var existing = document.getElementById("homeBatchConfirmModal");
            if (existing) return existing;

            var modal = document.createElement("div");
            modal.id = "homeBatchConfirmModal";
            modal.className = "modal-overlay hidden home-batch-confirm-modal";
            modal.setAttribute("role", "dialog");
            modal.setAttribute("aria-modal", "true");
            modal.setAttribute("aria-labelledby", "homeBatchConfirmTitle");
            modal.innerHTML = [
                '<div class="modal-content home-batch-confirm-content">',
                '  <h3 class="modal-title" id="homeBatchConfirmTitle">일괄 처리를 시작할까요?</h3>',
                '  <div class="home-batch-confirm-body" id="homeBatchConfirmBody" aria-live="polite"></div>',
                '  <div class="modal-error" id="homeBatchConfirmError"></div>',
                '  <div class="modal-actions">',
                '    <button type="button" class="btn-secondary" id="homeBatchConfirmCancel">취소</button>',
                '    <button type="button" class="settings-save-btn" id="homeBatchConfirmStart">시작</button>',
                '  </div>',
                '</div>',
            ].join("\n");
            document.body.appendChild(modal);

            modal.addEventListener("click", function (e) {
                if (e.target === modal) closeConfirmDialog();
            });
            document.addEventListener("keydown", function (e) {
                if (e.key === "Escape" && !modal.classList.contains("hidden")) {
                    closeConfirmDialog();
                }
            });
            modal.querySelector("#homeBatchConfirmCancel").addEventListener("click", closeConfirmDialog);
            modal.querySelector("#homeBatchConfirmStart").addEventListener("click", function () {
                var state = modal._batchConfirmState;
                if (!state || !state.payload) return;
                dispatchPayload(state.payload, modal);
            });

            return modal;
        }

        function closeConfirmDialog() {
            var modal = document.getElementById("homeBatchConfirmModal");
            if (!modal) return;
            modal.classList.add("hidden");
            modal._batchConfirmState = null;
            confirmSeq += 1;
        }

        function renderConfirmDialog(state) {
            var modal = ensureConfirmDialog();
            var body = modal.querySelector("#homeBatchConfirmBody");
            var error = modal.querySelector("#homeBatchConfirmError");
            var start = modal.querySelector("#homeBatchConfirmStart");
            var cancel = modal.querySelector("#homeBatchConfirmCancel");
            modal._batchConfirmState = state;
            error.textContent = "";
            cancel.textContent = state.canStart ? "취소" : "닫기";
            start.disabled = !state.canStart || state.loading;

            if (state.loading) {
                body.innerHTML = [
                    '<p class="modal-message">대상을 확인하는 중입니다.</p>',
                    '<dl class="home-batch-confirm-list">',
                    '  <div><dt>범위</dt><dd></dd></div>',
                    '  <div><dt>작업</dt><dd></dd></div>',
                    '</dl>',
                ].join("\n");
                body.querySelectorAll("dd")[0].textContent = state.scopeLabel;
                body.querySelectorAll("dd")[1].textContent = state.actionLabel;
                start.textContent = "확인 중...";
                modal.classList.remove("hidden");
                cancel.focus();
                return;
            }

            var queued = state.preview && typeof state.preview.queued === "number"
                ? state.preview.queued
                : 0;
            var skipped = state.preview && typeof state.preview.skipped === "number"
                ? state.preview.skipped
                : 0;
            var matched = state.preview && typeof state.preview.matched === "number"
                ? state.preview.matched
                : queued + skipped;

            body.innerHTML = [
                '<dl class="home-batch-confirm-list">',
                '  <div><dt>범위</dt><dd></dd></div>',
                '  <div><dt>작업</dt><dd></dd></div>',
                '  <div><dt>대상</dt><dd></dd></div>',
                '  <div><dt>건너뜀</dt><dd></dd></div>',
                '</dl>',
                '<p class="home-batch-confirm-warning">MacBook 발열과 RAM 사용량이 증가할 수 있습니다.</p>',
            ].join("\n");
            var values = body.querySelectorAll("dd");
            values[0].textContent = state.scopeLabel;
            values[1].textContent = state.actionLabel;
            values[2].textContent = queued + "건" + (matched !== queued ? " / 후보 " + matched + "건" : "");
            values[3].textContent = skipped + "건";

            if (queued === 0) {
                error.textContent = "처리할 회의가 없습니다.";
                start.textContent = "시작";
            } else {
                start.textContent = "시작";
            }
            modal.classList.remove("hidden");
            if (queued > 0) {
                start.focus();
            } else {
                cancel.focus();
            }
        }

        async function confirmOption(wrapper, option) {
            if (owner && owner._destroyed) return;
            var trigger = wrapper.querySelector(".home-action-btn--dropdown");
            if (!trigger) return;
            var state = buildPayload(wrapper, option);
            if (!state) return;
            var seq = confirmSeq + 1;
            confirmSeq = seq;
            renderConfirmDialog(Object.assign({}, state, {
                loading: true,
                canStart: false,
                seq: seq,
            }));

            trigger.disabled = true;
            try {
                var preview = await App.apiPost("/meetings/batch/preview", state.payload);
                if (owner && owner._destroyed) return;
                if (seq !== confirmSeq) return;
                renderConfirmDialog(Object.assign({}, state, {
                    loading: false,
                    canStart: !!(preview && preview.queued > 0),
                    preview: preview,
                    seq: seq,
                }));
            } catch (err) {
                if (owner && owner._destroyed) return;
                if (seq !== confirmSeq) return;
                renderConfirmDialog(Object.assign({}, state, {
                    loading: false,
                    canStart: false,
                    preview: null,
                    seq: seq,
                }));
                var modal = ensureConfirmDialog();
                var error = modal.querySelector("#homeBatchConfirmError");
                error.textContent = "대상 확인 실패: " + (err && err.message ? err.message : "서버 오류");
            } finally {
                if (owner && owner._destroyed) return;
                trigger.disabled = false;
            }
        }

        async function dispatchPayload(payload, modal) {
            if (owner && owner._destroyed) return;
            var start = modal.querySelector("#homeBatchConfirmStart");
            var cancel = modal.querySelector("#homeBatchConfirmCancel");
            start.disabled = true;
            cancel.disabled = true;
            start.textContent = "시작 중...";
            try {
                var resp = await App.apiPost("/meetings/batch", payload);
                if (owner && owner._destroyed) return;
                closeConfirmDialog();
                var queued = (resp && resp.queued != null) ? resp.queued : 0;
                var skipped = (resp && resp.skipped != null) ? resp.skipped : 0;
                var msg = queued + "건 처리"
                    + (skipped > 0 ? ", " + skipped + "건 건너뜀" : "");
                showBulkToast(msg, "info");
            } catch (err) {
                if (owner && owner._destroyed) return;
                var error = modal.querySelector("#homeBatchConfirmError");
                error.textContent = "처리 실패: " + (err && err.message ? err.message : "서버 오류");
                start.disabled = false;
                cancel.disabled = false;
                start.textContent = "시작";
            }
        }

        wrappers.forEach(function (wrapper) {
            var trigger = wrapper.querySelector(".home-action-btn--dropdown");
            var menu = wrapper.querySelector(".home-action-dropdown");
            if (!trigger || !menu) return;

            trigger.addEventListener("click", function (e) {
                e.stopPropagation();
                var open = trigger.getAttribute("aria-expanded") === "true";
                if (open) {
                    closeMenu(wrapper);
                } else {
                    openMenu(wrapper);
                }
            });

            // 트리거 키보드: Enter/Space → 열기 (브라우저 기본 동작이 click 을 발생시키지만
            // ArrowDown 으로 바로 열고 첫 항목 포커스도 가능하도록 명시 처리)
            trigger.addEventListener("keydown", function (e) {
                if (e.key === "ArrowDown") {
                    e.preventDefault();
                    if (trigger.getAttribute("aria-expanded") !== "true") {
                        openMenu(wrapper);
                    }
                }
            });

            menu.addEventListener("click", function (e) {
                var item = e.target.closest("[role='menuitemradio']");
                if (!item || !menu.contains(item)) return;
                e.stopPropagation();
                var option = item.getAttribute("data-option");
                if (!option) return;
                closeMenu(wrapper);
                confirmOption(wrapper, option);
            });

            menu.addEventListener("keydown", function (e) {
                var items = Array.prototype.slice.call(
                    menu.querySelectorAll("[role='menuitemradio']")
                );
                var idx = items.indexOf(document.activeElement);
                if (e.key === "ArrowDown") {
                    e.preventDefault();
                    var next = items[(idx + 1) % items.length];
                    if (next) next.focus();
                } else if (e.key === "ArrowUp") {
                    e.preventDefault();
                    var prev = items[(idx - 1 + items.length) % items.length];
                    if (prev) prev.focus();
                } else if (e.key === "Escape") {
                    e.preventDefault();
                    closeMenu(wrapper);
                    trigger.focus();
                } else if (e.key === "Tab") {
                    closeMenu(wrapper);
                } else if (e.key === "Enter" || e.key === " ") {
                    var current = document.activeElement;
                    if (current && current.getAttribute("role") === "menuitemradio") {
                        e.preventDefault();
                        var option2 = current.getAttribute("data-option");
                        closeMenu(wrapper);
                        if (option2) confirmOption(wrapper, option2);
                    }
                }
            });
        });

        // 외부 클릭 → 모든 드롭다운 닫기
        // (마운트 시점에 한 번만 등록 — 재진입 방지를 위해 데이터 플래그)
        if (!document._bulkDropdownOuterClick) {
            document._bulkDropdownOuterClick = true;
            document.addEventListener("click", function (e) {
                var inWrapper = e.target.closest(".home-action-dropdown-wrapper");
                if (inWrapper) return;
                document.querySelectorAll(".home-action-dropdown-wrapper").forEach(function (w) {
                    var t = w.querySelector(".home-action-btn--dropdown");
                    var m = w.querySelector(".home-action-dropdown");
                    if (!t || !m) return;
                    if (t.getAttribute("aria-expanded") === "true") {
                        t.setAttribute("aria-expanded", "false");
                        m.classList.remove("is-open");
                        m.hidden = true;
                    }
                });
            });
        }
    }

    function _removeHomeBatchConfirmDialog() {
        var modal = document.getElementById("homeBatchConfirmModal");
        if (modal) modal.remove();
    }


    // =================================================================
    // === EmptyView (회의 미선택 초기 상태) ===
    // =================================================================

    /**
     * 회의 목록에서 아무것도 선택하지 않은 초기 상태(홈 뷰).
     * 대시보드 통계 + 액션 버튼(폴더 열기 / 일괄 업로드 / 홈 드롭다운 2종) + 안내 메시지를 포함한다.
     * @constructor
     */
    function EmptyView() {
        // 리소스 모니터는 GlobalResourceBar 가 모든 탭에서 표시하므로 EmptyView 자체에서는 렌더하지 않음.
        var self = this;
        self._destroyed = false;
        self._statsSeq = 0;
        self._folderSeq = 0;
        self._statusTimeouts = [];
        self._statsTimer = null;
        self._dashboardRefreshHandler = function () {
            self._loadStats();
        };
        self._render();
        self._loadStats();
        // 대시보드는 가벼운 카운트 집계라 30 초 폴링 — 대시보드 정확도 vs 부하 절충.
        self._statsTimer = setInterval(function () { self._loadStats(); }, 30000);
        // 업로드/회의 변경 직후 즉시 갱신을 트리거할 수 있게 커스텀 이벤트 구독.
        document.addEventListener("recap:dashboard-refresh", self._dashboardRefreshHandler);
    }

    /**
     * EmptyView DOM을 생성한다.
     * 대시보드 통계 카드 + 액션 버튼 + 안내 메시지.
     */
    EmptyView.prototype._render = function () {
        var contentEl = Router.getContentEl();
        contentEl.innerHTML = "";

        // 통계 카드 4 개 — id 로 _loadStats 가 값을 채운다.
        // 처음에는 "—" 로 placeholder 표시 (스켈레톤 대신 단순 dash).
        var html = [
            '<div class="home-view">',
            '  <section class="home-stats" aria-label="대시보드 통계">',
            '    <div class="home-stat-card">',
            '      <div class="home-stat-label">이번 주</div>',
            '      <div class="home-stat-value" id="homeStatThisWeek">—</div>',
            '      <div class="home-stat-sub">최근 7 일 회의</div>',
            '    </div>',
            '    <div class="home-stat-card">',
            '      <div class="home-stat-label">전체 회의</div>',
            '      <div class="home-stat-value" id="homeStatTotal">—</div>',
            '      <div class="home-stat-sub">누적 등록 수</div>',
            '    </div>',
            '    <div class="home-stat-card" aria-label="처리 대기 및 미전사 녹음">',
            '      <div class="home-stat-label">대기열</div>',
            '      <div class="home-stat-value" id="homeStatQueue">—</div>',
            '      <div class="home-stat-sub" id="homeStatQueueSub">전사 대기 중</div>',
            '    </div>',
            '    <div class="home-stat-card">',
            '      <div class="home-stat-label">진행 중</div>',
            '      <div class="home-stat-value" id="homeStatActive">—</div>',
            '      <div class="home-stat-sub">현재 처리 중</div>',
            '    </div>',
            '  </section>',
            '',
            '  <section class="home-onboarding" id="homeOnboarding" aria-label="첫 회의 시작">',
            '    <div class="home-onboarding-copy">',
            '      <h2>첫 회의를 만들어 보세요</h2>',
            '      <p>녹음하거나 기존 오디오를 가져오면 전사·요약·검색 준비가 순서대로 진행됩니다.</p>',
            '    </div>',
            '    <div class="home-onboarding-actions">',
            '      <button class="home-primary-action" id="homeActionStartRecording" type="button">',
            '        <span>녹음 시작</span>',
            '      </button>',
            '      <button class="home-action-btn" id="homeActionImportPrimary" type="button">',
            '        <span>오디오 가져오기</span>',
            '      </button>',
            '    </div>',
            '    <ul class="home-readiness-list" aria-label="처리 준비 상태">',
            '      <li><span class="home-readiness-dot"></span>로컬 저장소에 회의 데이터를 보관합니다.</li>',
            '      <li><span class="home-readiness-dot"></span>모델과 오디오 권한은 설정에서 확인할 수 있습니다.</li>',
            '      <li><span class="home-readiness-dot"></span>완료 후 전사문, 요약, 검색 준비 상태를 확인합니다.</li>',
            '    </ul>',
            '  </section>',
            '',
            '  <section class="home-actions" aria-label="홈 빠른 작업">',
            '    <button class="home-action-btn" id="homeActionOpenFolder" type="button">',
            '      <svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">',
            '        <path d="M3 5a1 1 0 0 1 1-1h4l2 2h6a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5z"/>',
            '      </svg>',
            '      <span>전사 폴더 열기</span>',
            '    </button>',
            '    <button class="home-action-btn home-bulk-action" id="homeActionImport" type="button" hidden>',
            '      <svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">',
            '        <path d="M3 13v3a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-3"/>',
            '        <polyline points="6,7 10,3 14,7"/>',
            '        <line x1="10" y1="3" x2="10" y2="13"/>',
            '      </svg>',
            '      <span>일괄 업로드</span>',
            '    </button>',
            // 홈 드롭다운 (bulk-actions §C) — [전체 일괄 ▾] / [최근 24시간 ▾]
            // wrapper / trigger / menu 구조: aria-haspopup="menu" 트리거 + role="menu" + role="menuitemradio".
            // data-component="bulk-actions" 마커는 axe 한정 스캔용 — 컴포넌트 영역 인식.
            // 메뉴 항목은 lazy-render — 처음 열릴 때만 DOM 에 삽입, 닫힐 때 제거.
            //   사유: Playwright strict mode 에서 `.home-action-dropdown [role='menuitemradio'][data-option='X']`
            //   selector 가 두 메뉴 (각 항목 3 개씩) 를 동시에 매칭하는 것을 방지.
            '    <div class="home-action-dropdown-wrapper home-bulk-action" data-component="bulk-actions" hidden>',
            '      <button class="home-action-btn home-action-btn--dropdown"',
            '              type="button"',
            '              data-dropdown="all-bulk"',
            '              aria-haspopup="menu"',
            '              aria-expanded="false">',
            '        <span>전체 일괄</span>',
            '        <svg class="home-action-btn__chevron" width="10" height="10" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">',
            '          <polyline points="3,5 6,8 9,5"/>',
            '        </svg>',
            '      </button>',
            '      <div class="home-action-dropdown" role="menu" aria-label="전체 일괄 옵션" hidden></div>',
            '    </div>',
            '    <div class="home-action-dropdown-wrapper home-bulk-action" data-component="bulk-actions" hidden>',
            '      <button class="home-action-btn home-action-btn--dropdown"',
            '              type="button"',
            '              data-dropdown="recent-24h"',
            '              aria-haspopup="menu"',
            '              aria-expanded="false">',
            '        <span>최근 24시간</span>',
            '        <svg class="home-action-btn__chevron" width="10" height="10" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">',
            '          <polyline points="3,5 6,8 9,5"/>',
            '        </svg>',
            '      </button>',
            '      <div class="home-action-dropdown" role="menu" aria-label="최근 24시간 옵션" hidden></div>',
            '    </div>',
            '  </section>',
            '',
            '  <div class="home-status" id="homeStatusMessage" role="status" aria-live="polite"></div>',
            '',
            '  <div class="empty-view">',
            '    <div class="empty-view-icon">' + Icons.clipboard + '</div>',
            '    <h2 class="empty-view-title">아직 선택된 회의가 없습니다</h2>',
            '    <p class="empty-view-desc">회의가 생기면 여기에서 전사문과 요약을 바로 열 수 있습니다.</p>',
            '    <div class="empty-view-shortcuts">',
            '      <div class="empty-view-shortcut">\u2318K 검색</div>',
            '    </div>',
            '  </div>',
            '</div>',
        ].join("\n");

        contentEl.innerHTML = html;
        document.title = "회의록 · Recap";

        var self = this;

        // 홈 드롭다운 ([전체 일괄 ▾] / [최근 24시간 ▾]) 핸들러 마운트
        // bulk-actions §C — scope 별 디스패치 + ARIA 토글 + 키보드/외부클릭 닫기
        _mountHomeDropdowns(self);

        // 전사 폴더 열기 — POST /api/system/open-audio-folder
        var openBtn = document.getElementById("homeActionOpenFolder");
        if (openBtn) {
            openBtn.addEventListener("click", function () {
                self._openAudioFolder(openBtn);
            });
        }

        // 일괄 업로드 — 기존 importModal 재사용 (헤더의 importBtn 과 동일한 트리거).
        var importBtn = document.getElementById("homeActionImport");
        if (importBtn) {
            importBtn.addEventListener("click", function () {
                self._openImportModal();
            });
        }

        var importPrimaryBtn = document.getElementById("homeActionImportPrimary");
        if (importPrimaryBtn) {
            importPrimaryBtn.addEventListener("click", function () {
                self._openImportModal();
            });
        }

        var recordingBtn = document.getElementById("homeActionStartRecording");
        if (recordingBtn) {
            recordingBtn.addEventListener("click", function () {
                self._startRecording(recordingBtn);
            });
        }
    };

    /**
     * 대시보드 통계를 비동기로 로드해 카드에 채운다.
     * 실패 시 placeholder 유지 (사용자에게 차단성 에러를 띄우지 않음).
     */
    EmptyView.prototype._loadStats = function () {
        var self = this;
        if (self._destroyed) return;
        var seq = self._statsSeq + 1;
        self._statsSeq = seq;
        App.apiRequest("/dashboard/stats")
            .then(function (data) {
                if (self._destroyed || seq !== self._statsSeq) return;
                if (!data) return;
                _setStatCard("homeStatThisWeek", data.this_week_meetings);
                _setStatCard("homeStatTotal", data.total_meetings);
                self._syncHomeEmptyState(Number(data.total_meetings) || 0);
                // 대기열 카드: 메인 값은 자동 처리 대기(queued), sub 라인은 미전사 녹음(recorded).
                // 미전사가 0 이면 기본 안내 문구로 폴백 — 시각 노이즈 최소화.
                _setStatCard("homeStatQueue", data.queue_pending);
                var subEl = document.getElementById("homeStatQueueSub");
                var card = subEl ? subEl.parentElement : null;
                var pending = Number(data.queue_pending) || 0;
                var untranscribed = Number(data.untranscribed_recordings) || 0;
                if (subEl) {
                    if (untranscribed > 0) {
                        subEl.textContent = "미전사 " + untranscribed;
                    } else {
                        subEl.textContent = "전사 대기 중";
                    }
                }
                if (card) {
                    card.setAttribute(
                        "aria-label",
                        "처리 대기 " + pending + "개, 미전사 녹음 " + untranscribed + "개"
                    );
                }
                _setStatCard("homeStatActive", data.active_processing);
            })
            .catch(function () {
                // 통계 실패는 무시 — 헤더 status indicator 가 별도로 알림 책임.
            });
    };

    EmptyView.prototype._openImportModal = function () {
        if (this._destroyed) return;
        var modal = document.getElementById("importModal");
        if (modal) {
            modal.classList.remove("hidden");
            var dz = document.getElementById("importDropzone");
            if (dz) dz.focus();
        }
    };

    EmptyView.prototype._syncHomeEmptyState = function (totalMeetings) {
        var onboarding = document.getElementById("homeOnboarding");
        var bulkActions = document.querySelectorAll(".home-bulk-action");
        var hasMeetings = totalMeetings > 0;
        if (onboarding) {
            onboarding.hidden = hasMeetings;
        }
        bulkActions.forEach(function (el) {
            el.hidden = !hasMeetings;
        });
    };

    EmptyView.prototype._startRecording = function (btn) {
        var self = this;
        if (self._destroyed || !btn) return;
        var msgEl = document.getElementById("homeStatusMessage");
        var original = btn.textContent;
        btn.disabled = true;
        btn.textContent = "녹음 시작 중...";
        App.apiRequest("/recording/start", { method: "POST" })
            .then(function () {
                if (self._destroyed) return;
                if (msgEl) msgEl.textContent = "녹음이 시작되었습니다. 상단 녹음 상태에서 정지할 수 있습니다.";
            })
            .catch(function (err) {
                if (self._destroyed) return;
                if (msgEl) {
                    msgEl.textContent = "녹음 시작 실패: " + (err && err.message ? err.message : "오디오 입력 상태를 확인해 주세요.");
                }
            })
            .finally(function () {
                if (self._destroyed) return;
                btn.disabled = false;
                btn.textContent = original;
            });
    };

    /**
     * 폴더 열기 액션. 성공 시 toast/badge 메시지로 결과 표시.
     */
    EmptyView.prototype._openAudioFolder = function (btn) {
        var self = this;
        if (self._destroyed) return;
        var seq = self._folderSeq + 1;
        self._folderSeq = seq;
        var msgEl = document.getElementById("homeStatusMessage");
        btn.disabled = true;
        var original = btn.querySelector("span") ? btn.querySelector("span").textContent : "";
        if (btn.querySelector("span")) btn.querySelector("span").textContent = "여는 중…";
        App.apiPost("/system/open-audio-folder", {})
            .then(function (data) {
                if (self._destroyed || seq !== self._folderSeq) return;
                if (msgEl) {
                    msgEl.textContent = data && data.opened
                        ? "Finder 에서 폴더를 열었습니다: " + (data.path || "")
                        : "폴더를 열 수 없습니다. 경로: " + (data && data.path ? data.path : "—");
                }
                var timeoutId = setTimeout(function () {
                    if (self._destroyed || seq !== self._folderSeq) return;
                    if (msgEl) msgEl.textContent = "";
                }, 5000);
                self._statusTimeouts.push(timeoutId);
            })
            .catch(function (err) {
                if (self._destroyed || seq !== self._folderSeq) return;
                if (msgEl) {
                    msgEl.textContent = "폴더 열기 실패: " + (err && err.message ? err.message : "알 수 없는 오류");
                }
            })
            .then(function () {
                if (self._destroyed || seq !== self._folderSeq) return;
                btn.disabled = false;
                if (btn.querySelector("span")) btn.querySelector("span").textContent = original || "전사 폴더 열기";
            });
    };

    /**
     * 뷰를 정리한다. (리소스 모니터는 GlobalResourceBar 가 관리)
     */
    EmptyView.prototype.destroy = function () {
        this._destroyed = true;
        this._statsSeq += 1;
        this._folderSeq += 1;
        if (this._statsTimer) {
            clearInterval(this._statsTimer);
            this._statsTimer = null;
        }
        this._statusTimeouts.forEach(function (timeoutId) {
            clearTimeout(timeoutId);
        });
        this._statusTimeouts = [];
        if (this._dashboardRefreshHandler) {
            document.removeEventListener("recap:dashboard-refresh", this._dashboardRefreshHandler);
            this._dashboardRefreshHandler = null;
        }
        _removeHomeBatchConfirmDialog();
    };

    /**
     * 통계 카드 값을 안전하게 설정한다 (null/undefined 는 "—" 로 표시).
     * 모듈 스코프 헬퍼로 EmptyView 의 인스턴스 메서드 의존성을 줄인다.
     */
    function _setStatCard(elementId, value) {
        var el = document.getElementById(elementId);
        if (!el) return;
        if (value === null || value === undefined) {
            el.textContent = "—";
        } else {
            el.textContent = String(value);
        }
    }


        return EmptyView;
    }

    window.MeetingEmptyView = {
        create: create,
    };
})();
