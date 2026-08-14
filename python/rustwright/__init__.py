"""Rust-backed browser automation with a Playwright-shaped Python API."""

from ._compat import disable_playwright_compat, enable_playwright_compat
from .sync_api import (
    Browser,
    BrowserContext,
    BrowserType,
    BackendMarker,
    ConsoleMessage,
    ElementHandle,
    Error,
    Download,
    FileChooser,
    Frame,
    FrameLocator,
    JSHandle,
    Locator,
    Page,
    Playwright,
    Response,
    TargetClosedError,
    TimeoutError,
    UnknownOutcomeError,
    Video,
    backend_marker,
    expect,
    sync_playwright,
)

__all__ = [
    "Browser",
    "BrowserContext",
    "BrowserType",
    "BackendMarker",
    "ConsoleMessage",
    "Download",
    "ElementHandle",
    "Error",
    "FileChooser",
    "Frame",
    "FrameLocator",
    "JSHandle",
    "Locator",
    "Page",
    "Playwright",
    "Response",
    "TargetClosedError",
    "TimeoutError",
    "UnknownOutcomeError",
    "Video",
    "backend_marker",
    "disable_playwright_compat",
    "enable_playwright_compat",
    "expect",
    "async_playwright",
    "sync_playwright",
]


# Import attribution showed the async facade subtree is about 20% of package import, so defer it for sync-only users.
def __getattr__(name: str):
    if name == "async_playwright":
        from .async_api import async_playwright

        globals()[name] = async_playwright
        return async_playwright
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
