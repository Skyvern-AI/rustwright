"""Explicit opt-in Playwright/Patchright/Cloakbrowser import compatibility."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Optional


_ALIASES = (
    ("playwright", "rustwright._compat.playwright"),
    ("playwright.__main__", "rustwright._compat.playwright.__main__"),
    ("playwright._impl", "rustwright._compat.playwright._impl"),
    ("playwright._impl._api_structures", "rustwright._compat.playwright._impl._api_structures"),
    ("playwright._impl._errors", "rustwright._compat.playwright._impl._errors"),
    ("playwright.async_api", "rustwright._compat.playwright.async_api"),
    ("playwright.async_api._generated", "rustwright._compat.playwright.async_api._generated"),
    ("playwright.pytest_plugin", "rustwright._compat.playwright.pytest_plugin"),
    ("playwright.sync_api", "rustwright._compat.playwright.sync_api"),
    ("playwright.sync_api._generated", "rustwright._compat.playwright.sync_api._generated"),
    ("patchright", "rustwright._compat.patchright"),
    ("patchright.__main__", "rustwright._compat.patchright.__main__"),
    ("patchright._impl", "rustwright._compat.patchright._impl"),
    ("patchright._impl._api_structures", "rustwright._compat.patchright._impl._api_structures"),
    ("patchright._impl._errors", "rustwright._compat.patchright._impl._errors"),
    ("patchright.async_api", "rustwright._compat.patchright.async_api"),
    ("patchright.async_api._generated", "rustwright._compat.patchright.async_api._generated"),
    ("patchright.pytest_plugin", "rustwright._compat.patchright.pytest_plugin"),
    ("patchright.sync_api", "rustwright._compat.patchright.sync_api"),
    ("patchright.sync_api._generated", "rustwright._compat.patchright.sync_api._generated"),
    ("cloakbrowser", "rustwright._compat.cloakbrowser"),
    # The pytest_playwright aliases re-export the full rustwright plugin. A
    # real pytest-playwright distribution's entry point resolving here loads
    # the plugin a second time, which is safe by construction: option
    # registration skips already-taken flags and browser_name parametrization
    # is guarded to run at most once per test.
    ("pytest_playwright", "rustwright._compat.pytest_playwright"),
    ("pytest_playwright.pytest_playwright", "rustwright._compat.pytest_playwright.pytest_playwright"),
)

_PREVIOUS_MODULES: dict[str, Optional[ModuleType]] = {}
_ENABLED = False


def _set_parent_attribute(module_name: str, module: ModuleType) -> None:
    parent_name, _, child_name = module_name.rpartition(".")
    if not parent_name:
        return
    parent = sys.modules.get(parent_name)
    if parent is not None:
        setattr(parent, child_name, module)


def enable_playwright_compat() -> None:
    """Enable legacy Playwright-compatible import names for this Python process.

    After this is called, subsequent imports such as ``playwright.sync_api`` or
    ``patchright.async_api`` resolve to Rustwright's compatibility shims.
    """

    global _ENABLED
    if _ENABLED:
        return

    loaded_modules = [(alias_name, importlib.import_module(target_name)) for alias_name, target_name in _ALIASES]
    for alias_name, module in loaded_modules:
        _PREVIOUS_MODULES[alias_name] = sys.modules.get(alias_name)
        sys.modules[alias_name] = module
        _set_parent_attribute(alias_name, module)

    _ENABLED = True


def disable_playwright_compat() -> None:
    """Undo aliases installed by :func:`enable_playwright_compat`."""

    global _ENABLED
    if not _ENABLED:
        return

    for alias_name, _target_name in _ALIASES:
        previous = _PREVIOUS_MODULES.get(alias_name)
        if previous is None:
            sys.modules.pop(alias_name, None)
        else:
            sys.modules[alias_name] = previous
            _set_parent_attribute(alias_name, previous)

    _PREVIOUS_MODULES.clear()
    _ENABLED = False


__all__ = ["disable_playwright_compat", "enable_playwright_compat"]
