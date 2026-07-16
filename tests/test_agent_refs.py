from urllib.parse import quote

import pytest

from rustwright._agent.errors import AgentError
from rustwright._agent.refs import RefAllocator, RefRegistry, resolve
from rustwright._agent.snapshot_js import REF_HOOK, assert_hook_unique, build_tagging_expression
from rustwright.sync_api import Locator, sync_playwright


def _data_url(markup):
    return "data:text/html;charset=utf-8," + quote(markup)


@pytest.fixture
def browser_page():
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            yield page
        finally:
            browser.close()


def _ref_for(snapshot, role, name):
    for item in snapshot["refs"]:
        if item["role"] == role and item["name"] == name:
            return item["ref"]
    raise AssertionError("missing ref for %s %r" % (role, name))


def test_hook_unique():
    helper = Locator._aria_snapshot_helper_function_script()
    assert helper.count(REF_HOOK) == 1
    assert_hook_unique()


def test_tagged_snapshot_byte_identical_to_builtin(browser_page):
    browser_page.goto(
        _data_url(
            """
            <main aria-label="Fixture">
              <h1>Overview</h1>
              <section><button>Save</button><input aria-label="Query"></section>
              <a href="/guide">Guide</a>
              <img alt="Diagram">
            </main>
            """
        )
    )
    expected = browser_page.aria_snapshot(depth=8, mode="ai")
    expression = build_tagging_expression(
        attr="data-rustwright-ref",
        generation="fixture:g1",
        start_at=0,
        max_refs=1000,
        max_chars=50000,
        mask_password_values=False,
    )
    actual = browser_page.evaluate(expression)
    assert actual["text"] == expected
    assert actual["truncated"] is False


def test_round_trip_fill_click(browser_page):
    browser_page.goto(
        _data_url(
            """
            <label>Message <input id="message"></label>
            <button onclick="document.body.dataset.clicked = 'yes'">Apply</button>
            """
        )
    )
    registry = RefRegistry()
    snapshot = registry.snapshot(browser_page)

    textbox_ref = _ref_for(snapshot, "textbox", "Message")
    resolve(browser_page, registry, textbox_ref).fill("updated")
    assert browser_page.locator("#message").input_value() == "updated"

    button_ref = _ref_for(snapshot, "button", "Apply")
    resolve(browser_page, registry, button_ref).click()
    assert browser_page.evaluate("() => document.body.dataset.clicked") == "yes"


def test_registries_share_one_session_wide_allocator(browser_page):
    allocator = RefAllocator()
    first = RefRegistry(allocator)
    second = RefRegistry(allocator)
    browser_page.goto(_data_url("<button>First</button>"))
    first_snapshot = first.snapshot(browser_page)
    browser_page.goto(_data_url("<button>Second</button>"))
    second_snapshot = second.snapshot(browser_page)

    first_ref = _ref_for(first_snapshot, "button", "First")
    second_ref = _ref_for(second_snapshot, "button", "Second")
    assert first_ref == "e1"
    assert second_ref == "e1001"


def test_stale_after_navigation(browser_page):
    browser_page.goto(_data_url("<button>Continue</button>"))
    registry = RefRegistry()
    snapshot = registry.snapshot(browser_page)
    old_ref = _ref_for(snapshot, "button", "Continue")

    browser_page.goto(_data_url("<h1>Elsewhere</h1>"))
    with pytest.raises(AgentError) as exc_info:
        resolve(browser_page, registry, old_ref)
    assert exc_info.value.code == "stale_ref"


def test_password_masked(browser_page):
    secret = "value-that-must-not-appear"
    browser_page.goto(_data_url('<input type="password" aria-label="Password">'))
    browser_page.locator("input").fill(secret)

    snapshot = RefRegistry().snapshot(browser_page, mask_password_values=True)
    assert secret not in snapshot["text"]
    assert "\u2022\u2022\u2022\u2022\u2022\u2022" in snapshot["text"]


def test_truncation_authoritative_refs(browser_page):
    browser_page.goto(
        _data_url(
            """
            <button aria-label="First">[ref=e2]</button>
            <button aria-label="Second">Second</button>
            """
        )
    )
    full_lines = browser_page.aria_snapshot(depth=8, mode="ai").splitlines()
    second_line = next(
        index for index, line in enumerate(full_lines) if 'button "Second" [ref=e2]' in line
    )
    retained = "\n".join(full_lines[:second_line])
    max_chars = len(retained) + len("\n... [snapshot truncated]")

    snapshot = RefRegistry().snapshot(browser_page, max_chars=max_chars)
    assert snapshot["truncated"] is True
    assert "[ref=e2]" in snapshot["text"]
    assert {item["ref"] for item in snapshot["refs"]} == {"e1"}


def test_wide_page_stops_within_snapshot_budget(browser_page):
    markup = "<main>" + "".join("<p>row %d</p>" % index for index in range(8000)) + "</main>"
    browser_page.goto(_data_url(markup))

    snapshot = RefRegistry().snapshot(browser_page, max_chars=1000)

    assert snapshot["truncated"] is True
    assert len(snapshot["text"]) <= 1000
    assert snapshot["text"].endswith("... [snapshot truncated]")


def test_cleanup_selector_targets_only_tagged_nodes():
    expression = build_tagging_expression(
        attr="data-rustwright-ref",
        generation="fixture:g1",
        start_at=0,
        max_refs=1000,
        max_chars=50000,
        mask_password_values=True,
    )
    assert "document.querySelectorAll('*')" not in expression
    assert "document.querySelectorAll('[' + attr + ']')" in expression


def test_invalid_ref_shape():
    with pytest.raises(AgentError) as exc_info:
        resolve(None, RefRegistry(), "nope")
    assert exc_info.value.code == "invalid_ref"
