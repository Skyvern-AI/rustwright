"""Serialized in-process browser sessions for agent-facing transports.

Pages opened by popups are adopted when the session next observes the browser
context.  A dialog that opens immediately in a brand-new popup can therefore
be auto-dismissed before a handler is installed; it is not reported in v1.
"""

from collections import OrderedDict
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from rustwright.sync_api import sync_playwright

from .errors import AgentError
from .refs import RefAllocator, RefRegistry


class BrowserSession:
    """Transport-neutral state for one serialized browser session."""

    def __init__(
        self,
        *,
        headless: bool = True,
        action_timeout_ms: int = 5000,
        navigation_timeout_ms: int = 60000,
        snapshot_depth: int = 8,
        snapshot_max_chars: int = 50000,
        mask_password_values: bool = True,
        allow_eval: bool = False,
        max_image_bytes: int = 5 * 1024 * 1024,
        context_options: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.headless = headless
        self.action_timeout_ms = action_timeout_ms
        self.navigation_timeout_ms = navigation_timeout_ms
        self.snapshot_depth = snapshot_depth
        self.snapshot_max_chars = snapshot_max_chars
        self.mask_password_values = mask_password_values
        self.allow_eval = allow_eval
        self.max_image_bytes = max_image_bytes
        self.context_options = dict(context_options or {})

        self.playwright = None  # type: Optional[Any]
        self.browser = None  # type: Optional[Any]
        self.context = None  # type: Optional[Any]
        self._tabs = OrderedDict()  # type: OrderedDict[str, Any]
        self._registries = {}  # type: Dict[str, RefRegistry]
        self._ref_allocator = RefAllocator()
        self._active_tab_id = None  # type: Optional[str]
        self._next_tab_number = 1
        self._dialog_policy = None  # type: Optional[Dict[str, Any]]

    @property
    def active_tab_id(self) -> Optional[str]:
        return self._active_tab_id

    def ensure_page(self) -> Any:
        """Ensure a page exists. Owned implementations launch lazily."""

        raise NotImplementedError

    def _tab_id_for_page(self, page: Any) -> Optional[str]:
        for tab_id, candidate in self._tabs.items():
            if candidate is page:
                return tab_id
        return None

    def _adopt_page(self, page: Any, make_active: bool = True) -> str:
        existing = self._tab_id_for_page(page)
        if existing is not None:
            if make_active:
                self._active_tab_id = existing
            return existing

        tab_id = "t%d" % self._next_tab_number
        self._next_tab_number += 1
        self._tabs[tab_id] = page
        self._registries[tab_id] = RefRegistry(self._ref_allocator)

        def on_dialog(dialog: Any, adopted_page: Any = page) -> None:
            self._handle_dialog(adopted_page, dialog)

        page.on("dialog", on_dialog)
        if make_active or self._active_tab_id is None:
            self._active_tab_id = tab_id
        return tab_id

    def _observe_pages(self) -> None:
        if self.context is None:
            return

        for tab_id, page in list(self._tabs.items()):
            try:
                closed = page.is_closed()
            except Exception:
                closed = True
            if closed:
                del self._tabs[tab_id]
                self._registries.pop(tab_id, None)
                if self._active_tab_id == tab_id:
                    self._active_tab_id = None

        try:
            pages = list(self.context.pages)
        except Exception:
            pages = []
        for page in pages:
            try:
                if page.is_closed():
                    continue
            except Exception:
                continue
            if self._tab_id_for_page(page) is None:
                self._adopt_page(page, make_active=True)

        if self._active_tab_id not in self._tabs:
            self._active_tab_id = next(reversed(self._tabs), None)

    def active_page(self) -> Any:
        self.ensure_page()
        self._observe_pages()
        if self._active_tab_id is None or self._active_tab_id not in self._tabs:
            raise AgentError("session_lost", "The browser session has no active tab")
        return self._tabs[self._active_tab_id]

    def active_registry(self) -> RefRegistry:
        self.active_page()
        if self._active_tab_id is None:
            raise AgentError("session_lost", "The browser session has no active tab")
        return self._registries[self._active_tab_id]

    def prepare_ref_reservation(self, count: int) -> int:
        """Reserve a range that the next snapshot will consume."""

        return self._ref_allocator.prepare(count)

    def list_tabs(self) -> List[Dict[str, Any]]:
        self.ensure_page()
        self._observe_pages()
        result = []
        for tab_id, page in self._tabs.items():
            try:
                title = str(page.title())
            except Exception:
                title = ""
            try:
                url = str(page.url)
            except Exception:
                url = ""
            result.append(
                {
                    "tab_id": tab_id,
                    "title": title,
                    "url": url,
                    "active": tab_id == self._active_tab_id,
                }
            )
        return result

    def new_tab(self, url: Optional[str] = None) -> str:
        self.ensure_page()
        if self.context is None:
            raise AgentError("session_lost", "The browser context is unavailable")
        page = self.context.new_page()
        tab_id = self._adopt_page(page, make_active=True)
        if url is not None:
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.navigation_timeout_ms,
            )
        return tab_id

    def select_tab(self, tab_id: str) -> str:
        self.ensure_page()
        self._observe_pages()
        if tab_id not in self._tabs:
            raise AgentError(
                "invalid_argument",
                "Unknown tab id",
                "List tabs and choose an active tab id.",
            )
        self._active_tab_id = tab_id
        return tab_id

    def close_tab(self, tab_id: str) -> str:
        self.ensure_page()
        self._observe_pages()
        if tab_id not in self._tabs:
            raise AgentError(
                "invalid_argument",
                "Unknown tab id",
                "List tabs and choose an active tab id.",
            )

        page = self._tabs[tab_id]
        page.close()
        del self._tabs[tab_id]
        self._registries.pop(tab_id, None)
        if self._dialog_policy is not None and self._dialog_policy.get("tab_id") == tab_id:
            self._dialog_policy = None
        if self._active_tab_id == tab_id:
            self._active_tab_id = next(reversed(self._tabs), None)

        if not self._tabs:
            if self.context is None:
                raise AgentError("session_lost", "The browser context is unavailable")
            replacement = self.context.new_page()
            self._adopt_page(replacement, make_active=True)
        return tab_id

    def arm_dialog(self, action: str, prompt_text: Optional[str] = None) -> None:
        self.active_page()
        if self._active_tab_id is None:
            raise AgentError("session_lost", "The browser session has no active tab")
        self._dialog_policy = {
            "tab_id": self._active_tab_id,
            "action": action,
            "prompt_text": prompt_text,
        }

    def _handle_dialog(self, page: Any, dialog: Any) -> None:
        tab_id = self._tab_id_for_page(page)
        policy = self._dialog_policy
        if policy is None or policy.get("tab_id") != tab_id:
            try:
                dialog.dismiss()
            except Exception:
                pass
            return

        try:
            if policy.get("action") == "accept":
                dialog.accept(prompt_text=policy.get("prompt_text"))
            else:
                dialog.dismiss()
        finally:
            if self._dialog_policy is policy:
                self._dialog_policy = None

    @contextmanager
    def dialog_action_scope(self) -> Iterator[None]:
        """Make an armed policy one-shot for the next serialized action."""

        policy = self._dialog_policy
        try:
            yield
        finally:
            if policy is not None and self._dialog_policy is policy:
                self._dialog_policy = None

    def close(self) -> None:
        """Reset owned resources; a later ensure_page call launches again."""

        self._dialog_policy = None
        context = self.context
        browser = self.browser
        playwright = self.playwright
        self.context = None
        self.browser = None
        self.playwright = None
        self._tabs.clear()
        self._registries.clear()
        self._active_tab_id = None

        if context is not None:
            try:
                context.close()
            except Exception:
                pass
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


class OwnedSession(BrowserSession):
    """A lazily launched, in-process browser owned by this session."""

    def ensure_page(self) -> Any:
        if self.context is None:
            try:
                self.playwright = sync_playwright().start()
                self.browser = self.playwright.chromium.launch(headless=self.headless)
                self.context = self.browser.new_context(**self.context_options)
                page = self.context.new_page()
                self._adopt_page(page, make_active=True)
            except Exception:
                self.close()
                raise AgentError("browser_launch_failed", "The browser could not be started") from None

        self._observe_pages()
        if not self._tabs:
            if self.context is None:
                raise AgentError("session_lost", "The browser context is unavailable")
            try:
                page = self.context.new_page()
                self._adopt_page(page, make_active=True)
            except Exception:
                raise AgentError("session_lost", "The browser could not create a tab") from None

        if self._active_tab_id is None:
            self._active_tab_id = next(reversed(self._tabs))
        return self._tabs[self._active_tab_id]
