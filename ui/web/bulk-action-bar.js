/* =================================================================
 * Recap BulkActionBar boundary
 *
 * 목적: 선택된 회의 일괄 작업 컨트롤러를 SPA shell 에서 분리한다.
 * 공개 API: window.MeetingBulkActionBar
 * ================================================================= */
(function () {
    "use strict";

    function create(deps) {
        deps = deps || {};
        var App = deps.App || window.MeetingApp;
        var ListPanel = deps.ListPanel || window.ListPanel;
        var doc = deps.document || window.document;
        var setTimeoutFn = deps.setTimeout || window.setTimeout.bind(window);
        var requestAnimationFrameFn = deps.requestAnimationFrame ||
            window.requestAnimationFrame.bind(window);

        if (!App || !ListPanel || !doc) {
            throw new Error("MeetingBulkActionBar requires App, ListPanel, and document");
        }

        var _bar = null;
        var _countNum = null;
        var _inFlight = false;
        var _initialized = false;
        var reviewDialog = null;
        var historyTimer = null;
        var ACTIONS = {
            transcribe: "전사만",
            summarize: "교정·요약",
            full: "전사 + 교정·요약",
        };
        var DETAILS = {
            transcribe: "음성 인식·화자분리·병합까지 진행하고 전사문을 저장합니다. 교정·요약은 나중에 따로 실행합니다.",
            summarize: "저장된 전사문을 사용해 교정·요약을 만듭니다. 음성을 다시 인식하지 않습니다.",
            full: "회의마다 전사 → 교정·요약 순서로 처리합니다. 한 회의가 실패해도 나머지 접수된 회의는 계속 진행합니다.",
        };

        function makeElement(tag, text, className) {
            var el = doc.createElement(tag);
            if (text != null) el.textContent = text;
            if (className) el.className = className;
            return el;
        }

        function makeDialog(title) {
            if (historyTimer) clearTimeout(historyTimer);
            historyTimer = null;
            if (reviewDialog) reviewDialog.close();
            var previousFocus = doc.activeElement;
            var dialog = doc.createElement("dialog");
            dialog.className = "batch-review-dialog";
            dialog.setAttribute("aria-label", title);
            dialog.appendChild(makeElement("h2", title));
            var close = makeElement("button", "닫기", "btn-secondary");
            close.type = "button";
            close.addEventListener("click", function () { dialog.close(); });
            dialog.appendChild(close);
            dialog.addEventListener("keydown", function (event) {
                if (event.key === "Escape") {
                    // 확인창 닫기가 뒤쪽 회의 선택 해제까지 전파되지 않게 한다.
                    event.stopPropagation();
                    return;
                }
                if (event.key !== "Tab") return;
                var focusable = Array.from(dialog.querySelectorAll(
                    "button:not(:disabled), input:not(:disabled), a[href], [tabindex='0']"
                )).filter(function (el) { return el.getClientRects().length; });
                var first = focusable[0], last = focusable[focusable.length - 1];
                if (event.shiftKey && doc.activeElement === first) {
                    event.preventDefault(); last.focus();
                } else if (!event.shiftKey && doc.activeElement === last) {
                    event.preventDefault(); first.focus();
                }
            });
            dialog.addEventListener("close", function () {
                dialog.remove();
                if (reviewDialog === dialog) {
                    if (historyTimer) clearTimeout(historyTimer);
                    historyTimer = null;
                    reviewDialog = null;
                    if (previousFocus && previousFocus.isConnected) previousFocus.focus();
                }
            });
            doc.body.appendChild(dialog);
            reviewDialog = dialog;
            dialog.showModal();
            return dialog;
        }

        function renderReceipt(receipt, host) {
            var box = makeElement("section", null, "batch-receipt");
            box.appendChild(makeElement("h3", (ACTIONS[receipt.action] || receipt.action) + " · " + receipt.queued + "건 대기열 등록 · " + receipt.skipped + "건 제외"));
            box.appendChild(makeElement("p", "접수 " + (receipt.created_at || "방금") + " · 요청 ID " + receipt.request_id));
            (receipt.candidates || []).forEach(function (row) {
                var item = makeElement("div", null, "batch-review-row");
                var link = makeElement("a", row.title || row.meeting_id);
                link.href = "/app/viewer/" + encodeURIComponent(row.meeting_id);
                item.appendChild(link);
                var events = (receipt.events || []).filter(function (e) { return e.meeting_id === row.meeting_id; });
                var last = events.length ? events[events.length - 1] : null;
                var status = row.admission === "queued" ? "대기열 등록" : "제외 · " + row.reason;
                if (last) status = last.status_label || App.getStatusLabel(last.status);
                item.appendChild(makeElement("p", status, "batch-item-state"));
                if (last && last.error_message) item.appendChild(makeElement("p", last.error_message));
                box.appendChild(item);
            });
            host.appendChild(box);
        }

        function showHistory() {
            var dialog = makeDialog("일괄 처리 접수 내역");
            var content = makeElement("div");
            dialog.appendChild(content);
            async function refresh() {
                try {
                    var result = await App.apiRequest("/batch-requests");
                    if (!dialog.open) return;
                    content.replaceChildren();
                    if (!result.requests.length) content.appendChild(makeElement("p", "아직 접수 내역이 없습니다."));
                    result.requests.forEach(function (receipt) { renderReceipt(receipt, content); });
                } catch (err) {
                    if (dialog.open) content.textContent = "내역 조회 실패: " + err.message;
                }
                if (dialog.open) historyTimer = setTimeoutFn(refresh, 3000);
            }
            refresh();
        }

        async function openReview(payload) {
            var dialog = makeDialog("일괄 처리 대상 확인");
            dialog.id = "homeBatchConfirmModal";
            var description = makeElement("p", (ACTIONS[payload.action] || payload.action) + " — " + DETAILS[payload.action]);
            dialog.appendChild(description);
            dialog.appendChild(makeElement("p", payload.scope === "recent"
                ? "최근 " + payload.hours + "시간: 녹음 시각이 아닌 앱에 등록된 시각 기준입니다."
                : payload.scope === "selected" ? "목록에서 선택한 회의입니다." : "전체 회의에서 대상을 확인합니다."));
            var status = makeElement("p", "대상을 확인하는 중입니다.");
            status.setAttribute("role", "status");
            dialog.appendChild(status);
            var rowsHost = makeElement("div", null, "batch-review-rows");
            dialog.appendChild(rowsHost);
            var start = makeElement("button", "대기열에 등록", "btn-primary");
            start.id = "homeBatchConfirmStart";
            start.disabled = true;
            dialog.appendChild(start);
            var selected = new Set();
            var rows = [];
            var inFlight = false;
            // 같은 화면에서 통신 오류 후 다시 눌러도 중복 접수하지 않는다.
            var requestId = "batch-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
            function updateCount() {
                var chosen = rows.filter(function (row) { return selected.has(row.meeting_id); });
                var ready = chosen.filter(function (row) { return row.eligible; }).length;
                var blocked = chosen.some(function (row) { return row.blocked; });
                status.textContent = "후보 " + rows.length + "건 · 선택 " + chosen.length + "건 · 실행 가능 " + ready + "건 · 제외 " + (chosen.length - ready) + "건" +
                    (blocked ? " — 검증에 실패한 파일을 선택 해제해야 접수할 수 있습니다." : "") +
                    (chosen.length > 500 ? " — 한 번에 최대 500건까지 선택할 수 있습니다." : "");
                start.disabled = inFlight || ready === 0 || blocked || chosen.length > 500;
                start.textContent = inFlight ? "접수 확인 중…" : ready + "건 대기열에 등록";
            }
            try {
                var preview = await App.apiPost("/meetings/batch/review", payload);
                if (!dialog.open) return;
                rows = preview.candidates || [];
                var selectAll = makeElement("button", "모두 선택", "btn-secondary");
                var selectNone = makeElement("button", "모두 해제", "btn-secondary");
                dialog.insertBefore(selectAll, rowsHost);
                dialog.insertBefore(selectNone, rowsHost);
                function setAll(checked) {
                    rowsHost.querySelectorAll("input[type=checkbox]").forEach(function (cb) {
                        cb.checked = checked;
                        if (checked) selected.add(cb.value); else selected.delete(cb.value);
                    });
                    updateCount();
                }
                selectAll.onclick = function () { setAll(true); };
                selectNone.onclick = function () { setAll(false); };
                rows.forEach(function (row) {
                    selected.add(row.meeting_id);
                    var line = makeElement("div", null, "batch-review-row");
                    var label = makeElement("label");
                    var cb = doc.createElement("input");
                    cb.type = "checkbox"; cb.value = row.meeting_id; cb.checked = true;
                    label.appendChild(cb);
                    label.appendChild(makeElement("span", row.title || row.meeting_id));
                    line.appendChild(label);
                    line.appendChild(makeElement("p", (row.status_label || row.status || "상태 확인 필요") + (row.created_at ? " · 등록 " + row.created_at : "")));
                    line.appendChild(makeElement("p", row.reason || "실행 가능"));
                    cb.addEventListener("change", function () {
                        if (cb.checked) selected.add(row.meeting_id); else selected.delete(row.meeting_id);
                        updateCount();
                    });
                    rowsHost.appendChild(line);
                });
                updateCount();
            } catch (err) {
                if (dialog.open) status.textContent = "대상 확인 실패: " + err.message;
            }
            start.addEventListener("click", async function () {
                if (start.disabled || inFlight) return;
                inFlight = true; updateCount();
                _inFlight = true;
                if (_bar) _bar.setAttribute("data-inflight", "true");
                var submittedIds = Array.from(selected);
                try {
                    var receipt = await App.apiPost("/meetings/batch", {action: payload.action, scope: "selected", meeting_ids: submittedIds, request_id: requestId});
                    if (ListPanel && ListPanel.loadMeetings) ListPanel.loadMeetings();
                    if (ListPanel && ListPanel.clearSelection) ListPanel.clearSelection();
                    doc.dispatchEvent(new CustomEvent("recap:dashboard-refresh"));
                    if (!dialog.open) return;
                    dialog.close();
                    showBulkToast(receipt.queued + "건 대기열 등록 · 접수 내역에서 진행 상황을 확인하세요.", "info");
                    showHistory();
                } catch (err) {
                    if (dialog.open) {
                        status.setAttribute("role", "alert");
                        status.textContent = "접수 확인 실패: " + err.message + " 접수 내역을 확인한 후 다시 시도하세요.";
                        var history = makeElement("button", "접수 내역 확인", "btn-secondary");
                        history.onclick = showHistory;
                        dialog.appendChild(history);
                    }
                } finally {
                    _inFlight = false;
                    if (_bar) _bar.removeAttribute("data-inflight");
                    if (!ListPanel.getSelectedIds().length) _hide();
                }
            });
        }

        /**
         * 일괄 작업 토스트 헬퍼 — 메시지 + role 을 받아 in-flow 토스트 노출.
         * 기존 `.home-status` (homeStatusMessage) 가 있으면 우선 사용 (홈뷰 한정),
         * 없으면 동적으로 `<div role="status|alert">` 를 body 에 임시 부착.
         *
         * level 'info' 는 role="status" + .home-status, level 'error' 는 role="alert".
         */
        function showBulkToast(message, level) {
            var role = level === "error" ? "alert" : "status";
            var msg = String(message == null ? "" : message);

            var statusEl = doc.getElementById("homeStatusMessage");
            if (statusEl) {
                statusEl.setAttribute("role", role);
                if (level === "error") {
                    statusEl.setAttribute("data-level", "error");
                } else {
                    statusEl.removeAttribute("data-level");
                }
                App.safeText(statusEl, msg);
                if (statusEl._bulkClearTimer) clearTimeout(statusEl._bulkClearTimer);
                statusEl._bulkClearTimer = setTimeoutFn(function () {
                    if (statusEl.textContent === msg) {
                        App.safeText(statusEl, "");
                        statusEl.removeAttribute("data-level");
                    }
                }, 5000);
                return;
            }

            var toast = doc.createElement("div");
            toast.className = "bulk-toast" + (level === "error" ? " bulk-toast--error" : "");
            toast.setAttribute("role", role);
            toast.setAttribute("aria-live", level === "error" ? "assertive" : "polite");
            App.safeText(toast, msg);
            doc.body.appendChild(toast);
            setTimeoutFn(function () {
                if (toast.parentNode) toast.parentNode.removeChild(toast);
            }, 5000);
        }

        function _show() {
            if (!_bar) return;
            if (!_bar.hidden && !_bar.classList.contains("is-leaving")) {
                return;
            }
            _bar.hidden = false;
            _bar.classList.add("is-leaving");
            void _bar.offsetWidth;
            requestAnimationFrameFn(function () {
                if (_bar) _bar.classList.remove("is-leaving");
            });
        }

        function _hide() {
            if (!_bar) return;
            if (_inFlight) return;
            _bar.classList.add("is-leaving");
            var bar = _bar;
            setTimeoutFn(function () {
                if (!bar) return;
                bar.hidden = true;
                bar.classList.remove("is-leaving");
            }, 200);
        }

        function _onSelectionChanged(e) {
            if (!_bar) return;
            var detail = e.detail || {};
            var count = detail.count || 0;
            var nearby = doc.getElementById("selectionActions");
            if (nearby) {
                nearby.hidden = count === 0;
                nearby.querySelector("span").textContent = count + "건 선택";
            }
            if (_countNum) App.safeText(_countNum, String(count));
            if (count > 0) {
                _show();
            } else {
                _hide();
            }
        }

        function _executeAction(action) {
            if (_inFlight) return;
            var ids = (ListPanel && ListPanel.getSelectedIds)
                ? ListPanel.getSelectedIds()
                : [];
            if (ids.length === 0) return;
            openReview({action: action, scope: "selected", meeting_ids: ids});
            return;

        }

        function _onClick(e) {
            var t = e.target.closest("[data-action]");
            if (!t || !_bar.contains(t)) return;
            var action = t.getAttribute("data-action");
            if (action === "dismiss") {
                if (_inFlight) return;
                if (ListPanel && ListPanel.clearSelection) {
                    ListPanel.clearSelection();
                }
                return;
            }
            if (action === "transcribe" || action === "summarize" || action === "both") {
                var apiAction = (action === "both") ? "full" : action;
                _executeAction(apiAction);
            }
        }

        function init() {
            if (_initialized) return;
            _bar = doc.getElementById("bulkActionBar");
            if (!_bar) return;
            _countNum = _bar.querySelector(".bulk-action-bar__count-num");
            _bar.addEventListener("click", _onClick);
            doc.addEventListener("recap:selection-changed", _onSelectionChanged);
            var nearby = doc.getElementById("selectionActions");
            if (nearby) nearby.addEventListener("click", function (event) {
                var button = event.target.closest("button[data-batch-action]");
                if (button) _executeAction(button.dataset.batchAction);
            });
            var history = doc.getElementById("batchHistoryButton");
            if (history) history.addEventListener("click", showHistory);
            _initialized = true;
        }

        return {
            init: init,
            showBulkToast: showBulkToast,
            openReview: openReview,
            showHistory: showHistory,
        };
    }

    window.MeetingBulkActionBar = {
        create: create,
    };
})();
