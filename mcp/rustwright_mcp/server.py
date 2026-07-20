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
    RUSTWRIGHT_MCP_ALLOW_EVAL  true/false toggle for browser_evaluate; unknown values fail startup
    RUSTWRIGHT_MCP_CAPS        accepted comma-separated capability groups
    RUSTWRIGHT_MCP_TOOLSET     "mirror" (default) or the smaller "lean" profile
    RUSTWRIGHT_MCP_OUTPUT_DIR   root for files written by tools
    RUSTWRIGHT_MCP_WORKSPACE    allowed absolute input root for upload tools

When RUSTWRIGHT_MCP_CDP_ENDPOINT is set, the local headless, channel, and
executable options are ignored.
"""

from __future__ import annotations

import base64
import functools
from dataclasses import dataclass
from importlib import metadata
import json
import math
import mimetypes
import os
import re
import sys
import threading
from typing import Annotated, Any, Literal
import uuid

from typing_extensions import NotRequired, TypedDict

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
            if (
                fn.__name__ not in _MODAL_SAFE_TOOLS
                and _state.page is not None
                and _has_pending_modal()
            ):
                return _render_response(
                    f"{fn.__name__} deferred until the pending modal is handled.",
                    page=_state.page,
                )
            return fn(*args, **kwargs)

    return wrapper


_FALSE_VALUES = {"0", "false", "no", "off"}
_TRUE_VALUES = {"1", "true", "yes", "on"}
_MODAL_SAFE_TOOLS = {
    "browser_handle_dialog",
    "browser_file_upload",
    "browser_console_messages",
    "browser_network_requests",
    "browser_network_request",
    "browser_tabs",
    "browser_close",
}
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
    # PR-3 intentionally adds only viewport resize to the lean profile.
    "browser_resize",
    # Evaluation is controlled only by RUSTWRIGHT_MCP_ALLOW_EVAL. Profiles
    # must not quietly change that security setting.
    "browser_evaluate",
}


def _allow_eval() -> bool:
    raw = os.environ.get("RUSTWRIGHT_MCP_ALLOW_EVAL")
    if raw is None:
        return True
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    accepted = ", ".join(sorted(_TRUE_VALUES | _FALSE_VALUES))
    raise ValueError(
        "RUSTWRIGHT_MCP_ALLOW_EVAL must be one of "
        f"{accepted}; got {raw!r}"
    )


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
NETWORK_BODY_INLINE_BYTES = 64 * 1024
FIND_MATCH_LIMIT = 20
_OUTLINE_REF_PATTERN = re.compile(r"\[ref=(e[1-9][0-9]*)\]")
_CONSOLE_LEVEL_RANK = {
    "error": 0,
    "warning": 1,
    "warn": 1,
    "info": 2,
    "log": 2,
    "debug": 3,
}


def _register_page_handlers(page: Any) -> None:
    _state.download_saver = _save_download
    _state.register_page_handlers(page)
    _capture_page_title(page)


def _pending_modals(page: Any) -> tuple[Any | None, Any | None]:
    return _state.pending_modals(page)


def _context_pages(reference_page: Any | None = None) -> list[Any]:
    page = reference_page if reference_page is not None else _state.page
    try:
        pages = list(page.context.pages) if page is not None else []
    except Exception:
        pages = []
    for registered_page, _, _ in _state.pending_modal_pages():
        if not any(candidate is registered_page for candidate in pages):
            pages.append(registered_page)
    return pages


def _pending_modal_entries(
    reference_page: Any | None = None,
) -> list[tuple[int, Any, Any | None, Any | None]]:
    """Return every pending modal ordered by its owning tab index."""
    pages = _context_pages(reference_page)
    entries: list[tuple[int, Any, Any | None, Any | None]] = []
    for owner, dialog, chooser in _state.pending_modal_pages():
        index = next(
            (position for position, tab in enumerate(pages) if tab is owner), -1
        )
        entries.append((index, owner, dialog, chooser))
    entries.sort(key=lambda entry: (entry[0] < 0, entry[0]))
    return entries


def _has_pending_modal(page: Any | None = None) -> bool:
    del page
    return bool(_state.pending_modal_pages())


def _capture_page_title(page: Any) -> str | None:
    """Cache a safe title without querying a page blocked by a dialog."""
    dialog, _ = _pending_modals(page)
    if dialog is not None:
        return _state.known_page_title(page)
    try:
        title = str(page.title())
    except Exception:
        return _state.known_page_title(page)
    _state.remember_page_title(page, title)
    return title


def _save_download(download: Any, suggested_filename: str) -> str:
    """Save an untrusted download name into the confined artifact root."""
    policy = get_file_policy()
    leaf = suggested_filename.replace("\\", "/").rsplit("/", 1)[-1]
    leaf = re.sub(r"[^A-Za-z0-9._-]+", "_", leaf).lstrip(".")
    if not leaf:
        leaf = "download"
    if len(leaf) > 180:
        stem, suffix = os.path.splitext(leaf)
        leaf = stem[: max(1, 180 - len(suffix))] + suffix[:20]

    candidate = leaf
    while True:
        try:
            output_path = policy.reserve_output(
                candidate, purpose="download", suffix=".bin"
            )
            break
        except ValueError as exc:
            if "already exists" not in str(exc):
                raise
            stem, suffix = os.path.splitext(leaf)
            candidate = f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"
    try:
        download.save_as(str(output_path))
        return policy.finalize_output(output_path)
    except Exception:
        policy.discard_output(output_path)
        raise


def _page():
    """Return the active page, launching locally or attaching over remote CDP.

    In remote CDP mode, local launch options (headless, channel, and executable)
    are ignored.
    """
    if _state.page is not None:
        if _has_pending_modal(_state.page):
            # Chromium blocks evaluation while a JavaScript dialog is live.
            # Event state is enough to service the modal tools, so never run
            # the health-check evaluation in this state.
            return _state.page
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
) -> str | None:
    if depth is not None and depth < 0:
        raise ValueError("depth must be non-negative")
    if _has_pending_modal():
        # This check is deliberately before wait_for_load_state/evaluate: a live
        # dialog blocks both in Chromium. The triggering action can therefore
        # return its Modal section promptly without attempting a fresh snapshot.
        return None
    try:
        # An action may have triggered a navigation; settle before reading the DOM.
        page.wait_for_load_state(timeout=10_000)
    except Exception:
        pass
    _capture_page_title(page)
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
    pending_dialog, _ = _pending_modals(page)
    if pending_dialog is not None:
        title = _state.known_page_title(page) or "(dialog pending)"
    else:
        title = _capture_page_title(page) or "(unavailable)"
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


def _metadata_value(items: tuple[tuple[str, Any], ...], *names: str) -> Any:
    values = dict(items)
    for name in names:
        if name in values:
            return values[name]
    return None


def _console_level(message_type: str) -> str:
    normalized = message_type.lower()
    if normalized in {"warning", "warn"}:
        return "warning"
    if normalized in {"error", "debug"}:
        return normalized
    return "info"


def _format_console_record(record: Any) -> str:
    level = _console_level(record.message_type).upper()
    url = _metadata_value(record.location, "url") or "(unknown)"
    line = _metadata_value(record.location, "lineNumber", "line")
    location = str(url) if line is None else f"{url}:{line}"
    text = str(record.text).replace("\r", " ").replace("\n", " ")
    return f"{level} {location} {text}"


def _format_modal(page: Any) -> str | None:
    lines: list[str] = []
    for tab_index, _, dialog, chooser in _pending_modal_entries(page):
        owner = f"Tab {tab_index}" if tab_index >= 0 else "Registered page"
        if dialog is not None:
            try:
                dialog_type = str(dialog.type)
            except Exception:
                dialog_type = "dialog"
            try:
                message = str(dialog.message)
            except Exception:
                message = "(message unavailable)"
            lines.append(
                f"- {owner}: Dialog pending: type={dialog_type}; "
                f"message={message!r}. Call browser_handle_dialog."
            )
        if chooser is not None:
            try:
                multiple = bool(chooser.is_multiple())
            except Exception:
                multiple = False
            hint = "multiple files allowed" if multiple else "single file only"
            lines.append(
                f"- {owner}: File chooser pending: {hint}. "
                "Call browser_file_upload."
            )
    return "\n".join(lines) if lines else None


def _render_response(
    result: str | None = None,
    *,
    page: Any | None = None,
    snapshot: str | None = None,
    include_tabs: bool = False,
) -> str:
    """Render an E2 envelope with deterministic, cursor-aware section order."""
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
            pending_dialog, _ = _pending_modals(tab)
            if pending_dialog is not None:
                tab_title = _state.known_page_title(tab) or "(dialog pending)"
            else:
                tab_title = _state.known_page_title(tab)
                if tab_title is None:
                    tab_title = _capture_page_title(tab) or "(unavailable)"
            lines.append(f"- {index}: {tab_title} — {tab.url}{marker}")
        sections.append("### Tabs\n" + ("\n".join(lines) or "- (none)"))
    if snapshot is not None:
        sections.append(f"### Snapshot\n{snapshot}")

    console_records, _, downloads = _state.response_events()
    terse_console = [
        record
        for record in console_records
        if _CONSOLE_LEVEL_RANK[_console_level(record.message_type)] <= 1
    ]
    if terse_console:
        visible = terse_console[:5]
        lines = [_format_console_record(record) for record in visible]
        overflow = len(terse_console) - len(visible)
        if overflow:
            lines.append(f"(and {overflow} more)")
        sections.append("### Console\n" + "\n".join(lines))

    if page is not None:
        modal = _format_modal(page)
        if modal is not None:
            sections.append(f"### Modal\n{modal}")

    if downloads:
        download_lines = []
        for record in downloads:
            if record.artifact is not None:
                download_lines.append(
                    f"- {record.suggested_filename}: `{record.artifact}`"
                )
            else:
                download_lines.append(
                    f"- {record.suggested_filename}: save failed ({record.error})"
                )
        sections.append("### Downloads\n" + "\n".join(download_lines))
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
    selector: str


_REF_PATTERN = re.compile(r"^e[1-9][0-9]*$")


def _resolve(
    page: Any,
    target: str,
    element_description: str | None = None,
) -> ResolvedTarget:
    """Resolve a current stamped ref or exactly one selector match."""
    if _has_pending_modal():
        raise ValueError(
            "A modal is pending; handle the dialog or file chooser before "
            "resolving page targets."
        )
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
        selector = f'[data-mcp-ref="{target}"]'
        return ResolvedTarget(locator, display_name, "ref", selector)

    locator = page.locator(target)
    count = locator.count()
    if count == 0:
        raise ValueError(f"Target selector matched no elements: {target}")
    if count > 1:
        raise ValueError(
            f"Target selector matched {count} elements; provide a unique selector: {target}"
        )
    return ResolvedTarget(locator, display_name, "selector", target)


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
PositiveDimension = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NetworkIndex = Annotated[int, Field(ge=1)]
UploadPaths = Annotated[list[str], Field(max_length=50)]


_SYNTHETIC_DROP_JS = """async (target, payload) => {
    const transfer = new DataTransfer();
    transfer.effectAllowed = "copy";
    transfer.dropEffect = "copy";

    for (const entry of payload.files) {
        const binary = atob(entry.base64);
        const bytes = new Uint8Array(binary.length);
        for (let index = 0; index < binary.length; index += 1) {
            bytes[index] = binary.charCodeAt(index);
        }
        transfer.items.add(new File([bytes], entry.name, {
            type: entry.mime,
            lastModified: 0,
        }));
    }
    for (const [mime, value] of payload.data) {
        transfer.setData(mime, value);
    }

    const bounds = target.getBoundingClientRect();
    const eventOptions = {
        bubbles: true,
        cancelable: true,
        composed: true,
        clientX: bounds.left + bounds.width / 2,
        clientY: bounds.top + bounds.height / 2,
        dataTransfer: transfer,
    };
    for (const type of ["dragenter", "dragover", "drop"]) {
        target.dispatchEvent(new DragEvent(type, eventOptions));
    }

    // Let FileReader- and promise-based handlers make progress before the
    // fresh snapshot. Application work that outlives this task remains async.
    await new Promise((resolve) => setTimeout(resolve, 0));
}"""


class FillField(TypedDict):
    """One strictly validated browser_fill_form operation."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    element: NotRequired[str]
    target: str
    name: str
    type: Literal["textbox", "checkbox", "radio", "combobox", "slider"]
    value: str


FillFields = Annotated[list[FillField], Field(min_length=1, max_length=50)]


def _round_half_away_from_zero(value: float) -> int:
    """Round a finite dimension to the nearest integer device pixel."""
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


def _run_action_with_pending_dialog(page: Any, action: Any) -> None:
    """Treat a dialog-stalled action as complete once the modal is recorded."""
    try:
        action()
    except Exception:
        dialog, _ = _pending_modals(page)
        if dialog is None:
            raise


def _compile_find_regex(expression: str) -> re.Pattern[str]:
    pattern = expression
    flags = 0
    if expression.startswith("/"):
        closing = expression.rfind("/")
        if closing == 0:
            raise ValueError(
                "regex slash form must be /pattern/flags (flags: i, m, s)"
            )
        pattern = expression[1:closing]
        raw_flags = expression[closing + 1 :]
        invalid = sorted(set(raw_flags) - {"i", "m", "s"})
        if invalid:
            raise ValueError(
                "invalid regex flags: " + ", ".join(invalid) + "; supported: i, m, s"
            )
        if len(set(raw_flags)) != len(raw_flags):
            raise ValueError("regex flags must not be repeated")
        for flag in raw_flags:
            flags |= {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}[flag]
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"invalid regex: {exc}") from None


def _outline_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _find_outline_matches(
    outline: str,
    matcher: Any,
    *,
    limit: int = FIND_MATCH_LIMIT,
) -> tuple[list[str], int]:
    """Return path-qualified matches with one same-parent sibling each side."""
    lines = outline.splitlines()
    matching_indices = [index for index, line in enumerate(lines) if matcher(line)]
    rendered: list[str] = []
    for match_number, index in enumerate(matching_indices[:limit], start=1):
        indent = _outline_indent(lines[index])
        ancestors: list[str] = []
        wanted_indent = indent - 2
        for candidate in range(index - 1, -1, -1):
            candidate_indent = _outline_indent(lines[candidate])
            if candidate_indent == wanted_indent:
                ancestors.append(lines[candidate].strip())
                wanted_indent -= 2
                if wanted_indent < 0:
                    break
        path = " > ".join(reversed(ancestors)) or "(root)"

        siblings: list[int] = []
        for direction in (-1, 1):
            candidate = index + direction
            while 0 <= candidate < len(lines):
                candidate_indent = _outline_indent(lines[candidate])
                if candidate_indent < indent:
                    break
                if candidate_indent == indent:
                    siblings.append(candidate)
                    break
                candidate += direction
        context = sorted({*siblings, index})
        snippets = [
            ("> " if candidate == index else "  ") + lines[candidate]
            for candidate in context
        ]
        rendered.append(
            f"Match {match_number}\nPath: {path}\n" + "\n".join(snippets)
        )
    return rendered, max(0, len(matching_indices) - limit)


# Successful image/media/font/stylesheet requests are the chosen static set.
# Scripts are intentionally visible because they often carry application logic.
_STATIC_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}


def _filter_network_records(
    records: list[Any],
    *,
    include_static: bool,
    pattern: re.Pattern[str] | None,
) -> list[Any]:
    visible = []
    for record in records:
        successful_static = (
            record.resource_type.lower() in _STATIC_RESOURCE_TYPES
            and record.response_status is not None
            and 200 <= record.response_status < 400
        )
        if not include_static and successful_static:
            continue
        if pattern is not None and pattern.search(record.url) is None:
            continue
        visible.append(record)
    return visible


def _thaw_metadata(value: Any) -> Any:
    if isinstance(value, tuple):
        if all(
            isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str)
            for item in value
        ):
            return {name: _thaw_metadata(item) for name, item in value}
        return [_thaw_metadata(item) for item in value]
    return value


def _header_content_type(headers: tuple[tuple[str, Any], ...]) -> str:
    for name, value in headers:
        if name.lower() == "content-type":
            return str(value).split(";", 1)[0].strip().lower()
    return ""


def _is_text_content_type(content_type: str) -> bool:
    return (
        content_type.startswith("text/")
        or content_type.endswith("+json")
        or content_type.endswith("+xml")
        or content_type
        in {
            "application/json",
            "application/xml",
            "application/javascript",
            "application/x-javascript",
            "application/x-www-form-urlencoded",
        }
    )


def _response_body_text(record: Any, *, byte_limit: int | None) -> str:
    if record.response is None:
        return "(response not received)"
    try:
        raw = record.response.body()
    except Exception as exc:
        return f"(body unavailable: {exc})"
    if isinstance(raw, str):
        body = raw.encode("utf-8")
    else:
        body = bytes(raw)
    if not body:
        return "(empty response body)"

    truncated = byte_limit is not None and len(body) > byte_limit
    delivered = body[:byte_limit] if truncated and byte_limit is not None else body
    content_type = _header_content_type(record.response_headers)
    if _is_text_content_type(content_type):
        rendered = delivered.decode("utf-8", errors="replace")
        note = ""
    else:
        rendered = base64.b64encode(delivered).decode("ascii")
        note = "\n(base64-encoded non-text response body)"
    if truncated:
        note += (
            f"\n(response body truncated to {byte_limit} bytes inline; "
            "use filename for the full body)"
        )
    return rendered + note


def _network_record_text(
    record: Any,
    part: Literal[
        "request-headers", "request-body", "response-headers", "response-body"
    ]
    | None,
    *,
    body_limit: int | None,
) -> str:
    parts = (
        [part]
        if part is not None
        else [
            "request-headers",
            "request-body",
            "response-headers",
            "response-body",
        ]
    )
    rendered: list[str] = []
    for selected in parts:
        if selected == "request-headers":
            value = json.dumps(
                _thaw_metadata(record.headers), ensure_ascii=False, indent=2
            )
        elif selected == "request-body":
            value = record.post_data if record.post_data not in {None, ""} else "(empty request body)"
        elif selected == "response-headers":
            value = (
                "(response not received)"
                if record.response is None
                else json.dumps(
                    _thaw_metadata(record.response_headers),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            value = _response_body_text(record, byte_limit=body_limit)
        rendered.append(f"#### {selected}\n{value}")
    return "\n\n".join(rendered)


@_tool()
def browser_navigate(url: str) -> str:
    """Navigate to a URL and return a fresh snapshot."""
    page = _page()
    page.goto(url)
    snapshot = _snapshot(page)
    return _render_response(f"Navigated to {url}.", page=page, snapshot=snapshot)


@_tool()
def browser_resize(width: PositiveDimension, height: PositiveDimension) -> str:
    """Resize the page viewport, not the operating-system browser window.

    Responsive layout and the rendered DOM may change, so a fresh snapshot is
    returned after ``page.set_viewport_size`` completes.
    """
    page = _page()
    pixel_width = _round_half_away_from_zero(width)
    pixel_height = _round_half_away_from_zero(height)
    page.set_viewport_size({"width": pixel_width, "height": pixel_height})
    snapshot = _snapshot(page)
    return _render_response(
        f"Viewport resized to {pixel_width}x{pixel_height} CSS pixels.",
        page=page,
        snapshot=snapshot,
    )


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
        if snapshot is None:
            return _render_response(
                "Snapshot deferred until the pending modal is handled.", page=page
            )
        artifact = _write_text_output(snapshot, filename, purpose="snapshot")
        return _render_response(
            f"Snapshot written to `{artifact}`.",
            page=page,
        )
    return _render_response(page=page, snapshot=snapshot)


@_tool()
def browser_find(text: str | None = None, regex: str | None = None) -> str:
    """Search one refreshed current outline and return compact actionable context."""
    if (text is None) == (regex is None):
        raise ValueError("Exactly one of text or regex is required.")
    if regex is not None:
        compiled = _compile_find_regex(regex)
        matcher = compiled.search
    else:
        assert text is not None
        needle = text.casefold()
        matcher = lambda line: needle in line.casefold()

    page = _page()
    outline = _snapshot(page)
    if outline is None:
        return _render_response(
            "Find deferred until the pending modal is handled.", page=page
        )
    matches, truncated = _find_outline_matches(
        outline, lambda line: matcher(line) is not None if regex is not None else matcher(line)
    )
    if not matches:
        result = "No matches in the current snapshot outline."
    else:
        result = "\n\n".join(matches)
        if truncated:
            result += f"\n\n… {truncated} additional matches truncated."
    return _render_response(result, page=page)


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
    with _state.event_lock:
        registry = _state.registry_for(page)
        retry_element = (
            None if registry is None else registry.file_chooser_retry_element
        )
    retry_same_file_input = False
    if retry_element is not None:
        try:
            retry_same_file_input = bool(
                resolved.locator.evaluate(
                    "(candidate, retryElement) => candidate === retryElement",
                    retry_element,
                )
            )
        except Exception:
            # If DOM identity cannot be established, never replay a click.
            with _state.event_lock:
                registry = _state.registry_for(page)
                if (
                    registry is not None
                    and registry.file_chooser_retry_element is retry_element
                ):
                    registry.file_chooser_retry_element = None
    def action() -> None:
        resolved.locator.click(
            click_count=2 if doubleClick else 1,
            button=button,
            modifiers=modifiers,
            timeout=1_000,
        )

    _run_action_with_pending_dialog(page, action)
    if retry_same_file_input:
        _, chooser = _pending_modals(page)
        if chooser is None:
            # A cancelled chooser after a validation failure is suppressed once
            # by some backends. Replay only the originating input's click once.
            page.wait_for_timeout(50)
            _run_action_with_pending_dialog(page, action)
        with _state.event_lock:
            registry = _state.registry_for(page)
            if (
                registry is not None
                and registry.file_chooser_retry_element is retry_element
            ):
                registry.file_chooser_retry_element = None
    snapshot = _snapshot(page)
    return _render_response(
        f"Clicked {resolved.display_name}.", page=page, snapshot=snapshot
    )


@_tool()
def browser_drag(
    startTarget: str,
    endTarget: str,
    startElement: str | None = None,
    endElement: str | None = None,
) -> str:
    """Strict-resolve both endpoints and drag the first element to the second."""
    page = _page()
    start = _resolve(page, startTarget, startElement)
    end = _resolve(page, endTarget, endElement)
    page.drag_and_drop(start.selector, end.selector, strict=True)
    snapshot = _snapshot(page)
    return _render_response(
        f"Dragged {start.display_name} to {end.display_name}.",
        page=page,
        snapshot=snapshot,
    )


@_tool()
def browser_drop(
    target: str,
    element: str | None = None,
    paths: UploadPaths | None = None,
    data: dict[str, str] | None = None,
) -> str:
    """Best-effort drop files and/or MIME strings onto a unique target.

    ``element`` is only a human-readable description. The tool uses synthetic DataTransfer semantics:
    synthesized drop events; sites checking event trust or native drag sessions may behave differently.
    """
    supplied_paths = [] if paths is None else paths
    supplied_data = {} if data is None else data
    if not supplied_paths and not supplied_data:
        raise ValueError("browser_drop requires at least one non-empty paths or data source")

    normalized_mime_keys: set[str] = set()
    for mime in supplied_data:
        if not mime.strip():
            raise ValueError("browser_drop data MIME keys must be non-empty")
        normalized = mime.casefold()
        if normalized in normalized_mime_keys:
            raise ValueError(
                "browser_drop data MIME keys must be unique ignoring case"
            )
        normalized_mime_keys.add(normalized)

    page = _page()
    resolved = _resolve(page, target, element)

    policy = get_file_policy()
    files = []
    for path, content in policy.read_inputs(supplied_paths):
        inferred_mime, _ = mimetypes.guess_type(path.name, strict=False)
        files.append(
            {
                "name": path.name,
                "mime": inferred_mime or "application/octet-stream",
                "base64": base64.b64encode(content).decode("ascii"),
            }
        )

    resolved.locator.evaluate(
        _SYNTHETIC_DROP_JS,
        {"files": files, "data": list(supplied_data.items())},
    )
    snapshot = _snapshot(page)
    source_summary = f"{len(files)} file(s) and {len(supplied_data)} data item(s)"
    return _render_response(
        f"Synthesized a drop of {source_summary} on {resolved.display_name}.",
        page=page,
        snapshot=snapshot,
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
def browser_fill_form(fields: FillFields) -> str:
    """Fill up to 50 fields sequentially as a non-transactional batch.

    If a field fails, earlier changes intentionally remain applied and the
    structured error names the first failing field.
    """
    page = _page()
    for field in fields:
        name = field["name"]
        try:
            resolved = _resolve(page, field["target"], field.get("element"))
            locator = resolved.locator
            field_type = field["type"]
            value = field["value"]
            if field_type in {"textbox", "slider"}:
                locator.fill(value)
            elif field_type == "checkbox":
                if value == "true":
                    locator.check()
                elif value == "false":
                    locator.uncheck()
                else:
                    raise ValueError("checkbox value must be 'true' or 'false'")
            elif field_type == "radio":
                if value == "false":
                    raise ValueError("unchecking a radio is not supported")
                if value != "true":
                    raise ValueError("radio value must be 'true'")
                locator.check()
            else:
                try:
                    locator.select_option(label=value)
                except Exception:
                    locator.select_option(value=value)
        except Exception as exc:
            raise ValueError(f"Field {name!r} failed: {exc}") from None
    snapshot = _snapshot(page)
    return _render_response(
        f"Filled {len(fields)} fields sequentially (non-transactional).",
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
    for tab in pages:
        _register_page_handlers(tab)

    if action == "new":
        page = context.new_page()
        _register_page_handlers(page)
        _state.page = page
        if url is not None:
            page.goto(url)
            _capture_page_title(page)
        snapshot = _snapshot(page)
    elif action == "select":
        if index is None or index < 0 or index >= len(pages):
            raise ValueError(
                f"Invalid tab index {index}; expected 0 through {len(pages) - 1}"
            )
        page = pages[index]
        _register_page_handlers(page)
        _state.page = page
        pending_dialog, _ = _pending_modals(page)
        if pending_dialog is None:
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
        if any(entry[1] is closing for entry in _pending_modal_entries(page)):
            raise ValueError(
                f"Tab {closing_index} has a pending modal; handle it before closing."
            )
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

    for tab in list(context.pages):
        _register_page_handlers(tab)

    return _render_response(
        f"Tab action `{action}` completed.",
        page=page,
        snapshot=snapshot,
        include_tabs=True,
    )


@_tool()
def browser_console_messages(
    level: Literal["error", "warning", "info", "debug"] = "info",
    all: bool = False,
    filename: str | None = None,
) -> str:
    """List console records at the requested threshold under the event lock."""
    page = _page()
    with _state.event_lock:
        registry = _state.registry_for(page)
        epoch = 0 if registry is None else registry.navigation_epoch
        records = [] if registry is None else list(registry.console_records)
        if not all:
            records = [record for record in records if record.epoch == epoch]
        evicted = 0
        if registry is not None:
            evicted = (
                registry.console_evictions_total
                if all
                else registry.console_evictions_current_epoch
            )

    threshold = _CONSOLE_LEVEL_RANK[level]
    visible = [
        record
        for record in records
        if _CONSOLE_LEVEL_RANK[_console_level(record.message_type)] <= threshold
    ]
    lines = [_format_console_record(record) for record in visible]
    if evicted:
        lines.append(
            f"(console ring buffer evicted {evicted} earlier matching-scope records)"
        )
    content = "\n".join(lines) or "(no console messages)"
    if filename is not None:
        artifact = _write_text_output(content, filename, purpose="console")
        result = f"Console messages written to `{artifact}`."
    else:
        result = content
    return _render_response(result, page=page)


@_tool()
def browser_network_requests(
    static: bool = False,
    filter: str | None = None,
    filename: str | None = None,
) -> str:
    """List current-navigation requests while preserving their stable indices."""
    pattern = None
    if filter is not None:
        try:
            pattern = re.compile(filter)
        except re.error as exc:
            raise ValueError(f"invalid network filter regex: {exc}") from None

    page = _page()
    with _state.event_lock:
        registry = _state.registry_for(page)
        epoch = 0 if registry is None else registry.navigation_epoch
        records = (
            []
            if registry is None
            else [
                record
                for record in registry.network_records
                if record.epoch == epoch
            ]
        )
        evicted = (
            0 if registry is None else registry.network_evictions.get(epoch, 0)
        )

    visible = _filter_network_records(
        records, include_static=static, pattern=pattern
    )
    lines = []
    for record in visible:
        if record.response_status is not None:
            status = str(record.response_status)
        elif record.failure is not None:
            status = "FAILED"
        else:
            status = "PENDING"
        lines.append(
            f"[{record.index}] {record.method} {status} {record.url} "
            f"({record.resource_type})"
        )
    if evicted:
        lines.append(
            f"(network ring buffer evicted {evicted} earlier current-epoch records)"
        )
    content = "\n".join(lines) or "(no matching network requests)"
    if filename is not None:
        artifact = _write_text_output(content, filename, purpose="network")
        result = f"Network requests written to `{artifact}`."
    else:
        result = content
    return _render_response(result, page=page)


@_tool()
def browser_network_request(
    index: NetworkIndex,
    part: Literal[
        "request-headers", "request-body", "response-headers", "response-body"
    ]
    | None = None,
    filename: str | None = None,
) -> str:
    """Return lazy details for one stable current-navigation request index."""
    page = _page()
    with _state.event_lock:
        registry = _state.registry_for(page)
        epoch = 0 if registry is None else registry.navigation_epoch
        navigation_start = (
            _state.next_request_index
            if registry is None
            else registry.navigation_start_request_index
        )
        records = (
            []
            if registry is None
            else [
                record
                for record in registry.network_records
                if record.epoch == epoch
            ]
        )
        record = next((item for item in records if item.index == index), None)
        previous_navigation = registry is not None and (
            index < navigation_start
            or any(
                item.index == index and item.epoch != epoch
                for item in registry.network_records
            )
        )
    if record is None:
        if records:
            valid = f"{min(item.index for item in records)}-{max(item.index for item in records)}"
        else:
            valid = "none"
        if previous_navigation:
            raise ValueError(
                f"Request index {index} is from a previous navigation; "
                f"current requests are {valid} (current navigation epoch)."
            )
        raise ValueError(
            f"Network request index {index} is unavailable in the current "
            f"navigation epoch; valid range: {valid}."
        )

    content = _network_record_text(
        record,
        part,
        body_limit=None if filename is not None else NETWORK_BODY_INLINE_BYTES,
    )
    if filename is not None:
        artifact = _write_text_output(content, filename, purpose="network-request")
        result = f"Network request {index} written to `{artifact}`."
    else:
        result = content
    return _render_response(result, page=page)


@_tool()
def browser_file_upload(paths: UploadPaths | None = None) -> str:
    """Resolve a pending file chooser with confined files, or cancel with []."""
    page = _page()
    entries = _pending_modal_entries(page)
    chooser_entry = next((entry for entry in entries if entry[3] is not None), None)
    if chooser_entry is None:
        raise ValueError("no file chooser is pending")
    if any(dialog is not None for _, _, dialog, _ in entries):
        raise ValueError("a dialog is pending; handle it before the file chooser")
    _, owner_page, _, chooser = chooser_entry
    assert chooser is not None

    supplied = [] if paths is None else paths
    chooser_released = False
    failure: Exception | None = None
    try:
        try:
            multiple = bool(chooser.is_multiple())
        except Exception:
            multiple = False
        if len(supplied) > 1 and not multiple:
            raise ValueError("the pending file chooser accepts only one file")
        policy = get_file_policy()
        validated = [str(policy.validate_input(path)) for path in supplied]
        # set_files([]) is the browser API's supported chooser cancellation path;
        # unlike merely dropping our reference, it releases the chooser event.
        chooser.set_files(validated)
        chooser_released = True
    except Exception as exc:
        failure = exc
    finally:
        if not chooser_released:
            # Validation/multiplicity errors still have to release the live
            # browser chooser before clearing our modal slot, or a later click
            # on the same input will not produce a new chooser event.
            try:
                chooser.set_files([])
            except Exception:
                pass
            try:
                # Some browser backends acknowledge [] after an input already
                # had a file without making the control reopenable. Resetting
                # only the failed attempt's input is the compatibility
                # workaround; explicit user cancellation still uses [] alone.
                chooser.element.evaluate(
                    "element => { element.value = ''; element.blur(); }"
                )
            except Exception:
                pass
            try:
                retry_element = chooser.element
            except Exception:
                retry_element = None
            with _state.event_lock:
                registry = _state.registry_for(owner_page)
                if registry is not None:
                    registry.file_chooser_retry_element = retry_element
        else:
            with _state.event_lock:
                registry = _state.registry_for(owner_page)
                if registry is not None:
                    registry.file_chooser_retry_element = None
        _state.clear_pending_file_chooser(owner_page, chooser)

    if failure is not None:
        raise ValueError(
            f"{failure} Retry by clicking the same file input again."
        ) from None

    snapshot = _snapshot(page)
    result = (
        "Cancelled the pending file chooser."
        if not supplied
        else f"Uploaded {len(supplied)} file(s) through the pending chooser."
    )
    return _render_response(result, page=page, snapshot=snapshot)


@_tool()
def browser_handle_dialog(accept: bool, promptText: PromptText = None) -> str:
    """Accept or dismiss the JavaScript dialog that is pending now."""
    page = _page()
    dialog_entry = next(
        (
            entry
            for entry in _pending_modal_entries(page)
            if entry[2] is not None
        ),
        None,
    )
    if dialog_entry is None:
        raise ValueError("no dialog is pending")
    _, owner_page, dialog, _ = dialog_entry
    assert dialog is not None
    if not accept and promptText is not None:
        raise ValueError("promptText cannot be honored when dismissing a dialog")
    try:
        if accept:
            dialog.accept(promptText)
        else:
            dialog.dismiss()
    finally:
        _state.clear_pending_dialog(owner_page, dialog)
    action = "Accepted" if accept else "Dismissed"
    return _render_response(f"{action} the pending dialog.", page=page)


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
