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
        var workTimer = null;
        var workRefreshing = false;
        var receipts = [];
        var revisions = new Map();
        var titleRequests = new Map();
        var titlePending = new Set();
        var historyMessages = new Map();
        var historyPending = new Set();
        var terminalStates = ["completed", "failed", "skipped", "interrupted", "restored", "cancelled", "blocked"];
        var ACTIONS = {
            title: "제목 자동 정리",
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
            box.appendChild(makeElement("h3", (ACTIONS[receipt.action] || receipt.action) + " · " + receipt.queued + "건 접수"));
            box.appendChild(makeElement("p", App.formatDate ? App.formatDate(receipt.created_at) : (receipt.created_at || "방금")));
            var retryIds = [];
            var canUndo = false;
            var canCancel = false;
            (receipt.candidates || []).forEach(function (row) {
                var item = makeElement("div", null, "batch-review-row");
                var link = makeElement("a", row.title || row.meeting_id);
                link.dataset.historyKey = receipt.request_id + ":" + row.meeting_id + ":link";
                link.href = "/app/viewer/" + encodeURIComponent(row.meeting_id);
                item.appendChild(link);
                var events = (receipt.events || []).filter(function (e) { return e.meeting_id === row.meeting_id; });
                var last = events.length ? events[events.length - 1] : null;
                if (receipt.action === "title" && row.admission === "queued" && (!last || terminalStates.indexOf(last.status) === -1)) canCancel = true;
                var status = row.admission === "queued" ? "대기열 등록" : "제외 · " + row.reason;
                if (last) status = last.status_label || App.getStatusLabel(last.status);
                item.appendChild(makeElement("p", status, "batch-item-state"));
                if (last && last.error_message) item.appendChild(makeElement("p", last.error_message));
                if (receipt.action === "title" && last) {
                    if (["failed", "interrupted", "cancelled"].indexOf(last.status) !== -1) retryIds.push(row.meeting_id);
                    if (last.status === "completed") canUndo = true;
                    if (last.applied_title) item.appendChild(makeElement("p", "변경 제목: " + last.applied_title));
                    if (last.date_source === "audio_mtime") item.appendChild(makeElement("p", "원본 파일 수정일 기준 — 실제 녹취 날짜를 확인해 주세요."));
                    if (last.sampled) item.appendChild(makeElement("p", "긴 녹취 일부 발췌"));
                }
                box.appendChild(item);
            });
            if (canCancel) {
                var cancel = makeElement("button", "중단", "btn-secondary");
                cancel.type = "button";
                cancel.dataset.historyKey = receipt.request_id + ":cancel";
                cancel.disabled = historyPending.has(receipt.request_id);
                cancel.onclick = async function () {
                    if (historyPending.has(receipt.request_id)) return;
                    historyPending.add(receipt.request_id);
                    cancel.disabled = true;
                    try {
                        await App.apiPost("/title-jobs/" + encodeURIComponent(receipt.request_id) + "/cancel", {});
                        showWorkMessage("제목 정리를 중단했습니다. 이미 적용한 제목은 작업 내역에서 되돌릴 수 있습니다.");
                        await refreshWork();
                        cancel.textContent = "중단 완료";
                    } catch (err) { cancel.disabled = false; showWorkMessage("중단 요청 실패: " + err.message, true); }
                    finally { historyPending.delete(receipt.request_id); }
                };
                box.appendChild(cancel);
            }
            if (retryIds.length) {
                var retry = makeElement("button", retryIds.length + "건 다시 시도", "btn-secondary");
                retry.type = "button";
                retry.dataset.historyKey = receipt.request_id + ":retry";
                retry.onclick = async function () {
                    retry.disabled = true;
                    try { await enqueueTitle(retryIds); }
                    catch (_) { retry.disabled = false; }
                };
                box.appendChild(retry);
            }
            if (canUndo) {
                var undo = makeElement("button", "이전 제목으로 되돌리기", "btn-secondary");
                undo.type = "button";
                undo.dataset.historyKey = receipt.request_id + ":undo";
                undo.disabled = historyPending.has(receipt.request_id);
                undo.onclick = async function () {
                    if (historyPending.has(receipt.request_id)) return;
                    historyPending.add(receipt.request_id);
                    undo.disabled = true;
                    try {
                        var result = await App.apiPost("/title-jobs/" + encodeURIComponent(receipt.request_id) + "/undo", {});
                        var message = result.restored + "건 제목을 되돌렸습니다." + (result.skipped ? " 이후 변경된 " + result.skipped + "건은 유지했습니다." : "");
                        historyMessages.set(receipt.request_id, message);
                        box.appendChild(makeElement("p", message, "batch-operation-result"));
                        showWorkMessage(message);
                        await refreshWork();
                        if (ListPanel.loadMeetings) ListPanel.loadMeetings();
                        doc.dispatchEvent(new CustomEvent("recap:titles-updated", {detail: {ids: (receipt.candidates || []).map(function (row) { return row.meeting_id; })}}));
                        undo.textContent = "되돌리기 완료";
                    } catch (err) {
                        showWorkMessage("제목 되돌리기 실패: " + err.message, true);
                        undo.disabled = false;
                    }
                    finally { historyPending.delete(receipt.request_id); }
                };
                box.appendChild(undo);
            }
            if (historyMessages.has(receipt.request_id)) box.appendChild(makeElement("p", historyMessages.get(receipt.request_id), "batch-operation-result"));
            host.appendChild(box);
        }

        function showHistory() {
            var dialog = makeDialog("작업 내역");
            var errorStatus = makeElement("p");
            errorStatus.setAttribute("role", "alert");
            dialog.appendChild(errorStatus);
            var content = makeElement("div");
            dialog.appendChild(content);
            var lastSnapshot = null;
            async function refresh() {
                try {
                    var result = await App.apiRequest("/batch-requests");
                    if (!dialog.open) return;
                    errorStatus.textContent = "";
                    var snapshot = JSON.stringify([result.requests, Array.from(historyMessages), Array.from(historyPending)]);
                    if (snapshot !== lastSnapshot) {
                        var focusKey = doc.activeElement && doc.activeElement.dataset.historyKey;
                        var scrollTop = dialog.scrollTop;
                        content.replaceChildren();
                        if (!result.requests.length) content.appendChild(makeElement("p", "아직 실행한 작업이 없습니다."));
                        result.requests.forEach(function (receipt) { renderReceipt(receipt, content); });
                        if (focusKey) {
                            var target = Array.from(content.querySelectorAll("[data-history-key]")).find(function (el) { return el.dataset.historyKey === focusKey && !el.disabled; });
                            (target || dialog.querySelector("button")).focus({preventScroll: true});
                        }
                        dialog.scrollTop = scrollTop;
                        lastSnapshot = snapshot;
                    }
                } catch (err) {
                    if (dialog.open) errorStatus.textContent = "내역 조회 실패: " + err.message + " 잠시 후 다시 확인합니다.";
                }
                if (dialog.open) historyTimer = setTimeoutFn(refresh, 3000);
            }
            refresh();
        }

        function showWorkMessage(message, error) {
            var box = doc.getElementById("backgroundWorkStatus");
            var text = doc.getElementById("backgroundWorkMessage");
            if (!box || !text) return;
            box.hidden = false;
            text.textContent = message;
            text.setAttribute("role", error ? "alert" : "status");
        }

        function lastEvent(receipt, id) {
            var events = (receipt.events || []).filter(function (event) { return event.meeting_id === id; });
            return events.length ? events[events.length - 1] : null;
        }

        function isTitlePending(id) {
            return titlePending.has(id) || receipts.some(function (receipt) {
                return receipt.action === "title" && (receipt.candidates || []).some(function (row) {
                    var event = lastEvent(receipt, id);
                    return row.meeting_id === id && row.admission === "queued" && (!event || terminalStates.indexOf(event.status) === -1);
                });
            });
        }

        async function refreshWork() {
            if (workRefreshing) return;
            workRefreshing = true;
            try {
                var result = await App.apiRequest("/batch-requests");
                receipts = result.requests || [];
                titleRequests.forEach(function (requestId, key) {
                    if (receipts.some(function (receipt) { return receipt.request_id === requestId; })) titleRequests.delete(key);
                });
                var changed = new Set();
                receipts.forEach(function (receipt) {
                    (receipt.candidates || []).forEach(function (row) {
                        var event = lastEvent(receipt, row.meeting_id);
                        var key = receipt.request_id + ":" + row.meeting_id;
                        var revision = JSON.stringify(event);
                        if (receipt.action === "title" && event && revisions.get(key) !== revision &&
                            (revisions.has(key) || ["completed", "restored"].indexOf(event.status) !== -1)) changed.add(row.meeting_id);
                        revisions.set(key, revision);
                    });
                });
                var active = receipts.filter(function (receipt) {
                    return (receipt.candidates || []).some(function (row) {
                        var event = lastEvent(receipt, row.meeting_id);
                        return row.admission === "queued" && (!event || terminalStates.indexOf(event.status) === -1);
                    });
                });
                var latest = active[0] || receipts[0];
                if (latest) {
                    var rows = (latest.candidates || []).filter(function (row) { return row.admission === "queued"; });
                    var finished = rows.filter(function (row) { var e = lastEvent(latest, row.meeting_id); return e && terminalStates.indexOf(e.status) !== -1; }).length;
                    var failed = rows.filter(function (row) { var e = lastEvent(latest, row.meeting_id); return e && ["failed", "interrupted", "blocked"].indexOf(e.status) !== -1; }).length;
                    var stopped = rows.filter(function (row) { var e = lastEvent(latest, row.meeting_id); return e && e.status === "cancelled"; }).length;
                    var restored = rows.filter(function (row) { var e = lastEvent(latest, row.meeting_id); return e && e.status === "restored"; }).length;
                    var skipped = rows.filter(function (row) { var e = lastEvent(latest, row.meeting_id); return e && e.status === "skipped"; }).length + (latest.skipped || 0);
                    var running = rows.some(function (row) { var e = lastEvent(latest, row.meeting_id); return e && e.status === "running"; });
                    showWorkMessage(rows.length ? (ACTIONS[latest.action] || "작업") + " · " + finished + "/" + rows.length + "건 처리" + (finished < rows.length ? (running ? " · 진행 중" : " · 차례 대기 중") + " — 다른 화면을 이용해도 계속됩니다." : " 종료") + (failed ? " · " + failed + "건 재시도 필요" : "") + (stopped ? " · " + stopped + "건 중단" : "") + (restored ? " · " + restored + "건 되돌림" : "") + (skipped ? " · " + skipped + "건 제외" : "") : "실행할 대상이 없습니다. 작업 내역에서 제외 사유를 확인해 주세요.");
                }
                if (changed.size && ListPanel.loadMeetings) ListPanel.loadMeetings();
                doc.dispatchEvent(new CustomEvent("recap:titles-updated", {detail: {ids: Array.from(changed)}}));
            } catch (err) {
                showWorkMessage("작업 상태를 확인하지 못했습니다. 잠시 후 자동으로 다시 확인합니다.", true);
            } finally {
                workRefreshing = false;
                if (workTimer) clearTimeout(workTimer);
                workTimer = setTimeoutFn(refreshWork, 3000);
            }
        }

        async function enqueueTitle(ids) {
            var selected = Array.from(new Set(ids)).filter(function (id) { return !isTitlePending(id); });
            if (!selected.length) {
                showWorkMessage("선택한 녹취의 제목을 이미 정리 중입니다. 작업 내역에서 진행 상황을 확인해 주세요.");
                return;
            }
            var key = selected.slice().sort().join("\n");
            var requestId = titleRequests.get(key);
            if (!requestId) {
                requestId = "title-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
                titleRequests.set(key, requestId);
            }
            selected.forEach(function (id) { titlePending.add(id); });
            doc.dispatchEvent(new CustomEvent("recap:titles-updated", {detail: {ids: []}}));
            showWorkMessage(selected.length + "건 제목 정리를 시작합니다…");
            try {
                var receipt = await App.apiPost("/meetings/titles", {meeting_ids: selected, request_id: requestId});
                receipts.unshift(receipt);
                (receipt.candidates || []).forEach(function (row) {
                    revisions.set(receipt.request_id + ":" + row.meeting_id, JSON.stringify(lastEvent(receipt, row.meeting_id)));
                });
                titleRequests.delete(key);
                var admitted = (receipt.candidates || []).filter(function (row) { return row.admission === "queued"; }).map(function (row) { return row.meeting_id; });
                if (ListPanel.clearSelectedIds) ListPanel.clearSelectedIds(admitted);
                showWorkMessage(receipt.queued + "건 제목을 정리하고 있습니다. 다른 화면을 이용해도 계속됩니다.");
                await refreshWork();
                return receipt;
            } catch (err) {
                showWorkMessage("제목 정리 접수 확인 실패: " + err.message + " 작업 내역을 확인한 뒤 다시 시도해 주세요.", true);
                throw err;
            } finally {
                selected.forEach(function (id) { titlePending.delete(id); });
                doc.dispatchEvent(new CustomEvent("recap:titles-updated", {detail: {ids: []}}));
            }
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
                    if (ListPanel && ListPanel.clearSelectedIds) ListPanel.clearSelectedIds((receipt.candidates || []).filter(function (row) { return row.admission === "queued"; }).map(function (row) { return row.meeting_id; }));
                    doc.dispatchEvent(new CustomEvent("recap:dashboard-refresh"));
                    if (!dialog.open) return;
                    dialog.close();
                    showWorkMessage(receipt.queued + "건을 접수했습니다. 다른 화면을 이용해도 계속됩니다.");
                    refreshWork();
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
                if (ListPanel.getSelectedIds().length) { bar.classList.remove("is-leaving"); return; }
                bar.hidden = true;
                bar.classList.remove("is-leaving");
            }, 200);
        }

        function _onSelectionChanged(e) {
            if (!_bar) return;
            var detail = e.detail || {};
            var count = detail.count || 0;
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
            if (action === "title") { enqueueTitle(ids).catch(function () {}); return; }
            openReview({action: action, scope: "selected", meeting_ids: ids});
            return;

        }

        function _onClick(e) {
            var t = e.target.closest("[data-action]");
            if (!t || !_bar.contains(t)) return;
            var action = t.getAttribute("data-action");
            if (action === "run") {
                _executeAction(doc.getElementById("bulkTaskSelect").value);
                return;
            }
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
            var history = doc.getElementById("batchHistoryButton");
            if (history) history.addEventListener("click", showHistory);
            var progressHistory = doc.getElementById("backgroundWorkHistory");
            if (progressHistory) progressHistory.addEventListener("click", showHistory);
            _initialized = true;
            refreshWork();
        }

        return {
            init: init,
            showBulkToast: showBulkToast,
            openReview: openReview,
            showHistory: showHistory,
            enqueueTitle: enqueueTitle,
            isTitlePending: isTitlePending,
        };
    }

    window.MeetingBulkActionBar = {
        create: create,
    };
})();
