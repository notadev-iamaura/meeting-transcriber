/* =================================================================
 * Recap API client boundary
 *
 * 목적: fetch/error handling 을 SPA view 코드에서 분리한다.
 * 공개 API: window.MeetingApi
 * ================================================================= */
(function () {
    "use strict";

    var API_BASE = "/api";

    var HTTP_ERROR_MESSAGES = {
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

    function getHttpErrorMessage(status, fallback) {
        return HTTP_ERROR_MESSAGES[status] || fallback || "알 수 없는 오류가 발생했습니다.";
    }

    function buildApiUrl(endpoint) {
        if (typeof endpoint !== "string") {
            throw new TypeError("endpoint must be a string");
        }
        if (endpoint.indexOf(API_BASE + "/") === 0) {
            return endpoint;
        }
        if (endpoint.charAt(0) !== "/") {
            return API_BASE + "/" + endpoint;
        }
        return API_BASE + endpoint;
    }

    async function request(endpoint, options) {
        var response;
        try {
            response = await fetch(buildApiUrl(endpoint), options || {});
        } catch (networkError) {
            if (networkError && networkError.name === "AbortError") {
                throw networkError;
            }
            var networkErr = new Error("서버에 연결할 수 없습니다. 네트워크 상태를 확인해 주세요.");
            networkErr.status = 0;
            throw networkErr;
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
        if (response.status === 204 || response.headers.get("content-length") === "0") {
            return null;
        }
        var contentType = response.headers.get("content-type") || "";
        if (contentType.indexOf("application/json") !== -1) {
            return response.json();
        }
        return response.text();
    }

    function post(endpoint, body) {
        return request(endpoint, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
    }

    function deleteRequest(endpoint) {
        return request(endpoint, { method: "DELETE" });
    }

    window.MeetingApi = {
        API_BASE: API_BASE,
        HTTP_ERROR_MESSAGES: HTTP_ERROR_MESSAGES,
        buildApiUrl: buildApiUrl,
        getHttpErrorMessage: getHttpErrorMessage,
        request: request,
        post: post,
        delete: deleteRequest,
    };
})();
