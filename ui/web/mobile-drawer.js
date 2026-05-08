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
        var mediaQuery = null;

        function _isReady() {
            return Boolean(toggleBtn && panel && backdrop);
        }

        function firstFocusable() {
            var focusables = getFocusable();
            return focusables.length > 0 ? focusables[0] : null;
        }

        function getFocusable() {
            if (!panel) return [];
            return Array.prototype.slice.call(panel.querySelectorAll(
                'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
            ));
        }

        function isDrawerMode() {
            return mediaQuery ? mediaQuery.matches : false;
        }

        function setPanelHidden(hidden) {
            if (!panel) return;
            if (!isDrawerMode()) {
                panel.removeAttribute("aria-hidden");
                panel.inert = false;
                return;
            }
            panel.setAttribute("aria-hidden", hidden ? "true" : "false");
            panel.inert = hidden;
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
            setPanelHidden(false);
            panel.classList.add("is-open");
            backdrop.classList.add("visible");
            body.style.overflow = "hidden";
            var first = firstFocusable();
            if (first) {
                first.focus();
            }
        }

        function close(options) {
            if (!_isReady()) return;
            options = options || {};
            toggleBtn.setAttribute("aria-expanded", "false");
            toggleBtn.setAttribute("aria-label", "메뉴 열기");
            panel.classList.remove("is-open");
            backdrop.classList.remove("visible");
            body.style.overflow = "";
            setPanelHidden(true);
            if (options.restoreFocus !== false) {
                toggleBtn.focus();
            }
        }

        function syncForViewport() {
            if (!_isReady()) return;
            if (!isDrawerMode()) {
                panel.classList.remove("is-open");
                backdrop.classList.remove("visible");
                body.style.overflow = "";
                toggleBtn.setAttribute("aria-expanded", "false");
                toggleBtn.setAttribute("aria-label", "메뉴 열기");
                setPanelHidden(false);
                return;
            }
            setPanelHidden(!isOpen());
        }

        function init() {
            toggleBtn = doc.getElementById(toggleId);
            panel = doc.getElementById(panelId);
            backdrop = doc.getElementById(backdropId);
            if (!_isReady()) return;
            mediaQuery = (doc.defaultView || window).matchMedia("(max-width: 768px)");
            syncForViewport();

            toggleBtn.addEventListener("click", function () {
                if (isOpen()) {
                    close();
                } else {
                    open();
                }
            });

            backdrop.addEventListener("click", function () { close(); });

            doc.addEventListener("keydown", function (e) {
                if (e.key === "Escape" && isOpen()) {
                    close();
                }
                if (e.key !== "Tab" || !isOpen()) return;
                var focusables = getFocusable();
                if (!focusables.length) return;
                var first = focusables[0];
                var last = focusables[focusables.length - 1];
                if (e.shiftKey && doc.activeElement === first) {
                    e.preventDefault();
                    last.focus();
                } else if (!e.shiftKey && doc.activeElement === last) {
                    e.preventDefault();
                    first.focus();
                }
            });

            if (mediaQuery.addEventListener) {
                mediaQuery.addEventListener("change", syncForViewport);
            } else if (mediaQuery.addListener) {
                mediaQuery.addListener(syncForViewport);
            }
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
