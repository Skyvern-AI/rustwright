from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

from tools.generate_async_api import (
    GENERATED_METHODS,
    HAND_ASYNC_HELPERS,
    HAND_METHODS,
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
    assert len(actual_hand | expected_generated) == 389


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
