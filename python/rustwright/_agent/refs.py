"""Snapshot generations, element references, and best-effort resolution."""

import re
import secrets
from collections import deque
from typing import Any, Deque, Dict, Optional, Tuple

from rustwright.sync_api import Locator

from .errors import AgentError
from .snapshot_js import build_fingerprint_expression, build_tagging_expression


_REF_PATTERN = re.compile(r"^@?e[1-9][0-9]*$")


class RefState:
    """The active references produced by one successful snapshot."""

    def __init__(
        self,
        generation: str,
        epoch: int,
        url: str,
        target_id: Optional[str],
        active: Dict[str, Dict[str, str]],
    ) -> None:
        self.generation = generation
        self.epoch = epoch
        self.url = url
        self.target_id = target_id
        self.active = active


class RefAllocator:
    """Allocate globally unique ref ranges for every tab in one session."""

    def __init__(
        self,
        next_ref: int = 1,
        session_nonce: Optional[str] = None,
    ) -> None:
        self.next_ref = _validated_integer("next_ref", next_ref, 1)
        self.session_nonce = session_nonce or secrets.token_hex(16)
        self._prepared = deque()  # type: Deque[Tuple[int, int]]

    def prepare(self, count: int) -> int:
        """Reserve a range now for a later snapshot to consume."""

        count = _validated_integer("count", count, 1)
        start = self.next_ref
        self.next_ref += count
        self._prepared.append((start, count))
        return start

    def take(self, count: int) -> int:
        """Return a prepared range, or reserve a fresh range in-process."""

        count = _validated_integer("count", count, 1)
        while self._prepared:
            start, prepared_count = self._prepared.popleft()
            if prepared_count == count:
                return start
            # A mismatched prepared range is intentionally burned. Reusing any
            # part of it would make durable high-water marks ambiguous.
        start = self.next_ref
        self.next_ref += count
        return start


def _page_url(page: Any) -> str:
    try:
        return str(page.url)
    except Exception:
        return ""


def _page_target_id(page: Any) -> Optional[str]:
    """Read the private target identifier when this backend exposes one."""

    try:
        value = getattr(page, "_target_id", None)
        if value:
            return str(value)
        core = getattr(page, "_core", None)
        value = getattr(core, "target_id", None)
        return str(value) if value else None
    except Exception:
        return None


def _validated_integer(name: str, value: Any, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AgentError(
            "invalid_argument",
            "%s must be an integer greater than or equal to %d" % (name, minimum),
        )
    return value


class RefRegistry:
    """Own one tab's active refs while sharing its session's allocator."""

    def __init__(self, allocator: Optional[RefAllocator] = None) -> None:
        self.allocator = allocator or RefAllocator()
        self.attr = "data-rustwright-ref"
        self._epoch = 0
        self.state = RefState("", 0, "", None, {})

    @property
    def session_nonce(self) -> str:
        return self.allocator.session_nonce

    @session_nonce.setter
    def session_nonce(self, value: str) -> None:
        self.allocator.session_nonce = value

    @property
    def next_ref(self) -> int:
        return self.allocator.next_ref

    @next_ref.setter
    def next_ref(self, value: int) -> None:
        self.allocator.next_ref = _validated_integer("next_ref", value, 1)

    def snapshot(
        self,
        page: Any,
        *,
        depth: int = 8,
        max_chars: int = 50000,
        max_refs: int = 1000,
        mask_password_values: bool = True,
    ) -> Dict[str, Any]:
        depth = _validated_integer("depth", depth, 0)
        max_chars = _validated_integer("max_chars", max_chars, 1)
        max_refs = _validated_integer("max_refs", max_refs, 1)
        if not isinstance(mask_password_values, bool):
            raise AgentError("invalid_argument", "mask_password_values must be a boolean")

        self._epoch += 1
        epoch = self._epoch
        generation = "%s:g%d" % (self.session_nonce, epoch)
        first_ref = self.allocator.take(max_refs)
        start_at = first_ref - 1

        # Reserve the whole bounded range before evaluation. If evaluation fails
        # after tagging nodes, a later snapshot still cannot reuse those ids.
        self.state = RefState(
            generation,
            epoch,
            _page_url(page),
            _page_target_id(page),
            {},
        )

        try:
            expression = build_tagging_expression(
                attr=self.attr,
                generation=generation,
                start_at=start_at,
                max_refs=max_refs,
                max_chars=max_chars,
                mask_password_values=mask_password_values,
                max_depth=depth,
            )
        except ValueError as exc:
            raise AgentError("invalid_argument", str(exc)) from None

        result = page.evaluate(expression)
        if not isinstance(result, dict):
            raise AgentError("snapshot_failed", "Snapshot renderer returned an invalid result")
        text = result.get("text")
        refs = result.get("refs")
        truncated = result.get("truncated")
        if not isinstance(text, str) or not isinstance(refs, list) or not isinstance(truncated, bool):
            raise AgentError("snapshot_failed", "Snapshot renderer returned an invalid result")
        if len(text) > max_chars or len(refs) > max_refs:
            raise AgentError("snapshot_failed", "Snapshot renderer exceeded its configured limit")

        active = {}  # type: Dict[str, Dict[str, str]]
        clean_refs = []
        for item in refs:
            if not isinstance(item, dict):
                raise AgentError("snapshot_failed", "Snapshot renderer returned an invalid ref")
            ref = item.get("ref")
            role = item.get("role")
            name = item.get("name")
            if (
                not isinstance(ref, str)
                or _REF_PATTERN.fullmatch(ref) is None
                or not isinstance(role, str)
                or not isinstance(name, str)
            ):
                raise AgentError("snapshot_failed", "Snapshot renderer returned an invalid ref")
            if ref in active:
                raise AgentError("ref_integrity_error", "Snapshot renderer returned a duplicate ref")
            number = int(ref[1:])
            if number < first_ref or number >= first_ref + max_refs:
                raise AgentError("ref_integrity_error", "Snapshot renderer returned an unreserved ref")
            fingerprint = {"role": role, "name": name}
            active[ref] = fingerprint
            clean_refs.append({"ref": ref, "role": role, "name": name})

        url = _page_url(page)
        target_id = _page_target_id(page)
        self.state = RefState(generation, epoch, url, target_id, active)
        return {
            "text": text,
            "refs": clean_refs,
            "truncated": truncated,
            "epoch": epoch,
            "url": url,
            "generation": generation,
        }


def _stale_ref() -> AgentError:
    return AgentError(
        "stale_ref",
        "The ref is no longer active for this page",
        "Take a new snapshot and use a ref from that snapshot.",
    )


def resolve(page: Any, registry: RefRegistry, ref: str) -> Locator:
    """Resolve a ref with best-effort identity checks, not a security guarantee."""

    if not isinstance(ref, str) or _REF_PATTERN.fullmatch(ref) is None:
        raise AgentError(
            "invalid_ref",
            "Ref must have the form e1 or @e1",
            "Use a ref returned by the latest snapshot.",
        )
    normalized = ref[1:] if ref.startswith("@") else ref
    fingerprint = registry.state.active.get(normalized)
    if fingerprint is None:
        raise _stale_ref()

    current_url = _page_url(page)
    if current_url and current_url != registry.state.url:
        raise _stale_ref()
    current_target_id = _page_target_id(page)
    if (
        registry.state.target_id is not None
        and current_target_id is not None
        and current_target_id != registry.state.target_id
    ):
        raise _stale_ref()

    selector = '[data-rustwright-ref="%s:%s"]' % (registry.state.generation, normalized)
    locator = page.locator(selector)
    count = locator.count()
    if count == 0:
        raise _stale_ref()
    if count > 1:
        raise AgentError(
            "ref_integrity_error",
            "The ref resolved to more than one element",
            "Take a new snapshot before retrying the action.",
        )

    # A page can still copy or mutate tagged nodes between these checks. This
    # fingerprint comparison only catches ordinary replacement and drift.
    current = None
    try:
        value = locator.evaluate(build_fingerprint_expression())
        if isinstance(value, dict) and isinstance(value.get("role"), str) and isinstance(value.get("name"), str):
            current = {"role": value["role"], "name": value["name"]}
    except Exception:
        current = None
    if current is not None and current != fingerprint:
        raise _stale_ref()

    return locator
