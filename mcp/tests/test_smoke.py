"""Smoke test for the Rustwright MCP server.

Speaks the real MCP stdio protocol against a local HTML fixture, so no
network access is required. Skips when no Chromium is available; set
RUSTWRIGHT_MCP_CHANNEL=chrome to use an installed Chrome.
"""

import asyncio
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "form.html"


def _server_command() -> list[str]:
    exe = shutil.which("rustwright-mcp")
    if exe:
        return [exe]
    return [sys.executable, "-m", "rustwright_mcp"]


async def _run_session(checks, env_overrides=None) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    command = _server_command()
    env = dict(os.environ)
    for name in (
        "RUSTWRIGHT_MCP_ALLOW_EVAL",
        "RUSTWRIGHT_MCP_CDP_ENDPOINT",
        "RUSTWRIGHT_MCP_CDP_HEADERS",
        "RUSTWRIGHT_MCP_CDP_TIMEOUT_MS",
    ):
        env.pop(name, None)
    if env_overrides:
        env.update(env_overrides)
    params = StdioServerParameters(
        command=command[0], args=command[1:], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await checks(session)


async def _call(session, name, **kwargs) -> str:
    result = await session.call_tool(name, kwargs)
    text = "\n".join(c.text for c in result.content if c.type == "text")
    if result.isError:
        if "Failed to launch chromium" in text:
            pytest.skip("no Chromium available for MCP smoke test")
        raise AssertionError(f"{name} failed: {text}")
    return text


def _result_section(response: str) -> str:
    marker = "### Result\n"
    if marker not in response:
        return ""
    return response.split(marker, 1)[1].split("\n\n### ", 1)[0]


def test_stdio_form_flow():
    async def checks(session):
        tools = {t.name for t in (await session.list_tools()).tools}
        assert {"browser_navigate", "browser_snapshot", "browser_click",
                "browser_type", "browser_close"} <= tools

        snap = await _call(session, "browser_navigate", url=FIXTURE.as_uri())
        assert "### Page" in snap
        assert "- Title: Form Test" in snap
        assert "### Snapshot" in snap
        name_ref = re.search(r'textbox "Customer name"[^\[]*\[ref=(e\d+)\]', snap).group(1)

        snap = await _call(
            session, "browser_type", target=name_ref, text="Rustwright Test"
        )
        size_ref = re.search(r'combobox "Size" \[ref=(e\d+)\]', snap).group(1)
        snap = await _call(
            session, "browser_select_option", target=size_ref, value="Large"
        )
        btn_ref = re.search(
            r'button "Place order"[^\[]*\[ref=(e\d+)\]', snap
        ).group(1)
        await _call(session, "browser_click", target=btn_ref)

        out = await _call(session, "browser_get_text", selector="#out")
        assert _result_section(out) == "name=Rustwright Test;size=l"

        assert _result_section(await _call(session, "browser_close")) == "Browser closed."

    asyncio.run(_run_session(checks))


def test_stdio_evaluate_opt_in():
    async def checks(session):
        tools = {t.name for t in (await session.list_tools()).tools}
        assert "browser_evaluate" in tools

        await _call(session, "browser_navigate", url=FIXTURE.as_uri())
        title = await _call(session, "browser_evaluate", expression="() => document.title")
        assert _result_section(title) == '"Form Test"'
        assert "### Snapshot" in title

        assert _result_section(await _call(session, "browser_close")) == "Browser closed."

    asyncio.run(
        _run_session(checks, {"RUSTWRIGHT_MCP_ALLOW_EVAL": "1"})
    )
