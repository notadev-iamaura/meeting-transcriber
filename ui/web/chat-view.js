/* =================================================================
 * Recap ChatView boundary
 *
 * 목적: RAG 채팅 화면을 SPA 라우터 본문에서 분리한다.
 * 공개 API: window.MeetingChatView
 * ================================================================= */
(function () {
    "use strict";

    function create(deps) {
        deps = deps || {};
        var App = deps.App || window.MeetingApp;
        var Router = deps.Router || (window.SPA && window.SPA.Router);
        var Icons = deps.Icons || {};
        var errorBanner = deps.errorBanner || {
            show: function () {},
            hide: function () {},
        };

        if (!App || !Router) {
            throw new Error("MeetingChatView requires App and Router");
        }

        // =================================================================
        // === ChatView (AI 채팅) ===
        // =================================================================

        /**
         * AI 채팅 뷰: RAG 기반 질문/답변, 참조 카드, 세션 관리.
         * @constructor
         */
        function ChatView() {
            var self = this;
            self._listeners = [];
            self._timers = [];
            self._els = {};
            self._isSending = false;
            self._messageCount = 0;
            self._currentAbortController = null;

            // 세션 ID 생성 (대화 컨텍스트 유지)
            self._sessionId = self._generateSessionId();

            self._render();
            self._bind();
            self._loadMeetingList();
        }

        /**
         * 세션 ID를 생성한다.
         * @returns {string} UUID v4 형식 세션 ID
         */
        ChatView.prototype._generateSessionId = function () {
            if (typeof crypto !== "undefined" && crypto.randomUUID) {
                return crypto.randomUUID();
            }
            // 폴백: 간단한 UUID v4
            return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
                var r = (Math.random() * 16) | 0;
                var v = c === "x" ? r : (r & 0x3) | 0x8;
                return v.toString(16);
            });
        };

        /**
         * 환영 메시지 HTML을 생성한다. (_render 및 _clearChat 공용)
         * @returns {string} 환영 메시지 HTML 문자열
         */
        ChatView.prototype._createWelcomeHtml = function () {
            // 채팅 빈 상태 (mockup §5.3) — empty-state 패턴 + 기존 welcome-tips 보존
            // Hidden AI 원칙(design.md §5.1)에 따라 'AI' 단어 사용 금지
            return '<div class="empty-state-container" id="chatWelcomeMessage" data-empty="chat">' +
                '<div class="empty-state" role="status">' +
                '<svg class="empty-state-icon" width="48" height="48" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
                '<path d="M6 12h22a4 4 0 014 4v12a4 4 0 01-4 4h-12l-6 6v-6H6a4 4 0 01-4-4V16a4 4 0 014-4z" transform="translate(2 0)"/>' +
                '<path d="M16 18h22a4 4 0 014 4v12a4 4 0 01-4 4h-2v6l-6-6h-14a4 4 0 01-4-4V22a4 4 0 014-4z" transform="translate(0 4)" opacity="0.6"/>' +
                '</svg>' +
                '<h2 class="empty-state-title">대화를 시작해 보세요</h2>' +
                '<p class="empty-state-description">회의 내용에 대해 무엇이든 물어보세요. 화자별 요약·결정사항·다음 액션 등을 정리해 드려요.</p>' +
                '</div>' +
                '<div class="welcome-tips">' +
                    '<div class="welcome-tip"><span class="tip-arrow">&rarr;</span> "지난 회의에서 결정된 일정이 뭐야?"</div>' +
                    '<div class="welcome-tip"><span class="tip-arrow">&rarr;</span> "프로젝트 진행 상황을 요약해줘"</div>' +
                    '<div class="welcome-tip"><span class="tip-arrow">&rarr;</span> "다음 마일스톤까지 해야 할 일은?"</div>' +
                '</div>' +
            '</div>';
        };

        /**
         * 채팅 뷰 DOM을 생성한다.
         */
        ChatView.prototype._render = function () {
            var contentEl = Router.getContentEl();
            contentEl.innerHTML = "";

            var html = [
                '<div class="chat-layout">',

                // 제어 바
                '  <div class="controls-bar">',
                '    <span class="controls-label">검색 범위:</span>',
                '    <select class="controls-select" id="chatMeetingFilter" aria-label="검색 범위 회의 선택">',
                '      <option value="">전체 회의</option>',
                '    </select>',
                '    <div class="controls-right">',
                '      <button class="btn-small" id="chatBtnClearChat">대화 초기화</button>',
                '    </div>',
                '  </div>',

                // 메시지 영역
                '  <div class="messages-area" id="chatMessagesArea" role="log" aria-live="polite" aria-label="채팅 메시지">',
                this._createWelcomeHtml(),
                '  </div>',

                // 타이핑 인디케이터
                '  <div class="typing-indicator" id="chatTypingIndicator" role="status" aria-live="polite">',
                '    <div class="typing-dots">',
                '      <span class="typing-dot"></span>',
                '      <span class="typing-dot"></span>',
                '      <span class="typing-dot"></span>',
                '    </div>',
                '    <span class="typing-text">답변을 생성하고 있어요…</span>',
                '  </div>',

                // 입력 영역
                '  <div class="input-area">',
                '    <div class="input-row">',
                '      <div class="input-wrapper">',
                '        <textarea class="chat-input" id="chatInput"',
                '                  placeholder="회의 내용에 대해 질문하세요..."',
                '                  aria-label="회의 내용 질문 입력"',
                '                  rows="1"></textarea>',
                '      </div>',
                '      <button class="send-btn" id="chatSendBtn" disabled aria-label="메시지 전송">전송</button>',
                '      <button class="btn-cancel-send" id="chatCancelBtn" aria-label="응답 생성 취소">취소</button>',
                '    </div>',
                '    <div class="input-hint">Enter로 전송, Shift+Enter로 줄바꿈</div>',
                '  </div>',

                '</div>',
            ].join("\n");

            contentEl.innerHTML = html;

            // DOM 참조 캐싱
            this._els = {
                meetingFilter: document.getElementById("chatMeetingFilter"),
                btnClearChat: document.getElementById("chatBtnClearChat"),
                messagesArea: document.getElementById("chatMessagesArea"),
                welcomeMessage: document.getElementById("chatWelcomeMessage"),
                typingIndicator: document.getElementById("chatTypingIndicator"),
                chatInput: document.getElementById("chatInput"),
                sendBtn: document.getElementById("chatSendBtn"),
                cancelBtn: document.getElementById("chatCancelBtn"),
            };

            // 페이지 타이틀 업데이트
            document.title = "채팅 · Recap";
        };

        /**
         * 이벤트 리스너를 바인딩한다.
         */
        ChatView.prototype._bind = function () {
            var self = this;
            var els = self._els;

            // 입력 필드 값 변경 → 전송 버튼 활성화 제어 + 자동 높이 조정
            var onInput = function () {
                els.sendBtn.disabled = self._isSending || !els.chatInput.value.trim();
                els.chatInput.style.height = "auto";
                els.chatInput.style.height = Math.min(els.chatInput.scrollHeight, 120) + "px";
            };
            els.chatInput.addEventListener("input", onInput);
            self._listeners.push({ el: els.chatInput, type: "input", fn: onInput });

            // 키보드 이벤트: Enter 전송, Shift+Enter 줄바꿈
            // 한국어 IME composing 처리
            var onKeydown = function (e) {
                // IME 조합 중이면 무시 (한국어 입력 시 Enter가 조합 확정에 사용됨)
                if (e.isComposing || e.keyCode === 229) return;

                if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    if (!self._isSending && els.chatInput.value.trim()) {
                        self._sendMessage();
                    }
                }
            };
            els.chatInput.addEventListener("keydown", onKeydown);
            self._listeners.push({ el: els.chatInput, type: "keydown", fn: onKeydown });

            // 전송 버튼 클릭
            var onSend = function () {
                if (!self._isSending && els.chatInput.value.trim()) {
                    self._sendMessage();
                }
            };
            els.sendBtn.addEventListener("click", onSend);
            self._listeners.push({ el: els.sendBtn, type: "click", fn: onSend });

            // 취소 버튼 클릭
            var onCancel = function () {
                self._cancelSending();
            };
            els.cancelBtn.addEventListener("click", onCancel);
            self._listeners.push({ el: els.cancelBtn, type: "click", fn: onCancel });

            // 대화 초기화
            var onClear = function () {
                self._clearChat();
            };
            els.btnClearChat.addEventListener("click", onClear);
            self._listeners.push({ el: els.btnClearChat, type: "click", fn: onClear });

            // WebSocket 이벤트: 새 회의 추가/완료 시 드롭다운 자동 갱신
            var onJobCompleted = function () {
                self._refreshMeetingFilter();
            };
            document.addEventListener("ws:job_completed", onJobCompleted);
            self._listeners.push({ el: document, type: "ws:job_completed", fn: onJobCompleted });

            var onJobAdded = function () {
                self._refreshMeetingFilter();
            };
            document.addEventListener("ws:job_added", onJobAdded);
            self._listeners.push({ el: document, type: "ws:job_added", fn: onJobAdded });

            // 입력에 포커스
            els.chatInput.focus();
        };

        /**
         * 회의 목록을 드롭다운에 로드한다.
         */
        ChatView.prototype._loadMeetingList = async function () {
            var self = this;
            var els = self._els;
            try {
                var data = await App.apiRequest("/meetings");
                var meetings = data.meetings || [];

                meetings.forEach(function (meeting) {
                    var option = document.createElement("option");
                    option.value = meeting.meeting_id;

                    var statusLabel = {
                        completed: "\u2713",
                        recorded: "\u25CF",
                        recording: "\u25CF",
                        transcribing: "\u2022",
                        diarizing: "\u2022",
                        merging: "\u2022",
                        embedding: "\u2022",
                        queued: "\u2022",
                        failed: "\u2717",
                    };
                    var icon = statusLabel[meeting.status] || "";
                    option.textContent = icon + " " + meeting.meeting_id;
                    els.meetingFilter.appendChild(option);
                });
            } catch (e) {
                console.warn("회의 목록 로드 실패:", e.message);
            }
        };

        /**
         * 회의 필터 드롭다운을 갱신한다.
         */
        ChatView.prototype._refreshMeetingFilter = function () {
            var self = this;
            var els = self._els;
            var currentValue = els.meetingFilter.value;
            els.meetingFilter.innerHTML = '<option value="">전체 회의</option>';
            self._loadMeetingList().then(function () {
                if (currentValue) {
                    els.meetingFilter.value = currentValue;
                }
            });
        };

        /**
         * 환영 메시지를 숨긴다.
         */
        ChatView.prototype._hideWelcome = function () {
            if (this._els.welcomeMessage) {
                this._els.welcomeMessage.style.display = "none";
            }
        };

        /**
         * 사용자 메시지를 추가한다.
         * @param {string} text - 메시지 텍스트
         */
        ChatView.prototype._addUserMessage = function (text) {
            this._hideWelcome();
            this._messageCount++;

            var msg = document.createElement("div");
            msg.className = "message user";

            var avatar = document.createElement("div");
            avatar.className = "message-avatar";
            avatar.innerHTML = Icons.person;

            var body = document.createElement("div");
            body.className = "message-body";

            var bubble = document.createElement("div");
            bubble.className = "message-bubble";
            bubble.textContent = text;

            body.appendChild(bubble);
            msg.appendChild(avatar);
            msg.appendChild(body);
            this._els.messagesArea.appendChild(msg);

            this._scrollToBottom();
        };

        /**
         * AI 답변 메시지를 추가한다.
         * @param {Object} data - 응답 데이터
         */
        ChatView.prototype._addAssistantMessage = function (data) {
            var self = this;
            self._messageCount++;

            var msg = document.createElement("div");
            msg.className = "message assistant";

            // 아바타
            var avatar = document.createElement("div");
            avatar.className = "message-avatar";
            avatar.textContent = "\uD83E\uDD16";

            // 메시지 본체
            var body = document.createElement("div");
            body.className = "message-body";

            // 답변 버블
            var bubble = document.createElement("div");
            bubble.className = "message-bubble";
            bubble.innerHTML = App.renderMarkdown(data.answer);

            body.appendChild(bubble);

            // Phase 5: 라우터 출처 배지 — source_type 이 있을 때만 표시
            // (router_enabled=False 면 source_type=null 이라 배지 없음 — 기존 UX 보존)
            if (data.source_type === "wiki" || data.source_type === "both") {
                var badge = document.createElement("span");
                badge.className = "chat-source-badge";
                // text 기반 — 이모지 없이 명시적 라벨
                if (data.source_type === "wiki") {
                    badge.textContent = "위키 답변";
                    badge.setAttribute("title", "위키 페이지 누적 합성 답변");
                    badge.setAttribute("data-source", "wiki");
                } else {
                    badge.textContent = "통합 답변 (RAG + 위키)";
                    badge.setAttribute(
                        "title",
                        "RAG 검색과 위키 페이지를 모두 합친 답변"
                    );
                    badge.setAttribute("data-source", "both");
                }
                // 라우터 신뢰도 정보가 있으면 title 에 부가
                if (
                    data.router_verdict &&
                    typeof data.router_verdict.confidence === "number"
                ) {
                    var conf = data.router_verdict.confidence;
                    var reason = data.router_verdict.reason || "";
                    badge.setAttribute(
                        "title",
                        badge.getAttribute("title") +
                            " (신뢰도 " +
                            conf +
                            "/10" +
                            (reason ? ", " + App.escapeHtml(reason) : "") +
                            ")"
                    );
                }
                // 버블 바로 위(같은 body 컨테이너) 에 삽입
                body.insertBefore(badge, bubble);
            }

            // Phase 5: 위키 인용 출처 — wiki_sources 가 있을 때만 표시
            if (data.wiki_sources && data.wiki_sources.length > 0) {
                var wikiSection = document.createElement("div");
                wikiSection.className = "wiki-sources";

                var wikiTitle = document.createElement("div");
                wikiTitle.className = "wiki-sources-title";
                wikiTitle.textContent =
                    "위키 페이지 (" + data.wiki_sources.length + "건)";
                wikiSection.appendChild(wikiTitle);

                data.wiki_sources.forEach(function (src) {
                    var card = document.createElement("div");
                    card.className = "wiki-source-card";

                    var titleEl = document.createElement("div");
                    titleEl.className = "wiki-source-card-title";
                    titleEl.textContent = src.title || src.page_path || "";

                    var pathEl = document.createElement("div");
                    pathEl.className = "wiki-source-card-path";
                    pathEl.textContent = src.page_path || "";

                    var snippetEl = document.createElement("div");
                    snippetEl.className = "wiki-source-card-snippet";
                    snippetEl.textContent = src.snippet || "";

                    card.appendChild(titleEl);
                    card.appendChild(pathEl);
                    if (src.snippet) {
                        card.appendChild(snippetEl);
                    }
                    wikiSection.appendChild(card);
                });

                body.appendChild(wikiSection);
            }

            // LLM 미사용 경고
            if (!data.llm_used && data.error_message) {
                var notice = document.createElement("div");
                notice.className = "llm-fallback-notice";
                notice.textContent = "\u26A0 응답을 받지 못했어요: " + data.error_message;
                body.appendChild(notice);
            }

            // 참조 출처
            if (data.references && data.references.length > 0) {
                var refsSection = document.createElement("div");
                refsSection.className = "references";

                var refsTitle = document.createElement("div");
                refsTitle.className = "references-title";
                refsTitle.innerHTML = Icons.clip + ' 참조 출처 (' + data.references.length + '건)';
                refsSection.appendChild(refsTitle);

                data.references.forEach(function (ref, index) {
                    var card = document.createElement("a");
                    card.className = "ref-card";
                    // SPA 내비게이션으로 뷰어 이동
                    card.href = "/app/viewer/" + encodeURIComponent(ref.meeting_id);
                    card.addEventListener("click", function (e) {
                        e.preventDefault();
                        Router.navigate("/app/viewer/" + encodeURIComponent(ref.meeting_id));
                    });

                    // 인덱스
                    var indexEl = document.createElement("span");
                    indexEl.className = "ref-card-index";
                    indexEl.textContent = "[" + (index + 1) + "]";

                    // 본체
                    var bodyEl = document.createElement("div");
                    bodyEl.className = "ref-card-body";

                    // REFERENCE 오버라인 (레퍼런스 Chat.jsx 기준 citation 문서화)
                    var overline = document.createElement("div");
                    overline.className = "overline ref-card-overline";
                    overline.textContent = "REFERENCE";
                    bodyEl.appendChild(overline);

                    // 메타 정보
                    var meta = document.createElement("div");
                    meta.className = "ref-card-meta";

                    var meetingSpan = document.createElement("span");
                    meetingSpan.textContent = ref.meeting_id;

                    var dateSpan = document.createElement("span");
                    dateSpan.textContent = ref.date || "";

                    var speakersSpan = document.createElement("span");
                    speakersSpan.textContent = (ref.speakers || []).join(", ");

                    var timeSpan = document.createElement("span");
                    timeSpan.textContent = App.formatTime(ref.start_time) + "~" + App.formatTime(ref.end_time);

                    meta.appendChild(meetingSpan);
                    if (ref.date) meta.appendChild(dateSpan);
                    if (ref.speakers && ref.speakers.length) meta.appendChild(speakersSpan);
                    meta.appendChild(timeSpan);

                    // 미리보기
                    var preview = document.createElement("div");
                    preview.className = "ref-card-preview";
                    preview.textContent = ref.text_preview || "";

                    bodyEl.appendChild(meta);
                    bodyEl.appendChild(preview);

                    // 점수
                    var scoreEl = document.createElement("span");
                    scoreEl.className = "ref-card-score";
                    scoreEl.textContent = (ref.score * 100).toFixed(0) + "%";

                    card.appendChild(indexEl);
                    card.appendChild(bodyEl);
                    card.appendChild(scoreEl);
                    refsSection.appendChild(card);
                });

                body.appendChild(refsSection);
            }

            // 복사 버튼
            var actions = document.createElement("div");
            actions.className = "message-actions";
            var copyBtn = document.createElement("button");
            copyBtn.className = "btn-copy";
            copyBtn.innerHTML = Icons.copy + ' 복사';
            copyBtn.setAttribute("aria-label", "답변 복사");
            copyBtn.addEventListener("click", function () {
                var textToCopy = data.answer || bubble.textContent;
                App.copyToClipboard(textToCopy).then(function (ok) {
                    if (ok) {
                        copyBtn.innerHTML = Icons.check + ' 복사됨';
                        copyBtn.classList.add("copied");
                        setTimeout(function () {
                            copyBtn.innerHTML = Icons.copy + ' 복사';
                            copyBtn.classList.remove("copied");
                        }, 2000);
                    }
                });
            });
            actions.appendChild(copyBtn);
            body.appendChild(actions);

            msg.appendChild(avatar);
            msg.appendChild(body);
            self._els.messagesArea.appendChild(msg);

            self._scrollToBottom();
        };

        /**
         * 메시지 영역을 맨 아래로 스크롤한다.
         */
        ChatView.prototype._scrollToBottom = function () {
            var messagesArea = this._els.messagesArea;
            requestAnimationFrame(function () {
                messagesArea.scrollTop = messagesArea.scrollHeight;
            });
        };

        /**
         * 전송 상태를 설정한다.
         * @param {boolean} sending - 전송 중 여부
         */
        ChatView.prototype._setSending = function (sending) {
            var els = this._els;
            this._isSending = sending;
            els.sendBtn.disabled = sending || !els.chatInput.value.trim();
            els.chatInput.disabled = sending;
            els.typingIndicator.classList.toggle("visible", sending);

            if (sending) {
                els.sendBtn.style.display = "none";
                els.cancelBtn.classList.add("visible");
                this._scrollToBottom();
            } else {
                els.sendBtn.style.display = "";
                els.cancelBtn.classList.remove("visible");
            }
        };

        /**
         * 채팅 메시지를 전송한다.
         */
        ChatView.prototype._sendMessage = async function () {
            var self = this;
            var els = self._els;
            var query = els.chatInput.value.trim();
            if (!query) return;

            // 입력 초기화
            els.chatInput.value = "";
            els.chatInput.style.height = "auto";
            errorBanner.hide();

            // 사용자 메시지 표시
            self._addUserMessage(query);

            // 전송 상태
            self._setSending(true);

            // AbortController 생성
            self._currentAbortController = new AbortController();

            try {
                var meetingIdFilter = els.meetingFilter.value || null;

                var result = await App.apiRequest("/chat", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        query: query,
                        session_id: self._sessionId,
                        meeting_id_filter: meetingIdFilter,
                        date_filter: null,
                        speaker_filter: null,
                    }),
                    signal: self._currentAbortController.signal,
                });

                // 빈 응답 처리
                if (!result || (!result.answer && (!result.references || result.references.length === 0))) {
                    result = result || {};
                    result.answer = result.answer || "관련 회의 내용을 찾을 수 없습니다. 다른 키워드로 질문해 보세요.";
                }

                // AI 답변 표시
                self._addAssistantMessage(result);

            } catch (e) {
                // 사용자가 직접 취소한 경우
                if (e.name === "AbortError") return;

                if (e.status === 503) {
                    errorBanner.show("아직 답변 준비가 덜 됐어요. 잠시 후 다시 시도해 주세요.");
                } else if (e.status === 400) {
                    errorBanner.show("입력 내용을 확인해 주세요: " + e.message);
                } else if (e.status === 0) {
                    errorBanner.show("서버에 연결할 수 없습니다. 네트워크 상태를 확인해 주세요.");
                } else {
                    errorBanner.show("답변 생성에 실패했습니다. 잠시 후 다시 시도해 주세요.");
                }
            } finally {
                self._currentAbortController = null;
                self._setSending(false);
                els.chatInput.focus();
            }
        };

        /**
         * 진행 중인 AI 응답 요청을 취소한다.
         */
        ChatView.prototype._cancelSending = function () {
            if (this._currentAbortController) {
                this._currentAbortController.abort();
                this._currentAbortController = null;
            }
            this._setSending(false);
            this._els.chatInput.focus();
        };

        /**
         * 대화를 초기화한다.
         */
        ChatView.prototype._clearChat = function () {
            var self = this;
            var els = self._els;

            // 새 세션 ID 생성
            self._sessionId = self._generateSessionId();

            // 메시지 영역 초기화
            els.messagesArea.innerHTML = "";
            self._messageCount = 0;

            // 환영 메시지 복원
            els.messagesArea.innerHTML = self._createWelcomeHtml();
            els.welcomeMessage = document.getElementById("chatWelcomeMessage");

            errorBanner.hide();
            els.chatInput.focus();
        };

        /**
         * 뷰를 정리한다.
         */
        ChatView.prototype.destroy = function () {
            // 진행 중인 요청 취소
            if (this._currentAbortController) {
                this._currentAbortController.abort();
                this._currentAbortController = null;
            }

            // 이벤트 리스너 해제
            this._listeners.forEach(function (entry) {
                entry.el.removeEventListener(entry.type, entry.fn);
            });
            this._listeners = [];

            // 타이머 해제
            this._timers.forEach(function (t) { clearInterval(t); clearTimeout(t); });
            this._timers = [];

            // chat-mode 클래스 제거 (리스트 패널 복원)
            var listPanel = document.getElementById("list-panel");
            if (listPanel) listPanel.classList.remove("chat-mode");

            // 페이지 타이틀 복원
            document.title = "회의록 · Recap";
        };

        return ChatView;
    }

    window.MeetingChatView = {
        create: create,
    };
})();
