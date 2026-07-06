from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from rustwright.async_api import async_playwright
from rustwright.sync_api import sync_playwright

_VALID_BACKENDS = {"playwright", "patchright"}


def _resolve_backend(backend: Any = None) -> str:
    value = backend if backend is not None else os.environ.get("CLOAKBROWSER_BACKEND", "playwright")
    if not isinstance(value, str):
        raise ValueError(f"Invalid cloakbrowser backend type: {type(value).__name__}")
    normalized = value.strip().lower()
    if normalized not in _VALID_BACKENDS:
        raise ValueError(f"Invalid cloakbrowser backend: {value!r}. Must be one of {sorted(_VALID_BACKENDS)}")
    return normalized


def _normalize_proxy(proxy: Any) -> Any:
    if isinstance(proxy, str):
        return {"server": proxy}
    return proxy


def _normalize_common_options(options: dict[str, Any], *, persistent: bool) -> dict[str, Any]:
    normalized = dict(options)
    _resolve_backend(normalized.pop("backend", None))
    normalized.pop("geoip", None)
    normalized.pop("humanize", None)

    if "proxy" in normalized:
        normalized["proxy"] = _normalize_proxy(normalized["proxy"])

    if persistent and "timezone" in normalized and "timezone_id" not in normalized:
        normalized["timezone_id"] = normalized.pop("timezone")
    else:
        normalized.pop("timezone", None)

    return normalized


def ensure_binary(*, force: bool = False, **_: Any) -> str:
    from rustwright.cli import _chromium_executable_path, _download_chromium

    executable = _chromium_executable_path()
    if executable and not force:
        return executable
    result = _download_chromium(force=force, dry_run=False)
    return str(result["executable"])


def launch(**kwargs: Any) -> Any:
    manager = sync_playwright()
    playwright = manager.start()
    return playwright.chromium.launch(**_normalize_common_options(kwargs, persistent=False))


async def launch_async(**kwargs: Any) -> Any:
    manager = async_playwright()
    playwright = await manager.start()
    return await playwright.chromium.launch(**_normalize_common_options(kwargs, persistent=False))


def launch_persistent_context(user_data_dir: str | Path, **kwargs: Any) -> Any:
    manager = sync_playwright()
    playwright = manager.start()
    return playwright.chromium.launch_persistent_context(
        user_data_dir,
        **_normalize_common_options(kwargs, persistent=True),
    )


async def launch_persistent_context_async(user_data_dir: str | Path, **kwargs: Any) -> Any:
    manager = async_playwright()
    playwright = await manager.start()
    return await playwright.chromium.launch_persistent_context(
        user_data_dir,
        **_normalize_common_options(kwargs, persistent=True),
    )


__all__ = [
    "ensure_binary",
    "launch",
    "launch_async",
    "launch_persistent_context",
    "launch_persistent_context_async",
]
