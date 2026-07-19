"""Unit tests for typed session state and browser event plumbing."""

from dataclasses import FrozenInstanceError
import threading

import pytest

from rustwright_mcp import server
from rustwright_mcp.session import SessionState


class FakePage:
    def __init__(self):
        self.handlers = {}
        self.main_frame = object()

    def on(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)

    def emit(self, event, *args):
        for callback in list(self.handlers.get(event, ())):
            callback(*args)


class FakeConsole:
    def __init__(self, text):
        self.type = "log"
        self.text = text
        self.location = {"url": "https://example.test", "lineNumber": 4}


class FakeRequest:
    def __init__(self, page, url, *, navigation=False):
        self.method = "POST"
        self.url = url
        self.resource_type = "document" if navigation else "fetch"
        self.headers = {"accept": "application/json"}
        self.post_data = "{}"
        self.failure = None
        self.frame = page.main_frame
        self._navigation = navigation

    def is_navigation_request(self):
        return self._navigation


class FakeResponse:
    def __init__(self, request):
        self.request = request
        self.status = 201
        self.headers = {"content-type": "application/json"}


class FakeDownload:
    url = "https://example.test/file"
    suggested_filename = "file.txt"


class FakeDialog:
    def __init__(self):
        self.action = None

    def accept(self, prompt_text=None):
        self.action = ("accept", prompt_text)

    def dismiss(self):
        self.action = ("dismiss", None)


def test_registrar_is_once_per_page_and_cleanup_is_per_page():
    state = SessionState()
    page = FakePage()
    state.page = page

    first = state.register_page_handlers(page)
    second = state.register_page_handlers(page)

    assert first is second
    for event in (
        "console",
        "request",
        "response",
        "requestfailed",
        "filechooser",
        "dialog",
        "download",
    ):
        assert len(page.handlers[event]) == 1

    page.emit("close", page)
    assert state.registry_for(page) is None
    assert state.page is None


def test_event_records_are_immutable_bounded_and_epoch_indexed():
    state = SessionState(console_quota=2, network_quota=2, download_quota=1)
    page = FakePage()
    registry = state.register_page_handlers(page)

    page.emit("console", FakeConsole("one"))
    page.emit("console", FakeConsole("two"))
    page.emit("console", FakeConsole("three"))
    assert [record.text for record in registry.console_records] == ["two", "three"]
    with pytest.raises(FrozenInstanceError):
        registry.console_records[-1].text = "changed"

    navigation = FakeRequest(page, "https://example.test/", navigation=True)
    request = FakeRequest(page, "https://example.test/api")
    page.emit("request", navigation)
    page.emit("request", request)
    page.emit("response", FakeResponse(request))

    assert [(item.epoch, item.index) for item in registry.network_records] == [
        (1, 1),
        (1, 2),
    ]
    assert registry.network_records[-1].response_status == 201
    assert registry.network_records[-1].response is not None
    assert registry.network_records[-1].headers == (("accept", "application/json"),)

    page.emit("framenavigated", page.main_frame)
    page.emit("framenavigated", page.main_frame)
    next_epoch = FakeRequest(page, "https://example.test/next")
    page.emit("request", next_epoch)
    assert [(item.epoch, item.index) for item in registry.network_records] == [
        (1, 2),
        (2, 3),
    ]

    chooser = object()
    page.emit("filechooser", chooser)
    page.emit("download", FakeDownload())
    assert registry.pending_file_chooser is chooser
    assert registry.downloads[-1].download.__class__ is FakeDownload


def test_dialog_slot_stays_pending_without_automatic_action():
    state = SessionState()
    page = FakePage()
    registry = state.register_page_handlers(page)

    pending = FakeDialog()
    page.emit("dialog", pending)
    assert registry.pending_dialog is pending
    assert pending.action is None
    state.clear_pending_dialog(page, pending)
    assert registry.pending_dialog is None


def test_event_callback_never_waits_for_tool_lock(monkeypatch):
    state = SessionState()
    page = FakePage()
    monkeypatch.setattr(server, "_state", state)
    server._register_page_handlers(page)
    completed = threading.Event()

    def tool_action():
        with server._lock:
            page.emit("console", FakeConsole("during tool"))
            completed.set()

    worker = threading.Thread(target=tool_action)
    worker.start()
    worker.join(timeout=1)

    assert completed.is_set(), "event callback attempted to acquire the tool lock"
    assert not worker.is_alive()
