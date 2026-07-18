from __future__ import annotations

from pathlib import Path
import re
import shutil
from typing import Any, Callable, Dict, Literal, Optional, Pattern, Protocol, Sequence, Union

import pytest

from .sync_api import (
    Browser,
    BrowserContext,
    BrowserType,
    Geolocation,
    HttpCredentials,
    Page,
    Playwright,
    ProxySettings,
    StorageState,
    ViewportSize,
    sync_playwright,
)
from ._devices import DEVICE_DESCRIPTORS


def _registered_option_strings(parser: pytest.Parser) -> set[str]:
    """Collect every option string already registered on the parser.

    Conflicting strings must be skipped at registration time: pytest
    materializes the argparse parser only later, where a duplicate surfaces as
    an unrecoverable ``argparse.ArgumentError`` during startup.

    Reads pytest's private ``Parser._groups``/``Parser._anonymous`` (stable
    across pytest 7-9). If a future pytest renames them, this degrades to an
    empty set and duplicate registration aborts startup again — update the
    attribute names here in that case.
    """
    names: set[str] = set()
    groups = list(getattr(parser, "_groups", []))
    anonymous = getattr(parser, "_anonymous", None)
    if anonymous is not None:
        groups.append(anonymous)
    for group in groups:
        for option in getattr(group, "options", []):
            try:
                names.update(option.names())
            except Exception:
                continue
    return names


def _addoption(group: Any, taken: set[str], *args: Any, **kwargs: Any) -> None:
    """Register an option unless another plugin already owns the flag.

    pytest-playwright and pytest-base-url register overlapping option strings
    (``--browser``, ``--base-url``, ...). When they are installed alongside
    Rustwright and load first, re-registering must not abort pytest startup;
    option values are then read through :func:`_getoption` fallbacks.
    """
    if any(arg in taken for arg in args if isinstance(arg, str)):
        return
    try:
        group.addoption(*args, **kwargs)
    except ValueError:
        pass


def _getoption(config: pytest.Config, *dests: str, default: Any = None) -> Any:
    """Read the first available of several option destinations."""
    for dest in dests:
        try:
            value = config.getoption(dest)
        except (KeyError, ValueError):
            continue
        if value is not None:
            return value
    return default


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("rustwright", "Rustwright browser automation")
    taken = _registered_option_strings(parser)
    _addoption(
        group,
        taken,
        "--browser",
        action="append",
        choices=["chromium", "firefox", "webkit"],
        dest="rustwright_browser",
        help="Browser engine to run tests against. Can be passed multiple times.",
    )
    _addoption(group, taken, "--headed", action="store_true", help="Run browser headed.")
    _addoption(group, taken, "--browser-channel", default=None, help="Browser channel name accepted for Playwright CLI compatibility.")
    _addoption(group, taken, "--slowmo", type=int, default=0, help="Slow motion delay in milliseconds.")
    _addoption(group, taken, "--device", default=None, help="Device descriptor name to use for browser contexts.")
    _addoption(group, taken, "--output", default="test-results", help="Directory for browser artifacts.")
    _addoption(group, taken, "--tracing", choices=["on", "off", "retain-on-failure"], default="off")
    _addoption(group, taken, "--video", choices=["on", "off", "retain-on-failure"], default="off")
    _addoption(group, taken, "--screenshot", choices=["on", "off", "only-on-failure"], default="off")
    _addoption(
        group,
        taken,
        "--ignore-https-errors",
        action="store_true",
        default=False,
        help="Ignore HTTPS certificate errors in browser contexts.",
    )
    _addoption(
        group,
        taken,
        "--full-page-screenshot",
        action="store_true",
        default=False,
        help="Capture full-page screenshots for pytest screenshot artifacts.",
    )
    _addoption(group, taken, "--base-url", default=None, help="Base URL used by page.goto() and API requests.")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "skip_browser(*names): mark test to be skipped for browsers")
    config.addinivalue_line("markers", "only_browser(*names): mark test to run only for browsers")
    config.addinivalue_line(
        "markers",
        "browser_context_args(**kwargs): provide additional arguments to browser.new_context()",
    )


@pytest.fixture(scope="session", autouse=True)
def delete_output_dir(pytestconfig: pytest.Config) -> None:
    output_dir = Path(str(_getoption(pytestconfig, "output", default="test-results"))).resolve()
    if not output_dir.exists():
        return
    try:
        shutil.rmtree(output_dir)
    except (FileNotFoundError, PermissionError):
        return
    except OSError as error:
        if getattr(error, "errno", None) != 16:
            raise
        for entry in output_dir.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                try:
                    entry.unlink()
                except FileNotFoundError:
                    pass


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Any:
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


def _slugify_nodeid(nodeid: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", nodeid).strip("-")
    return value[:180] or "test"


def _test_artifact_dir(output_path: str | Path, node: pytest.Item) -> Path:
    path = Path(output_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _unique_artifact_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    index = 2
    while True:
        candidate = directory / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def _test_failed(node: pytest.Item) -> bool:
    return bool(getattr(node, "rep_call", None) and getattr(node.rep_call, "failed", False))


def _browser_names_from_marker(marker: pytest.Mark) -> list[str]:
    names: list[str] = []
    for value in marker.args:
        if isinstance(value, (list, tuple, set, frozenset)):
            names.extend(str(item) for item in value)
        else:
            names.append(str(value))
    return names


def _get_browser_skiplist(item: pytest.Item, values: list[str]) -> list[str]:
    allowed: set[str] = set()
    for marker in item.iter_markers(name="only_browser"):
        allowed.update(_browser_names_from_marker(marker))
    skipped = [value for value in values if allowed and value not in allowed]
    for marker in item.iter_markers(name="skip_browser"):
        skipped.extend(_browser_names_from_marker(marker))
    return list(dict.fromkeys(skipped))


def _selected_browsers(config: pytest.Config) -> list[str]:
    browsers = _getoption(config, "rustwright_browser", "browser") or ["chromium"]
    # The "browser" fallback dest belongs to whatever plugin won the --browser
    # registration; some register it as a scalar string rather than append.
    if isinstance(browsers, str):
        return [browsers]
    return [str(browser) for browser in browsers]


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "browser_name" not in metafunc.fixturenames:
        return
    # This hook may run twice for the same test when the plugin module is
    # registered under a second name (e.g. a real pytest-playwright entry
    # point resolving to the compat re-export); parametrize at most once.
    if getattr(metafunc, "_rustwright_browser_parametrized", False):
        return
    metafunc._rustwright_browser_parametrized = True  # type: ignore[attr-defined]
    metafunc.parametrize("browser_name", _selected_browsers(metafunc.config), scope="session")


def pytest_runtest_setup(item: pytest.Item) -> None:
    callspec = getattr(item, "callspec", None)
    if callspec is None:
        return
    browser_name = callspec.params.get("browser_name")
    if browser_name in _get_browser_skiplist(item, ["chromium", "firefox", "webkit"]):
        pytest.skip(f"skipped for this browser: {browser_name}")


@pytest.fixture(scope="session")
def playwright() -> Playwright:
    with sync_playwright() as playwright_instance:
        yield playwright_instance


@pytest.fixture(scope="session")
def browser_channel(pytestconfig: pytest.Config) -> str | None:
    return _getoption(pytestconfig, "browser_channel")


@pytest.fixture(scope="session")
def browser_name(pytestconfig: pytest.Config) -> str:
    return _selected_browsers(pytestconfig)[0]


@pytest.fixture(scope="session")
def device(pytestconfig: pytest.Config) -> str | None:
    return _getoption(pytestconfig, "device")


@pytest.fixture(scope="session")
def is_chromium(browser_name: str) -> bool:
    return browser_name == "chromium"


@pytest.fixture(scope="session")
def is_firefox(browser_name: str) -> bool:
    return browser_name == "firefox"


@pytest.fixture(scope="session")
def is_webkit(browser_name: str) -> bool:
    return browser_name == "webkit"


@pytest.fixture(scope="session")
def browser_type(playwright: Playwright, browser_name: str) -> BrowserType:
    return getattr(playwright, browser_name)


@pytest.fixture(scope="session")
def browser_type_launch_args(pytestconfig: pytest.Config) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if _getoption(pytestconfig, "headed", default=False):
        options["headless"] = False
    slow_mo = _getoption(pytestconfig, "slowmo", default=0)
    if slow_mo:
        options["slow_mo"] = slow_mo
    browser_channel = _getoption(pytestconfig, "browser_channel")
    if browser_channel:
        options["channel"] = browser_channel
    return options


@pytest.fixture(scope="session")
def connect_options() -> dict[str, Any] | None:
    return {}


@pytest.fixture(scope="session")
def launch_browser(
    browser_type: BrowserType,
    browser_type_launch_args: dict[str, Any],
    connect_options: dict[str, Any] | None,
) -> Callable[..., Browser]:
    def launch(**kwargs: Any) -> Browser:
        if connect_options:
            return browser_type.connect(**connect_options)
        options = dict(browser_type_launch_args)
        options.update(kwargs)
        return browser_type.launch(**options)

    return launch


@pytest.fixture(scope="session")
def browser(launch_browser: Callable[..., Browser]) -> Browser:
    browser_instance = launch_browser()
    try:
        yield browser_instance
    finally:
        browser_instance.close()


@pytest.fixture(scope="session")
def base_url(pytestconfig: pytest.Config) -> str | None:
    return _getoption(pytestconfig, "base_url")


@pytest.fixture()
def output_path(pytestconfig: pytest.Config, request: pytest.FixtureRequest) -> str:
    path = Path(str(_getoption(pytestconfig, "output", default="test-results"))).resolve() / _slugify_nodeid(request.node.nodeid)
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


@pytest.fixture(scope="session")
def browser_context_args(
    base_url: str | None,
    pytestconfig: pytest.Config,
) -> dict[str, Any]:
    # Session-scoped to match pytest-playwright's contract: test suites
    # override this fixture with scope="session" in their conftest, which is a
    # ScopeMismatch collection error if the plugin's definition is
    # function-scoped. Per-test additions (video artifact paths, the
    # browser_context_args marker) are merged in the context fixtures instead.
    options: dict[str, Any] = {}
    device_name = _getoption(pytestconfig, "device")
    if device_name:
        descriptor = DEVICE_DESCRIPTORS.get(str(device_name))
        if descriptor is None:
            raise pytest.UsageError(f"Unknown device descriptor: {device_name}")
        options.update({key: value for key, value in descriptor.items() if key != "default_browser_type"})
    if base_url:
        options["base_url"] = base_url
    if _getoption(pytestconfig, "ignore_https_errors", default=False):
        options["ignore_https_errors"] = True
    return options


def _per_test_context_args(
    browser_context_args: dict[str, Any],
    output_path: str,
    pytestconfig: pytest.Config,
    request: pytest.FixtureRequest,
) -> dict[str, Any]:
    options = dict(browser_context_args)
    if str(_getoption(pytestconfig, "video", default="off")) in {"on", "retain-on-failure"}:
        options["record_video_dir"] = str(_test_artifact_dir(output_path, request.node) / "videos")
    context_args_marker = request.node.get_closest_marker("browser_context_args")
    if context_args_marker:
        options.update(context_args_marker.kwargs)
    return options


@pytest.fixture()
def new_context(
    browser: Browser,
    browser_context_args: dict[str, Any],
    output_path: str,
    pytestconfig: pytest.Config,
    request: pytest.FixtureRequest,
) -> Any:
    artifact_dir = _test_artifact_dir(output_path, request.node)
    tracing_mode = str(_getoption(pytestconfig, "tracing", default="off"))
    screenshot_mode = str(_getoption(pytestconfig, "screenshot", default="off"))
    contexts: list[BrowserContext] = []
    finalized_contexts: set[int] = set()
    trace_paths: list[Path] = []
    screenshot_paths: list[Path] = []

    def finalize_context(
        context_instance: BrowserContext,
        original_close: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        context_id = id(context_instance)
        if context_id not in finalized_contexts:
            finalized_contexts.add(context_id)
            if tracing_mode != "off":
                trace_path = _unique_artifact_path(artifact_dir, "trace.zip")
                context_instance.tracing.stop(path=trace_path)
                trace_paths.append(trace_path)
            if screenshot_mode != "off":
                for index, page_instance in enumerate(list(context_instance.pages), start=1):
                    screenshot_name = "screenshot.png" if index == 1 else f"screenshot-{index}.png"
                    screenshot_path = _unique_artifact_path(artifact_dir, screenshot_name)
                    try:
                        page_instance.screenshot(
                            path=str(screenshot_path),
                            full_page=bool(_getoption(pytestconfig, "full_page_screenshot", default=False)),
                        )
                    except Exception:
                        continue
                    screenshot_paths.append(screenshot_path)
        if context_instance in contexts:
            contexts.remove(context_instance)
        return original_close(*args, **kwargs)

    def create_context(**kwargs: Any) -> BrowserContext:
        options = _per_test_context_args(browser_context_args, output_path, pytestconfig, request)
        options.update(kwargs)
        context_instance = browser.new_context(**options)
        original_close = context_instance.close
        if tracing_mode != "off":
            context_instance.tracing.start(screenshots=True, snapshots=True)

        def close_wrapper(*args: Any, **kwargs: Any) -> Any:
            return finalize_context(context_instance, original_close, *args, **kwargs)

        context_instance.close = close_wrapper  # type: ignore[method-assign]
        contexts.append(context_instance)
        return context_instance

    try:
        yield create_context
    finally:
        failed = _test_failed(request.node)
        for context_instance in reversed(contexts):
            try:
                context_instance.close()
            except Exception:
                pass
        if tracing_mode == "retain-on-failure" and not failed:
            for trace_path in trace_paths:
                try:
                    trace_path.unlink()
                except FileNotFoundError:
                    pass
        if screenshot_mode == "only-on-failure" and not failed:
            for screenshot_path in screenshot_paths:
                try:
                    screenshot_path.unlink()
                except FileNotFoundError:
                    pass
        if str(_getoption(pytestconfig, "video", default="off")) == "retain-on-failure" and not failed:
            shutil.rmtree(_test_artifact_dir(output_path, request.node) / "videos", ignore_errors=True)


@pytest.fixture()
def context(
    browser: Browser,
    browser_context_args: dict[str, Any],
    output_path: str,
    pytestconfig: pytest.Config,
    request: pytest.FixtureRequest,
) -> BrowserContext:
    options = _per_test_context_args(browser_context_args, output_path, pytestconfig, request)
    artifact_dir = _test_artifact_dir(output_path, request.node)
    video_mode = str(_getoption(pytestconfig, "video", default="off"))
    tracing_mode = str(_getoption(pytestconfig, "tracing", default="off"))
    context_instance = browser.new_context(**options)
    tracing_started = False
    if tracing_mode != "off":
        context_instance.tracing.start(screenshots=True, snapshots=True)
        tracing_started = True
    try:
        yield context_instance
    finally:
        failed = _test_failed(request.node)
        if tracing_started:
            trace_path = artifact_dir / "trace.zip" if tracing_mode == "on" or failed else None
            context_instance.tracing.stop(path=trace_path)
        context_instance.close()
        if video_mode == "retain-on-failure" and not failed:
            shutil.rmtree(artifact_dir / "videos", ignore_errors=True)


@pytest.fixture()
def page(
    context: BrowserContext,
    output_path: str,
    pytestconfig: pytest.Config,
    request: pytest.FixtureRequest,
) -> Page:
    page_instance = context.new_page()
    try:
        yield page_instance
    finally:
        screenshot_mode = str(_getoption(pytestconfig, "screenshot", default="off"))
        failed = _test_failed(request.node)
        if screenshot_mode == "on" or (screenshot_mode == "only-on-failure" and failed):
            artifact_dir = _test_artifact_dir(output_path, request.node)
            try:
                page_instance.screenshot(
                    path=str(artifact_dir / "screenshot.png"),
                    full_page=bool(_getoption(pytestconfig, "full_page_screenshot", default=False)),
                )
            except Exception:
                pass
        page_instance.close()


class CreateContextCallback(Protocol):
    """Signature of the ``new_context`` fixture's factory callable.

    Defined here so the pytest-playwright compat module can stay a typing-only
    re-export with no pytest hooks of its own.
    """

    def __call__(
        self,
        viewport: Optional[ViewportSize] = None,
        screen: Optional[ViewportSize] = None,
        no_viewport: Optional[bool] = None,
        ignore_https_errors: Optional[bool] = None,
        java_script_enabled: Optional[bool] = None,
        bypass_csp: Optional[bool] = None,
        user_agent: Optional[str] = None,
        locale: Optional[str] = None,
        timezone_id: Optional[str] = None,
        geolocation: Optional[Geolocation] = None,
        permissions: Optional[Sequence[str]] = None,
        extra_http_headers: Optional[Dict[str, str]] = None,
        offline: Optional[bool] = None,
        http_credentials: Optional[HttpCredentials] = None,
        device_scale_factor: Optional[float] = None,
        is_mobile: Optional[bool] = None,
        has_touch: Optional[bool] = None,
        color_scheme: Optional[Literal["dark", "light", "no-preference", "no-override", "null"]] = None,
        reduced_motion: Optional[Literal["reduce", "no-preference", "no-override", "null"]] = None,
        forced_colors: Optional[Literal["active", "none", "no-override", "null"]] = None,
        contrast: Optional[Literal["no-preference", "more", "no-override"]] = None,
        accept_downloads: Optional[bool] = None,
        default_browser_type: Optional[str] = None,
        proxy: Optional[ProxySettings] = None,
        record_har_path: Optional[Union[str, Path]] = None,
        record_har_omit_content: Optional[bool] = None,
        record_video_dir: Optional[Union[str, Path]] = None,
        record_video_size: Optional[ViewportSize] = None,
        storage_state: Optional[Union[StorageState, str, Path]] = None,
        base_url: Optional[str] = None,
        strict_selectors: Optional[bool] = None,
        service_workers: Optional[Literal["allow", "block"]] = None,
        record_har_url_filter: Optional[Union[str, Pattern[str]]] = None,
        record_har_mode: Optional[Literal["full", "minimal"]] = None,
        record_har_content: Optional[Literal["attach", "embed", "omit"]] = None,
        client_certificates: Optional[list[Any]] = None,
    ) -> BrowserContext:
        ...
