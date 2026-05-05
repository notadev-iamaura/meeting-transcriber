/* =================================================================
 * Recap Command Palette boundary
 *
 * 목적: ⌘K 명령 팔레트를 SPA 라우터/뷰 코드에서 분리한다.
 * 공개 API: window.MeetingCommandPalette
 * ================================================================= */
(function () {
    "use strict";

    function isEditingContext(target) {
        if (!target) return false;
        var inPalette =
            target.closest && target.closest("dialog.command-palette");
        if (inPalette) return false;
        var tag = (target.tagName || "").toUpperCase();
        return (
            tag === "INPUT" || tag === "TEXTAREA" || target.isContentEditable
        );
    }

    function create(deps) {
        deps = deps || {};
        var App = deps.App || window.MeetingApp;
        var Router = deps.Router || (window.SPA && window.SPA.Router);
        var toggleTheme = deps.toggleTheme || null;
        if (!App || !Router) {
            throw new Error("MeetingCommandPalette requires App and Router");
        }

        function CommandPalette() {
            this._isOpen = false;
            // T-202: native <dialog> 기반으로 마이그레이션
            // _dialogEl: <dialog class="command-palette">
            // _listboxEl: <ul id="command-palette-list" role="listbox">
            // _emptyEl: <div class="command-palette-empty" role="status">
            this._dialogEl = null;
            this._inputEl = null;
            this._listboxEl = null;
            this._emptyEl = null;
            this._items = [];
            this._filteredItems = [];
            this._selectedIdx = 0;
            this._recentActions = [];
            this._meetingsCache = null;
            this._sttModelsCache = null;
            this._boundKeydown = null;
            this._loadRecent();
        }

        var CMDK_STORAGE_KEY = "cmdk-recent-actions";
        var CMDK_RECENT_LIMIT = 20;

        /**
         * LocalStorage 에서 최근 액션 ID 목록 로드.
         */
        CommandPalette.prototype._loadRecent = function () {
            try {
                var raw = localStorage.getItem(CMDK_STORAGE_KEY);
                this._recentActions = raw ? JSON.parse(raw) : [];
                if (!Array.isArray(this._recentActions)) {
                    this._recentActions = [];
                }
            } catch (err) {
                this._recentActions = [];
            }
        };

        /**
         * 최근 사용 액션 ID 를 앞쪽에 추가 + 저장.
         */
        CommandPalette.prototype._pushRecent = function (itemId) {
            if (!itemId) return;
            var next = [itemId];
            for (var i = 0; i < this._recentActions.length; i++) {
                if (this._recentActions[i] !== itemId) {
                    next.push(this._recentActions[i]);
                }
            }
            this._recentActions = next.slice(0, CMDK_RECENT_LIMIT);
            try {
                localStorage.setItem(
                    CMDK_STORAGE_KEY,
                    JSON.stringify(this._recentActions)
                );
            } catch (err) {
                // 저장 실패 무시
            }
        };

        /**
         * 간단한 fuzzy 매칭 점수: 완전일치(100) > 접두(50) > 부분(10) > 불일치(0).
         */
        function cmdkFuzzyMatch(query, text) {
            if (!query) return 1;
            if (!text) return 0;
            var q = String(query).toLowerCase();
            var t = String(text).toLowerCase();
            if (t === q) return 100;
            if (t.indexOf(q) === 0) return 50;
            if (t.indexOf(q) >= 0) return 10;
            return 0;
        }

        /**
         * 팔레트 열기. 최초 호출 시 DOM 생성, 매 호출마다 항목 갱신.
         *
         * T-202: native <dialog>.showModal() 사용 — focus trap + ::backdrop +
         * ESC 자동 닫기를 브라우저 native 로 위임 (커스텀 trap 코드 제거).
         */
        CommandPalette.prototype.open = function () {
            if (this._isOpen) return;
            var self = this;

            if (!this._dialogEl) {
                this._createDom();
            }

            this._items = this._buildStaticItems();
            this._filter("");
            this._selectedIdx = 0;

            this._isOpen = true;
            this._inputEl.value = "";
            this._render();

            // native dialog: showModal() 은 focus return 을 자동 처리 (WCAG 2.1.2)
            try {
                if (typeof this._dialogEl.showModal === "function") {
                    this._dialogEl.showModal();
                } else {
                    // 구형 브라우저 폴백 — open 속성 강제
                    this._dialogEl.setAttribute("open", "");
                }
            } catch (err) {
                // 이미 열려있는 등 InvalidStateError 무시
            }

            setTimeout(function () {
                if (self._inputEl) self._inputEl.focus();
            }, 0);

            this._boundKeydown = function (e) {
                self._handleKeydown(e);
            };
            document.addEventListener("keydown", this._boundKeydown, true);

            this._loadAsyncItems();
        };

        /**
         * 팔레트 닫기. native <dialog>.close() 가 focus return 을 자동 수행.
         */
        CommandPalette.prototype.close = function () {
            if (!this._isOpen) return;
            this._isOpen = false;
            if (this._dialogEl) {
                try {
                    if (typeof this._dialogEl.close === "function") {
                        this._dialogEl.close();
                    } else {
                        this._dialogEl.removeAttribute("open");
                    }
                } catch (err) {
                    // 이미 닫혀있음 무시
                }
            }
            if (this._boundKeydown) {
                document.removeEventListener("keydown", this._boundKeydown, true);
                this._boundKeydown = null;
            }
        };

        /**
         * 모달 DOM 생성 (최초 1회).
         *
         * T-202 마크업 인터페이스 (mockup §3):
         *   <dialog class="command-palette" aria-modal="true" aria-label="명령 팔레트">
         *     <div class="command-palette-content">
         *       <div role="combobox" aria-haspopup="listbox" aria-expanded="true"
         *            aria-controls="command-palette-list">
         *         <svg class="command-palette-icon">...</svg>
         *         <input role="searchbox" aria-autocomplete="list" aria-controls=...>
         *         <kbd class="command-palette-shortcut">ESC</kbd>
         *       </div>
         *       <ul id="command-palette-list" role="listbox" aria-label="검색 결과">…</ul>
         *       <div class="command-palette-empty" role="status" hidden>…</div>
         *       <div class="command-palette-footer">…</div>
         *     </div>
         *   </dialog>
         */
        CommandPalette.prototype._createDom = function () {
            var self = this;

            var dialog = document.createElement("dialog");
            dialog.className = "command-palette";
            dialog.id = "command-palette";
            dialog.setAttribute("aria-label", "명령 팔레트");
            dialog.setAttribute("aria-modal", "true");

            var content = document.createElement("div");
            content.className = "command-palette-content";

            // combobox 컨테이너 — ARIA 1.2 'Editable Combobox With List Autocomplete'
            var inputWrap = document.createElement("div");
            inputWrap.className = "command-palette-input-wrap";
            inputWrap.setAttribute("role", "combobox");
            inputWrap.setAttribute("aria-haspopup", "listbox");
            inputWrap.setAttribute("aria-expanded", "true");
            inputWrap.setAttribute("aria-controls", "command-palette-list");

            // 검색 아이콘 (장식적, aria-hidden)
            var icon = document.createElementNS(
                "http://www.w3.org/2000/svg",
                "svg"
            );
            icon.setAttribute("class", "command-palette-icon");
            icon.setAttribute("viewBox", "0 0 24 24");
            icon.setAttribute("fill", "none");
            icon.setAttribute("stroke", "currentColor");
            icon.setAttribute("stroke-width", "2");
            icon.setAttribute("stroke-linecap", "round");
            icon.setAttribute("stroke-linejoin", "round");
            icon.setAttribute("aria-hidden", "true");
            var circle = document.createElementNS(
                "http://www.w3.org/2000/svg",
                "circle"
            );
            circle.setAttribute("cx", "11");
            circle.setAttribute("cy", "11");
            circle.setAttribute("r", "7");
            var path = document.createElementNS(
                "http://www.w3.org/2000/svg",
                "path"
            );
            path.setAttribute("d", "M21 21l-4.5-4.5");
            icon.appendChild(circle);
            icon.appendChild(path);

            var input = document.createElement("input");
            input.type = "text";
            input.className = "command-palette-input";
            input.setAttribute("placeholder", "명령 또는 검색…");
            input.setAttribute("aria-label", "명령 검색");
            input.setAttribute("aria-autocomplete", "list");
            input.setAttribute("aria-controls", "command-palette-list");
            input.setAttribute("role", "searchbox");
            input.setAttribute("autocomplete", "off");
            input.setAttribute("spellcheck", "false");

            var escKbd = document.createElement("kbd");
            escKbd.className = "command-palette-shortcut";
            escKbd.textContent = "ESC";

            inputWrap.appendChild(icon);
            inputWrap.appendChild(input);
            inputWrap.appendChild(escKbd);

            // listbox — 단일 active option 만 aria-selected="true"
            var listbox = document.createElement("ul");
            listbox.id = "command-palette-list";
            listbox.setAttribute("role", "listbox");
            listbox.setAttribute("aria-label", "검색 결과");

            // 빈 상태 — role="status" live region (결과 없음 안내)
            var empty = document.createElement("div");
            empty.className = "command-palette-empty";
            empty.setAttribute("role", "status");
            empty.hidden = true;
            var emptyP = document.createElement("p");
            emptyP.textContent = "검색 결과가 없습니다";
            empty.appendChild(emptyP);

            // footer — 단축키 안내
            var footer = document.createElement("div");
            footer.className = "command-palette-footer";
            var hints = [
                { kbd: "↑↓", label: "탐색" },
                { kbd: "↵", label: "실행" },
                { kbd: "ESC", label: "닫기" },
            ];
            for (var hi = 0; hi < hints.length; hi++) {
                var span = document.createElement("span");
                var k = document.createElement("kbd");
                k.textContent = hints[hi].kbd;
                span.appendChild(k);
                span.appendChild(document.createTextNode(hints[hi].label));
                footer.appendChild(span);
            }

            content.appendChild(inputWrap);
            content.appendChild(listbox);
            content.appendChild(empty);
            content.appendChild(footer);
            dialog.appendChild(content);
            document.body.appendChild(dialog);

            this._dialogEl = dialog;
            this._inputEl = input;
            this._listboxEl = listbox;
            this._emptyEl = empty;

            // 입력 → 즉시 필터링
            input.addEventListener("input", function () {
                self._filter(input.value);
                self._selectedIdx = 0;
                self._render();
            });

            // ::backdrop 클릭 → 닫기. native <dialog> 의 backdrop 클릭은 dialog
            // 자체 click 이벤트로 들어오며 e.target === dialog 일 때만 외부 클릭.
            dialog.addEventListener("click", function (e) {
                if (e.target === dialog) {
                    self.close();
                }
            });

            // ESC 로 dialog 의 native cancel 이벤트가 발생하면 정리 작업 수행
            dialog.addEventListener("cancel", function () {
                // close() 에서 boundKeydown 정리. dialog 는 cancel 후 자동 닫힘.
                if (self._isOpen) {
                    self._isOpen = false;
                    if (self._boundKeydown) {
                        document.removeEventListener(
                            "keydown",
                            self._boundKeydown,
                            true
                        );
                        self._boundKeydown = null;
                    }
                }
            });

            // listbox 클릭 → 해당 option 실행
            listbox.addEventListener("click", function (e) {
                var target = e.target;
                while (target && target !== listbox) {
                    if (
                        target.tagName === "LI" &&
                        target.getAttribute("role") === "option"
                    ) {
                        var idx = parseInt(target.getAttribute("data-idx"), 10);
                        if (!isNaN(idx) && self._filteredItems[idx]) {
                            self._executeItem(self._filteredItems[idx]);
                        }
                        return;
                    }
                    target = target.parentNode;
                }
            });
        };

        /**
         * 정적 항목(뷰 전환/작업 명령/도움말) 빌드. 비동기 항목은 _loadAsyncItems() 가 이후 주입.
         *
         * T-202 mockup §5.1: 뷰 전환 4 종(홈/검색/채팅/설정) — 정적 카테고리 "뷰 전환".
         * mockup §5.3: 작업 명령(다크 모드 토글 등) — 정적 카테고리 "작업".
         */
        CommandPalette.prototype._buildStaticItems = function () {
            var items = [];

            // 뷰 전환 (mockup §5.1) — 홈/검색/채팅/설정 순서 (fixture baseline 매칭)
            items.push({
                id: "action:goto-home",
                category: "뷰 전환",
                title: "홈",
                subtitle: "뷰 전환",
                icon: "🏠",
                route: "/app",
                run: function () {
                    Router.navigate("/app");
                },
            });
            items.push({
                id: "action:goto-search",
                category: "뷰 전환",
                title: "검색",
                subtitle: "뷰 전환",
                icon: "🔍",
                route: "/app/search",
                run: function () {
                    Router.navigate("/app/search");
                },
            });
            items.push({
                id: "action:goto-chat",
                category: "뷰 전환",
                title: "채팅",
                subtitle: "뷰 전환",
                icon: "💬",
                route: "/app/chat",
                run: function () {
                    Router.navigate("/app/chat");
                },
            });
            items.push({
                id: "action:goto-settings",
                category: "뷰 전환",
                title: "설정",
                subtitle: "뷰 전환",
                icon: "⚙",
                route: "/app/settings",
                run: function () {
                    Router.navigate("/app/settings");
                },
            });

            // 작업 명령 (mockup §5.3)
            items.push({
                id: "theme.toggle",
                category: "작업",
                title: "다크 모드 전환",
                subtitle: "라이트 ↔ 다크 테마 토글",
                run: function () {
                    if (typeof toggleTheme === "function") {
                        toggleTheme();
                        return;
                    }
                    var root = document.documentElement;
                    var current = root.getAttribute("data-theme");
                    var next;
                    if (current === "dark") {
                        next = "light";
                    } else if (current === "light") {
                        next = "dark";
                    } else {
                        next = window.matchMedia("(prefers-color-scheme: dark)")
                            .matches
                            ? "light"
                            : "dark";
                    }
                    root.setAttribute("data-theme", next);
                    try {
                        localStorage.setItem("theme", next);
                    } catch (err) {
                        // 저장 실패 무시
                    }
                },
            });
            // 도움말 — 키보드 단축키 모달
            items.push({
                id: "help.shortcuts",
                category: "도움말",
                title: "키보드 단축키",
                subtitle: "⌘K 명령 팔레트 · ⌘F 찾기 · ⌘S 저장",
                run: function () {
                    alert(
                        "키보드 단축키\n\n" +
                            "⌘K  명령 팔레트 열기\n" +
                            "⌘F  뷰어 내 찾기\n" +
                            "⌘S  저장"
                    );
                },
            });

            return items;
        };

        /**
         * /api/meetings, /api/stt-models 를 병렬 호출해 항목 주입.
         */
        CommandPalette.prototype._loadAsyncItems = function () {
            var self = this;

            App.apiRequest("/meetings")
                .then(function (data) {
                    var meetings = (data && data.meetings) || [];
                    self._meetingsCache = meetings;
                    var added = [];
                    for (var i = 0; i < Math.min(meetings.length, 5); i++) {
                        var m = meetings[i];
                        if (!m || !m.id) continue;
                        var title =
                            m.title ||
                            (App.getFileName
                                ? App.getFileName(m.audio_file || "")
                                : "") ||
                            m.id;
                        added.push({
                            id: "meeting:" + m.id,
                            category: "회의",
                            title: String(title),
                            subtitle: m.created_at
                                ? App.formatDate
                                    ? App.formatDate(m.created_at)
                                    : String(m.created_at)
                                : "",
                            run: (function (mid) {
                                return function () {
                                    Router.navigate(
                                        "/app/viewer/" + encodeURIComponent(mid)
                                    );
                                };
                            })(m.id),
                        });
                    }
                    self._mergeDynamicItems("회의", added);
                    self._filter(self._inputEl ? self._inputEl.value : "");
                    self._render();
                })
                .catch(function () {
                    // 실패 시 정적 항목만 유지
                });

            App.apiRequest("/stt-models")
                .then(function (data) {
                    var models = (data && data.models) || [];
                    self._sttModelsCache = models;
                    var added = [];
                    for (var i = 0; i < models.length; i++) {
                        var m = models[i];
                        if (!m || !m.id) continue;
                        var st = m.status || "";
                        if (
                            st &&
                            st !== "ready" &&
                            st !== "active" &&
                            st !== "downloaded"
                        ) {
                            continue;
                        }
                        added.push({
                            id: "stt:" + m.id,
                            category: "STT 모델",
                            title: "모델 활성화: " + (m.name || m.id),
                            subtitle: m.description || m.id,
                            run: (function (modelId) {
                                return function () {
                                    App.apiPost(
                                        "/stt-models/" +
                                            encodeURIComponent(modelId) +
                                            "/activate",
                                        {}
                                    ).catch(function () {});
                                    Router.navigate("/app/settings/general");
                                };
                            })(m.id),
                        });
                    }
                    self._mergeDynamicItems("STT 모델", added);
                    self._filter(self._inputEl ? self._inputEl.value : "");
                    self._render();
                })
                .catch(function () {
                    // 실패 시 정적 항목만 유지
                });
        };

        /**
         * 특정 카테고리의 기존 항목을 제거하고 새 항목으로 교체 (중복 방지).
         */
        CommandPalette.prototype._mergeDynamicItems = function (category, added) {
            var base = [];
            for (var i = 0; i < this._items.length; i++) {
                if (this._items[i].category !== category) {
                    base.push(this._items[i]);
                }
            }
            this._items = base.concat(added);
        };

        /**
         * 쿼리로 필터링 + 최근 사용 가중치로 정렬.
         */
        CommandPalette.prototype._filter = function (query) {
            var q = (query || "").trim();
            var scored = [];
            for (var i = 0; i < this._items.length; i++) {
                var item = this._items[i];
                var haystack =
                    (item.title || "") +
                    " " +
                    (item.subtitle || "") +
                    " " +
                    (item.category || "");
                var score = cmdkFuzzyMatch(q, haystack);
                if (score > 0 || !q) {
                    var recentIdx = this._recentActions.indexOf(item.id);
                    var recentBoost =
                        recentIdx >= 0 ? CMDK_RECENT_LIMIT - recentIdx : 0;
                    scored.push({ item: item, score: score + recentBoost });
                }
            }
            scored.sort(function (a, b) {
                return b.score - a.score;
            });
            this._filteredItems = scored.map(function (s) {
                return s.item;
            });
        };

        /**
         * listbox 갱신. mockup §3 마크업: <ul role="listbox"> > <li role="option">.
         * XSS 방지: textContent 만 사용.
         *
         * 단일 active option (aria-selected="true") 만 유지 (mockup §6.3).
         * 빈 결과는 `.command-palette-empty[role=status]` live region 으로 표시.
         */
        CommandPalette.prototype._render = function () {
            if (!this._listboxEl) return;
            var listbox = this._listboxEl;
            var emptyEl = this._emptyEl;

            // 항목 비우기
            while (listbox.firstChild) {
                listbox.removeChild(listbox.firstChild);
            }

            if (this._filteredItems.length === 0) {
                // listbox 는 비워두고 빈 상태 live region 만 노출
                listbox.hidden = true;
                if (emptyEl) emptyEl.hidden = false;
                return;
            }

            listbox.hidden = false;
            if (emptyEl) emptyEl.hidden = true;

            for (var i = 0; i < this._filteredItems.length; i++) {
                var item = this._filteredItems[i];
                var li = document.createElement("li");
                li.setAttribute("role", "option");
                li.setAttribute("data-idx", String(i));
                li.setAttribute("tabindex", "-1");

                // data-action 식별자 — mockup §3.1
                // navigate / open-meeting / command 중 하나로 라우팅 식별
                if (item.id && item.id.indexOf("meeting:") === 0) {
                    li.setAttribute("data-action", "open-meeting");
                    li.setAttribute(
                        "data-meeting-id",
                        item.id.slice("meeting:".length)
                    );
                } else if (item.id && item.id.indexOf("action:goto-") === 0) {
                    li.setAttribute("data-action", "navigate");
                    if (item.route) {
                        li.setAttribute("data-route", item.route);
                    }
                } else {
                    li.setAttribute("data-action", "command");
                    if (item.id) {
                        li.setAttribute("data-command-id", item.id);
                    }
                }

                if (i === this._selectedIdx) {
                    li.setAttribute("aria-selected", "true");
                } else {
                    li.setAttribute("aria-selected", "false");
                }

                // icon (선택)
                if (item.icon) {
                    var iconSpan = document.createElement("span");
                    iconSpan.className = "command-palette-item-icon";
                    iconSpan.setAttribute("aria-hidden", "true");
                    iconSpan.textContent = item.icon;
                    li.appendChild(iconSpan);
                }

                // label
                var label = document.createElement("span");
                label.className = "command-palette-item-label";
                label.textContent = item.title || "";
                li.appendChild(label);

                // subtitle 은 카테고리에 따라 meta(시간/메타정보) 또는 hint(보조 안내)
                if (item.subtitle) {
                    var sub = document.createElement("span");
                    sub.className =
                        item.category === "회의"
                            ? "command-palette-item-meta"
                            : "command-palette-item-hint";
                    sub.textContent = item.subtitle;
                    li.appendChild(sub);
                }

                listbox.appendChild(li);
            }

            var selectedEl = listbox.querySelector(
                'li[role="option"][aria-selected="true"]'
            );
            if (selectedEl && selectedEl.scrollIntoView) {
                selectedEl.scrollIntoView({ block: "nearest" });
            }
        };

        /**
         * 팔레트 내부 키보드 조작 (document capture phase).
         *
         * ESC 는 native <dialog> 의 cancel 이벤트가 처리하므로 본 핸들러에서는
         * ↑↓/Enter 만 가로챈다 (mockup §4).
         */
        CommandPalette.prototype._handleKeydown = function (e) {
            if (!this._isOpen) return;
            if (e.key === "Escape") {
                // native <dialog> 의 cancel 이벤트가 close() 정리를 수행한다.
                // 별도로 close() 를 호출하면 boundKeydown 이 두 번 정리될 수 있으므로
                // 여기서는 명시적으로 close() 호출만 보장하고 default 진행.
                this.close();
                return;
            }
            if (e.key === "ArrowDown") {
                e.preventDefault();
                if (this._filteredItems.length > 0) {
                    this._selectedIdx =
                        (this._selectedIdx + 1) % this._filteredItems.length;
                    this._render();
                }
                return;
            }
            if (e.key === "ArrowUp") {
                e.preventDefault();
                if (this._filteredItems.length > 0) {
                    this._selectedIdx =
                        (this._selectedIdx - 1 + this._filteredItems.length) %
                        this._filteredItems.length;
                    this._render();
                }
                return;
            }
            if (e.key === "Enter") {
                e.preventDefault();
                var item = this._filteredItems[this._selectedIdx];
                if (item) this._executeItem(item);
                return;
            }
        };

        /**
         * 항목 실행: 최근 목록 갱신 → 닫기 → run().
         */
        CommandPalette.prototype._executeItem = function (item) {
            if (!item || typeof item.run !== "function") return;
            this._pushRecent(item.id);
            this.close();
            try {
                item.run();
            } catch (err) {
                // 실행 실패 무시
            }
        };

        return new CommandPalette();
    }

    window.MeetingCommandPalette = {
        create: create,
        isEditingContext: isEditingContext,
    };
})();
