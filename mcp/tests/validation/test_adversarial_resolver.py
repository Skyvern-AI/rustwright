"""Hostile ref and selector-race validation."""

from __future__ import annotations

import pytest

from rustwright.sync_api import sync_playwright

from rustwright_mcp import server
from rustwright_mcp.session import SessionState


@pytest.fixture
def browser_page():
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            yield page
        finally:
            browser.close()


def test_ref_shaped_css_never_falls_through_to_selector(monkeypatch, browser_page) -> None:
    browser_page.set_content("<e999 id='hostile'>hostile selector collision</e999>")
    assert browser_page.locator("e999").count() == 1
    state = SessionState()
    state.record_snapshot(browser_page, [], 1)
    monkeypatch.setattr(server, "_state", state)

    with pytest.raises(ValueError) as error:
        server._resolve(browser_page, "e999")
    assert str(error.value) == (
        "Ref e999 is not in the current page snapshot; take a fresh snapshot."
    )


def test_selector_race_stays_strict_at_action_time(monkeypatch, browser_page) -> None:
    browser_page.set_content(
        "<button class='race' onclick='window.clicks += 1'>first</button>"
        "<script>window.clicks = 0</script>"
    )

    class InjectingLocator:
        def __init__(self, locator) -> None:
            self._locator = locator

        def count(self) -> int:
            count = self._locator.count()
            browser_page.evaluate(
                """() => document.body.insertAdjacentHTML(
                'beforeend',
                '<button class="race" onclick="window.clicks += 1">second</button>'
                )"""
            )
            return count

        def click(self) -> None:
            self._locator.click()

    class InjectingPage:
        def locator(self, selector):
            return InjectingLocator(browser_page.locator(selector))

    resolved = server._resolve(InjectingPage(), ".race")
    with pytest.raises(Exception, match="strict mode violation.*2 elements"):
        resolved.locator.click()
    assert browser_page.evaluate("() => window.clicks") == 0


class TruncatedSnapshotPage:
    def __init__(self, outline: str) -> None:
        self.outline = outline
        self.url = "https://snapshot.example.test"

    def wait_for_load_state(self, timeout) -> None:
        return None

    def evaluate(self, expression, start_ref):
        return {
            "outline": self.outline,
            "refs": ["e1", "e2"],
            "nextRef": 3,
        }

    def title(self) -> str:
        return "Truncation"


def test_ref_hidden_by_character_truncation_is_not_invocable(monkeypatch) -> None:
    outline = "- button visible [ref=e1]\n" + (
        "x" * server.SNAPSHOT_CHAR_LIMIT
    ) + "\n- button hidden [ref=e2]"
    page = TruncatedSnapshotPage(outline)
    state = SessionState()
    monkeypatch.setattr(server, "_state", state)

    delivered = server._snapshot(page)
    assert "[ref=e1]" in delivered
    assert "[ref=e2]" not in delivered
    assert state.snapshot_status(page) == (True, frozenset({"e1"}))
    with pytest.raises(ValueError) as error:
        server._resolve(page, "e2")
    assert str(error.value) == (
        "Ref e2 is not in the current page snapshot; take a fresh snapshot."
    )
