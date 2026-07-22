"""Typed browser session and per-page event state."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
import threading
from typing import Any, Callable, Mapping


MetadataItems = tuple[tuple[str, Any], ...]


def _freeze_metadata(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(
            sorted((str(key), _freeze_metadata(item)) for key, item in value.items())
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_metadata(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_freeze_metadata(item) for item in value), key=repr))
    if isinstance(value, bytearray):
        return bytes(value)
    return value


def _immutable_mapping(value: Any) -> MetadataItems:
    """Copy the scalar metadata exposed by browser event objects."""
    if not isinstance(value, Mapping):
        return ()
    return tuple(
        sorted((str(key), _freeze_metadata(item)) for key, item in value.items())
    )


@dataclass(frozen=True)
class ConsoleRecord:
    sequence: int
    epoch: int
    message_type: str
    text: str
    location: MetadataItems


@dataclass(frozen=True)
class NetworkRecord:
    epoch: int
    index: int
    method: str
    url: str
    resource_type: str
    headers: MetadataItems
    post_data: str | None
    request: Any = field(compare=False, repr=False)
    response_status: int | None = None
    response_headers: MetadataItems = ()
    response: Any = field(default=None, compare=False, repr=False)
    failure: str | None = None


@dataclass(frozen=True)
class DownloadRecord:
    sequence: int
    epoch: int
    url: str
    suggested_filename: str
    download: Any = field(compare=False, repr=False)
    artifact: str | None = None
    error: str | None = None
    finished: bool = False
    reported: bool = False


@dataclass
class PageRegistry:
    page: Any = field(repr=False)
    console_records: deque[ConsoleRecord] = field(default_factory=deque)
    network_records: deque[NetworkRecord] = field(default_factory=deque)
    downloads: deque[DownloadRecord] = field(default_factory=deque)
    snapshot_refs: frozenset[str] = frozenset()
    snapshot_generation: int = 0
    snapshot_taken: bool = False
    navigation_epoch: int = 0
    navigation_start_request_index: int = 1
    navigation_pending: bool = False
    last_known_title: str | None = None
    console_evictions_total: int = 0
    console_evictions_current_epoch: int = 0
    network_evictions: dict[int, int] = field(default_factory=dict)
    pending_file_chooser: Any = field(default=None, repr=False)
    file_chooser_retry_element: Any = field(default=None, repr=False)
    pending_dialog: Any = field(default=None, repr=False)
    request_keys: dict[int, tuple[int, int]] = field(default_factory=dict, repr=False)
    callbacks: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class SessionState:
    """All state owned by one stdio MCP client session.

    Tool calls are serialized by the server's tool lock. Browser callbacks use
    ``event_lock`` exclusively so an event can be delivered while a tool action
    is waiting for the browser.
    """

    console_quota: int = 200
    network_quota: int = 500
    download_quota: int = 100
    pw: Any = None
    browser: Any = None
    context: Any = None
    page: Any = None
    remote: bool = False
    next_ref: int = 1
    page_registries: dict[int, PageRegistry] = field(default_factory=dict)
    next_request_index: int = 1
    next_console_sequence: int = 1
    next_download_sequence: int = 1
    response_console_cursor: int = 0
    download_saver: Callable[[Any, str], str] | None = field(
        default=None, repr=False
    )
    context_page_callback: Any = field(default=None, repr=False)
    event_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def attach(
        self,
        *,
        pw: Any,
        browser: Any,
        context: Any,
        page: Any,
        remote: bool,
    ) -> None:
        self._remove_context_page_handler()
        self.pw = pw
        self.browser = browser
        self.context = context
        self.page = page
        self.remote = remote
        self.next_ref = 1

        callback = lambda created_page: self.register_page_handlers(created_page)
        self.context_page_callback = callback
        try:
            context.on("page", callback)
            existing_pages = list(context.pages)
        except Exception:
            try:
                context.remove_listener("page", callback)
            except Exception:
                pass
            self.context_page_callback = None
            raise
        for existing_page in existing_pages:
            self.register_page_handlers(existing_page)
        if not any(existing_page is page for existing_page in existing_pages):
            self.register_page_handlers(page)

    def _remove_context_page_handler(self) -> None:
        context = self.context
        callback = self.context_page_callback
        self.context_page_callback = None
        if context is None or callback is None:
            return
        try:
            context.remove_listener("page", callback)
        except Exception:
            pass

    def registry_for(self, page: Any, *, create: bool = False) -> PageRegistry | None:
        key = id(page)
        registry = self.page_registries.get(key)
        if registry is not None and registry.page is page:
            return registry
        if not create:
            return None
        registry = PageRegistry(
            page=page,
            console_records=deque(maxlen=self.console_quota),
            network_records=deque(maxlen=self.network_quota),
            downloads=deque(maxlen=self.download_quota),
            navigation_start_request_index=self.next_request_index,
        )
        self.page_registries[key] = registry
        return registry

    def register_page_handlers(self, page: Any) -> PageRegistry:
        """Register every browser event handler once for ``page``."""
        with self.event_lock:
            existing = self.registry_for(page)
            if existing is not None and existing.callbacks:
                return existing
            registry = self.registry_for(page, create=True)
            assert registry is not None
            callbacks = {
                "console": lambda message: self._on_console(page, message),
                "request": lambda request: self._on_request(page, request),
                "response": lambda response: self._on_response(page, response),
                "requestfailed": lambda request: self._on_request_failed(page, request),
                "filechooser": lambda chooser: self._on_file_chooser(page, chooser),
                "dialog": lambda dialog: self._on_dialog(page, dialog),
                "download": lambda download: self._on_download(page, download),
                "framenavigated": lambda frame: self._on_frame_navigated(page, frame),
                "close": lambda *_: self.drop_page(page),
            }
            registry.callbacks = callbacks

        attached: list[tuple[str, Any]] = []
        try:
            for event, callback in callbacks.items():
                page.on(event, callback)
                attached.append((event, callback))
        except Exception:
            for event, callback in reversed(attached):
                try:
                    page.remove_listener(event, callback)
                except Exception:
                    pass
            self.drop_page(page)
            raise
        return registry

    def drop_page(self, page: Any) -> None:
        with self.event_lock:
            registry = self.page_registries.get(id(page))
            if registry is not None and registry.page is page:
                self.page_registries.pop(id(page), None)
            if self.page is page:
                self.page = None

    def clear(self) -> None:
        self._remove_context_page_handler()
        with self.event_lock:
            self.page_registries.clear()
            self.pw = None
            self.browser = None
            self.context = None
            self.page = None
            self.remote = False
            self.next_ref = 1
            self.next_request_index = 1
            self.next_console_sequence = 1
            self.next_download_sequence = 1
            self.response_console_cursor = 0

    def snapshot_start_ref(self) -> int:
        with self.event_lock:
            return self.next_ref

    def record_snapshot(self, page: Any, refs: list[str], next_ref: int) -> None:
        with self.event_lock:
            registry = self.registry_for(page, create=True)
            assert registry is not None
            registry.snapshot_refs = frozenset(refs)
            registry.snapshot_generation += 1
            registry.snapshot_taken = True
            self.next_ref = next_ref

    def snapshot_status(self, page: Any) -> tuple[bool, frozenset[str]]:
        with self.event_lock:
            registry = self.registry_for(page)
            if registry is None:
                return False, frozenset()
            return registry.snapshot_taken, registry.snapshot_refs

    def pending_modals(self, page: Any) -> tuple[Any | None, Any | None]:
        """Return the current dialog and file chooser without browser calls."""
        with self.event_lock:
            registry = self.registry_for(page)
            if registry is None:
                return None, None
            return registry.pending_dialog, registry.pending_file_chooser

    def pending_modal_pages(self) -> list[tuple[Any, Any | None, Any | None]]:
        """Return every registered page with live modal state."""
        with self.event_lock:
            return [
                (registry.page, registry.pending_dialog, registry.pending_file_chooser)
                for registry in self.page_registries.values()
                if registry.pending_dialog is not None
                or registry.pending_file_chooser is not None
            ]

    def remember_page_title(self, page: Any, title: str) -> None:
        with self.event_lock:
            registry = self.registry_for(page, create=True)
            assert registry is not None
            registry.last_known_title = title

    def known_page_title(self, page: Any) -> str | None:
        with self.event_lock:
            registry = self.registry_for(page)
            return None if registry is None else registry.last_known_title

    def clear_pending_dialog(self, page: Any, dialog: Any) -> None:
        with self.event_lock:
            registry = self.registry_for(page)
            if registry is not None and registry.pending_dialog is dialog:
                registry.pending_dialog = None

    def clear_pending_file_chooser(self, page: Any, chooser: Any) -> None:
        with self.event_lock:
            registry = self.registry_for(page)
            if registry is not None and registry.pending_file_chooser is chooser:
                registry.pending_file_chooser = None

    def response_events(
        self,
    ) -> tuple[list[ConsoleRecord], int, list[DownloadRecord]]:
        """Consume response-scoped console and completed download events."""
        with self.event_lock:
            console = sorted(
                (
                    record
                    for registry in self.page_registries.values()
                    for record in registry.console_records
                    if record.sequence > self.response_console_cursor
                ),
                key=lambda record: record.sequence,
            )
            latest_console = self.next_console_sequence - 1
            self.response_console_cursor = latest_console

            downloads: list[DownloadRecord] = []
            for registry in self.page_registries.values():
                for position, record in enumerate(registry.downloads):
                    if not record.finished or record.reported:
                        continue
                    downloads.append(record)
                    registry.downloads[position] = replace(record, reported=True)
            downloads.sort(key=lambda record: record.sequence)
            return console, latest_console, downloads

    def _invalidate_snapshot(self, registry: PageRegistry) -> None:
        registry.snapshot_refs = frozenset()
        registry.snapshot_taken = False

    def _begin_navigation(self, registry: PageRegistry) -> None:
        registry.navigation_epoch += 1
        registry.navigation_start_request_index = self.next_request_index
        registry.navigation_pending = True
        registry.console_evictions_current_epoch = 0
        registry.network_evictions.clear()
        registry.file_chooser_retry_element = None
        self._invalidate_snapshot(registry)

    @staticmethod
    def _is_main_navigation(page: Any, request: Any) -> bool:
        try:
            return bool(request.is_navigation_request()) and request.frame is page.main_frame
        except Exception:
            return False

    def _on_console(self, page: Any, message: Any) -> None:
        with self.event_lock:
            registry = self.registry_for(page)
            if registry is None:
                return
            if (
                registry.console_records.maxlen is not None
                and len(registry.console_records) == registry.console_records.maxlen
            ):
                evicted_epoch = registry.console_records[0].epoch
                registry.console_evictions_total += 1
                if evicted_epoch == registry.navigation_epoch:
                    registry.console_evictions_current_epoch += 1
            registry.console_records.append(
                ConsoleRecord(
                    sequence=self.next_console_sequence,
                    epoch=registry.navigation_epoch,
                    message_type=str(message.type),
                    text=str(message.text),
                    location=_immutable_mapping(message.location),
                )
            )
            self.next_console_sequence += 1

    def _on_request(self, page: Any, request: Any) -> None:
        with self.event_lock:
            registry = self.registry_for(page)
            if registry is None:
                return
            if self._is_main_navigation(page, request) and not registry.navigation_pending:
                self._begin_navigation(registry)
            index = self.next_request_index
            self.next_request_index += 1
            record = NetworkRecord(
                epoch=registry.navigation_epoch,
                index=index,
                method=str(request.method),
                url=str(request.url),
                resource_type=str(request.resource_type),
                headers=_immutable_mapping(request.headers),
                post_data=request.post_data,
                request=request,
            )
            if (
                registry.network_records.maxlen is not None
                and len(registry.network_records) == registry.network_records.maxlen
            ):
                evicted_epoch = registry.network_records[0].epoch
                if evicted_epoch == registry.navigation_epoch:
                    registry.network_evictions[evicted_epoch] = (
                        registry.network_evictions.get(evicted_epoch, 0) + 1
                    )
            registry.network_records.append(record)
            registry.request_keys[id(request)] = (record.epoch, record.index)
            live_keys = {(item.epoch, item.index) for item in registry.network_records}
            registry.request_keys = {
                request_id: key
                for request_id, key in registry.request_keys.items()
                if key in live_keys
            }

    @staticmethod
    def _request_record_position(
        registry: PageRegistry, request: Any
    ) -> int | None:
        key = registry.request_keys.get(id(request))
        if key is None:
            return None
        for position, record in enumerate(registry.network_records):
            if (
                (record.epoch, record.index) == key
                and record.request is request
            ):
                return position
        registry.request_keys.pop(id(request), None)
        return None

    def _replace_request_record(
        self, registry: PageRegistry, request: Any, **changes: Any
    ) -> None:
        position = self._request_record_position(registry, request)
        if position is not None:
            registry.network_records[position] = replace(
                registry.network_records[position], **changes
            )

    def _on_response(self, page: Any, response: Any) -> None:
        with self.event_lock:
            registry = self.registry_for(page)
            if registry is None:
                return
            request = response.request
            if request is None:
                return
            if self._request_record_position(registry, request) is None:
                return
            self._replace_request_record(
                registry,
                request,
                response_status=(
                    None if response.status is None else int(response.status)
                ),
                response_headers=_immutable_mapping(response.headers),
                response=response,
            )

    def _on_request_failed(self, page: Any, request: Any) -> None:
        with self.event_lock:
            registry = self.registry_for(page)
            if registry is None:
                return
            if self._request_record_position(registry, request) is None:
                return
            failure = request.failure
            self._replace_request_record(
                registry,
                request,
                failure=None if failure is None else str(failure),
            )
            if self._is_main_navigation(page, request):
                registry.navigation_pending = False

    def _on_file_chooser(self, page: Any, chooser: Any) -> None:
        with self.event_lock:
            registry = self.registry_for(page)
            if registry is not None:
                registry.pending_file_chooser = chooser

    def _on_dialog(self, page: Any, dialog: Any) -> None:
        with self.event_lock:
            registry = self.registry_for(page)
            if registry is None:
                return
            registry.pending_dialog = dialog

    def _on_download(self, page: Any, download: Any) -> None:
        record: DownloadRecord | None = None
        with self.event_lock:
            registry = self.registry_for(page)
            if registry is not None:
                record = DownloadRecord(
                    sequence=self.next_download_sequence,
                    epoch=registry.navigation_epoch,
                    url=str(download.url),
                    suggested_filename=str(download.suggested_filename),
                    download=download,
                )
                self.next_download_sequence += 1
                registry.downloads.append(record)

        # Browser/file I/O must never run under event_lock. The callback may be
        # delivered while a serialized tool is in flight, so record first and
        # publish the finished artifact in a second short critical section.
        if record is None or self.download_saver is None:
            return
        artifact: str | None = None
        error: str | None = None
        try:
            artifact = self.download_saver(download, record.suggested_filename)
        except Exception as exc:
            error = str(exc)
        with self.event_lock:
            registry = self.registry_for(page)
            if registry is None:
                return
            for position, candidate in enumerate(registry.downloads):
                if candidate.sequence == record.sequence:
                    registry.downloads[position] = replace(
                        candidate,
                        artifact=artifact,
                        error=error,
                        finished=True,
                    )
                    break

    def _on_frame_navigated(self, page: Any, frame: Any) -> None:
        with self.event_lock:
            registry = self.registry_for(page)
            if registry is None:
                return
            try:
                is_main_frame = frame is page.main_frame
            except Exception:
                is_main_frame = False
            if not is_main_frame:
                return
            if registry.navigation_pending:
                registry.navigation_pending = False
            else:
                self._begin_navigation(registry)
                registry.navigation_pending = False
            self._invalidate_snapshot(registry)
