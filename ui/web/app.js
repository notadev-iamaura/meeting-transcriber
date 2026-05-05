/* =================================================================
 * 회의 전사 시스템 — 공통 JavaScript 모듈 (app.js)
 *
 * 목적: 모든 웹 UI 페이지에서 공유하는 유틸리티 함수, API 요청,
 *       에러 배너 관리, 마크다운 파서, WebSocket 연결 관리를 제공한다.
 * 의존성: /api/* API 엔드포인트, /ws/events WebSocket 엔드포인트
 * 공개 API: window.MeetingApp 네임스페이스
 * ================================================================= */
(function () {
    "use strict";

    // === 상수 ===
    var ApiClient = window.MeetingApi || null;
    var API_BASE = ApiClient && ApiClient.API_BASE ? ApiClient.API_BASE : "/api";

    // 상태별 한국어 레이블 (회의 상태 표시에 사용)
    var STATUS_LABELS = {
        completed: "완료",
        recorded: "녹음 완료",
        recording: "녹음 중",
        transcribing: "전사 중",
        diarizing: "화자분리 중",
        merging: "병합 중",
        embedding: "임베딩 중",
        queued: "대기 중",
        failed: "실패",
    };

    // 파이프라인 6단계 정의 (순서 보장, 한국어 레이블 포함)
    var PIPELINE_STEPS = [
        { key: "convert",    label: "변환",  labelFull: "오디오 변환" },
        { key: "transcribe", label: "전사",  labelFull: "음성 인식" },
        { key: "diarize",    label: "화자",  labelFull: "화자 분리" },
        { key: "merge",      label: "병합",  labelFull: "결과 병합" },
        { key: "correct",    label: "보정",  labelFull: "텍스트 보정" },
        { key: "summarize",  label: "요약",  labelFull: "회의록 생성" },
    ];

    // 화자별 CSS 변수 색상 매핑 (최대 10명)
    var SPEAKER_COLORS = [
        "var(--speaker-0)", "var(--speaker-1)", "var(--speaker-2)",
        "var(--speaker-3)", "var(--speaker-4)", "var(--speaker-5)",
        "var(--speaker-6)", "var(--speaker-7)", "var(--speaker-8)",
        "var(--speaker-9)",
    ];


    // =================================================================
    // === 유틸리티 함수 ===
    // =================================================================

    /**
     * 초 단위 시간을 MM:SS 또는 HH:MM:SS 형식으로 변환한다.
     * @param {number} seconds - 초
     * @returns {string} 포맷된 시간 문자열
     */
    function formatTime(seconds) {
        if (seconds == null || isNaN(seconds)) return "--:--";
        var totalSec = Math.floor(seconds);
        var h = Math.floor(totalSec / 3600);
        var m = Math.floor((totalSec % 3600) / 60);
        var s = totalSec % 60;
        var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
        if (h > 0) return h + ":" + pad(m) + ":" + pad(s);
        return pad(m) + ":" + pad(s);
    }

    /**
     * ISO 날짜 문자열을 한국어 날짜 형식(YYYY-MM-DD HH:MM)으로 포맷한다.
     * @param {string} dateStr - ISO 날짜 문자열
     * @returns {string} 포맷된 날짜
     */
    function formatDate(dateStr) {
        if (!dateStr) return "-";
        try {
            var d = new Date(dateStr);
            if (isNaN(d.getTime())) return dateStr;
            var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
            return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" +
                pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
        } catch (e) {
            return dateStr;
        }
    }

    /**
     * HTML 특수 문자를 이스케이프한다 (XSS 방지).
     * @param {string} text - 원본 텍스트
     * @returns {string} 이스케이프된 텍스트
     */
    function escapeHtml(text) {
        return text
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    /**
     * 엘리먼트에 안전하게 텍스트를 설정한다 (XSS 방지).
     * @param {HTMLElement} el - 대상 엘리먼트
     * @param {string} text - 설정할 텍스트
     */
    function safeText(el, text) {
        if (el) el.textContent = text;
    }

    /**
     * 파일 경로에서 파일명만 추출한다.
     * @param {string} path - 전체 경로
     * @returns {string} 파일명
     */
    function getFileName(path) {
        if (!path) return "-";
        var parts = path.split("/");
        return parts[parts.length - 1];
    }

    /**
     * 상태 문자열을 한국어 레이블로 변환한다.
     * @param {string} status - 영문 상태
     * @returns {string} 한국어 레이블
     */
    function getStatusLabel(status) {
        return STATUS_LABELS[status] || status;
    }


    // =================================================================
    // === API 요청 ===
    // =================================================================

    // HTTP 상태 코드별 한국어 에러 메시지 매핑
    var HTTP_ERROR_MESSAGES = (ApiClient && ApiClient.HTTP_ERROR_MESSAGES) || {
        400: "잘못된 요청입니다. 입력 내용을 확인해 주세요.",
        401: "인증이 필요합니다.",
        403: "접근 권한이 없습니다.",
        404: "요청한 데이터를 찾을 수 없습니다.",
        408: "요청 시간이 초과되었습니다. 다시 시도해 주세요.",
        429: "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
        500: "서버 내부 오류가 발생했습니다.",
        502: "서버에 연결할 수 없습니다.",
        503: "서비스가 일시적으로 이용 불가합니다. 잠시 후 다시 시도해 주세요.",
        504: "서버 응답 시간이 초과되었습니다.",
    };

    /**
     * HTTP 상태 코드에 해당하는 한국어 에러 메시지를 반환한다.
     * @param {number} status - HTTP 상태 코드
     * @param {string} [fallback] - 매핑되지 않은 경우 사용할 기본 메시지
     * @returns {string} 한국어 에러 메시지
     */
    function getHttpErrorMessage(status, fallback) {
        if (ApiClient && typeof ApiClient.getHttpErrorMessage === "function") {
            return ApiClient.getHttpErrorMessage(status, fallback);
        }
        return HTTP_ERROR_MESSAGES[status] || fallback || "알 수 없는 오류가 발생했습니다.";
    }

    /**
     * API 요청을 수행한다 (GET/POST 모두 지원).
     * @param {string} endpoint - API 경로 (예: "/status", "/chat")
     * @param {Object} [options] - fetch 옵션 (method, headers, body 등)
     * @returns {Promise<Object>} 응답 JSON
     * @throws {Error} API 에러 (status 속성 포함)
     */
    async function apiRequest(endpoint, options) {
        if (ApiClient && typeof ApiClient.request === "function") {
            return ApiClient.request(endpoint, options);
        }
        var url = API_BASE + endpoint;
        var response;
        try {
            response = await fetch(url, options || {});
        } catch (networkError) {
            // 네트워크 오류 (서버 미연결, 오프라인 등) 한국어 처리
            var err = new Error("서버에 연결할 수 없습니다. 네트워크 상태를 확인해 주세요.");
            err.status = 0;
            throw err;
        }
        if (!response.ok) {
            var errorData;
            try {
                errorData = await response.json();
            } catch (e) {
                errorData = { detail: response.statusText };
            }
            var detail = errorData.detail || getHttpErrorMessage(response.status);
            var err = new Error(detail);
            err.status = response.status;
            throw err;
        }
        // 204 No Content 등 body 가 없는 응답 처리
        if (response.status === 204 || response.headers.get("content-length") === "0") {
            return null;
        }
        return response.json();
    }

    /**
     * API POST 요청을 수행한다.
     * @param {string} endpoint - API 경로
     * @param {Object} body - 요청 본문 (JSON으로 직렬화됨)
     * @returns {Promise<Object>} 응답 JSON
     */
    async function apiPost(endpoint, body) {
        if (ApiClient && typeof ApiClient.post === "function") {
            return ApiClient.post(endpoint, body);
        }
        return apiRequest(endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
    }


    // =================================================================
    // === 에러 배너 관리 ===
    // =================================================================

    /**
     * 에러 배너를 초기화한다. 닫기 버튼 이벤트를 바인딩한다.
     * @param {string} bannerId - 배너 엘리먼트 ID
     * @param {string} messageId - 메시지 텍스트 엘리먼트 ID
     * @param {string} closeId - 닫기 버튼 엘리먼트 ID
     * @returns {Object} { show, hide } 함수를 가진 객체
     */
    function initErrorBanner(bannerId, messageId, closeId) {
        var banner = document.getElementById(bannerId);
        var message = document.getElementById(messageId);
        var closeBtn = document.getElementById(closeId);

        function show(text) {
            safeText(message, text);
            if (banner) banner.classList.add("visible");
        }

        function hide() {
            if (banner) banner.classList.remove("visible");
        }

        if (closeBtn) {
            closeBtn.addEventListener("click", hide);
        }

        return { show: show, hide: hide };
    }


    // =================================================================
    // === 간이 마크다운 파서 ===
    // =================================================================

    /**
     * 마크다운 텍스트를 안전한 HTML로 변환한다.
     * 지원: ## 제목, ### 부제목, - 리스트, 1. 순서 리스트,
     *       **볼드**, *이탤릭*, `코드`, - [ ] 체크박스
     * @param {string} md - 마크다운 텍스트
     * @returns {string} HTML 문자열
     */
    function renderMarkdown(md) {
        if (!md) return "";

        var lines = md.split("\n");
        var html = [];
        var inList = false;
        var inOl = false;

        function closeList() {
            if (inList) { html.push("</ul>"); inList = false; }
            if (inOl) { html.push("</ol>"); inOl = false; }
        }

        /** 인라인 포맷 변환 (코드, 볼드, 이탤릭) */
        function inlineFormat(text) {
            text = text.replace(/`([^`]+)`/g, "<code>$1</code>");
            text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
            text = text.replace(/\*([^*]+)\*/g, "<em>$1</em>");
            return text;
        }

        lines.forEach(function (line) {
            var trimmed = line.trim();

            // 빈 줄
            if (!trimmed) {
                closeList();
                return;
            }

            // ## 제목
            var h2Match = trimmed.match(/^##\s+(.+)/);
            if (h2Match) {
                closeList();
                html.push("<h2>" + inlineFormat(escapeHtml(h2Match[1])) + "</h2>");
                return;
            }

            // ### 부제목
            var h3Match = trimmed.match(/^###\s+(.+)/);
            if (h3Match) {
                closeList();
                html.push("<h3>" + inlineFormat(escapeHtml(h3Match[1])) + "</h3>");
                return;
            }

            // 체크박스 - [ ] 또는 - [x]
            var checkMatch = trimmed.match(/^-\s+\[([ xX])\]\s+(.*)/);
            if (checkMatch) {
                if (!inList) {
                    html.push("<ul>");
                    inList = true;
                }
                var checked = checkMatch[1].toLowerCase() === "x" ? " checked" : "";
                html.push(
                    '<li class="checkbox-item"><input type="checkbox" disabled' +
                    checked + "> " + inlineFormat(escapeHtml(checkMatch[2])) + "</li>"
                );
                return;
            }

            // 비순서 리스트 (- 또는 * 항목)
            var ulMatch = trimmed.match(/^[-*]\s+(.*)/);
            if (ulMatch) {
                if (inOl) { closeList(); }
                if (!inList) {
                    html.push("<ul>");
                    inList = true;
                }
                html.push("<li>" + inlineFormat(escapeHtml(ulMatch[1])) + "</li>");
                return;
            }

            // 순서 리스트 (1. 항목)
            var olMatch = trimmed.match(/^\d+\.\s+(.*)/);
            if (olMatch) {
                if (inList) { closeList(); }
                if (!inOl) {
                    html.push("<ol>");
                    inOl = true;
                }
                html.push("<li>" + inlineFormat(escapeHtml(olMatch[1])) + "</li>");
                return;
            }

            // 들여쓰기 서브 리스트 항목
            var subMatch = trimmed.match(/^\s{2,}[-*]\s+(.*)/);
            if (subMatch && (inList || inOl)) {
                html.push('<li style="margin-left:20px">' +
                    inlineFormat(escapeHtml(subMatch[1])) + "</li>");
                return;
            }

            // 일반 단락
            closeList();
            html.push("<p>" + inlineFormat(escapeHtml(trimmed)) + "</p>");
        });

        closeList();
        return html.join("\n");
    }


    // =================================================================
    // === 검색어 하이라이팅 ===
    // =================================================================

    /**
     * 텍스트에서 검색어를 <mark> 태그로 감싸 하이라이팅한다.
     * XSS 방지: HTML 이스케이프 후 하이라이팅 마크업 삽입.
     * @param {string} text - 원본 텍스트
     * @param {string} query - 검색어
     * @returns {string} 하이라이팅된 HTML
     */
    function highlightText(text, query) {
        if (!query || !text) return escapeHtml(text || "");

        var escaped = escapeHtml(text);
        var escapedQuery = escapeHtml(query);
        // 정규식 특수문자 이스케이프
        var regexSafe = escapedQuery.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

        try {
            var regex = new RegExp("(" + regexSafe + ")", "gi");
            return escaped.replace(regex, "<mark>$1</mark>");
        } catch (e) {
            return escaped;
        }
    }


    // =================================================================
    // === WebSocket 연결 관리 ===
    // =================================================================

    var _ws = null;              // WebSocket 인스턴스
    var _wsReconnectAttempts = 0; // 재연결 시도 횟수
    var _wsReconnectTimer = null; // 재연결 타이머 ID
    var _wsConnected = false;     // 연결 상태

    // WebSocket 설정 상수
    var _WS_INITIAL_DELAY = 1000;   // 초기 재연결 지연 (1초)
    var _WS_MAX_DELAY = 30000;      // 최대 재연결 지연 (30초)
    var _WS_MAX_RETRIES = 0;        // 0 = 무제한 재시도 (로컬 환경)

    /**
     * WebSocket 서버에 연결한다.
     * 자동 재연결(지수 백오프)과 이벤트 디스패치를 포함한다.
     *
     * 수신한 이벤트는 document에 CustomEvent로 디스패치된다:
     *   - 이벤트 이름: "ws:" + event_type (예: "ws:pipeline_status")
     *   - detail: 이벤트 데이터 객체
     *
     * 연결 상태 변경 시 document에 "ws:connection" 이벤트를 디스패치한다:
     *   - detail: { connected: boolean }
     *
     * 사용 예:
     *   MeetingApp.connectWebSocket();
     *   document.addEventListener("ws:pipeline_status", function(e) {
     *       console.log(e.detail);
     *   });
     */
    function connectWebSocket() {
        // 이미 연결 중이면 무시
        if (_ws && (_ws.readyState === WebSocket.CONNECTING ||
                    _ws.readyState === WebSocket.OPEN)) {
            return;
        }

        // WebSocket URL 구성 (현재 호스트 기준 상대 경로)
        var protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        var wsUrl = protocol + "//" + window.location.host + "/ws/events";

        try {
            _ws = new WebSocket(wsUrl);
        } catch (e) {
            _scheduleReconnect();
            return;
        }

        // --- 연결 성공 ---
        _ws.onopen = function () {
            _wsReconnectAttempts = 0;
            _wsConnected = true;

            // 연결 상태 이벤트 디스패치
            document.dispatchEvent(new CustomEvent("ws:connection", {
                detail: { connected: true },
            }));
        };

        // --- 메시지 수신 ---
        _ws.onmessage = function (event) {
            try {
                var data = JSON.parse(event.data);
                var eventType = data.event_type || "unknown";

                // CustomEvent로 디스패치 (예: "ws:pipeline_status")
                document.dispatchEvent(new CustomEvent("ws:" + eventType, {
                    detail: data.data || {},
                }));

                // 전체 이벤트도 디스패치 (범용 리스너용)
                document.dispatchEvent(new CustomEvent("ws:event", {
                    detail: data,
                }));

            } catch (e) {
                // 파싱 실패한 메시지는 무시 (로깅만)
                console.warn("WebSocket 메시지 파싱 실패:", e);
            }
        };

        // --- 연결 종료 ---
        _ws.onclose = function (event) {
            _wsConnected = false;

            // 연결 해제 이벤트 디스패치
            document.dispatchEvent(new CustomEvent("ws:connection", {
                detail: { connected: false, code: event.code, reason: event.reason },
            }));

            // 정상 종료가 아닌 경우에만 재연결
            if (event.code !== 1000) {
                _scheduleReconnect();
            }
        };

        // --- 에러 ---
        _ws.onerror = function () {
            // onclose가 자동으로 호출되므로 여기서는 추가 처리 불필요
        };
    }

    /**
     * 지수 백오프로 재연결을 스케줄링한다.
     * 지연 시간: min(초기지연 * 2^시도횟수, 최대지연)
     */
    function _scheduleReconnect() {
        if (_wsReconnectTimer) return; // 이미 스케줄됨

        // 최대 재시도 횟수 확인 (0 = 무제한)
        if (_WS_MAX_RETRIES > 0 && _wsReconnectAttempts >= _WS_MAX_RETRIES) {
            return;
        }

        var delay = Math.min(
            _WS_INITIAL_DELAY * Math.pow(2, _wsReconnectAttempts),
            _WS_MAX_DELAY
        );

        _wsReconnectTimer = setTimeout(function () {
            _wsReconnectTimer = null;
            _wsReconnectAttempts++;
            connectWebSocket();
        }, delay);
    }

    /**
     * WebSocket 연결을 종료한다.
     * 재연결 타이머도 취소한다.
     */
    function disconnectWebSocket() {
        if (_wsReconnectTimer) {
            clearTimeout(_wsReconnectTimer);
            _wsReconnectTimer = null;
        }

        if (_ws) {
            _ws.onclose = null; // 재연결 방지
            _ws.close(1000, "클라이언트 종료");
            _ws = null;
        }

        _wsConnected = false;
    }

    /**
     * WebSocket 연결 상태를 반환한다.
     * @returns {boolean} 연결 여부
     */
    function isWebSocketConnected() {
        return _wsConnected;
    }


    // =================================================================
    // === 공개 API ===
    // =================================================================

    /**
     * API DELETE 요청을 수행한다.
     * @param {string} endpoint - API 경로
     * @returns {Promise<Object>} 응답 JSON
     */
    async function apiDelete(endpoint) {
        if (ApiClient && typeof ApiClient.delete === "function") {
            return ApiClient.delete(endpoint);
        }
        return apiRequest(endpoint, {
            method: "DELETE",
        });
    }

    /**
     * 텍스트를 클립보드에 복사한다.
     * @param {string} text - 복사할 텍스트
     * @returns {Promise<boolean>} 복사 성공 여부
     */
    async function copyToClipboard(text) {
        try {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                await navigator.clipboard.writeText(text);
                return true;
            }
            // 폴백: 임시 textarea 사용 (구형 브라우저)
            var textarea = document.createElement("textarea");
            textarea.value = text;
            textarea.style.position = "fixed";
            textarea.style.opacity = "0";
            document.body.appendChild(textarea);
            textarea.select();
            var ok = document.execCommand("copy");
            document.body.removeChild(textarea);
            return ok;
        } catch (e) {
            return false;
        }
    }

    /**
     * 로딩 스켈레톤 카드를 생성한다.
     * @param {number} count - 생성할 스켈레톤 카드 수
     * @returns {DocumentFragment} 스켈레톤 카드 프래그먼트
     */
    function createSkeletonCards(count) {
        var fragment = document.createDocumentFragment();
        for (var i = 0; i < count; i++) {
            var card = document.createElement("div");
            card.className = "skeleton-card";
            card.setAttribute("aria-hidden", "true");

            var line1 = document.createElement("div");
            line1.className = "skeleton-line short";
            var line2 = document.createElement("div");
            line2.className = "skeleton-line medium";
            var line3 = document.createElement("div");
            line3.className = "skeleton-line";

            card.appendChild(line1);
            card.appendChild(line2);
            card.appendChild(line3);
            fragment.appendChild(card);
        }
        return fragment;
    }

    /**
     * 회의 표시용 제목을 반환한다.
     * 사용자 정의 title 이 있으면 그대로, 없으면 meeting_id 의 타임스탬프를 파싱.
     * @param {Object} meeting - { meeting_id, created_at, title }
     * @returns {string} 표시용 제목
     */
    function extractMeetingTitle(meeting) {
        if (!meeting) return "-";
        // 사용자 정의 title 우선
        if (meeting.title && meeting.title.trim()) {
            return meeting.title.trim();
        }
        // meeting_YYYYMMDD_HHMMSS 패턴 매칭
        var mid = meeting.meeting_id || "";
        var match = mid.match(/(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/);
        if (match) {
            return (
                match[1] + "-" + match[2] + "-" + match[3] + " " +
                match[4] + ":" + match[5]
            );
        }
        // 폴백: created_at
        if (meeting.created_at) {
            return formatDate(meeting.created_at);
        }
        return mid || "-";
    }

    window.MeetingApp = {
        // 상수
        API_BASE: API_BASE,
        STATUS_LABELS: STATUS_LABELS,
        SPEAKER_COLORS: SPEAKER_COLORS,
        PIPELINE_STEPS: PIPELINE_STEPS,
        HTTP_ERROR_MESSAGES: HTTP_ERROR_MESSAGES,

        // 유틸리티
        formatTime: formatTime,
        formatDate: formatDate,
        escapeHtml: escapeHtml,
        safeText: safeText,
        getFileName: getFileName,
        getStatusLabel: getStatusLabel,
        getHttpErrorMessage: getHttpErrorMessage,
        highlightText: highlightText,
        copyToClipboard: copyToClipboard,
        createSkeletonCards: createSkeletonCards,
        extractMeetingTitle: extractMeetingTitle,

        // API 요청
        apiRequest: apiRequest,
        apiPost: apiPost,
        apiDelete: apiDelete,

        // UI 컴포넌트
        initErrorBanner: initErrorBanner,
        renderMarkdown: renderMarkdown,

        // WebSocket
        connectWebSocket: connectWebSocket,
        disconnectWebSocket: disconnectWebSocket,
        isWebSocketConnected: isWebSocketConnected,
    };

})();
