"""Explicit opt-in Playwright/Patchright/Cloakbrowser import compatibility.

Aliases are installed eagerly. If pytest is not importable when
:func:`enable_playwright_compat` runs, pytest-plugin aliases are skipped. Call
the function again after pytest becomes importable to add them. Pytest users
normally enable compatibility inside a process where pytest is importable.

Target imports happen before alias publication. If enable fails, canonical
``rustwright._compat.*`` modules and pytest imported during that phase stay in
``sys.modules``. Complete rollback covers only legacy alias entries and their
parent attributes; removing canonical imports could disturb unrelated users.

Do not enable or disable compatibility concurrently with in-flight imports of
aliased names. Direct ``sys.modules`` aliasing cannot make those imports atomic.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from threading import RLock
from types import ModuleType
from typing import NamedTuple

_CORE_ALIASES = (
    ("playwright", "rustwright._compat.playwright"),
    ("playwright.__main__", "rustwright._compat.playwright.__main__"),
    ("playwright._impl", "rustwright._compat.playwright._impl"),
    ("playwright._impl._api_structures", "rustwright._compat.playwright._impl._api_structures"),
    ("playwright._impl._errors", "rustwright._compat.playwright._impl._errors"),
    ("playwright.async_api", "rustwright._compat.playwright.async_api"),
    ("playwright.async_api._generated", "rustwright._compat.playwright.async_api._generated"),
    ("playwright.sync_api", "rustwright._compat.playwright.sync_api"),
    ("playwright.sync_api._generated", "rustwright._compat.playwright.sync_api._generated"),
    ("patchright", "rustwright._compat.patchright"),
    ("patchright.__main__", "rustwright._compat.patchright.__main__"),
    ("patchright._impl", "rustwright._compat.patchright._impl"),
    ("patchright._impl._api_structures", "rustwright._compat.patchright._impl._api_structures"),
    ("patchright._impl._errors", "rustwright._compat.patchright._impl._errors"),
    ("patchright.async_api", "rustwright._compat.patchright.async_api"),
    ("patchright.async_api._generated", "rustwright._compat.patchright.async_api._generated"),
    ("patchright.sync_api", "rustwright._compat.patchright.sync_api"),
    ("patchright.sync_api._generated", "rustwright._compat.patchright.sync_api._generated"),
    ("cloakbrowser", "rustwright._compat.cloakbrowser"),
)

_PYTEST_ALIASES = (
    ("pytest_playwright", "rustwright._compat.pytest_playwright"),
    ("playwright.pytest_plugin", "rustwright._compat.playwright.pytest_plugin"),
    ("patchright.pytest_plugin", "rustwright._compat.patchright.pytest_plugin"),
    ("pytest_playwright.pytest_playwright", "rustwright._compat.pytest_playwright.pytest_playwright"),
)

_MISSING = object()
_PREVIOUS_MODULES: dict[str, object] = {}
_PREVIOUS_PARENT_ATTRIBUTES: dict[str, tuple[ModuleType, object]] = {}
_STATE_LOCK = RLock()
_ENABLED = False
_PYTEST_ALIASES_ENABLED = False


class PlaywrightCompatEnableResult(NamedTuple):
    """Aliases registered or skipped by the active compatibility state."""

    enabled: bool
    registered_aliases: tuple[str, ...]
    skipped_aliases: tuple[str, ...]


_LAST_ENABLE_RESULT = PlaywrightCompatEnableResult(False, (), ())


def _compat_transaction_hook(event: str, alias_name: str | None = None) -> None:
    """Stable no-op event seam for compatibility transaction tests."""


def _set_parent_attribute(
    module_name: str,
    module: ModuleType,
    previous_parent_attributes: dict[str, tuple[ModuleType, object]],
) -> None:
    parent_name, _, child_name = module_name.rpartition(".")
    parent = sys.modules.get(parent_name)
    if not isinstance(parent, ModuleType):
        return
    if module_name not in previous_parent_attributes:
        previous_parent_attributes[module_name] = (
            parent,
            vars(parent).get(child_name, _MISSING),
        )
    ModuleType.__setattr__(parent, child_name, module)


def _restore_aliases(
    previous_modules: dict[str, object],
    previous_parent_attributes: dict[str, tuple[ModuleType, object]],
) -> None:
    for alias_name in sorted(previous_modules, key=lambda name: name.count("."), reverse=True):
        previous_module = previous_modules[alias_name]
        if previous_module is _MISSING:
            sys.modules.pop(alias_name, None)
        else:
            sys.modules[alias_name] = previous_module

        parent_snapshot = previous_parent_attributes.get(alias_name)
        if parent_snapshot is None:
            continue
        parent, previous_attribute = parent_snapshot
        child_name = alias_name.rpartition(".")[2]
        if previous_attribute is _MISSING:
            if child_name in vars(parent):
                ModuleType.__delattr__(parent, child_name)
        else:
            ModuleType.__setattr__(parent, child_name, previous_attribute)


def enable_playwright_compat() -> PlaywrightCompatEnableResult:
    """Enable legacy aliases and report any aliases skipped without pytest.

    Every target import completes before the compatibility lock is acquired.
    Calling again after pytest becomes importable upgrades an enabled core-only
    state with the pytest aliases.

    Target imports are outside the rollback boundary. Successfully imported
    canonical compatibility modules, including pytest dependencies, remain in
    ``sys.modules`` if enable later fails. Legacy alias entries and their parent
    attributes are the complete transactional publication boundary.
    """

    global _ENABLED, _LAST_ENABLE_RESULT, _PYTEST_ALIASES_ENABLED

    pytest_available = importlib.util.find_spec("pytest") is not None
    aliases_to_import = _CORE_ALIASES + (_PYTEST_ALIASES if pytest_available else ())
    _compat_transaction_hook("enable-before-import")
    loaded_modules = tuple(
        (alias_name, importlib.import_module(target_name))
        for alias_name, target_name in aliases_to_import
    )
    _compat_transaction_hook("enable-after-import")

    with _STATE_LOCK:
        _compat_transaction_hook("enable-lock-acquired")
        if _ENABLED:
            if not pytest_available or _PYTEST_ALIASES_ENABLED:
                return _LAST_ENABLE_RESULT
            modules_to_publish = loaded_modules[len(_CORE_ALIASES) :]
        else:
            modules_to_publish = loaded_modules

        previous_modules = {
            alias_name: sys.modules.get(alias_name, _MISSING)
            for alias_name, _module in modules_to_publish
        }
        previous_parent_attributes: dict[str, tuple[ModuleType, object]] = {}
        try:
            for alias_name, module in modules_to_publish:
                sys.modules[alias_name] = module
                _set_parent_attribute(alias_name, module, previous_parent_attributes)
                _compat_transaction_hook("enable-after-alias-publish", alias_name)
        except BaseException:
            _restore_aliases(previous_modules, previous_parent_attributes)
            raise

        _PREVIOUS_MODULES.update(previous_modules)
        _PREVIOUS_PARENT_ATTRIBUTES.update(previous_parent_attributes)
        _ENABLED = True
        _PYTEST_ALIASES_ENABLED = _PYTEST_ALIASES_ENABLED or pytest_available
        skipped_aliases = (
            ()
            if _PYTEST_ALIASES_ENABLED
            else tuple(alias_name for alias_name, _target_name in _PYTEST_ALIASES)
        )
        _LAST_ENABLE_RESULT = PlaywrightCompatEnableResult(
            True,
            tuple(_PREVIOUS_MODULES),
            skipped_aliases,
        )
        return _LAST_ENABLE_RESULT


def disable_playwright_compat() -> None:
    """Restore modules and parent attributes replaced by compatibility."""

    global _ENABLED, _LAST_ENABLE_RESULT, _PYTEST_ALIASES_ENABLED

    with _STATE_LOCK:
        if not _ENABLED:
            return
        _restore_aliases(_PREVIOUS_MODULES, _PREVIOUS_PARENT_ATTRIBUTES)
        _PREVIOUS_MODULES.clear()
        _PREVIOUS_PARENT_ATTRIBUTES.clear()
        _ENABLED = False
        _PYTEST_ALIASES_ENABLED = False
        _LAST_ENABLE_RESULT = PlaywrightCompatEnableResult(False, (), ())


__all__ = [
    "PlaywrightCompatEnableResult",
    "disable_playwright_compat",
    "enable_playwright_compat",
]
