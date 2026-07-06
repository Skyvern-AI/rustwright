from __future__ import annotations

from pathlib import Path
import re
import shutil
from typing import Any, Callable

import pytest

from .sync_api import Browser, BrowserContext, BrowserType, Page, Playwright, sync_playwright
from ._devices import DEVICE_DESCRIPTORS


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("rustwright", "Rustwright browser automation")
    group.addoption(
        "--browser",
        action="append",
        choices=["chromium", "firefox", "webkit"],
        dest="rustwright_browser",
        help="Browser engine to run tests against. Can be passed multiple times.",
    )
    group.addoption("--headed", action="store_true", help="Run browser headed.")
    group.addoption("--browser-channel", default=None, help="Browser channel name accepted for Playwright CLI compatibility.")
    group.addoption("--slowmo", type=int, default=0, help="Slow motion delay in milliseconds.")
    group.addoption("--device", default=None, help="Device descriptor name to use for browser contexts.")
    group.addoption("--output", default="test-results", help="Directory for browser artifacts.")
    group.addoption("--tracing", choices=["on", "off", "retain-on-failure"], default="off")
    group.addoption("--video", choices=["on", "off", "retain-on-failure"], default="off")
    group.addoption("--screenshot", choices=["on", "off", "only-on-failure"], default="off")
    group.addoption(
        "--ignore-https-errors",
        action="store_true",
        default=False,
        help="Ignore HTTPS certificate errors in browser contexts.",
    )
    group.addoption(
        "--full-page-screenshot",
        action="store_true",
        default=False,
        help="Capture full-page screenshots for pytest screenshot artifacts.",
    )
    group.addoption("--base-url", default=None, help="Base URL used by page.goto() and API requests.")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "skip_browser(*names): mark test to be skipped for browsers")
    config.addinivalue_line("markers", "only_browser(*names): mark test to run only for browsers")
    config.addinivalue_line(
        "markers",
        "browser_context_args(**kwargs): provide additional arguments to browser.new_context()",
    )


@pytest.fixture(scope="session", autouse=True)
def delete_output_dir(pytestconfig: pytest.Config) -> None:
    output_dir = Path(str(pytestconfig.getoption("output"))).resolve()
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


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "browser_name" not in metafunc.fixturenames:
        return
    browsers = metafunc.config.getoption("rustwright_browser") or ["chromium"]
    metafunc.parametrize("browser_name", browsers, scope="session")


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
    return pytestconfig.getoption("browser_channel")


@pytest.fixture(scope="session")
def browser_name(pytestconfig: pytest.Config) -> str:
    browsers = pytestconfig.getoption("rustwright_browser") or ["chromium"]
    return str(browsers[0])


@pytest.fixture(scope="session")
def device(pytestconfig: pytest.Config) -> str | None:
    return pytestconfig.getoption("device")


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
    if pytestconfig.getoption("headed"):
        options["headless"] = False
    slow_mo = pytestconfig.getoption("slowmo")
    if slow_mo:
        options["slow_mo"] = slow_mo
    browser_channel = pytestconfig.getoption("browser_channel")
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
    return pytestconfig.getoption("base_url")


@pytest.fixture()
def output_path(pytestconfig: pytest.Config, request: pytest.FixtureRequest) -> str:
    path = Path(str(pytestconfig.getoption("output"))).resolve() / _slugify_nodeid(request.node.nodeid)
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


@pytest.fixture()
def browser_context_args(
    base_url: str | None,
    output_path: str,
    pytestconfig: pytest.Config,
    request: pytest.FixtureRequest,
) -> dict[str, Any]:
    options: dict[str, Any] = {}
    device_name = pytestconfig.getoption("device")
    if device_name:
        descriptor = DEVICE_DESCRIPTORS.get(str(device_name))
        if descriptor is None:
            raise pytest.UsageError(f"Unknown device descriptor: {device_name}")
        options.update({key: value for key, value in descriptor.items() if key != "default_browser_type"})
    if base_url:
        options["base_url"] = base_url
    if pytestconfig.getoption("ignore_https_errors"):
        options["ignore_https_errors"] = True
    if str(pytestconfig.getoption("video")) in {"on", "retain-on-failure"}:
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
    tracing_mode = str(pytestconfig.getoption("tracing"))
    screenshot_mode = str(pytestconfig.getoption("screenshot"))
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
                            full_page=bool(pytestconfig.getoption("full_page_screenshot")),
                        )
                    except Exception:
                        continue
                    screenshot_paths.append(screenshot_path)
        if context_instance in contexts:
            contexts.remove(context_instance)
        return original_close(*args, **kwargs)

    def create_context(**kwargs: Any) -> BrowserContext:
        options = dict(browser_context_args)
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
        if str(pytestconfig.getoption("video")) == "retain-on-failure" and not failed:
            shutil.rmtree(_test_artifact_dir(output_path, request.node) / "videos", ignore_errors=True)


@pytest.fixture()
def context(
    browser: Browser,
    browser_context_args: dict[str, Any],
    output_path: str,
    pytestconfig: pytest.Config,
    request: pytest.FixtureRequest,
) -> BrowserContext:
    options = dict(browser_context_args)
    artifact_dir = _test_artifact_dir(output_path, request.node)
    video_mode = str(pytestconfig.getoption("video"))
    tracing_mode = str(pytestconfig.getoption("tracing"))
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
        screenshot_mode = str(pytestconfig.getoption("screenshot"))
        failed = _test_failed(request.node)
        if screenshot_mode == "on" or (screenshot_mode == "only-on-failure" and failed):
            artifact_dir = _test_artifact_dir(output_path, request.node)
            try:
                page_instance.screenshot(
                    path=str(artifact_dir / "screenshot.png"),
                    full_page=bool(pytestconfig.getoption("full_page_screenshot")),
                )
            except Exception:
                pass
        page_instance.close()
