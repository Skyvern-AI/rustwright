from __future__ import annotations

import ast
import asyncio
import threading
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.generate_async_api import (
    GENERATED_METHODS,
    HAND_ASYNC_HELPERS,
    HAND_METHODS,
    SINGLE_DISPATCH_METHODS,
    SLICED_WAIT_METHODS,
    generate_async_api,
)


ROOT = Path(__file__).resolve().parents[1]
SYNC_API = ROOT / "python" / "rustwright" / "sync_api.py"
ASYNC_API = ROOT / "python" / "rustwright" / "async_api.py"
GENERATED_API = ROOT / "python" / "rustwright" / "_async_generated.py"


def test_async_generated_file_is_fresh(tmp_path: Path) -> None:
    regenerated = tmp_path / "_async_generated.py"
    regenerated.write_text(
        generate_async_api(SYNC_API.read_text(encoding="utf-8")),
        encoding="utf-8",
    )

    assert regenerated.read_bytes() == GENERATED_API.read_bytes(), (
        "python/rustwright/_async_generated.py is stale; "
        "run `python tools/generate_async_api.py`"
    )


def test_every_class_async_method_has_one_owner() -> None:
    tree = ast.parse(ASYNC_API.read_text(encoding="utf-8"))
    actual_hand = {
        (class_node.name, method.name)
        for class_node in tree.body
        if isinstance(class_node, ast.ClassDef)
        for method in class_node.body
        if isinstance(method, ast.AsyncFunctionDef)
    }
    expected_hand = {
        (class_name, method_name)
        for class_name, method_names in HAND_METHODS.items()
        for method_name in method_names
    }
    expected_generated = {
        (class_name, method_name)
        for class_name, method_names in GENERATED_METHODS.items()
        for method_name in method_names
    }

    assert actual_hand == expected_hand
    assert actual_hand.isdisjoint(expected_generated)
    assert len(actual_hand | expected_generated) == 391


def test_only_observational_waits_use_sliced_runner() -> None:
    assert SLICED_WAIT_METHODS == {
        "AsyncPage": ("wait_for_url", "wait_for_function"),
        "AsyncFrame": ("wait_for_selector", "wait_for_url", "wait_for_function"),
        "AsyncLocator": ("wait_for",),
        "AsyncElementHandle": ("wait_for_selector", "wait_for_element_state"),
    }
    wait_names = {name for names in SLICED_WAIT_METHODS.values() for name in names}
    wait_names.add("wait_for_load_state")
    for class_name, methods in SINGLE_DISPATCH_METHODS.items():
        assert set(methods).isdisjoint(wait_names)
        assert set(methods) <= set(GENERATED_METHODS[class_name])

    # Include handwritten fallbacks, such as Page.click and Locator.drag_to.
    for source in (ASYNC_API, GENERATED_API):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for class_node in tree.body:
            if not isinstance(class_node, ast.ClassDef):
                continue
            for method in class_node.body:
                if not isinstance(method, ast.AsyncFunctionDef):
                    continue
                runners = {
                    node.func.id
                    for node in ast.walk(method)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                }
                if runners & {"_run_sync_wait_sliced", "_generated_run_sync_wait_sliced"}:
                    assert method.name in wait_names, (class_node.name, method.name)


@pytest.mark.parametrize("timeout", [2000, None, 0])
@pytest.mark.parametrize("message", [
    "Locator.press: timed out after 50 ms",
    "Locator.press: Timeout 50ms exceeded.\nCall log: waiting for target",
    "timed out waiting for locator to be actionable while trying to click; no element matched",
])
def test_async_mutation_timeout_is_not_retried(timeout, message) -> None:
    from rustwright.async_api import AsyncLocator, TimeoutError

    calls = []
    error = TimeoutError(message)
    error._rustwright_error_kind = "action_timeout"
    error._rustwright_error_payload = {"phase": "dispatch", "retryable": False}

    class SyncLocator:
        _page = SimpleNamespace(_default_timeout=1234)

        def press(self, key, **kwargs):
            calls.append((key, kwargs["timeout"]))
            raise error

    async def run():
        with pytest.raises(TimeoutError) as caught:
            await AsyncLocator(SyncLocator()).press("Enter", timeout=timeout)
        label = 1234 if timeout is None else timeout
        expected = message.replace("timed out after 50 ms", f"Timeout {label}ms exceeded.")
        expected = expected.replace("Timeout 50ms exceeded", f"Timeout {label}ms exceeded")
        assert str(caught.value) == expected
        assert caught.value._rustwright_error_kind == "action_timeout"
        assert caught.value._rustwright_error_payload == error._rustwright_error_payload

    asyncio.run(run())
    assert calls == [("Enter", timeout)]


@pytest.mark.parametrize("cancel_at", [None, "wait", "dispatch"])
def test_async_mutation_yields_and_cancellation_does_not_retry(cancel_at, monkeypatch) -> None:
    from rustwright import async_api, sync_api

    tokens = []
    original_run_sync_call = async_api._run_sync_call

    async def record_executor_call(func, *args, **kwargs):
        tokens.append(sync_api._ACTION_CANCELLATION.get())
        return await original_run_sync_call(func, *args, **kwargs)

    monkeypatch.setattr(async_api, "_run_sync_call", record_executor_call)

    async def run():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        committed = asyncio.Event()
        release = threading.Event()
        finished = threading.Event()
        stopped_by_cancellation = threading.Event()
        calls = []
        events = []

        def dispatch():
            loop.call_soon_threadsafe(committed.set)
            try:
                assert release.wait(2), "dispatch must leave the event loop free"
                events.append("input")
            finally:
                # Even a native call in cleanup must survive late cancellation.
                sync_api._call(events.append, "cleanup")

        class SyncLocator:
            def click(self, **kwargs):
                calls.append((threading.get_ident(), kwargs["timeout"]))
                loop.call_soon_threadsafe(entered.set)
                try:
                    if cancel_at != "dispatch":
                        deadline = time.monotonic() + 30
                        while not release.wait(0.01):
                            sync_api._check_action_cancelled()
                            assert time.monotonic() < deadline, "worker did not observe cancellation"
                    sync_api._check_action_cancelled()
                    sync_api._call_action(dispatch)
                except sync_api.Error:
                    stopped_by_cancellation.set()
                    raise
                finally:
                    finished.set()

        task = asyncio.create_task(async_api.AsyncLocator(SyncLocator()).click(timeout=30000))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            assert not task.done()
            assert len(calls) == 1
            assert calls[0][1] == 30000
            assert calls[0][0] != threading.get_ident()
            assert len(tokens) == 1 and tokens[0] is not None
            if cancel_at:
                if cancel_at == "dispatch":
                    await asyncio.wait_for(committed.wait(), timeout=1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert tokens[0].is_cancelled()
                if cancel_at == "wait":
                    # No manual release: signalling the token must stop the worker.
                    assert await asyncio.to_thread(finished.wait, 2)
                    assert stopped_by_cancellation.is_set()
                    with pytest.raises(RuntimeError):
                        tokens[0].run_committed(events.append, ("late input",), {})
                    assert events == []
        finally:
            release.set()
        if cancel_at is None:
            assert await task is None
        assert await asyncio.to_thread(finished.wait, 2)
        assert len(calls) == 1
        assert events == ([] if cancel_at == "wait" else ["input", "cleanup"])
        assert sync_api._ACTION_CANCELLATION.get() is None

    asyncio.run(run())


def test_non_class_async_helpers_stay_hand_written() -> None:
    tree = ast.parse(ASYNC_API.read_text(encoding="utf-8"))
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    qualified: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.ClassDef):
            continue
        parts = [node.name]
        while parent is not None and not isinstance(parent, ast.Module):
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                parts.append(parent.name)
            parent = parents.get(parent)
        qualified.append(".".join(reversed(parts)))

    assert Counter(qualified) == Counter(HAND_ASYNC_HELPERS)
