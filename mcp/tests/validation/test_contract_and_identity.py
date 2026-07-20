"""Clean-room contract and real-stdio identity/inventory validation."""

from __future__ import annotations

import asyncio
from importlib import metadata
import json
import os
from pathlib import Path
import shutil
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

from test_smoke import _result_section

FIXTURE = Path(__file__).parents[1] / "contract" / "fixtures" / "default_toolset.json"
FORM_FIXTURE = Path(__file__).parents[1] / "fixtures" / "form.html"
HARDENING_FIXTURE = Path(__file__).parents[1] / "fixtures" / "hardening.html"


def _server_command() -> list[str]:
    executable = shutil.which("rustwright-mcp")
    if executable:
        return [executable]
    return [sys.executable, "-m", "rustwright_mcp"]


async def _call(session, name: str, **arguments) -> str:
    result = await session.call_tool(name, arguments)
    text = "\n".join(item.text for item in result.content if item.type == "text")
    if result.isError:
        if "Failed to launch chromium" in text:
            pytest.skip("no Chromium available for regression smoke")
        raise AssertionError(f"{name} failed: {text}")
    return text


def test_contract_fixture_contains_only_neutral_schema_fields() -> None:
    def assert_shape(shape: dict, *, parameter: bool) -> None:
        structural = {"type", "enum", "default", "items", "params", "additionalProperties"}
        allowed = structural | ({"name", "required"} if parameter else set())
        assert set(shape) <= allowed
        assert "type" in shape
        if parameter:
            assert {"name", "required"} <= set(shape)
        if "items" in shape:
            assert_shape(shape["items"], parameter=False)
        if "additionalProperties" in shape:
            assert_shape(shape["additionalProperties"], parameter=False)
        for nested in shape.get("params", []):
            assert_shape(nested, parameter=True)

    raw = json.loads(FIXTURE.read_text())
    assert isinstance(raw, dict)
    for tool_name, tool in raw.items():
        assert isinstance(tool_name, str)
        assert set(tool) == {"params"}
        for param in tool["params"]:
            assert_shape(param, parameter=True)


def test_real_stdio_identity_and_complete_tool_inventory() -> None:
    expected_tools = {
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_select_option",
        "browser_hover",
        "browser_press_key",
        "browser_navigate_back",
        "browser_reload",
        "browser_tabs",
        "browser_handle_dialog",
        "browser_wait_for",
        "browser_get_text",
        "browser_evaluate",
        "browser_take_screenshot",
        "browser_close",
        "browser_console_messages",
        "browser_drag",
        "browser_drop",
        "browser_file_upload",
        "browser_fill_form",
        "browser_find",
        "browser_network_request",
        "browser_network_requests",
        "browser_resize",
    }

    async def checks() -> None:
        command = _server_command()
        env = dict(os.environ)
        env["RUSTWRIGHT_MCP_ALLOW_EVAL"] = "1"
        params = StdioServerParameters(command=command[0], args=command[1:], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "rustwright-mcp"
                assert initialized.serverInfo.version == metadata.version(
                    "rustwright-mcp"
                )
                assert {tool.name for tool in (await session.list_tools()).tools} == (
                    expected_tools
                )

    asyncio.run(checks())


def test_real_stdio_less_traveled_tool_regressions() -> None:
    """Exercise the five tools/branches not covered by the original form flow."""

    async def checks() -> None:
        command = _server_command()
        params = StdioServerParameters(
            command=command[0],
            args=command[1:],
            env=dict(os.environ),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await _call(session, "browser_navigate", url=FORM_FIXTURE.as_uri())
                waited = await _call(session, "browser_wait_for", text="Order form")
                assert "- Title: Form Test" in waited
                hovered = await _call(session, "browser_hover", target="#name")
                assert "- Title: Form Test" in hovered
                await _call(session, "browser_click", target="#name")
                keyed = await _call(session, "browser_press_key", key="A")
                assert '[value="A"]' in keyed

                opened = await _call(
                    session,
                    "browser_tabs",
                    action="new",
                    url=HARDENING_FIXTURE.as_uri(),
                )
                assert "- Title: Hardening Test" in opened
                selected = await _call(
                    session, "browser_tabs", action="select", index=0
                )
                assert "- Title: Form Test" in selected
                selected = await _call(
                    session, "browser_tabs", action="select", index=1
                )
                assert "- Title: Hardening Test" in selected

                await _call(
                    session, "browser_navigate", url=FORM_FIXTURE.as_uri()
                )
                backed = await _call(session, "browser_navigate_back")
                assert "- Title: Hardening Test" in backed
                assert _result_section(await _call(session, "browser_close")) == (
                    "Browser closed."
                )

    asyncio.run(checks())
