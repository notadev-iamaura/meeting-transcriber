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

            _inFlight = true;
            _bar.setAttribute("data-inflight", "true");

            App.apiPost("/meetings/batch", {
                action: action,
                scope: "selected",
                meeting_ids: ids,
            }).then(function (resp) {
                var queued = (resp && resp.queued != null) ? resp.queued : ids.length;
                var skipped = (resp && resp.skipped != null) ? resp.skipped : 0;
                var msg = queued + "건 처리"
                    + (skipped > 0 ? ", " + skipped + "건 건너뜀" : "");
                showBulkToast(msg, "info");
                _inFlight = false;
                if (_bar) _bar.removeAttribute("data-inflight");
                if (ListPanel && ListPanel.clearSelection) {
                    ListPanel.clearSelection();
                }
            }).catch(function (err) {
                showBulkToast(
                    "처리 실패: " + (err && err.message ? err.message : "서버 오류"),
                    "error"
                );
                _inFlight = false;
                if (_bar) _bar.removeAttribute("data-inflight");
            });
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
            _initialized = true;
        }

        return {
            init: init,
            showBulkToast: showBulkToast,
        };
    }

    window.MeetingBulkActionBar = {
        create: create,
    };
})();
