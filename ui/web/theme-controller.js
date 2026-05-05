/* =================================================================
 * Recap Theme Controller boundary
 *
 * Public API: window.MeetingThemeController
 * ================================================================= */
(function () {
    "use strict";

    function create(deps) {
        deps = deps || {};
        var doc = deps.document || document;
        var root = deps.root || doc.documentElement;
        var storage = deps.localStorage || window.localStorage;
        var media = deps.matchMedia || window.matchMedia;
        var buttonId = deps.buttonId || "themeToggle";

        function _setStoredTheme(theme) {
            root.setAttribute("data-theme", theme);
            try {
                storage.setItem("theme", theme);
            } catch (err) {
                // Storage can be unavailable in private or fixture contexts.
            }
            return theme;
        }

        function _systemPrefersDark() {
            return Boolean(
                media &&
                    media.call(window, "(prefers-color-scheme: dark)").matches
            );
        }

        function restore() {
            var saved = null;
            try {
                saved = storage.getItem("theme");
            } catch (err) {
                saved = null;
            }
            if (saved === "dark" || saved === "light") {
                root.setAttribute("data-theme", saved);
                return saved;
            }
            return null;
        }

        function toggle() {
            var current = root.getAttribute("data-theme");
            if (current === "dark") {
                return _setStoredTheme("light");
            }
            if (current === "light") {
                return _setStoredTheme("dark");
            }
            return _setStoredTheme(_systemPrefersDark() ? "light" : "dark");
        }

        function init() {
            var btn = doc.getElementById(buttonId);
            restore();
            if (!btn) return;
            btn.addEventListener("click", toggle);
        }

        return {
            init: init,
            restore: restore,
            toggle: toggle,
        };
    }

    window.MeetingThemeController = {
        create: create,
    };
})();
