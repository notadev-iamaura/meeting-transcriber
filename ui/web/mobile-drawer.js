/* =================================================================
 * Recap Mobile Drawer boundary
 *
 * Public API: window.MeetingMobileDrawer
 * ================================================================= */
(function () {
    "use strict";

    function create(deps) {
        deps = deps || {};
        var doc = deps.document || document;
        var body = deps.body || doc.body;
        var toggleId = deps.toggleId || "mobile-menu-toggle";
        var panelId = deps.panelId || "list-panel";
        var backdropId = deps.backdropId || "drawer-backdrop";

        var toggleBtn = null;
        var panel = null;
        var backdrop = null;

        function _isReady() {
            return Boolean(toggleBtn && panel && backdrop);
        }

        function firstFocusable() {
            if (!panel) return null;
            return panel.querySelector(
                'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
            );
        }

        function isOpen() {
            return Boolean(
                toggleBtn &&
                    toggleBtn.getAttribute("aria-expanded") === "true"
            );
        }

        function open() {
            if (!_isReady()) return;
            toggleBtn.setAttribute("aria-expanded", "true");
            toggleBtn.setAttribute("aria-label", "메뉴 닫기");
            panel.classList.add("is-open");
            backdrop.classList.add("visible");
            body.style.overflow = "hidden";
            var first = firstFocusable();
            if (first) {
                first.focus();
            }
        }

        function close() {
            if (!_isReady()) return;
            toggleBtn.setAttribute("aria-expanded", "false");
            toggleBtn.setAttribute("aria-label", "메뉴 열기");
            panel.classList.remove("is-open");
            backdrop.classList.remove("visible");
            body.style.overflow = "";
            toggleBtn.focus();
        }

        function init() {
            toggleBtn = doc.getElementById(toggleId);
            panel = doc.getElementById(panelId);
            backdrop = doc.getElementById(backdropId);
            if (!_isReady()) return;

            toggleBtn.addEventListener("click", function () {
                if (isOpen()) {
                    close();
                } else {
                    open();
                }
            });

            backdrop.addEventListener("click", close);

            doc.addEventListener("keydown", function (e) {
                if (e.key === "Escape" && isOpen()) {
                    close();
                }
            });
        }

        return {
            init: init,
            open: open,
            close: close,
            isOpen: isOpen,
        };
    }

    window.MeetingMobileDrawer = {
        create: create,
    };
})();
