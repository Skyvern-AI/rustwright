"""MCP server exposing Rustwright browser automation over stdio.

Tool names mirror the Playwright MCP server so agents can switch between
the two without re-learning the surface. Element targeting uses refs from
``browser_snapshot`` (``e1``, ``e2``, ...) or raw CSS selectors.

Environment variables:
    RUSTWRIGHT_MCP_HEADLESS    "0" to show the browser window (default headless)
    RUSTWRIGHT_MCP_CHANNEL     chromium channel, e.g. "chrome" (default: bundled chromium)
    RUSTWRIGHT_MCP_EXECUTABLE  explicit browser executable path (overrides channel)
    RUSTWRIGHT_MCP_CDP_ENDPOINT remote browser CDP endpoint (uses remote mode when set)
    RUSTWRIGHT_MCP_CDP_HEADERS optional JSON object of CDP connection headers
    RUSTWRIGHT_MCP_CDP_TIMEOUT_MS remote connection timeout in milliseconds (default: 60000)
    RUSTWRIGHT_MCP_ALLOW_EVAL  "1", "true", or "yes" to expose browser_evaluate
    RUSTWRIGHT_MCP_OUTPUT_DIR   root for files written by tools
    RUSTWRIGHT_MCP_WORKSPACE    allowed input root for future upload tools

When RUSTWRIGHT_MCP_CDP_ENDPOINT is set, the local headless, channel, and
executable options are ignored.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from importlib import metadata
import json
import os
import re
import threading
from typing import Any, Literal, Optional

from mcp.server.fastmcp import FastMCP

from rustwright_mcp.filepolicy import get_file_policy
from rustwright_mcp.session import SessionState
from rustwright_mcp.snapshot import SNAPSHOT_JS

PACKAGE_VERSION = metadata.version("rustwright-mcp")
mcp = FastMCP("rustwright-mcp")
# FastMCP 1.x does not expose its low-level Server's version constructor
# argument, so set the same field that initialize reads.
mcp._mcp_server.version = PACKAGE_VERSION

_state = SessionState()
# FastMCP executes sync tools on a thread pool; the browser session is
# shared state, so tool bodies are serialized.
_lock = threading.Lock()


def _serialized(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _lock:
            return fn(*args, **kwargs)

    return wrapper

SNAPSHOT_CHAR_LIMIT = 30_000
_OUTLINE_REF_PATTERN = re.compile(r"\[ref=(e[1-9][0-9]*)\]")


def _register_page_handlers(page: Any) -> None:
    _state.register_page_handlers(page)


def _page():
    """Return the active page, launching locally or attaching over remote CDP.

    In remote CDP mode, local launch options (headless, channel, and executable)
    are ignored.
    """
    if _state.page is not None:
        try:
            # The user may have closed a headed window; detect a dead
            # session and relaunch instead of failing every call.
            _state.page.evaluate("() => 1")
            _register_page_handlers(_state.page)
        except Exception:
            if _state.remote:
                _teardown()
                raise RuntimeError(
                    "Remote CDP session is no longer reachable — "
                    "reconnect/restart the MCP server."
                ) from None
            _teardown()
    if _state.page is None and _state.browser is not None and _state.context is not None:
        pages = list(_state.context.pages)
        page = pages[0] if pages else _state.context.new_page()
        _register_page_handlers(page)
        _state.page = page
        return page
    if _state.page is None:
        from rustwright.sync_api import sync_playwright

        endpoint = os.environ.get("RUSTWRIGHT_MCP_CDP_ENDPOINT")
        if endpoint:
            raw_headers = os.environ.get("RUSTWRIGHT_MCP_CDP_HEADERS", "")
            headers: dict[str, str] = {}
            if raw_headers:
                try:
                    parsed_headers = json.loads(raw_headers)
                except json.JSONDecodeError:
                    raise ValueError(
                        "RUSTWRIGHT_MCP_CDP_HEADERS must contain a valid JSON object"
                    ) from None
                if not isinstance(parsed_headers, dict) or not all(
                    isinstance(name, str) and isinstance(value, str)
                    for name, value in parsed_headers.items()
                ):
                    raise ValueError(
                        "RUSTWRIGHT_MCP_CDP_HEADERS must be a JSON object with "
                        "string keys and values"
                    )
                headers = parsed_headers

            try:
                timeout_ms = int(
                    os.environ.get("RUSTWRIGHT_MCP_CDP_TIMEOUT_MS", "60000")
                )
            except ValueError:
                raise ValueError(
                    "RUSTWRIGHT_MCP_CDP_TIMEOUT_MS must be a non-negative integer"
                ) from None
            if timeout_ms < 0:
                raise ValueError(
                    "RUSTWRIGHT_MCP_CDP_TIMEOUT_MS must be a non-negative integer"
                )

            pw = None
            browser = None
            try:
                pw = sync_playwright().start()
                browser = pw.chromium.connect_over_cdp(
                    endpoint,
                    headers=headers,
                    timeout=timeout_ms,
                )
                context = browser.contexts[0]
                pages = context.pages
                page = pages[0] if pages else context.new_page()
                _state.attach(
                    pw=pw,
                    browser=browser,
                    context=context,
                    page=page,
                    remote=True,
                )
                _register_page_handlers(page)
            except Exception:
                for close in (
                    lambda: browser.close() if browser is not None else None,
                    lambda: pw.stop() if pw is not None else None,
                ):
                    try:
                        close()
                    except Exception:
                        pass
                _state.clear()
                raise RuntimeError(
                    "Remote CDP browser is unreachable; check the connection "
                    "settings and try again."
                ) from None
            return _state.page

        headless = os.environ.get("RUSTWRIGHT_MCP_HEADLESS", "1") != "0"
        launch_kwargs: dict = {"headless": headless}
        executable = os.environ.get("RUSTWRIGHT_MCP_EXECUTABLE")
        channel = os.environ.get("RUSTWRIGHT_MCP_CHANNEL")
        if executable:
            launch_kwargs["executable_path"] = executable
        elif channel:
            launch_kwargs["channel"] = channel
        pw = sync_playwright().start()
        browser = pw.chromium.launch(**launch_kwargs)
        page = browser.new_page()
        _state.attach(
            pw=pw,
            browser=browser,
            context=page.context,
            page=page,
            remote=False,
        )
        _register_page_handlers(page)
    return _state.page


def _snapshot(page) -> str:
    try:
        # An action may have triggered a navigation; settle before reading the DOM.
        page.wait_for_load_state(timeout=10_000)
    except Exception:
        pass
    result = page.evaluate(SNAPSHOT_JS, _state.snapshot_start_ref())
    outline = result["outline"]
    header = f"Page: {page.title()}\nURL: {page.url}\n\n"
    body = outline[:SNAPSHOT_CHAR_LIMIT]
    if len(outline) > SNAPSHOT_CHAR_LIMIT:
        body += "\n- … (snapshot truncated, use browser_get_text for full content)"
    delivered_refs = set(_OUTLINE_REF_PATTERN.findall(body))
    _state.record_snapshot(
        page,
        [ref for ref in result["refs"] if ref in delivered_refs],
        result["nextRef"],
    )
    return header + body


def _teardown() -> None:
    # For remote sessions, closing the connected Browser detaches this client;
    # Rustwright leaves the remotely owned browser running.
    for close in (
        lambda: _state.browser.close(),
        lambda: _state.pw.stop(),
    ):
        try:
            close()
        except Exception:
            pass
    _state.clear()


@dataclass(frozen=True)
class ResolvedTarget:
    locator: Any
    display_name: str
    source: Literal["ref", "selector"]


_REF_PATTERN = re.compile(r"^e[1-9][0-9]*$")


def _resolve(
    page: Any,
    target: str,
    element_description: str | None = None,
) -> ResolvedTarget:
    """Resolve a current stamped ref or exactly one selector match."""
    display_name = target if element_description is None else element_description
    if _REF_PATTERN.fullmatch(target):
        snapshot_taken, snapshot_refs = _state.snapshot_status(page)
        if not snapshot_taken:
            raise ValueError("No current snapshot; call browser_snapshot first.")
        if target not in snapshot_refs:
            raise ValueError(
                f"Ref {target} is not in the current page snapshot; take a fresh snapshot."
            )
        locator = page.locator(f'[data-mcp-ref="{target}"]')
        return ResolvedTarget(locator, display_name, "ref")

    locator = page.locator(target)
    count = locator.count()
    if count == 0:
        raise ValueError(f"Target selector matched no elements: {target}")
    if count > 1:
        raise ValueError(
            f"Target selector matched {count} elements; provide a unique selector: {target}"
        )
    return ResolvedTarget(locator, display_name, "selector")


@mcp.tool()
@_serialized
def browser_navigate(url: str) -> str:
    """Navigate to a URL. Returns the page snapshot with element refs."""
    page = _page()
    page.goto(url)
    return _snapshot(page)


@mcp.tool()
@_serialized
def browser_snapshot() -> str:
    """Accessibility-style outline of the current page. Interactive elements
    carry [ref=eN] handles usable with browser_click / browser_type."""
    return _snapshot(_page())


@mcp.tool()
@_serialized
def browser_click(target: str, double_click: bool = False) -> str:
    """Click an element. `target` is a ref from the snapshot (e.g. "e12") or a
    CSS selector. Returns a fresh snapshot."""
    page = _page()
    resolved = _resolve(page, target)
    if double_click:
        resolved.locator.dblclick()
    else:
        resolved.locator.click()
    return _snapshot(page)


@mcp.tool()
@_serialized
def browser_type(target: str, text: str, submit: bool = False, clear: bool = True) -> str:
    """Type into an input. `target` is a snapshot ref or CSS selector. Set
    submit=True to press Enter afterwards. Returns a fresh snapshot."""
    page = _page()
    resolved = _resolve(page, target)
    if clear:
        resolved.locator.fill(text)
    else:
        resolved.locator.type(text)
    if submit:
        resolved.locator.press("Enter")
    return _snapshot(page)


@mcp.tool()
@_serialized
def browser_select_option(target: str, value: str) -> str:
    """Select an option in a <select> element by value or visible label."""
    page = _page()
    resolved = _resolve(page, target)
    try:
        resolved.locator.select_option(value=value)
    except Exception:
        resolved.locator.select_option(label=value)
    return _snapshot(page)


@mcp.tool()
@_serialized
def browser_hover(target: str) -> str:
    """Hover over an element identified by snapshot ref or CSS selector."""
    page = _page()
    _resolve(page, target).locator.hover()
    return _snapshot(page)


@mcp.tool()
@_serialized
def browser_press_key(key: str) -> str:
    """Press a keyboard key (e.g. "Enter", "Escape", "ArrowDown") on the page."""
    page = _page()
    page.keyboard.press(key)
    return _snapshot(page)


@mcp.tool()
@_serialized
def browser_navigate_back() -> str:
    """Go back in browser history. Returns a fresh snapshot."""
    page = _page()
    page.go_back()
    return _snapshot(page)


@mcp.tool()
@_serialized
def browser_reload() -> str:
    """Reload the active page. Returns a fresh snapshot."""
    page = _page()
    page.reload()
    return _snapshot(page)


@mcp.tool()
@_serialized
def browser_tabs(
    action: str, index: int | None = None, url: str | None = None
) -> str:
    """List, open, select, or close browser tabs.

    `action` is one of "list", "new", "select", or "close". `index` is
    required when selecting or closing a tab; `url` is optional for new tabs.
    """
    page = _page()
    context = page.context
    pages = list(context.pages)
    action = action.lower()

    if action == "list":
        return "\n".join(
            f"{i}: {tab.title()} — {tab.url}" for i, tab in enumerate(pages)
        )
    if action == "new":
        page = context.new_page()
        _register_page_handlers(page)
        _state.page = page
        if url:
            page.goto(url)
        return _snapshot(page)
    if action not in {"select", "close"}:
        raise ValueError('action must be one of "list", "new", "select", or "close"')
    if index is None or index < 0 or index >= len(pages):
        raise ValueError(f"Invalid tab index {index}; expected 0 through {len(pages) - 1}")

    if action == "select":
        page = pages[index]
        _register_page_handlers(page)
        _state.page = page
        page.bring_to_front()
        return _snapshot(page)

    closing = pages[index]
    was_active = closing is page
    closing.close()
    remaining = list(context.pages)
    if not remaining:
        page = context.new_page()
    elif was_active:
        page = remaining[min(index, len(remaining) - 1)]
    _register_page_handlers(page)
    _state.page = page
    return _snapshot(page)


@mcp.tool()
@_serialized
def browser_handle_dialog(accept: bool, prompt_text: str | None = None) -> str:
    """Accept or dismiss the next JavaScript dialog on the active page.

    A dialog opened by a brand-new popup may appear before its page can be
    registered. The policy is consumed once; unarmed dialogs remain pending.
    """
    page = _page()
    _state.arm_dialog(page, accept, prompt_text)
    action = "accepted" if accept else "dismissed"
    return f"The next dialog on the active page will be {action}."


@mcp.tool()
@_serialized
def browser_wait_for(text: Optional[str] = None, timeout_ms: float = 10_000) -> str:
    """Wait for text to appear on the page (or for load state when no text
    is given), then return a snapshot."""
    page = _page()
    if text:
        page.wait_for_selector(f"text={text}", timeout=timeout_ms)
    else:
        page.wait_for_load_state(timeout=timeout_ms)
    return _snapshot(page)


@mcp.tool()
@_serialized
def browser_get_text(selector: str = "body", max_chars: int = 20_000) -> str:
    """Visible text content of a CSS selector (defaults to the whole page)."""
    page = _page()
    text = _resolve(page, selector).locator.inner_text() or ""
    return text[:max_chars]


def browser_evaluate(expression: str) -> str:
    """Run JavaScript in the page. Use an arrow function, e.g.
    "() => document.title". Returns the JSON-ish result as a string."""
    return str(_page().evaluate(expression))


def _allow_eval() -> bool:
    return os.environ.get("RUSTWRIGHT_MCP_ALLOW_EVAL", "").lower() in {
        "1",
        "true",
        "yes",
    }


if _allow_eval():
    mcp.tool()(_serialized(browser_evaluate))


@mcp.tool()
@_serialized
def browser_take_screenshot(path: Optional[str] = None, full_page: bool = False) -> str:
    """Save a PNG under the configured output root and return its artifact path."""
    policy = get_file_policy()
    output_path = policy.reserve_output(path, purpose="screenshot")
    try:
        _page().screenshot(path=str(output_path), full_page=full_page)
        return policy.finalize_output(output_path)
    except Exception:
        policy.discard_output(output_path)
        raise


@mcp.tool()
@_serialized
def browser_close() -> str:
    """Close the browser. The next tool call starts a fresh session."""
    if _state.browser is not None:
        _teardown()
        return "Browser closed."
    return "No browser session was open."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
