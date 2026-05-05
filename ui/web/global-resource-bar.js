/* =================================================================
 * Recap GlobalResourceBar boundary
 *
 * 목적: 전역 RAM/CPU/모델 상태 바를 SPA shell 에서 분리한다.
 * 공개 API: window.MeetingGlobalResourceBar
 * ================================================================= */
(function () {
    "use strict";

    function create(deps) {
        deps = deps || {};
        var App = deps.App || window.MeetingApp;
        var doc = deps.document || window.document;
        var intervalMs = deps.intervalMs || 5000;
        var setIntervalFn = deps.setInterval || window.setInterval.bind(window);
        var clearIntervalFn = deps.clearInterval || window.clearInterval.bind(window);

        if (!App || !doc) {
            throw new Error("MeetingGlobalResourceBar requires App and document");
        }

        var _timer = null;
        var _el = null;
        var _refreshSeq = 0;
        var _stopped = true;

        function _ensureDom() {
            if (_el && doc.body && doc.body.contains(_el)) return _el;
            _el = doc.getElementById("globalResourceBar");
            if (_el) return _el;

            _el = doc.createElement("div");
            _el.id = "globalResourceBar";
            _el.className = "global-resource-bar";
            _el.setAttribute("role", "status");
            _el.setAttribute("aria-live", "polite");
            _el.innerHTML = [
                '<div class="grb-item">',
                '  <span class="grb-label">RAM</span>',
                '  <div class="grb-bar-bg"><div class="grb-bar-fill" id="grb-ram-bar"></div></div>',
                '  <span class="grb-value" id="grb-ram-text">--</span>',
                '</div>',
                '<div class="grb-item">',
                '  <span class="grb-label">CPU</span>',
                '  <div class="grb-bar-bg"><div class="grb-bar-fill" id="grb-cpu-bar"></div></div>',
                '  <span class="grb-value" id="grb-cpu-text">--</span>',
                '</div>',
                '<div class="grb-model" id="grb-model-text" title="현재 로드된 모델"></div>',
            ].join("");
            doc.body.appendChild(_el);
            return _el;
        }

        function _applyBarState(bar, pct) {
            bar.style.width = pct + "%";
            bar.className = "grb-bar-fill" +
                (pct > 85 ? " danger" : pct > 70 ? " warning" : "");
        }

        function _refresh() {
            if (_stopped) return;
            _ensureDom();
            var seq = _refreshSeq + 1;
            _refreshSeq = seq;

            App.apiRequest("/system/resources")
                .then(function (data) {
                    if (_stopped || seq !== _refreshSeq) return;

                    var ramBar = doc.getElementById("grb-ram-bar");
                    var ramText = doc.getElementById("grb-ram-text");
                    if (ramBar && ramText) {
                        var ramPct = 0;
                        if (data.ram_total_gb > 0) {
                            ramPct = Math.round(
                                (data.ram_used_gb / data.ram_total_gb) * 100
                            );
                        }
                        _applyBarState(ramBar, ramPct);
                        ramText.textContent = data.ram_used_gb + "/" + data.ram_total_gb + "G";
                    }

                    var cpuBar = doc.getElementById("grb-cpu-bar");
                    var cpuText = doc.getElementById("grb-cpu-text");
                    if (cpuBar && cpuText) {
                        var cpuPct = data.cpu_percent || 0;
                        _applyBarState(cpuBar, cpuPct);
                        cpuText.textContent = cpuPct + "%";
                    }

                    var modelText = doc.getElementById("grb-model-text");
                    if (modelText) {
                        modelText.textContent = data.loaded_model || "";
                    }
                })
                .catch(function () {
                    // 서버 미시작 등은 무시
                });
        }

        function start() {
            _stopped = false;
            _ensureDom();
            _refresh();
            if (_timer) clearIntervalFn(_timer);
            _timer = setIntervalFn(_refresh, intervalMs);
        }

        function stop() {
            _stopped = true;
            _refreshSeq += 1;
            if (_timer) {
                clearIntervalFn(_timer);
                _timer = null;
            }
        }

        return { start: start, stop: stop, refresh: _refresh };
    }

    window.MeetingGlobalResourceBar = {
        create: create,
    };
})();
