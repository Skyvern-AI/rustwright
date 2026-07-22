"""PR-3 default tools, observability cursors, and pending-modal semantics."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from types import SimpleNamespace
import threading
import time

import pytest

from rustwright_mcp import server
from rustwright_mcp.filepolicy import FilePolicy
from rustwright_mcp.session import SessionState
from test_smoke import _call, _result_section, _run_session


FIXTURES = Path(__file__).parent / "fixtures"


class FixtureHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FIXTURES), **kwargs)

    def do_GET(self) -> None:
        if self.path == "/":
            self.path = "/pr3.html"
        if self.path == "/api/get":
            payload = b'{"kind":"get-response"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == "/download":
            payload = b"download-body"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header(
                "Content-Disposition", 'attachment; filename="fixture-download.txt"'
            )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.path != "/api/post":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        payload = b"post-response:" + body
        self.send_response(201)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        del format, args


@contextmanager
def fixture_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        host, port = httpd.server_address
        yield f"http://{host}:{port}/"
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=2)


def _error_text(result) -> str:
    return "\n".join(item.text for item in result.content if item.type == "text")


def test_new_schemas_are_strict_canonical_and_nested_strict() -> None:
    schemas = {
        tool.name: tool.inputSchema for tool in asyncio.run(server.mcp.list_tools())
    }
    expected = {
        "browser_resize": {"width", "height"},
        "browser_drag": {"startElement", "startTarget", "endElement", "endTarget"},
        "browser_fill_form": {"fields"},
        "browser_find": {"text", "regex"},
        "browser_console_messages": {"level", "all", "filename"},
        "browser_network_requests": {"static", "filter", "filename"},
        "browser_network_request": {"index", "part", "filename"},
        "browser_file_upload": {"paths"},
    }
    for name, properties in expected.items():
        assert schemas[name]["additionalProperties"] is False
        assert set(schemas[name]["properties"]) == properties
    fill_definition = schemas["browser_fill_form"]["$defs"]["FillField"]
    assert fill_definition["additionalProperties"] is False
    assert set(fill_definition["required"]) == {"target", "name", "type", "value"}
    assert fill_definition["properties"]["type"]["enum"] == [
        "textbox",
        "checkbox",
        "radio",
        "combobox",
        "slider",
    ]


def test_resize_rounds_fractional_dimensions_half_away_from_zero(
    monkeypatch,
) -> None:
    class Page:
        url = "https://example.test/"

        def __init__(self) -> None:
            self.context = SimpleNamespace(pages=[self])
            self.viewport = None

        def title(self) -> str:
            return "Resize fixture"

        def set_viewport_size(self, viewport) -> None:
            self.viewport = viewport

    page = Page()
    state = SessionState(page=page)
    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_page", lambda: page)
    monkeypatch.setattr(server, "_snapshot", lambda current: "- snapshot")

    response = server.browser_resize(480.5, 639.5)

    assert page.viewport == {"width": 481, "height": 640}
    assert "Viewport resized to 481x640 CSS pixels" in response
    assert server._round_half_away_from_zero(-2.5) == -3


def test_background_dialog_is_visible_tabs_are_fast_and_handle_is_cross_tab(
    monkeypatch,
) -> None:
    class Page:
        def __init__(self, title: str) -> None:
            self.handlers = {}
            self.url = f"https://example.test/{title.lower()}"
            self._title = title
            self.title_calls = 0
            self.dialog_pending = False

        def on(self, event, callback) -> None:
            self.handlers.setdefault(event, []).append(callback)

        def title(self) -> str:
            self.title_calls += 1
            assert not self.dialog_pending, "title() called on dialog-blocked tab"
            return self._title

    class Dialog:
        type = "alert"
        message = "second-tab alert"

        def __init__(self) -> None:
            self.accepted = False

        def accept(self, prompt_text=None) -> None:
            del prompt_text
            self.accepted = True

    first = Page("First")
    second = Page("Second")
    context = SimpleNamespace(pages=[first, second])
    first.context = context
    second.context = context
    state = SessionState(page=first)
    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_page", lambda: first)
    monkeypatch.setattr(server, "_snapshot", lambda current, **kwargs: "- fresh")
    server._register_page_handlers(first)
    second_registry = state.register_page_handlers(second)
    state.remember_page_title(second, "Second")
    dialog = Dialog()
    second_registry.pending_dialog = dialog
    second.dialog_pending = True

    acting_on_first = server.browser_snapshot()
    assert "### Modal" in acting_on_first
    assert "Tab 1: Dialog pending: type=alert" in acting_on_first
    assert "second-tab alert" in acting_on_first
    assert "### Snapshot" not in acting_on_first

    started = time.monotonic()
    tabs = server.browser_tabs("list")
    assert time.monotonic() - started < 0.5
    assert "### Tabs" in tabs and "1: Second" in tabs
    assert second.title_calls == 0

    handled = server.browser_handle_dialog(True)
    assert "Accepted the pending dialog" in handled
    assert dialog.accepted
    assert second_registry.pending_dialog is None
    assert state.page is first


def test_download_name_is_sanitized_and_confined(monkeypatch, tmp_path) -> None:
    policy = FilePolicy(output_root=tmp_path / "output")
    monkeypatch.setattr(server, "get_file_policy", lambda: policy)

    class Download:
        def save_as(self, path: str) -> None:
            Path(path).write_bytes(b"safe")

    artifact = server._save_download(Download(), "../../unsafe name.txt")
    assert artifact == "unsafe_name.txt"
    assert (policy.output_root / artifact).read_bytes() == b"safe"
    assert not (tmp_path / "unsafe name.txt").exists()


def test_find_regex_flags_errors_paths_and_context() -> None:
    assert server._compile_find_regex("Needle").search("Needle")
    assert not server._compile_find_regex("Needle").search("needle")
    assert server._compile_find_regex("/Needle/i").search("needle")
    assert server._compile_find_regex("/^second/m").search("first\nsecond")
    assert server._compile_find_regex("/first.second/s").search("first\nsecond")
    with pytest.raises(ValueError, match="supported: i, m, s"):
        server._compile_find_regex("/x/g")
    with pytest.raises(ValueError, match="invalid regex"):
        server._compile_find_regex("[")

    outline = "\n".join(
        [
            '- main "Fixture"',
            '  - region "Find region"',
            '    - button "Before" [ref=e1]',
            '    - button "Unique Needle" [ref=e2]',
            '    - button "After" [ref=e3]',
        ]
    )
    matches, truncated = server._find_outline_matches(
        outline, lambda line: "needle" in line.casefold()
    )
    assert truncated == 0
    assert "Path: - main" in matches[0]
    assert "- region" in matches[0]
    assert "[ref=e1]" in matches[0]
    assert ">     - button \"Unique Needle\" [ref=e2]" in matches[0]
    assert "[ref=e3]" in matches[0]


def test_network_static_filter_and_original_index_stability() -> None:
    records = [
        SimpleNamespace(
            index=4,
            url="https://example.test/image.png",
            resource_type="image",
            response_status=200,
        ),
        SimpleNamespace(
            index=7,
            url="https://example.test/api",
            resource_type="fetch",
            response_status=200,
        ),
        SimpleNamespace(
            index=9,
            url="https://example.test/broken.css",
            resource_type="stylesheet",
            response_status=500,
        ),
    ]
    visible = server._filter_network_records(
        records,
        include_static=False,
        pattern=re.compile("api|css"),
    )
    assert [record.index for record in visible] == [7, 9]
    assert server._STATIC_RESOURCE_TYPES == {
        "image",
        "media",
        "font",
        "stylesheet",
    }


def test_response_console_cursor_is_terse_bounded_and_once(monkeypatch) -> None:
    class Page:
        def __init__(self) -> None:
            self.handlers = {}
            self.url = "https://example.test/"
            self.context = SimpleNamespace(pages=[self])

        def on(self, event, callback) -> None:
            self.handlers.setdefault(event, []).append(callback)

        def title(self) -> str:
            return "Cursor fixture"

        def emit(self, event, value) -> None:
            for callback in self.handlers.get(event, []):
                callback(value)

    page = Page()
    state = SessionState(console_quota=20, page=page)
    state.register_page_handlers(page)
    monkeypatch.setattr(server, "_state", state)
    for index in range(7):
        page.emit(
            "console",
            SimpleNamespace(
                type="warning",
                text=f"warning-{index}",
                location={"url": "https://example.test/app.js", "lineNumber": index},
            ),
        )
    page.emit(
        "console",
        SimpleNamespace(type="info", text="quiet", location={}),
    )

    first = server._render_response("done", page=page)
    assert first.count("WARNING ") == 5
    assert "(and 2 more)" in first
    assert "quiet" not in first
    second = server._render_response("again", page=page)
    assert "### Console" not in second


def test_console_ring_eviction_and_response_body_states(monkeypatch) -> None:
    class Page:
        def __init__(self) -> None:
            self.handlers = {}
            self.url = "https://example.test/"
            self.context = SimpleNamespace(pages=[self])

        def on(self, event, callback) -> None:
            self.handlers.setdefault(event, []).append(callback)

        def title(self) -> str:
            return "Ring fixture"

        def emit(self, event, value) -> None:
            for callback in self.handlers.get(event, []):
                callback(value)

    page = Page()
    state = SessionState(console_quota=2, page=page)
    state.register_page_handlers(page)
    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_page", lambda: page)
    for index in range(3):
        page.emit(
            "console",
            SimpleNamespace(type="error", text=f"error-{index}", location={}),
        )
    listed = server.browser_console_messages()
    assert "error-0" not in listed
    assert "error-1" in listed and "error-2" in listed
    assert "ring buffer evicted 1" in listed

    unavailable = SimpleNamespace(response=None, response_headers=())
    assert server._response_body_text(unavailable, byte_limit=10) == "(response not received)"
    empty = SimpleNamespace(
        response=SimpleNamespace(body=lambda: b""),
        response_headers=(("content-type", "text/plain"),),
    )
    assert server._response_body_text(empty, byte_limit=10) == "(empty response body)"
    binary = SimpleNamespace(
        response=SimpleNamespace(body=lambda: b"\x00\x01\x02"),
        response_headers=(("content-type", "application/octet-stream"),),
    )
    rendered = server._response_body_text(binary, byte_limit=2)
    assert "AAE=" in rendered
    assert "base64-encoded non-text" in rendered
    assert "truncated to 2 bytes" in rendered


def test_real_resize_drag_fill_and_find(tmp_path) -> None:
    with fixture_server() as url:
        async def checks(session) -> None:
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert {
                "browser_resize",
                "browser_drag",
                "browser_fill_form",
                "browser_find",
            } <= tools
            await _call(session, "browser_navigate", url=url)

            resized = await _call(session, "browser_resize", width=480, height=640)
            assert "Viewport resized to 480x640" in resized
            assert "### Snapshot" in resized

            filled = await _call(
                session,
                "browser_fill_form",
                fields=[
                    {"target": "#text-field", "name": "text", "type": "textbox", "value": "filled"},
                    {"target": "#check-field", "name": "check", "type": "checkbox", "value": "true"},
                    {"target": "#radio-field", "name": "radio", "type": "radio", "value": "true"},
                    {"target": "#combo-field", "name": "combo", "type": "combobox", "value": "Large label"},
                    {"target": "#slider-field", "name": "slider", "type": "slider", "value": "55"},
                ],
            )
            assert "Filled 5 fields sequentially" in filled
            assert filled.count("### Snapshot") == 1
            values = await _call(
                session,
                "browser_evaluate",
                function="() => ({text: document.querySelector('#text-field').value, check: document.querySelector('#check-field').checked, radio: document.querySelector('#radio-field').checked, combo: document.querySelector('#combo-field').value, slider: document.querySelector('#slider-field').value})",
            )
            assert json.loads(_result_section(values)) == {
                "text": "filled",
                "check": True,
                "radio": True,
                "combo": "large-value",
                "slider": "55",
            }

            partial = await session.call_tool(
                "browser_fill_form",
                {
                    "fields": [
                        {"target": "#text-field", "name": "earlier", "type": "textbox", "value": "kept"},
                        {"target": "#radio-field", "name": "bad-radio", "type": "radio", "value": "false"},
                    ]
                },
            )
            assert partial.isError
            assert "bad-radio" in _error_text(partial)
            assert "unchecking a radio is not supported" in _error_text(partial)
            kept = await _call(
                session,
                "browser_evaluate",
                function="() => document.querySelector('#text-field').value",
            )
            assert json.loads(_result_section(kept)) == "kept"

            dragged = await _call(
                session,
                "browser_drag",
                startTarget="#drag-source",
                startElement="source",
                endTarget="#drag-target",
                endElement="target",
            )
            assert "Dragged source to target" in dragged
            assert "dragged" in await _call(
                session, "browser_get_text", selector="#drag-result"
            )

            found = await _call(session, "browser_find", text="unique needle")
            assert "Path:" in found and "[ref=" in found
            ref = re.search(r'Unique Needle[^\n]*\[ref=(e\d+)\]', found).group(1)
            assert "Clicked" in await _call(session, "browser_click", target=ref)
            regex_found = await _call(session, "browser_find", regex="/unique needle/i")
            assert "Unique Needle" in regex_found
            invalid = await session.call_tool("browser_find", {"regex": "/x/g"})
            assert invalid.isError and "invalid regex flags" in _error_text(invalid)
            both = await session.call_tool(
                "browser_find", {"text": "x", "regex": "x"}
            )
            assert both.isError and "Exactly one" in _error_text(both)
            await _call(session, "browser_close")

        asyncio.run(
            _run_session(
                checks,
                {"RUSTWRIGHT_MCP_OUTPUT_DIR": str(tmp_path / "output")},
            )
        )


def test_real_console_and_network_observability(tmp_path) -> None:
    output = tmp_path / "output"
    with fixture_server() as url:
        async def checks(session) -> None:
            navigated = await _call(session, "browser_navigate", url=url)
            assert "### Console" in navigated
            assert "ERROR" in navigated and "WARNING" in navigated
            assert "console-info" not in navigated
            await _call(session, "browser_wait_for", time=0.2)

            error_only = await _call(
                session, "browser_console_messages", level="error"
            )
            assert "console-error" in error_only
            assert "console-warning" not in error_only
            debug = await _call(
                session,
                "browser_console_messages",
                level="debug",
                all=True,
                filename="console.txt",
            )
            assert "Console messages written to `console.txt`" in debug
            console_file = (output / "console.txt").read_text()
            assert all(
                value in console_file
                for value in (
                    "console-error",
                    "console-warning",
                    "console-info",
                    "console-debug",
                )
            )

            default_list = await _call(session, "browser_network_requests")
            assert "/api/get" in default_list and "/api/post" in default_list
            assert "static.css" not in default_list
            all_list = await _call(
                session, "browser_network_requests", static=True
            )
            assert "static.css" in all_list
            filtered = await _call(
                session,
                "browser_network_requests",
                static=True,
                filter="/api/(get|post)$",
                filename="network.txt",
            )
            assert "Network requests written to `network.txt`" in filtered
            network_file = (output / "network.txt").read_text()
            assert "/api/get" in network_file and "/api/post" in network_file
            indices = {
                endpoint: int(
                    re.search(rf"\[(\d+)\].*{re.escape(endpoint)}", all_list).group(1)
                )
                for endpoint in ("/api/get", "/api/post")
            }

            post = await _call(
                session,
                "browser_network_request",
                index=indices["/api/post"],
                part="request-body",
            )
            assert "posted-body" in post
            get = await _call(
                session,
                "browser_network_request",
                index=indices["/api/get"],
                part="response-body",
            )
            assert "get-response" in get
            full = await _call(
                session,
                "browser_network_request",
                index=indices["/api/get"],
                filename="request.txt",
            )
            assert "Network request" in full
            assert "get-response" in (output / "request.txt").read_text()
            invalid = await session.call_tool(
                "browser_network_requests", {"filter": "["}
            )
            assert invalid.isError and "invalid network filter regex" in _error_text(invalid)
            missing = await session.call_tool(
                "browser_network_request", {"index": 9999}
            )
            assert missing.isError and "valid range" in _error_text(missing)
            await _call(session, "browser_close")

        asyncio.run(
            _run_session(
                checks,
                {"RUSTWRIGHT_MCP_OUTPUT_DIR": str(output)},
            )
        )


def test_real_file_upload_security_cancel_and_download_cursor(tmp_path) -> None:
    output = tmp_path / "output"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    upload = workspace / "upload.txt"
    upload.write_text("upload-body")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    symlink = workspace / "link.txt"
    symlink.symlink_to(upload)

    with fixture_server() as url:
        async def checks(session) -> None:
            await _call(session, "browser_navigate", url=url)
            pending = await _call(session, "browser_click", target="#upload")
            assert "### Modal" in pending and "File chooser pending" in pending
            assert "### Snapshot" not in pending
            uploaded = await _call(
                session, "browser_file_upload", paths=[str(upload)]
            )
            assert "Uploaded 1 file" in uploaded and "### Snapshot" in uploaded
            assert "upload.txt" in await _call(
                session, "browser_get_text", selector="#upload-result"
            )
            no_pending = await session.call_tool("browser_file_upload", {})
            assert no_pending.isError and "no file chooser is pending" in _error_text(no_pending)

            for rejected, message in (
                ("relative.txt", "absolute"),
                (str(outside), "outside the allowed workspace"),
                (str(symlink), "must not be a symlink"),
            ):
                await _call(session, "browser_click", target="#upload")
                result = await session.call_tool(
                    "browser_file_upload", {"paths": [rejected]}
                )
                assert result.isError and message in _error_text(result)

            await _call(session, "browser_click", target="#upload")
            cancelled = await _call(session, "browser_file_upload")
            assert "Cancelled" in cancelled and "### Snapshot" in cancelled

            downloaded = await _call(session, "browser_click", target="#download")
            assert "### Downloads" in downloaded
            assert "fixture-download.txt" in downloaded
            assert (output / "fixture-download.txt").read_bytes() == b"download-body"
            next_response = await _call(session, "browser_snapshot")
            assert "### Downloads" not in next_response
            await _call(session, "browser_close")

        asyncio.run(
            _run_session(
                checks,
                {
                    "RUSTWRIGHT_MCP_OUTPUT_DIR": str(output),
                    "RUSTWRIGHT_MCP_WORKSPACE": str(workspace),
                },
            )
        )


def test_real_pending_dialog_flip_and_no_hang(tmp_path) -> None:
    with fixture_server() as url:
        async def checks(session) -> None:
            await _call(session, "browser_navigate", url=url)

            started = time.monotonic()
            alert = await _call(session, "browser_click", target="#alert")
            elapsed = time.monotonic() - started
            assert elapsed < 3
            assert "### Modal" in alert and "type=alert" in alert
            assert "alert message" in alert
            assert "### Snapshot" not in alert
            snapshot_started = time.monotonic()
            blocked_snapshot = await _call(session, "browser_snapshot")
            assert time.monotonic() - snapshot_started < 1
            assert "### Modal" in blocked_snapshot
            assert "### Snapshot" not in blocked_snapshot
            accepted = await _call(
                session, "browser_handle_dialog", accept=True
            )
            assert "Accepted the pending dialog" in accepted
            missing = await session.call_tool(
                "browser_handle_dialog", {"accept": True}
            )
            assert missing.isError and "no dialog is pending" in _error_text(missing)

            await _call(session, "browser_click", target="#confirm")
            await _call(session, "browser_handle_dialog", accept=False)
            confirm_false = await _call(
                session,
                "browser_evaluate",
                function="() => window.confirmResult",
            )
            assert json.loads(_result_section(confirm_false)) is False

            await _call(session, "browser_click", target="#confirm")
            await _call(session, "browser_handle_dialog", accept=True)
            confirm_true = await _call(
                session,
                "browser_evaluate",
                function="() => window.confirmResult",
            )
            assert json.loads(_result_section(confirm_true)) is True

            prompt = await _call(session, "browser_click", target="#prompt")
            assert "prompt message" in prompt
            await _call(
                session,
                "browser_handle_dialog",
                accept=True,
                promptText="typed prompt",
            )
            prompt_value = await _call(
                session,
                "browser_evaluate",
                function="() => window.promptResult",
            )
            assert json.loads(_result_section(prompt_value)) == "typed prompt"
            await _call(session, "browser_close")

        asyncio.run(
            _run_session(
                checks,
                {"RUSTWRIGHT_MCP_OUTPUT_DIR": str(tmp_path / "output")},
            )
        )
