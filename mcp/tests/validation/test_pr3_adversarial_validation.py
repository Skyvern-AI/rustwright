"""Independent adversarial validation for the PR-3 MCP compatibility slice."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import re
from types import SimpleNamespace
import sys

import pytest

from rustwright_mcp import server
from rustwright_mcp.filepolicy import FilePolicy
from rustwright_mcp.session import (
    ConsoleRecord,
    DownloadRecord,
    NetworkRecord,
    SessionState,
)

sys.path.insert(0, str(Path(__file__).parents[1]))
from contract.contract_schema import ToolContract, compare_tool_schema
from test_pr3_compatibility import fixture_server
from test_smoke import _call, _run_session


AUDITED_PR3_CONTRACT = {
    "browser_resize": [
        {"name": "width", "type": "number", "required": True},
        {"name": "height", "type": "number", "required": True},
    ],
    "browser_drag": [
        {"name": "startElement", "type": "string", "required": False},
        {"name": "startTarget", "type": "string", "required": True},
        {"name": "endElement", "type": "string", "required": False},
        {"name": "endTarget", "type": "string", "required": True},
    ],
    "browser_fill_form": [
        {
            "name": "fields",
            "type": "array",
            "required": True,
            "items": {
                "type": "object",
                "params": [
                    {"name": "element", "type": "string", "required": False},
                    {"name": "target", "type": "string", "required": True},
                    {"name": "name", "type": "string", "required": True},
                    {
                        "name": "type",
                        "type": "string",
                        "required": True,
                        "enum": [
                            "textbox",
                            "checkbox",
                            "radio",
                            "combobox",
                            "slider",
                        ],
                    },
                    {"name": "value", "type": "string", "required": True},
                ],
            },
        },
    ],
    "browser_find": [
        {"name": "text", "type": "string", "required": False},
        {"name": "regex", "type": "string", "required": False},
    ],
    "browser_console_messages": [
        {
            "name": "level",
            "type": "string",
            "required": False,
            "enum": ["error", "warning", "info", "debug"],
            "default": "info",
        },
        {"name": "all", "type": "boolean", "required": False, "default": False},
        {"name": "filename", "type": "string", "required": False},
    ],
    "browser_network_requests": [
        {"name": "static", "type": "boolean", "required": False, "default": False},
        {"name": "filter", "type": "string", "required": False},
        {"name": "filename", "type": "string", "required": False},
    ],
    "browser_network_request": [
        {"name": "index", "type": "integer", "required": True},
        {
            "name": "part",
            "type": "string",
            "required": False,
            "enum": [
                "request-headers",
                "request-body",
                "response-headers",
                "response-body",
            ],
        },
        {"name": "filename", "type": "string", "required": False},
    ],
    "browser_file_upload": [
        {
            "name": "paths",
            "type": "array",
            "required": False,
            "items": {"type": "string"},
        },
    ],
}


class FakePage:
    def __init__(self, title: str = "Adversarial fixture") -> None:
        self.handlers: dict[str, list] = {}
        self.url = "https://example.test/"
        self._title = title
        self.context = SimpleNamespace(pages=[self])

    def on(self, event, callback) -> None:
        self.handlers.setdefault(event, []).append(callback)

    def title(self) -> str:
        return self._title

    def emit(self, event, value) -> None:
        for callback in self.handlers.get(event, ()):
            callback(value)


def _error_text(result) -> str:
    return "\n".join(item.text for item in result.content if item.type == "text")


def test_fixture_and_advertised_schemas_match_all_eight_audited_contracts() -> None:
    fixture_path = (
        Path(__file__).parents[1] / "contract" / "fixtures" / "default_toolset.json"
    )
    fixture = json.loads(fixture_path.read_text())
    schemas = {
        tool.name: tool.inputSchema for tool in asyncio.run(server.mcp.list_tools())
    }
    errors: list[str] = []

    for name, params in AUDITED_PR3_CONTRACT.items():
        expected = {"params": params}
        if fixture.get(name) != expected:
            errors.append(
                f"{name} fixture differs: expected {expected!r}, got {fixture.get(name)!r}"
            )
        contract = ToolContract.from_mapping(name, expected)
        for mismatch in compare_tool_schema(schemas[name], contract):
            errors.append(f"{name} advertised schema: {mismatch}")

    assert errors == []


def test_network_static_set_body_modes_and_full_filename(monkeypatch, tmp_path) -> None:
    records = [
        SimpleNamespace(
            index=index,
            url=f"https://example.test/{resource_type}",
            resource_type=resource_type,
            response_status=status,
        )
        for index, (resource_type, status) in enumerate(
            [
                ("image", 200),
                ("media", 204),
                ("font", 304),
                ("stylesheet", 200),
                ("script", 200),
                ("image", 404),
            ],
            start=1,
        )
    ]
    visible = server._filter_network_records(
        records, include_static=False, pattern=None
    )
    assert [(record.resource_type, record.response_status) for record in visible] == [
        ("script", 200),
        ("image", 404),
    ]

    page = FakePage()
    state = SessionState(page=page)
    registry = state.register_page_handlers(page)
    policy = FilePolicy(output_root=tmp_path / "output")
    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_page", lambda: page)
    monkeypatch.setattr(server, "get_file_policy", lambda: policy)

    large_body = b"x" * (server.NETWORK_BODY_INLINE_BYTES + 4096)
    large_record = NetworkRecord(
        epoch=0,
        index=7,
        method="GET",
        url="https://example.test/large",
        resource_type="fetch",
        headers=(),
        post_data=None,
        request=object(),
        response_status=200,
        response_headers=(("content-type", "text/plain"),),
        response=SimpleNamespace(body=lambda: large_body),
    )
    registry.network_records.append(large_record)

    inline = server.browser_network_request(7, part="response-body")
    assert f"truncated to {server.NETWORK_BODY_INLINE_BYTES} bytes inline" in inline
    assert inline.count("x") >= server.NETWORK_BODY_INLINE_BYTES
    written = server.browser_network_request(
        7, part="response-body", filename="large-response.txt"
    )
    assert "large-response.txt" in written
    saved = (policy.output_root / "large-response.txt").read_bytes()
    assert large_body in saved
    assert b"truncated" not in saved

    binary = SimpleNamespace(
        response=SimpleNamespace(body=lambda: b"\x00\x01\xfe\xff"),
        response_headers=(("content-type", "application/octet-stream"),),
    )
    binary_text = server._response_body_text(binary, byte_limit=64)
    assert base64.b64encode(b"\x00\x01\xfe\xff").decode() in binary_text
    assert "base64-encoded non-text" in binary_text
    aborted = SimpleNamespace(response=None, response_headers=())
    assert server._response_body_text(aborted, byte_limit=64) == "(response not received)"


def test_old_network_index_is_invalid_after_navigation_epoch_reset(tmp_path) -> None:
    with fixture_server() as url:
        async def checks(session) -> None:
            try:
                await _call(session, "browser_navigate", url=url)
                first = await _call(session, "browser_network_requests", static=True)
                old_index = int(
                    re.search(r"\[(\d+)\] .* \(document\)", first).group(1)
                )

                await _call(session, "browser_navigate", url=url + "?epoch=2")
                current = await _call(
                    session, "browser_network_requests", static=True
                )
                current_document_index = int(
                    re.search(r"\[(\d+)\] .* \(document\)", current).group(1)
                )
                assert current_document_index != old_index, (
                    "request indices were reused across navigation epochs, so an old "
                    "integer can silently select a different request"
                )
                old_detail = await session.call_tool(
                    "browser_network_request", {"index": old_index}
                )
                assert old_detail.isError
                assert "current navigation epoch" in _error_text(old_detail)
            finally:
                await session.call_tool("browser_close", {})

        asyncio.run(
            _run_session(
                checks,
                {"RUSTWRIGHT_MCP_OUTPUT_DIR": str(tmp_path / "output")},
            )
        )


def test_background_tab_dialog_is_reported_while_acting_on_first_tab(
    monkeypatch,
) -> None:
    first = FakePage("First")
    second = FakePage("Second")
    context = SimpleNamespace(pages=[first, second])
    first.context = context
    second.context = context
    state = SessionState(page=first)
    state.register_page_handlers(first)
    second_registry = state.register_page_handlers(second)
    second_registry.pending_dialog = SimpleNamespace(
        type="alert", message="second-tab alert"
    )
    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_page", lambda: first)
    monkeypatch.setattr(server, "_snapshot", lambda page, **kwargs: "- fresh")

    response = server.browser_snapshot()
    assert "### Modal" in response
    assert "second-tab alert" in response
    assert "### Snapshot" not in response


def test_download_cap_failure_collision_and_response_cursor(monkeypatch, tmp_path) -> None:
    page = FakePage()
    state = SessionState(page=page)
    state.register_page_handlers(page)
    capped_policy = FilePolicy(
        output_root=tmp_path / "capped",
        max_file_bytes=4,
        max_total_bytes=100,
    )
    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "get_file_policy", lambda: capped_policy)
    state.download_saver = server._save_download

    class Download:
        url = "https://example.test/download"

        def __init__(self, payload: bytes, suggested_filename: str) -> None:
            self.payload = payload
            self.suggested_filename = suggested_filename

        def save_as(self, path: str) -> None:
            Path(path).write_bytes(self.payload)

    page.emit("download", Download(b"oversize", "oversize.txt"))
    capped = server._render_response("clicked", page=page)
    assert capped.count("### Downloads") == 1
    assert "oversize.txt" in capped
    assert "save failed" in capped and "per-file cap" in capped
    assert list(capped_policy.output_root.iterdir()) == []
    assert "### Downloads" not in server._render_response("next", page=page)

    collision_page = FakePage()
    collision_state = SessionState(page=collision_page)
    collision_state.register_page_handlers(collision_page)
    collision_policy = FilePolicy(output_root=tmp_path / "collisions")
    monkeypatch.setattr(server, "_state", collision_state)
    monkeypatch.setattr(server, "get_file_policy", lambda: collision_policy)
    collision_state.download_saver = server._save_download
    collision_page.emit("download", Download(b"first", "../unsafe name.txt"))
    collision_page.emit("download", Download(b"second", "../unsafe name.txt"))
    collisions = server._render_response("clicked", page=collision_page)
    artifacts = re.findall(r"`([^`]+)`", collisions.split("### Downloads", 1)[1])
    assert len(artifacts) == 2
    assert len(set(artifacts)) == 2
    assert all("/" not in artifact and " " not in artifact for artifact in artifacts)
    assert {path.read_bytes() for path in collision_policy.output_root.iterdir()} == {
        b"first",
        b"second",
    }
    assert "### Downloads" not in server._render_response(
        "unrelated", page=collision_page
    )


def test_download_cursor_does_not_skip_an_earlier_unfinished_download() -> None:
    page = FakePage()
    state = SessionState(page=page)
    registry = state.register_page_handlers(page)
    first = DownloadRecord(
        sequence=1,
        epoch=0,
        url="https://example.test/first",
        suggested_filename="first.txt",
        download=object(),
    )
    second = DownloadRecord(
        sequence=2,
        epoch=0,
        url="https://example.test/second",
        suggested_filename="second.txt",
        download=object(),
        artifact="second.txt",
        finished=True,
    )
    registry.downloads.extend([first, second])
    state.next_download_sequence = 3

    assert [record.sequence for record in state.response_events()[2]] == [2]
    registry.downloads[0] = DownloadRecord(
        sequence=1,
        epoch=0,
        url=first.url,
        suggested_filename=first.suggested_filename,
        download=first.download,
        artifact="first.txt",
        finished=True,
    )
    assert [record.sequence for record in state.response_events()[2]] == [1]


def test_find_cap_fill_rejections_and_empty_upload_cancel(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first_upload = workspace / "first.txt"
    second_upload = workspace / "second.txt"
    first_upload.write_text("first")
    second_upload.write_text("second")

    with fixture_server() as url:
        async def checks(session) -> None:
            try:
                await _call(session, "browser_navigate", url=url)
                missing = await session.call_tool("browser_find", {})
                assert missing.isError and "Exactly one" in _error_text(missing)
                checkbox = await session.call_tool(
                    "browser_fill_form",
                    {
                        "fields": [
                            {
                                "target": "#check-field",
                                "name": "uncertain checkbox",
                                "type": "checkbox",
                                "value": "maybe",
                            }
                        ]
                    },
                )
                assert checkbox.isError
                assert "uncertain checkbox" in _error_text(checkbox)
                assert "'true' or 'false'" in _error_text(checkbox)

                await _call(
                    session,
                    "browser_evaluate",
                    function="() => { const root = document.querySelector('#find-root'); for (let i = 0; i < 25; i++) { const b = document.createElement('button'); b.textContent = 'cap match ' + i; root.appendChild(b); } return true; }",
                )
                found = await _call(session, "browser_find", text="cap match")
                assert found.count("Match ") == server.FIND_MATCH_LIMIT
                overflow = re.search(
                    r"(\d+) additional matches truncated", found
                )
                assert overflow is not None and int(overflow.group(1)) >= 5

                await _call(session, "browser_click", target="#upload")
                cancelled = await _call(session, "browser_file_upload", paths=[])
                assert "Cancelled" in cancelled
                await _call(session, "browser_click", target="#upload")
                multiple = await session.call_tool(
                    "browser_file_upload",
                    {"paths": [str(first_upload), str(second_upload)]},
                )
                assert multiple.isError
                assert "only one file" in _error_text(multiple)
            finally:
                await session.call_tool("browser_close", {})

        asyncio.run(
            _run_session(
                checks,
                {
                    "RUSTWRIGHT_MCP_OUTPUT_DIR": str(tmp_path / "output"),
                    "RUSTWRIGHT_MCP_WORKSPACE": str(workspace),
                },
            )
        )


def test_e2_section_order_applicability_and_new_console_semantics(monkeypatch) -> None:
    page = FakePage()
    state = SessionState(page=page)
    registry = state.register_page_handlers(page)
    monkeypatch.setattr(server, "_state", state)

    for index in range(7):
        registry.console_records.append(
            ConsoleRecord(
                sequence=index + 1,
                epoch=0,
                message_type="warning",
                text=f"warning-{index}",
                location=(),
            )
        )
    state.next_console_sequence = 8
    registry.pending_dialog = SimpleNamespace(type="alert", message="pending")
    registry.downloads.append(
        DownloadRecord(
            sequence=1,
            epoch=0,
            url="https://example.test/download",
            suggested_filename="artifact.txt",
            download=object(),
            artifact="artifact.txt",
            finished=True,
        )
    )
    state.next_download_sequence = 2

    first = server._render_response(
        "done", page=page, snapshot="- current", include_tabs=True
    )
    section_names = re.findall(r"^### (\w+)", first, flags=re.MULTILINE)
    assert section_names == [
        "Result",
        "Page",
        "Tabs",
        "Snapshot",
        "Console",
        "Modal",
        "Downloads",
    ]
    assert first.count("WARNING ") == 5
    assert "(and 2 more)" in first
    assert first.count("### Downloads") == 1

    second = server._render_response("next", page=page)
    assert "### Console" not in second
    assert "### Downloads" not in second
    registry.console_records.append(
        ConsoleRecord(
            sequence=8,
            epoch=0,
            message_type="error",
            text="new-only",
            location=(),
        )
    )
    state.next_console_sequence = 9
    third = server._render_response("third", page=page)
    assert "new-only" in third
    assert "warning-" not in third
