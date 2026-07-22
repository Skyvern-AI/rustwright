"""Adversarial validation of session event plumbing and eviction semantics."""

from __future__ import annotations

import threading

import pytest

from rustwright_mcp import server
from rustwright_mcp.session import SessionState


class FakePage:
    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}
        self.main_frame = object()
        self.goto_started = threading.Event()
        self.allow_goto = threading.Event()

    def on(self, event, callback) -> None:
        self.handlers.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback) -> None:
        self.handlers[event].remove(callback)

    def emit(self, event, *args) -> None:
        for callback in list(self.handlers.get(event, ())):
            callback(*args)

    def goto(self, url) -> None:
        self.goto_started.set()
        assert self.allow_goto.wait(2), f"slow navigation never released: {url}"


class MutableConsole:
    def __init__(self) -> None:
        self.type = "warning"
        self.text = "captured"
        self.location = {
            "url": "https://example.test",
            "position": {"line": 7, "columns": [1, 2]},
        }


class MutableRequest:
    def __init__(self, page: FakePage, url: str) -> None:
        self.method = "POST"
        self.url = url
        self.resource_type = "fetch"
        self.headers = {
            "accept": "application/json",
            "trace": {"parts": ["original"]},
        }
        self.post_data = "{}"
        self.failure = None
        self.frame = page.main_frame

    def is_navigation_request(self) -> bool:
        return False


class MutableResponse:
    def __init__(self, request: MutableRequest) -> None:
        self.request = request
        self.status = 200
        self.headers = {"content-type": "application/json"}


class FakeDialog:
    def __init__(self) -> None:
        self.dismissed = False
        self.type = "alert"
        self.message = "captured"

    def dismiss(self) -> None:
        self.dismissed = True


def test_event_metadata_is_copied_before_source_mutation() -> None:
    state = SessionState()
    page = FakePage()
    registry = state.register_page_handlers(page)
    message = MutableConsole()
    request = MutableRequest(page, "https://example.test/api")
    response = MutableResponse(request)

    page.emit("console", message)
    page.emit("request", request)
    page.emit("response", response)

    message.location["url"] = "https://mutated.invalid"
    message.location["position"]["line"] = 99
    message.location["position"]["columns"].append(3)
    request.headers["accept"] = "text/plain"
    request.headers["trace"]["parts"].append("mutated")
    response.headers["content-type"] = "text/plain"

    console = registry.console_records[0]
    network = registry.network_records[0]
    assert console.location == (
        ("position", (("columns", (1, 2)), ("line", 7))),
        ("url", "https://example.test"),
    )
    assert network.headers == (
        ("accept", "application/json"),
        ("trace", (("parts", ("original",)),)),
    )
    assert network.response_headers == (("content-type", "application/json"),)


def test_callback_flood_completes_while_slow_tool_holds_tool_lock(monkeypatch) -> None:
    state = SessionState(console_quota=200, network_quota=200)
    page = FakePage()
    registry = state.register_page_handlers(page)
    monkeypatch.setattr(server, "_state", state)
    monkeypatch.setattr(server, "_page", lambda: page)
    monkeypatch.setattr(server, "_snapshot", lambda current: "snapshot")
    result: list[str] = []
    failures: list[BaseException] = []

    def navigate() -> None:
        try:
            result.append(server.browser_navigate("https://slow.example.test"))
        except BaseException as exc:  # pragma: no cover - assertion reports it
            failures.append(exc)

    worker = threading.Thread(target=navigate)
    worker.start()
    assert page.goto_started.wait(1), "tool did not acquire the lock and enter goto"

    dialog = FakeDialog()
    page.emit("dialog", dialog)
    for index in range(100):
        page.emit("console", MutableConsole())
        page.emit(
            "request",
            MutableRequest(page, f"https://example.test/request/{index}"),
        )

    assert not dialog.dismissed
    assert registry.pending_dialog is dialog
    assert len(registry.console_records) == 100
    assert len(registry.network_records) == 100

    page.allow_goto.set()
    worker.join(timeout=2)
    assert not worker.is_alive(), "tool/callback lock inversion deadlocked"
    assert failures == []
    assert len(result) == 1
    assert "### Snapshot\nsnapshot" in result[0]
    assert "### Modal" in result[0]


def test_late_response_does_not_resurrect_an_evicted_network_record() -> None:
    """An evicted request keeps its original unavailable index and stays evicted."""
    state = SessionState(network_quota=1)
    page = FakePage()
    registry = state.register_page_handlers(page)
    evicted = MutableRequest(page, "https://example.test/evicted")
    retained = MutableRequest(page, "https://example.test/retained")

    page.emit("request", evicted)
    page.emit("request", retained)
    assert [(record.index, record.url) for record in registry.network_records] == [
        (2, retained.url)
    ]

    # The response/body for index 1 arrives after index 1 was quota-evicted.
    # It must be ignored rather than reappearing under a different stable index.
    page.emit("response", MutableResponse(evicted))
    assert [(record.index, record.url) for record in registry.network_records] == [
        (2, retained.url)
    ]


def test_page_registers_live_remote_once_then_fails_loudly_when_it_dies(
    monkeypatch,
) -> None:
    class RemotePage(FakePage):
        dead = False

        def evaluate(self, expression):
            if self.dead:
                raise RuntimeError("transport closed")
            return 1

    class Closeable:
        closed = False

        def close(self) -> None:
            self.closed = True

    class Stoppable:
        stopped = False

        def stop(self) -> None:
            self.stopped = True

    page = RemotePage()
    browser = Closeable()
    runtime = Stoppable()
    state = SessionState(
        pw=runtime,
        browser=browser,
        context=object(),
        page=page,
        remote=True,
    )
    monkeypatch.setattr(server, "_state", state)

    assert server._page() is page
    assert server._page() is page
    assert all(len(callbacks) == 1 for callbacks in page.handlers.values())

    page.dead = True
    with pytest.raises(RuntimeError) as error:
        server._page()
    assert str(error.value) == (
        "Remote CDP session is no longer reachable — reconnect/restart the MCP server."
    )
    assert browser.closed
    assert runtime.stopped
    assert state.page is None
