from __future__ import annotations

import asyncio

import pytest

import rustwright


rustwright.enable_playwright_compat()

import playwright.async_api as async_api
import playwright.sync_api as sync_api


class _ContextBlockError(RuntimeError):
    pass


class _CloseError(RuntimeError):
    pass


class _SyncPageCloseStub(sync_api.Page):
    def __init__(self, *, closed: bool = False, close_error: BaseException | None = None) -> None:
        self.closed = closed
        self.close_error = close_error
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.closed:
            return
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


class _AsyncPageCloseStub(async_api.Page):
    def __init__(self, *, closed: bool = False, close_error: BaseException | None = None) -> None:
        self.closed = closed
        self.close_error = close_error
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        if self.closed:
            return
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


def test_sync_playwright_handle_stops_and_propagates_exceptions() -> None:
    manager = sync_api.sync_playwright()

    with pytest.raises(_ContextBlockError):
        with manager as playwright:
            assert manager._playwright is playwright
            assert not hasattr(playwright, "__enter__")
            raise _ContextBlockError

    assert manager._playwright is None


def test_sync_browser_context_manager_closes_and_propagates_exceptions() -> None:
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        with pytest.raises(_ContextBlockError):
            with browser as entered:
                assert entered is browser
                raise _ContextBlockError

        assert not browser.is_connected()


def test_sync_browser_context_context_manager_closes_and_propagates_exceptions() -> None:
    with sync_api.sync_playwright() as playwright:
        with playwright.chromium.launch(headless=True) as browser:
            context = browser.new_context()

            with pytest.raises(_ContextBlockError):
                with context as entered:
                    assert entered is context
                    raise _ContextBlockError

            assert context.is_closed()


def test_sync_chromium_browser_context_alias_has_context_manager_protocol() -> None:
    assert sync_api.ChromiumBrowserContext is sync_api.BrowserContext
    assert hasattr(sync_api.ChromiumBrowserContext, "__enter__")
    assert hasattr(sync_api.ChromiumBrowserContext, "__exit__")


def test_sync_page_context_manager_closes_only_page_and_propagates_exceptions() -> None:
    with sync_api.sync_playwright() as playwright:
        with playwright.chromium.launch(headless=True) as browser:
            with browser.new_context() as context:
                page = context.new_page()

                with pytest.raises(_ContextBlockError):
                    with page as entered:
                        assert entered is page
                        raise _ContextBlockError

                assert page.is_closed()
                assert not context.is_closed()


def test_sync_page_exit_accepts_upstream_keyword_names() -> None:
    page = _SyncPageCloseStub()

    page.__exit__(exc_type=None, exc_val=None, _traceback=None)

    assert page.close_calls == 1


def test_sync_page_normal_exit_closes_exactly_once() -> None:
    page = _SyncPageCloseStub()

    with page as entered:
        assert entered is page

    assert page.closed
    assert page.close_calls == 1


def test_sync_page_close_error_replaces_body_error_with_context() -> None:
    page = _SyncPageCloseStub(close_error=_CloseError())

    with pytest.raises(_CloseError) as exc_info:
        with page:
            raise _ContextBlockError

    assert isinstance(exc_info.value.__context__, _ContextBlockError)
    assert page.close_calls == 1


def test_sync_already_closed_page_exit_is_a_noop() -> None:
    page = _SyncPageCloseStub(closed=True)

    with page:
        pass

    assert page.closed
    assert page.close_calls == 1


def test_sync_nested_page_contexts_each_call_close() -> None:
    page = _SyncPageCloseStub()

    with page:
        with page:
            pass

    assert page.closed
    assert page.close_calls == 2


def test_async_playwright_handle_stops_and_propagates_exceptions() -> None:
    async def run() -> None:
        manager = async_api.async_playwright()

        with pytest.raises(_ContextBlockError):
            async with manager as playwright:
                assert manager._playwright is playwright
                assert not hasattr(playwright, "__aenter__")
                raise _ContextBlockError

        assert manager._playwright is None

    asyncio.run(run())


def test_async_browser_context_manager_closes_and_propagates_exceptions() -> None:
    async def run() -> None:
        async with async_api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)

            with pytest.raises(_ContextBlockError):
                async with browser as entered:
                    assert entered is browser
                    raise _ContextBlockError

            assert not browser.is_connected()

    asyncio.run(run())


def test_async_browser_context_context_manager_closes_and_propagates_exceptions() -> None:
    async def run() -> None:
        async with async_api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            async with browser:
                context = await browser.new_context()

                with pytest.raises(_ContextBlockError):
                    async with context as entered:
                        assert entered is context
                        raise _ContextBlockError

                assert context.is_closed()

    asyncio.run(run())


def test_async_chromium_browser_context_alias_has_context_manager_protocol() -> None:
    assert async_api.ChromiumBrowserContext is async_api.BrowserContext
    assert hasattr(async_api.ChromiumBrowserContext, "__aenter__")
    assert hasattr(async_api.ChromiumBrowserContext, "__aexit__")


def test_async_page_context_manager_closes_only_page_and_propagates_exceptions() -> None:
    async def run() -> None:
        async with async_api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            async with browser:
                context = await browser.new_context()
                async with context:
                    page = await context.new_page()

                    with pytest.raises(_ContextBlockError):
                        async with page as entered:
                            assert entered is page
                            raise _ContextBlockError

                    assert page.is_closed()
                    assert not context.is_closed()

    asyncio.run(run())


def test_async_page_exit_accepts_upstream_keyword_names() -> None:
    async def run() -> None:
        page = _AsyncPageCloseStub()

        await page.__aexit__(exc_type=None, exc_val=None, traceback=None)

        assert page.close_calls == 1

    asyncio.run(run())


def test_async_page_normal_exit_closes_exactly_once() -> None:
    async def run() -> None:
        page = _AsyncPageCloseStub()

        async with page as entered:
            assert entered is page

        assert page.closed
        assert page.close_calls == 1

    asyncio.run(run())


def test_async_page_close_error_replaces_body_error_with_context() -> None:
    async def run() -> None:
        page = _AsyncPageCloseStub(close_error=_CloseError())

        with pytest.raises(_CloseError) as exc_info:
            async with page:
                raise _ContextBlockError

        assert isinstance(exc_info.value.__context__, _ContextBlockError)
        assert page.close_calls == 1

    asyncio.run(run())


def test_async_already_closed_page_exit_is_a_noop() -> None:
    async def run() -> None:
        page = _AsyncPageCloseStub(closed=True)

        async with page:
            pass

        assert page.closed
        assert page.close_calls == 1

    asyncio.run(run())


def test_async_nested_page_contexts_each_call_close() -> None:
    async def run() -> None:
        page = _AsyncPageCloseStub()

        async with page:
            async with page:
                pass

        assert page.closed
        assert page.close_calls == 2

    asyncio.run(run())
