"""프론트엔드 모듈 경계 스모크 테스트."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.harness

_WINDOW_DOT_ASSIGNMENT_RE = re.compile(r"\bwindow\.([A-Za-z_$][\w$]*)\s*=")
_WINDOW_BRACKET_ASSIGNMENT_RE = re.compile(r"""\bwindow\[\s*["']([A-Za-z_$][\w$]*)["']\s*\]\s*=""")
_WINDOW_MUTATION_RE = re.compile(
    r"\bObject\.(?:assign|defineProperty|defineProperties)\(\s*window\b"
)
_WEB_JS_DIR = Path("ui/web")
_REACT_SRC_DIR = Path("ui/web-src")
_ALLOWED_WINDOW_GLOBALS_BY_FILE = {
    "ui/web/ab-test-view.js": ["MeetingAbTestView"],
    "ui/web/api-client.js": ["MeetingApi"],
    "ui/web/app.js": ["MeetingApp"],
    "ui/web/bulk-action-bar.js": ["MeetingBulkActionBar"],
    "ui/web/chat-view.js": ["MeetingChatView"],
    "ui/web/command-palette.js": ["MeetingCommandPalette"],
    "ui/web/empty-view.js": ["MeetingEmptyView"],
    "ui/web/global-resource-bar.js": ["MeetingGlobalResourceBar"],
    "ui/web/list-panel.js": ["MeetingListPanel"],
    "ui/web/mobile-drawer.js": ["MeetingMobileDrawer"],
    "ui/web/search-view.js": ["MeetingSearchView"],
    "ui/web/settings-view.js": ["MeetingSettingsView"],
    "ui/web/shortcut-controller.js": ["MeetingShortcutController"],
    "ui/web/spa.js": ["ListPanel", "SPA"],
    "ui/web/theme-controller.js": ["MeetingThemeController"],
    "ui/web/viewer-view.js": ["MeetingViewerView"],
    "ui/web/wiki-view.js": ["MeetingWikiView"],
}


def _window_global_assignments(path: Path) -> list[str]:
    """JS 파일의 직접 window 전역 할당 이름을 반환한다."""
    content = path.read_text(encoding="utf-8")
    return sorted(
        {
            *_WINDOW_DOT_ASSIGNMENT_RE.findall(content),
            *_WINDOW_BRACKET_ASSIGNMENT_RE.findall(content),
        }
    )


def test_frontend_modules_load_in_dependency_order() -> None:
    html = Path("ui/web/index.html").read_text(encoding="utf-8")

    api_client = html.index("/static/api-client.js")
    app = html.index("/static/app.js")
    list_panel = html.index("/static/list-panel.js")
    bulk_action_bar = html.index("/static/bulk-action-bar.js")
    command_palette = html.index("/static/command-palette.js")
    settings_view = html.index("/static/settings-view.js")
    viewer_view = html.index("/static/viewer-view.js")
    chat_view = html.index("/static/chat-view.js")
    wiki_view = html.index("/static/wiki-view.js")
    ab_test_view = html.index("/static/ab-test-view.js")
    search_view = html.index("/static/search-view.js")
    empty_view = html.index("/static/empty-view.js")
    global_resource_bar = html.index("/static/global-resource-bar.js")
    theme_controller = html.index("/static/theme-controller.js")
    mobile_drawer = html.index("/static/mobile-drawer.js")
    shortcut_controller = html.index("/static/shortcut-controller.js")
    spa = html.index("/static/spa.js")

    assert (
        api_client
        < app
        < list_panel
        < bulk_action_bar
        < command_palette
        < settings_view
        < viewer_view
        < chat_view
        < wiki_view
        < ab_test_view
        < search_view
        < empty_view
        < global_resource_bar
        < theme_controller
        < mobile_drawer
        < shortcut_controller
        < spa
    )


def test_app_delegates_api_requests_to_meeting_api() -> None:
    app_js = Path("ui/web/app.js").read_text(encoding="utf-8")

    assert "var ApiClient = window.MeetingApi || null;" in app_js
    assert "return ApiClient.request(endpoint, options);" in app_js
    assert "return ApiClient.post(endpoint, body);" in app_js
    assert "return ApiClient.delete(endpoint);" in app_js


def test_api_client_exposes_stable_namespace() -> None:
    api_client = Path("ui/web/api-client.js").read_text(encoding="utf-8")

    assert "window.MeetingApi" in api_client
    assert "buildApiUrl: buildApiUrl" in api_client
    assert "request: request" in api_client
    assert "post: post" in api_client
    assert "delete: deleteRequest" in api_client


def test_legacy_window_global_assignments_are_allowlisted() -> None:
    """React 전환 전 legacy window 전역 노출면을 고정한다."""
    observed = {}
    for path in sorted(_WEB_JS_DIR.glob("*.js")):
        assignments = _window_global_assignments(path)
        if assignments:
            observed[str(path)] = assignments

    assert observed == _ALLOWED_WINDOW_GLOBALS_BY_FILE


def test_frontend_modules_do_not_broaden_global_surface() -> None:
    """신규 전역 확산 패턴을 도입하지 않는다."""
    for path in sorted(_WEB_JS_DIR.glob("*.js")):
        content = path.read_text(encoding="utf-8")
        assert "globalThis" not in content, f"{path} uses globalThis"
        assert not _WINDOW_MUTATION_RE.search(content), (
            f"{path} mutates window outside the allowlist"
        )


def test_future_react_source_does_not_use_window_globals() -> None:
    """React/TypeScript island 코드에는 window 전역 의존을 추가하지 않는다."""
    if not _REACT_SRC_DIR.exists():
        return

    for path in sorted(_REACT_SRC_DIR.rglob("*")):
        if path.suffix not in {".ts", ".tsx"}:
            continue
        content = path.read_text(encoding="utf-8")
        assert "window." not in content, f"{path} uses window.*"
        assert "globalThis" not in content, f"{path} uses globalThis"


def test_api_client_preserves_abort_error_contract() -> None:
    api_client = Path("ui/web/api-client.js").read_text(encoding="utf-8")

    assert 'networkError.name === "AbortError"' in api_client
    assert "throw networkError;" in api_client


def test_api_client_supports_non_json_success_payloads() -> None:
    api_client = Path("ui/web/api-client.js").read_text(encoding="utf-8")

    assert 'response.headers.get("content-type")' in api_client
    assert 'contentType.indexOf("application/json")' in api_client
    assert "return response.text();" in api_client


def test_setup_view_stays_private_read_only_and_abortable() -> None:
    index_html = Path("ui/web/index.html").read_text(encoding="utf-8")
    spa_js = Path("ui/web/spa.js").read_text(encoding="utf-8")
    setup_block = spa_js[spa_js.index("// === SetupView") : spa_js.index("// === ListPanel")]

    assert 'data-route="/app/setup"' in index_html
    assert "function SetupView()" in setup_block
    assert "window.MeetingSetupView" not in spa_js
    assert "SetupView: SetupView" not in spa_js
    assert "MeetingSetupView" not in setup_block
    assert re.findall(r'App\.apiRequest\("([^"]+)"', setup_block) == ["/setup/readiness"]
    assert "App.apiPost(" not in setup_block
    assert "App.apiDelete(" not in setup_block
    assert "fetch(" not in setup_block
    assert "new AbortController()" in setup_block
    assert "this._controller.abort()" in setup_block
    assert "if (self._destroyed) return;" in setup_block
    assert 'App.escapeHtml(check.message || "")' in setup_block
    assert "App.escapeHtml(hint)" in setup_block
    assert "setupSafeExternalHref(action.value)" in setup_block
    assert "https://huggingface.co/" in setup_block
    assert "setupSafeRoute(action.value)" in setup_block
    assert 'return value === "/app/settings" ? value : "";' in setup_block
    assert 'target="_blank" rel="noopener noreferrer"' in setup_block
    assert "App.escapeHtml(command)" in setup_block
    assert "Router.navigate(route)" in setup_block


def test_command_palette_exposes_factory_boundary() -> None:
    command_palette = Path("ui/web/command-palette.js").read_text(encoding="utf-8")
    spa_js = Path("ui/web/spa.js").read_text(encoding="utf-8")

    assert "window.MeetingCommandPalette" in command_palette
    assert "create: create" in command_palette
    assert "isEditingContext: isEditingContext" in command_palette
    assert "CommandPaletteModule.create(commandPaletteDeps)" in spa_js
    assert "commandPaletteDeps.toggleTheme = ThemeController.toggle" in spa_js
    assert 'typeof ThemeControllerModule.create === "function"' in spa_js
    assert 'if (typeof toggleTheme === "function")' in command_palette


def test_list_panel_exposes_factory_boundary() -> None:
    list_panel = Path("ui/web/list-panel.js").read_text(encoding="utf-8")
    spa_js = Path("ui/web/spa.js").read_text(encoding="utf-8")

    assert "window.MeetingListPanel" in list_panel
    assert "create: create" in list_panel
    assert "ListPanelModule.create({" in spa_js
    assert "window.ListPanel = ListPanel;" in spa_js


def test_settings_view_exposes_factory_boundary() -> None:
    settings_view = Path("ui/web/settings-view.js").read_text(encoding="utf-8")
    spa_js = Path("ui/web/spa.js").read_text(encoding="utf-8")

    assert "window.MeetingSettingsView" in settings_view
    assert "create: create" in settings_view
    assert "return SettingsView;" in settings_view
    assert "SettingsViewModule.create({" in spa_js


def test_viewer_view_exposes_factory_boundary() -> None:
    viewer_view = Path("ui/web/viewer-view.js").read_text(encoding="utf-8")
    spa_js = Path("ui/web/spa.js").read_text(encoding="utf-8")

    assert "window.MeetingViewerView" in viewer_view
    assert "create: create" in viewer_view
    assert "return ViewerView;" in viewer_view
    assert "ViewerViewModule.create({" in spa_js


def test_chat_view_exposes_factory_boundary() -> None:
    chat_view = Path("ui/web/chat-view.js").read_text(encoding="utf-8")
    spa_js = Path("ui/web/spa.js").read_text(encoding="utf-8")

    assert "window.MeetingChatView" in chat_view
    assert "create: create" in chat_view
    assert "return ChatView;" in chat_view
    assert "ChatViewModule.create({" in spa_js
    assert "ChatView: ChatView" in spa_js


def test_wiki_view_exposes_factory_boundary() -> None:
    wiki_view = Path("ui/web/wiki-view.js").read_text(encoding="utf-8")
    spa_js = Path("ui/web/spa.js").read_text(encoding="utf-8")

    assert "window.MeetingWikiView" in wiki_view
    assert "create: create" in wiki_view
    assert "return WikiView;" in wiki_view
    assert "WikiViewModule.create({" in spa_js
    assert "WikiView: WikiView" in spa_js
    assert "function WikiView()" not in spa_js


def test_wiki_view_preserves_lifecycle_and_compatibility_guards() -> None:
    wiki_view = Path("ui/web/wiki-view.js").read_text(encoding="utf-8")

    assert "self._destroyed = false" in wiki_view
    assert "this._destroyed = true" in wiki_view
    assert "if (self._destroyed) return;" in wiki_view
    assert 'err && err.name === "AbortError"' in wiki_view
    assert "function _wikiEscapeCssIdent(value)" in wiki_view
    assert "CSS.escape(catId)" not in wiki_view
    assert 'slug.split("/").map(encodeURIComponent).join("/")' in wiki_view


def test_wiki_view_citation_pattern_accepts_real_meeting_ids() -> None:
    wiki_view = Path("ui/web/wiki-view.js").read_text(encoding="utf-8")

    assert "[A-Za-z0-9_]+" in wiki_view
    assert "[a-f0-9]{8}" not in wiki_view
    assert "meeting_YYYYMMDD_HHMMSS" in wiki_view


def test_ab_test_view_exposes_factory_boundary() -> None:
    ab_test_view = Path("ui/web/ab-test-view.js").read_text(encoding="utf-8")
    spa_js = Path("ui/web/spa.js").read_text(encoding="utf-8")

    assert "window.MeetingAbTestView" in ab_test_view
    assert "create: create" in ab_test_view
    assert "ListView: AbTestListView" in ab_test_view
    assert "NewView: AbTestNewView" in ab_test_view
    assert "ResultView: AbTestResultView" in ab_test_view
    assert "AbTestViewModule.create({" in spa_js
    assert "var AbTestListView = AbTestViews.ListView;" in spa_js
    assert "function AbTestListView()" not in spa_js
    assert "function AbTestNewView()" not in spa_js
    assert "function AbTestResultView(" not in spa_js


def test_ab_test_view_preserves_lifecycle_guards() -> None:
    ab_test_view = Path("ui/web/ab-test-view.js").read_text(encoding="utf-8")

    assert ab_test_view.count("self._destroyed = false") >= 3
    assert ab_test_view.count("this._destroyed = true") >= 3
    assert "if (self._destroyed) return;" in ab_test_view
    assert "if (this._destroyed) return;" in ab_test_view
    assert "removeEventListener(l.type, l.fn)" in ab_test_view
    assert "clearInterval(this._timers[i])" in ab_test_view


def test_search_view_exposes_factory_boundary() -> None:
    search_view = Path("ui/web/search-view.js").read_text(encoding="utf-8")
    spa_js = Path("ui/web/spa.js").read_text(encoding="utf-8")

    assert "window.MeetingSearchView" in search_view
    assert "create: create" in search_view
    assert "return SearchView;" in search_view
    assert "SearchViewModule.create({" in spa_js
    assert "SearchView: SearchView" in spa_js
    assert "function SearchView()" not in spa_js


def test_search_view_preserves_lifecycle_guards() -> None:
    search_view = Path("ui/web/search-view.js").read_text(encoding="utf-8")

    assert "self._destroyed = false" in search_view
    assert "self._searchSeq = 0" in search_view
    assert "seq !== self._searchSeq" in search_view
    assert "if (self._destroyed || seq !== self._searchSeq) return;" in search_view
    assert "this._destroyed = true" in search_view
    assert "this._searchSeq += 1" in search_view
    assert "entry.el.removeEventListener(entry.type, entry.fn)" in search_view


def test_empty_view_exposes_factory_boundary() -> None:
    empty_view = Path("ui/web/empty-view.js").read_text(encoding="utf-8")
    spa_js = Path("ui/web/spa.js").read_text(encoding="utf-8")

    assert "window.MeetingEmptyView" in empty_view
    assert "create: create" in empty_view
    assert "return EmptyView;" in empty_view
    assert "EmptyViewModule.create({" in spa_js
    assert "showBulkToast: BulkActionBar.showBulkToast" in spa_js
    assert "EmptyView: EmptyView" in spa_js
    assert "function EmptyView()" not in spa_js
    assert "function _mountHomeDropdowns()" not in spa_js


def test_empty_view_preserves_lifecycle_guards() -> None:
    empty_view = Path("ui/web/empty-view.js").read_text(encoding="utf-8")

    assert "self._destroyed = false" in empty_view
    assert "self._statsSeq = 0" in empty_view
    assert "self._folderSeq = 0" in empty_view
    assert "self._statusTimeouts = []" in empty_view
    assert "seq !== self._statsSeq" in empty_view
    assert "seq !== self._folderSeq" in empty_view
    assert "this._destroyed = true" in empty_view
    assert "this._statsSeq += 1" in empty_view
    assert "this._folderSeq += 1" in empty_view
    assert "clearTimeout(timeoutId)" in empty_view
    assert 'removeEventListener("recap:dashboard-refresh"' in empty_view


def test_global_resource_bar_exposes_factory_boundary() -> None:
    resource_bar = Path("ui/web/global-resource-bar.js").read_text(encoding="utf-8")
    spa_js = Path("ui/web/spa.js").read_text(encoding="utf-8")

    assert "window.MeetingGlobalResourceBar" in resource_bar
    assert "create: create" in resource_bar
    assert "return { start: start, stop: stop, refresh: _refresh }" in resource_bar
    assert "GlobalResourceBarModule.create({" in spa_js
    assert "intervalMs: 5000" in spa_js
    assert "var GlobalResourceBar = (function ()" not in spa_js


def test_bulk_action_bar_exposes_factory_boundary() -> None:
    bulk_action_bar = Path("ui/web/bulk-action-bar.js").read_text(encoding="utf-8")
    spa_js = Path("ui/web/spa.js").read_text(encoding="utf-8")

    assert "window.MeetingBulkActionBar" in bulk_action_bar
    assert "create: create" in bulk_action_bar
    assert "showBulkToast: showBulkToast" in bulk_action_bar
    assert "BulkActionBarModule.create({" in spa_js
    assert "ListPanel: ListPanel" in spa_js
    assert "BulkActionBar: BulkActionBar" in spa_js
    assert "var BulkActionBar = (function ()" not in spa_js


def test_global_shell_controllers_expose_factory_boundaries() -> None:
    theme_controller = Path("ui/web/theme-controller.js").read_text(encoding="utf-8")
    mobile_drawer = Path("ui/web/mobile-drawer.js").read_text(encoding="utf-8")
    shortcut_controller = Path("ui/web/shortcut-controller.js").read_text(encoding="utf-8")
    spa_js = Path("ui/web/spa.js").read_text(encoding="utf-8")

    assert "window.MeetingThemeController" in theme_controller
    assert "create: create" in theme_controller
    assert "toggle: toggle" in theme_controller
    assert "ThemeControllerModule.create({" in spa_js
    assert "ThemeController: ThemeController" in spa_js
    assert "function initThemeToggle()" not in spa_js

    assert "window.MeetingMobileDrawer" in mobile_drawer
    assert "create: create" in mobile_drawer
    assert "isOpen: isOpen" in mobile_drawer
    assert "MobileDrawerModule.create({" in spa_js
    assert "MobileDrawer: MobileDrawer" in spa_js
    assert "function initMobileDrawer()" not in spa_js

    assert "window.MeetingShortcutController" in shortcut_controller
    assert "create: create" in shortcut_controller
    assert "start: start" in shortcut_controller
    assert "ShortcutControllerModule.create({" in spa_js
    assert "ShortcutController: ShortcutController" in spa_js
    assert 'document.addEventListener("keydown", function (e)' not in spa_js
    assert "function _showBulkToast(" not in spa_js


def test_bulk_action_bar_preserves_behavior_guards() -> None:
    bulk_action_bar = Path("ui/web/bulk-action-bar.js").read_text(encoding="utf-8")

    assert "var _inFlight = false" in bulk_action_bar
    assert "if (_inFlight) return;" in bulk_action_bar
    assert 'App.apiPost("/meetings/batch"' in bulk_action_bar
    assert 'scope: "selected"' in bulk_action_bar
    assert "meeting_ids: ids" in bulk_action_bar
    assert '(action === "both") ? "full" : action' in bulk_action_bar
    assert "ListPanel.getSelectedIds()" in bulk_action_bar
    assert "ListPanel.clearSelection()" in bulk_action_bar
    assert 'doc.addEventListener("recap:selection-changed"' in bulk_action_bar
    assert 'setAttribute("role", role)' in bulk_action_bar


def test_global_resource_bar_preserves_lifecycle_guards() -> None:
    resource_bar = Path("ui/web/global-resource-bar.js").read_text(encoding="utf-8")

    assert "var _refreshSeq = 0" in resource_bar
    assert "var _stopped = true" in resource_bar
    assert "if (_stopped) return;" in resource_bar
    assert "seq !== _refreshSeq" in resource_bar
    assert "_refreshSeq += 1" in resource_bar
    assert 'setAttribute("role", "status")' in resource_bar
    assert 'setAttribute("aria-live", "polite")' in resource_bar
    assert 'App.apiRequest("/system/resources")' in resource_bar


def test_viewer_recovery_actions_distinguish_retry_from_restart() -> None:
    viewer_view = Path("ui/web/viewer-view.js").read_text(encoding="utf-8")
    style_css = Path("ui/web/style.css").read_text(encoding="utf-8")

    assert "실패한 단계부터 다시 시도" in viewer_view
    assert "기존 결과와 진행 기록을 유지" in viewer_view
    assert "/retry" in viewer_view
    assert "실패한 단계부터 다시 시도 실패" in viewer_view

    assert "viewer-action-btn retranscribe" in viewer_view
    assert "처음부터 다시 전사" in viewer_view
    assert "기존 전사문, 요약, 진행 기록을 삭제" in viewer_view
    assert "/re-transcribe" in viewer_view
    assert "일시적인 오류라면 '실패한 단계부터 다시 시도'" in viewer_view
    assert "처음부터 다시 전사 요청 중" in viewer_view
    assert ".viewer-action-btn.retranscribe" in style_css


def test_openai_transcription_settings_keep_credentials_separate_from_general_settings() -> None:
    settings_view = Path("ui/web/settings-view.js").read_text(encoding="utf-8")
    settings_css = Path("ui/web/settings.css").read_text(encoding="utf-8")

    general_panel = settings_view[
        settings_view.index("function GeneralSettingsPanel") : settings_view.index(
            "// === PromptsSettingsPanel"
        )
    ]
    save_settings = general_panel[
        general_panel.index("GeneralSettingsPanel.prototype._saveSettings") : general_panel.index(
            "GeneralSettingsPanel.prototype._loadAutoProcessingStatus"
        )
    ]

    assert 'App.apiRequest("/transcription-models")' in general_panel
    assert 'App.apiRequest("/openai-credentials", {' in general_panel
    assert 'method: "PUT"' in general_panel
    assert 'method: "DELETE"' in general_panel
    assert "body: JSON.stringify({ api_key: apiKey })" in general_panel
    assert 'type="password"' in general_panel
    assert 'autocomplete="new-password"' in general_panel
    assert 'els.openAIKeyInput.value = ""' in general_panel
    assert "localStorage" not in general_panel

    assert "stt_provider:" in save_settings
    assert "stt_openai_model:" in save_settings
    assert "external_upload_confirmed:" in save_settings
    assert "selectedTranscriptionModel.model || selectedTranscriptionModel.id" in save_settings
    assert "api_key" not in save_settings
    assert ".external-upload-confirmation" in settings_css
    assert ".credential-status.configured" in settings_css


def test_viewer_alternate_transcription_is_confirmed_non_destructive_ab_test() -> None:
    viewer_view = Path("ui/web/viewer-view.js").read_text(encoding="utf-8")
    style_css = Path("ui/web/style.css").read_text(encoding="utf-8")

    alternate_flow = viewer_view[
        viewer_view.index(
            "ViewerView.prototype._openAlternateTranscriptionDialog"
        ) : viewer_view.index("ViewerView.prototype._handleStepProgress")
    ]

    assert "다른 모델로 텍스트 변환하기…" in viewer_view
    assert "기존 회의록은 유지" in viewer_view
    assert 'if (data.status === "completed") {' in viewer_view
    assert 'App.apiRequest("/transcription-models"' in alternate_flow
    assert 'App.apiRequest("/ab-tests/stt"' in alternate_flow
    assert 'backend: "mlx"' in alternate_flow
    assert 'backend: "openai"' in alternate_flow
    assert "allow_diarize_rerun: false" in alternate_flow
    assert "external_upload_confirmed: true" in alternate_flow
    assert 'Router.navigate("/app/settings?focus=openai")' in alternate_flow
    assert 'Router.navigate("/app/ab-test/"' in alternate_flow
    assert "consentInput.checked" in alternate_flow
    assert "abortController.abort()" in alternate_flow
    assert 'dialog.addEventListener("cancel", onDialogCancel)' in alternate_flow
    assert 'dialog.removeEventListener("cancel", onDialogCancel)' in alternate_flow
    assert "if (requestInFlight) event.preventDefault()" in alternate_flow
    assert "setDialogDismissalBlocked(true)" in alternate_flow
    assert "setDialogDismissalBlocked(false)" in alternate_flow
    assert "closeBtn.disabled = blocked" in alternate_flow
    assert "cancelBtn.disabled = blocked" in alternate_flow
    assert "settingsBtn.disabled = blocked" in alternate_flow
    assert 'dialog.setAttribute("aria-busy", "true")' in alternate_flow
    assert "self._els.viewerActions.querySelector(" in alternate_flow
    assert '".alternate-transcription"' in alternate_flow
    assert "this._alternateTranscriptionCleanup(false)" in viewer_view
    assert 'id="viewerMetaTranscription"' in viewer_view
    assert 'data.stt_provider === "openai"' in viewer_view

    assert "dialog.transcription-model-dialog" in style_css
    assert ".external-upload-warning" in style_css
    assert ".external-upload-consent" in style_css


def test_viewer_recorded_meeting_has_one_off_transcription_model_selection() -> None:
    """개별 전사 선택은 전역 설정과 분리되고 OpenAI 동의를 파일마다 요구한다."""
    viewer_view = Path("ui/web/viewer-view.js").read_text(encoding="utf-8")
    style_css = Path("ui/web/style.css").read_text(encoding="utf-8")

    start_flow = viewer_view[
        viewer_view.index(
            "ViewerView.prototype._openStartTranscriptionDialog"
        ) : viewer_view.index("ViewerView.prototype._transcribeMeeting")
    ]

    assert 'if (data.status === "recorded") {' in viewer_view
    assert 'transcribeBtn.setAttribute("aria-haspopup", "dialog")' in viewer_view
    assert "이 회의 전사 시작" in start_flow
    assert "이 선택은 이 회의에만 적용되며 기본 전사 모델은 바뀌지 않습니다." in start_flow
    assert 'App.apiRequest("/transcription-models"' in start_flow
    assert 'name = "meetingTranscriptionModel"' in start_flow
    assert "model_id: selectedModel.id" in start_flow
    assert "external_upload_confirmed: external" in start_flow
    assert "consentInput.checked" in start_flow
    assert 'Router.navigate("/app/settings?focus=openai")' in start_flow
    assert 'App.apiPost("/settings"' not in start_flow
    assert "localStorage" not in start_flow
    assert "abortController.abort()" in start_flow
    assert "abortController.signal" in start_flow
    assert "setDialogDismissalBlocked(true)" in start_flow
    assert "this._startTranscriptionCleanup(false)" in viewer_view

    transcribe_flow = viewer_view[
        viewer_view.index("ViewerView.prototype._transcribeMeeting") : viewer_view.index(
            "ViewerView.prototype._retryMeeting"
        )
    ]
    assert "signal: signal" in transcribe_flow
    assert "if (signal && signal.aborted) return;" in transcribe_flow

    assert ".transcription-model-choice-list" in style_css
    assert ".transcription-model-choice.selected" in style_css
    assert ".external-upload-warning[hidden]" in style_css


def test_viewer_missing_transcript_uses_meeting_status_not_unconditional_polling() -> None:
    viewer_view = Path("ui/web/viewer-view.js").read_text(encoding="utf-8")

    load_transcript = viewer_view[
        viewer_view.index("ViewerView.prototype._loadTranscript") : viewer_view.index(
            "ViewerView.prototype._handleMissingTranscript"
        )
    ]
    missing_handler = viewer_view[
        viewer_view.index("ViewerView.prototype._handleMissingTranscript") : viewer_view.index(
            "ViewerView.prototype._startPipelinePolling"
        )
    ]

    assert "self._handleMissingTranscript(self._lastMeetingData)" in load_transcript
    assert "self._startPipelinePolling()" not in load_transcript

    assert "ViewerView.prototype._stopPipelinePolling" in viewer_view
    assert "self._stopPipelinePolling();" in missing_handler
    assert 'status === "recorded"' in missing_handler
    assert "전사 시작 대기 중" in missing_handler
    assert 'status === "failed"' in missing_handler
    assert "전사 처리 실패" in missing_handler
    assert 'status === "completed"' in missing_handler
    assert "전사문을 찾을 수 없습니다" in missing_handler

    fallback_index = missing_handler.index("self._startPipelinePolling();")
    assert missing_handler.index('status === "recorded"') < fallback_index
    assert missing_handler.index('status === "failed"') < fallback_index
