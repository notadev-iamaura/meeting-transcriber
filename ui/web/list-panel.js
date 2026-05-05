/* =================================================================
 * Recap ListPanel boundary
 *
 * 목적: 회의 목록/선택/녹음 HUD/가져오기 패널을 SPA 라우터 본문에서 분리한다.
 * 공개 API: window.MeetingListPanel
 * ================================================================= */
(function () {
    "use strict";

    function create(deps) {
        deps = deps || {};
        var App = deps.App || window.MeetingApp;
        var Router = deps.Router || (window.SPA && window.SPA.Router);
        var errorBanner = deps.errorBanner || { show: function () {} };
        var STATUS_SORT_ORDER = deps.STATUS_SORT_ORDER || {};
        var STATUS_POLL_INTERVAL = deps.STATUS_POLL_INTERVAL || 5000;
        var MEETINGS_POLL_INTERVAL = deps.MEETINGS_POLL_INTERVAL || 15000;

        if (!App || !Router) {
            throw new Error("MeetingListPanel requires App and Router");
        }

        var ListPanel = (function () {
            var _meetings = [];           // 전체 회의 목록 데이터
            var _activeId = null;         // 현재 활성화된 회의 ID
            var _listEl = null;           // #listContent 엘리먼트
            var _searchEl = null;         // #listSearchInput 엘리먼트
            var _sortEl = null;           // #listSortSelect 엘리먼트
            var _countEl = null;          // #listCount 엘리먼트
            var _statusDot = null;        // #statusDot 엘리먼트
            var _statusText = null;       // #statusText 엘리먼트
            var _statusTimer = null;      // 상태 폴링 타이머
            var _meetingsTimer = null;    // 회의 목록 폴링 타이머
            var _searchTimeout = null;    // 검색 디바운스 타이머

            // 다중 선택 상태 (bulk-actions §A)
            // _selectedIds: 현재 체크된 meeting_id 의 Set
            // _lastClickedId: Shift+클릭 범위 선택의 앵커 (마지막 단일 토글 id)
            var _selectedIds = new Set();
            var _lastClickedId = null;

            /**
             * 선택 변경 시 호출 — UI 동기화 + recap:selection-changed 이벤트 발행.
             * BulkActionBar 가 이 이벤트를 받아 슬라이드 다운/업.
             */
            function _emitSelectionChanged() {
                var ids = [];
                _selectedIds.forEach(function (id) { ids.push(id); });
                _syncSelectionUI();
                try {
                    document.dispatchEvent(new CustomEvent("recap:selection-changed", {
                        detail: { count: ids.length, ids: ids },
                    }));
                } catch (_) { /* 구버전 브라우저 폴백 — 무시 */ }
            }

            /**
             * 모든 .meeting-item 의 aria-checked / .selected 클래스를
             * _selectedIds 와 동기화. 옵션 B (ARIA-only) — span 체크박스는
             * aria-hidden 이고 부모 .meeting-item 의 aria-checked 가 SR 단일 진실.
             */
            function _syncSelectionUI() {
                if (!_listEl) return;
                var items = _listEl.querySelectorAll(".meeting-item");
                items.forEach(function (item) {
                    var id = item.getAttribute("data-meeting-id");
                    var checked = id != null && _selectedIds.has(id);
                    item.classList.toggle("selected", checked);
                    item.setAttribute("aria-checked", checked ? "true" : "false");
                });
                // 부모 컨테이너에 selection-mode 클래스 토글 (체크박스 항상 표시 등 CSS 대상)
                if (_selectedIds.size > 0) {
                    _listEl.classList.add("selection-mode-active");
                    _listEl.classList.add("meetings-list--selecting");
                } else {
                    _listEl.classList.remove("selection-mode-active");
                    _listEl.classList.remove("meetings-list--selecting");
                }
            }

            /**
             * 단일 항목 토글.
             */
            function _toggleSelection(id) {
                if (_selectedIds.has(id)) {
                    _selectedIds.delete(id);
                } else {
                    _selectedIds.add(id);
                }
                _lastClickedId = id;
                _emitSelectionChanged();
            }

            /**
             * Shift+클릭 범위 선택 — 앵커(_lastClickedId) 부터 현재 항목까지 모두 선택.
             */
            function _selectRange(fromId, toId) {
                if (!_listEl) return;
                var items = Array.prototype.slice.call(
                    _listEl.querySelectorAll(".meeting-item")
                );
                var fromIdx = -1;
                var toIdx = -1;
                items.forEach(function (it, idx) {
                    var mid = it.getAttribute("data-meeting-id");
                    if (mid === fromId) fromIdx = idx;
                    if (mid === toId) toIdx = idx;
                });
                if (fromIdx < 0 || toIdx < 0) {
                    // 앵커 없으면 단순 토글
                    _toggleSelection(toId);
                    return;
                }
                var lo = Math.min(fromIdx, toIdx);
                var hi = Math.max(fromIdx, toIdx);
                for (var i = lo; i <= hi; i++) {
                    var mid2 = items[i].getAttribute("data-meeting-id");
                    if (mid2) _selectedIds.add(mid2);
                }
                _lastClickedId = toId;
                _emitSelectionChanged();
            }

            /**
             * 외부에서 선택 전체 해제 (BulkActionBar dismiss / 액션 후 자동 종료).
             */
            function clearSelection() {
                if (_selectedIds.size === 0) return;
                _selectedIds.clear();
                _lastClickedId = null;
                _emitSelectionChanged();
            }

            /**
             * 외부에서 현재 선택된 id 목록 조회 (BulkActionBar 액션 클릭 시).
             */
            function getSelectedIds() {
                var arr = [];
                _selectedIds.forEach(function (id) { arr.push(id); });
                return arr;
            }

            /**
             * 리스트 패널을 초기화한다.
             */
            function init() {
                _listEl = document.getElementById("listContent");
                _searchEl = document.getElementById("listSearchInput");
                _sortEl = document.getElementById("listSortSelect");
                _countEl = document.getElementById("listCount");
                _statusDot = document.getElementById("statusDot");
                _statusText = document.getElementById("statusText");

                // 검색 입력 디바운스
                if (_searchEl) {
                    _searchEl.addEventListener("input", function () {
                        clearTimeout(_searchTimeout);
                        _searchTimeout = setTimeout(function () {
                            _applyFilterAndSort();
                        }, 250);
                    });
                }

                // 정렬 변경
                if (_sortEl) {
                    _sortEl.addEventListener("change", function () {
                        _applyFilterAndSort();
                    });
                }

                // WebSocket 이벤트 리스닝 — 회의 목록 자동 갱신
                document.addEventListener("ws:job_completed", function () {
                    loadMeetings();
                });
                document.addEventListener("ws:job_added", function () {
                    loadMeetings();
                });

                // WebSocket 연결 상태 표시
                document.addEventListener("ws:connection", function (e) {
                    if (e.detail.connected) {
                        _statusDot.className = "status-dot connected";
                        App.safeText(_statusText, "연결됨");
                    } else {
                        _statusDot.className = "status-dot disconnected";
                        App.safeText(_statusText, "연결 끊김 — 재연결 중...");
                    }
                });

                // 녹음 이벤트 처리 (플로팅 바)
                // ──────────────────────────────────────────────
                // 녹음 경과시간 표시 전략:
                //   - 백엔드는 10초마다 `ws:recording_duration` 을 쏜다 (싱크 포인트)
                //   - 프론트는 싱크 포인트를 기준선으로 잡고, 로컬 1초 타이머로
                //     부드럽게 증가시킨다. 다음 싱크가 오면 드리프트를 보정한다.
                //   - 탭 전환(SPA 뷰 이동)에는 영향받지 않지만, 브라우저 탭
                //     자체가 백그라운드로 가면 setInterval 이 throttle 되므로
                //     포커스 복귀 시 다음 싱크 포인트(최대 10초)에 정확히 보정된다.
                // ──────────────────────────────────────────────
                var _recTickTimer = null;
                var _recBaseSeconds = 0;          // 마지막 싱크 시점의 서버 초
                var _recBaseWallClock = 0;        // 그 싱크를 받은 로컬 타임스탬프 (ms)
                function _formatRecDuration(sec) {
                    var s0 = Math.floor(sec);
                    if (s0 < 0) s0 = 0;
                    var m = Math.floor(s0 / 60);
                    var s = s0 % 60;
                    var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
                    return pad(m) + ":" + pad(s);
                }
                // Overlay 타이머용 HH:MM:SS 포맷 (레퍼런스 RecordingOverlay 스펙)
                function _formatRecDurationLong(sec) {
                    var s0 = Math.floor(sec);
                    if (s0 < 0) s0 = 0;
                    var h = Math.floor(s0 / 3600);
                    var m = Math.floor((s0 % 3600) / 60);
                    var s = s0 % 60;
                    var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
                    return pad(h) + ":" + pad(m) + ":" + pad(s);
                }
                function _renderRecDuration() {
                    var elapsedMs = Date.now() - _recBaseWallClock;
                    var cur = _recBaseSeconds + elapsedMs / 1000;
                    var pillDur = document.getElementById("recordingDuration");
                    if (pillDur) App.safeText(pillDur, _formatRecDuration(cur));
                    var overlayTimer = document.getElementById("recordingOverlayTimer");
                    if (overlayTimer) App.safeText(overlayTimer, _formatRecDurationLong(cur));
                    var overlayMeta = document.getElementById("recordingOverlayMetaDuration");
                    if (overlayMeta) App.safeText(overlayMeta, _formatRecDurationLong(cur));
                }
                function _startRecTicker(initialSeconds) {
                    _recBaseSeconds = initialSeconds || 0;
                    _recBaseWallClock = Date.now();
                    _renderRecDuration();
                    if (_recTickTimer) clearInterval(_recTickTimer);
                    _recTickTimer = setInterval(_renderRecDuration, 1000);
                }
                function _stopRecTicker() {
                    if (_recTickTimer) {
                        clearInterval(_recTickTimer);
                        _recTickTimer = null;
                    }
                    _recBaseSeconds = 0;
                    _recBaseWallClock = 0;
                }

                // 녹음 HUD 상태 제어 — overlay(기본) ↔ pill(최소화) 배타 표시.
                // _recOverlayShown 이 true 면 pill 은 숨김, false 면 pill 표시.
                function _showRecHUD(mode) {
                    // mode: "overlay" | "pill" | "none"
                    var pill = document.getElementById("recordingStatus");
                    var overlay = document.getElementById("recordingOverlay");
                    if (mode === "overlay") {
                        if (pill) pill.classList.remove("visible");
                        if (overlay) {
                            overlay.classList.add("visible");
                            overlay.setAttribute("aria-hidden", "false");
                        }
                    } else if (mode === "pill") {
                        if (overlay) {
                            overlay.classList.remove("visible");
                            overlay.setAttribute("aria-hidden", "true");
                        }
                        if (pill) pill.classList.add("visible");
                    } else {
                        if (pill) pill.classList.remove("visible");
                        if (overlay) {
                            overlay.classList.remove("visible");
                            overlay.setAttribute("aria-hidden", "true");
                        }
                    }
                }

                // 48-bar waveform 마운트 — 레퍼런스 RecordingOverlay.jsx 기준.
                // 백엔드 실시간 audio-level 스트림이 없어 CSS keyframe 기반 시각 신호.
                (function mountRecordingWaveBars() {
                    var wave = document.querySelector("#recordingOverlay .recording-wave");
                    if (!wave || wave.childElementCount > 0) return;
                    for (var i = 0; i < 48; i++) {
                        var span = document.createElement("span");
                        // 각 bar 에 다른 animation-delay 로 자연스러운 파도 효과
                        span.style.animationDelay = (-(Math.random() * 1.2)).toFixed(2) + "s";
                        wave.appendChild(span);
                    }
                })();

                document.addEventListener("ws:recording_started", function () {
                    // 새 녹음 → overlay 기본 표시
                    _showRecHUD("overlay");
                    _startRecTicker(0);
                    // 버튼 disabled 초기화
                    var recStopBtn = document.getElementById("recordingStopBtn");
                    if (recStopBtn) recStopBtn.disabled = false;
                    var overlayStop = document.getElementById("recordingOverlayStopBtn");
                    if (overlayStop) overlayStop.disabled = false;
                    var overlayCancel = document.getElementById("recordingOverlayCancelBtn");
                    if (overlayCancel) overlayCancel.disabled = false;
                    loadMeetings();
                });
                document.addEventListener("ws:recording_stopped", function () {
                    _showRecHUD("none");
                    _stopRecTicker();
                    loadMeetings();
                });
                document.addEventListener("ws:recording_duration", function (e) {
                    // 백엔드 싱크 포인트: 기준선을 갱신하고 즉시 렌더
                    var detail = e.detail || {};
                    var seconds = detail.duration_seconds || 0;
                    _recBaseSeconds = seconds;
                    _recBaseWallClock = Date.now();
                    // 싱크가 왔는데 타이머가 없다면(복원 누락 등) 새로 시작
                    if (!_recTickTimer) {
                        var overlay = document.getElementById("recordingOverlay");
                        var pill = document.getElementById("recordingStatus");
                        // 현재 어느 쪽이 표시 중인지 확인 — overlay 우선, 없으면 pill
                        var mode = (overlay && overlay.classList.contains("visible")) ? "overlay"
                                 : (pill && pill.classList.contains("visible")) ? "pill"
                                 : "overlay";
                        _showRecHUD(mode);
                        _recTickTimer = setInterval(_renderRecDuration, 1000);
                    }
                    _renderRecDuration();
                });
                document.addEventListener("ws:recording_error", function (e) {
                    var detail = e.detail || {};
                    _showRecHUD("none");
                    _stopRecTicker();
                    var msg = detail.error || detail.message || "녹음 중 오류가 발생했습니다";
                    errorBanner.show(msg);
                });

                // Overlay ↔ pill 전환 버튼들
                (function bindRecOverlayControls() {
                    var expandBtn = document.getElementById("recordingExpandBtn");
                    if (expandBtn) {
                        expandBtn.addEventListener("click", function () {
                            _showRecHUD("overlay");
                        });
                    }

                    async function sendStop() {
                        try {
                            await App.apiRequest("/recording/stop", { method: "POST" });
                            _showRecHUD("none");
                            _stopRecTicker();
                        } catch (err) {
                            errorBanner.show("녹음 정지 실패: " + (err.message || "알 수 없는 오류"));
                            var ob = document.getElementById("recordingOverlayStopBtn");
                            var oc = document.getElementById("recordingOverlayCancelBtn");
                            if (ob) ob.disabled = false;
                            if (oc) oc.disabled = false;
                        }
                    }

                    // Overlay 내부 버튼들 — data-recording-action 속성으로 delegate
                    var overlay = document.getElementById("recordingOverlay");
                    if (overlay) {
                        overlay.addEventListener("click", function (e) {
                            var t = e.target.closest("[data-recording-action]");
                            if (!t) return;
                            var action = t.getAttribute("data-recording-action");
                            if (action === "minimize") {
                                _showRecHUD("pill");
                            } else if (action === "cancel" || action === "stop") {
                                var stopBtn = document.getElementById("recordingOverlayStopBtn");
                                var cancelBtn = document.getElementById("recordingOverlayCancelBtn");
                                if (stopBtn) stopBtn.disabled = true;
                                if (cancelBtn) cancelBtn.disabled = true;
                                sendStop();
                            }
                        });
                    }
                })();

                // 범용 안내 모달 (#infoModal) — 아직 구현되지 않은 기능 안내 등에 재사용.
                // 사용: showInfoModal("제목", "본문")
                function showInfoModal(title, message) {
                    var modal = document.getElementById("infoModal");
                    if (!modal) return;
                    var t = document.getElementById("infoModalTitle");
                    var m = document.getElementById("infoModalMessage");
                    if (t) App.safeText(t, title || "안내");
                    if (m) App.safeText(m, message || "");
                    modal.classList.remove("hidden");
                    var closeBtn = document.getElementById("infoModalClose");
                    if (closeBtn) closeBtn.focus();
                }
                function hideInfoModal() {
                    var modal = document.getElementById("infoModal");
                    if (!modal) return;
                    modal.classList.add("hidden");
                }
                // 닫기 버튼 + 배경 클릭 + ESC 키로 닫기
                var _infoModalEl = document.getElementById("infoModal");
                if (_infoModalEl) {
                    var _infoCloseBtn = document.getElementById("infoModalClose");
                    if (_infoCloseBtn) _infoCloseBtn.addEventListener("click", hideInfoModal);
                    _infoModalEl.addEventListener("click", function (e) {
                        // 컨테이너(= overlay)를 직접 눌렀을 때만 — 내부 모달 콘텐츠 클릭은 무시
                        if (e.target === _infoModalEl) hideInfoModal();
                    });
                    document.addEventListener("keydown", function (e) {
                        if (e.key === "Escape" && !_infoModalEl.classList.contains("hidden")) {
                            hideInfoModal();
                        }
                    });
                }

                // 가져오기 모달 (#importModal) — 레퍼런스 ImportPanel.jsx 기준 dropzone + 큐.
                // 실제 업로드 엔드포인트는 아직 없음 → 선택된 파일 큐만 시각화하고
                // "파이프라인 시작" 버튼은 안내 메시지를 노출한다.
                var _importModalEl = document.getElementById("importModal");
                var _importDropzone = document.getElementById("importDropzone");
                var _importFileInput = document.getElementById("importFileInput");
                var _importQueue = document.getElementById("importQueue");
                var _importQueueList = document.getElementById("importQueueList");
                var _importNotice = document.getElementById("importNotice");
                var _importCancelBtn = document.getElementById("importModalCancel");
                var _importStartBtn = document.getElementById("importModalStart");
                var _importFiles = [];

                function _importFormatSize(bytes) {
                    if (!bytes || bytes < 0) return "—";
                    if (bytes < 1024) return bytes + " B";
                    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
                    if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
                    return (bytes / 1024 / 1024 / 1024).toFixed(2) + " GB";
                }

                function _renderImportQueue() {
                    if (!_importQueueList) return;
                    _importQueueList.innerHTML = "";
                    _importFiles.forEach(function (f, idx) {
                        var row = document.createElement("div");
                        row.className = "import-queue-item";

                        var name = document.createElement("span");
                        name.className = "import-queue-item-name";
                        name.textContent = f.name;
                        name.setAttribute("title", f.name);

                        var meta = document.createElement("span");
                        meta.className = "import-queue-item-meta";
                        meta.textContent = _importFormatSize(f.size);

                        var remove = document.createElement("button");
                        remove.type = "button";
                        remove.className = "import-queue-item-remove";
                        remove.setAttribute("aria-label", "제거");
                        remove.innerHTML = '<svg width="14" height="14" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="5" y1="5" x2="15" y2="15"></line><line x1="15" y1="5" x2="5" y2="15"></line></svg>';
                        remove.addEventListener("click", function () {
                            _importFiles.splice(idx, 1);
                            _renderImportQueue();
                        });

                        row.appendChild(name);
                        row.appendChild(meta);
                        row.appendChild(remove);
                        _importQueueList.appendChild(row);
                    });

                    if (_importQueue) _importQueue.hidden = _importFiles.length === 0;
                    if (_importStartBtn) _importStartBtn.disabled = _importFiles.length === 0;
                    if (_importNotice) _importNotice.hidden = _importFiles.length === 0;
                }

                function _addImportFiles(fileList) {
                    if (!fileList) return;
                    for (var i = 0; i < fileList.length; i++) {
                        _importFiles.push(fileList[i]);
                    }
                    _renderImportQueue();
                }

                function _openImportModal() {
                    if (!_importModalEl) return;
                    _importModalEl.classList.remove("hidden");
                    if (_importDropzone) _importDropzone.focus();
                }

                function _closeImportModal() {
                    if (!_importModalEl) return;
                    _importModalEl.classList.add("hidden");
                }

                var _importBtn = document.getElementById("importBtn");
                if (_importBtn) {
                    _importBtn.addEventListener("click", _openImportModal);
                }

                if (_importDropzone && _importFileInput) {
                    _importDropzone.addEventListener("click", function () {
                        _importFileInput.click();
                    });
                    _importDropzone.addEventListener("keydown", function (e) {
                        if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            _importFileInput.click();
                        }
                    });
                    _importDropzone.addEventListener("dragover", function (e) {
                        e.preventDefault();
                        _importDropzone.classList.add("dragging");
                    });
                    _importDropzone.addEventListener("dragleave", function () {
                        _importDropzone.classList.remove("dragging");
                    });
                    _importDropzone.addEventListener("drop", function (e) {
                        e.preventDefault();
                        _importDropzone.classList.remove("dragging");
                        _addImportFiles(e.dataTransfer && e.dataTransfer.files);
                    });
                    _importFileInput.addEventListener("change", function () {
                        _addImportFiles(_importFileInput.files);
                        _importFileInput.value = ""; // 같은 파일 재선택 허용
                    });
                }

                if (_importCancelBtn) {
                    _importCancelBtn.addEventListener("click", function () {
                        _importFiles = [];
                        _renderImportQueue();
                        _closeImportModal();
                    });
                }
                if (_importStartBtn) {
                    _importStartBtn.addEventListener("click", async function () {
                        if (_importFiles.length === 0) return;
                        var originalLabel = _importStartBtn.textContent;
                        _importStartBtn.disabled = true;
                        if (_importNotice) {
                            _importNotice.hidden = false;
                            _importNotice.textContent = "업로드 시작…";
                        }

                        var succeeded = 0;
                        var failed = [];
                        // 직렬 업로드 — 동시 업로드는 디스크/네트워크 경합만 늘림.
                        for (var i = 0; i < _importFiles.length; i++) {
                            var file = _importFiles[i];
                            if (_importNotice) {
                                _importNotice.textContent =
                                    "업로드 중 (" + (i + 1) + "/" + _importFiles.length + ") · " +
                                    file.name;
                            }
                            try {
                                var resp = await fetch("/api/uploads", {
                                    method: "POST",
                                    headers: {
                                        "Content-Type": "application/octet-stream",
                                        // RFC 3986 unreserved 외 문자(한글 등) 안전하게 전달.
                                        "X-Filename": encodeURIComponent(file.name),
                                    },
                                    body: file,
                                });
                                if (!resp.ok) {
                                    var errText = "";
                                    try {
                                        var errJson = await resp.json();
                                        errText = errJson && errJson.detail ? errJson.detail : "";
                                    } catch (_e) {
                                        errText = String(resp.status);
                                    }
                                    failed.push({ name: file.name, reason: errText });
                                } else {
                                    succeeded++;
                                }
                            } catch (e) {
                                failed.push({ name: file.name, reason: e.message || String(e) });
                            }
                        }

                        _importStartBtn.disabled = false;
                        _importStartBtn.textContent = originalLabel;

                        if (failed.length === 0) {
                            if (_importNotice) {
                                _importNotice.textContent =
                                    succeeded + "건 업로드 완료. 자동 감지 후 큐에 등록됩니다.";
                            }
                            _importFiles = [];
                            _renderImportQueue();
                            // 업로드 직후 사이드바 + 대시보드를 즉시 한 번 갱신.
                            if (window.ListPanel && ListPanel.loadMeetings) {
                                try { ListPanel.loadMeetings(); } catch (_) {}
                            }
                            document.dispatchEvent(new CustomEvent("recap:dashboard-refresh"));
                            // 사용자가 결과를 인지할 수 있게 짧게 노출 후 닫기.
                            setTimeout(_closeImportModal, 800);
                        } else {
                            if (_importNotice) {
                                var summary =
                                    succeeded + "건 성공, " + failed.length + "건 실패";
                                var detail = failed
                                    .slice(0, 3)
                                    .map(function (f) { return f.name + ": " + (f.reason || "오류"); })
                                    .join(" · ");
                                _importNotice.textContent = summary + " — " + detail;
                            }
                        }
                    });
                }
                // importModal 배경 클릭으로 닫기 + ESC 로 닫기 (infoModal 과 동일 패턴)
                if (_importModalEl) {
                    _importModalEl.addEventListener("click", function (e) {
                        if (e.target === _importModalEl) _closeImportModal();
                    });
                    document.addEventListener("keydown", function (e) {
                        if (e.key === "Escape" && !_importModalEl.classList.contains("hidden")) {
                            _closeImportModal();
                        }
                    });
                }

                // 녹음 HUD 의 즉시 정지 버튼: 클릭 시 /api/recording/stop 호출.
                // POST 성공 시 pill 을 즉시 숨김 — WebSocket 이 끊긴 상태에서도 UI 가 멈추지 않도록.
                // 백엔드의 ws:recording_stopped 가 뒤이어 도착해도 기존 핸들러의 동작(hide+ticker stop)은 멱등.
                var _recStopBtn = document.getElementById("recordingStopBtn");
                if (_recStopBtn) {
                    _recStopBtn.addEventListener("click", async function () {
                        if (_recStopBtn.disabled) return;
                        _recStopBtn.disabled = true;
                        try {
                            await App.apiRequest("/recording/stop", { method: "POST" });
                            var recStatus = document.getElementById("recordingStatus");
                            if (recStatus) recStatus.classList.remove("visible");
                            _stopRecTicker();
                        } catch (err) {
                            errorBanner.show("녹음 정지 실패: " + (err.message || "알 수 없는 오류"));
                            _recStopBtn.disabled = false;
                        }
                    });
                }

                // 다중 선택 키보드 핸들러 (bulk-actions §A.9)
                //  - Esc: 사이드바 또는 액션 바 포커스 시 전체 해제
                //  - Cmd/Ctrl+A: 사이드바 컨테이너에 포커스가 있을 때만 가로채기
                //                (회의 0 건이면 no-op — B10 정책)
                document.addEventListener("keydown", function (e) {
                    // Esc — selection 활성 시 전체 해제
                    if (e.key === "Escape") {
                        if (_selectedIds.size > 0) {
                            // 사이드바·액션바·문서 어느 곳에 포커스가 있어도 동작
                            clearSelection();
                        }
                        return;
                    }
                    // Cmd+A / Ctrl+A — 사이드바 한정 전체 선택
                    var isMeta = e.metaKey || e.ctrlKey;
                    if (!isMeta || (e.key !== "a" && e.key !== "A")) return;
                    if (!_listEl) return;
                    var active = document.activeElement;
                    var inSidebar =
                        active === _listEl ||
                        (active && _listEl.contains(active));
                    if (!inSidebar) return;
                    var items = _listEl.querySelectorAll(".meeting-item");
                    if (items.length === 0) {
                        // B10 — 빈 사이드바면 no-op (브라우저 기본도 차단해 텍스트 전체선택 방지)
                        e.preventDefault();
                        return;
                    }
                    e.preventDefault();
                    items.forEach(function (it) {
                        var id = it.getAttribute("data-meeting-id");
                        if (id) _selectedIds.add(id);
                    });
                    _emitSelectionChanged();
                });

                // 초기 데이터 로드
                loadMeetings();
                fetchStatus();

                // 새로고침/재방문 시 진행 중인 녹음 상태 복원
                // (백엔드는 녹음을 계속하지만 ws:recording_started 이벤트는 다시 안 오므로
                //  프론트 플로팅 바가 사라지는 UX 결함을 방지)
                App.apiRequest("/recording/status")
                    .then(function (rec) {
                        if (rec && rec.is_recording) {
                            // 복원 시엔 최소화(pill) 모드로 표시해 작업 중이던 뷰를 가리지 않는다.
                            // 사용자가 pill 의 확장 버튼을 눌러 overlay 로 볼 수 있음.
                            _showRecHUD("pill");
                            _startRecTicker(rec.duration_seconds || 0);
                            // 새로고침으로 새 DOM 이 로드된 상태라 버튼은 기본 활성이지만,
                            // 브라우저 form state 자동 복원에 대비해 명시적으로 리셋.
                            var recStopBtn = document.getElementById("recordingStopBtn");
                            if (recStopBtn) recStopBtn.disabled = false;
                            var overlayStop = document.getElementById("recordingOverlayStopBtn");
                            if (overlayStop) overlayStop.disabled = false;
                            var overlayCancel = document.getElementById("recordingOverlayCancelBtn");
                            if (overlayCancel) overlayCancel.disabled = false;
                        }
                    })
                    .catch(function () {
                        // recorder 미초기화 등은 무시
                    });

                // 주기적 폴링 (WebSocket 폴백)
                _statusTimer = setInterval(fetchStatus, STATUS_POLL_INTERVAL);
                _meetingsTimer = setInterval(loadMeetings, MEETINGS_POLL_INTERVAL);
            }

            /**
             * 시스템 상태를 폴링한다.
             */
            async function fetchStatus() {
                try {
                    var data = await App.apiRequest("/status");
                    _statusDot.className = "status-dot connected";
                    var activeCount = data.active_jobs || 0;
                    if (activeCount > 0) {
                        App.safeText(_statusText, "처리 중 " + activeCount + "건");
                    } else {
                        App.safeText(_statusText, "대기 중");
                    }
                } catch (e) {
                    _statusDot.className = "status-dot error";
                    App.safeText(_statusText, "서버 미연결");
                }
            }

            /**
             * 회의 목록을 API에서 가져와 렌더링한다.
             *
             * 초기 로딩 (목록이 비어있을 때) 만 스켈레톤 카드 4 개를 표시한다.
             * 폴링 (이미 데이터가 렌더링된 상태) 에서는 깜빡임 방지를 위해
             * 스켈레톤을 표시하지 않는다. mockup §3.3 표 (회의 목록 = 카드형 × 4).
             * render() 진입 시 _listEl.innerHTML="" 으로 자동 cleanup.
             */
            async function loadMeetings() {
                // 최초 로딩 시점 (목록 비어있고 _meetings 도 비어있음) 에만 스켈레톤 노출
                if (_listEl && _meetings.length === 0 && _listEl.children.length === 0) {
                    _listEl.appendChild(App.createSkeletonCards(4));
                }
                try {
                    var data = await App.apiRequest("/meetings");
                    _meetings = data.meetings || [];
                    _applyFilterAndSort();
                } catch (e) {
                    // 조용히 처리 (리스트 로드 실패는 치명적이지 않음)
                    // 실패 시 스켈레톤이 남아있을 수 있으므로 정리
                    if (_listEl) {
                        var skeletons = _listEl.querySelectorAll(".skeleton-card");
                        if (skeletons.length > 0) {
                            _listEl.innerHTML = "";
                        }
                    }
                }
            }

            /**
             * 현재 검색어와 정렬 기준으로 필터링 및 정렬 후 렌더링한다.
             */
            function _applyFilterAndSort() {
                var query = _searchEl ? _searchEl.value.trim() : "";
                var filtered = _meetings;

                // 검색 필터 — meeting_id 의 날짜를 YYYY-MM-DD 형식으로 정규화한 idNorm
                // 도 함께 매칭해 사용자가 "2026-04-30" 같은 하이픈 포함 날짜를 입력해도
                // meeting_20260430_xxx 같은 raw ID 와 일치하도록 한다. title 필드도 누락
                // 되어 있어 추가 (사용자가 회의 제목으로도 검색 가능하게).
                if (query) {
                    var lower = query.toLowerCase();
                    filtered = _meetings.filter(function (m) {
                        var rawId = (m.meeting_id || "").toLowerCase();
                        var idMatch = rawId.indexOf(lower) >= 0;
                        var idNorm = rawId.replace(
                            /^meeting_(\d{4})(\d{2})(\d{2})_.*$/,
                            "$1-$2-$3"
                        );
                        var idNormMatch = idNorm.indexOf(lower) >= 0;
                        var titleMatch = (m.title || "").toLowerCase().indexOf(lower) >= 0;
                        var summaryMatch = (m.summary_preview || "").toLowerCase().indexOf(lower) >= 0;
                        return idMatch || idNormMatch || titleMatch || summaryMatch;
                    });
                }

                // 정렬
                var sortBy = _sortEl ? _sortEl.value : "newest";
                filtered = _sortMeetings(filtered, sortBy);

                // 카운트 업데이트
                if (_countEl) {
                    App.safeText(_countEl, filtered.length + "/" + _meetings.length);
                }

                // Progressive Disclosure: 회의가 하나도 없을 때 검색/정렬/카운트 UI 숨김
                // (§5.3 Progressive Onboarding — 빈 상태에서 사용자를 압도하지 않음)
                var listPanelEl = document.getElementById("list-panel");
                if (listPanelEl) {
                    listPanelEl.classList.toggle("list-panel-empty", _meetings.length === 0);
                }

                render(filtered);
            }

            /**
             * 회의 목록을 정렬한다.
             * @param {Array} meetings - 회의 목록
             * @param {string} sortBy - 정렬 기준
             * @returns {Array} 정렬된 배열
             */
            function _sortMeetings(meetings, sortBy) {
                var sorted = meetings.slice();

                if (sortBy === "newest") {
                    sorted.sort(function (a, b) {
                        return (b.created_at || "").localeCompare(a.created_at || "");
                    });
                } else if (sortBy === "oldest") {
                    sorted.sort(function (a, b) {
                        return (a.created_at || "").localeCompare(b.created_at || "");
                    });
                } else if (sortBy === "status") {
                    sorted.sort(function (a, b) {
                        var oa = STATUS_SORT_ORDER[a.status] != null ? STATUS_SORT_ORDER[a.status] : 99;
                        var ob = STATUS_SORT_ORDER[b.status] != null ? STATUS_SORT_ORDER[b.status] : 99;
                        if (oa !== ob) return oa - ob;
                        return (b.created_at || "").localeCompare(a.created_at || "");
                    });
                }

                return sorted;
            }

            /**
             * meeting_id에서 날짜 기반 제목을 추출한다.
             * 예: "meeting_20260310_193619" → "2026-03-10 19:36"
             * @param {string} meetingId - 회의 ID
             * @param {string} createdAt - 생성일 (폴백)
             * @returns {string} 날짜 기반 제목
             */
            function _extractTitle(meetingId, createdAt) {
                // meeting_YYYYMMDD_HHMMSS 패턴 매칭
                var match = (meetingId || "").match(/(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/);
                if (match) {
                    return match[1] + "-" + match[2] + "-" + match[3] + " " +
                           match[4] + ":" + match[5];
                }
                // 폴백: created_at 날짜 사용
                if (createdAt) {
                    return App.formatDate(createdAt);
                }
                return meetingId || "-";
            }

            /**
             * 회의 목록을 렌더링한다.
             * @param {Array} meetings - 회의 목록
             */
            function render(meetings) {
                _listEl.innerHTML = "";

                if (meetings.length === 0) {
                    // 빈 상태 (mockup §5.1) — fixture 의 마크업 인터페이스와 일치
                    var empty = document.createElement("div");
                    empty.className = "empty-state-container";
                    empty.setAttribute("data-empty", "meeting-list");
                    empty.innerHTML =
                        '<div class="empty-state" role="status" aria-live="polite">' +
                        '  <svg class="empty-state-icon" width="48" height="48" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
                        '    <circle cx="24" cy="24" r="20"/>' +
                        '    <line x1="14" y1="20" x2="14" y2="28"/>' +
                        '    <line x1="19" y1="16" x2="19" y2="32"/>' +
                        '    <line x1="24" y1="14" x2="24" y2="34"/>' +
                        '    <line x1="29" y1="16" x2="29" y2="32"/>' +
                        '    <line x1="34" y1="20" x2="34" y2="28"/>' +
                        '  </svg>' +
                        '  <h2 class="empty-state-title">아직 회의가 없어요</h2>' +
                        '  <p class="empty-state-description">첫 회의를 녹음하면 자동으로 전사·요약돼요.</p>' +
                        '  <button class="empty-state-cta" type="button" data-action="start-recording">녹음 시작</button>' +
                        '</div>';
                    _listEl.appendChild(empty);
                    // CTA → /api/recording/start 호출 (메뉴바 _on_start_recording 과 동일 엔드포인트)
                    var cta = empty.querySelector('[data-action="start-recording"]');
                    if (cta) {
                        cta.addEventListener("click", function () {
                            App.apiRequest("/recording/start", { method: "POST" }).catch(function (err) {
                                console.error("녹음 시작 실패:", err);
                            });
                        });
                    }
                    return;
                }

                meetings.forEach(function (meeting) {
                    var item = document.createElement("div");
                    item.className = "meeting-item";
                    item.setAttribute("data-meeting-id", meeting.meeting_id);
                    item.setAttribute("role", "option");
                    item.setAttribute("tabindex", "0");
                    item.setAttribute("aria-label",
                        _extractTitle(meeting.meeting_id, meeting.created_at) +
                        " — " + App.getStatusLabel(meeting.status));
                    // 다중 선택 상태 (bulk-actions §A) — multi-select listbox 표준
                    // aria-checked: 'true' | 'false' (체크박스 의미) — SR 단일 진실
                    var isSelectedInit = _selectedIds.has(meeting.meeting_id);
                    item.setAttribute("aria-checked", isSelectedInit ? "true" : "false");
                    if (isSelectedInit) {
                        item.classList.add("selected");
                    }
                    if (meeting.meeting_id === _activeId) {
                        item.classList.add("active");
                    }

                    // 처리 중인 항목: pulse 애니메이션
                    var isProcessing = (
                        meeting.status !== "completed" &&
                        meeting.status !== "failed" &&
                        meeting.status !== "recorded" &&
                        meeting.status !== "queued"
                    );
                    if (isProcessing) {
                        item.classList.add("processing");
                    }

                    // 다중 선택 시각 체크박스 (옵션 B — span + aria-hidden)
                    // SR 단일 진실은 부모 .meeting-item 의 aria-checked.
                    // hover 또는 selection-mode-active 시 opacity 1 (CSS 처리).
                    var checkboxEl = document.createElement("span");
                    checkboxEl.className = "meeting-item-checkbox";
                    checkboxEl.setAttribute("aria-hidden", "true");
                    checkboxEl.setAttribute("data-checkbox", "true");

                    // 상태 도트
                    var statusDot = document.createElement("span");
                    statusDot.className = "meeting-item-dot";
                    if (meeting.status === "completed") {
                        statusDot.classList.add("completed");
                    } else if (meeting.status === "failed") {
                        statusDot.classList.add("failed");
                    } else if (isProcessing) {
                        statusDot.classList.add("processing");
                    } else if (meeting.status === "recorded") {
                        statusDot.classList.add("recorded");
                    } else {
                        statusDot.classList.add("queued");
                    }

                    // 텍스트 컨테이너
                    var textContainer = document.createElement("div");
                    textContainer.className = "meeting-item-text";

                    // 제목: 사용자 정의 title 우선, 없으면 날짜 기반 폴백
                    var titleEl = document.createElement("div");
                    titleEl.className = "meeting-item-title";
                    titleEl.textContent = App.extractMeetingTitle(meeting);

                    // 요약 프리뷰 1줄 — summary 가 있으면 우선, 없으면 상태 라벨
                    // 전체 요약을 native tooltip 으로 노출해 한 줄 잘림 보완.
                    var previewEl = document.createElement("div");
                    previewEl.className = "meeting-item-preview";
                    if (meeting.summary_preview) {
                        previewEl.textContent = meeting.summary_preview;
                        item.setAttribute("title", meeting.summary_preview);
                    } else {
                        previewEl.textContent = App.getStatusLabel(meeting.status);
                    }

                    textContainer.appendChild(titleEl);
                    textContainer.appendChild(previewEl);

                    // DOM 순서: 체크박스 → 상태 도트 → 텍스트 (좌측 정렬)
                    item.appendChild(checkboxEl);
                    item.appendChild(statusDot);
                    item.appendChild(textContainer);

                    // 클릭 분기 (bulk-actions §A.3):
                    //  - 체크박스 영역(closest [data-checkbox]) → 토글 + stopPropagation
                    //  - Cmd/Ctrl+클릭 (본문) → 토글 (뷰어 이동 X)
                    //  - Shift+클릭 (본문) → 마지막 앵커 ↔ 현재 항목 사이 범위 토글
                    //  - 단일 클릭 (modifier 없음, 본문) → 기존 라우팅
                    item.addEventListener("click", function (e) {
                        var meetingId = meeting.meeting_id;
                        var cbHit = e.target && e.target.closest
                            ? e.target.closest("[data-checkbox]")
                            : null;
                        if (cbHit && item.contains(cbHit)) {
                            e.stopPropagation();
                            e.preventDefault();
                            _toggleSelection(meetingId);
                            return;
                        }
                        if (e.metaKey || e.ctrlKey) {
                            e.preventDefault();
                            _toggleSelection(meetingId);
                            return;
                        }
                        if (e.shiftKey) {
                            e.preventDefault();
                            if (_lastClickedId) {
                                _selectRange(_lastClickedId, meetingId);
                            } else {
                                _toggleSelection(meetingId);
                            }
                            return;
                        }
                        // 일반 클릭 — 기존대로 라우팅 (선택 상태는 변경하지 않음)
                        Router.navigate(
                            "/app/viewer/" + encodeURIComponent(meetingId)
                        );
                    });

                    // 키보드 접근성:
                    //  - Enter → 라우팅 (뷰어 열기)
                    //  - Space → 토글 (선택)
                    item.addEventListener("keydown", function (e) {
                        if (e.key === "Enter") {
                            e.preventDefault();
                            Router.navigate(
                                "/app/viewer/" + encodeURIComponent(meeting.meeting_id)
                            );
                        } else if (e.key === " ") {
                            e.preventDefault();
                            _toggleSelection(meeting.meeting_id);
                        }
                    });

                    _listEl.appendChild(item);
                });

                // render() 재호출 후에도 selection 보존 — DOM 이 새로 만들어졌으므로
                // _selectedIds 상태를 시각/ARIA 에 다시 반영 (B12 정책).
                _syncSelectionUI();
            }

            /**
             * 활성 항목을 설정한다 (하이라이트).
             * @param {string} meetingId - 활성화할 회의 ID (null이면 해제)
             */
            function setActive(meetingId) {
                _activeId = meetingId;
                // 라우팅 active 표시. 다중 선택의 aria-checked 와는 별도 차원.
                var items = _listEl.querySelectorAll(".meeting-item");
                items.forEach(function (item) {
                    var itemId = item.getAttribute("data-meeting-id");
                    if (itemId === meetingId) {
                        item.classList.add("active");
                    } else {
                        item.classList.remove("active");
                    }
                });
            }

            /**
             * URL 경로에서 활성 회의를 추출하여 설정한다.
             * @param {string} path - URL 경로
             */
            function setActiveFromPath(path) {
                var match = path.match(/^\/app\/viewer\/(.+)$/);
                if (match) {
                    setActive(decodeURIComponent(match[1]));
                } else {
                    setActive(null);
                }
            }

            /**
             * 현재 회의 목록 데이터를 반환한다.
             * @returns {Array}
             */
            function getMeetings() {
                return _meetings;
            }

            /**
             * 리스트 패널 타이머를 정리한다.
             */
            function destroy() {
                if (_statusTimer) { clearInterval(_statusTimer); _statusTimer = null; }
                if (_meetingsTimer) { clearInterval(_meetingsTimer); _meetingsTimer = null; }
                if (_searchTimeout) { clearTimeout(_searchTimeout); _searchTimeout = null; }
            }

            return {
                init: init,
                loadMeetings: loadMeetings,
                setActive: setActive,
                setActiveFromPath: setActiveFromPath,
                getMeetings: getMeetings,
                destroy: destroy,
                // 다중 선택 API — BulkActionBar 가 사용
                clearSelection: clearSelection,
                getSelectedIds: getSelectedIds,
            };
        })();

        return ListPanel;
    }

    window.MeetingListPanel = {
        create: create,
    };
})();
