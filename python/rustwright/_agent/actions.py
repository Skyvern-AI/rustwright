"""Transport-neutral browser actions returning plain dictionaries."""

import re
from typing import Any, Callable, Dict, List, Optional

from rustwright.sync_api import TimeoutError as BrowserTimeoutError

from .errors import AgentError
from .refs import RefRegistry, resolve
from .session import BrowserSession


_WAIT_UNTIL = {"domcontentloaded", "load", "networkidle"}


def _string(
    name: str,
    value: Any,
    minimum: int,
    maximum: int,
) -> str:
    if not isinstance(value, str) or len(value) < minimum or len(value) > maximum:
        raise AgentError(
            "invalid_argument",
            "%s must be a string with length between %d and %d" % (name, minimum, maximum),
        )
    return value


def _integer(name: str, value: Any, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise AgentError(
            "invalid_argument",
            "%s must be an integer between %d and %d" % (name, minimum, maximum),
        )
    return value


def _boolean(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise AgentError("invalid_argument", "%s must be a boolean" % name)
    return value


def _wait_state(value: Optional[str]) -> str:
    if value is None:
        return "domcontentloaded"
    if not isinstance(value, str) or value not in _WAIT_UNTIL:
        raise AgentError(
            "invalid_argument",
            "wait_until must be domcontentloaded, load, or networkidle",
        )
    return value


def _invalidate(registry: RefRegistry) -> None:
    registry.state.active = {}


def _dispatch(session: BrowserSession, operation: Callable[[], Any]) -> Any:
    try:
        with session.dialog_action_scope():
            return operation()
    except AgentError:
        raise
    except BrowserTimeoutError:
        raise AgentError("timeout", "The browser action timed out") from None
    except Exception:
        raise AgentError("action_failed", "The browser action failed") from None


def _title(page: Any) -> str:
    try:
        return str(page.title())
    except Exception:
        return ""


def _fresh_snapshot(session: BrowserSession, message: str) -> Dict[str, Any]:
    page = session.active_page()
    registry = session.active_registry()
    try:
        snap = registry.snapshot(
            page,
            depth=session.snapshot_depth,
            max_chars=session.snapshot_max_chars,
            max_refs=1000,
            mask_password_values=session.mask_password_values,
        )
    except Exception:
        raise AgentError(
            "action_succeeded_snapshot_failed",
            "The action succeeded, but the fresh snapshot failed",
            "Take a new snapshot before issuing another action.",
        ) from None
    return {
        "message": message,
        "url": snap["url"],
        "title": _title(page),
        "tab_id": session.active_tab_id,
        "epoch": snap["epoch"],
        "snapshot": snap["text"],
    }


def navigate(
    session: BrowserSession,
    url: str,
    wait_until: Optional[str] = None,
) -> Dict[str, Any]:
    url = _string("url", url, 1, 8192)
    state = _wait_state(wait_until)
    page = session.active_page()
    _invalidate(session.active_registry())
    _dispatch(
        session,
        lambda: page.goto(
            url,
            wait_until=state,
            timeout=session.navigation_timeout_ms,
        ),
    )
    return _fresh_snapshot(session, "navigated")


def navigate_back(
    session: BrowserSession,
    wait_until: Optional[str] = None,
) -> Dict[str, Any]:
    state = _wait_state(wait_until)
    page = session.active_page()
    _invalidate(session.active_registry())
    _dispatch(
        session,
        lambda: page.go_back(
            wait_until=state,
            timeout=session.navigation_timeout_ms,
        ),
    )
    return _fresh_snapshot(session, "navigated back")


def reload(
    session: BrowserSession,
    wait_until: Optional[str] = None,
) -> Dict[str, Any]:
    state = _wait_state(wait_until)
    page = session.active_page()
    _invalidate(session.active_registry())
    _dispatch(
        session,
        lambda: page.reload(
            wait_until=state,
            timeout=session.navigation_timeout_ms,
        ),
    )
    return _fresh_snapshot(session, "reloaded")


def snapshot(
    session: BrowserSession,
    depth: Optional[int] = None,
    max_chars: Optional[int] = None,
) -> Dict[str, Any]:
    if depth is None:
        depth_value = session.snapshot_depth
    else:
        depth_value = _integer("depth", depth, 0, 12)
    if max_chars is None:
        max_chars_value = session.snapshot_max_chars
    else:
        max_chars_value = _integer("max_chars", max_chars, 1000, 200000)

    page = session.active_page()
    registry = session.active_registry()

    def take_snapshot() -> Dict[str, Any]:
        return registry.snapshot(
            page,
            depth=depth_value,
            max_chars=max_chars_value,
            max_refs=1000,
            mask_password_values=session.mask_password_values,
        )

    snap = _dispatch(session, take_snapshot)
    return {
        "message": "snapshot",
        "url": snap["url"],
        "title": _title(page),
        "tab_id": session.active_tab_id,
        "epoch": snap["epoch"],
        "snapshot": snap["text"],
    }


def click(
    session: BrowserSession,
    ref: str,
    button: str = "left",
    click_count: int = 1,
) -> Dict[str, Any]:
    if not isinstance(button, str) or button not in {"left", "right", "middle"}:
        raise AgentError("invalid_argument", "button must be left, right, or middle")
    click_count = _integer("click_count", click_count, 1, 3)
    page = session.active_page()
    registry = session.active_registry()
    locator = resolve(page, registry, ref)
    _invalidate(registry)
    _dispatch(
        session,
        lambda: locator.click(
            button=button,
            click_count=click_count,
            timeout=session.action_timeout_ms,
        ),
    )
    normalized = ref[1:] if ref.startswith("@") else ref
    return _fresh_snapshot(session, "clicked %s" % normalized)


def fill(session: BrowserSession, ref: str, text: str) -> Dict[str, Any]:
    text = _string("text", text, 0, 200000)
    page = session.active_page()
    registry = session.active_registry()
    locator = resolve(page, registry, ref)
    _invalidate(registry)
    _dispatch(
        session,
        lambda: locator.fill(text, timeout=session.action_timeout_ms),
    )
    normalized = ref[1:] if ref.startswith("@") else ref
    return _fresh_snapshot(session, "filled %s" % normalized)


def type_text(
    session: BrowserSession,
    ref: str,
    text: str,
    delay_ms: int = 0,
) -> Dict[str, Any]:
    text = _string("text", text, 0, 200000)
    delay_ms = _integer("delay_ms", delay_ms, 0, 1000)
    page = session.active_page()
    registry = session.active_registry()
    locator = resolve(page, registry, ref)
    _invalidate(registry)
    _dispatch(
        session,
        lambda: locator.type(
            text,
            delay=delay_ms,
            timeout=session.action_timeout_ms,
        ),
    )
    normalized = ref[1:] if ref.startswith("@") else ref
    return _fresh_snapshot(session, "typed into %s" % normalized)


def select_option(
    session: BrowserSession,
    ref: str,
    values: List[str],
) -> Dict[str, Any]:
    if not isinstance(values, list) or len(values) < 1 or len(values) > 50:
        raise AgentError("invalid_argument", "values must contain between 1 and 50 strings")
    if any(not isinstance(value, str) for value in values):
        raise AgentError("invalid_argument", "values must contain only strings")
    page = session.active_page()
    registry = session.active_registry()
    locator = resolve(page, registry, ref)
    _invalidate(registry)
    selected = _dispatch(
        session,
        lambda: locator.select_option(
            value=values,
            timeout=session.action_timeout_ms,
        ),
    )
    count = len(selected) if isinstance(selected, list) else len(values)
    return _fresh_snapshot(session, "selected %d option(s)" % count)


def hover(session: BrowserSession, ref: str) -> Dict[str, Any]:
    page = session.active_page()
    registry = session.active_registry()
    locator = resolve(page, registry, ref)
    _invalidate(registry)
    _dispatch(
        session,
        lambda: locator.hover(timeout=session.action_timeout_ms),
    )
    normalized = ref[1:] if ref.startswith("@") else ref
    return _fresh_snapshot(session, "hovered %s" % normalized)


def press_key(session: BrowserSession, key: str) -> Dict[str, Any]:
    key = _string("key", key, 1, 100)
    page = session.active_page()
    _invalidate(session.active_registry())
    _dispatch(session, lambda: page.keyboard.press(key))
    return _fresh_snapshot(session, "pressed key")


def wait_for(
    session: BrowserSession,
    *,
    time_ms: Optional[int] = None,
    text: Optional[str] = None,
    text_gone: Optional[str] = None,
    load_state: Optional[str] = None,
) -> Dict[str, Any]:
    supplied = [time_ms is not None, text is not None, text_gone is not None, load_state is not None]
    if sum(1 for value in supplied if value) != 1:
        raise AgentError("invalid_argument", "Exactly one wait condition is required")

    if time_ms is not None:
        duration = _integer("time_ms", time_ms, 0, 60000)
        kind = "time"
        value = duration  # type: Any
        message = "waited %d ms" % duration
    elif text is not None:
        visible_text = _string("text", text, 1, 10000)
        kind = "text"
        value = visible_text
        message = "waited for text"
    elif text_gone is not None:
        hidden_text = _string("text_gone", text_gone, 1, 10000)
        kind = "text_gone"
        value = hidden_text
        message = "waited for text to disappear"
    else:
        if not isinstance(load_state, str) or load_state not in _WAIT_UNTIL:
            raise AgentError(
                "invalid_argument",
                "load_state must be domcontentloaded, load, or networkidle",
            )
        kind = "load_state"
        value = load_state
        message = "waited for load state"

    page = session.active_page()
    _invalidate(session.active_registry())
    if kind == "time":
        operation = lambda: page.wait_for_timeout(value)
    elif kind == "text":
        operation = lambda: page.get_by_text(value).wait_for(
            state="visible",
            timeout=session.action_timeout_ms,
        )
    elif kind == "text_gone":
        operation = lambda: page.get_by_text(value).wait_for(
            state="hidden",
            timeout=session.action_timeout_ms,
        )
    else:
        operation = lambda: page.wait_for_load_state(
            value,
            timeout=session.navigation_timeout_ms,
        )

    _dispatch(session, operation)
    return _fresh_snapshot(session, message)


def _one_line(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")


def tabs(
    session: BrowserSession,
    action: str,
    tab_id: Optional[str] = None,
    url: Optional[str] = None,
) -> Dict[str, Any]:
    if not isinstance(action, str) or action not in {"list", "new", "select", "close"}:
        raise AgentError("invalid_argument", "action must be list, new, select, or close")

    if action == "new":
        if tab_id is not None:
            raise AgentError("invalid_argument", "new does not accept tab_id")
        if url is not None:
            url = _string("url", url, 1, 8192)
    elif action == "select":
        if not isinstance(tab_id, str) or re.fullmatch(r"^t[1-9][0-9]*$", tab_id) is None:
            raise AgentError("invalid_argument", "tab_id must have the form t1")
        if url is not None:
            raise AgentError("invalid_argument", "select does not accept url")
    elif action == "close":
        if tab_id is not None and (
            not isinstance(tab_id, str) or re.fullmatch(r"^t[1-9][0-9]*$", tab_id) is None
        ):
            raise AgentError("invalid_argument", "tab_id must have the form t1")
        if url is not None:
            raise AgentError("invalid_argument", "close does not accept url")

    if action == "list":
        if tab_id is not None or url is not None:
            raise AgentError("invalid_argument", "list does not accept tab_id or url")
        with session.dialog_action_scope():
            entries = session.list_tabs()
        lines = ["tabs"]
        for entry in entries:
            marker = "*" if entry["active"] else " "
            lines.append(
                "%s %s\t%s\t%s"
                % (
                    marker,
                    entry["tab_id"],
                    _one_line(entry["title"]),
                    _one_line(entry["url"]),
                )
            )
        return {"message": "\n".join(lines)}

    current_registry = session.active_registry()
    _invalidate(current_registry)
    if action == "new":
        created = _dispatch(session, lambda: session.new_tab(url))
        return _fresh_snapshot(session, "opened tab %s" % created)

    if action == "select":
        selected = _dispatch(session, lambda: session.select_tab(tab_id))
        return _fresh_snapshot(session, "selected tab %s" % selected)

    if tab_id is None:
        tab_id = session.active_tab_id
    if not isinstance(tab_id, str) or re.fullmatch(r"^t[1-9][0-9]*$", tab_id) is None:
        raise AgentError("invalid_argument", "tab_id must have the form t1")
    closed = _dispatch(session, lambda: session.close_tab(tab_id))
    return _fresh_snapshot(session, "closed tab %s" % closed)


def handle_dialog(
    session: BrowserSession,
    action: str,
    prompt_text: Optional[str] = None,
) -> Dict[str, Any]:
    if not isinstance(action, str) or action not in {"accept", "dismiss"}:
        raise AgentError("invalid_argument", "action must be accept or dismiss")
    if prompt_text is not None:
        prompt_text = _string("prompt_text", prompt_text, 0, 200000)
    if action == "dismiss" and prompt_text is not None:
        raise AgentError("invalid_argument", "prompt_text is only valid when accepting")
    session.arm_dialog(action, prompt_text)
    return {"message": "armed one-shot dialog %s" % action}


def take_screenshot(
    session: BrowserSession,
    *,
    ref: Optional[str] = None,
    type: str = "png",
    full_page: bool = False,
    quality: Optional[int] = None,
) -> Dict[str, Any]:
    if not isinstance(type, str) or type not in {"png", "jpeg"}:
        raise AgentError("invalid_argument", "type must be png or jpeg")
    full_page = _boolean("full_page", full_page)
    if quality is not None:
        quality = _integer("quality", quality, 0, 100)
        if type != "jpeg":
            raise AgentError("invalid_argument", "quality is only valid for jpeg screenshots")
    if ref is not None and full_page:
        raise AgentError("invalid_argument", "ref and full_page cannot be used together")

    page = session.active_page()
    options = {"type": type, "timeout": session.action_timeout_ms}  # type: Dict[str, Any]
    if quality is not None:
        options["quality"] = quality
    if ref is None:
        options["full_page"] = full_page
        image = _dispatch(session, lambda: page.screenshot(**options))
    else:
        locator = resolve(page, session.active_registry(), ref)
        image = _dispatch(session, lambda: locator.screenshot(**options))

    if not isinstance(image, bytes):
        raise AgentError("screenshot_failed", "The screenshot did not return image bytes")
    return {
        "message": "captured screenshot",
        "image": image,
        "mime_type": "image/%s" % type,
    }


def close(session: BrowserSession) -> Dict[str, Any]:
    session.close()
    return {"message": "closed browser session; the next browser action starts a new session"}


def evaluate(session: BrowserSession, expression: str) -> Dict[str, Any]:
    if not session.allow_eval:
        raise AgentError(
            "eval_disabled",
            "Browser evaluation is disabled",
            "Start the server with evaluation explicitly enabled.",
        )
    expression = _string("expression", expression, 1, 200000)
    page = session.active_page()
    _invalidate(session.active_registry())
    value = _dispatch(session, lambda: page.evaluate(expression))
    result = _fresh_snapshot(session, "evaluated expression")
    result["value"] = value
    return result
