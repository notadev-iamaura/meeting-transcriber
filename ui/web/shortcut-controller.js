/* =================================================================
 * Recap Shortcut Controller boundary
 *
 * Public API: window.MeetingShortcutController
 * ================================================================= */
(function () {
    "use strict";

    function _fallbackIsEditingContext(target) {
        if (!target) return false;
        var tag = (target.tagName || "").toUpperCase();
        return (
            tag === "INPUT" || tag === "TEXTAREA" || target.isContentEditable
        );
    }

    function create(deps) {
        deps = deps || {};
        var doc = deps.document || document;
        var Router = deps.Router || (window.SPA && window.SPA.Router);
        var commandPalette = deps.CommandPalette || deps.commandPalette;
        var isEditingContext =
            deps.isEditingContext || _fallbackIsEditingContext;
        var started = false;

        if (!Router || !commandPalette) {
            throw new Error(
                "MeetingShortcutController requires Router and CommandPalette"
            );
        }

        function onKeydown(e) {
            if (!(e.metaKey || e.ctrlKey)) return;

            if (e.key === "k") {
                if (isEditingContext(e.target)) return;
                e.preventDefault();
                commandPalette.open();
                return;
            }

            if (e.key === ",") {
                e.preventDefault();
                Router.navigate("/app/settings");
                return;
            }

            if (e.key === "1" || e.key === "2" || e.key === "3") {
                if (isEditingContext(e.target)) return;
                e.preventDefault();
                if (e.key === "1") Router.navigate("/app");
                else if (e.key === "2") Router.navigate("/app/search");
                else Router.navigate("/app/chat");
            }
        }

        function start() {
            if (started) return;
            doc.addEventListener("keydown", onKeydown);
            started = true;
        }

        function stop() {
            if (!started) return;
            doc.removeEventListener("keydown", onKeydown);
            started = false;
        }

        return {
            start: start,
            stop: stop,
            onKeydown: onKeydown,
        };
    }

    window.MeetingShortcutController = {
        create: create,
    };
})();
