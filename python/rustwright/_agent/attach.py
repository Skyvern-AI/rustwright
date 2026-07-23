"""Attach a short-lived CLI process to a persistent browser owner."""

import re
from collections import OrderedDict
from typing import Any, Dict, Optional

from rustwright.sync_api import sync_playwright

from .errors import AgentError
from .refs import RefRegistry, RefState
from .snapshot_js import build_fingerprint_expression
from .session import BrowserSession


_TAB_ID = re.compile(r"^t([1-9][0-9]*)$")


def page_target_id(page: Any) -> Optional[str]:
    """Return the private Chromium target id when the backend exposes it."""

    try:
        value = getattr(page, "_target_id", None)
        if value:
            return str(value)
        core = getattr(page, "_core", None)
        value = getattr(core, "target_id", None)
        return str(value) if value else None
    except Exception:
        return None


def browser_ws_endpoint(browser: Any) -> Optional[str]:
    """Contain the private endpoint accessor used by the owner process."""

    try:
        value = getattr(browser, "_ws_endpoint", None)
        return str(value) if value else None
    except Exception:
        return None


class AttachedSession(BrowserSession):
    """A CDP-attached session whose close operation only disconnects."""

    def __init__(
        self,
        endpoint: str,
        tabs: Optional[Dict[str, str]] = None,
        *,
        active_target_id: Optional[str] = None,
        next_tab_id: int = 1,
        next_ref_id: int = 1,
        session_nonce: Optional[str] = None,
        restore_refs: bool = True,
        action_timeout_ms: int = 5000,
        navigation_timeout_ms: int = 60000,
        snapshot_depth: int = 8,
        snapshot_max_chars: int = 50000,
        allow_eval: bool = False,
        headers: Optional[Dict[str, str]] = None,
        connect_timeout_ms: Optional[int] = None,
    ) -> None:
        super().__init__(
            headless=True,
            action_timeout_ms=action_timeout_ms,
            navigation_timeout_ms=navigation_timeout_ms,
            snapshot_depth=snapshot_depth,
            snapshot_max_chars=snapshot_max_chars,
            mask_password_values=True,
            allow_eval=allow_eval,
        )
        self.endpoint = endpoint
        self.headers = dict(headers) if headers is not None else None
        self.connect_timeout_ms = connect_timeout_ms
        self._persisted_tabs = dict(tabs or {})
        self._persisted_active_target_id = active_target_id
        self._next_tab_number = max(1, int(next_tab_id))
        self._session_nonce = session_nonce
        self._restore_refs = restore_refs
        self._ref_allocator.next_ref = max(1, int(next_ref_id))
        if session_nonce:
            self._ref_allocator.session_nonce = session_nonce
        self._connect()

    def _normalized_persisted_tabs(self) -> Dict[str, str]:
        """Accept the state schema and the inverse shape used by early callers."""

        normalized = {}  # type: Dict[str, str]
        for key, value in self._persisted_tabs.items():
            if isinstance(value, str) and _TAB_ID.fullmatch(value):
                normalized[str(key)] = value
            elif isinstance(key, str) and _TAB_ID.fullmatch(key) and isinstance(value, str):
                normalized[value] = key
        return normalized

    def _new_registry(self) -> RefRegistry:
        return RefRegistry(self._ref_allocator)

    def _register_page(self, tab_id: str, page: Any) -> None:
        self._tabs[tab_id] = page
        registry = self._new_registry()
        self._registries[tab_id] = registry

        match = _TAB_ID.fullmatch(tab_id)
        if match is not None:
            self._next_tab_number = max(self._next_tab_number, int(match.group(1)) + 1)

        def on_dialog(dialog: Any, adopted_page: Any = page) -> None:
            self._handle_dialog(adopted_page, dialog)

        page.on("dialog", on_dialog)
        if self._restore_refs and self._session_nonce:
            self._restore_page_refs(page, registry)

    def _adopt_page(self, page: Any, make_active: bool = True) -> str:
        existing = self._tab_id_for_page(page)
        if existing is not None:
            if make_active:
                self._active_tab_id = existing
            return existing
        tab_id = "t%d" % self._next_tab_number
        self._next_tab_number += 1
        self._register_page(tab_id, page)
        if make_active or self._active_tab_id is None:
            self._active_tab_id = tab_id
        return tab_id

    def _restore_page_refs(self, page: Any, registry: RefRegistry) -> None:
        """Rebuild clean ref fingerprints from tags left by the last snapshot."""

        nonce = self._session_nonce
        if not nonce:
            return
        expression = re.compile(r"^%s:g([1-9][0-9]*):(e[1-9][0-9]*)$" % re.escape(nonce))
        try:
            locator = page.locator('[data-rustwright-ref^="%s:g"]' % nonce)
            count = locator.count()
            tagged = []
            highest_epoch = 0
            for index in range(count):
                candidate = locator.nth(index)
                value = candidate.get_attribute("data-rustwright-ref")
                match = expression.fullmatch(value or "")
                if match is None:
                    continue
                epoch = int(match.group(1))
                highest_epoch = max(highest_epoch, epoch)
                tagged.append((candidate, value, epoch, match.group(2)))

            if highest_epoch == 0:
                return
            generation = "%s:g%d" % (nonce, highest_epoch)
            active = {}  # type: Dict[str, Dict[str, str]]
            fingerprint_expression = build_fingerprint_expression()
            for candidate, _value, epoch, ref in tagged:
                if epoch != highest_epoch:
                    continue
                fingerprint = candidate.evaluate(fingerprint_expression)
                if not isinstance(fingerprint, dict):
                    continue
                role = fingerprint.get("role")
                name = fingerprint.get("name")
                if isinstance(role, str) and isinstance(name, str):
                    active[ref] = {"role": role, "name": name}
            registry._epoch = highest_epoch
            registry.state = RefState(
                generation,
                highest_epoch,
                str(page.url),
                page_target_id(page),
                active,
            )
        except Exception:
            # Restoration is best-effort. Empty active refs fail safely and make
            # callers take a fresh snapshot.
            registry.state.active = {}

    def _connect(self) -> None:
        if self.context is not None:
            return
        try:
            self.playwright = sync_playwright().start()
            connect_options = {}  # type: Dict[str, Any]
            if self.headers is not None:
                connect_options["headers"] = self.headers
            if self.connect_timeout_ms is not None:
                connect_options["timeout"] = self.connect_timeout_ms
            self.browser = self.playwright.chromium.connect_over_cdp(
                self.endpoint,
                **connect_options,
            )
            contexts = list(self.browser.contexts)
            if not contexts:
                raise AgentError("session_lost", "The browser owner has no context")
            self.context = contexts[0]
            pages = [page for page in self.context.pages if not page.is_closed()]
            persisted = self._normalized_persisted_tabs()
            by_target = {}
            for page in pages:
                target = page_target_id(page)
                if target:
                    by_target[target] = page

            ordered = OrderedDict()
            for target, tab_id in sorted(
                persisted.items(),
                key=lambda item: int(_TAB_ID.fullmatch(item[1]).group(1)),
            ):
                page = by_target.get(target)
                if page is not None:
                    ordered[tab_id] = page

            self._tabs = OrderedDict()
            self._registries = {}
            for tab_id, page in ordered.items():
                self._register_page(tab_id, page)

            registered = set(id(page) for page in self._tabs.values())
            for page in pages:
                if id(page) in registered:
                    continue
                tab_id = "t%d" % self._next_tab_number
                self._next_tab_number += 1
                self._register_page(tab_id, page)

            active_page = by_target.get(self._persisted_active_target_id or "")
            self._active_tab_id = self._tab_id_for_page(active_page) if active_page is not None else None
            if self._active_tab_id is None:
                self._active_tab_id = next(reversed(self._tabs), None)
            if not self._tabs:
                raise AgentError("session_lost", "The browser owner has no page")
        except AgentError:
            self.close()
            raise
        except Exception:
            self.close()
            raise AgentError("session_lost", "The browser session could not be attached") from None

    def ensure_page(self) -> Any:
        if self.context is None:
            self._connect()
        self._observe_pages()
        if self._active_tab_id is None or self._active_tab_id not in self._tabs:
            self._active_tab_id = next(reversed(self._tabs), None)
        if self._active_tab_id is None:
            raise AgentError("session_lost", "The browser session has no active tab")
        return self._tabs[self._active_tab_id]

    @property
    def active_target_id(self) -> Optional[str]:
        try:
            return page_target_id(self.active_page())
        except AgentError:
            return None

    @property
    def next_tab_id(self) -> int:
        return self._next_tab_number

    @property
    def next_ref_id(self) -> int:
        return self._ref_allocator.next_ref

    def tab_metadata(self) -> Dict[str, str]:
        self._observe_pages()
        result = {}
        for tab_id, page in self._tabs.items():
            target = page_target_id(page)
            if target:
                result[target] = tab_id
        return result

    def clear_active_refs(self) -> None:
        for registry in self._registries.values():
            registry.state.active = {}

    def is_connected(self) -> bool:
        browser = self.browser
        if browser is None:
            return False
        try:
            return bool(browser.is_connected())
        except Exception:
            return False

    def close(self) -> None:
        """Disconnect the CDP client without closing owner pages or contexts."""

        self._dialog_policy = None
        browser = self.browser
        playwright = self.playwright
        self.context = None
        self.browser = None
        self.playwright = None
        self._tabs.clear()
        self._registries.clear()
        self._active_tab_id = None
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
