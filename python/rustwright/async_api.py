from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import functools
import inspect
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Union

from . import _rustwright
from .sync_api import (
    APIRequest as SyncAPIRequest,
    APIRequestContext as SyncAPIRequestContext,
    APIResponse as SyncAPIResponse,
    BackendMarker,
    BrowserBindResult,
    Browser as SyncBrowser,
    BrowserContext as SyncBrowserContext,
    BrowserType as SyncBrowserType,
    CDPSession as SyncCDPSession,
    Cookie,
    ConsoleMessage as SyncConsoleMessage,
    Debugger as SyncDebugger,
    DebuggerLocation,
    DebuggerPausedDetails,
    Dialog as SyncDialog,
    Download as SyncDownload,
    ElementHandle as SyncElementHandle,
    Error,
    Expect,
    FileChooser as SyncFileChooser,
    FilePayload,
    FloatRect,
    Frame as SyncFrame,
    Geolocation,
    HttpCredentials,
    JSHandle as SyncJSHandle,
    Page as SyncPage,
    PageAssertionsImpl as SyncPageAssertionsImpl,
    PdfMargins,
    Position,
    ProxySettings,
    Request as SyncRequest,
    ResourceTiming,
    Response as SyncResponse,
    Route as SyncRoute,
    ScreencastFrame,
    SourceLocation,
    StorageState,
    StorageStateCookie,
    TargetClosedError,
    TimeoutError,
    Tracing as SyncTracing,
    ViewportSize,
    Video as SyncVideo,
    WebError as SyncWebError,
    WebSocket as SyncWebSocket,
    WebSocketRoute as SyncWebSocketRoute,
    Worker as SyncWorker,
    APIResponseAssertionsImpl as SyncAPIResponseAssertionsImpl,
    LocatorAssertionsImpl as SyncLocatorAssertionsImpl,
    _Expectation as SyncExpectation,
    _MISSING,
    _UNSET,
    backend_marker,
    _decode_json_result,
    _default_timeout_for_method,
    _emit_event,
    _event_handler_positional_args,
    _json,
    _is_ignorable_close_error,
    _navigation_timeout_for_method,
    _normalize_action_boolean,
    _normalize_lifecycle_state,
    _normalize_path_arg,
    _normalize_required_string_argument,
    _normalize_screenshot_options,
    _normalize_selector_option,
    _normalize_string_option,
    _normalize_wait_for_selector_state,
    _options_from_explicit_kwargs,
    _response_from_payload,
    _translate_error,
    _unsafe_dom_fastpath_enabled,
    _validate_timeout_value,
)
from .sync_api import sync_playwright as _sync_playwright
from ._async_generated import (
    _AsyncAPIRequestContextGeneratedMixin,
    _AsyncAPIRequestGeneratedMixin,
    _AsyncAPIResponseGeneratedMixin,
    _AsyncAccessibilityGeneratedMixin,
    _AsyncBrowserContextGeneratedMixin,
    _AsyncBrowserGeneratedMixin,
    _AsyncBrowserTypeGeneratedMixin,
    _AsyncCDPSessionGeneratedMixin,
    _AsyncClockGeneratedMixin,
    _AsyncDebuggerGeneratedMixin,
    _AsyncDialogGeneratedMixin,
    _AsyncDownloadGeneratedMixin,
    _AsyncElementHandleGeneratedMixin,
    _AsyncFileChooserGeneratedMixin,
    _AsyncFrameGeneratedMixin,
    _AsyncJSHandleGeneratedMixin,
    _AsyncKeyboardGeneratedMixin,
    _AsyncLocatorGeneratedMixin,
    _AsyncMouseGeneratedMixin,
    _AsyncPageGeneratedMixin,
    _AsyncPlaywrightGeneratedMixin,
    _AsyncRequestGeneratedMixin,
    _AsyncResponseGeneratedMixin,
    _AsyncRouteGeneratedMixin,
    _AsyncScreencastFrameGeneratedMixin,
    _AsyncScreencastGeneratedMixin,
    _AsyncSelectorsGeneratedMixin,
    _AsyncTouchscreenGeneratedMixin,
    _AsyncTracingGeneratedMixin,
    _AsyncVideoGeneratedMixin,
    _AsyncWebSocketRouteGeneratedMixin,
    _AsyncWorkerGeneratedMixin,
)


if hasattr(asyncio, "to_thread"):
    _DEFAULT_ASYNCIO_TO_THREAD = asyncio.to_thread
else:
    # asyncio.to_thread was added in Python 3.9. Preserve the same behavior on
    # Python 3.8 by running the call in the loop's default executor with the
    # current context copied, exactly as asyncio.to_thread does.
    async def _DEFAULT_ASYNCIO_TO_THREAD(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        ctx = contextvars.copy_context()
        call = functools.partial(ctx.run, func, *args, **kwargs)
        return await loop.run_in_executor(None, call)
_ASYNC_EXECUTOR_LOCK = threading.Lock()
_ASYNC_EXECUTOR: Optional[concurrent.futures.Executor] = None
_ASYNC_EXECUTOR_OWNS = False


def configure_async_executor(
    *,
    max_workers: Optional[int] = None,
    executor: Optional[concurrent.futures.Executor] = None,
    thread_name_prefix: str = "rustwright-async",
    shutdown_existing: bool = True,
) -> None:
    """Configure the executor used by async wrappers for blocking sync calls.

    By default Rustwright preserves asyncio.to_thread behavior, which uses the
    event loop's default ThreadPoolExecutor. Passing max_workers installs a
    Rustwright-owned executor; passing executor installs a caller-owned one.
    Passing neither resets Rustwright to the default asyncio executor.
    """
    if max_workers is not None and executor is not None:
        raise ValueError("configure_async_executor accepts max_workers or executor, not both")
    if max_workers is not None and max_workers < 1:
        raise ValueError("max_workers must be >= 1")

    new_executor = executor
    owns_new_executor = False
    if max_workers is not None:
        new_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )
        owns_new_executor = True

    old_executor: Optional[concurrent.futures.Executor] = None
    global _ASYNC_EXECUTOR, _ASYNC_EXECUTOR_OWNS
    with _ASYNC_EXECUTOR_LOCK:
        if shutdown_existing and _ASYNC_EXECUTOR_OWNS:
            old_executor = _ASYNC_EXECUTOR
        _ASYNC_EXECUTOR = new_executor
        _ASYNC_EXECUTOR_OWNS = owns_new_executor
    if old_executor is not None:
        # cancel_futures was added in Python 3.9; the default is False, so omit
        # it to keep this call working on Python 3.8.
        old_executor.shutdown(wait=False)


def async_executor_info() -> dict[str, Any]:
    with _ASYNC_EXECUTOR_LOCK:
        executor = _ASYNC_EXECUTOR
        owns_executor = _ASYNC_EXECUTOR_OWNS
    return {
        "configured": executor is not None,
        "owned": owns_executor,
        "max_workers": getattr(executor, "_max_workers", None),
    }


async def _run_sync_call(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    with _ASYNC_EXECUTOR_LOCK:
        executor = _ASYNC_EXECUTOR
    if executor is None:
        return await _DEFAULT_ASYNCIO_TO_THREAD(func, *args, **kwargs)

    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    call = functools.partial(ctx.run, func, *args, **kwargs)
    return await loop.run_in_executor(executor, call)


async def _await_native(awaitable: Any) -> Any:
    try:
        return await awaitable
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise _translate_error(exc) from None


async def _await_native_method(method: str, awaitable: Any) -> Any:
    try:
        return await _await_native(awaitable)
    except Error as exc:
        message = str(exc)
        if message.startswith(f"{method}:"):
            raise
        if isinstance(exc, TimeoutError):
            match = re.fullmatch(r"timed out after ([0-9]+(?:\.[0-9]+)?) ms", message)
            if match:
                raise TimeoutError(f"{method}: Timeout {match.group(1)}ms exceeded.") from None
        error_type = TargetClosedError if isinstance(exc, TargetClosedError) else type(exc)
        raise error_type(f"{method}: {message}") from None


async def _await_native_action(method: str, awaitable: Any) -> Any:
    try:
        return await _await_native(awaitable)
    except Error as exc:
        message = str(exc)
        marker = "__rustwright_action_timeout__:"
        if message.startswith(marker):
            try:
                payload = json.loads(message[len(marker) :])
                info = _decode_json_result(json.loads(str(payload["last_info_json"])))
                info_key = payload.get("last_info_key")
                if info_key is not None:
                    info = info[info_key]
                count = int(info.get("count") or 0) if isinstance(info, dict) else 0
                detail = "no element matched" if count == 0 else f"last state was {info}"
                raise TimeoutError(
                    f"timed out waiting for locator to be {payload['state']} "
                    f"while trying to {payload['action']}; {detail}"
                ) from None
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
        if message.startswith(("Locator.", "strict mode violation:", "Page crashed")):
            raise
        if message.startswith(f"{method}:"):
            raise
        error_type = TargetClosedError if isinstance(exc, TargetClosedError) else type(exc)
        raise error_type(f"{method}: {message}") from None


async def _await_cleanup_completion(awaitable: Any) -> Any:
    """Finish lifecycle cleanup before propagating cancellation to the caller."""
    cleanup_task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            cancelled = True
    result = cleanup_task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


_CLOSE_OPEN = "open"
_CLOSE_CLOSING = "closing"
_CLOSE_CLOSED = "closed"


async def _single_flight_close(sync_obj: Any, cleanup: Callable[[], Any]) -> None:
    """Run one cancellation-safe close task and share its result with every caller."""
    state = getattr(sync_obj, "_rustwright_async_close_state", _CLOSE_OPEN)
    task = getattr(sync_obj, "_rustwright_async_close_task", None)
    if state == _CLOSE_CLOSED:
        return
    if state == _CLOSE_CLOSING and task is not None:
        if task is asyncio.current_task():
            return
        await _await_cleanup_completion(task)
        return

    task = asyncio.create_task(cleanup())
    sync_obj._rustwright_async_close_state = _CLOSE_CLOSING
    sync_obj._rustwright_async_close_task = task
    try:
        await _await_cleanup_completion(task)
    except BaseException:
        if task.done() and not task.cancelled() and task.exception() is None:
            sync_obj._rustwright_async_close_state = _CLOSE_CLOSED
            sync_obj._rustwright_async_close_task = None
        else:
            sync_obj._rustwright_async_close_state = _CLOSE_OPEN
            sync_obj._rustwright_async_close_task = None
        raise
    else:
        sync_obj._rustwright_async_close_state = _CLOSE_CLOSED
        sync_obj._rustwright_async_close_task = None


def _native_normalize_selector(selector: str, *, method: str) -> str:
    missing_method = "_click" if method == "Page.click" else method.rsplit(".", 1)[-1]
    return _normalize_selector_option(
        selector,
        method=method,
        missing_type_error=f"Frame.{missing_method}() missing 1 required positional argument: 'selector'",
    )


def _native_selector_locator(
    sync_page: SyncPage,
    normalized_selector: str,
    strict: Optional[bool],
    *,
    method: str,
) -> Any:
    return sync_page._selector_locator(
        normalized_selector,
        {"strict": strict, "strict_method": method},
    )


def _native_locator(sync_page: SyncPage, selector: str, strict: Optional[bool], *, method: str) -> Any:
    normalized_selector = _native_normalize_selector(selector, method=method)
    return _native_selector_locator(sync_page, normalized_selector, strict, method=method)


def _native_page_options_supported(context: SyncBrowserContext) -> bool:
    if not isinstance(context, SyncBrowserContext):
        return False
    unsupported_context_events = set(context._event_handlers) - {"page", "close"}
    return bool(
        context._core is not None
        and not context._options
        and not context._init_scripts
        and not context._routes
        and not context._har_routes
        and not context._websocket_routes
        and not context._bindings
        and not context._record_har_path
        and not context._record_video_dir
        and not context._clock._installed
        and not unsupported_context_events
    )


def _native_page_hot_path_supported(page: Any) -> bool:
    if not isinstance(page, SyncPage):
        return False
    context = page._context
    browser = context._browser if context is not None else None
    return bool(
        not page._owned_cdp_sessions
        and not page._routes
        and not page._bindings
        and not page._locator_handlers
        and not page._har_recordings
        and not page._fetch_enabled
        and not page._slow_mo_ms
        and (context is None or not context._options)
        and (browser is None or not browser._owned_cdp_sessions)
    )


async def _native_context_page(context: SyncBrowserContext) -> SyncPage:
    core = await _await_native(context._core.new_page_async())
    return await _finish_native_page(context, core)


async def _finish_native_page(context: SyncBrowserContext, core: Any) -> SyncPage:
    page = SyncPage(core, context=context, _start_event_pump=False)
    registered = False
    try:
        await _await_native(core.set_device_metrics_async(1280, 720, 1.0, False, page._default_timeout))
        page._viewport_size = {"width": 1280, "height": 720}
        page.set_default_timeout(context._default_timeout)
        if context._default_navigation_timeout is not None:
            page.set_default_navigation_timeout(context._default_navigation_timeout)
        context._pages.append(page)
        registered = True
        if context._event_handlers.get("page"):
            await _await_cleanup_completion(_run_sync_call(context._ensure_page_popup_bridge, page))
        core.mark_delivered()
        _emit_event(context._event_handlers, "page", page)
        return page
    except BaseException:
        if registered and page in context._pages:
            context._pages.remove(page)
        popup_handler = context._popup_bridge_handlers.pop(page, None)
        if popup_handler is not None:
            page.remove_listener("popup", popup_handler)
        try:
            await _await_cleanup_completion(
                core.close_async(page._default_timeout, False)
            )
        except Exception as exc:
            if not _is_ignorable_close_error(_translate_error(exc)):
                raise
        raise


class _AsyncWrapper:
    def __init__(self, sync_obj: Any):
        source_loop = None
        if isinstance(sync_obj, _AsyncWrapper):
            source_loop = object.__getattribute__(sync_obj, "_loop")
            sync_obj = object.__getattribute__(sync_obj, "_sync")
        self._sync = sync_obj
        if source_loop is not None:
            self._loop = source_loop
        else:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None

    def on(self, event: str, f: Any) -> None:
        self._sync.on(event, _wrap_async_event_handler(self, event, f))

    def once(self, event: str, f: Any) -> None:
        self._sync.once(event, _wrap_async_event_handler(self, event, f))

    def remove_listener(self, event: str, f: Any) -> None:
        self._sync.remove_listener(event, _forget_async_event_handler(self, event, f))

    def __await__(self):
        if False:
            yield None
        return self


class _AwaitableEventValue:
    def __init__(self, value: Any):
        self._value = value

    def __await__(self):
        if False:
            yield None
        return self._value

    def __getattr__(self, name: str) -> Any:
        return getattr(self._value, name)

    def __getitem__(self, key: Any) -> Any:
        return self._value[key]

    def __iter__(self):
        return iter(self._value)

    def __contains__(self, item: Any) -> bool:
        return item in self._value

    def __len__(self) -> int:
        return len(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, _AwaitableEventValue):
            other = other._value
        return self._value == other

    def __hash__(self) -> int:
        return hash(self._value)

    def __repr__(self) -> str:
        return repr(self._value)

    def __str__(self) -> str:
        return str(self._value)


class _AwaitableEventStr(str):
    def __new__(cls, value: str):
        return str.__new__(cls, value)

    def __await__(self):
        if False:
            yield None
        return str(self)


class _AwaitableEventBytes(bytes):
    def __new__(cls, value: bytes):
        return bytes.__new__(cls, value)

    def __await__(self):
        if False:
            yield None
        return bytes(self)


def _awaitable_event_context_value(value: Any) -> Any:
    if isinstance(value, _AsyncWrapper) or inspect.isawaitable(value):
        return value
    if isinstance(value, str):
        return _AwaitableEventStr(value)
    if isinstance(value, bytes):
        return _AwaitableEventBytes(value)
    return _AwaitableEventValue(value)


def _unwrap_async_arg(value: Any) -> Any:
    if isinstance(value, _AsyncWrapper):
        return object.__getattribute__(value, "_sync")
    if isinstance(value, list):
        return [_unwrap_async_arg(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_unwrap_async_arg(item) for item in value)
    if isinstance(value, dict):
        return {key: _unwrap_async_arg(item) for key, item in value.items()}
    return value


class _AsyncEventContextManager:
    def __init__(self, sync_manager: Any, value_mapper: Any = None):
        self._sync_manager = sync_manager
        self._value_mapper = value_mapper

    async def __aenter__(self) -> "_AsyncEventContextManager":
        await _run_sync_call(self._sync_manager.__enter__)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await _run_sync_call(self._sync_manager.__exit__, exc_type, exc, tb)

    @property
    def value(self) -> Any:
        value = self._sync_manager.value
        mapped = self._value_mapper(value) if self._value_mapper else value
        return _awaitable_event_context_value(mapped)


_ASYNC_WAIT_SLICE_MS = 50.0


def _async_wait_default_timeout(sync_owner: Any) -> float:
    page = getattr(sync_owner, "_page", None)
    if page is not None:
        return float(getattr(page, "_default_timeout", 30_000.0))
    return float(getattr(sync_owner, "_default_timeout", 30_000.0))


def _async_wait_timeout_label(timeout: Any) -> str:
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return str(timeout)
    if value.is_integer():
        return str(int(value))
    return str(value)


def _rewrite_sliced_wait_timeout_message(message: str, timeout: Any) -> str:
    timeout_label = _async_wait_timeout_label(timeout)
    rewritten = re.sub(r"Timeout [0-9]+(?:\.[0-9]+)?ms exceeded", f"Timeout {timeout_label}ms exceeded", message, count=1)
    if rewritten != message:
        return rewritten
    return re.sub(r"timed out after [0-9]+(?:\.[0-9]+)? ms", f"Timeout {timeout_label}ms exceeded.", message, count=1)


async def _run_sync_wait_sliced(
    sync_owner: Any,
    wait_func: Callable[..., Any],
    *args: Any,
    timeout: Optional[float] = None,
    **kwargs: Any,
) -> Any:
    raw_timeout = _async_wait_default_timeout(sync_owner) if timeout is None else timeout
    if isinstance(raw_timeout, bool):
        return await _run_sync_call(wait_func, *args, timeout=timeout, **kwargs)
    try:
        total_timeout = float(raw_timeout)
    except (TypeError, ValueError):
        return await _run_sync_call(wait_func, *args, timeout=timeout, **kwargs)
    if total_timeout <= _ASYNC_WAIT_SLICE_MS:
        return await _run_sync_call(wait_func, *args, timeout=timeout, **kwargs)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + (total_timeout / 1000)
    last_timeout: Optional[TimeoutError] = None
    while True:
        remaining = (deadline - loop.time()) * 1000
        if remaining <= 0:
            if last_timeout is not None:
                raise TimeoutError(_rewrite_sliced_wait_timeout_message(str(last_timeout), raw_timeout)) from None
            raise TimeoutError(f"Timeout {_async_wait_timeout_label(raw_timeout)}ms exceeded.")
        try:
            return await _run_sync_call(
                wait_func,
                *args,
                timeout=min(_ASYNC_WAIT_SLICE_MS, remaining),
                **kwargs,
            )
        except TimeoutError as exc:
            last_timeout = exc
            if loop.time() >= deadline:
                raise TimeoutError(_rewrite_sliced_wait_timeout_message(str(exc), raw_timeout)) from None
            await asyncio.sleep(min(0.01, max(deadline - loop.time(), 0.0)))


class AsyncClock(_AsyncClockGeneratedMixin, _AsyncWrapper):
    pass


class AsyncDebugger(_AsyncDebuggerGeneratedMixin, _AsyncWrapper):
    @property
    def paused_details(self) -> Optional[DebuggerPausedDetails]:
        return self._sync.paused_details


class AsyncScreencastFrame(_AsyncScreencastFrameGeneratedMixin, _AsyncWrapper):
    @property
    def data(self) -> bytes:
        return self._sync.data

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(self._sync.metadata)

    @property
    def timestamp(self) -> Optional[float]:
        return self._sync.timestamp

    @property
    def width(self) -> int:
        return self._sync.width

    @property
    def height(self) -> int:
        return self._sync.height


class AsyncScreencast(_AsyncScreencastGeneratedMixin, _AsyncWrapper):
    async def start(self, **kwargs: Any) -> None:
        options = dict(kwargs)
        on_frame = options.get("on_frame")
        if on_frame is not None:
            def wrapper(frame: ScreencastFrame) -> None:
                _run_awaitable(on_frame(AsyncScreencastFrame(frame)))

            options["on_frame"] = wrapper
        await _run_sync_call(self._sync.start, **options)


class AsyncVideo(_AsyncVideoGeneratedMixin, _AsyncWrapper):
    async def save_as(self, path: Any) -> None:
        await _run_sync_call(self._sync._save_as, path, allow_before_close=True)


class AsyncBrowserType(_AsyncBrowserTypeGeneratedMixin, _AsyncWrapper):
    @property
    def name(self) -> str:
        return self._sync.name

    @property
    def executable_path(self) -> str:
        return self._sync.executable_path

    async def launch(
        self,
        *,
        executable_path: Optional[Any] = None,
        channel: Optional[str] = None,
        args: Optional[Any] = None,
        ignore_default_args: Optional[Any] = None,
        handle_sigint: Optional[bool] = None,
        handle_sigterm: Optional[bool] = None,
        handle_sighup: Optional[bool] = None,
        timeout: Optional[float] = None,
        env: Optional[dict[str, Any]] = None,
        headless: Optional[bool] = None,
        proxy: Optional[dict[str, Any]] = None,
        downloads_path: Optional[Any] = None,
        slow_mo: Optional[float] = None,
        traces_dir: Optional[Any] = None,
        artifacts_dir: Optional[Any] = None,
        chromium_sandbox: Optional[bool] = None,
        firefox_user_prefs: Optional[dict[str, Any]] = None,
    ) -> "AsyncBrowser":
        if (
            not isinstance(self._sync, SyncBrowserType)
            or self._sync.name != "chromium"
            or traces_dir is not None
            or artifacts_dir is not None
            or firefox_user_prefs is not None
        ):
            return _wrap_async_browser(
                await _run_sync_call(
                    self._sync.launch,
                    executable_path=executable_path,
                    channel=channel,
                    args=args,
                    ignore_default_args=ignore_default_args,
                    handle_sigint=handle_sigint,
                    handle_sigterm=handle_sigterm,
                    handle_sighup=handle_sighup,
                    timeout=timeout,
                    env=env,
                    headless=headless,
                    proxy=proxy,
                    downloads_path=downloads_path,
                    slow_mo=slow_mo,
                    traces_dir=traces_dir,
                    artifacts_dir=artifacts_dir,
                    chromium_sandbox=chromium_sandbox,
                    firefox_user_prefs=firefox_user_prefs,
                )
            )
        options, launch_options = self._sync._normalize_launch_options(
            headless=headless,
            executable_path=executable_path,
            channel=channel,
            args=args,
            timeout=timeout,
            env=env,
            chromium_sandbox=chromium_sandbox,
            proxy=proxy,
            user_data_dir=None,
            downloads_path=downloads_path,
            slow_mo=slow_mo,
            ignore_default_args=ignore_default_args,
            handle_sigint=handle_sigint,
            handle_sigterm=handle_sigterm,
            handle_sighup=handle_sighup,
            method="BrowserType.launch",
        )
        core = await _await_native(
            _rustwright.launch_chromium_async(json.dumps(options, separators=(",", ":")))
        )
        return _wrap_async_browser(SyncBrowser(core, launch_options=launch_options))


class AsyncSelectors(_AsyncSelectorsGeneratedMixin, _AsyncWrapper):
    def set_test_id_attribute(self, attribute_name: str) -> None:
        self._sync.set_test_id_attribute(attribute_name)


class AsyncPlaywright(_AsyncPlaywrightGeneratedMixin, _AsyncWrapper):
    def __init__(self, sync_obj: Any):
        super().__init__(sync_obj)
        sync_obj = self._sync
        self._chromium = AsyncBrowserType(sync_obj.chromium)
        self._firefox = AsyncBrowserType(sync_obj.firefox)
        self._webkit = AsyncBrowserType(sync_obj.webkit)
        self._request = AsyncAPIRequest(sync_obj.request)
        self._selectors = AsyncSelectors(sync_obj.selectors)

    @property
    def chromium(self) -> AsyncBrowserType:
        return self._chromium

    @property
    def firefox(self) -> AsyncBrowserType:
        return self._firefox

    @property
    def webkit(self) -> AsyncBrowserType:
        return self._webkit

    @property
    def request(self) -> "AsyncAPIRequest":
        return self._request

    @property
    def selectors(self) -> "AsyncSelectors":
        return self._selectors

    @property
    def devices(self) -> dict[str, dict[str, Any]]:
        return self._sync.devices


class _AsyncPlaywrightContextManager:
    def __init__(self):
        self._manager = _sync_playwright()
        self._playwright: Optional[AsyncPlaywright] = None

    async def __aenter__(self) -> AsyncPlaywright:
        return await self.start()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._stop()

    async def start(self) -> AsyncPlaywright:
        self._playwright = AsyncPlaywright(self._manager.start())
        return self._playwright

    async def _stop(self) -> None:
        self._manager._stop()
        self._playwright = None


def async_playwright() -> _AsyncPlaywrightContextManager:
    return _AsyncPlaywrightContextManager()


PlaywrightContextManager = _AsyncPlaywrightContextManager


def _should_await_callback_result(result: Any) -> bool:
    return not isinstance(result, _AsyncWrapper) and inspect.isawaitable(result)


def _run_awaitable(result: Any) -> Any:
    if not _should_await_callback_result(result):
        return result
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(result)

    outcome: dict[str, Any] = {}

    def runner() -> None:
        try:
            outcome["value"] = asyncio.run(result)
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=runner, daemon=True, name="rustwright-async-handler")
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def _run_awaitable_on_loop(loop: asyncio.AbstractEventLoop, result: Any) -> concurrent.futures.Future[Any]:
    outcome: concurrent.futures.Future[Any] = concurrent.futures.Future()
    if isinstance(result, asyncio.Future):
        outcome.set_result(None)
        return outcome
    if not _should_await_callback_result(result):
        outcome.set_result(result)
        return outcome

    async def runner() -> Any:
        return await result

    task = loop.create_task(runner())

    def finish(done_task: asyncio.Future[Any]) -> None:
        try:
            outcome.set_result(done_task.result())
        except BaseException as exc:
            outcome.set_exception(exc)

    task.add_done_callback(finish)
    return outcome


def _complete_future_from_future(
    outcome: concurrent.futures.Future[Any],
    done_future: concurrent.futures.Future[Any],
) -> None:
    if outcome.done():
        return
    try:
        outcome.set_result(done_future.result())
    except BaseException as exc:
        outcome.set_exception(exc)


def _run_callback_on_owner_loop(
    owner_loop: Optional[asyncio.AbstractEventLoop],
    call_callback: Callable[[], Any],
) -> Any:
    if owner_loop is not None and owner_loop.is_running():
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is owner_loop:
            result = call_callback()
            if _should_await_callback_result(result):
                owner_loop.create_task(result)
                return None
            return result

        outcome: concurrent.futures.Future[Any] = concurrent.futures.Future()

        def run_on_loop() -> None:
            try:
                result = call_callback()
                awaited = _run_awaitable_on_loop(owner_loop, result)
                awaited.add_done_callback(lambda done: _complete_future_from_future(outcome, done))
            except BaseException as exc:
                outcome.set_exception(exc)

        owner_loop.call_soon_threadsafe(run_on_loop)
        return outcome.result()

    return _run_awaitable(call_callback())


def _async_handler_accepts_locator(handler: Any) -> bool:
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):
        return True
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            return True
        if parameter.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}:
            return True
    return False


def _wrap_async_locator_handler(handler: Any, owner: Any = None) -> Any:
    owner_loop = getattr(owner, "_loop", None)

    def wrapper(locator: Any) -> None:
        def call_handler() -> Any:
            async_locator = AsyncLocator(locator)
            return handler(async_locator) if _async_handler_accepts_locator(handler) else handler()

        _run_callback_on_owner_loop(owner_loop, call_handler)

    return wrapper


def _wrap_async_browser(value: Any) -> Any:
    if value is None or not isinstance(value, SyncBrowser):
        return value
    existing = getattr(value, "_rustwright_async_browser", None)
    if existing is not None:
        return existing
    wrapper = AsyncBrowser(value)
    setattr(value, "_rustwright_async_browser", wrapper)
    return wrapper


def _wrap_async_browser_context(value: Any) -> Any:
    if value is None or not isinstance(value, SyncBrowserContext):
        return value
    existing = getattr(value, "_rustwright_async_browser_context", None)
    if existing is not None:
        return existing
    wrapper = AsyncBrowserContext(value)
    setattr(value, "_rustwright_async_browser_context", wrapper)
    return wrapper


def _wrap_async_page(value: Any) -> Any:
    if value is None or not isinstance(value, SyncPage):
        return value
    existing = getattr(value, "_rustwright_async_page", None)
    if existing is not None:
        return existing
    wrapper = AsyncPage(value)
    setattr(value, "_rustwright_async_page", wrapper)
    return wrapper


def _wrap_async_frame(value: Any) -> Any:
    if value is None or not isinstance(value, SyncFrame):
        return value
    existing = getattr(value, "_rustwright_async_frame", None)
    if existing is not None:
        return existing
    page = getattr(value, "_page", None)
    if page is not None:
        cache = getattr(page, "_rustwright_async_frame_cache", None)
        if cache is None:
            cache = {}
            setattr(page, "_rustwright_async_frame_cache", cache)
        is_main = bool(getattr(value, "_is_main", False))
        frame_id = getattr(value, "_frame_id", None)
        frame_spec = getattr(value, "_frame_spec", None)
        frame_index = getattr(value, "_frame_index", None)
        frame_selector = getattr(value, "_frame_selector", None)
        if is_main:
            key = ("main",)
        elif frame_id:
            key = ("id", str(frame_id))
        elif isinstance(frame_spec, dict):
            key = ("spec", json.dumps(frame_spec, sort_keys=True, default=str, separators=(",", ":")))
        elif frame_selector is not None or frame_index is not None:
            key = ("selector-index", frame_selector, frame_index)
        else:
            key = ("named", getattr(value, "_name", ""), getattr(value, "_url", ""))
        existing = cache.get(key)
        if existing is not None:
            setattr(value, "_rustwright_async_frame", existing)
            return existing
        wrapper = AsyncFrame(value)
        cache[key] = wrapper
        setattr(value, "_rustwright_async_frame", wrapper)
        return wrapper
    wrapper = AsyncFrame(value)
    setattr(value, "_rustwright_async_frame", wrapper)
    return wrapper


def _wrap_async_js_handle(value: Any) -> Any:
    if value is None or not isinstance(value, SyncJSHandle):
        return value
    existing = getattr(value, "_rustwright_async_js_handle", None)
    if existing is not None:
        return existing
    wrapper = AsyncJSHandle(value)
    setattr(value, "_rustwright_async_js_handle", wrapper)
    return wrapper


def _wrap_async_element_handle(value: Any) -> Any:
    if value is None or not isinstance(value, SyncElementHandle):
        return value
    existing = getattr(value, "_rustwright_async_element_handle", None)
    if existing is not None:
        return existing
    wrapper = AsyncElementHandle(value)
    setattr(value, "_rustwright_async_element_handle", wrapper)
    return wrapper


def _wrap_async_binding_value(value: Any) -> Any:
    if isinstance(value, SyncElementHandle):
        return _wrap_async_element_handle(value)
    if isinstance(value, SyncJSHandle):
        return _wrap_async_js_handle(value)
    if isinstance(value, dict) and {"page", "frame", "context"} & set(value):
        mapped = dict(value)
        if "page" in mapped:
            mapped["page"] = _wrap_async_page(mapped["page"])
        if "frame" in mapped:
            mapped["frame"] = _wrap_async_frame(mapped["frame"])
        if "context" in mapped:
            mapped["context"] = _wrap_async_browser_context(mapped["context"])
        return mapped
    return value


def _wrap_async_binding_callback(callback: Any) -> Any:
    try:
        owner_loop = asyncio.get_running_loop()
    except RuntimeError:
        owner_loop = None

    def wrapper(*args: Any) -> Any:
        def call_callback() -> Any:
            mapped_args = tuple(_wrap_async_binding_value(arg) for arg in args)
            return callback(*mapped_args)

        if owner_loop is not None and owner_loop.is_running():
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is owner_loop:
                return _run_awaitable(call_callback())

            outcome: concurrent.futures.Future[Any] = concurrent.futures.Future()

            def run_on_loop() -> None:
                try:
                    result = call_callback()
                    awaited = _run_awaitable_on_loop(owner_loop, result)
                    awaited.add_done_callback(lambda done: _complete_future_from_future(outcome, done))
                except BaseException as exc:
                    outcome.set_exception(exc)

            owner_loop.call_soon_threadsafe(run_on_loop)
            return outcome.result()

        return _run_awaitable(call_callback())

    return wrapper


def _wrap_async_api_request_context(value: Any) -> Any:
    if value is None or not isinstance(value, SyncAPIRequestContext):
        return value
    existing = getattr(value, "_rustwright_async_api_request_context", None)
    if existing is not None:
        return existing
    wrapper = AsyncAPIRequestContext(value)
    setattr(value, "_rustwright_async_api_request_context", wrapper)
    return wrapper


def _wrap_async_request(value: Any) -> Any:
    if value is None or not isinstance(value, SyncRequest):
        return value
    existing = getattr(value, "_rustwright_async_request", None)
    if existing is not None:
        return existing
    wrapper = AsyncRequest(value)
    setattr(value, "_rustwright_async_request", wrapper)
    return wrapper


def _wrap_async_response(value: Any) -> Any:
    if value is None or not isinstance(value, SyncResponse):
        return value
    existing = getattr(value, "_rustwright_async_response", None)
    if existing is not None:
        return existing
    wrapper = AsyncResponse(value)
    setattr(value, "_rustwright_async_response", wrapper)
    return wrapper


def _wrap_async_dialog(value: Any) -> Any:
    if value is None or not isinstance(value, SyncDialog):
        return value
    existing = getattr(value, "_rustwright_async_dialog", None)
    if existing is not None:
        return existing
    wrapper = AsyncDialog(value)
    setattr(value, "_rustwright_async_dialog", wrapper)
    return wrapper


def _wrap_async_download(value: Any) -> Any:
    if value is None or not isinstance(value, SyncDownload):
        return value
    existing = getattr(value, "_rustwright_async_download", None)
    if existing is not None:
        return existing
    wrapper = AsyncDownload(value)
    setattr(value, "_rustwright_async_download", wrapper)
    return wrapper


def _wrap_async_file_chooser(value: Any) -> Any:
    if value is None or not isinstance(value, SyncFileChooser):
        return value
    existing = getattr(value, "_rustwright_async_file_chooser", None)
    if existing is not None:
        return existing
    wrapper = AsyncFileChooser(value)
    setattr(value, "_rustwright_async_file_chooser", wrapper)
    return wrapper


def _wrap_async_console_message(value: Any) -> Any:
    if value is None or not isinstance(value, SyncConsoleMessage):
        return value
    existing = getattr(value, "_rustwright_async_console_message", None)
    if existing is not None:
        return existing
    wrapper = AsyncConsoleMessage(value)
    setattr(value, "_rustwright_async_console_message", wrapper)
    return wrapper


def _wrap_async_web_error(value: Any) -> Any:
    if value is None or not isinstance(value, SyncWebError):
        return value
    existing = getattr(value, "_rustwright_async_web_error", None)
    if existing is not None:
        return existing
    wrapper = AsyncWebError(value)
    setattr(value, "_rustwright_async_web_error", wrapper)
    return wrapper


def _wrap_async_websocket(value: Any) -> Any:
    if value is None or not isinstance(value, SyncWebSocket):
        return value
    existing = getattr(value, "_rustwright_async_websocket", None)
    if existing is not None:
        return existing
    wrapper = AsyncWebSocket(value)
    setattr(value, "_rustwright_async_websocket", wrapper)
    return wrapper


def _wrap_async_cdp_session(value: Any) -> Any:
    if value is None or not isinstance(value, SyncCDPSession):
        return value
    existing = getattr(value, "_rustwright_async_cdp_session", None)
    if existing is not None:
        return existing
    wrapper = AsyncCDPSession(value)
    setattr(value, "_rustwright_async_cdp_session", wrapper)
    return wrapper


def _wrap_async_worker(value: Any) -> Any:
    if value is None or not isinstance(value, SyncWorker):
        return value
    existing = getattr(value, "_rustwright_async_worker", None)
    if existing is not None:
        return existing
    wrapper = AsyncWorker(value)
    setattr(value, "_rustwright_async_worker", wrapper)
    return wrapper


def _wrap_async_debugger(value: Any) -> Any:
    if value is None or not isinstance(value, SyncDebugger):
        return value
    existing = getattr(value, "_rustwright_async_debugger", None)
    if existing is not None:
        return existing
    wrapper = AsyncDebugger(value)
    setattr(value, "_rustwright_async_debugger", wrapper)
    return wrapper


def _wrap_async_event_value(event: str, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, SyncBrowser):
        return _wrap_async_browser(value)
    if isinstance(value, SyncBrowserContext):
        return _wrap_async_browser_context(value)
    if isinstance(value, SyncPage):
        return _wrap_async_page(value)
    if isinstance(value, SyncFrame):
        return _wrap_async_frame(value)
    if isinstance(value, SyncWebSocket):
        return _wrap_async_websocket(value)
    if isinstance(value, SyncCDPSession):
        return _wrap_async_cdp_session(value)
    if event in {"request", "requestfinished", "requestfailed"}:
        return _wrap_async_request(value)
    if event in {"response", "navigation"}:
        return _wrap_async_response(value)
    if event in {"page", "popup", "crash"}:
        return _wrap_async_page(value)
    if event in {"load", "domcontentloaded"}:
        return _wrap_async_page(value)
    if event in {"framenavigated", "frameattached", "framedetached"}:
        return _wrap_async_frame(value)
    if event == "websocket":
        return _wrap_async_websocket(value)
    if event == "worker" or event == "serviceworker":
        return _wrap_async_worker(value)
    if event == "dialog":
        return _wrap_async_dialog(value)
    if event == "download":
        return _wrap_async_download(value)
    if event == "filechooser":
        return _wrap_async_file_chooser(value)
    if event == "console":
        return _wrap_async_console_message(value)
    if event == "weberror":
        return _wrap_async_web_error(value)
    return value


def _wrap_async_event_predicate(event: str, predicate: Any = None, owner: Any = None) -> Any:
    if predicate is None:
        return None

    owner_loop = getattr(owner, "_loop", None)

    def wrapper(value: Any) -> bool:
        def call_predicate() -> Any:
            return predicate(_wrap_async_event_value(event, value))

        return bool(_run_callback_on_owner_loop(owner_loop, call_predicate))

    return wrapper


def _wrap_async_url_or_predicate(event: str, url_or_predicate: Any, owner: Any = None) -> Any:
    if callable(url_or_predicate):
        return _wrap_async_event_predicate(event, url_or_predicate, owner)
    return url_or_predicate


def _wrap_async_event_handler(owner: Any, event: str, handler: Any) -> Any:
    wrappers = getattr(owner, "_event_handler_wrappers", None)
    if wrappers is None:
        wrappers = []
        setattr(owner, "_event_handler_wrappers", wrappers)
    for stored_event, original, wrapped in wrappers:
        if stored_event == event and original == handler:
            return wrapped

    def wrapper(*args: Any) -> None:
        def call_handler() -> Any:
            mapped_args = tuple(_wrap_async_event_value(event, arg) for arg in args)
            return handler(*_event_handler_positional_args(handler, mapped_args))

        loop = getattr(owner, "_loop", None)
        if loop is not None and loop.is_running():
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is loop:
                result = call_handler()
                _run_awaitable_on_loop(loop, result)
                return

            def run_on_loop() -> None:
                try:
                    result = call_handler()
                    if _should_await_callback_result(result):
                        loop.create_task(result)
                except BaseException:
                    pass

            loop.call_soon_threadsafe(run_on_loop)
            return

        result = call_handler()
        _run_awaitable(result)

    wrappers.append((event, handler, wrapper))
    return wrapper


def _forget_async_event_handler(owner: Any, event: str, handler: Any) -> Any:
    wrappers = getattr(owner, "_event_handler_wrappers", [])
    for index in range(len(wrappers) - 1, -1, -1):
        stored_event, original, wrapped = wrappers[index]
        if stored_event == event and original == handler:
            wrappers.pop(index)
            return wrapped
    return handler


def _wrap_async_websocket_route_handler(handler: Any, owner: Any = None) -> Any:
    owner_loop = getattr(owner, "_loop", None)

    def wrapper(route: SyncWebSocketRoute) -> None:
        def call_handler() -> Any:
            return handler(AsyncWebSocketRoute(route))

        _run_callback_on_owner_loop(owner_loop, call_handler)

    return wrapper


def _wrap_async_route_handler(handler: Any, owner: Any = None) -> Any:
    owner_loop = getattr(owner, "_loop", None)

    def wrapper(route: SyncRoute, request: SyncRequest) -> None:
        def call_handler() -> Any:
            async_route = AsyncRoute(route)
            async_request = _wrap_async_request(request)
            try:
                parameters = inspect.signature(handler).parameters
            except (TypeError, ValueError):
                return handler(async_route, async_request)
            positional = [
                parameter
                for parameter in parameters.values()
                if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
            ]
            if len(positional) <= 1 and not any(
                parameter.kind == parameter.VAR_POSITIONAL for parameter in parameters.values()
            ):
                return handler(async_route)
            return handler(async_route, async_request)

        try:
            _run_callback_on_owner_loop(owner_loop, call_handler)
        except asyncio.CancelledError:
            return

    return wrapper


def _remember_async_route_handler(owner: Any, url: Any, handler: Any) -> Any:
    wrapped = _wrap_async_route_handler(handler, owner)
    wrappers = getattr(owner, "_route_handler_wrappers", None)
    if wrappers is None:
        wrappers = []
        setattr(owner, "_route_handler_wrappers", wrappers)
    wrappers.append((url, handler, wrapped))
    return wrapped


def _forget_async_route_handler(owner: Any, url: Any, handler: Any = None) -> Any:
    wrappers = getattr(owner, "_route_handler_wrappers", [])
    if handler is None:
        setattr(owner, "_route_handler_wrappers", [item for item in wrappers if item[0] != url])
        return None
    for index in range(len(wrappers) - 1, -1, -1):
        route_url, original, wrapped = wrappers[index]
        if route_url == url and original == handler:
            wrappers.pop(index)
            return wrapped
    return handler


def _forget_all_async_route_handlers(owner: Any) -> None:
    setattr(owner, "_route_handler_wrappers", [])


class AsyncRequest(_AsyncRequestGeneratedMixin, _AsyncWrapper):
    @property
    def url(self) -> str:
        return self._sync.url

    @property
    def method(self) -> str:
        return self._sync.method

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._sync.headers or {})

    @property
    def post_data(self) -> Optional[str]:
        return self._sync.post_data

    @property
    def post_data_buffer(self) -> Optional[bytes]:
        return self._sync.post_data_buffer

    @property
    def post_data_json(self) -> Any:
        return self._sync.post_data_json

    @property
    def resource_type(self) -> str:
        return self._sync.resource_type

    @property
    def failure(self) -> Optional[str]:
        return self._sync.failure

    @property
    def frame(self) -> Optional["AsyncFrame"]:
        frame = self._sync.frame
        return _wrap_async_frame(frame)

    @property
    def redirected_from(self) -> Optional["AsyncRequest"]:
        return _wrap_async_request(self._sync.redirected_from)

    @property
    def redirected_to(self) -> Optional["AsyncRequest"]:
        return _wrap_async_request(self._sync.redirected_to)

    @property
    def existing_response(self) -> Any:
        return _wrap_async_response(self._sync.existing_response)

    @property
    def service_worker(self) -> Any:
        return self._sync.service_worker

    @property
    def timing(self) -> dict[str, float]:
        return self._sync.timing

    def is_navigation_request(self) -> bool:
        return self._sync.is_navigation_request()


class AsyncResponse(_AsyncResponseGeneratedMixin, _AsyncWrapper):
    @property
    def url(self) -> str:
        return self._sync.url

    @property
    def ok(self) -> bool:
        return self._sync.ok

    @property
    def status(self) -> Optional[int]:
        return self._sync.status

    @property
    def status_text(self) -> str:
        return self._sync.status_text

    @property
    def headers(self) -> dict[str, str]:
        return {str(key).lower(): str(value) for key, value in (self._sync.headers or {}).items()}

    @property
    def request(self) -> Optional[AsyncRequest]:
        return _wrap_async_request(self._sync.request)

    @property
    def frame(self) -> Optional["AsyncFrame"]:
        frame = self._sync.frame
        return _wrap_async_frame(frame)

    @property
    def from_service_worker(self) -> bool:
        return self._sync.from_service_worker


class AsyncDialog(_AsyncDialogGeneratedMixin, _AsyncWrapper):
    @property
    def type(self) -> str:
        return self._sync.type

    @property
    def message(self) -> str:
        return self._sync.message

    @property
    def default_value(self) -> str:
        return self._sync.default_value

    @property
    def page(self) -> "AsyncPage":
        return _wrap_async_page(self._sync.page)


class AsyncDownload(_AsyncDownloadGeneratedMixin, _AsyncWrapper):
    @property
    def url(self) -> str:
        return self._sync.url

    @property
    def suggested_filename(self) -> str:
        return self._sync.suggested_filename

    @property
    def page(self) -> "AsyncPage":
        return _wrap_async_page(self._sync.page)


class AsyncFileChooser(_AsyncFileChooserGeneratedMixin, _AsyncWrapper):
    @property
    def page(self) -> "AsyncPage":
        return _wrap_async_page(self._sync.page)

    @property
    def element(self) -> "AsyncElementHandle":
        return _wrap_async_element_handle(self._sync.element)

    def is_multiple(self) -> bool:
        return self._sync.is_multiple()


class AsyncConsoleMessage(_AsyncWrapper):
    @property
    def type(self) -> str:
        return self._sync.type

    @property
    def text(self) -> str:
        return self._sync.text

    @property
    def args(self) -> list[Any]:
        return [_wrap_async_js_handle(arg) if isinstance(arg, SyncJSHandle) else arg for arg in self._sync.args]

    @property
    def location(self) -> dict[str, Any]:
        return dict(self._sync.location)

    @property
    def page(self) -> "AsyncPage":
        return _wrap_async_page(self._sync.page)

    @property
    def worker(self) -> Any:
        worker = self._sync.worker
        return None if worker is None else _wrap_async_worker(worker)

    @property
    def timestamp(self) -> float:
        return self._sync.timestamp


class AsyncWebError(_AsyncWrapper):
    @property
    def error(self) -> Error:
        return self._sync.error

    @property
    def page(self) -> Optional["AsyncPage"]:
        return _wrap_async_page(self._sync.page)


def _sync_assertion_target(actual: Any) -> Any:
    return actual._sync if isinstance(actual, _AsyncWrapper) else actual


class AsyncExpectation:
    def __init__(
        self,
        actual: Any,
        negate: bool = False,
        timeout: Optional[float] = None,
        message: Optional[str] = None,
    ):
        self.actual = actual
        self._negate = negate
        self._timeout = timeout
        self._custom_message = message
        self._sync = SyncExpectation(_sync_assertion_target(actual), negate=negate, timeout=timeout, message=message)

    @property
    def not_to(self) -> "AsyncExpectation":
        return AsyncExpectation(self.actual, not self._negate, self._timeout, self._custom_message)

    @property
    def not_(self) -> "AsyncExpectation":
        return self.not_to

    def on(self, event: Any, f: Any) -> None:
        self._sync.on(event, f)

    def once(self, event: Any, f: Any) -> None:
        self._sync.once(event, f)

    def remove_listener(self, event: Any, f: Any) -> None:
        self._sync.remove_listener(event, f)

    async def _run_sync_assertion(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        return await _run_sync_call(getattr(self._sync, method_name), *args, **kwargs)

    async def to_have_text(
        self,
        expected: Any,
        *,
        timeout: Optional[float] = None,
        ignore_case: Optional[bool] = None,
        use_inner_text: Optional[bool] = None,
    ) -> None:
        await self._run_sync_assertion(
            "to_have_text", expected, timeout=timeout, ignore_case=ignore_case, use_inner_text=use_inner_text
        )

    async def to_contain_text(
        self,
        expected: Any,
        *,
        timeout: Optional[float] = None,
        ignore_case: Optional[bool] = None,
        use_inner_text: Optional[bool] = None,
    ) -> None:
        await self._run_sync_assertion(
            "to_contain_text", expected, timeout=timeout, ignore_case=ignore_case, use_inner_text=use_inner_text
        )

    async def to_be_visible(self, *, visible: Optional[bool] = None, timeout: Optional[float] = None) -> None:
        await self._run_sync_assertion("to_be_visible", visible=visible, timeout=timeout)

    async def to_be_hidden(self, *, timeout: Optional[float] = None) -> None:
        await self._run_sync_assertion("to_be_hidden", timeout=timeout)

    async def to_be_enabled(self, *, enabled: Optional[bool] = None, timeout: Optional[float] = None) -> None:
        await self._run_sync_assertion("to_be_enabled", enabled=enabled, timeout=timeout)

    async def to_be_disabled(self, *, timeout: Optional[float] = None) -> None:
        await self._run_sync_assertion("to_be_disabled", timeout=timeout)

    async def to_be_editable(self, *, editable: Optional[bool] = None, timeout: Optional[float] = None) -> None:
        await self._run_sync_assertion("to_be_editable", editable=editable, timeout=timeout)

    async def to_be_checked(
        self,
        *,
        checked: Optional[bool] = None,
        indeterminate: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> None:
        await self._run_sync_assertion("to_be_checked", checked=checked, indeterminate=indeterminate, timeout=timeout)

    async def to_be_attached(self, *, attached: Optional[bool] = None, timeout: Optional[float] = None) -> None:
        await self._run_sync_assertion("to_be_attached", attached=attached, timeout=timeout)

    async def to_be_empty(self, *, timeout: Optional[float] = None) -> None:
        await self._run_sync_assertion("to_be_empty", timeout=timeout)

    async def to_be_focused(self, *, timeout: Optional[float] = None) -> None:
        await self._run_sync_assertion("to_be_focused", timeout=timeout)

    async def to_have_count(self, count: Any, *, timeout: Optional[float] = None) -> None:
        await self._run_sync_assertion("to_have_count", count, timeout=timeout)

    async def to_have_value(self, value: Any, *, timeout: Optional[float] = None) -> None:
        await self._run_sync_assertion("to_have_value", value, timeout=timeout)

    async def to_have_values(self, values: Any, *, timeout: Optional[float] = None) -> None:
        await self._run_sync_assertion("to_have_values", values, timeout=timeout)

    async def to_have_attribute(
        self,
        name: str,
        value: Any,
        *,
        ignore_case: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> None:
        await self._run_sync_assertion("to_have_attribute", name, value, ignore_case=ignore_case, timeout=timeout)

    async def to_have_id(self, id: Any, *, timeout: Optional[float] = None) -> None:
        await self._run_sync_assertion("to_have_id", id, timeout=timeout)

    async def to_have_class(self, expected: Any, *, timeout: Optional[float] = None) -> None:
        await self._run_sync_assertion("to_have_class", expected, timeout=timeout)

    async def to_contain_class(self, expected: Any, *, timeout: Optional[float] = None) -> None:
        await self._run_sync_assertion("to_contain_class", expected, timeout=timeout)

    async def to_have_role(self, role: Any, *, timeout: Optional[float] = None) -> None:
        await self._run_sync_assertion("to_have_role", role, timeout=timeout)

    async def to_have_accessible_name(
        self,
        name: Any,
        *,
        ignore_case: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> None:
        await self._run_sync_assertion("to_have_accessible_name", name, ignore_case=ignore_case, timeout=timeout)

    async def to_have_accessible_description(
        self,
        description: Any,
        *,
        ignore_case: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> None:
        await self._run_sync_assertion(
            "to_have_accessible_description", description, ignore_case=ignore_case, timeout=timeout
        )

    async def to_have_accessible_error_message(
        self,
        error_message: Any,
        *,
        ignore_case: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> None:
        await self._run_sync_assertion(
            "to_have_accessible_error_message", error_message, ignore_case=ignore_case, timeout=timeout
        )

    async def to_match_aria_snapshot(self, expected: str, *, timeout: Optional[float] = None) -> None:
        await self._run_sync_assertion("to_match_aria_snapshot", expected, timeout=timeout)

    async def to_have_css(self, name: str, value: Any, *, timeout: Optional[float] = None) -> None:
        await self._run_sync_assertion("to_have_css", name, value, timeout=timeout)

    async def to_have_js_property(self, name: str, value: Any, *, timeout: Optional[float] = None) -> None:
        await self._run_sync_assertion("to_have_js_property", name, value, timeout=timeout)

    async def to_be_in_viewport(self, *, ratio: Optional[float] = None, timeout: Optional[float] = None) -> None:
        await self._run_sync_assertion("to_be_in_viewport", ratio=ratio, timeout=timeout)

    async def to_be_ok(self) -> None:
        await self._run_sync_assertion("to_be_ok")

    async def to_have_title(self, title_or_reg_exp: Any, *, timeout: Optional[float] = None) -> None:
        await self._run_sync_assertion("to_have_title", title_or_reg_exp, timeout=timeout)

    async def to_have_url(
        self,
        url_or_reg_exp: Any,
        *,
        timeout: Optional[float] = None,
        ignore_case: Optional[bool] = None,
    ) -> None:
        await self._run_sync_assertion("to_have_url", url_or_reg_exp, timeout=timeout, ignore_case=ignore_case)


def _install_async_expectation_negated_aliases() -> None:
    def make_alias(target_name: str) -> Callable[..., Any]:
        if target_name == "to_be_checked":
            async def alias(self: AsyncExpectation, *, timeout: Optional[float] = None) -> Any:
                return await self.not_to.to_be_checked(timeout=timeout)

            alias.__name__ = "not_to_be_checked"
            return alias

        async def alias(self: AsyncExpectation, *args: Any, **kwargs: Any) -> Any:
            if target_name == "to_have_accessible_description" and "name" in kwargs and "description" not in kwargs:
                kwargs = dict(kwargs)
                kwargs["description"] = kwargs.pop("name")
            return await getattr(self.not_to, target_name)(*args, **kwargs)

        alias.__name__ = f"not_{target_name}"
        signature = inspect.signature(getattr(AsyncExpectation, target_name))
        if target_name == "to_have_accessible_description":
            parameters = [
                parameter.replace(name="name") if parameter.name == "description" else parameter
                for parameter in signature.parameters.values()
            ]
            signature = signature.replace(parameters=parameters)
        alias.__signature__ = signature  # type: ignore[attr-defined]
        return alias

    assertion_method_names = [
        "to_be_attached",
        "to_be_checked",
        "to_be_disabled",
        "to_be_editable",
        "to_be_empty",
        "to_be_enabled",
        "to_be_focused",
        "to_be_hidden",
        "to_be_in_viewport",
        "to_be_ok",
        "to_be_visible",
        "to_contain_class",
        "to_contain_text",
        "to_have_accessible_description",
        "to_have_accessible_error_message",
        "to_have_accessible_name",
        "to_have_attribute",
        "to_have_class",
        "to_have_count",
        "to_have_css",
        "to_have_id",
        "to_have_js_property",
        "to_have_role",
        "to_have_text",
        "to_have_title",
        "to_have_url",
        "to_have_value",
        "to_have_values",
        "to_match_aria_snapshot",
    ]
    for target_name in assertion_method_names:
        getattr(AsyncExpectation, target_name).__signature__ = inspect.signature(  # type: ignore[attr-defined]
            getattr(SyncExpectation, target_name)
        )
        setattr(AsyncExpectation, f"not_{target_name}", make_alias(target_name))


_install_async_expectation_negated_aliases()


_ASYNC_ASSERTION_IMPL_REVERSE_PARAMETER_RENAMES = {
    "errorMessage": "error_message",
    "ignoreCase": "ignore_case",
    "titleOrRegExp": "title_or_reg_exp",
    "urlOrRegExp": "url_or_reg_exp",
    "useInnerText": "use_inner_text",
}


async def _call_async_assertion_impl_method(
    target: Any,
    method_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    signature = inspect.signature(getattr(type(target), method_name))
    public_parameters = list(signature.parameters.values())[1:]
    if len(args) > len(public_parameters):
        raise TypeError(f"{method_name}() takes {len(public_parameters) + 1} positional arguments but more were given")

    translated = dict(kwargs)
    for index, value in enumerate(args):
        public_name = public_parameters[index].name
        internal_name = _ASYNC_ASSERTION_IMPL_REVERSE_PARAMETER_RENAMES.get(public_name, public_name)
        if public_name in translated or internal_name in translated:
            raise TypeError(f"{method_name}() got multiple values for argument '{public_name}'")
        translated[internal_name] = value
    for public_name, internal_name in _ASYNC_ASSERTION_IMPL_REVERSE_PARAMETER_RENAMES.items():
        if public_name in translated:
            if internal_name in translated:
                raise TypeError(f"{method_name}() got multiple values for argument '{public_name}'")
            translated[internal_name] = translated.pop(public_name)
    return await getattr(target._expectation, method_name)(**translated)


def _make_async_assertion_impl_method(method_name: str, sync_impl_class: Any) -> Callable[..., Any]:
    async def method(self: Any, *args: Any, **kwargs: Any) -> Any:
        return await _call_async_assertion_impl_method(self, method_name, args, kwargs)

    method.__name__ = method_name
    method.__signature__ = inspect.signature(getattr(sync_impl_class, method_name))  # type: ignore[attr-defined]
    return method


class _AsyncAssertionImplBase:
    def __init__(
        self,
        actual: Any,
        *,
        negate: bool = False,
        timeout: Optional[float] = None,
        message: Optional[str] = None,
    ):
        self._expectation = AsyncExpectation(actual, negate=negate, timeout=timeout, message=message)


class _AsyncAPIResponseAssertionsImpl(_AsyncAssertionImplBase):
    pass


class _AsyncLocatorAssertionsImpl(_AsyncAssertionImplBase):
    pass


class _AsyncPageAssertionsImpl(_AsyncAssertionImplBase):
    pass


for _async_impl_cls, _sync_impl_cls in (
    (_AsyncAPIResponseAssertionsImpl, SyncAPIResponseAssertionsImpl),
    (_AsyncLocatorAssertionsImpl, SyncLocatorAssertionsImpl),
    (_AsyncPageAssertionsImpl, SyncPageAssertionsImpl),
):
    for _method_name in [
        name
        for name in dir(_sync_impl_cls)
        if name.startswith("to_") or name.startswith("not_to_")
    ]:
        setattr(
            _async_impl_cls,
            _method_name,
            _make_async_assertion_impl_method(_method_name, _sync_impl_cls),
        )
del _async_impl_cls, _sync_impl_cls, _method_name


class _AsyncAssertionPublicMixin:
    def on(self, event: Any, f: Any) -> None:
        self._expectation.on(event, f)

    def once(self, event: Any, f: Any) -> None:
        self._expectation.once(event, f)

    def remove_listener(self, event: Any, f: Any) -> None:
        self._expectation.remove_listener(event, f)


def _make_async_assertion_public_method(method_name: str) -> Callable[..., Any]:
    async def method(self: Any, *args: Any, **kwargs: Any) -> Any:
        return await getattr(self._expectation, method_name)(*args, **kwargs)

    method.__name__ = method_name
    method.__signature__ = inspect.signature(getattr(AsyncExpectation, method_name))  # type: ignore[attr-defined]
    return method


class AsyncAPIResponseAssertions(_AsyncAssertionPublicMixin, _AsyncAssertionImplBase):
    pass


class AsyncLocatorAssertions(_AsyncAssertionPublicMixin, _AsyncAssertionImplBase):
    pass


class AsyncPageAssertions(_AsyncAssertionPublicMixin, _AsyncAssertionImplBase):
    pass


for _async_public_cls, _sync_impl_cls in (
    (AsyncAPIResponseAssertions, SyncAPIResponseAssertionsImpl),
    (AsyncLocatorAssertions, SyncLocatorAssertionsImpl),
    (AsyncPageAssertions, SyncPageAssertionsImpl),
):
    for _method_name in [
        name
        for name in dir(_sync_impl_cls)
        if name.startswith("to_") or name.startswith("not_to_")
    ]:
        setattr(_async_public_cls, _method_name, _make_async_assertion_public_method(_method_name))
del _async_public_cls, _sync_impl_cls, _method_name


class AsyncExpect:
    def __init__(self) -> None:
        self._timeout: Any = None

    def __call__(
        self,
        actual: Any,
        message: Optional[str] = None,
        *,
        timeout: Optional[float] = None,
    ) -> AsyncAPIResponseAssertions | AsyncLocatorAssertions | AsyncPageAssertions:
        if not isinstance(actual, (AsyncPage, AsyncLocator, AsyncAPIResponse)):
            raise ValueError(f"Unsupported type: {type(actual)}")
        effective_timeout = timeout if timeout is not None else self._timeout
        if isinstance(actual, AsyncPage):
            return AsyncPageAssertions(actual, timeout=effective_timeout, message=message)
        if isinstance(actual, AsyncLocator):
            return AsyncLocatorAssertions(actual, timeout=effective_timeout, message=message)
        return AsyncAPIResponseAssertions(actual, timeout=effective_timeout, message=message)

    def set_options(self, timeout: Any = _UNSET) -> None:
        if timeout is not _UNSET:
            self._timeout = timeout


class AsyncCDPSession(_AsyncCDPSessionGeneratedMixin, _AsyncWrapper):
    pass


class AsyncAccessibility(_AsyncAccessibilityGeneratedMixin, _AsyncWrapper):
    pass


class AsyncTracing(_AsyncTracingGeneratedMixin, _AsyncWrapper):
    pass


class AsyncBrowser(_AsyncBrowserGeneratedMixin, _AsyncWrapper):
    async def __aenter__(self) -> "AsyncBrowser":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    @property
    def contexts(self) -> list["AsyncBrowserContext"]:
        return [_wrap_async_browser_context(context) for context in self._sync.contexts]

    @property
    def _ws_endpoint(self) -> str:
        return self._sync._ws_endpoint

    async def new_page(
        self,
        *,
        viewport: Any = _MISSING,
        screen: Any = None,
        no_viewport: Optional[bool] = None,
        ignore_https_errors: Optional[bool] = None,
        java_script_enabled: Optional[bool] = None,
        bypass_csp: Optional[bool] = None,
        user_agent: Optional[str] = None,
        locale: Optional[str] = None,
        timezone_id: Optional[str] = None,
        geolocation: Any = None,
        permissions: Any = None,
        extra_http_headers: Optional[dict[str, str]] = None,
        offline: Optional[bool] = None,
        http_credentials: Any = None,
        device_scale_factor: Optional[float] = None,
        is_mobile: Optional[bool] = None,
        has_touch: Optional[bool] = None,
        color_scheme: Optional[str] = None,
        forced_colors: Optional[str] = None,
        contrast: Optional[str] = None,
        reduced_motion: Optional[str] = None,
        accept_downloads: Optional[bool] = None,
        default_browser_type: Optional[str] = None,
        proxy: Any = None,
        record_har_path: Any = None,
        record_har_omit_content: Optional[bool] = None,
        record_video_dir: Any = None,
        record_video_size: Any = None,
        storage_state: Any = None,
        base_url: Optional[str] = None,
        strict_selectors: Optional[bool] = None,
        service_workers: Optional[str] = None,
        record_har_url_filter: Any = None,
        record_har_mode: Optional[str] = None,
        record_har_content: Optional[str] = None,
        client_certificates: Any = None,
    ) -> "AsyncPage":
        options = _options_from_explicit_kwargs(locals())
        if (
            isinstance(self._sync, SyncBrowser)
            and not options
            and not self._sync._launch_proxy
            and not self._sync._launch_downloads_path
        ):
            context = SyncBrowserContext(None, browser=self._sync, options={})
            self._sync._contexts.append(context)
            try:
                core = await _await_native(self._sync._core.new_page_async())
                page = await _finish_native_page(context, core)
            except BaseException:
                self._sync._contexts.remove(context)
                raise
            page._owns_context = True
            return _wrap_async_page(page)
        return _wrap_async_page(await _run_sync_call(self._sync.new_page, **options))

    async def new_context(
        self,
        *,
        viewport: Any = _MISSING,
        screen: Any = None,
        no_viewport: Optional[bool] = None,
        ignore_https_errors: Optional[bool] = None,
        java_script_enabled: Optional[bool] = None,
        bypass_csp: Optional[bool] = None,
        user_agent: Optional[str] = None,
        locale: Optional[str] = None,
        timezone_id: Optional[str] = None,
        geolocation: Any = None,
        permissions: Any = None,
        extra_http_headers: Optional[dict[str, str]] = None,
        offline: Optional[bool] = None,
        http_credentials: Any = None,
        device_scale_factor: Optional[float] = None,
        is_mobile: Optional[bool] = None,
        has_touch: Optional[bool] = None,
        color_scheme: Optional[str] = None,
        reduced_motion: Optional[str] = None,
        forced_colors: Optional[str] = None,
        contrast: Optional[str] = None,
        accept_downloads: Optional[bool] = None,
        default_browser_type: Optional[str] = None,
        proxy: Any = None,
        record_har_path: Any = None,
        record_har_omit_content: Optional[bool] = None,
        record_video_dir: Any = None,
        record_video_size: Any = None,
        storage_state: Any = None,
        base_url: Optional[str] = None,
        strict_selectors: Optional[bool] = None,
        service_workers: Optional[str] = None,
        record_har_url_filter: Any = None,
        record_har_mode: Optional[str] = None,
        record_har_content: Optional[str] = None,
        client_certificates: Any = None,
    ) -> "AsyncBrowserContext":
        options = _options_from_explicit_kwargs(locals())
        if (
            isinstance(self._sync, SyncBrowser)
            and not options
            and not self._sync._closed
            and not self._sync._browser_download_behavior
            and not self._sync._launch_proxy
            and not self._sync._launch_downloads_path
            and not bool(self._sync._core.single_process_fallback())
        ):
            core = await _await_native(self._sync._core.new_context_async(None))
            context = SyncBrowserContext(core, browser=self._sync, options={})
            self._sync._contexts.append(context)
            return _wrap_async_browser_context(context)
        return _wrap_async_browser_context(await _run_sync_call(self._sync.new_context, **options))

    async def close(self, *, reason: Optional[str] = None) -> None:
        if not isinstance(self._sync, SyncBrowser):
            await _single_flight_close(
                self._sync,
                lambda: _run_sync_call(self._sync.close, reason=reason),
            )
            return
        if self._sync._closed or getattr(
            self._sync, "_rustwright_async_close_state", _CLOSE_OPEN
        ) == _CLOSE_CLOSED:
            return
        normalized_reason = None
        if reason is not None:
            normalized_reason = _normalize_string_option(reason, method="Browser.close", name="reason")
        await _single_flight_close(
            self._sync,
            lambda: self._close_native(normalized_reason),
        )

    async def _close_native(self, normalized_reason: Optional[str]) -> None:
        if self._sync._connected_over_cdp:
            self._sync._closed_reason = normalized_reason
            self._sync._stop_page_event_pumps()
            await _await_native(self._sync._core.close_async())
            self._sync._closed = True
            self._sync._mark_owned_cdp_sessions_closed()
            self._sync._contexts.clear()
            self._sync._emit_disconnected()
            return
        for context in list(self._sync._contexts):
            await _wrap_async_browser_context(context)._close_for_browser_close(reason=normalized_reason)
        self._sync._closed_reason = normalized_reason
        await _await_native(self._sync._core.close_async())
        self._sync._closed = True
        self._sync._mark_owned_cdp_sessions_closed()
        self._sync._contexts.clear()
        self._sync._emit_disconnected()

    def is_connected(self) -> bool:
        return self._sync.is_connected()

    @property
    def version(self) -> str:
        return self._sync.version

    @property
    def browser_type(self) -> AsyncBrowserType:
        return AsyncBrowserType(self._sync.browser_type)


class AsyncBrowserContext(_AsyncBrowserContextGeneratedMixin, _AsyncWrapper):
    async def __aenter__(self) -> "AsyncBrowserContext":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    @property
    def pages(self) -> list["AsyncPage"]:
        return [_wrap_async_page(page) for page in self._sync.pages]

    @property
    def browser(self) -> Optional[AsyncBrowser]:
        browser = self._sync.browser
        return _wrap_async_browser(browser)

    @property
    def background_pages(self) -> list["AsyncPage"]:
        return [_wrap_async_page(page) for page in self._sync.background_pages]

    @property
    def service_workers(self) -> list["AsyncWorker"]:
        return [_wrap_async_worker(worker) for worker in self._sync.service_workers]

    @property
    def request(self) -> "AsyncAPIRequestContext":
        return _wrap_async_api_request_context(self._sync.request)

    @property
    def tracing(self) -> "AsyncTracing":
        return AsyncTracing(self._sync.tracing)

    @property
    def clock(self) -> AsyncClock:
        return AsyncClock(self._sync.clock)

    @property
    def debugger(self) -> Any:
        return _wrap_async_debugger(self._sync.debugger)

    async def new_page(self) -> "AsyncPage":
        if _native_page_options_supported(self._sync):
            return _wrap_async_page(await _native_context_page(self._sync))
        return _wrap_async_page(await _run_sync_call(self._sync.new_page))

    async def close(self, *, reason: Optional[str] = None) -> None:
        browser = getattr(self._sync, "_browser", None)
        if getattr(self._sync, "_owns_browser", False) and browser is not None:
            normalized_reason = None
            if reason is not None:
                normalized_reason = _normalize_string_option(
                    reason,
                    method="BrowserContext.close",
                    name="reason",
                )
            await _wrap_async_browser(browser).close(reason=normalized_reason)
            return
        await self._close_native(reason=reason, for_browser_close=False)

    async def _close_for_browser_close(self, *, reason: Optional[str] = None) -> None:
        await self._close_native(reason=reason, for_browser_close=True)

    async def _close_native(self, *, reason: Optional[str], for_browser_close: bool) -> None:
        await _single_flight_close(
            self._sync,
            lambda: self._close_native_impl(reason=reason, for_browser_close=for_browser_close),
        )

    async def _close_native_impl(self, *, reason: Optional[str], for_browser_close: bool) -> None:
        if not isinstance(self._sync, SyncBrowserContext) or self._sync._record_har_path:
            close = self._sync._close_for_browser_close if for_browser_close else self._sync.close
            await _run_sync_call(close, reason=reason)
            return
        if self._sync._closed:
            return
        normalized_reason = None
        if reason is not None:
            normalized_reason = _normalize_string_option(
                reason,
                method="BrowserContext.close",
                name="reason",
            )
        self._sync._closed_reason = normalized_reason
        if not for_browser_close or self._sync._owns_browser:
            await _run_sync_call(self._sync._cleanup_default_context_state)
        for page in list(self._sync._pages):
            await _wrap_async_page(page).close(reason=normalized_reason)
        if self._sync._core is not None:
            await _await_native(self._sync._core.close_async())
        await _run_sync_call(self._sync.request.dispose)
        self._sync._closed = True
        self._sync._pages.clear()
        if self._sync._browser is not None and self._sync in self._sync._browser._contexts:
            self._sync._browser._contexts.remove(self._sync)
        _emit_event(self._sync._event_handlers, "close", self._sync)

    def is_closed(self) -> bool:
        return self._sync.is_closed()

    def set_default_timeout(self, timeout: float) -> None:
        self._sync.set_default_timeout(timeout)

    def set_default_navigation_timeout(self, timeout: float) -> None:
        self._sync.set_default_navigation_timeout(timeout)

    async def expose_function(self, name: str, callback: Any) -> None:
        await _run_sync_call(self._sync.expose_function, name, _wrap_async_binding_callback(callback))

    async def expose_binding(self, name: str, callback: Any, *, handle: Optional[bool] = None) -> None:
        await _run_sync_call(self._sync.expose_binding, name, _wrap_async_binding_callback(callback), handle=handle)

    async def route(self, url: Any, handler: Any, *, times: Optional[int] = None) -> None:
        wrapped_handler = _remember_async_route_handler(self, url, handler)
        await _run_sync_call(self._sync.route, url, wrapped_handler, times=times)

    async def unroute(self, url: Any, handler: Any = None) -> None:
        await _run_sync_call(self._sync.unroute, url, _forget_async_route_handler(self, url, handler))

    async def unroute_all(self, *, behavior: Optional[str] = None) -> None:
        _forget_all_async_route_handlers(self)
        await _run_sync_call(self._sync.unroute_all, behavior=behavior)

    def expect_page(self, predicate: Any = None, *, timeout: Optional[float] = None) -> _AsyncEventContextManager:
        return _AsyncEventContextManager(
            self._sync.expect_page(_wrap_async_event_predicate("page", predicate, self), timeout=timeout),
            lambda value: _wrap_async_event_value("page", value),
        )

    def expect_console_message(self, predicate: Any = None, *, timeout: Optional[float] = None) -> _AsyncEventContextManager:
        return _AsyncEventContextManager(
            self._sync.expect_console_message(_wrap_async_event_predicate("console", predicate, self), timeout=timeout),
            _wrap_async_console_message,
        )

    def expect_event(self, event: str, predicate: Any = None, *, timeout: Optional[float] = None) -> _AsyncEventContextManager:
        return _AsyncEventContextManager(
            self._sync.expect_event(event, _wrap_async_event_predicate(event, predicate, self), timeout=timeout),
            lambda value: _wrap_async_event_value(event, value),
        )

    async def wait_for_event(self, event: str, predicate: Any = None, *, timeout: Optional[float] = None) -> Any:
        value = await _run_sync_call(
            self._sync.wait_for_event,
            event,
            _wrap_async_event_predicate(event, predicate, self),
            timeout=timeout,
        )
        return _wrap_async_event_value(event, value)

    async def route_web_socket(self, url: Any, handler: Any) -> None:
        await _run_sync_call(self._sync.route_web_socket, url, _wrap_async_websocket_route_handler(handler, self))


class AsyncPage(_AsyncPageGeneratedMixin, _AsyncWrapper):
    def __init__(self, sync_obj: Any):
        super().__init__(sync_obj)
        sync_obj = self._sync
        self._keyboard = AsyncKeyboard(sync_obj.keyboard)
        self._mouse = AsyncMouse(sync_obj.mouse)
        self._touchscreen = AsyncTouchscreen(sync_obj.touchscreen)
        self.accessibility = AsyncAccessibility(sync_obj.accessibility)
        self._event_pump_task: Optional[asyncio.Task[Any]] = getattr(
            sync_obj,
            "_rustwright_async_event_pump_task",
            None,
        )
        if getattr(sync_obj, "_event_pump_thread", None) is None and self._loop is not None:
            if self._event_pump_task is None or self._event_pump_task.done():
                self._event_pump_task = self._loop.create_task(self._event_pump())
                sync_obj._rustwright_async_event_pump_task = self._event_pump_task

    async def _event_pump(self) -> None:
        while self._sync._event_listeners_active():
            try:
                batch = json.loads(
                    await _await_native(self._sync._event_stream.wait_batch_async(500.0, 64))
                )
            except asyncio.CancelledError:
                self._sync._event_stream.rollback_batch()
                return
            except RuntimeError as exc:
                if "page event stream is already waiting" not in str(exc):
                    raise
                await asyncio.sleep(0.01)
                continue
            except Error:
                return
            if not isinstance(batch, list):
                self._sync._event_stream.rollback_batch()
                continue
            try:
                stream_closed = await _await_cleanup_completion(self._consume_event_batch(batch))
            except asyncio.CancelledError:
                return
            if stream_closed:
                self._sync._stop_event_pump()
                return

    async def _consume_event_batch(self, batch: list[Any]) -> bool:
        completed = False
        stream_closed = False
        try:
            for envelope in batch:
                if not isinstance(envelope, dict):
                    continue
                kind = str(envelope.get("kind") or "")
                if kind == "_closed":
                    stream_closed = True
                    break
                if kind == "_overflow":
                    await _run_sync_call(
                        self._sync._reconcile_event_stream_overflow,
                        envelope.get("payload"),
                    )
                    continue
                await _run_sync_call(
                    self._sync._handle_observation_event,
                    kind,
                    envelope.get("payload"),
                )
                if not self._sync._event_listeners_active():
                    break
            completed = True
        finally:
            if completed:
                self._sync._event_stream.ack_batch()
            else:
                self._sync._event_stream.rollback_batch()
        return stream_closed

    @property
    def url(self) -> str:
        return self._sync.url

    @property
    def context(self) -> Optional[AsyncBrowserContext]:
        context = self._sync.context
        return _wrap_async_browser_context(context)

    @property
    def request(self) -> "AsyncAPIRequestContext":
        return _wrap_async_api_request_context(self._sync.request)

    @property
    def keyboard(self) -> "AsyncKeyboard":
        return self._keyboard

    @property
    def mouse(self) -> "AsyncMouse":
        return self._mouse

    @property
    def touchscreen(self) -> "AsyncTouchscreen":
        return self._touchscreen

    @property
    def viewport_size(self) -> Optional[dict[str, int]]:
        return self._sync.viewport_size

    @property
    def clock(self) -> AsyncClock:
        return AsyncClock(self._sync.clock)

    @property
    def screencast(self) -> AsyncScreencast:
        return AsyncScreencast(self._sync.screencast)

    @property
    def video(self) -> Optional[AsyncVideo]:
        video = self._sync.video
        return None if video is None else AsyncVideo(video)

    @property
    def main_frame(self) -> "AsyncFrame":
        return _wrap_async_frame(self._sync.main_frame)

    @property
    def frames(self) -> list["AsyncFrame"]:
        return [_wrap_async_frame(frame) for frame in self._sync.frames]

    def frame(self, name: Optional[str] = None, *, url: Any = None) -> Optional["AsyncFrame"]:
        frame = self._sync.frame(name, url=url)
        return _wrap_async_frame(frame)

    def frame_locator(self, selector: str) -> "AsyncFrameLocator":
        return AsyncFrameLocator(self._sync.frame_locator(selector))

    def locator(
        self,
        selector: str,
        *,
        has_text: Any = None,
        has_not_text: Any = None,
        has: Optional["AsyncLocator"] = None,
        has_not: Optional["AsyncLocator"] = None,
    ) -> "AsyncLocator":
        sync_has = has._sync if isinstance(has, _AsyncWrapper) else has
        sync_has_not = has_not._sync if isinstance(has_not, _AsyncWrapper) else has_not
        return AsyncLocator(
            self._sync.locator(
                selector,
                has_text=has_text,
                has_not_text=has_not_text,
                has=sync_has,
                has_not=sync_has_not,
            )
        )

    def frame_locator(self, selector: str) -> "AsyncFrameLocator":
        return AsyncFrameLocator(self._sync.frame_locator(selector))

    def get_by_text(self, text: str, *, exact: Optional[bool] = None) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_text(text, exact=bool(exact) if exact is not None else False))

    def get_by_role(
        self,
        role: str,
        *,
        checked: Optional[bool] = None,
        disabled: Optional[bool] = None,
        expanded: Optional[bool] = None,
        include_hidden: Optional[bool] = None,
        level: Optional[int] = None,
        name: Any = None,
        pressed: Optional[bool] = None,
        selected: Optional[bool] = None,
        exact: Optional[bool] = None,
    ) -> "AsyncLocator":
        return AsyncLocator(
            self._sync.get_by_role(
                role,
                checked=checked,
                disabled=disabled,
                expanded=expanded,
                include_hidden=include_hidden,
                level=level,
                name=name,
                pressed=pressed,
                selected=selected,
                exact=exact,
            )
        )

    def get_by_test_id(self, test_id: str) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_test_id(test_id))

    def get_by_placeholder(self, text: str, *, exact: Optional[bool] = None) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_placeholder(text, exact=bool(exact) if exact is not None else False))

    def get_by_label(self, text: str, *, exact: Optional[bool] = None) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_label(text, exact=bool(exact) if exact is not None else False))

    def get_by_alt_text(self, text: str, *, exact: Optional[bool] = None) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_alt_text(text, exact=bool(exact) if exact is not None else False))

    def get_by_title(self, text: str, *, exact: Optional[bool] = None) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_title(text, exact=bool(exact) if exact is not None else False))

    async def goto(
        self,
        url: str,
        *,
        timeout: Optional[float] = None,
        wait_until: Optional[str] = None,
        referer: Optional[str] = None,
    ) -> Optional["AsyncResponse"]:
        if not _native_page_hot_path_supported(self._sync):
            response = await _run_sync_call(
                self._sync.goto,
                url,
                timeout=timeout,
                wait_until=wait_until,
                referer=referer,
            )
            return _wrap_async_response(response)
        target = _normalize_required_string_argument(
            url,
            method="Page.goto",
            name="url",
            missing_type_error="Frame.goto() missing 1 required positional argument: 'url'",
        )
        navigation_timeout = _navigation_timeout_for_method(self._sync, timeout, method="Page.goto")
        normalized_state = _normalize_lifecycle_state(wait_until, label="wait_until", method="Page.goto")
        normalized_referer = (
            None
            if referer is None
            else _normalize_string_option(referer, method="Page.goto", name="referer")
        )
        target = self._sync._resolve_url(target)
        self._sync._mark_request_cookie_sync_required()
        await _run_sync_call(self._sync._retain_navigation_response_bodies)
        await _run_sync_call(self._sync._mark_navigation_history_boundary)
        self._sync._set_content_html_document_known = None
        download_waiter = (
            await _run_sync_call(self._sync._download_event_waiter)
            if target.lower().startswith(("http://", "https://"))
            else None
        )
        try:
            payload = json.loads(
                await _await_native_method(
                    "Page.goto",
                    self._sync._core.goto_async(
                        target,
                        normalized_state,
                        navigation_timeout,
                        normalized_referer,
                    ),
                )
            )
        except Error as exc:
            message = str(exc).splitlines()[0]
            if (
                download_waiter is not None
                and message.startswith("Page.goto: net::ERR_ABORTED")
                and await _run_sync_call(
                    self._sync._download_started_for_url,
                    download_waiter,
                    target,
                    timeout=1_000.0,
                )
            ):
                raise Error("Page.goto: Download is starting") from None
            raise
        response = (
            None
            if payload is None or target.lower().startswith(("about:", "data:"))
            else _response_from_payload(self._sync, payload, fallback_url=target)
        )
        if response is not None:
            self._sync._remember_navigation_response(response)
        if self._sync._context is not None:
            await _run_sync_call(self._sync._context._apply_storage_state_to_page, self._sync)
        self._sync._slow_mo()
        return _wrap_async_response(response)

    async def wait_for_load_state(self, state: Optional[str] = None, *, timeout: Optional[float] = None) -> None:
        await _run_sync_wait_sliced(self._sync, self._sync.wait_for_load_state, "load" if state is None else state, timeout=timeout)

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        if not _native_page_hot_path_supported(self._sync):
            return await _run_sync_call(self._sync.evaluate, expression, _unwrap_async_arg(arg))
        normalized_expression = _normalize_string_option(
            expression,
            method="Page.evaluate",
            name="expression",
        )
        value = _unwrap_async_arg(arg)
        try:
            arg_json = None if value is None else json.dumps(value)
        except (TypeError, ValueError):
            return await _run_sync_call(self._sync.evaluate, expression, value)
        self._sync._mark_request_cookie_sync_required()
        self._sync._mark_history_events_may_arrive()
        await _run_sync_call(self._sync._maybe_start_transient_popup_adoption, normalized_expression)
        result = await _await_native_method(
            "Page.evaluate",
            self._sync._core.evaluate_async(
                normalized_expression,
                arg_json,
                self._sync._default_timeout,
            )
        )
        return _decode_json_result(json.loads(result))

    async def add_script_tag(
        self,
        *,
        url: Optional[str] = None,
        path: Any = None,
        content: Optional[str] = None,
        type: Optional[str] = None,
    ) -> Any:
        return await _run_sync_call(self._sync.add_script_tag, url=url, path=path, content=content, type=type)

    async def add_style_tag(
        self,
        *,
        url: Optional[str] = None,
        path: Any = None,
        content: Optional[str] = None,
    ) -> Any:
        return await _run_sync_call(self._sync.add_style_tag, url=url, path=path, content=content)

    async def expose_function(self, name: str, callback: Any) -> None:
        await _run_sync_call(self._sync.expose_function, name, _wrap_async_binding_callback(callback))

    async def expose_binding(self, name: str, callback: Any, *, handle: Optional[bool] = None) -> None:
        await _run_sync_call(self._sync.expose_binding, name, _wrap_async_binding_callback(callback), handle=handle)

    def set_default_timeout(self, timeout: float) -> None:
        self._sync.set_default_timeout(timeout)

    def set_default_navigation_timeout(self, timeout: float) -> None:
        self._sync.set_default_navigation_timeout(timeout)

    async def wait_for_selector(
        self,
        selector: str,
        *,
        timeout: Optional[float] = None,
        state: Optional[str] = None,
        strict: Optional[bool] = None,
    ) -> Optional["AsyncElementHandle"]:
        if not _native_page_hot_path_supported(self._sync):
            handle = await _run_sync_wait_sliced(
                self._sync,
                self._sync.wait_for_selector,
                selector,
                timeout=timeout,
                state=state,
                strict=strict,
            )
            return _wrap_async_element_handle(handle)
        normalized_state = _normalize_wait_for_selector_state(state, method="Page.wait_for_selector")
        timeout_ms = _default_timeout_for_method(self._sync, timeout, method="Page.wait_for_selector")
        locator = _native_locator(self._sync, selector, strict, method="Page.wait_for_selector")
        attached = await _await_native_method(
            "Page.wait_for_selector",
            self._sync._core.wait_for_selector_async(
                _json(locator._spec),
                locator._index,
                normalized_state,
                timeout_ms,
                locator._strict,
            )
        )
        handle = None
        if attached and normalized_state in {"attached", "visible"}:
            handle = SyncElementHandle(locator.nth(0), handle=None)
        return _wrap_async_element_handle(handle)

    async def click(
        self,
        selector: str,
        *,
        modifiers: Any = None,
        position: Any = None,
        delay: Optional[float] = None,
        button: Optional[str] = None,
        click_count: Optional[int] = None,
        timeout: Optional[float] = None,
        force: Optional[bool] = None,
        no_wait_after: Optional[bool] = None,
        trial: Optional[bool] = None,
        strict: Optional[bool] = None,
    ) -> None:
        if (
            not _native_page_hot_path_supported(self._sync)
            or getattr(self._sync, "_active_page_cdp_event_contexts", 0) > 0
            or any(
                value is not None
                for value in (modifiers, position, delay, button, click_count, force, no_wait_after, trial)
            )
        ):
            await _run_sync_wait_sliced(
                self._sync,
                self._sync.click,
                selector,
                modifiers=modifiers,
                position=position,
                delay=delay,
                button=button,
                click_count=click_count,
                timeout=timeout,
                force=force,
                no_wait_after=no_wait_after,
                trial=trial,
                strict=strict,
            )
            return
        normalized_selector = _native_normalize_selector(selector, method="Page.click")
        timeout_ms = _default_timeout_for_method(self._sync, timeout, method="Page.click")
        locator = _native_selector_locator(self._sync, normalized_selector, strict, method="Page.click")
        if _unsafe_dom_fastpath_enabled():
            await _await_native_method(
                "Page.click",
                self._sync._core.click_async(
                    _json(locator._spec),
                    locator._index,
                    timeout_ms,
                    locator._strict,
                ),
            )
            return
        point_info = await _await_native_action(
            "Page.click",
            self._sync._core.click_actionable_wait_async(
                _json(locator._spec),
                locator._index,
                timeout_ms,
                locator._strict,
            ),
        )
        mouse = self._sync.mouse
        initial_buttons = mouse._buttons
        start_x = mouse._x
        start_y = mouse._y
        modifiers = self._sync.keyboard._modifiers_mask()
        target_x = float(point_info[0])
        target_y = float(point_info[1])
        mouse._x = target_x
        mouse._y = target_y
        mouse._buttons &= ~1
        await _await_native_action(
            "Page.click",
            self._sync._core.dispatch_mouse_click_async(
                target_x,
                target_y,
                start_x,
                start_y,
                initial_buttons,
                modifiers,
                float(point_info[2]),
            ),
        )

    async def fill(
        self,
        selector: str,
        value: str,
        *,
        timeout: Optional[float] = None,
        no_wait_after: Optional[bool] = None,
        strict: Optional[bool] = None,
        force: Optional[bool] = None,
    ) -> None:
        if (
            not _native_page_hot_path_supported(self._sync)
            or getattr(self._sync, "_active_page_cdp_event_contexts", 0) > 0
            or no_wait_after is not None
            or force is not None
        ):
            await _run_sync_wait_sliced(
                self._sync,
                self._sync.fill,
                selector,
                value,
                timeout=timeout,
                no_wait_after=no_wait_after,
                strict=strict,
                force=force,
            )
            return
        normalized_selector = _native_normalize_selector(selector, method="Page.fill")
        normalized_value = _normalize_required_string_argument(
            value,
            method="Page.fill",
            name="value",
            missing_type_error="Frame.fill() missing 1 required positional argument: 'value'",
        )
        timeout_ms = _default_timeout_for_method(self._sync, timeout, method="Page.fill")
        locator = _native_selector_locator(self._sync, normalized_selector, strict, method="Page.fill")
        if _unsafe_dom_fastpath_enabled():
            await _await_native_method(
                "Page.fill",
                self._sync._core.fill_async(
                    _json(locator._spec),
                    locator._index,
                    normalized_value,
                    timeout_ms,
                    locator._strict,
                ),
            )
            return
        await _await_native_action(
            "Page.fill",
            self._sync._core.fill_actionable_async(
                _json(locator._spec),
                locator._index,
                normalized_value,
                timeout_ms,
                locator._strict,
            ),
        )

    async def inner_text(
        self,
        selector: str,
        *,
        strict: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> Optional[str]:
        if not _native_page_hot_path_supported(self._sync):
            return await _run_sync_call(self._sync.inner_text, selector, strict=strict, timeout=timeout)
        timeout_ms = _default_timeout_for_method(self._sync, timeout, method="Page.inner_text")
        locator = _native_locator(self._sync, selector, strict, method="Page.inner_text")
        await _await_native_method(
            "Page.inner_text",
            self._sync._core.wait_for_selector_async(
                _json(locator._spec),
                locator._index,
                "attached",
                timeout_ms,
                locator._strict,
            )
        )
        return await _await_native_method(
            "Page.inner_text",
            self._sync._core.inner_text_async(
                _json(locator._spec),
                locator._index,
                timeout_ms,
            )
        )

    def expect_request(self, url_or_predicate: Any, *, timeout: Optional[float] = None) -> _AsyncEventContextManager:
        return _AsyncEventContextManager(
            self._sync.expect_request(_wrap_async_url_or_predicate("request", url_or_predicate, self), timeout=timeout),
            _wrap_async_request,
        )

    def expect_response(self, url_or_predicate: Any, *, timeout: Optional[float] = None) -> _AsyncEventContextManager:
        return _AsyncEventContextManager(
            self._sync.expect_response(_wrap_async_url_or_predicate("response", url_or_predicate, self), timeout=timeout),
            _wrap_async_response,
        )

    def expect_console_message(self, predicate: Any = None, *, timeout: Optional[float] = None) -> _AsyncEventContextManager:
        return _AsyncEventContextManager(
            self._sync.expect_console_message(_wrap_async_event_predicate("console", predicate, self), timeout=timeout),
            _wrap_async_console_message,
        )

    def expect_download(self, predicate: Any = None, *, timeout: Optional[float] = None) -> _AsyncEventContextManager:
        return _AsyncEventContextManager(
            self._sync.expect_download(_wrap_async_event_predicate("download", predicate, self), timeout=timeout),
            _wrap_async_download,
        )

    def expect_file_chooser(self, predicate: Any = None, *, timeout: Optional[float] = None) -> _AsyncEventContextManager:
        return _AsyncEventContextManager(
            self._sync.expect_file_chooser(_wrap_async_event_predicate("filechooser", predicate, self), timeout=timeout),
            _wrap_async_file_chooser,
        )

    def expect_popup(self, predicate: Any = None, *, timeout: Optional[float] = None) -> _AsyncEventContextManager:
        return _AsyncEventContextManager(
            self._sync.expect_popup(_wrap_async_event_predicate("popup", predicate, self), timeout=timeout),
            lambda value: _wrap_async_event_value("popup", value),
        )

    def expect_request_finished(self, predicate: Any = None, *, timeout: Optional[float] = None) -> _AsyncEventContextManager:
        return _AsyncEventContextManager(
            self._sync.expect_request_finished(_wrap_async_event_predicate("requestfinished", predicate, self), timeout=timeout),
            _wrap_async_request,
        )

    def expect_navigation(
        self,
        *,
        url: Any = None,
        wait_until: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> _AsyncEventContextManager:
        return _AsyncEventContextManager(
            self._sync.expect_navigation(url=url, wait_until=wait_until, timeout=timeout),
            _wrap_async_response,
        )

    def expect_websocket(self, predicate: Any = None, *, timeout: Optional[float] = None) -> _AsyncEventContextManager:
        return _AsyncEventContextManager(
            self._sync.expect_websocket(_wrap_async_event_predicate("websocket", predicate, self), timeout=timeout),
            _wrap_async_websocket,
        )

    def expect_worker(self, predicate: Any = None, *, timeout: Optional[float] = None) -> _AsyncEventContextManager:
        return _AsyncEventContextManager(
            self._sync.expect_worker(_wrap_async_event_predicate("worker", predicate, self), timeout=timeout),
            _wrap_async_worker,
        )

    async def wait_for_event(self, event: str, predicate: Any = None, *, timeout: Optional[float] = None) -> Any:
        value = await _run_sync_call(
            self._sync.wait_for_event,
            event,
            _wrap_async_event_predicate(event, predicate, self),
            timeout=timeout,
        )
        return _wrap_async_event_value(event, value)

    def expect_event(self, event: str, predicate: Any = None, *, timeout: Optional[float] = None) -> _AsyncEventContextManager:
        return _AsyncEventContextManager(
            self._sync.expect_event(event, _wrap_async_event_predicate(event, predicate, self), timeout=timeout),
            lambda value: _wrap_async_event_value(event, value),
        )

    def on(self, event: str, f: Any) -> None:
        self._sync.on(event, _wrap_async_event_handler(self, event, f))

    def once(self, event: str, f: Any) -> None:
        self._sync.once(event, _wrap_async_event_handler(self, event, f))

    def remove_listener(self, event: str, f: Any) -> None:
        self._sync.remove_listener(event, _forget_async_event_handler(self, event, f))

    async def route(self, url: Any, handler: Any, *, times: Optional[int] = None) -> None:
        await _run_sync_call(self._sync.route, url, _remember_async_route_handler(self, url, handler), times=times)

    async def unroute(self, url: Any, handler: Any = None) -> None:
        await _run_sync_call(self._sync.unroute, url, _forget_async_route_handler(self, url, handler))

    async def unroute_all(self, *, behavior: Optional[str] = None) -> None:
        _forget_all_async_route_handlers(self)
        await _run_sync_call(self._sync.unroute_all, behavior=behavior)

    async def route_web_socket(self, url: Any, handler: Any) -> None:
        await _run_sync_call(self._sync.route_web_socket, url, _wrap_async_websocket_route_handler(handler, self))

    async def screenshot(
        self,
        *,
        timeout: Optional[float] = None,
        type: Optional[str] = None,
        path: Optional[str] = None,
        quality: Optional[int] = None,
        omit_background: Optional[bool] = None,
        full_page: Optional[bool] = None,
        clip: Optional[dict[str, float]] = None,
        animations: Optional[str] = None,
        caret: Optional[str] = None,
        scale: Optional[str] = None,
        mask: Any = None,
        mask_color: Optional[str] = None,
        style: Optional[str] = None,
    ) -> bytes:
        sync_mask = mask
        if isinstance(sync_mask, _AsyncWrapper):
            sync_mask = sync_mask._sync
        elif isinstance(sync_mask, (list, tuple)):
            sync_mask = [item._sync if isinstance(item, _AsyncWrapper) else item for item in sync_mask]
        if not _native_page_hot_path_supported(self._sync) or any(
            value is not None for value in (animations, caret, scale, mask, mask_color, style)
        ):
            return await _run_sync_call(
                self._sync.screenshot,
                timeout=timeout,
                type=type,
                path=path,
                quality=quality,
                omit_background=omit_background,
                full_page=full_page,
                clip=clip,
                animations=animations,
                caret=caret,
                scale=scale,
                mask=sync_mask,
                mask_color=mask_color,
                style=style,
            )
        normalized_path = _normalize_path_arg(path)
        normalized_full_page = False if full_page is None else bool(
            _normalize_action_boolean(full_page, method="Page.screenshot", name="full_page")
        )
        normalized_omit_background = _normalize_action_boolean(
            omit_background,
            method="Page.screenshot",
            name="omit_background",
        )
        normalized_timeout = (
            self._sync._default_timeout
            if timeout is None
            else _validate_timeout_value(timeout, method="Page.screenshot")
        )
        image_type, normalized_quality = _normalize_screenshot_options(
            method="Page.screenshot",
            path=normalized_path,
            image_type=type,
            quality=quality,
        )
        normalized_clip = self._sync._normalize_screenshot_clip(
            clip,
            method="Page.screenshot",
            full_page=normalized_full_page,
            scale="device",
        )
        return await _await_native_method(
            "Page.screenshot",
            self._sync._core.screenshot_async(
                normalized_path,
                normalized_full_page,
                None
                if normalized_clip is None
                else json.dumps(normalized_clip, separators=(",", ":")),
                normalized_timeout,
                image_type,
                normalized_quality,
                normalized_omit_background,
            )
        )

    def is_closed(self) -> bool:
        return self._sync.is_closed()

    @property
    def workers(self) -> list["AsyncWorker"]:
        return [_wrap_async_worker(worker) for worker in self._sync.workers]

    async def add_locator_handler(
        self,
        locator: Any,
        handler: Any,
        *,
        no_wait_after: Optional[bool] = None,
        times: Optional[int] = None,
    ) -> None:
        sync_locator = locator._sync if isinstance(locator, AsyncLocator) else locator
        await _run_sync_call(
            self._sync.add_locator_handler,
            sync_locator,
            _wrap_async_locator_handler(handler, self),
            no_wait_after=no_wait_after,
            times=times,
        )

    async def remove_locator_handler(self, locator: Any) -> None:
        sync_locator = locator._sync if isinstance(locator, AsyncLocator) else locator
        await _run_sync_call(self._sync.remove_locator_handler, sync_locator)

    async def close(self, *, run_before_unload: Optional[bool] = None, reason: Optional[str] = None) -> None:
        if not isinstance(self._sync, SyncPage):
            await _single_flight_close(
                self._sync,
                lambda: _run_sync_call(
                    self._sync.close,
                    run_before_unload=run_before_unload,
                    reason=reason,
                ),
            )
            return
        if self._sync._closed or getattr(
            self._sync, "_rustwright_async_close_state", _CLOSE_OPEN
        ) == _CLOSE_CLOSED:
            return
        if (
            self._sync._video is not None
            or self._sync._har_recordings
            or self._sync._fetch_enabled
            or self._sync._binding_server is not None
            or self._sync._crash_session is not None
        ):
            await _single_flight_close(
                self._sync,
                lambda: _run_sync_call(
                    self._sync.close,
                    run_before_unload=run_before_unload,
                    reason=reason,
                ),
            )
            return
        normalized_reason = None
        if reason is not None:
            normalized_reason = _normalize_string_option(reason, method="Page.close", name="reason")
        unload = bool(run_before_unload or False)
        await _single_flight_close(
            self._sync,
            lambda: self._close_native(normalized_reason, unload),
        )

    async def _close_native(self, normalized_reason: Optional[str], unload: bool) -> None:
        self._sync._closed_reason = normalized_reason
        if self._sync._owns_context and self._sync._context is not None:
            await _run_sync_call(self._sync._context._cleanup_default_context_state)
        dialog_dispatch_count = self._sync._dialog_dispatch_count
        try:
            try:
                await _await_native_method(
                    "Page.close",
                    self._sync._core.close_async(self._sync._default_timeout, unload)
                )
            except Error as exc:
                if not _is_ignorable_close_error(exc):
                    raise
            if unload and self._sync._event_handlers.get("dialog"):
                deadline = time.monotonic() + min(self._sync._default_timeout / 1000, 0.5)
                while (
                    self._sync._dialog_dispatch_count == dialog_dispatch_count
                    and time.monotonic() < deadline
                ):
                    await asyncio.sleep(0.01)
        finally:
            self._sync._stop_event_pump()
            pump_task = self._event_pump_task
            if pump_task is not None and pump_task is not asyncio.current_task():
                await asyncio.gather(pump_task, return_exceptions=True)
        self._sync._closed = True
        self._sync._closing = True
        self._sync._mark_owned_cdp_sessions_closed()
        if self._sync._context is not None and self._sync in self._sync._context._pages:
            self._sync._context._pages.remove(self._sync)
        _emit_event(self._sync._event_handlers, "close", self._sync)
        if (
            self._sync._owns_context
            and self._sync._context is not None
            and getattr(
                self._sync._context,
                "_rustwright_async_close_state",
                _CLOSE_OPEN,
            )
            != _CLOSE_CLOSING
        ):
            await _wrap_async_browser_context(self._sync._context).close()


class AsyncJSHandle(_AsyncJSHandleGeneratedMixin, _AsyncWrapper):
    def __str__(self) -> str:
        return str(self._sync)

    def __repr__(self) -> str:
        return repr(self._sync)

    def as_element(self) -> Optional["AsyncElementHandle"]:
        handle = self._sync.as_element()
        return _wrap_async_element_handle(handle)


class AsyncFrame(_AsyncFrameGeneratedMixin, _AsyncWrapper):
    @property
    def name(self) -> str:
        return self._sync.name

    @property
    def url(self) -> str:
        return self._sync.url

    @property
    def page(self) -> AsyncPage:
        return _wrap_async_page(self._sync.page)

    @property
    def parent_frame(self) -> Optional["AsyncFrame"]:
        frame = self._sync.parent_frame
        return _wrap_async_frame(frame)

    @property
    def child_frames(self) -> list["AsyncFrame"]:
        return [_wrap_async_frame(frame) for frame in self._sync.child_frames]

    def locator(
        self,
        selector: str,
        *,
        has_text: Any = None,
        has_not_text: Any = None,
        has: Optional["AsyncLocator"] = None,
        has_not: Optional["AsyncLocator"] = None,
    ) -> "AsyncLocator":
        sync_has = has._sync if isinstance(has, _AsyncWrapper) else has
        sync_has_not = has_not._sync if isinstance(has_not, _AsyncWrapper) else has_not
        return AsyncLocator(
            self._sync.locator(
                selector,
                has_text=has_text,
                has_not_text=has_not_text,
                has=sync_has,
                has_not=sync_has_not,
            )
        )

    def frame_locator(self, selector: str) -> "AsyncFrameLocator":
        return AsyncFrameLocator(self._sync.frame_locator(selector))

    def get_by_text(self, text: str, *, exact: Optional[bool] = None) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_text(text, exact=bool(exact) if exact is not None else False))

    def get_by_role(
        self,
        role: str,
        *,
        checked: Optional[bool] = None,
        disabled: Optional[bool] = None,
        expanded: Optional[bool] = None,
        include_hidden: Optional[bool] = None,
        level: Optional[int] = None,
        name: Any = None,
        pressed: Optional[bool] = None,
        selected: Optional[bool] = None,
        exact: Optional[bool] = None,
    ) -> "AsyncLocator":
        return AsyncLocator(
            self._sync.get_by_role(
                role,
                checked=checked,
                disabled=disabled,
                expanded=expanded,
                include_hidden=include_hidden,
                level=level,
                name=name,
                pressed=pressed,
                selected=selected,
                exact=exact,
            )
        )

    def get_by_test_id(self, test_id: str) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_test_id(test_id))

    def get_by_placeholder(self, text: str, *, exact: Optional[bool] = None) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_placeholder(text, exact=bool(exact) if exact is not None else False))

    def get_by_label(self, text: str, *, exact: Optional[bool] = None) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_label(text, exact=bool(exact) if exact is not None else False))

    def get_by_alt_text(self, text: str, *, exact: Optional[bool] = None) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_alt_text(text, exact=bool(exact) if exact is not None else False))

    def get_by_title(self, text: str, *, exact: Optional[bool] = None) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_title(text, exact=bool(exact) if exact is not None else False))

    async def wait_for_load_state(self, state: Optional[str] = None, *, timeout: Optional[float] = None) -> None:
        await _run_sync_wait_sliced(self._sync, self._sync.wait_for_load_state, "load" if state is None else state, timeout=timeout)

    def is_detached(self) -> bool:
        return self._sync.is_detached()

    def expect_navigation(
        self,
        *,
        url: Any = None,
        wait_until: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> _AsyncEventContextManager:
        return _AsyncEventContextManager(
            self._sync.expect_navigation(url=url, wait_until=wait_until, timeout=timeout),
            _wrap_async_response,
        )


class AsyncFrameLocator(_AsyncWrapper):
    def nth(self, index: Any) -> "AsyncFrameLocator":
        return AsyncFrameLocator(self._sync.nth(index))

    @property
    def first(self) -> "AsyncFrameLocator":
        return AsyncFrameLocator(self._sync.first)

    @property
    def last(self) -> "AsyncFrameLocator":
        return AsyncFrameLocator(self._sync.last)

    def locator(
        self,
        selector_or_locator: Any,
        *,
        has_text: Any = None,
        has_not_text: Any = None,
        has: Optional["AsyncLocator"] = None,
        has_not: Optional["AsyncLocator"] = None,
    ) -> "AsyncLocator":
        sync_selector = selector_or_locator._sync if isinstance(selector_or_locator, AsyncLocator) else selector_or_locator
        return AsyncLocator(
            self._sync.locator(
                sync_selector,
                has_text=has_text,
                has_not_text=has_not_text,
                has=has._sync if isinstance(has, AsyncLocator) else has,
                has_not=has_not._sync if isinstance(has_not, AsyncLocator) else has_not,
            )
        )

    def frame_locator(self, selector: str) -> "AsyncFrameLocator":
        return AsyncFrameLocator(self._sync.frame_locator(selector))

    @property
    def owner(self) -> "AsyncLocator":
        return AsyncLocator(self._sync.owner)

    def get_by_text(self, text: Any, *, exact: Optional[bool] = None) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_text(text, exact=bool(exact) if exact is not None else False))

    def get_by_role(
        self,
        role: str,
        *,
        checked: Optional[bool] = None,
        disabled: Optional[bool] = None,
        expanded: Optional[bool] = None,
        include_hidden: Optional[bool] = None,
        level: Optional[int] = None,
        name: Any = None,
        pressed: Optional[bool] = None,
        selected: Optional[bool] = None,
        exact: Optional[bool] = None,
    ) -> "AsyncLocator":
        return AsyncLocator(
            self._sync.get_by_role(
                role,
                checked=checked,
                disabled=disabled,
                expanded=expanded,
                include_hidden=include_hidden,
                level=level,
                name=name,
                pressed=pressed,
                selected=selected,
                exact=exact,
            )
        )

    def get_by_test_id(self, test_id: str) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_test_id(test_id))

    def get_by_placeholder(self, text: Any, *, exact: Optional[bool] = None) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_placeholder(text, exact=bool(exact) if exact is not None else False))

    def get_by_label(self, text: Any, *, exact: Optional[bool] = None) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_label(text, exact=bool(exact) if exact is not None else False))

    def get_by_alt_text(self, text: Any, *, exact: Optional[bool] = None) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_alt_text(text, exact=bool(exact) if exact is not None else False))

    def get_by_title(self, text: Any, *, exact: Optional[bool] = None) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_title(text, exact=bool(exact) if exact is not None else False))


class AsyncLocator(_AsyncLocatorGeneratedMixin, _AsyncWrapper):
    @property
    def page(self) -> AsyncPage:
        return _wrap_async_page(self._sync.page)

    def nth(self, index: Any) -> "AsyncLocator":
        return AsyncLocator(self._sync.nth(index))

    @property
    def first(self) -> "AsyncLocator":
        return AsyncLocator(self._sync.first)

    @property
    def last(self) -> "AsyncLocator":
        return AsyncLocator(self._sync.last)

    def filter(
        self,
        *,
        has_text: Any = None,
        has_not_text: Any = None,
        has: Optional["AsyncLocator"] = None,
        has_not: Optional["AsyncLocator"] = None,
        visible: Optional[bool] = None,
    ) -> "AsyncLocator":
        sync_has = has._sync if isinstance(has, AsyncLocator) else has
        sync_has_not = has_not._sync if isinstance(has_not, AsyncLocator) else has_not
        return AsyncLocator(
            self._sync.filter(
                has_text=has_text,
                has_not_text=has_not_text,
                has=sync_has,
                has_not=sync_has_not,
                visible=visible,
            )
        )

    def and_(self, locator: "AsyncLocator") -> "AsyncLocator":
        if not isinstance(locator, AsyncLocator):
            getattr(locator, "_impl_obj")
        return AsyncLocator(self._sync.and_(locator._sync))

    def or_(self, locator: "AsyncLocator") -> "AsyncLocator":
        if not isinstance(locator, AsyncLocator):
            getattr(locator, "_impl_obj")
        return AsyncLocator(self._sync.or_(locator._sync))

    def frame_locator(self, selector: str) -> AsyncFrameLocator:
        return AsyncFrameLocator(self._sync.frame_locator(selector))

    def locator(
        self,
        selector_or_locator: Any,
        *,
        has_text: Any = None,
        has_not_text: Any = None,
        has: Optional["AsyncLocator"] = None,
        has_not: Optional["AsyncLocator"] = None,
    ) -> "AsyncLocator":
        if isinstance(selector_or_locator, AsyncLocator):
            sync_selector = selector_or_locator._sync
        else:
            if not isinstance(selector_or_locator, str):
                getattr(selector_or_locator, "_frame")
            sync_selector = selector_or_locator
        sync_has = has._sync if isinstance(has, AsyncLocator) else has
        sync_has_not = has_not._sync if isinstance(has_not, AsyncLocator) else has_not
        return AsyncLocator(
            self._sync.locator(
                sync_selector,
                has_text=has_text,
                has_not_text=has_not_text,
                has=sync_has,
                has_not=sync_has_not,
            )
        )

    def get_by_text(self, text: Any, *, exact: Optional[bool] = None) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_text(text, exact=bool(exact)))

    def get_by_role(
        self,
        role: str,
        *,
        checked: Optional[bool] = None,
        disabled: Optional[bool] = None,
        expanded: Optional[bool] = None,
        include_hidden: Optional[bool] = None,
        level: Optional[int] = None,
        name: Any = None,
        pressed: Optional[bool] = None,
        selected: Optional[bool] = None,
        exact: Optional[bool] = None,
    ) -> "AsyncLocator":
        return AsyncLocator(
            self._sync.get_by_role(
                role,
                checked=checked,
                disabled=disabled,
                expanded=expanded,
                include_hidden=include_hidden,
                level=level,
                name=name,
                pressed=pressed,
                selected=selected,
                exact=exact,
            )
        )

    def get_by_test_id(self, test_id: str) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_test_id(test_id))

    def get_by_placeholder(self, text: Any, *, exact: Optional[bool] = None) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_placeholder(text, exact=bool(exact)))

    def get_by_label(self, text: Any, *, exact: Optional[bool] = None) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_label(text, exact=bool(exact)))

    def get_by_alt_text(self, text: Any, *, exact: Optional[bool] = None) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_alt_text(text, exact=bool(exact)))

    def get_by_title(self, text: Any, *, exact: Optional[bool] = None) -> "AsyncLocator":
        return AsyncLocator(self._sync.get_by_title(text, exact=bool(exact)))

    async def drag_to(
        self,
        target: "AsyncLocator",
        *,
        force: Optional[bool] = None,
        no_wait_after: Optional[bool] = None,
        timeout: Optional[float] = None,
        trial: Optional[bool] = None,
        source_position: Any = None,
        target_position: Any = None,
        steps: Optional[int] = None,
    ) -> None:
        if not isinstance(target, _AsyncWrapper):
            getattr(target, "_impl_obj")
        await _run_sync_wait_sliced(
            self._sync,
            self._sync.drag_to,
            target._sync,
            force=force,
            no_wait_after=no_wait_after,
            timeout=timeout,
            trial=trial,
            source_position=source_position,
            target_position=target_position,
            steps=steps,
        )

    @property
    def content_frame(self) -> Optional["AsyncFrameLocator"]:
        frame_locator = self._sync.content_frame
        return None if frame_locator is None else AsyncFrameLocator(frame_locator)

    def describe(self, description: str) -> "AsyncLocator":
        return AsyncLocator(self._sync.describe(description))

    @property
    def description(self) -> Optional[str]:
        return self._sync.description


class AsyncElementHandle(_AsyncElementHandleGeneratedMixin, _AsyncWrapper):
    def __str__(self) -> str:
        return str(self._sync)

    def __repr__(self) -> str:
        return repr(self._sync)

    def as_element(self) -> "AsyncElementHandle":
        return self



class AsyncKeyboard(_AsyncKeyboardGeneratedMixin, _AsyncWrapper):
    pass


class AsyncMouse(_AsyncMouseGeneratedMixin, _AsyncWrapper):
    pass


class AsyncTouchscreen(_AsyncTouchscreenGeneratedMixin, _AsyncWrapper):
    pass


class AsyncAPIResponse(_AsyncAPIResponseGeneratedMixin, _AsyncWrapper):
    @property
    def url(self) -> str:
        return self._sync.url

    @property
    def ok(self) -> bool:
        return self._sync.ok

    @property
    def status(self) -> int:
        return self._sync.status

    @property
    def status_text(self) -> str:
        return self._sync.status_text

    @property
    def headers(self) -> dict[str, str]:
        return self._sync.headers

    @property
    def headers_array(self) -> list[dict[str, str]]:
        return self._sync.headers_array


class AsyncRoute(_AsyncRouteGeneratedMixin, _AsyncWrapper):
    @property
    def request(self) -> AsyncRequest:
        return _wrap_async_request(self._sync.request)


class AsyncAPIRequest(_AsyncAPIRequestGeneratedMixin, _AsyncWrapper):
    pass


class AsyncAPIRequestContext(_AsyncAPIRequestContextGeneratedMixin, _AsyncWrapper):
    def __init__(self, sync_obj: Any):
        super().__init__(sync_obj)
        self._request_lock = asyncio.Lock()

    async def _run_request(
        self,
        request_method: Callable[..., SyncAPIResponse],
        *args: Any,
        **kwargs: Any,
    ) -> AsyncAPIResponse:
        async with self._request_lock:
            return AsyncAPIResponse(await _run_sync_call(request_method, *args, **kwargs))

    async def fetch(
        self,
        url_or_request: Any,
        *,
        params: Any = None,
        method: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        data: Any = None,
        form: Optional[dict[str, Any]] = None,
        multipart: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
        fail_on_status_code: Optional[bool] = None,
        ignore_https_errors: Optional[bool] = None,
        max_redirects: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> AsyncAPIResponse:
        if isinstance(url_or_request, AsyncRequest):
            url_or_request = url_or_request._sync
        return await self._run_request(
            self._sync.fetch,
                url_or_request,
                params=params,
                method=method,
                headers=headers,
                data=data,
                form=form,
                multipart=multipart,
                timeout=timeout,
                fail_on_status_code=fail_on_status_code,
                ignore_https_errors=ignore_https_errors,
                max_redirects=max_redirects,
                max_retries=max_retries,
        )

    async def get(
        self,
        url: str,
        *,
        params: Any = None,
        headers: Optional[dict[str, str]] = None,
        data: Any = None,
        form: Optional[dict[str, Any]] = None,
        multipart: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
        fail_on_status_code: Optional[bool] = None,
        ignore_https_errors: Optional[bool] = None,
        max_redirects: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> AsyncAPIResponse:
        return await self._run_request(
            self._sync.get,
                url,
                params=params,
                headers=headers,
                data=data,
                form=form,
                multipart=multipart,
                timeout=timeout,
                fail_on_status_code=fail_on_status_code,
                ignore_https_errors=ignore_https_errors,
                max_redirects=max_redirects,
                max_retries=max_retries,
        )

    async def post(
        self,
        url: str,
        *,
        params: Any = None,
        headers: Optional[dict[str, str]] = None,
        data: Any = None,
        form: Optional[dict[str, Any]] = None,
        multipart: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
        fail_on_status_code: Optional[bool] = None,
        ignore_https_errors: Optional[bool] = None,
        max_redirects: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> AsyncAPIResponse:
        return await self._run_request(
            self._sync.post,
                url,
                params=params,
                headers=headers,
                data=data,
                form=form,
                multipart=multipart,
                timeout=timeout,
                fail_on_status_code=fail_on_status_code,
                ignore_https_errors=ignore_https_errors,
                max_redirects=max_redirects,
                max_retries=max_retries,
        )

    async def put(
        self,
        url: str,
        *,
        params: Any = None,
        headers: Optional[dict[str, str]] = None,
        data: Any = None,
        form: Optional[dict[str, Any]] = None,
        multipart: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
        fail_on_status_code: Optional[bool] = None,
        ignore_https_errors: Optional[bool] = None,
        max_redirects: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> AsyncAPIResponse:
        return await self._run_request(
            self._sync.put,
                url,
                params=params,
                headers=headers,
                data=data,
                form=form,
                multipart=multipart,
                timeout=timeout,
                fail_on_status_code=fail_on_status_code,
                ignore_https_errors=ignore_https_errors,
                max_redirects=max_redirects,
                max_retries=max_retries,
        )

    async def patch(
        self,
        url: str,
        *,
        params: Any = None,
        headers: Optional[dict[str, str]] = None,
        data: Any = None,
        form: Optional[dict[str, Any]] = None,
        multipart: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
        fail_on_status_code: Optional[bool] = None,
        ignore_https_errors: Optional[bool] = None,
        max_redirects: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> AsyncAPIResponse:
        return await self._run_request(
            self._sync.patch,
                url,
                params=params,
                headers=headers,
                data=data,
                form=form,
                multipart=multipart,
                timeout=timeout,
                fail_on_status_code=fail_on_status_code,
                ignore_https_errors=ignore_https_errors,
                max_redirects=max_redirects,
                max_retries=max_retries,
        )

    async def delete(
        self,
        url: str,
        *,
        params: Any = None,
        headers: Optional[dict[str, str]] = None,
        data: Any = None,
        form: Optional[dict[str, Any]] = None,
        multipart: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
        fail_on_status_code: Optional[bool] = None,
        ignore_https_errors: Optional[bool] = None,
        max_redirects: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> AsyncAPIResponse:
        return await self._run_request(
            self._sync.delete,
                url,
                params=params,
                headers=headers,
                data=data,
                form=form,
                multipart=multipart,
                timeout=timeout,
                fail_on_status_code=fail_on_status_code,
                ignore_https_errors=ignore_https_errors,
                max_redirects=max_redirects,
                max_retries=max_retries,
        )

    async def head(
        self,
        url: str,
        *,
        params: Any = None,
        headers: Optional[dict[str, str]] = None,
        data: Any = None,
        form: Optional[dict[str, Any]] = None,
        multipart: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
        fail_on_status_code: Optional[bool] = None,
        ignore_https_errors: Optional[bool] = None,
        max_redirects: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> AsyncAPIResponse:
        return await self._run_request(
            self._sync.head,
                url,
                params=params,
                headers=headers,
                data=data,
                form=form,
                multipart=multipart,
                timeout=timeout,
                fail_on_status_code=fail_on_status_code,
                ignore_https_errors=ignore_https_errors,
                max_redirects=max_redirects,
                max_retries=max_retries,
        )


class AsyncWebSocket(_AsyncWrapper):
    @property
    def url(self) -> str:
        return self._sync.url

    def is_closed(self) -> bool:
        return self._sync.is_closed()

    def _event_predicate(self, event: str, predicate: Any = None) -> Any:
        if predicate is None:
            return None

        def wrapper(value: Any) -> bool:
            mapped = self if event == "close" and value is self._sync else _wrap_async_event_value(event, value)
            return bool(_run_callback_on_owner_loop(self._loop, lambda: predicate(mapped)))

        return wrapper

    async def wait_for_event(self, event: str, predicate: Any = None, *, timeout: Optional[float] = None) -> Any:
        value = await _run_sync_call(
            self._sync.wait_for_event,
            event,
            self._event_predicate(event, predicate),
            timeout=timeout,
        )
        if event == "close" and value is self._sync:
            return self
        return _wrap_async_event_value(event, value)

    def expect_event(self, event: str, predicate: Any = None, *, timeout: Optional[float] = None) -> _AsyncEventContextManager:
        def mapper(value: Any) -> Any:
            if event == "close" and value is self._sync:
                return self
            return _wrap_async_event_value(event, value)

        return _AsyncEventContextManager(
            self._sync.expect_event(event, self._event_predicate(event, predicate), timeout=timeout),
            mapper,
        )


class AsyncWebSocketRoute(_AsyncWebSocketRouteGeneratedMixin, _AsyncWrapper):
    @property
    def url(self) -> str:
        return self._sync.url

    def connect_to_server(self) -> "AsyncWebSocketRoute":
        return AsyncWebSocketRoute(self._sync.connect_to_server())

    def send(self, message: str | bytes) -> None:
        self._sync.send(message)

    def on_message(self, handler: Any) -> None:
        def wrapper(message: str | bytes) -> None:
            _run_callback_on_owner_loop(self._loop, lambda: handler(message))

        self._sync.on_message(wrapper)

    def on_close(self, handler: Any) -> None:
        def wrapper(code: Optional[int], reason: Optional[str]) -> None:
            _run_callback_on_owner_loop(self._loop, lambda: handler(code, reason))

        self._sync.on_close(wrapper)


class AsyncWorker(_AsyncWorkerGeneratedMixin, _AsyncWrapper):
    @property
    def url(self) -> str:
        return self._sync.url

    def _event_predicate(self, event: str, predicate: Any = None) -> Any:
        if predicate is None:
            return None

        def wrapper(value: Any) -> bool:
            mapped = self if event == "close" and value is self._sync else _wrap_async_event_value(event, value)
            return bool(_run_callback_on_owner_loop(self._loop, lambda: predicate(mapped)))

        return wrapper

    def expect_event(self, event: str, predicate: Any = None, *, timeout: Optional[float] = None) -> _AsyncEventContextManager:
        def mapper(value: Any) -> Any:
            if event == "close" and value is self._sync:
                return self
            return _wrap_async_event_value(event, value)

        return _AsyncEventContextManager(
            self._sync.expect_event(event, self._event_predicate(event, predicate), timeout=timeout),
            mapper,
        )


APIRequest = AsyncAPIRequest
APIRequestContext = AsyncAPIRequestContext
APIResponse = AsyncAPIResponse
APIResponseAssertions = AsyncAPIResponseAssertions
APIResponseAssertionsImpl = _AsyncAPIResponseAssertionsImpl
Browser = AsyncBrowser
ChromiumBrowserContext = AsyncBrowserContext
BrowserContext = AsyncBrowserContext
BrowserType = AsyncBrowserType
CDPSession = AsyncCDPSession
ConsoleMessage = AsyncConsoleMessage
Dialog = AsyncDialog
Download = AsyncDownload
ElementHandle = AsyncElementHandle
FileChooser = AsyncFileChooser
Frame = AsyncFrame
FrameLocator = AsyncFrameLocator
JSHandle = AsyncJSHandle
Keyboard = AsyncKeyboard
Locator = AsyncLocator
LocatorAssertions = AsyncLocatorAssertions
LocatorAssertionsImpl = _AsyncLocatorAssertionsImpl
Mouse = AsyncMouse
Page = AsyncPage
PageAssertions = AsyncPageAssertions
PageAssertionsImpl = _AsyncPageAssertionsImpl
Playwright = AsyncPlaywright
Expect = AsyncExpect
Request = AsyncRequest
Response = AsyncResponse
Route = AsyncRoute
Selectors = AsyncSelectors
Touchscreen = AsyncTouchscreen
WebError = AsyncWebError
WebSocket = AsyncWebSocket
WebSocketRoute = AsyncWebSocketRoute
Worker = AsyncWorker
Video = AsyncVideo
expect = AsyncExpect()

__all__ = [
    "APIRequest",
    "APIRequestContext",
    "APIResponse",
    "APIResponseAssertions",
    "APIResponseAssertionsImpl",
    "BackendMarker",
    "Browser",
    "BrowserBindResult",
    "BrowserContext",
    "BrowserType",
    "CDPSession",
    "ChromiumBrowserContext",
    "ConsoleMessage",
    "Cookie",
    "DebuggerLocation",
    "DebuggerPausedDetails",
    "Dialog",
    "Download",
    "ElementHandle",
    "Error",
    "Expect",
    "FileChooser",
    "FilePayload",
    "FloatRect",
    "Frame",
    "FrameLocator",
    "Geolocation",
    "HttpCredentials",
    "JSHandle",
    "Keyboard",
    "Locator",
    "LocatorAssertions",
    "LocatorAssertionsImpl",
    "Mouse",
    "Page",
    "PageAssertions",
    "PageAssertionsImpl",
    "PdfMargins",
    "Playwright",
    "PlaywrightContextManager",
    "Position",
    "ProxySettings",
    "Request",
    "ResourceTiming",
    "Response",
    "Route",
    "Selectors",
    "SourceLocation",
    "StorageState",
    "StorageStateCookie",
    "TimeoutError",
    "Touchscreen",
    "ViewportSize",
    "Video",
    "WebError",
    "WebSocket",
    "WebSocketRoute",
    "Worker",
    "async_playwright",
    "backend_marker",
    "expect",
]
