"""Independent adversarial validation for the Phase-A compatibility surface."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import sys

from mcp.server.fastmcp.exceptions import ToolError
import pytest

from rustwright_mcp import server
from rustwright_mcp.filepolicy import FilePolicy
from rustwright_mcp.session import SessionState

sys.path.insert(0, str(Path(__file__).parents[1]))
from test_pr2_compatibility import FakePage, _stdio_tools, _tool_call
from test_smoke import FIXTURE, _call, _result_section, _run_session


ADAPTED_CONTRACT = {
    "browser_click": [
        {"name": "element", "type": "string", "required": False},
        {"name": "target", "type": "string", "required": True},
        {
            "name": "doubleClick",
            "type": "boolean",
            "required": False,
            "default": False,
        },
        {
            "name": "button",
            "type": "string",
            "required": False,
            "enum": ["left", "right", "middle"],
            "default": "left",
        },
        {
            "name": "modifiers",
            "type": "array",
            "required": False,
            "items": {
                "type": "string",
                "enum": ["Alt", "Control", "ControlOrMeta", "Meta", "Shift"],
            },
        },
    ],
    "browser_type": [
        {"name": "element", "type": "string", "required": False},
        {"name": "target", "type": "string", "required": True},
        {"name": "text", "type": "string", "required": True},
        {"name": "submit", "type": "boolean", "required": False, "default": False},
        {"name": "slowly", "type": "boolean", "required": False, "default": False},
        {"name": "clear", "type": "boolean", "required": False, "default": True},
    ],
    "browser_select_option": [
        {"name": "element", "type": "string", "required": False},
        {"name": "target", "type": "string", "required": True},
        {
            "name": "values",
            "type": "array",
            "required": True,
            "items": {"type": "string"},
        },
    ],
    "browser_hover": [
        {"name": "element", "type": "string", "required": False},
        {"name": "target", "type": "string", "required": True},
    ],
    "browser_snapshot": [
        {"name": "target", "type": "string", "required": False},
        {"name": "filename", "type": "string", "required": False},
        {"name": "depth", "type": "number", "required": False},
        {
            "name": "boxes",
            "type": "boolean",
            "required": False,
            "default": False,
        },
    ],
    "browser_take_screenshot": [
        {"name": "element", "type": "string", "required": False},
        {"name": "target", "type": "string", "required": False},
        {
            "name": "type",
            "type": "string",
            "required": False,
            "enum": ["png", "jpeg"],
            "default": "png",
        },
        {"name": "filename", "type": "string", "required": False},
        {
            "name": "fullPage",
            "type": "boolean",
            "required": False,
            "default": False,
        },
        {
            "name": "scale",
            "type": "string",
            "required": False,
            "enum": ["css", "device"],
            "default": "css",
        },
    ],
    "browser_evaluate": [
        {"name": "element", "type": "string", "required": False},
        {"name": "target", "type": "string", "required": False},
        {"name": "function", "type": "string", "required": True},
        {"name": "filename", "type": "string", "required": False},
    ],
    "browser_handle_dialog": [
        {"name": "accept", "type": "boolean", "required": True},
        {"name": "promptText", "type": "string", "required": False},
    ],
    "browser_wait_for": [
        {"name": "time", "type": "number", "required": False},
        {"name": "text", "type": "string", "required": False},
        {"name": "textGone", "type": "string", "required": False},
        {
            "name": "timeout_ms",
            "type": "number",
            "required": False,
            "default": 10000,
        },
    ],
    "browser_tabs": [
        {
            "name": "action",
            "type": "string",
            "required": True,
            "enum": ["list", "new", "close", "select"],
        },
        {"name": "index", "type": "integer", "required": False},
        {"name": "url", "type": "string", "required": False},
    ],
}


@pytest.fixture
def fake_runtime(monkeypatch, tmp_path) -> tuple[FakePage, SessionState, FilePolicy]:
    page = FakePage()
    state = SessionState(page=page)
    policy = FilePolicy(output_root=tmp_path / "output")
    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_page", lambda: state.page)
    monkeypatch.setattr(server, "_snapshot", lambda *args, **kwargs: "- snapshot")
    monkeypatch.setattr(server, "get_file_policy", lambda: policy)
    return page, state, policy


def test_fixture_exactly_matches_the_ten_audited_schemas_and_has_no_prose() -> None:
    def assert_neutral(shape: dict, *, parameter: bool) -> None:
        structural = {"type", "enum", "default", "items", "params", "additionalProperties"}
        allowed = structural | ({"name", "required"} if parameter else set())
        assert set(shape) <= allowed
        if "items" in shape:
            assert_neutral(shape["items"], parameter=False)
        if "additionalProperties" in shape:
            assert_neutral(shape["additionalProperties"], parameter=False)
        for nested in shape.get("params", []):
            assert_neutral(nested, parameter=True)

    fixture_path = (
        Path(__file__).parents[1] / "contract" / "fixtures" / "default_toolset.json"
    )
    raw = json.loads(fixture_path.read_text())

    for tool_name, params in ADAPTED_CONTRACT.items():
        assert raw[tool_name] == {"params": params}
    for tool in raw.values():
        assert set(tool) == {"params"}
        for param in tool["params"]:
            assert_neutral(param, parameter=True)


def test_alias_conflicts_always_prefer_the_canonical_spelling(fake_runtime) -> None:
    page, state, policy = fake_runtime

    class Dialog:
        prompt_text = None

        def accept(self, prompt_text=None) -> None:
            self.prompt_text = prompt_text

    _tool_call(
        "browser_click",
        {"target": "#click", "doubleClick": True, "double_click": False},
    )
    _tool_call(
        "browser_select_option",
        {"target": "#select", "values": ["canonical"], "value": "legacy"},
    )
    _tool_call(
        "browser_take_screenshot",
        {
            "filename": "capture.png",
            "fullPage": True,
            "full_page": False,
        },
    )
    evaluate = _tool_call(
        "browser_evaluate",
        {"function": "canonical", "expression": "legacy"},
    )
    dialog = Dialog()
    state.registry_for(page, create=True).pending_dialog = dialog
    _tool_call(
        "browser_handle_dialog",
        {"accept": True, "promptText": "canonical", "prompt_text": "legacy"},
    )
    _tool_call(
        "browser_wait_for",
        {"textGone": "canonical", "text_gone": "legacy"},
    )

    click = next(event for event in page.events if event[0] == "click")
    selected = next(event for event in page.events if event[0] == "select")
    screenshot = next(event for event in page.events if event[0] == "page-screenshot")
    waits = [event for event in page.events if event[0] == "text-wait"]
    assert click[2]["click_count"] == 2
    assert selected[2] == ["canonical"]
    assert screenshot[-1] is True
    assert (policy.output_root / "capture.png").exists()
    assert "canonical" in evaluate and "legacy" not in evaluate
    assert dialog.prompt_text == "canonical"
    assert waits == [("text-wait", "canonical", "hidden", 10_000)]


def test_advertised_schemas_are_canonical_and_exact(fake_runtime) -> None:
    del fake_runtime
    schemas = {
        tool.name: tool.inputSchema for tool in asyncio.run(server.mcp.list_tools())
    }
    extension_names = {
        "browser_type": {"clear"},
        "browser_wait_for": {"timeout_ms"},
    }
    hidden_aliases = {
        "double_click",
        "value",
        "path",
        "full_page",
        "expression",
        "prompt_text",
        "text_gone",
    }

    for tool_name, contract in ADAPTED_CONTRACT.items():
        schema = schemas[tool_name]
        expected = {param["name"] for param in contract}
        expected |= extension_names.get(tool_name, set())
        assert set(schema["properties"]) == expected
        assert not hidden_aliases & set(schema["properties"])
        assert set(schema.get("required", ())) == {
            param["name"] for param in contract if param["required"]
        }

        for param in contract:
            advertised = schema["properties"][param["name"]]
            if "default" in param:
                assert advertised["default"] == param["default"]
            if "enum" in param:
                candidate = advertised
                if "enum" not in candidate:
                    candidate = next(
                        option
                        for option in advertised.get("anyOf", ())
                        if "enum" in option or "items" in option
                    )
                actual_enum = candidate.get("enum", candidate.get("items", {}).get("enum"))
                assert actual_enum == param["enum"]


UNKNOWN_CASES = [
    ("browser_click", {"target": "#x"}),
    ("browser_type", {"target": "#x", "text": "value"}),
    ("browser_select_option", {"target": "#x", "values": ["value"]}),
    ("browser_hover", {"target": "#x"}),
    ("browser_snapshot", {}),
    ("browser_take_screenshot", {"filename": "unknown.png"}),
    ("browser_evaluate", {"function": "() => 1"}),
    ("browser_handle_dialog", {"accept": True}),
    ("browser_wait_for", {"time": 0}),
    ("browser_tabs", {"action": "list"}),
]


@pytest.mark.parametrize(("tool_name", "valid"), UNKNOWN_CASES)
def test_unknown_parameters_are_structured_rejections(
    fake_runtime, tool_name: str, valid: dict
) -> None:
    del fake_runtime
    with pytest.raises(ToolError, match="validation error"):
        _tool_call(tool_name, {**valid, "semantic_change": True})


def test_wait_cap_missing_condition_and_hidden_state(fake_runtime) -> None:
    page, _, _ = fake_runtime
    _tool_call("browser_wait_for", {"time": 31, "textGone": "departed"})
    assert ("time-wait", 30_000) in page.events
    assert ("text-wait", "departed", "hidden", 10_000) in page.events

    with pytest.raises(ToolError, match="At least one"):
        _tool_call("browser_wait_for", {})


def test_tabs_close_current_middle_and_every_action_returns_tabs(fake_runtime) -> None:
    first, state, _ = fake_runtime
    second = first.context.new_page()
    third = first.context.new_page()
    state.page = second

    listed = _tool_call("browser_tabs", {"action": "list"})
    selected = _tool_call("browser_tabs", {"action": "select", "index": 1})
    closed = _tool_call("browser_tabs", {"action": "close"})
    opened = _tool_call("browser_tabs", {"action": "new"})

    assert second not in first.context.pages
    assert state.page is first.context.pages[-1]
    assert first in first.context.pages and third in first.context.pages
    assert all("### Tabs" in response for response in (listed, selected, closed, opened))
    with pytest.raises(ToolError, match="Invalid tab index"):
        _tool_call("browser_tabs", {"action": "select", "index": 99})


def test_envelope_order_and_one_snapshot_per_mutating_adapter(
    fake_runtime, monkeypatch
) -> None:
    _, state, _ = fake_runtime
    snapshot_calls: list[object] = []

    def counted_snapshot(page, **kwargs):
        snapshot_calls.append(page)
        return "- counted snapshot"

    monkeypatch.setattr(server, "_snapshot", counted_snapshot)
    cases = [
        ("browser_click", {"target": "#x"}, False),
        ("browser_type", {"target": "#x", "text": "value"}, False),
        ("browser_select_option", {"target": "#x", "values": ["value"]}, False),
        ("browser_hover", {"target": "#x"}, False),
        ("browser_wait_for", {"time": 0}, False),
        ("browser_evaluate", {"function": "() => 1"}, False),
        ("browser_tabs", {"action": "new"}, True),
        ("browser_tabs", {"action": "select", "index": 0}, True),
        ("browser_tabs", {"action": "close"}, True),
    ]

    for tool_name, arguments, has_tabs in cases:
        before = len(snapshot_calls)
        response = _tool_call(tool_name, arguments)
        assert len(snapshot_calls) == before + 1, tool_name
        expected = ["### Result", "### Page"]
        if has_tabs:
            expected.append("### Tabs")
        expected.append("### Snapshot")
        assert re.findall(r"(?m)^### [^\n]+$", response) == expected
        assert state.page is not None


def test_caps_environment_precedence_and_eval_startup_warning_once() -> None:
    tools, stderr = asyncio.run(
        _stdio_tools(
            env_overrides={"RUSTWRIGHT_MCP_CAPS": "network"},
            extra_args=["--caps=storage,bogus"],
        )
    )
    assert "browser_navigate" in tools
    assert "capability group 'network' is not implemented" in stderr
    assert "capability group 'storage'" not in stderr
    assert "capability group 'bogus'" not in stderr
    assert stderr.count("warning: browser_evaluate is enabled") == 1

    for profile in ("mirror", "lean"):
        disabled, disabled_stderr = asyncio.run(
            _stdio_tools(
                env_overrides={
                    "RUSTWRIGHT_MCP_TOOLSET": profile,
                    "RUSTWRIGHT_MCP_ALLOW_EVAL": "0",
                }
            )
        )
        assert "browser_evaluate" not in disabled
        assert "warning: browser_evaluate is enabled" not in disabled_stderr


def test_real_browser_adversarial_schema_semantics(tmp_path) -> None:
    output_root = tmp_path / "output"
    escaped = tmp_path / "escaped.jpeg"
    invalid_boxes: list[str] = []

    async def checks(session) -> None:
        await _call(session, "browser_navigate", url=FIXTURE.as_uri())

        depth_zero = await _call(session, "browser_snapshot", target="#f", depth=0)
        depth_one = await _call(session, "browser_snapshot", target="#f", depth=1)
        boxes = await _call(session, "browser_snapshot", target="#f", boxes=True)
        assert "- form" in depth_zero
        assert "textbox" not in depth_zero
        assert 'textbox "Customer name"' in depth_one
        assert 'option "Small"' not in depth_one
        box_values = re.findall(r"\[box=([^\]]+)\]", boxes)
        assert box_values
        for raw_box in box_values:
            parts = raw_box.split(",")
            if len(parts) != 4 or not all(
                re.fullmatch(r"-?\d+", part) for part in parts
            ):
                invalid_boxes.append(raw_box)
                continue
            x, y, width, height = map(int, parts)
            if not (
                -2000 <= x <= 4000
                and -2000 <= y <= 4000
                and 0 < width <= 4000
                and 0 < height <= 4000
            ):
                invalid_boxes.append(raw_box)

        negative_depth = await session.call_tool(
            "browser_snapshot", {"target": "#f", "depth": -1}
        )
        assert negative_depth.isError
        assert "depth must be non-negative" in "\n".join(
            item.text for item in negative_depth.content if item.type == "text"
        )

        evaluated = await _call(
            session,
            "browser_evaluate",
            function="el => el.tagName",
            element="Customer name",
            target="#name",
        )
        assert json.loads(_result_section(evaluated)) == "INPUT"

        conflict = await session.call_tool(
            "browser_take_screenshot",
            {"element": "Form", "target": "#f", "fullPage": True},
        )
        assert conflict.isError
        assert "mutually exclusive" in "\n".join(
            item.text for item in conflict.content if item.type == "text"
        )

        traversal = await session.call_tool(
            "browser_take_screenshot",
            {"type": "jpeg", "filename": "../escaped.jpeg"},
        )
        assert traversal.isError
        assert "confined to RUSTWRIGHT_MCP_OUTPUT_DIR" in "\n".join(
            item.text for item in traversal.content if item.type == "text"
        )
        assert not escaped.exists()

        await _call(
            session,
            "browser_take_screenshot",
            type="png",
            filename="device.png",
            scale="device",
        )
        assert (output_root / "device.png").read_bytes().startswith(b"\x89PNG")
        await _call(
            session,
            "browser_take_screenshot",
            type="jpeg",
            filename="capture.jpeg",
        )
        assert (output_root / "capture.jpeg").read_bytes().startswith(b"\xff\xd8\xff")

        tab_responses = []
        tab_responses.append(
            await _call(
                session,
                "browser_tabs",
                action="new",
                url="data:text/html,<title>Middle</title><p>middle</p>",
            )
        )
        tab_responses.append(
            await _call(
                session,
                "browser_tabs",
                action="new",
                url="data:text/html,<title>Last</title><p>last</p>",
            )
        )
        tab_responses.append(await _call(session, "browser_tabs", action="select", index=1))
        tab_responses.append(await _call(session, "browser_tabs", action="close"))
        tab_responses.append(await _call(session, "browser_tabs", action="list"))
        assert all("### Tabs" in response for response in tab_responses)
        assert "Middle" not in tab_responses[-1]
        assert "Form Test" in tab_responses[-1]
        assert "Last" in tab_responses[-1]

        out_of_bounds = await session.call_tool(
            "browser_tabs", {"action": "select", "index": 99}
        )
        assert out_of_bounds.isError
        assert "Invalid tab index" in "\n".join(
            item.text for item in out_of_bounds.content if item.type == "text"
        )
        await _call(session, "browser_close")
        assert invalid_boxes == [], f"non-integer or implausible boxes: {invalid_boxes}"

    asyncio.run(
        _run_session(
            checks,
            {"RUSTWRIGHT_MCP_OUTPUT_DIR": str(output_root)},
        )
    )
