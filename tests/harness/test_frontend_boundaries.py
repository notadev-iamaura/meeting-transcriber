"""프론트엔드 모듈 경계 스모크 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.harness


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


def test_api_client_preserves_abort_error_contract() -> None:
    api_client = Path("ui/web/api-client.js").read_text(encoding="utf-8")

    assert 'networkError.name === "AbortError"' in api_client
    assert "throw networkError;" in api_client


def test_api_client_supports_non_json_success_payloads() -> None:
    api_client = Path("ui/web/api-client.js").read_text(encoding="utf-8")

    assert 'response.headers.get("content-type")' in api_client
    assert 'contentType.indexOf("application/json")' in api_client
    assert "return response.text();" in api_client


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
