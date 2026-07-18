"""Real stdio tests for remote CDP browser sessions."""

import asyncio
from pathlib import Path

from rustwright.sync_api import sync_playwright

from test_smoke import _call, _result_section, _run_session

FIXTURE = Path(__file__).parent / "fixtures" / "form.html"


def test_stdio_connects_over_cdp():
    with sync_playwright() as playwright:
        remote_browser = playwright.chromium.launch(
            headless=True,
            args=["--remote-debugging-port=0"],
        )
        endpoint = remote_browser._ws_endpoint

        async def checks(session):
            snap = await _call(session, "browser_navigate", url=FIXTURE.as_uri())
            assert "- Title: Form Test" in snap
            assert "### Snapshot" in snap
            assert "Customer name" in snap

            snap = await _call(session, "browser_snapshot")
            assert "- Title: Form Test" in snap
            assert "Place order" in snap

            assert _result_section(await _call(session, "browser_close")) == (
                "Browser closed."
            )

        try:
            asyncio.run(
                _run_session(
                    checks,
                    {
                        "RUSTWRIGHT_MCP_CDP_ENDPOINT": endpoint,
                        "RUSTWRIGHT_MCP_CDP_HEADERS": '{"x-test":"rustwright-mcp"}',
                        "RUSTWRIGHT_MCP_CDP_TIMEOUT_MS": "10000",
                    },
                )
            )
            assert remote_browser.is_connected()
        finally:
            remote_browser.close()


def test_dead_cdp_endpoint_fails_without_local_fallback():
    endpoint = "ws://127.0.0.1:1/devtools/browser/nope"
    header_value = "not-a-real-secret"

    async def checks(session):
        result = await session.call_tool("browser_snapshot", {})
        text = "\n".join(c.text for c in result.content if c.type == "text")

        assert result.isError
        assert "Remote CDP browser is unreachable" in text
        assert "Failed to launch chromium" not in text
        assert "Page:" not in text
        assert endpoint not in text
        assert header_value not in text

    asyncio.run(
        _run_session(
            checks,
            {
                "RUSTWRIGHT_MCP_CDP_ENDPOINT": endpoint,
                "RUSTWRIGHT_MCP_CDP_HEADERS": (
                    '{"Authorization":"' + header_value + '"}'
                ),
                "RUSTWRIGHT_MCP_CDP_TIMEOUT_MS": "1000",
                "RUSTWRIGHT_MCP_EXECUTABLE": "/not/a/browser",
            },
        )
    )
