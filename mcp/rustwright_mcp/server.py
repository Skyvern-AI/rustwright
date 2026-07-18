"""MCP server exposing Rustwright browser automation over stdio.

Tool names mirror the de-facto standard ``browser_*`` toolset so agents can
switch without re-learning the surface. Element targeting uses refs from
``browser_snapshot`` (``e1``, ``e2``, ...) or raw CSS selectors.

Environment variables:
    RUSTWRIGHT_MCP_HEADLESS    "0" to show the browser window (default headless)
    RUSTWRIGHT_MCP_CHANNEL     chromium channel, e.g. "chrome" (default: bundled chromium)
    RUSTWRIGHT_MCP_EXECUTABLE  explicit browser executable path (overrides channel)
    RUSTWRIGHT_MCP_CDP_ENDPOINT remote browser CDP endpoint (uses remote mode when set)
    RUSTWRIGHT_MCP_CDP_HEADERS optional JSON object of CDP connection headers
    RUSTWRIGHT_MCP_CDP_TIMEOUT_MS remote connection timeout in milliseconds (default: 60000)
    RUSTWRIGHT_MCP_ALLOW_EVAL  explicit falsy value disables browser_evaluate
    RUSTWRIGHT_MCP_CAPS        accepted comma-separated capability groups
    RUSTWRIGHT_MCP_TOOLSET     "mirror" (default) or the smaller "lean" profile
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
import sys
import threading
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import (
    AliasChoices,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)

from rustwright_mcp.filepolicy import get_file_policy
from rustwright_mcp.session import SessionState
from rustwright_mcp.snapshot import SNAPSHOT_JS, TARGET_SNAPSHOT_JS

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


_FALSE_VALUES = {"0", "false", "no", "off"}
_LEAN_TOOLS = {
    "browser_navigate",
    "browser_navigate_back",
    "browser_reload",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_select_option",
    "browser_hover",
    "browser_press_key",
    "browser_wait_for",
    "browser_tabs",
    "browser_take_screenshot",
    "browser_close",
    # Evaluation is controlled only by RUSTWRIGHT_MCP_ALLOW_EVAL. Profiles
    # must not quietly change that security setting.
    "browser_evaluate",
}


def _allow_eval() -> bool:
    raw = os.environ.get("RUSTWRIGHT_MCP_ALLOW_EVAL")
    return raw is None or raw.strip().lower() not in _FALSE_VALUES


def _toolset_profile() -> Literal["mirror", "lean"]:
    raw = os.environ.get("RUSTWRIGHT_MCP_TOOLSET", "mirror").strip().lower()
    if raw in {"mirror", "lean"}:
        return raw
    print(
        f"warning: unknown RUSTWRIGHT_MCP_TOOLSET={raw!r}; using 'mirror'",
        file=sys.stderr,
    )
    return "mirror"


_TOOLSET_PROFILE = _toolset_profile()


def _tool():
    """Register a serialized tool when it belongs to the active profile."""

    def decorator(fn):
        wrapped = _serialized(fn)
        in_profile = _TOOLSET_PROFILE == "mirror" or fn.__name__ in _LEAN_TOOLS
        eval_allowed = fn.__name__ != "browser_evaluate" or _allow_eval()
        if in_profile and eval_allowed:
            mcp.tool()(wrapped)
            registered = mcp._tool_manager.get_tool(fn.__name__)
            if registered is None:  # pragma: no cover - registration is synchronous
                raise RuntimeError(f"Tool registration failed for {fn.__name__}")

            generated_model = registered.fn_metadata.arg_model

            class StrictArguments(generated_model):
                model_config = ConfigDict(
                    arbitrary_types_allowed=True,
                    extra="forbid",
                )

                @model_validator(mode="before")
                @classmethod
                def canonical_alias_wins(cls, data: Any) -> Any:
                    """Discard only a legacy alias shadowed by its canonical key.

                    Pydantic consumes the first ``AliasChoices`` entry, but with
                    forbidden extras a simultaneously supplied legacy spelling
                    would otherwise remain as an unknown key. Normalize that
                    conflict before extra checking; a legacy spelling supplied on
                    its own is still consumed through ``validation_alias``.
                    """
                    if not isinstance(data, dict):
                        return data
                    normalized = data.copy()
                    for field in cls.model_fields.values():
                        validation_alias = field.validation_alias
                        if not isinstance(validation_alias, AliasChoices):
                            continue
                        canonical, *legacy_aliases = validation_alias.choices
                        if (
                            not isinstance(canonical, str)
                            or canonical not in normalized
                        ):
                            continue
                        for legacy_alias in legacy_aliases:
                            if isinstance(legacy_alias, str):
                                normalized.pop(legacy_alias, None)
                    return normalized

            # Preserve the generated model's stable schema title while replacing
            # it with the strict subclass used for both validation and advertising.
            StrictArguments.__name__ = generated_model.__name__
            StrictArguments.__qualname__ = generated_model.__qualname__
            registered.fn_metadata.arg_model = StrictArguments
            registered.parameters = StrictArguments.model_json_schema(by_alias=True)
        return wrapped

    return decorator


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


def _snapshot(
    page: Any,
    *,
    target: Any | None = None,
    depth: float | None = None,
    boxes: bool = False,
) -> str:
    if depth is not None and depth < 0:
        raise ValueError("depth must be non-negative")
    try:
        # An action may have triggered a navigation; settle before reading the DOM.
        page.wait_for_load_state(timeout=10_000)
    except Exception:
        pass
    options = {
        "startRef": _state.snapshot_start_ref(),
        "maxDepth": depth,
        "boxes": boxes,
    }
    result = (
        target.evaluate(TARGET_SNAPSHOT_JS, options)
        if target is not None
        else page.evaluate(SNAPSHOT_JS, options)
    )
    outline = result["outline"]
    body = outline[:SNAPSHOT_CHAR_LIMIT]
    if len(outline) > SNAPSHOT_CHAR_LIMIT:
        body += "\n- … (snapshot truncated; use a targeted snapshot for more detail)"
    delivered_refs = set(_OUTLINE_REF_PATTERN.findall(body))
    _state.record_snapshot(
        page,
        [ref for ref in result["refs"] if ref in delivered_refs],
        result["nextRef"],
    )
    return body


def _page_details(page: Any) -> tuple[str, str, int, list[Any]]:
    try:
        title = str(page.title())
    except Exception:
        title = "(unavailable)"
    try:
        url = str(page.url)
    except Exception:
        url = "(unavailable)"
    try:
        pages = list(page.context.pages)
    except Exception:
        pages = [page]
    try:
        active_index = next(index for index, tab in enumerate(pages) if tab is page)
    except StopIteration:
        active_index = -1
    return title, url, active_index, pages


def _render_response(
    result: str | None = None,
    *,
    page: Any | None = None,
    snapshot: str | None = None,
    include_tabs: bool = False,
) -> str:
    """Render every successful tool result with deterministic section order."""
    sections: list[str] = []
    details: tuple[str, str, int, list[Any]] | None = None
    if page is not None:
        details = _page_details(page)
    if result is not None:
        sections.append(f"### Result\n{result}")
    if details is not None:
        title, url, active_index, _ = details
        sections.append(
            "### Page\n"
            f"- URL: {url}\n"
            f"- Title: {title}\n"
            f"- Active tab: {active_index}"
        )
    if include_tabs:
        if details is None:
            raise ValueError("tab rendering requires an active page")
        _, _, active_index, pages = details
        lines = []
        for index, tab in enumerate(pages):
            marker = " (active)" if index == active_index else ""
            lines.append(f"- {index}: {tab.title()} — {tab.url}{marker}")
        sections.append("### Tabs\n" + ("\n".join(lines) or "- (none)"))
    if snapshot is not None:
        sections.append(f"### Snapshot\n{snapshot}")
    return "\n\n".join(sections)


def _write_text_output(content: str, filename: str, *, purpose: str) -> str:
    policy = get_file_policy()
    output_path = policy.reserve_output(filename, purpose=purpose)
    try:
        flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(output_path, flags)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
                handle.write(content)
        finally:
            os.close(descriptor)
        return policy.finalize_output(output_path)
    except Exception:
        policy.discard_output(output_path)
        raise


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


def _scalar_to_array(value: Any) -> Any:
    if value is None or isinstance(value, list):
        return value
    return [value]


DoubleClick = Annotated[
    bool,
    Field(validation_alias=AliasChoices("doubleClick", "double_click")),
]
Modifiers = Annotated[
    list[Literal["Alt", "Control", "ControlOrMeta", "Meta", "Shift"]],
    BeforeValidator(_scalar_to_array),
]
Values = Annotated[
    list[str],
    BeforeValidator(_scalar_to_array),
    Field(validation_alias=AliasChoices("values", "value")),
]
PromptText = Annotated[
    str | None,
    Field(validation_alias=AliasChoices("promptText", "prompt_text")),
]
TextGone = Annotated[
    str | None,
    Field(validation_alias=AliasChoices("textGone", "text_gone")),
]
Filename = Annotated[
    str | None,
    Field(validation_alias=AliasChoices("filename", "path")),
]
FullPage = Annotated[
    bool,
    Field(validation_alias=AliasChoices("fullPage", "full_page")),
]
Function = Annotated[
    str,
    Field(validation_alias=AliasChoices("function", "expression")),
]


@_tool()
def browser_navigate(url: str) -> str:
    """Navigate to a URL and return a fresh snapshot."""
    page = _page()
    page.goto(url)
    snapshot = _snapshot(page)
    return _render_response(f"Navigated to {url}.", page=page, snapshot=snapshot)


@_tool()
def browser_snapshot(
    target: str | None = None,
    filename: str | None = None,
    depth: float | None = None,
    boxes: bool = False,
) -> str:
    """Return a full or targeted accessibility outline with current refs.

    ``depth`` bounds tree traversal, ``boxes`` adds viewport-relative CSS-pixel
    metadata, and ``filename`` writes the snapshot through the output policy.
    """
    page = _page()
    resolved = None if target is None else _resolve(page, target)
    snapshot = _snapshot(
        page,
        target=None if resolved is None else resolved.locator,
        depth=depth,
        boxes=boxes,
    )
    if filename is not None:
        artifact = _write_text_output(snapshot, filename, purpose="snapshot")
        return _render_response(
            f"Snapshot written to `{artifact}`.",
            page=page,
        )
    return _render_response(page=page, snapshot=snapshot)


@_tool()
def browser_click(
    target: str,
    element: str | None = None,
    doubleClick: DoubleClick = False,
    button: Literal["left", "right", "middle"] = "left",
    modifiers: Modifiers | None = None,
) -> str:
    """Click a unique target. ``element`` is only a human-readable description."""
    page = _page()
    resolved = _resolve(page, target, element)
    resolved.locator.click(
        click_count=2 if doubleClick else 1,
        button=button,
        modifiers=modifiers,
    )
    snapshot = _snapshot(page)
    return _render_response(
        f"Clicked {resolved.display_name}.", page=page, snapshot=snapshot
    )


@_tool()
def browser_type(
    target: str,
    text: str,
    element: str | None = None,
    submit: bool = False,
    slowly: bool = False,
    clear: bool = True,
) -> str:
    """Enter text into a target and optionally submit.

    ``slowly`` types with a character delay. ``clear`` is a Rustwright
    extension that independently controls replacement versus append behavior.
    """
    page = _page()
    resolved = _resolve(page, target, element)
    locator = resolved.locator
    if clear and not slowly:
        locator.fill(text)
    else:
        if clear:
            locator.fill("")
        if slowly:
            locator.press_sequentially(text, delay=50)
        else:
            locator.type(text)
    if submit:
        locator.press("Enter")
    snapshot = _snapshot(page)
    return _render_response(
        f"Entered text in {resolved.display_name}.", page=page, snapshot=snapshot
    )


@_tool()
def browser_select_option(
    target: str,
    values: Values,
    element: str | None = None,
) -> str:
    """Select one or more values; legacy singular ``value`` is accepted."""
    page = _page()
    resolved = _resolve(page, target, element)
    try:
        resolved.locator.select_option(value=values)
    except Exception:
        resolved.locator.select_option(label=values)
    snapshot = _snapshot(page)
    return _render_response(
        f"Selected {json.dumps(values, ensure_ascii=False)} in {resolved.display_name}.",
        page=page,
        snapshot=snapshot,
    )


@_tool()
def browser_hover(target: str, element: str | None = None) -> str:
    """Hover a unique target. ``element`` is only a human description."""
    page = _page()
    resolved = _resolve(page, target, element)
    resolved.locator.hover()
    snapshot = _snapshot(page)
    return _render_response(
        f"Hovered {resolved.display_name}.", page=page, snapshot=snapshot
    )


@_tool()
def browser_press_key(key: str) -> str:
    """Press a browser key or character on the active page."""
    page = _page()
    page.keyboard.press(key)
    snapshot = _snapshot(page)
    return _render_response(f"Pressed {key}.", page=page, snapshot=snapshot)


@_tool()
def browser_navigate_back() -> str:
    """Go back in browser history and return a fresh snapshot."""
    page = _page()
    page.go_back()
    snapshot = _snapshot(page)
    return _render_response("Navigated back.", page=page, snapshot=snapshot)


@_tool()
def browser_reload() -> str:
    """Reload the active page and return a fresh snapshot."""
    page = _page()
    page.reload()
    snapshot = _snapshot(page)
    return _render_response("Reloaded the active page.", page=page, snapshot=snapshot)


@_tool()
def browser_tabs(
    action: Literal["list", "new", "close", "select"],
    index: int | None = None,
    url: str | None = None,
) -> str:
    """List, open, select, or close tabs. Every action returns the tab list."""
    page = _page()
    context = page.context
    pages = list(context.pages)
    snapshot = None

    if action == "new":
        page = context.new_page()
        _register_page_handlers(page)
        _state.page = page
        if url is not None:
            page.goto(url)
        snapshot = _snapshot(page)
    elif action == "select":
        if index is None or index < 0 or index >= len(pages):
            raise ValueError(
                f"Invalid tab index {index}; expected 0 through {len(pages) - 1}"
            )
        page = pages[index]
        _register_page_handlers(page)
        _state.page = page
        page.bring_to_front()
        snapshot = _snapshot(page)
    elif action == "close":
        if index is None:
            closing = page
            closing_index = next(
                (position for position, tab in enumerate(pages) if tab is page), 0
            )
        else:
            if index < 0 or index >= len(pages):
                raise ValueError(
                    f"Invalid tab index {index}; expected 0 through {len(pages) - 1}"
                )
            closing = pages[index]
            closing_index = index
        was_active = closing is page
        closing.close()
        remaining = list(context.pages)
        if not remaining:
            page = context.new_page()
        elif was_active:
            page = remaining[min(closing_index, len(remaining) - 1)]
        _register_page_handlers(page)
        _state.page = page
        snapshot = _snapshot(page)

    return _render_response(
        f"Tab action `{action}` completed.",
        page=page,
        snapshot=snapshot,
        include_tabs=True,
    )


@_tool()
def browser_handle_dialog(accept: bool, promptText: PromptText = None) -> str:
    """Arm the one-shot policy for the next JavaScript dialog."""
    page = _page()
    _state.arm_dialog(page, accept, promptText)
    action = "accepted" if accept else "dismissed"
    return _render_response(
        f"The next dialog on the active page will be {action}.", page=page
    )


@_tool()
def browser_wait_for(
    time: float | None = None,
    text: str | None = None,
    textGone: TextGone = None,
    timeout_ms: float = 10_000,
) -> str:
    """Wait for time and/or text state, then return one fresh snapshot.

    ``time`` is seconds and is capped at 30. ``timeout_ms`` is a Rustwright
    extension controlling the visible/hidden text waits.
    """
    if time is None and text is None and textGone is None:
        raise ValueError("At least one of time, text, or textGone is required.")
    if time is not None and time < 0:
        raise ValueError("time must be non-negative")
    if timeout_ms < 0:
        raise ValueError("timeout_ms must be non-negative")
    page = _page()
    if time is not None:
        page.wait_for_timeout(min(time, 30) * 1000)
    if text is not None:
        page.get_by_text(text).wait_for(state="visible", timeout=timeout_ms)
    if textGone is not None:
        page.get_by_text(textGone).wait_for(state="hidden", timeout=timeout_ms)
    snapshot = _snapshot(page)
    return _render_response("Wait completed.", page=page, snapshot=snapshot)


@_tool()
def browser_get_text(selector: str = "body", max_chars: int = 20_000) -> str:
    """Return visible text for a unique selector (mirror profile only)."""
    page = _page()
    text = _resolve(page, selector).locator.inner_text() or ""
    return _render_response(text[:max_chars], page=page)


@_tool()
def browser_evaluate(
    function: Function,
    element: str | None = None,
    target: str | None = None,
    filename: str | None = None,
) -> str:
    """Evaluate a function in page or unique-element context and return JSON."""
    if element is not None and target is None:
        raise ValueError("element requires target for browser_evaluate")
    page = _page()
    if target is None:
        evaluated = page.evaluate(function)
    else:
        resolved = _resolve(page, target, element)
        evaluated = resolved.locator.evaluate(function)
    serialized = json.dumps(evaluated, ensure_ascii=False, default=str)
    result = serialized
    if filename is not None:
        artifact = _write_text_output(serialized, filename, purpose="evaluate")
        result += f"\n\nSaved to: `{artifact}`"
    snapshot = _snapshot(page)
    return _render_response(result, page=page, snapshot=snapshot)


def _supports_screenshot_scale(screenshot_target: Any) -> bool:
    import inspect

    try:
        return "scale" in inspect.signature(screenshot_target.screenshot).parameters
    except (TypeError, ValueError):
        return False


@_tool()
def browser_take_screenshot(
    element: str | None = None,
    target: str | None = None,
    type: Literal["png", "jpeg"] = "png",
    filename: Filename = None,
    fullPage: FullPage = False,
    scale: Literal["css", "device"] = "css",
) -> str:
    """Save a page or element screenshot through the confined output policy."""
    if element is not None and target is None:
        raise ValueError("element requires target for browser_take_screenshot")
    if fullPage and target is not None:
        raise ValueError("fullPage and an element target are mutually exclusive")

    page = _page()
    resolved = None if target is None else _resolve(page, target, element)
    screenshot_target = page if resolved is None else resolved.locator
    supports_scale = _supports_screenshot_scale(screenshot_target)
    if scale == "device" and not supports_scale:
        raise ValueError(
            "scale=device is unsupported by this Rustwright screenshot API"
        )

    policy = get_file_policy()
    output_path = policy.reserve_output(
        filename,
        purpose="screenshot",
        suffix=f".{type}",
    )
    kwargs: dict[str, Any] = {"path": str(output_path), "type": type}
    if supports_scale:
        kwargs["scale"] = scale
    if resolved is None:
        kwargs["full_page"] = fullPage
    try:
        screenshot_target.screenshot(**kwargs)
        artifact = policy.finalize_output(output_path)
    except Exception:
        policy.discard_output(output_path)
        raise
    return _render_response(f"Screenshot written to `{artifact}`.", page=page)


@_tool()
def browser_close() -> str:
    """Close the browser. The next browser tool starts a fresh session."""
    if _state.browser is not None:
        _teardown()
        return _render_response("Browser closed.")
    return _render_response("No browser session was open.")


def _configured_caps(
    argv: list[str] | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Return requested capability groups; environment takes precedence."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    environment = os.environ if environ is None else environ
    if "RUSTWRIGHT_MCP_CAPS" in environment:
        raw_groups = environment["RUSTWRIGHT_MCP_CAPS"]
    else:
        raw_groups = ",".join(
            argument.removeprefix("--caps=")
            for argument in arguments
            if argument.startswith("--caps=")
        )
    groups = []
    for group in raw_groups.split(","):
        normalized = group.strip().lower()
        if normalized and normalized not in groups:
            groups.append(normalized)
    return tuple(groups)


def _warn_ignored_caps(groups: tuple[str, ...]) -> None:
    for group in groups:
        print(
            f"warning: capability group {group!r} is not implemented and will be ignored",
            file=sys.stderr,
        )


_eval_warning_emitted = False


def _warn_eval_enabled() -> None:
    global _eval_warning_emitted
    if _allow_eval() and not _eval_warning_emitted:
        print(
            "warning: browser_evaluate is enabled; set "
            "RUSTWRIGHT_MCP_ALLOW_EVAL=0 to disable page-world evaluation",
            file=sys.stderr,
        )
        _eval_warning_emitted = True


def main() -> None:
    _warn_ignored_caps(_configured_caps())
    _warn_eval_enabled()
    # FastMCP does not consume capability flags. Strip only the accepted form
    # so future argument parsing cannot reject a compatibility-only option.
    sys.argv[:] = [
        sys.argv[0],
        *[arg for arg in sys.argv[1:] if not arg.startswith("--caps=")],
    ]
    mcp.run()


if __name__ == "__main__":
    main()
