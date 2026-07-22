#!/usr/bin/env python3
"""Generate the mechanical async API mixins from ``sync_api.py``.

The public async classes inherit these mixins. Methods with callback bridges,
native async implementations, cancellation/close semantics, sliced waits, or
other async-specific behavior stay in ``async_api.py`` and are inventoried in
``HAND_METHODS`` below.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import re
from pathlib import Path
from typing import Iterable, Optional


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SYNC_API = ROOT / "python" / "rustwright" / "sync_api.py"
DEFAULT_OUTPUT = ROOT / "python" / "rustwright" / "_async_generated.py"


ASYNC_TO_SYNC_CLASS = {
    "AsyncClock": "Clock",
    "AsyncDebugger": "Debugger",
    "AsyncScreencastFrame": "ScreencastFrame",
    "AsyncScreencast": "Screencast",
    "AsyncVideo": "Video",
    "AsyncBrowserType": "BrowserType",
    "AsyncSelectors": "Selectors",
    "AsyncPlaywright": "Playwright",
    "AsyncRequest": "Request",
    "AsyncResponse": "Response",
    "AsyncDialog": "Dialog",
    "AsyncDownload": "Download",
    "AsyncFileChooser": "FileChooser",
    "AsyncCDPSession": "CDPSession",
    "AsyncAccessibility": "Accessibility",
    "AsyncTracing": "Tracing",
    "AsyncBrowser": "Browser",
    "AsyncBrowserContext": "BrowserContext",
    "AsyncPage": "Page",
    "AsyncJSHandle": "JSHandle",
    "AsyncFrame": "Frame",
    "AsyncLocator": "Locator",
    "AsyncElementHandle": "ElementHandle",
    "AsyncKeyboard": "Keyboard",
    "AsyncMouse": "Mouse",
    "AsyncTouchscreen": "Touchscreen",
    "AsyncAPIResponse": "APIResponse",
    "AsyncRoute": "Route",
    "AsyncAPIRequest": "APIRequest",
    "AsyncAPIRequestContext": "APIRequestContext",
    "AsyncWebSocketRoute": "WebSocketRoute",
    "AsyncWorker": "Worker",
}


# Explicit source-level inventory. A method belongs in exactly one of these
# tables. Keeping the boundary here makes async-specific behavior a deliberate
# choice rather than a heuristic made by the generator.
GENERATED_METHODS = {
    "AsyncClock": ("install", "set_fixed_time", "set_system_time", "pause_at", "resume", "fast_forward", "run_for"),
    "AsyncDebugger": ("request_pause", "resume", "next", "run_to"),
    "AsyncScreencastFrame": ("save_as",),
    "AsyncScreencast": ("stop", "show_overlay", "hide_overlays", "show_overlays", "show_actions", "hide_actions", "show_chapter"),
    "AsyncVideo": ("path", "delete"),
    "AsyncBrowserType": ("launch_persistent_context", "connect_over_cdp", "connect"),
    "AsyncSelectors": ("register",),
    "AsyncPlaywright": ("stop",),
    "AsyncRequest": ("all_headers", "header_value", "headers_array", "response", "sizes"),
    "AsyncResponse": ("all_headers", "body", "finished", "header_value", "header_values", "headers_array", "http_version", "json", "security_details", "server_addr", "text"),
    "AsyncDialog": ("accept", "dismiss"),
    "AsyncDownload": ("path", "save_as", "failure", "delete", "cancel"),
    "AsyncFileChooser": ("set_files",),
    "AsyncCDPSession": ("send", "detach"),
    "AsyncAccessibility": ("snapshot",),
    "AsyncTracing": ("start", "stop", "start_chunk", "stop_chunk", "group", "group_end"),
    "AsyncBrowser": ("new_browser_cdp_session", "start_tracing", "stop_tracing", "bind", "unbind"),
    "AsyncBrowserContext": ("add_init_script", "cookies", "add_cookies", "clear_cookies", "storage_state", "set_storage_state", "set_extra_http_headers", "grant_permissions", "clear_permissions", "set_geolocation", "set_offline", "route_from_har", "new_cdp_session"),
    "AsyncPage": ("reload", "go_back", "go_forward", "wait_for_timeout", "set_content", "add_init_script", "evaluate_handle", "set_extra_http_headers", "set_viewport_size", "emulate_media", "title", "content", "query_selector", "query_selector_all", "eval_on_selector", "eval_on_selector_all", "dispatch_event", "get_attribute", "inner_html", "text_content", "input_value", "is_visible", "is_hidden", "is_enabled", "is_disabled", "is_checked", "is_editable", "route_from_har", "pdf", "bring_to_front", "requests", "console_messages", "clear_console_messages", "page_errors", "clear_page_errors", "request_gc", "pause", "pick_locator", "cancel_pick_locator", "aria_snapshot", "wait_for_url", "wait_for_function", "dblclick", "type", "press", "hover", "tap", "drag_and_drop", "focus", "check", "uncheck", "select_option", "set_input_files", "set_checked", "opener"),
    "AsyncJSHandle": ("json_value", "get_property", "get_properties", "evaluate", "evaluate_handle", "dispose"),
    "AsyncFrame": ("query_selector", "query_selector_all", "evaluate", "evaluate_handle", "eval_on_selector", "eval_on_selector_all", "dispatch_event", "content", "title", "set_content", "goto", "wait_for_timeout", "add_script_tag", "add_style_tag", "text_content", "inner_text", "inner_html", "get_attribute", "input_value", "is_visible", "is_hidden", "is_enabled", "is_disabled", "is_checked", "is_editable", "frame_element", "wait_for_selector", "wait_for_url", "wait_for_function", "click", "dblclick", "fill", "type", "press", "hover", "tap", "focus", "check", "uncheck", "set_checked", "select_option", "set_input_files", "drag_and_drop"),
    "AsyncLocator": ("count", "evaluate", "evaluate_handle", "evaluate_all", "dispatch_event", "inner_text", "inner_html", "text_content", "get_attribute", "is_visible", "is_hidden", "is_enabled", "is_disabled", "is_checked", "is_editable", "input_value", "all_inner_texts", "all_text_contents", "all", "element_handles", "bounding_box", "highlight", "aria_snapshot", "element_handle", "normalize", "click", "dblclick", "fill", "type", "press", "hover", "tap", "focus", "blur", "clear", "check", "uncheck", "set_checked", "select_option", "set_input_files", "scroll_into_view_if_needed", "select_text", "press_sequentially", "screenshot", "wait_for"),
    "AsyncElementHandle": ("dispatch_event", "evaluate", "evaluate_handle", "eval_on_selector", "eval_on_selector_all", "query_selector", "query_selector_all", "focus", "text_content", "inner_text", "inner_html", "get_attribute", "is_visible", "is_hidden", "is_enabled", "is_disabled", "is_editable", "input_value", "is_checked", "bounding_box", "get_property", "get_properties", "json_value", "content_frame", "owner_frame", "dispose", "click", "dblclick", "fill", "type", "press", "hover", "tap", "wait_for_selector", "check", "uncheck", "set_checked", "select_option", "set_input_files", "scroll_into_view_if_needed", "select_text", "wait_for_element_state", "screenshot"),
    "AsyncKeyboard": ("type", "insert_text", "press", "down", "up"),
    "AsyncMouse": ("move", "click", "dblclick", "down", "up", "wheel"),
    "AsyncTouchscreen": ("tap",),
    "AsyncAPIResponse": ("body", "text", "json", "dispose"),
    "AsyncRoute": ("continue_", "fallback", "fetch", "abort", "fulfill"),
    "AsyncAPIRequest": ("new_context",),
    "AsyncAPIRequestContext": ("storage_state", "dispose"),
    "AsyncWebSocketRoute": ("close",),
    "AsyncWorker": ("evaluate", "evaluate_handle"),
}


HAND_METHODS = {
    "_AsyncEventContextManager": ("__aenter__", "__aexit__"),
    "AsyncScreencast": ("start",),
    "AsyncVideo": ("save_as",),
    "AsyncBrowserType": ("launch",),
    "_AsyncPlaywrightContextManager": ("__aenter__", "__aexit__", "start", "_stop"),
    "AsyncExpectation": ("_run_sync_assertion", "to_have_text", "to_contain_text", "to_be_visible", "to_be_hidden", "to_be_enabled", "to_be_disabled", "to_be_editable", "to_be_checked", "to_be_attached", "to_be_empty", "to_be_focused", "to_have_count", "to_have_value", "to_have_values", "to_have_attribute", "to_have_id", "to_have_class", "to_contain_class", "to_have_role", "to_have_accessible_name", "to_have_accessible_description", "to_have_accessible_error_message", "to_match_aria_snapshot", "to_have_css", "to_have_js_property", "to_be_in_viewport", "to_be_ok", "to_have_title", "to_have_url"),
    "AsyncBrowser": ("__aenter__", "__aexit__", "new_page", "new_context", "close", "_close_native"),
    "AsyncBrowserContext": ("__aenter__", "__aexit__", "new_page", "close", "_close_for_browser_close", "_close_native", "_close_native_impl", "expose_function", "expose_binding", "route", "unroute", "unroute_all", "wait_for_event", "route_web_socket"),
    "AsyncPage": ("_event_pump", "_consume_event_batch", "goto", "wait_for_load_state", "evaluate", "add_script_tag", "add_style_tag", "expose_function", "expose_binding", "wait_for_selector", "click", "fill", "inner_text", "wait_for_event", "route", "unroute", "unroute_all", "route_web_socket", "screenshot", "add_locator_handler", "remove_locator_handler", "close", "_close_native"),
    "AsyncFrame": ("wait_for_load_state",),
    "AsyncLocator": ("drag_to",),
    "AsyncAPIRequestContext": ("_run_request", "fetch", "get", "post", "put", "patch", "delete", "head"),
    "AsyncWebSocket": ("wait_for_event",),
}


# Non-class async defs are all hand-maintained infrastructure or factories.
# The duplicate alias entry represents the two branch-local definitions in
# _install_async_expectation_negated_aliases.make_alias.
HAND_ASYNC_HELPERS = (
    "_DEFAULT_ASYNCIO_TO_THREAD",
    "_run_sync_call",
    "_await_native",
    "_await_native_method",
    "_await_native_action",
    "_await_cleanup_completion",
    "_single_flight_close",
    "_native_context_page",
    "_finish_native_page",
    "_run_sync_wait_sliced",
    "_run_awaitable_on_loop.runner",
    "_install_async_expectation_negated_aliases.make_alias.alias",
    "_install_async_expectation_negated_aliases.make_alias.alias",
    "_call_async_assertion_impl_method",
    "_make_async_assertion_impl_method.method",
    "_make_async_assertion_public_method.method",
)


# Return wrapping is intentionally explicit. The helper names refer to the
# existing identity-preserving wrappers in async_api.py. Locator/APIResponse
# match the current direct-constructor behavior.
RETURN_WRAPPERS = {
    "APIRequestContext": "_wrap_async_api_request_context",
    "APIResponse": "AsyncAPIResponse",
    "Browser": "_wrap_async_browser",
    "BrowserContext": "_wrap_async_browser_context",
    "CDPSession": "_wrap_async_cdp_session",
    "ConsoleMessage": "_wrap_async_console_message",
    "Debugger": "_wrap_async_debugger",
    "Dialog": "_wrap_async_dialog",
    "Download": "_wrap_async_download",
    "ElementHandle": "_wrap_async_element_handle",
    "FileChooser": "_wrap_async_file_chooser",
    "Frame": "_wrap_async_frame",
    "JSHandle": "_wrap_async_js_handle",
    "Locator": "AsyncLocator",
    "Page": "_wrap_async_page",
    "Request": "_wrap_async_request",
    "Response": "_wrap_async_response",
    "WebError": "_wrap_async_web_error",
    "WebSocket": "_wrap_async_websocket",
    "Worker": "_wrap_async_worker",
}


# Parameters that cross from the async wrapper back into the sync API need to
# be unwrapped. This remains data, not hand-written method bodies.
UNWRAP_ARGUMENTS = {
    "AsyncAccessibility.snapshot": ("root",),
    "AsyncBrowser.start_tracing": ("page",),
    "AsyncBrowserContext.new_cdp_session": ("page",),
    "AsyncPage.evaluate_handle": ("arg",),
    "AsyncPage.wait_for_function": ("arg",),
    "AsyncPage.eval_on_selector": ("arg",),
    "AsyncPage.eval_on_selector_all": ("arg",),
    "AsyncPage.dispatch_event": ("event_init",),
    "AsyncJSHandle.evaluate": ("arg",),
    "AsyncJSHandle.evaluate_handle": ("arg",),
    "AsyncFrame.evaluate": ("arg",),
    "AsyncFrame.evaluate_handle": ("arg",),
    "AsyncFrame.wait_for_function": ("arg",),
    "AsyncFrame.eval_on_selector": ("arg",),
    "AsyncFrame.eval_on_selector_all": ("arg",),
    "AsyncFrame.dispatch_event": ("event_init",),
    "AsyncLocator.evaluate": ("arg",),
    "AsyncLocator.evaluate_handle": ("arg",),
    "AsyncLocator.evaluate_all": ("arg",),
    "AsyncLocator.dispatch_event": ("event_init",),
    "AsyncElementHandle.dispatch_event": ("event_init",),
    "AsyncElementHandle.evaluate": ("arg",),
    "AsyncElementHandle.evaluate_handle": ("arg",),
    "AsyncElementHandle.eval_on_selector": ("arg",),
    "AsyncElementHandle.eval_on_selector_all": ("arg",),
    "AsyncRoute.fulfill": ("response",),
    "AsyncWorker.evaluate": ("arg",),
    "AsyncWorker.evaluate_handle": ("arg",),
}


# These methods intentionally slice blocking sync waits so cancellation and
# unrelated event-loop work can make progress. The method bodies are otherwise
# ordinary delegation, so the runner choice stays explicit generator data.
SLICED_WAIT_METHODS = {
    "AsyncPage": (
        "wait_for_url", "wait_for_function", "dblclick", "type", "press",
        "hover", "tap", "drag_and_drop", "focus", "check", "uncheck",
        "select_option", "set_input_files", "set_checked",
    ),
    "AsyncFrame": (
        "wait_for_selector", "wait_for_url", "wait_for_function", "click",
        "dblclick", "fill", "type", "press", "hover", "tap", "focus",
        "check", "uncheck", "set_checked", "select_option",
        "set_input_files", "drag_and_drop",
    ),
    "AsyncLocator": (
        "click", "dblclick", "fill", "type", "press", "hover", "tap",
        "focus", "blur", "clear", "check", "uncheck", "set_checked",
        "select_option", "set_input_files", "scroll_into_view_if_needed",
        "select_text", "press_sequentially", "wait_for",
    ),
    "AsyncElementHandle": (
        "click", "dblclick", "fill", "type", "press", "hover", "tap",
        "wait_for_selector", "check", "uncheck", "set_checked",
        "select_option", "set_input_files", "scroll_into_view_if_needed",
        "select_text", "wait_for_element_state",
    ),
}


# A few sync methods accept either one async wrapper or a sequence of wrappers.
# Preserve the former hand-written behavior: tuples become lists and only the
# direct sequence items are unwrapped.
WRAPPER_SEQUENCE_ARGUMENTS = {
    "AsyncFrame.select_option": ("element",),
    "AsyncLocator.select_option": ("element",),
    "AsyncLocator.screenshot": ("mask",),
    "AsyncElementHandle.select_option": ("element",),
    "AsyncElementHandle.screenshot": ("mask",),
}


# These signatures intentionally preserve the current async public surface
# where its annotations, keyword ordering, or defaults differ from sync_api.
# The generator still requires the corresponding sync method and derives the
# call and return wrapping from it.
SIGNATURE_OVERRIDES = {
    "AsyncClock.install": "async def install(self, **kwargs: Any) -> None: ...",
    "AsyncScreencastFrame.save_as": "async def save_as(self, path: Any) -> None: ...",
    "AsyncScreencast.show_overlay": "async def show_overlay(self, html: str, **kwargs: Any) -> None: ...",
    "AsyncScreencast.show_actions": "async def show_actions(self, **kwargs: Any) -> None: ...",
    "AsyncScreencast.show_chapter": "async def show_chapter(self, title: str, **kwargs: Any) -> None: ...",
    "AsyncBrowserType.launch_persistent_context": "async def launch_persistent_context(self, user_data_dir: Any, *, channel: Optional[str] = None, executable_path: Optional[Any] = None, args: Optional[Any] = None, ignore_default_args: Optional[Any] = None, handle_sigint: Optional[bool] = None, handle_sigterm: Optional[bool] = None, handle_sighup: Optional[bool] = None, timeout: Optional[float] = None, env: Optional[dict[str, Any]] = None, headless: Optional[bool] = None, proxy: Optional[dict[str, Any]] = None, downloads_path: Optional[Any] = None, slow_mo: Optional[float] = None, viewport: Optional[dict[str, Any]] = None, screen: Optional[dict[str, Any]] = None, no_viewport: Optional[bool] = None, ignore_https_errors: Optional[bool] = None, java_script_enabled: Optional[bool] = None, bypass_csp: Optional[bool] = None, user_agent: Optional[str] = None, locale: Optional[str] = None, timezone_id: Optional[str] = None, geolocation: Optional[dict[str, Any]] = None, permissions: Optional[Any] = None, extra_http_headers: Optional[dict[str, str]] = None, offline: Optional[bool] = None, http_credentials: Optional[dict[str, Any]] = None, device_scale_factor: Optional[float] = None, is_mobile: Optional[bool] = None, has_touch: Optional[bool] = None, color_scheme: Optional[str] = None, reduced_motion: Optional[str] = None, forced_colors: Optional[str] = None, contrast: Optional[str] = None, accept_downloads: Optional[bool] = None, traces_dir: Optional[Any] = None, artifacts_dir: Optional[Any] = None, chromium_sandbox: Optional[bool] = None, firefox_user_prefs: Optional[dict[str, Any]] = None, record_har_path: Optional[Any] = None, record_har_omit_content: Optional[bool] = None, record_video_dir: Optional[Any] = None, record_video_size: Optional[dict[str, Any]] = None, base_url: Optional[str] = None, strict_selectors: Optional[bool] = None, service_workers: Optional[str] = None, record_har_url_filter: Optional[Any] = None, record_har_mode: Optional[str] = None, record_har_content: Optional[str] = None, client_certificates: Optional[list[Any]] = None) -> 'AsyncBrowserContext': ...",
    "AsyncBrowserType.connect": "async def connect(self, endpoint: str, *, timeout: Optional[float] = None, slow_mo: Optional[float] = None, headers: Optional[dict[str, str]] = None, expose_network: Optional[str] = None) -> 'AsyncBrowser': ...",
    "AsyncSelectors.register": "async def register(self, name: str, script: Optional[str] = None, *, path: Any = None, content_script: Optional[bool] = None) -> None: ...",
    "AsyncDownload.save_as": "async def save_as(self, path: Any) -> None: ...",
    "AsyncAccessibility.snapshot": "async def snapshot(self, *, interesting_only: Optional[bool] = None, root: Any = None) -> Optional[dict[str, Any]]: ...",
    "AsyncTracing.stop": "async def stop(self, *, path: Optional[Union[str, Path]] = None) -> None: ...",
    "AsyncTracing.stop_chunk": "async def stop_chunk(self, *, path: Optional[Union[str, Path]] = None) -> None: ...",
    "AsyncBrowser.new_browser_cdp_session": "async def new_browser_cdp_session(self) -> 'AsyncCDPSession': ...",
    "AsyncBrowser.start_tracing": "async def start_tracing(self, *, page: Optional['AsyncPage'] = None, path: Any = None, screenshots: Optional[bool] = None, categories: Any = None) -> None: ...",
    "AsyncBrowser.bind": "async def bind(self, title: str, *, workspace_dir: Optional[str] = None, host: Optional[str] = None, port: Optional[int] = None) -> Any: ...",
    "AsyncBrowserContext.add_init_script": "async def add_init_script(self, script: Optional[str] = None, *, path: Any = None) -> None: ...",
    "AsyncBrowserContext.storage_state": "async def storage_state(self, *, path: Any = None, indexed_db: Optional[bool] = None) -> dict[str, Any]: ...",
    "AsyncBrowserContext.set_storage_state": "async def set_storage_state(self, storage_state: Any) -> None: ...",
    "AsyncBrowserContext.set_extra_http_headers": "async def set_extra_http_headers(self, headers: dict[str, str]) -> None: ...",
    "AsyncBrowserContext.set_geolocation": "async def set_geolocation(self, geolocation: Optional[dict[str, Any]] = None) -> None: ...",
    "AsyncBrowserContext.route_from_har": "async def route_from_har(self, har: Any, *, url: Any = None, not_found: Optional[str] = None, update: Optional[bool] = None, update_content: Optional[str] = None, update_mode: Optional[str] = None) -> None: ...",
    "AsyncBrowserContext.new_cdp_session": "async def new_cdp_session(self, page: Union['AsyncPage', 'AsyncFrame']) -> 'AsyncCDPSession': ...",
    "AsyncPage.reload": "async def reload(self, *, timeout: Optional[float] = None, wait_until: Optional[str] = None) -> Optional['AsyncResponse']: ...",
    "AsyncPage.go_back": "async def go_back(self, *, timeout: Optional[float] = None, wait_until: Optional[str] = None) -> Optional['AsyncResponse']: ...",
    "AsyncPage.go_forward": "async def go_forward(self, *, timeout: Optional[float] = None, wait_until: Optional[str] = None) -> Optional['AsyncResponse']: ...",
    "AsyncPage.add_init_script": "async def add_init_script(self, script: Optional[str] = None, *, path: Any = None) -> None: ...",
    "AsyncPage.evaluate_handle": "async def evaluate_handle(self, expression: str, arg: Any = None) -> 'AsyncJSHandle': ...",
    "AsyncPage.set_extra_http_headers": "async def set_extra_http_headers(self, headers: dict[str, str]) -> None: ...",
    "AsyncPage.set_viewport_size": "async def set_viewport_size(self, viewport_size: dict[str, int]) -> None: ...",
    "AsyncPage.emulate_media": "async def emulate_media(self, *, media: Optional[str] = None, color_scheme: Optional[str] = None, reduced_motion: Optional[str] = None, forced_colors: Optional[str] = None, contrast: Optional[str] = None) -> None: ...",
    "AsyncPage.route_from_har": "async def route_from_har(self, har: Any, *, url: Any = None, not_found: Optional[str] = None, update: Optional[bool] = None, update_content: Optional[str] = None, update_mode: Optional[str] = None) -> None: ...",
    "AsyncPage.pdf": "async def pdf(self, *, scale: Any = None, display_header_footer: Optional[bool] = None, header_template: Optional[str] = None, footer_template: Optional[str] = None, print_background: Optional[bool] = None, landscape: Optional[bool] = None, page_ranges: Optional[str] = None, format: Optional[str] = None, width: Any = None, height: Any = None, prefer_css_page_size: Optional[bool] = None, margin: Optional[dict[str, Any]] = None, path: Optional[str] = None, outline: Optional[bool] = None, tagged: Optional[bool] = None) -> bytes: ...",
    "AsyncPage.requests": "async def requests(self) -> list['AsyncRequest']: ...",
    "AsyncPage.console_messages": "async def console_messages(self, *, filter: Optional[str] = None) -> list['AsyncConsoleMessage']: ...",
    "AsyncFrame.goto": "async def goto(self, url: str, *, timeout: Optional[float] = None, wait_until: Optional[str] = None, referer: Optional[str] = None) -> Optional['AsyncResponse']: ...",
    "AsyncFrame.add_script_tag": "async def add_script_tag(self, *, url: Optional[str] = None, path: Any = None, content: Optional[str] = None, type: Optional[str] = None) -> 'AsyncElementHandle': ...",
    "AsyncFrame.add_style_tag": "async def add_style_tag(self, *, url: Optional[str] = None, path: Any = None, content: Optional[str] = None) -> 'AsyncElementHandle': ...",
    "AsyncFrame.frame_element": "async def frame_element(self) -> 'AsyncElementHandle': ...",
    "AsyncRoute.fetch": "async def fetch(self, *, url: Optional[str] = None, method: Optional[str] = None, headers: Optional[dict[str, str]] = None, post_data: Any = None, max_redirects: Optional[int] = None, max_retries: Optional[int] = None, timeout: Optional[float] = None) -> 'AsyncAPIResponse': ...",
    "AsyncRoute.abort": "async def abort(self, error_code: Optional[str] = None) -> None: ...",
    "AsyncRoute.fulfill": "async def fulfill(self, *, status: Optional[int] = None, headers: Optional[dict[str, str]] = None, body: Any = None, json: Any = None, path: Any = None, content_type: Optional[str] = None, response: Optional[Any] = None) -> None: ...",
    "AsyncAPIRequest.new_context": "async def new_context(self, *, base_url: Optional[str] = None, extra_http_headers: Optional[dict[str, str]] = None, http_credentials: Optional[dict[str, Any]] = None, ignore_https_errors: Optional[bool] = None, proxy: Optional[dict[str, Any]] = None, user_agent: Optional[str] = None, timeout: Optional[float] = None, storage_state: Any = None, client_certificates: Optional[list[Any]] = None, fail_on_status_code: Optional[bool] = None, max_redirects: Optional[int] = None) -> 'AsyncAPIRequestContext': ...",
    "AsyncAPIRequestContext.storage_state": "async def storage_state(self, *, path: Any = None, indexed_db: Optional[bool] = None) -> dict[str, Any]: ...",
    "AsyncPage.wait_for_url": "async def wait_for_url(self, url: Any, *, wait_until: Optional[str] = None, timeout: Optional[float] = None) -> None: ...",
    "AsyncPage.wait_for_function": "async def wait_for_function(self, expression: str, *, arg: Any = None, timeout: Optional[float] = None, polling: Any = None) -> 'AsyncJSHandle': ...",
    "AsyncPage.dblclick": "async def dblclick(self, selector: str, *, modifiers: Any = None, position: Any = None, delay: Optional[float] = None, button: Optional[str] = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, strict: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncPage.hover": "async def hover(self, selector: str, *, modifiers: Any = None, position: Any = None, timeout: Optional[float] = None, no_wait_after: Optional[bool] = None, force: Optional[bool] = None, strict: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncPage.tap": "async def tap(self, selector: str, *, modifiers: Any = None, position: Any = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, strict: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncPage.drag_and_drop": "async def drag_and_drop(self, source: str, target: str, *, source_position: Any = None, target_position: Any = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, timeout: Optional[float] = None, strict: Optional[bool] = None, trial: Optional[bool] = None, steps: Optional[int] = None) -> None: ...",
    "AsyncPage.check": "async def check(self, selector: str, *, position: Any = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, strict: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncPage.uncheck": "async def uncheck(self, selector: str, *, position: Any = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, strict: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncPage.set_checked": "async def set_checked(self, selector: str, checked: bool, *, position: Any = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, strict: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncPage.opener": "async def opener(self) -> Any: ...",
    "AsyncFrame.wait_for_url": "async def wait_for_url(self, url: Any, *, wait_until: Optional[str] = None, timeout: Optional[float] = None) -> None: ...",
    "AsyncFrame.click": "async def click(self, selector: str, *, modifiers: Any = None, position: Any = None, delay: Optional[float] = None, button: Optional[str] = None, click_count: Optional[int] = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, strict: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncFrame.dblclick": "async def dblclick(self, selector: str, *, modifiers: Any = None, position: Any = None, delay: Optional[float] = None, button: Optional[str] = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, strict: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncFrame.type": "async def type(self, selector: str, text: str, *, delay: Optional[float] = None, strict: Optional[bool] = None, timeout: Optional[float] = None, no_wait_after: Optional[bool] = None) -> None: ...",
    "AsyncFrame.press": "async def press(self, selector: str, key: str, *, delay: Optional[float] = None, strict: Optional[bool] = None, timeout: Optional[float] = None, no_wait_after: Optional[bool] = None) -> None: ...",
    "AsyncFrame.hover": "async def hover(self, selector: str, *, modifiers: Any = None, position: Any = None, timeout: Optional[float] = None, no_wait_after: Optional[bool] = None, force: Optional[bool] = None, strict: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncFrame.tap": "async def tap(self, selector: str, *, modifiers: Any = None, position: Any = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, strict: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncFrame.check": "async def check(self, selector: str, *, position: Any = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, strict: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncFrame.uncheck": "async def uncheck(self, selector: str, *, position: Any = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, strict: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncFrame.set_checked": "async def set_checked(self, selector: str, checked: bool, *, position: Any = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, strict: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncFrame.drag_and_drop": "async def drag_and_drop(self, source: str, target: str, *, source_position: Any = None, target_position: Any = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, strict: Optional[bool] = None, timeout: Optional[float] = None, trial: Optional[bool] = None, steps: Optional[int] = None) -> None: ...",
    "AsyncLocator.click": "async def click(self, *, modifiers: Any = None, position: Any = None, delay: Optional[float] = None, button: Optional[str] = None, click_count: Optional[int] = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, trial: Optional[bool] = None, steps: Optional[int] = None) -> None: ...",
    "AsyncLocator.dblclick": "async def dblclick(self, *, modifiers: Any = None, position: Any = None, delay: Optional[float] = None, button: Optional[str] = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, trial: Optional[bool] = None, steps: Optional[int] = None) -> None: ...",
    "AsyncLocator.hover": "async def hover(self, *, modifiers: Any = None, position: Any = None, timeout: Optional[float] = None, no_wait_after: Optional[bool] = None, force: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncLocator.tap": "async def tap(self, *, modifiers: Any = None, position: Any = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncLocator.check": "async def check(self, *, position: Any = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncLocator.uncheck": "async def uncheck(self, *, position: Any = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncLocator.set_checked": "async def set_checked(self, checked: bool, *, position: Any = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncLocator.select_option": "async def select_option(self, value: Any = None, *, index: Any = None, label: Any = None, element: Any = None, timeout: Optional[float] = None, no_wait_after: Optional[bool] = None, force: Optional[bool] = None) -> Any: ...",
    "AsyncLocator.wait_for": "async def wait_for(self, *, timeout: Optional[float] = None, state: Optional[str] = None) -> None: ...",
    "AsyncElementHandle.click": "async def click(self, *, modifiers: Any = None, position: Any = None, delay: Optional[float] = None, button: Optional[str] = None, click_count: Optional[int] = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, trial: Optional[bool] = None, steps: Optional[int] = None) -> None: ...",
    "AsyncElementHandle.dblclick": "async def dblclick(self, *, modifiers: Any = None, position: Any = None, delay: Optional[float] = None, button: Optional[str] = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, trial: Optional[bool] = None, steps: Optional[int] = None) -> None: ...",
    "AsyncElementHandle.hover": "async def hover(self, *, modifiers: Any = None, position: Any = None, timeout: Optional[float] = None, no_wait_after: Optional[bool] = None, force: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncElementHandle.tap": "async def tap(self, *, modifiers: Any = None, position: Any = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncElementHandle.check": "async def check(self, *, position: Any = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncElementHandle.uncheck": "async def uncheck(self, *, position: Any = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
    "AsyncElementHandle.set_checked": "async def set_checked(self, checked: bool, *, position: Any = None, timeout: Optional[float] = None, force: Optional[bool] = None, no_wait_after: Optional[bool] = None, trial: Optional[bool] = None) -> None: ...",
}


TYPE_RENAMES = {
    **{sync_name: async_name for async_name, sync_name in ASYNC_TO_SYNC_CLASS.items()},
    "APIResponseAssertions": "AsyncAPIResponseAssertions",
    "ConsoleMessage": "AsyncConsoleMessage",
    "FileChooser": "AsyncFileChooser",
    "FrameLocator": "AsyncFrameLocator",
    "LocatorAssertions": "AsyncLocatorAssertions",
    "PageAssertions": "AsyncPageAssertions",
    "WebError": "AsyncWebError",
    "WebSocket": "AsyncWebSocket",
}


class _AsyncAnnotationTransformer(ast.NodeTransformer):
    def __init__(self) -> None:
        names = sorted(TYPE_RENAMES, key=len, reverse=True)
        self._forward_ref_pattern = re.compile(
            r"(?<![A-Za-z0-9_])(" + "|".join(map(re.escape, names)) + r")(?![A-Za-z0-9_])"
        )

    def visit_Name(self, node: ast.Name) -> ast.AST:
        renamed = TYPE_RENAMES.get(node.id)
        if renamed is not None:
            node.id = renamed
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, str):
            node.value = self._forward_ref_pattern.sub(
                lambda match: TYPE_RENAMES[match.group(1)],
                node.value,
            )
        return node


def _class_methods(tree: ast.Module) -> dict[str, dict[str, ast.FunctionDef]]:
    result: dict[str, dict[str, ast.FunctionDef]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        result[node.name] = {
            member.name: member
            for member in node.body
            if isinstance(member, ast.FunctionDef)
        }
    return result


def _parse_signature_override(source: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(source)
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.AsyncFunctionDef):
        raise ValueError(f"invalid async signature override: {source!r}")
    return tree.body[0]


def _generated_signature(
    async_class: str,
    sync_method: ast.FunctionDef,
) -> ast.AsyncFunctionDef:
    key = f"{async_class}.{sync_method.name}"
    override = SIGNATURE_OVERRIDES.get(key)
    if override is not None:
        method = _parse_signature_override(override)
        if method.name != sync_method.name:
            raise ValueError(f"signature override name mismatch for {key}")
        return method

    transformer = _AsyncAnnotationTransformer()
    arguments = copy.deepcopy(sync_method.args)
    for argument in (
        *arguments.posonlyargs,
        *arguments.args,
        *arguments.kwonlyargs,
    ):
        if argument.annotation is not None:
            argument.annotation = transformer.visit(argument.annotation)
    if arguments.vararg is not None and arguments.vararg.annotation is not None:
        arguments.vararg.annotation = transformer.visit(arguments.vararg.annotation)
    if arguments.kwarg is not None and arguments.kwarg.annotation is not None:
        arguments.kwarg.annotation = transformer.visit(arguments.kwarg.annotation)
    returns = copy.deepcopy(sync_method.returns)
    if returns is not None:
        returns = transformer.visit(returns)
    return ast.AsyncFunctionDef(
        name=sync_method.name,
        args=arguments,
        body=[],
        decorator_list=[],
        returns=returns,
        type_comment=None,
    )


def _argument_value(key: str, name: str) -> ast.expr:
    value: ast.expr = ast.Name(id=name, ctx=ast.Load())
    if name in WRAPPER_SEQUENCE_ARGUMENTS.get(key, ()):
        value = ast.Call(
            func=ast.Name(id="_generated_unwrap_async_wrapper_sequence", ctx=ast.Load()),
            args=[value],
            keywords=[],
        )
    elif name in UNWRAP_ARGUMENTS.get(key, ()):
        value = ast.Call(
            func=ast.Name(id="_generated_unwrap_async_arg", ctx=ast.Load()),
            args=[value],
            keywords=[],
        )
    return value


def _delegating_call(
    async_class: str,
    method: ast.AsyncFunctionDef,
) -> ast.Await:
    key = f"{async_class}.{method.name}"
    positional: list[ast.expr] = []
    all_positional = [*method.args.posonlyargs, *method.args.args]
    for index, argument in enumerate(all_positional):
        if index == 0 and argument.arg == "self":
            continue
        positional.append(_argument_value(key, argument.arg))
    if method.args.vararg is not None:
        positional.append(
            ast.Starred(
                value=ast.Name(id=method.args.vararg.arg, ctx=ast.Load()),
                ctx=ast.Load(),
            )
        )

    keywords = [
        ast.keyword(arg=argument.arg, value=_argument_value(key, argument.arg))
        for argument in method.args.kwonlyargs
    ]
    if method.args.kwarg is not None:
        keywords.append(
            ast.keyword(
                arg=None,
                value=ast.Name(id=method.args.kwarg.arg, ctx=ast.Load()),
            )
        )

    sync_method = ast.Attribute(
        value=ast.Attribute(
            value=ast.Name(id="self", ctx=ast.Load()),
            attr="_sync",
            ctx=ast.Load(),
        ),
        attr=method.name,
        ctx=ast.Load(),
    )
    runner = "_generated_run_sync_call"
    runner_args: list[ast.expr] = [sync_method, *positional]
    if method.name in SLICED_WAIT_METHODS.get(async_class, ()):
        runner = "_generated_run_sync_wait_sliced"
        runner_args = [
            ast.Attribute(
                value=ast.Name(id="self", ctx=ast.Load()),
                attr="_sync",
                ctx=ast.Load(),
            ),
            sync_method,
            *positional,
        ]
    return ast.Await(
        value=ast.Call(
            func=ast.Name(id=runner, ctx=ast.Load()),
            args=runner_args,
            keywords=keywords,
        )
    )


def _annotation_name(annotation: ast.AST) -> Optional[str]:
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    return None


def _parsed_forward_ref(annotation: ast.AST) -> ast.AST:
    if not isinstance(annotation, ast.Constant) or not isinstance(annotation.value, str):
        return annotation
    try:
        return ast.parse(annotation.value, mode="eval").body
    except SyntaxError:
        return annotation


def _wrapper_shape(annotation: Optional[ast.AST]) -> Optional[tuple[str, str]]:
    if annotation is None:
        return None
    annotation = _parsed_forward_ref(annotation)
    name = _annotation_name(annotation)
    if name in RETURN_WRAPPERS:
        return ("scalar", RETURN_WRAPPERS[name])
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _wrapper_shape(annotation.left) or _wrapper_shape(annotation.right)
    if not isinstance(annotation, ast.Subscript):
        return None

    container = _annotation_name(annotation.value)
    arguments = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
    if container in {"Optional", "Union"}:
        for argument in arguments:
            wrapped = _wrapper_shape(argument)
            if wrapped is not None:
                return wrapped
        return None
    if container in {"list", "List"} and arguments:
        wrapped = _wrapper_shape(arguments[0])
        if wrapped is not None and wrapped[0] == "scalar":
            return ("list", wrapped[1])
    if container in {"dict", "Dict"} and len(arguments) == 2:
        wrapped = _wrapper_shape(arguments[1])
        if wrapped is not None and wrapped[0] == "scalar":
            return ("dict", wrapped[1])
    return None


def _is_none_return(annotation: Optional[ast.AST]) -> bool:
    if annotation is None:
        return False
    annotation = _parsed_forward_ref(annotation)
    return (
        isinstance(annotation, ast.Constant)
        and annotation.value is None
        or isinstance(annotation, ast.Name)
        and annotation.id == "None"
    )


def _wrap_call(helper: str, value: ast.expr) -> ast.Call:
    return ast.Call(
        func=ast.Name(id="_generated_call_async_api", ctx=ast.Load()),
        args=[ast.Constant(helper), value],
        keywords=[],
    )


def _generated_body(
    async_class: str,
    method: ast.AsyncFunctionDef,
    sync_return: Optional[ast.AST],
) -> list[ast.stmt]:
    call = _delegating_call(async_class, method)
    if _is_none_return(sync_return):
        return [ast.Expr(value=call)]

    shape = _wrapper_shape(sync_return)
    if shape is None:
        return [ast.Return(value=call)]
    kind, helper = shape
    if kind == "scalar":
        return [ast.Return(value=_wrap_call(helper, call))]

    result_name = ast.Name(id="result", ctx=ast.Load())
    assign = ast.Assign(
        targets=[ast.Name(id="result", ctx=ast.Store())],
        value=call,
    )
    if kind == "list":
        item = ast.Name(id="item", ctx=ast.Load())
        wrapped: ast.expr = ast.ListComp(
            elt=_wrap_call(helper, item),
            generators=[
                ast.comprehension(
                    target=ast.Name(id="item", ctx=ast.Store()),
                    iter=result_name,
                    ifs=[],
                    is_async=0,
                )
            ],
        )
    elif kind == "dict":
        wrapped = ast.DictComp(
            key=ast.Name(id="key", ctx=ast.Load()),
            value=_wrap_call(helper, ast.Name(id="value", ctx=ast.Load())),
            generators=[
                ast.comprehension(
                    target=ast.Tuple(
                        elts=[
                            ast.Name(id="key", ctx=ast.Store()),
                            ast.Name(id="value", ctx=ast.Store()),
                        ],
                        ctx=ast.Store(),
                    ),
                    iter=ast.Call(
                        func=ast.Attribute(value=result_name, attr="items", ctx=ast.Load()),
                        args=[],
                        keywords=[],
                    ),
                    ifs=[],
                    is_async=0,
                )
            ],
        )
    else:  # pragma: no cover - shapes are closed above
        raise ValueError(f"unknown wrapper shape: {kind}")
    return [assign, ast.Return(value=wrapped)]


def _indent(source: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line if line else line for line in source.splitlines())


def _render_method(
    async_class: str,
    sync_method: ast.FunctionDef,
) -> str:
    method = _generated_signature(async_class, sync_method)
    method.body = _generated_body(async_class, method, sync_method.returns)
    ast.fix_missing_locations(method)
    return _indent(ast.unparse(method))


def generated_method_count() -> int:
    return sum(len(methods) for methods in GENERATED_METHODS.values())


def hand_method_count() -> int:
    return sum(len(methods) for methods in HAND_METHODS.values())


def generate_async_api(sync_source: str) -> str:
    tree = ast.parse(sync_source)
    methods_by_class = _class_methods(tree)
    sections: list[str] = []
    metadata: list[tuple[str, str, str]] = []

    for async_class, method_names in GENERATED_METHODS.items():
        sync_class = ASYNC_TO_SYNC_CLASS[async_class]
        sync_methods = methods_by_class.get(sync_class)
        if sync_methods is None:
            raise ValueError(f"sync class {sync_class!r} was not found")
        mixin = f"_{async_class}GeneratedMixin"
        rendered = [f"class {mixin}:"]
        for method_name in method_names:
            sync_method = sync_methods.get(method_name)
            if sync_method is None:
                raise ValueError(f"sync method {sync_class}.{method_name} was not found")
            rendered.append(_render_method(async_class, sync_method))
            rendered.append("")
            metadata.append((mixin, async_class, method_name))
        sections.append("\n".join(rendered).rstrip())

    fingerprint = hashlib.sha256(sync_source.encode("utf-8")).hexdigest()
    header = f'''# This file is generated by tools/generate_async_api.py. Do not edit.
# sync_api.py sha256: {fingerprint}
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Literal, Optional, Pattern, Union

from .sync_api import DebuggerLocation


async def _generated_run_sync_call(func: Any, /, *args: Any, **kwargs: Any) -> Any:
    from .async_api import _run_sync_call

    return await _run_sync_call(func, *args, **kwargs)


async def _generated_run_sync_wait_sliced(sync_owner: Any, func: Any, /, *args: Any, **kwargs: Any) -> Any:
    from .async_api import _run_sync_wait_sliced

    return await _run_sync_wait_sliced(sync_owner, func, *args, **kwargs)


def _generated_call_async_api(name: str, value: Any) -> Any:
    from . import async_api

    return getattr(async_api, name)(value)


def _generated_unwrap_async_arg(value: Any) -> Any:
    from .async_api import _unwrap_async_arg

    return _unwrap_async_arg(value)


def _generated_unwrap_async_wrapper_sequence(value: Any) -> Any:
    from .async_api import _AsyncWrapper

    if isinstance(value, _AsyncWrapper):
        return value._sync
    if isinstance(value, (list, tuple)):
        return [item._sync if isinstance(item, _AsyncWrapper) else item for item in value]
    return value
'''
    metadata_lines = [
        "",
        "# Preserve the introspection metadata of methods formerly declared",
        "# directly on the public async classes.",
        f'_ASYNC_API_MODULE = f"{{__package__}}.async_api"',
    ]
    for mixin, async_class, method_name in metadata:
        metadata_lines.extend(
            [
                f"{mixin}.{method_name}.__module__ = _ASYNC_API_MODULE",
                f'{mixin}.{method_name}.__qualname__ = "{async_class}.{method_name}"',
            ]
        )
    metadata_lines.extend(["", "__all__: tuple[str, ...] = ()", ""])
    return header + "\n\n" + "\n\n\n".join(sections) + "\n" + "\n".join(metadata_lines)


def _write_or_check(rendered: str, output: Path, *, check: bool) -> int:
    if check:
        if not output.exists() or output.read_text(encoding="utf-8") != rendered:
            print(f"{output} is stale; run tools/generate_async_api.py")
            return 1
        print(f"{output} is fresh")
        return 0
    output.write_text(rendered, encoding="utf-8")
    print(f"wrote {output} ({generated_method_count()} methods)")
    return 0


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sync-api", type=Path, default=DEFAULT_SYNC_API)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args(argv)

    rendered = generate_async_api(args.sync_api.read_text(encoding="utf-8"))
    if args.stdout:
        print(rendered, end="")
        return 0
    return _write_or_check(rendered, args.output, check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
