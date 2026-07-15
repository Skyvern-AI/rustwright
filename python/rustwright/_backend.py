from __future__ import annotations

from importlib import metadata
from pathlib import Path
from typing import Optional, TypedDict

from . import _rustwright


class BackendMarker(TypedDict):
    implementation: str
    package: str
    version: str
    api_package: str
    api_module: str
    replacement_backend: bool
    runtime: str
    runtime_module: str
    runtime_module_file: Optional[str]
    transport: str
    transport_protocol: str
    cdp_first: bool
    python_playwright_driver: bool
    playwright_driver: str


def _version() -> str:
    try:
        return metadata.version("rustwright")
    except metadata.PackageNotFoundError:
        return "0.1.1+local"


def backend_marker(api_module: str | None = None) -> BackendMarker:
    """Return JSON-safe evidence that this API is backed by Rustwright CDP."""

    module_name = api_module or "rustwright"
    api_package = module_name.split(".", 1)[0]
    runtime_file = getattr(_rustwright, "__file__", None)
    return {
        "implementation": "rustwright",
        "package": "rustwright",
        "version": _version(),
        "api_package": api_package,
        "api_module": module_name,
        "replacement_backend": True,
        "runtime": "rust-pyo3-extension",
        "runtime_module": _rustwright.__name__,
        "runtime_module_file": str(Path(runtime_file).resolve()) if runtime_file else None,
        "transport": "raw-cdp",
        "transport_protocol": "Chrome DevTools Protocol",
        "cdp_first": True,
        "python_playwright_driver": False,
        "playwright_driver": "none",
    }


__all__ = ["BackendMarker", "backend_marker"]
