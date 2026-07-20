"""Phase-A schema, response, capability, and profile compatibility tests."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError
import pytest

from rustwright_mcp import server
from rustwright_mcp.filepolicy import FilePolicy
from rustwright_mcp.session import SessionState
from test_smoke import _call, _result_section, _run_session


FORM_FIXTURE = Path(__file__).parent / "fixtures" / "form.html"


class FakeLocator:
    def __init__(self, page: "FakePage", selector: str) -> None:
        self.page = page
        self.selector = selector

    def count(self) -> int:
        return 1

    def click(self, **kwargs) -> None:
        self.page.events.append(("click", self.selector, kwargs))

    def fill(self, value: str) -> None:
        self.page.events.append(("fill", self.selector, value))

    def type(self, value: str) -> None:
        self.page.events.append(("type", self.selector, value))

    def press_sequentially(self, value: str, *, delay: float) -> None:
        self.page.events.append(("slow", self.selector, value, delay))

    def press(self, key: str) -> None:
        self.page.events.append(("press", self.selector, key))

    def select_option(self, *, value=None, label=None):
        self.page.events.append(("select", self.selector, value, label))
        return value or label

    def hover(self) -> None:
        self.page.events.append(("hover", self.selector))

    def evaluate(self, function: str, argument=None):
        if argument is not None:
            return self.selector == argument.selector
        self.page.events.append(("locator-evaluate", self.selector, function))
        return {"context": self.selector, "function": function}

    def inner_text(self) -> str:
        return "fake text"

    def screenshot(self, *, path: str, type: str, scale: str) -> None:
        Path(path).write_bytes(b"image")
        self.page.events.append(("element-screenshot", self.selector, type, scale))


class FakeTextLocator:
    def __init__(self, page: "FakePage", text: str) -> None:
        self.page = page
        self.text = text

    def wait_for(self, *, state: str, timeout: float) -> None:
        self.page.events.append(("text-wait", self.text, state, timeout))


class FakeKeyboard:
    def __init__(self, page: "FakePage") -> None:
        self.page = page

    def press(self, key: str) -> None:
        self.page.events.append(("page-press", key))


class FakeContext:
    def __init__(self) -> None:
        self.pages: list[FakePage] = []

    def new_page(self) -> "FakePage":
        return FakePage(self)


class FakePage:
    def __init__(self, context: FakeContext | None = None) -> None:
        self.context = context or FakeContext()
        self.context.pages.append(self)
        self.url = "https://example.test/"
        self.events: list[tuple] = []
        self.keyboard = FakeKeyboard(self)
        self.handlers: dict[str, list] = {}

    def title(self) -> str:
        return "Fake page"

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def evaluate(self, function: str):
        self.events.append(("page-evaluate", function))
        return {"context": "page", "function": function}

    def get_by_text(self, text: str) -> FakeTextLocator:
        return FakeTextLocator(self, text)

    def wait_for_timeout(self, timeout: float) -> None:
        self.events.append(("time-wait", timeout))

    def screenshot(
        self, *, path: str, type: str, scale: str, full_page: bool
    ) -> None:
        Path(path).write_bytes(b"image")
        self.events.append(("page-screenshot", type, scale, full_page))

    def on(self, event: str, callback) -> None:
        self.handlers.setdefault(event, []).append(callback)

    def bring_to_front(self) -> None:
        self.events.append(("front",))

    def close(self) -> None:
        self.context.pages.remove(self)


@pytest.fixture
def fake_page(monkeypatch) -> FakePage:
    page = FakePage()
    state = SessionState(page=page)
    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_page", lambda: page)
    monkeypatch.setattr(server, "_snapshot", lambda *args, **kwargs: "- snapshot")
    return page


def _tool_call(name: str, arguments: dict):
    return asyncio.run(server.mcp._tool_manager.call_tool(name, arguments, {}))


def test_click_camel_snake_scalar_array_and_canonical_precedence(fake_page) -> None:
    _tool_call(
        "browser_click",
        {
            "target": "#one",
            "element": "First button",
            "doubleClick": True,
            "modifiers": "Alt",
        },
    )
    _tool_call("browser_click", {"target": "#two", "double_click": True})
    _tool_call(
        "browser_click",
        {"target": "#three", "doubleClick": False, "double_click": True},
    )

    clicks = [event for event in fake_page.events if event[0] == "click"]
    assert clicks[0][2]["click_count"] == 2
    assert clicks[0][2]["modifiers"] == ["Alt"]
    assert clicks[1][2]["click_count"] == 2
    assert clicks[2][2]["click_count"] == 1


def test_failed_upload_does_not_replay_click_on_different_element(
    fake_page, monkeypatch, tmp_path
) -> None:
    class OriginatingElement:
        selector = "#upload"

        def evaluate(self, function):
            fake_page.events.append(("reset-upload", function))

    class Chooser:
        element = OriginatingElement()

        def is_multiple(self):
            return False

        def set_files(self, paths):
            fake_page.events.append(("set-files", paths))

    state = server._state
    registry = state.register_page_handlers(fake_page)
    registry.pending_file_chooser = Chooser()
    policy = FilePolicy(output_root=tmp_path / "output")
    monkeypatch.setattr(server, "get_file_policy", lambda: policy)

    with pytest.raises(ValueError, match="Retry by clicking the same file input"):
        server.browser_file_upload(paths=["relative.txt"])
    assert registry.file_chooser_retry_element is not None

    server.browser_click(target="#unrelated")
    unrelated_clicks = [
        event
        for event in fake_page.events
        if event[0] == "click" and event[1] == "#unrelated"
    ]
    assert len(unrelated_clicks) == 1


def test_type_canonical_slowly_and_legacy_clear_are_independent(fake_page) -> None:
    _tool_call(
        "browser_type",
        {"target": "#field", "text": "abc", "slowly": True, "clear": True},
    )
    _tool_call(
        "browser_type",
        {"target": "#field", "text": "xyz", "clear": False},
    )
    assert ("fill", "#field", "") in fake_page.events
    assert ("slow", "#field", "abc", 50) in fake_page.events
    assert ("type", "#field", "xyz") in fake_page.events


def test_select_values_and_legacy_value_with_canonical_precedence(fake_page) -> None:
    _tool_call("browser_select_option", {"target": "#size", "values": ["m"]})
    _tool_call("browser_select_option", {"target": "#size", "value": "s"})
    _tool_call(
        "browser_select_option",
        {"target": "#size", "values": ["l"], "value": "s"},
    )
    selected = [event[2] for event in fake_page.events if event[0] == "select"]
    assert selected == [["m"], ["s"], ["l"]]


def test_hover_accepts_description_and_legacy_target_only(fake_page) -> None:
    _tool_call("browser_hover", {"target": "#one", "element": "First item"})
    _tool_call("browser_hover", {"target": "#two"})
    assert ("hover", "#one") in fake_page.events
    assert ("hover", "#two") in fake_page.events


def test_snapshot_canonical_options_and_legacy_minimal_call(fake_page, monkeypatch) -> None:
    calls: list[dict] = []

    def snapshot(page, **kwargs):
        calls.append(kwargs)
        return "- snapshot"

    monkeypatch.setattr(server, "_snapshot", snapshot)
    _tool_call(
        "browser_snapshot",
        {"target": "#root", "depth": 2, "boxes": True},
    )
    _tool_call("browser_snapshot", {})
    assert calls[0]["target"].selector == "#root"
    assert calls[0]["depth"] == 2
    assert calls[0]["boxes"] is True
    assert calls[1] == {"target": None, "depth": None, "boxes": False}


def test_screenshot_camel_snake_and_canonical_precedence(
    fake_page, monkeypatch, tmp_path
) -> None:
    policy = FilePolicy(output_root=tmp_path / "output")
    monkeypatch.setattr(server, "get_file_policy", lambda: policy)

    _tool_call(
        "browser_take_screenshot",
        {"filename": "camel.png", "fullPage": True, "scale": "device"},
    )
    _tool_call(
        "browser_take_screenshot",
        {"path": "snake.png", "full_page": True},
    )
    _tool_call(
        "browser_take_screenshot",
        {
            "filename": "winner.png",
            "path": "loser.png",
            "fullPage": False,
            "full_page": True,
        },
    )
    _tool_call(
        "browser_take_screenshot",
        {"target": "#field", "element": "Field", "filename": "element.png"},
    )
    assert (policy.output_root / "camel.png").exists()
    assert (policy.output_root / "snake.png").exists()
    assert (policy.output_root / "winner.png").exists()
    assert (policy.output_root / "element.png").exists()
    assert not (policy.output_root / "loser.png").exists()
    screenshots = [event for event in fake_page.events if event[0] == "page-screenshot"]
    assert screenshots == [
        ("page-screenshot", "png", "device", True),
        ("page-screenshot", "png", "css", True),
        ("page-screenshot", "png", "css", False),
    ]
    assert ("element-screenshot", "#field", "png", "css") in fake_page.events


def test_evaluate_function_expression_target_json_and_precedence(
    fake_page, monkeypatch, tmp_path
) -> None:
    policy = FilePolicy(output_root=tmp_path / "output")
    monkeypatch.setattr(server, "get_file_policy", lambda: policy)
    canonical = _tool_call("browser_evaluate", {"function": "canonical"})
    legacy = _tool_call("browser_evaluate", {"expression": "legacy"})
    winner = _tool_call(
        "browser_evaluate",
        {"function": "winner", "expression": "loser", "filename": "value.json"},
    )
    targeted = _tool_call(
        "browser_evaluate",
        {"function": "element => element.id", "target": "#field", "element": "Field"},
    )

    assert '"function": "canonical"' in canonical
    assert '"function": "legacy"' in legacy
    assert '"function": "winner"' in winner
    assert "loser" not in winner
    assert '"context": "#field"' in targeted
    assert "### Snapshot" in targeted
    assert '"function": "winner"' in (policy.output_root / "value.json").read_text()


def test_dialog_camel_snake_and_canonical_precedence(fake_page) -> None:
    class Dialog:
        def __init__(self) -> None:
            self.prompt_text = None

        def accept(self, prompt_text=None) -> None:
            self.prompt_text = prompt_text

    def pending_dialog() -> Dialog:
        registry = server._state.registry_for(fake_page, create=True)
        dialog = Dialog()
        registry.pending_dialog = dialog
        return dialog

    camel = pending_dialog()
    _tool_call("browser_handle_dialog", {"accept": True, "promptText": "camel"})
    assert camel.prompt_text == "camel"
    snake = pending_dialog()
    _tool_call("browser_handle_dialog", {"accept": True, "prompt_text": "snake"})
    assert snake.prompt_text == "snake"
    winner = pending_dialog()
    _tool_call(
        "browser_handle_dialog",
        {"accept": True, "promptText": "winner", "prompt_text": "loser"},
    )
    assert winner.prompt_text == "winner"


def test_wait_camel_snake_precedence_time_cap_and_text_states(fake_page) -> None:
    _tool_call(
        "browser_wait_for",
        {"time": 99, "text": "visible", "textGone": "camel", "timeout_ms": 123},
    )
    _tool_call("browser_wait_for", {"text_gone": "snake"})
    _tool_call(
        "browser_wait_for",
        {"textGone": "winner", "text_gone": "loser"},
    )
    assert ("time-wait", 30_000) in fake_page.events
    assert ("text-wait", "visible", "visible", 123) in fake_page.events
    assert ("text-wait", "camel", "hidden", 123) in fake_page.events
    assert ("text-wait", "snake", "hidden", 10_000) in fake_page.events
    assert ("text-wait", "winner", "hidden", 10_000) in fake_page.events
    assert not any(event[1:2] == ("loser",) for event in fake_page.events)


def test_tabs_list_and_close_current_register_replacement(fake_page) -> None:
    listed = _tool_call("browser_tabs", {"action": "list"})
    assert "### Tabs" in listed
    assert "0: Fake page" in listed
    closed = _tool_call("browser_tabs", {"action": "close"})
    assert "### Tabs" in closed
    assert server._state.page is not fake_page
    assert len(server._state.page.context.pages) == 1


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("browser_click", {"target": "#x", "button": "primary"}),
        ("browser_click", {"target": "#x", "modifiers": ["Command"]}),
        ("browser_take_screenshot", {"type": "gif"}),
        ("browser_take_screenshot", {"scale": "logical"}),
        ("browser_tabs", {"action": "switch"}),
    ],
)
def test_literal_enums_reject_unknown_values(fake_page, tool_name, arguments) -> None:
    with pytest.raises(ToolError, match="validation error"):
        _tool_call(tool_name, arguments)


def test_runtime_structured_rejections(fake_page) -> None:
    with pytest.raises(ToolError, match="At least one"):
        _tool_call("browser_wait_for", {})
    with pytest.raises(ToolError, match="element requires target"):
        _tool_call("browser_evaluate", {"function": "x", "element": "orphan"})
    with pytest.raises(ToolError, match="mutually exclusive"):
        _tool_call(
            "browser_take_screenshot",
            {"target": "#x", "fullPage": True},
        )


def test_device_scale_has_structured_unsupported_error(fake_page, monkeypatch) -> None:
    monkeypatch.setattr(server, "_supports_screenshot_scale", lambda target: False)
    with pytest.raises(ToolError, match="scale=device is unsupported"):
        _tool_call("browser_take_screenshot", {"scale": "device"})


def test_advertised_schema_hides_legacy_aliases() -> None:
    schemas = {
        tool.name: tool.inputSchema for tool in asyncio.run(server.mcp.list_tools())
    }
    assert "doubleClick" in schemas["browser_click"]["properties"]
    assert "double_click" not in schemas["browser_click"]["properties"]
    assert "promptText" in schemas["browser_handle_dialog"]["properties"]
    assert "prompt_text" not in schemas["browser_handle_dialog"]["properties"]
    assert "textGone" in schemas["browser_wait_for"]["properties"]
    assert "text_gone" not in schemas["browser_wait_for"]["properties"]
    assert "fullPage" in schemas["browser_take_screenshot"]["properties"]
    assert "full_page" not in schemas["browser_take_screenshot"]["properties"]
    assert "function" in schemas["browser_evaluate"]["properties"]
    assert "expression" not in schemas["browser_evaluate"]["properties"]


def test_response_envelope_has_deterministic_section_order(fake_page) -> None:
    response = server._render_response(
        "ok", page=fake_page, include_tabs=True, snapshot="- tree"
    )
    headings = ["### Result", "### Page", "### Tabs", "### Snapshot"]
    assert all(heading in response for heading in headings)
    assert [response.index(heading) for heading in headings] == sorted(
        response.index(heading) for heading in headings
    )


def test_caps_parser_env_precedence_and_warnings(capsys) -> None:
    assert server._configured_caps(
        ["--caps=vision,pdf"], {"RUSTWRIGHT_MCP_CAPS": "network, testing"}
    ) == ("network", "testing")
    assert server._configured_caps(["--caps=vision,pdf"], {}) == ("vision", "pdf")
    server._warn_ignored_caps(("vision", "pdf"))
    warning = capsys.readouterr().err
    assert "capability group 'vision' is not implemented" in warning
    assert "capability group 'pdf' is not implemented" in warning


def test_unknown_allow_eval_value_fails_startup_instead_of_enabling_eval() -> None:
    env = dict(os.environ)
    env["RUSTWRIGHT_MCP_ALLOW_EVAL"] = "flase"
    result = subprocess.run(
        [sys.executable, "-c", "import rustwright_mcp.server"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "RUSTWRIGHT_MCP_ALLOW_EVAL must be one of" in result.stderr
    assert "'flase'" in result.stderr


async def _stdio_tools(
    *, env_overrides: dict[str, str] | None = None, extra_args: list[str] | None = None
) -> tuple[set[str], str]:
    env = dict(os.environ)
    for name in (
        "RUSTWRIGHT_MCP_ALLOW_EVAL",
        "RUSTWRIGHT_MCP_CAPS",
        "RUSTWRIGHT_MCP_TOOLSET",
    ):
        env.pop(name, None)
    if env_overrides:
        env.update(env_overrides)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "rustwright_mcp", *(extra_args or [])],
        env=env,
    )
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {tool.name for tool in (await session.list_tools()).tools}
        errlog.seek(0)
        errors = errlog.read()
    return tools, errors


def test_caps_argv_and_env_warn_without_blocking_tools_list() -> None:
    argv_tools, argv_stderr = asyncio.run(
        _stdio_tools(extra_args=["--caps=vision,pdf"])
    )
    assert "browser_navigate" in argv_tools
    assert "capability group 'vision' is not implemented" in argv_stderr
    assert "capability group 'pdf' is not implemented" in argv_stderr

    env_tools, env_stderr = asyncio.run(
        _stdio_tools(
            env_overrides={"RUSTWRIGHT_MCP_CAPS": "network"},
            extra_args=["--caps=vision"],
        )
    )
    assert "browser_navigate" in env_tools
    assert "capability group 'network' is not implemented" in env_stderr
    assert "capability group 'vision'" not in env_stderr


def test_mirror_and_lean_tool_profiles_with_eval_default_on() -> None:
    mirror, _ = asyncio.run(_stdio_tools())
    assert len(mirror) == 25
    assert {
        "browser_evaluate",
        "browser_get_text",
        "browser_handle_dialog",
        "browser_console_messages",
        "browser_drag",
        "browser_drop",
        "browser_file_upload",
        "browser_fill_form",
        "browser_find",
        "browser_network_request",
        "browser_network_requests",
        "browser_resize",
    } <= mirror

    lean, _ = asyncio.run(
        _stdio_tools(env_overrides={"RUSTWRIGHT_MCP_TOOLSET": "lean"})
    )
    assert lean == server._LEAN_TOOLS

    lean_without_eval, _ = asyncio.run(
        _stdio_tools(
            env_overrides={
                "RUSTWRIGHT_MCP_TOOLSET": "lean",
                "RUSTWRIGHT_MCP_ALLOW_EVAL": "0",
            }
        )
    )
    assert lean_without_eval == server._LEAN_TOOLS - {"browser_evaluate"}


def test_real_snapshot_target_depth_boxes_files_and_target_evaluate(tmp_path) -> None:
    output_root = tmp_path / "output"

    async def checks(session) -> None:
        await _call(session, "browser_navigate", url=FORM_FIXTURE.as_uri())
        targeted = await _call(
            session,
            "browser_snapshot",
            target="#f",
            depth=1,
            boxes=True,
        )
        assert "### Snapshot" in targeted
        assert "- form" in targeted
        assert "[box=" in targeted
        assert 'option "Small"' not in targeted

        written = await _call(
            session,
            "browser_snapshot",
            target="#f",
            filename="tree.md",
        )
        assert "Snapshot written to `tree.md`" in written
        assert (output_root / "tree.md").read_text().startswith("- form")

        evaluated = await _call(
            session,
            "browser_evaluate",
            function="element => ({tag: element.tagName, id: element.id})",
            element="Customer name field",
            target="#name",
            filename="value.json",
        )
        serialized = _result_section(evaluated).split("\n\nSaved to:", 1)[0]
        assert json.loads(serialized) == {"tag": "INPUT", "id": "name"}
        assert "### Snapshot" in evaluated
        assert json.loads((output_root / "value.json").read_text()) == {
            "tag": "INPUT",
            "id": "name",
        }
        await _call(session, "browser_close")

    asyncio.run(
        _run_session(
            checks,
            {"RUSTWRIGHT_MCP_OUTPUT_DIR": str(output_root)},
        )
    )
