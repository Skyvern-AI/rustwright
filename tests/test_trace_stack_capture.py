from __future__ import annotations

import gc
import functools
import inspect
import linecache
import sys
import types
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from rustwright import sync_api


_SYNC_API_FILE = Path(sync_api.__file__).resolve()


class _Frame:
    def __init__(self, filename: str, lineno: Any, function: str, back: "_Frame | None" = None):
        self.f_code = SimpleNamespace(co_filename=filename, co_name=function)
        self.f_lineno = lineno
        self.f_back = back


def _tracing() -> sync_api.Tracing:
    tracing = sync_api.Tracing.__new__(sync_api.Tracing)
    tracing._sources = True
    tracing._source_file_indexes = {}
    tracing._source_files = []
    tracing._source_stacks = []
    return tracing


def _state(tracing: sync_api.Tracing) -> tuple[dict[str, int], list[str], list[list[Any]]]:
    return tracing._source_file_indexes, tracing._source_files, tracing._source_stacks


def _eager_record_source_stack(tracing: sync_api.Tracing, call_id: str) -> None:
    """Faithful reference for the replaced eager inspect.stack()[2:] path."""
    if not tracing._sources:
        return
    try:
        stack_id = int(str(call_id).rsplit("@", 1)[1])
    except (IndexError, TypeError, ValueError):
        return
    stack_info = inspect.stack()[2:]
    frames: list[list[Any]] = []
    fallback_frames: list[list[Any]] = []
    current_file = Path(sync_api.__file__).resolve()
    try:
        for item in stack_info:
            filename = item.filename
            if not filename:
                continue
            try:
                resolved = Path(filename).resolve()
            except OSError:
                resolved = Path(filename)
            file_key = str(resolved)
            file_index = tracing._source_file_indexes.get(file_key)
            if file_index is None:
                file_index = len(tracing._source_files)
                tracing._source_file_indexes[file_key] = file_index
                tracing._source_files.append(filename)
            entry = [file_index, int(item.lineno), 0, str(item.function or "<module>")]
            if len(fallback_frames) < 8:
                fallback_frames.append(entry)
            if resolved != current_file:
                parts = set(resolved.parts)
                if not ({"concurrent", "futures"}.issubset(parts) or resolved.name in {"threading.py", "_base.py"}):
                    frames.append(entry)
                    if len(frames) >= 8:
                        break
        if not frames:
            frames = fallback_frames[:8]
        if frames:
            tracing._source_stacks.append([stack_id, frames])
    finally:
        stack_info = []


Recorder = Callable[[str], None]


def _same_topology(recorder: Recorder) -> None:
    recorder("call@17")


def _run(tracing: sync_api.Tracing, production: bool) -> None:
    recorder = tracing._record_source_stack if production else functools.partial(_eager_record_source_stack, tracing)
    _same_topology(recorder)


def _run_pair(reference: sync_api.Tracing, production: sync_api.Tracing) -> None:
    recorders = (
        functools.partial(_eager_record_source_stack, reference),
        production._record_source_stack,
    )
    for recorder in recorders:
        _same_topology(recorder)


def _run_reference(tracing: sync_api.Tracing) -> None:
    _run(tracing, False)


def _run_production(tracing: sync_api.Tracing) -> None:
    _run(tracing, True)


def _frame_chain(descriptors: list[tuple[str, Any, str]]) -> _Frame | None:
    frame = None
    for filename, lineno, function in reversed(descriptors):
        frame = _Frame(filename, lineno, function, frame)
    return frame


def _install_equivalent_synthetic_stacks(
    monkeypatch: pytest.MonkeyPatch, descriptors: list[tuple[str, Any, str]]
) -> list[int]:
    frame = _frame_chain(descriptors)
    depths: list[int] = []

    def getframe(depth: int):
        depths.append(depth)
        return frame

    items = [SimpleNamespace(filename="skip.py", lineno=0, function="skip")] * 2
    items.extend(SimpleNamespace(filename=name, lineno=line, function=function) for name, line, function in descriptors)
    monkeypatch.setattr(sys, "_getframe", getframe)
    monkeypatch.setattr(inspect, "stack", lambda: items)
    return depths


@pytest.mark.parametrize(
    "descriptors",
    [
        [
            (str(_SYNC_API_FILE), 10, "_trace_begin_action"),
            ("", 11, "ignored"),
            ("relative/user.py", 12, "first"),
            ("/usr/lib/python/concurrent/futures/thread.py", 13, "run"),
            ("relative/../relative/user.py", 14, "second"),
            ("module.py", 15, ""),
        ],
        [(f"depth_{index}.py", 100 + index, f"call_{index}") for index in range(12)],
        [(str(_SYNC_API_FILE), index, "internal") for index in range(1, 11)],
        [("threading.py", 1, "thread"), ("_base.py", 2, "base")],
    ],
)
def test_source_stack_capture_matches_eager_reference_and_depth(monkeypatch, descriptors):
    depths = _install_equivalent_synthetic_stacks(monkeypatch, descriptors)
    reference = _tracing()
    production = _tracing()

    _run_reference(reference)
    _run_production(production)

    assert depths == [2]
    assert _state(production) == _state(reference)


def test_source_stack_capture_matches_eager_reference_on_real_stack():
    reference = _tracing()
    production = _tracing()

    _run_pair(reference, production)

    reference_frames = _state(reference)
    production_frames = _state(production)
    assert production_frames == reference_frames
    assert production._source_stacks[0][1][0][3] == "_run_pair"


@pytest.mark.parametrize("call_id", [None, "", "call", "call@", "call@not-an-int", object()])
def test_source_stack_capture_silently_ignores_malformed_call_ids(monkeypatch, call_id):
    tracing = _tracing()
    monkeypatch.setattr(sys, "_getframe", lambda depth: pytest.fail("stack getter must not run"))
    tracing._record_source_stack(call_id)  # type: ignore[arg-type]
    assert _state(tracing) == ({}, [], [])


def test_source_stack_capture_preserves_resolved_key_and_original_filename(monkeypatch):
    aliases = ["folder/../user.py", "user.py"]
    descriptors = [(aliases[0], 1, "first"), (aliases[1], 2, "second")]
    _install_equivalent_synthetic_stacks(monkeypatch, descriptors)
    tracing = _tracing()
    _run_production(tracing)
    assert list(tracing._source_file_indexes.values()) == [0]
    assert tracing._source_files == [aliases[0]]
    assert [frame[0] for frame in tracing._source_stacks[0][1]] == [0, 0]


def test_source_stack_capture_avoids_rich_inspection_and_source_lookup(monkeypatch):
    frame = _frame_chain([("user.py", 23, "action")])
    monkeypatch.setattr(sys, "_getframe", lambda depth: frame)
    for owner, name in (
        (inspect, "stack"),
        (inspect, "getouterframes"),
        (inspect, "getframeinfo"),
        (linecache, "getline"),
        (linecache, "getlines"),
    ):
        monkeypatch.setattr(owner, name, lambda *args, **kwargs: pytest.fail("rich stack/source lookup"))
    tracing = _tracing()
    tracing._record_source_stack("call@3")
    assert tracing._source_stacks == [[3, [[0, 23, 0, "action"]]]]


class _Sentinel:
    pass


def _assert_primitive_trace_state(value: Any) -> None:
    forbidden = (types.FrameType, types.TracebackType, types.CodeType)
    assert not isinstance(value, forbidden)
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_primitive_trace_state(key)
            _assert_primitive_trace_state(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            _assert_primitive_trace_state(item)


def _capture_real_local(tracing: sync_api.Tracing, explode: bool) -> weakref.ReferenceType[_Sentinel]:
    sentinel = _Sentinel()
    reference = weakref.ref(sentinel)
    if explode:
        class ExplodingIndexes(dict):
            def get(self, key, default=None):
                raise RuntimeError("real-frame indexing failure")

        tracing._source_file_indexes = ExplodingIndexes()
        try:
            tracing._record_source_stack("call@1")
        except RuntimeError as error:
            assert str(error) == "real-frame indexing failure"
        finally:
            error = None
    else:
        tracing._record_source_stack("call@1")
    return reference


@pytest.mark.parametrize("explode", [False, True])
def test_source_stack_capture_releases_real_caller_frame_and_locals(explode):
    tracing = _tracing()
    references = [_capture_real_local(tracing, explode) for _ in range(5)]
    gc.collect()
    assert all(reference() is None for reference in references)
    _assert_primitive_trace_state(_state(tracing))


def test_source_stack_capture_preserves_early_getter_exception(monkeypatch):
    def explode(depth: int):
        assert depth == 2
        raise LookupError("early getter failure")

    monkeypatch.setattr(sys, "_getframe", explode)
    with pytest.raises(LookupError, match="early getter failure") as caught:
        _tracing()._record_source_stack("call@1")
    assert caught.value.args == ("early getter failure",)


def test_source_stack_capture_preserves_f_code_exception(monkeypatch):
    class ExplodingFrame:
        @property
        def f_code(self):
            raise AttributeError("f_code failure")

    monkeypatch.setattr(sys, "_getframe", lambda depth: ExplodingFrame())
    with pytest.raises(AttributeError, match="f_code failure") as caught:
        _tracing()._record_source_stack("call@1")
    assert caught.value.args == ("f_code failure",)


@pytest.mark.parametrize("failure_site", ["normalization", "indexing", "line"])
def test_source_stack_capture_matches_reference_exception_and_partial_state(monkeypatch, failure_site):
    descriptors = [("first.py", 10, "first"), ("explode.py", 11, "second")]
    _install_equivalent_synthetic_stacks(monkeypatch, descriptors)
    original_resolve = Path.resolve

    def configure(tracing):
        if failure_site == "indexing":
            class ExplodingIndexes(dict):
                def get(self, key, default=None):
                    if str(key).endswith("explode.py"):
                        raise KeyError("index failure")
                    return super().get(key, default)
            tracing._source_file_indexes = ExplodingIndexes()

    if failure_site == "normalization":
        def resolve(path, *args, **kwargs):
            if str(path).endswith("explode.py"):
                raise RuntimeError("normalization failure")
            return original_resolve(path, *args, **kwargs)
        monkeypatch.setattr(Path, "resolve", resolve)
    elif failure_site == "line":
        descriptors[1] = ("explode.py", "not-an-int", "second")
        _install_equivalent_synthetic_stacks(monkeypatch, descriptors)

    outcomes = []
    for runner in (_run_reference, _run_production):
        tracing = _tracing()
        configure(tracing)
        with pytest.raises(Exception) as caught:
            runner(tracing)
        outcomes.append((type(caught.value), caught.value.args, _state(tracing)))
    assert outcomes[1] == outcomes[0]
