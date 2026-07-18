"""Unit tests for strict ref-or-selector target resolution."""

import pytest

from rustwright_mcp import server
from rustwright_mcp.session import SessionState


class FakeLocator:
    def __init__(self, selector, count):
        self.selector = selector
        self._count = count

    def count(self):
        return self._count


class FakePage:
    def __init__(self, counts=None):
        self.counts = counts or {}
        self.locators = []

    def locator(self, selector):
        locator = FakeLocator(selector, self.counts.get(selector, 0))
        self.locators.append(locator)
        return locator


def test_ref_requires_current_snapshot(monkeypatch):
    state = SessionState()
    page = FakePage()
    monkeypatch.setattr(server, "_state", state)

    with pytest.raises(ValueError) as error:
        server._resolve(page, "e1")
    assert str(error.value) == "No current snapshot; call browser_snapshot first."


def test_ref_must_be_in_current_registry(monkeypatch):
    state = SessionState()
    page = FakePage()
    state.record_snapshot(page, ["e2"], 3)
    monkeypatch.setattr(server, "_state", state)

    with pytest.raises(ValueError) as error:
        server._resolve(page, "e1")
    assert str(error.value) == (
        "Ref e1 is not in the current page snapshot; take a fresh snapshot."
    )


def test_selector_must_match_at_least_one_element(monkeypatch):
    page = FakePage({"#missing": 0})
    monkeypatch.setattr(server, "_state", SessionState())

    with pytest.raises(ValueError) as error:
        server._resolve(page, "#missing")
    assert str(error.value) == "Target selector matched no elements: #missing"


def test_selector_must_be_unique(monkeypatch):
    page = FakePage({"button": 3})
    monkeypatch.setattr(server, "_state", SessionState())

    with pytest.raises(ValueError) as error:
        server._resolve(page, "button")
    assert str(error.value) == (
        "Target selector matched 3 elements; provide a unique selector: button"
    )


def test_ref_and_selector_success_return_typed_targets(monkeypatch):
    state = SessionState()
    page = FakePage({"#unique": 1, "e0": 1})
    state.record_snapshot(page, ["e7"], 8)
    monkeypatch.setattr(server, "_state", state)

    ref = server._resolve(page, "e7", element_description="Submit button")
    assert ref.source == "ref"
    assert ref.display_name == "Submit button"
    assert ref.locator.selector == '[data-mcp-ref="e7"]'

    selector = server._resolve(page, "#unique")
    assert selector.source == "selector"
    assert selector.display_name == "#unique"
    assert selector.locator.selector == "#unique"

    assert server._resolve(page, "e0").source == "selector"
