"""Focused stdio tests for MCP server hardening."""

import asyncio
import re
from pathlib import Path

from test_smoke import _call, _result_section, _run_session

FIXTURE = Path(__file__).parent / "fixtures" / "hardening.html"


def test_password_value_is_masked():
    async def checks(session):
        snap = await _call(session, "browser_navigate", url=FIXTURE.as_uri())
        assert "SECRET_PW" not in snap
        assert "[value=••••••]" in snap
        await _call(session, "browser_close")

    asyncio.run(_run_session(checks))


def test_snapshot_refs_are_never_reused():
    async def checks(session):
        first = await _call(session, "browser_navigate", url=FIXTURE.as_uri())
        old_ref = re.search(r'button "Continue" \[ref=(e\d+)\]', first).group(1)

        second = await _call(session, "browser_snapshot")
        new_ref = re.search(r'button "Continue" \[ref=(e\d+)\]', second).group(1)
        assert int(new_ref[1:]) > int(old_ref[1:])

        result = await session.call_tool("browser_click", {"target": old_ref})
        text = "\n".join(c.text for c in result.content if c.type == "text")
        assert result.isError
        assert (
            "Ref " + old_ref
            + " is not in the current page snapshot; take a fresh snapshot."
        ) in text
        await _call(session, "browser_close")

    asyncio.run(_run_session(checks))


def test_browser_evaluate_is_default_on_and_explicitly_disableable():
    async def check_default(session):
        tools = {t.name for t in (await session.list_tools()).tools}
        assert "browser_evaluate" in tools

    async def check_disabled(session):
        tools = {t.name for t in (await session.list_tools()).tools}
        assert "browser_evaluate" not in tools

    asyncio.run(_run_session(check_default))
    asyncio.run(
        _run_session(check_disabled, {"RUSTWRIGHT_MCP_ALLOW_EVAL": "0"})
    )


def test_browser_reload_returns_snapshot():
    async def checks(session):
        await _call(session, "browser_navigate", url=FIXTURE.as_uri())
        snap = await _call(session, "browser_reload")
        assert "- Title: Hardening Test" in snap
        assert "### Snapshot" in snap
        assert "SECRET_PW" not in snap
        await _call(session, "browser_close")

    asyncio.run(_run_session(checks))


def test_browser_tabs_and_dialog_policy():
    async def checks(session):
        await _call(session, "browser_navigate", url=FIXTURE.as_uri())

        tabs = await _call(session, "browser_tabs", action="list")
        assert "0: Hardening Test" in tabs
        snap = await _call(
            session, "browser_tabs", action="new", url=FIXTURE.as_uri()
        )
        assert "- Title: Hardening Test" in snap
        assert "### Tabs" in snap
        tabs = await _call(session, "browser_tabs", action="list")
        assert "1: Hardening Test" in tabs
        await _call(session, "browser_tabs", action="close", index=1)

        pending = await _call(session, "browser_click", target="#prompt")
        assert "### Modal" in pending
        confirmation = await _call(
            session,
            "browser_handle_dialog",
            accept=True,
            prompt_text="Rustwright",
        )
        assert _result_section(confirmation) == "Accepted the pending dialog."
        assert _result_section(
            await _call(session, "browser_get_text", selector="#out")
        ) == "Rustwright"
        await _call(session, "browser_close")

    asyncio.run(_run_session(checks))
