#!/usr/bin/env python3
"""Generate the reproducible Rustwright API parity map.

The comparison is intentionally structural.  A matching name means that a
public member exists, not that every argument or behavior is compatible.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import venv
from collections import defaultdict, deque
from pathlib import Path
from typing import Any


if sys.version_info < (3, 9):
    # The analyzer relies on ast.unparse (3.9+). The library itself supports
    # 3.8+; only this maintainer tool needs the newer interpreter.
    sys.exit("tools/generate_parity_map.py requires Python 3.9+ (ast.unparse)")

ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = ROOT / "docs" / "PARITY.md"
CASES_PATH = ROOT / "benchmarks" / "automation_cases.py"
SUITE_PATH = ROOT / "tests" / "test_playwright_parity_cases.py"
LIMITATIONS_PATH = ROOT / "LIMITATIONS.md"
NODE_README_PATH = ROOT / "node" / "README.md"
THROWAWAY_VENV = ROOT / ".venv-parity"
PINNED_PLAYWRIGHT = "1.61.0"

# ast.TryStar (PEP 654 `except*`) only exists on Python 3.11+. The repo supports
# 3.8+, so guard the reference to keep the analyzer importable on older versions.
_TRY_NODES = (ast.Try, ast.TryStar) if hasattr(ast, "TryStar") else (ast.Try,)


def venv_python(venv_dir: Path) -> Path:
    """Interpreter path inside a venv, correct on POSIX and Windows."""
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


GROUPS: list[tuple[str, tuple[str, ...]]] = [
    (
        "Browser",
        (
            "Playwright",
            "PlaywrightContextManager",
            "BrowserType",
            "Browser",
            "ChromiumBrowserContext",
            "Selectors",
        ),
    ),
    ("BrowserContext", ("BrowserContext",)),
    ("Page", ("Page",)),
    ("Locator", ("Locator", "ElementHandle", "JSHandle", "FrameLocator")),
    ("Frame", ("Frame",)),
    ("Input", ("Keyboard", "Mouse", "Touchscreen")),
    (
        "Network and routing",
        (
            "Request",
            "Response",
            "Route",
            "WebSocket",
            "WebSocketRoute",
            "APIRequest",
            "APIRequestContext",
            "APIResponse",
        ),
    ),
    (
        "Tracing and protocol",
        ("Tracing", "CDPSession", "Clock", "Debugger", "Screencast", "WebStorage"),
    ),
    (
        "Events and artifacts",
        ("ConsoleMessage", "Dialog", "Download", "FileChooser", "Video", "Worker", "WebError"),
    ),
    (
        "Assertions and other returned objects",
        (
            "PageAssertions",
            "LocatorAssertions",
            "APIResponseAssertions",
            "Expect",
            "Credentials",
            "Disposable",
        ),
    ),
]

MAJOR_CLASSES = ("Browser", "BrowserContext", "Page", "Locator", "Frame")

EVENT_TYPES = {
    "backgroundpage": "Page",
    "close": None,
    "console": "ConsoleMessage",
    "dialog": "Dialog",
    "download": "Download",
    "filechooser": "FileChooser",
    "page": "Page",
    "pageerror": "WebError",
    "popup": "Page",
    "request": "Request",
    "requestfailed": "Request",
    "requestfinished": "Request",
    "response": "Response",
    "serviceworker": "Worker",
    "websocket": "WebSocket",
    "worker": "Worker",
}


def snake_name(name: str) -> str:
    """Return a stable comparison key for Python and camelCase spellings."""
    first = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name.replace("-", "_"))
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", first).lower()


INTROSPECTION_PROGRAM = r'''
import importlib.metadata
import inspect
import json
import re
import sys

package = sys.argv[1]
sync = __import__(package + ".sync_api", fromlist=["*"])
async_mod = __import__(package + ".async_api", fromlist=["*"])
root = __import__(package, fromlist=["*"])

def generated(mod):
    try:
        return __import__(mod.__name__ + "._generated", fromlist=["*"])
    except ImportError:
        return None

def classes(mod):
    result = {}
    candidates = [mod]
    internal = generated(mod)
    if internal is not None:
        candidates.append(internal)
    for source in candidates:
        for name, value in vars(source).items():
            if name.startswith("_") or not inspect.isclass(value):
                continue
            owner = getattr(value, "__module__", "")
            if source is internal and owner != internal.__name__:
                continue
            if source is mod and not (owner.startswith(mod.__name__) or name in {"Expect"}):
                continue
            result.setdefault(name, value)
    return result

sync_classes = classes(sync)
async_classes = classes(async_mod)
known = set(sync_classes) | set(async_classes)

def annotation_types(annotation):
    if annotation is inspect.Signature.empty:
        return []
    text = str(annotation)
    return sorted(
        name
        for name in known
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", text)
    )

def surface(class_map):
    result = {}
    for class_name, cls in sorted(class_map.items()):
        members = {}
        # The generated class dictionary is the documented API declaration.
        # Inherited pyee helpers such as emit() are implementation machinery.
        for name, raw in sorted(vars(cls).items()):
            if name.startswith("_"):
                continue
            if isinstance(raw, property):
                target = raw.fget
                kind = "property"
            elif isinstance(raw, (staticmethod, classmethod)):
                target = raw.__func__
                kind = "method"
            elif callable(raw):
                target = raw
                kind = "method"
            else:
                continue
            try:
                signature = inspect.signature(target)
            except (TypeError, ValueError):
                signature = None
            parameters = []
            returns = []
            if signature is not None:
                returns = annotation_types(signature.return_annotation)
                for parameter in signature.parameters.values():
                    if parameter.name in {"self", "cls"}:
                        continue
                    parameters.append({
                        "name": parameter.name,
                        "types": annotation_types(parameter.annotation),
                    })
            members[name] = {"kind": kind, "returns": returns, "parameters": parameters}
        result[class_name] = members
    return result

def available(class_map):
    result = {}
    for class_name, cls in sorted(class_map.items()):
        result[class_name] = sorted(name for name in dir(cls) if not name.startswith("_"))
    return result

def exports(mod):
    names = getattr(mod, "__all__", None)
    if names is None:
        names = [name for name in vars(mod) if not name.startswith("_")]
    return sorted(set(names))

try:
    version = importlib.metadata.version(package)
except importlib.metadata.PackageNotFoundError:
    version = "local"

print(json.dumps({
    "version": version,
    "root_exports": exports(root),
    "sync_exports": exports(sync),
    "async_exports": exports(async_mod),
    "sync": surface(sync_classes),
    "async": surface(async_classes),
    "sync_available": available(sync_classes),
    "async_available": available(async_classes),
}, sort_keys=True))
'''


def can_import(interpreter: Path, package: str) -> bool:
    if not interpreter.exists():
        return False
    result = subprocess.run(
        [str(interpreter), "-c", f"import {package}.sync_api, {package}.async_api"],
        cwd=tempfile.gettempdir(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def package_version(interpreter: Path, package: str) -> str | None:
    if not can_import(interpreter, package):
        return None
    result = subprocess.run(
        [
            str(interpreter),
            "-c",
            f"import importlib.metadata; print(importlib.metadata.version({package!r}))",
        ],
        cwd=tempfile.gettempdir(),
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def reference_interpreter() -> Path:
    # Only accept the project venv when it holds the pinned Playwright; the
    # published map must not vary with whatever version happens to be local.
    project_python = venv_python(ROOT / ".venv")
    if package_version(project_python, "playwright") == PINNED_PLAYWRIGHT:
        return project_python
    python = venv_python(THROWAWAY_VENV)
    if package_version(python, "playwright") != PINNED_PLAYWRIGHT:
        if not python.exists():
            venv.EnvBuilder(with_pip=True).create(THROWAWAY_VENV)
        subprocess.run(
            [str(python), "-m", "pip", "install", f"playwright=={PINNED_PLAYWRIGHT}"],
            cwd=ROOT,
            check=True,
        )
    return python


def rustwright_interpreter() -> Path:
    """Interpreter whose importable ``rustwright`` belongs to this checkout.

    Prefer the repo-local ``.venv``; fall back to the current interpreter only
    when its ``rustwright`` actually resolves inside ``ROOT``, so the map can
    never describe a stale globally-installed build instead of the reviewed code.
    """
    built = venv_python(ROOT / ".venv")
    if can_import(built, "rustwright"):
        return built
    current = Path(sys.executable)
    if can_import(current, "rustwright") and _rustwright_in_checkout(current):
        return current
    raise RuntimeError(
        "rustwright is not importable from this checkout; build it with "
        "`maturin develop` into .venv first"
    )


def _rustwright_in_checkout(interpreter: Path) -> bool:
    """True when ``interpreter`` imports a ``rustwright`` located under ROOT."""
    probe = "import rustwright, pathlib, sys; sys.stdout.write(rustwright.__file__ or '')"
    result = subprocess.run(
        [str(interpreter), "-c", probe],
        capture_output=True,
        text=True,
        check=False,
    )
    location = result.stdout.strip()
    if not location:
        return False
    try:
        Path(location).resolve().relative_to(ROOT.resolve())
    except ValueError:
        return False
    return True


def introspect(interpreter: Path, package: str) -> dict[str, Any]:
    result = subprocess.run(
        [str(interpreter), "-c", INTROSPECTION_PROGRAM, package],
        cwd=tempfile.gettempdir(),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"failed to introspect {package}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def registry_names(interpreter: Path) -> list[str]:
    program = textwrap.dedent(
        f"""
        import json, sys
        sys.path.insert(0, {str(ROOT)!r})
        from benchmarks.automation_cases import CASES
        print(json.dumps([case.__name__ for case in CASES]))
        """
    )
    result = subprocess.run(
        [str(interpreter), "-c", program],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    names = json.loads(result.stdout)
    if len(names) != len(set(names)):
        raise RuntimeError("the parity case registry contains duplicate function names")
    return names


def validate_suite_uses_real_playwright_registry() -> None:
    tree = ast.parse(SUITE_PATH.read_text(encoding="utf-8"), filename=str(SUITE_PATH))
    imports_cases = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "benchmarks.automation_cases"
        and any(alias.name == "CASES" for alias in node.names)
        for node in ast.walk(tree)
    )
    mentions_playwright = any(
        isinstance(node, ast.Constant) and node.value == "playwright" for node in ast.walk(tree)
    )
    runs_parity = any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_run_parity"
        for node in ast.walk(tree)
    )
    checks_registry_length = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "len"
        and any(isinstance(arg, ast.Name) and arg.id == "CASES" for arg in node.args)
        for node in ast.walk(tree)
    )
    if not all((imports_cases, mentions_playwright, runs_parity, checks_registry_length)):
        raise RuntimeError("the shared parity test no longer proves that real Playwright runs the CASES registry")


def normalized_members(surface: dict[str, Any], class_name: str) -> dict[str, tuple[str, dict[str, Any]]]:
    result: dict[str, tuple[str, dict[str, Any]]] = {}
    for name, metadata in surface.get(class_name, {}).items():
        key = snake_name(name)
        if key in result and result[key][0] != name:
            raise RuntimeError(f"normalization collision in {class_name}: {result[key][0]} and {name}")
        result[key] = (name, metadata)
    return result


def normalized_names(surface: dict[str, list[str]], class_name: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in surface.get(class_name, []):
        key = snake_name(name)
        if key in result and result[key] != name:
            raise RuntimeError(f"normalization collision in {class_name}: {result[key]} and {name}")
        result[key] = name
    return result


class ExerciseAnalyzer:
    """Conservatively resolve API member use in registered parity cases."""

    def __init__(self, tree: ast.Module, cases: list[str], reference: dict[str, Any]) -> None:
        self.tree = tree
        self.case_names = set(cases)
        self.reference = reference
        self.known_classes = set(reference)
        self.top_functions = {
            node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        missing = self.case_names - self.top_functions.keys()
        if missing:
            raise RuntimeError(f"registered cases missing from source: {', '.join(sorted(missing))}")
        self.reachable = self._reachable_functions()
        self.functions: list[ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda] = []
        for fn in self.reachable.values():
            self.functions.append(fn)
            self.functions.extend(
                node
                for node in ast.walk(fn)
                if node is not fn and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            )
        self.functions_by_name: dict[str, list[Any]] = defaultdict(list)
        for fn in self.functions:
            if not isinstance(fn, ast.Lambda):
                self.functions_by_name[fn.name].append(fn)
        self.envs: dict[int, dict[str, set[str]]] = {id(fn): {} for fn in self.functions}
        self.returns: dict[int, set[str]] = {id(fn): set() for fn in self.functions}
        self.exercised: set[tuple[str, str]] = set()
        self.ambiguous: set[tuple[int, int]] = set()
        for name in self.case_names:
            fn = self.top_functions[name]
            args = list(fn.args.posonlyargs) + list(fn.args.args) + list(fn.args.kwonlyargs)
            for arg in args:
                if arg.arg == "page":
                    self.envs[id(fn)][arg.arg] = {"Page"}
                elif arg.arg == "playwright":
                    self.envs[id(fn)][arg.arg] = {"Playwright"}
                else:
                    annotated = self._annotation_classes(arg.annotation)
                    if annotated:
                        self.envs[id(fn)][arg.arg] = annotated

    def _reachable_functions(self) -> dict[str, Any]:
        reachable: dict[str, Any] = {}
        queue = deque(sorted(self.case_names))
        while queue:
            name = queue.popleft()
            if name in reachable:
                continue
            fn = self.top_functions[name]
            reachable[name] = fn
            referenced = {
                node.id
                for node in ast.walk(fn)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in self.top_functions
            }
            queue.extend(sorted(referenced - reachable.keys()))
        return reachable

    def _annotation_classes(self, annotation: ast.expr | None) -> set[str]:
        if annotation is None:
            return set()
        text = ast.unparse(annotation)
        return {name for name in self.known_classes if re.search(rf"\b{re.escape(name)}\b", text)}

    def _member(self, class_name: str, name: str) -> tuple[str, dict[str, Any]] | None:
        return normalized_members(self.reference, class_name).get(snake_name(name))

    @staticmethod
    def _args(fn: Any) -> list[ast.arg]:
        return list(fn.args.posonlyargs) + list(fn.args.args) + list(fn.args.kwonlyargs)

    def _bind_function(self, node: ast.AST, types: list[set[str]]) -> bool:
        changed = False
        targets: list[Any] = []
        if isinstance(node, ast.Lambda):
            targets = [node]
            if node not in self.functions:
                self.functions.append(node)
                self.envs[id(node)] = {}
                self.returns[id(node)] = set()
        elif isinstance(node, ast.Name):
            targets = self.functions_by_name.get(node.id, [])
        for fn in targets:
            env = self.envs[id(fn)]
            for arg, inferred in zip(self._args(fn), types):
                if not inferred:
                    continue
                before = env.get(arg.arg, set())
                after = before | inferred
                if after != before:
                    env[arg.arg] = after
                    changed = True
        return changed

    def _record(self, receiver_types: set[str], member_name: str, node: ast.AST) -> dict[str, Any] | None:
        matches = []
        for class_name in receiver_types:
            member = self._member(class_name, member_name)
            if member is not None:
                matches.append((class_name, member))
        if len(matches) != 1:
            if len(matches) > 1:
                self.ambiguous.add((getattr(node, "lineno", 0), getattr(node, "col_offset", 0)))
            return None
        class_name, (actual_name, metadata) = matches[0]
        self.exercised.add((class_name, snake_name(actual_name)))
        return metadata

    def _event_type(self, call: ast.Call, receiver_types: set[str]) -> set[str]:
        if not call.args or not isinstance(call.args[0], ast.Constant) or not isinstance(call.args[0].value, str):
            return set()
        event = snake_name(call.args[0].value).replace("_", "")
        event_type = EVENT_TYPES.get(event)
        if event_type is None and event == "close" and len(receiver_types) == 1:
            return set(receiver_types)
        return {event_type} if event_type else set()

    def _infer(self, node: ast.AST | None, env: dict[str, set[str]]) -> set[str]:
        if node is None:
            return set()
        if isinstance(node, ast.Name):
            return set(env.get(node.id, set()))
        if isinstance(node, ast.IfExp):
            return self._infer(node.body, env) | self._infer(node.orelse, env)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            result: set[str] = set()
            for item in node.elts:
                result |= self._infer(item, env)
            return result
        if isinstance(node, ast.Subscript):
            return self._infer(node.value, env)
        if isinstance(node, ast.Attribute):
            receiver = self._infer(node.value, env)
            if node.attr == "value" and receiver:
                return receiver
            metadata = self._record(receiver, node.attr, node)
            return set(metadata.get("returns", [])) if metadata else set()
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id == "expect" and node.args:
                    actual = self._infer(node.args[0], env)
                    if "Page" in actual:
                        return {"PageAssertions"}
                    if "Locator" in actual:
                        return {"LocatorAssertions"}
                    if "APIResponse" in actual or "Response" in actual:
                        return {"APIResponseAssertions"}
                targets = self.functions_by_name.get(node.func.id, [])
                for target in targets:
                    types = [self._infer(arg, env) for arg in node.args]
                    self._bind_function(node.func, types)
                result: set[str] = set()
                for target in targets:
                    result |= self.returns[id(target)]
                for arg in node.args:
                    self._infer(arg, env)
                for keyword in node.keywords:
                    self._infer(keyword.value, env)
                return result
            if isinstance(node.func, ast.Attribute):
                receiver = self._infer(node.func.value, env)
                metadata = self._record(receiver, node.func.attr, node.func)
                positional = list(node.args)
                keyword = {item.arg: item.value for item in node.keywords if item.arg}
                if metadata:
                    parameters = metadata.get("parameters", [])
                    for index, parameter in enumerate(parameters):
                        value = positional[index] if index < len(positional) else keyword.get(parameter["name"])
                        callback_types = [set(parameter.get("types", []))]
                        if value is not None and callback_types[0]:
                            self._bind_function(value, callback_types)
                    if snake_name(node.func.attr) in {"on", "once", "expect_event", "wait_for_event"}:
                        event_types = self._event_type(node, receiver)
                        if len(positional) > 1 and event_types:
                            self._bind_function(positional[1], [event_types])
                    result = set(metadata.get("returns", []))
                    normalized = snake_name(node.func.attr)
                    if normalized.startswith("expect_") or normalized == "wait_for_event":
                        result |= self._event_type(node, receiver)
                else:
                    result = set()
                for arg in node.args:
                    self._infer(arg, env)
                for item in node.keywords:
                    self._infer(item.value, env)
                return result
            self._infer(node.func, env)
            return set()
        result: set[str] = set()
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.expr_context, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                result |= self._infer(child, env)
        return result

    def _walk_statements(self, statements: list[ast.stmt], fn: Any) -> bool:
        env = self.envs[id(fn)]
        changed = False
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(statement, ast.Assign):
                inferred = self._infer(statement.value, env)
                for target in statement.targets:
                    if isinstance(target, ast.Name) and inferred and env.get(target.id) != inferred:
                        env[target.id] = inferred
                        changed = True
            elif isinstance(statement, ast.AnnAssign):
                inferred = self._infer(statement.value, env) | self._annotation_classes(statement.annotation)
                if isinstance(statement.target, ast.Name) and inferred and env.get(statement.target.id) != inferred:
                    env[statement.target.id] = inferred
                    changed = True
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                for item in statement.items:
                    inferred = self._infer(item.context_expr, env)
                    if (
                        isinstance(item.optional_vars, ast.Name)
                        and inferred
                        and env.get(item.optional_vars.id) != inferred
                    ):
                        env[item.optional_vars.id] = inferred
                        changed = True
                changed |= self._walk_statements(statement.body, fn)
            elif isinstance(statement, (ast.For, ast.AsyncFor)):
                inferred = self._infer(statement.iter, env)
                if isinstance(statement.target, ast.Name) and inferred and env.get(statement.target.id) != inferred:
                    env[statement.target.id] = inferred
                    changed = True
                changed |= self._walk_statements(statement.body, fn)
                changed |= self._walk_statements(statement.orelse, fn)
            elif isinstance(statement, ast.If):
                self._infer(statement.test, env)
                changed |= self._walk_statements(statement.body, fn)
                changed |= self._walk_statements(statement.orelse, fn)
            elif isinstance(statement, _TRY_NODES):
                changed |= self._walk_statements(statement.body, fn)
                for handler in statement.handlers:
                    changed |= self._walk_statements(handler.body, fn)
                changed |= self._walk_statements(statement.orelse, fn)
                changed |= self._walk_statements(statement.finalbody, fn)
            elif isinstance(statement, (ast.While,)):
                self._infer(statement.test, env)
                changed |= self._walk_statements(statement.body, fn)
                changed |= self._walk_statements(statement.orelse, fn)
            elif isinstance(statement, ast.Return):
                inferred = self._infer(statement.value, env)
                before = self.returns[id(fn)]
                after = before | inferred
                if after != before:
                    self.returns[id(fn)] = after
                    changed = True
            else:
                self._infer(statement, env)
        return changed

    def analyze(self) -> tuple[set[tuple[str, str]], int]:
        for _ in range(12):
            changed = False
            # New callback lambdas can be discovered during a pass.
            for fn in list(self.functions):
                body = [fn.body] if isinstance(fn, ast.Lambda) else fn.body
                changed |= self._walk_statements(body, fn)
            if not changed:
                break
        return self.exercised, len(self.ambiguous)


def availability(
    candidate_available: dict[str, list[str]], class_name: str, member_name: str
) -> bool:
    return snake_name(member_name) in normalized_names(candidate_available, class_name)


def status(present: bool, exercised: bool) -> str:
    if not present:
        return "❌ missing"
    if exercised:
        return "✅ present + exercised"
    return "🟡 present, not exercised"


def class_counts(
    reference: dict[str, Any],
    candidate_available: dict[str, list[str]],
    exercised: set[tuple[str, str]],
    class_name: str,
) -> tuple[int, int, int, int]:
    methods = {
        name: metadata
        for name, metadata in reference.get(class_name, {}).items()
        if metadata["kind"] == "method"
    }
    present = sum(availability(candidate_available, class_name, name) for name in methods)
    tested = sum(
        availability(candidate_available, class_name, name)
        and (class_name, snake_name(name)) in exercised
        for name in methods
    )
    return present, tested, len(methods) - present, len(methods)


def all_counts(
    reference: dict[str, Any], candidate_available: dict[str, list[str]], exercised: set[tuple[str, str]]
) -> tuple[int, int, int, int]:
    totals = [class_counts(reference, candidate_available, exercised, name) for name in sorted(reference)]
    return tuple(sum(values) for values in zip(*totals))  # type: ignore[return-value]


def parse_limitations() -> list[str]:
    lines = LIMITATIONS_PATH.read_text(encoding="utf-8").splitlines()
    bullets: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("- "):
            if current:
                bullets.append(" ".join(current))
            current = [line[2:].strip()]
        elif current and line.startswith("  "):
            current.append(line.strip())
        elif current and not line.strip():
            bullets.append(" ".join(current))
            current = []
    if current:
        bullets.append(" ".join(current))
    return [item.replace("(docs/async-design.md)", "(async-design.md)") for item in bullets]


def parse_node_subset() -> tuple[list[str], list[str]]:
    text = NODE_README_PATH.read_text(encoding="utf-8")
    match = re.search(r"Currently bridged:\s*(.*?)\nNot yet bridged:\s*(.*?)\.", text, re.DOTALL)
    if not match:
        raise RuntimeError("could not parse the Node subset from node/README.md")
    bridged = re.findall(r"`([^`]+)`", match.group(1))
    gaps = [re.sub(r"^and\s+", "", item.strip()) for item in re.sub(r"\s+", " ", match.group(2)).split(",")]
    return bridged, gaps


def export_summary(reference: list[str], candidate: list[str]) -> tuple[int, int, list[str], list[str]]:
    ref = {snake_name(name): name for name in reference}
    cand = {snake_name(name): name for name in candidate}
    missing = [ref[key] for key in sorted(ref.keys() - cand.keys())]
    extra = [cand[key] for key in sorted(cand.keys() - ref.keys())]
    return len(ref) - len(missing), len(ref), missing, extra


def render(
    playwright: dict[str, Any],
    rustwright: dict[str, Any],
    cases: list[str],
    exercised: set[tuple[str, str]],
    ambiguous_calls: int,
) -> tuple[str, dict[str, tuple[int, int, int, int]]]:
    sync_counts = all_counts(playwright["sync"], rustwright["sync_available"], exercised)
    async_counts = all_counts(playwright["async"], rustwright["async_available"], set())
    major = {
        name: class_counts(playwright["sync"], rustwright["sync_available"], exercised, name)
        for name in MAJOR_CLASSES
    }
    sync_exports = export_summary(playwright["sync_exports"], rustwright["sync_exports"])
    async_exports = export_summary(playwright["async_exports"], rustwright["async_exports"])
    limitations = parse_limitations()
    node_bridged, node_gaps = parse_node_subset()

    lines = [
        "<!-- Generated by tools/generate_parity_map.py; do not edit by hand. -->",
        "# Rustwright API parity map",
        "",
        f"Reference: Playwright Python **{playwright['version']}**. Candidate: Rustwright **{rustwright['version']}**.",
        "",
        "This is an API-presence and shared-suite exercise map, not a claim of complete behavioral parity.",
        "",
        "## Summary",
        "",
        f"Rustwright provides **{sync_counts[0]} of {sync_counts[3]}** reference sync-API methods; "
        f"**{sync_counts[1]}** are exercised by the **{len(cases)}-case** shared parity registry and "
        f"**{sync_counts[2]}** are missing. Its async API provides **{async_counts[0]} of {async_counts[3]}** "
        "reference async methods. The parity registry is sync-only, so it does not exercise async wrappers.",
        "",
        "| Surface | Present | Exercised by shared parity suite | Missing | Reference total |",
        "| --- | ---: | ---: | ---: | ---: |",
        f"| Sync methods | {sync_counts[0]} | {sync_counts[1]} | {sync_counts[2]} | {sync_counts[3]} |",
        f"| Async methods | {async_counts[0]} | not measured | {async_counts[2]} | {async_counts[3]} |",
        "",
        "### Major classes",
        "",
        "| Class | Sync present | Sync exercised | Sync missing | Reference sync methods | "
        "Async present | Async missing |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name in MAJOR_CLASSES:
        present, tested, missing, total = major[name]
        ap, _, am, _ = class_counts(playwright["async"], rustwright["async_available"], set(), name)
        lines.append(f"| {name} | {present} | {tested} | {missing} | {total} | {ap} | {am} |")

    lines.extend(
        [
            "",
            "Legend: ✅ present and statically resolved in a registered parity case; 🟡 present but not "
            "resolved in that sync suite; ❌ missing from Rustwright.",
            "",
            "## API groups",
            "",
        ]
    )
    covered_classes: set[str] = set()
    for title, classes in GROUPS:
        existing = [name for name in classes if name in playwright["sync"] or name in playwright["async"]]
        if not existing:
            continue
        covered_classes.update(existing)
        lines.extend(
            [
                f"### {title}",
                "",
                "| Reference member | Kind | Sync status | Async status |",
                "| --- | --- | --- | --- |",
            ]
        )
        for class_name in existing:
            members = playwright["sync"].get(class_name, playwright["async"].get(class_name, {}))
            for member_name, metadata in sorted(members.items(), key=lambda item: snake_name(item[0])):
                sync_present = availability(rustwright["sync_available"], class_name, member_name)
                async_present = availability(rustwright["async_available"], class_name, member_name)
                was_exercised = (class_name, snake_name(member_name)) in exercised
                lines.append(
                    f"| `{class_name}.{member_name}` | {metadata['kind']} | "
                    f"{status(sync_present, was_exercised)} | {status(async_present, False)} |"
                )
        lines.append("")

    ungrouped = sorted((set(playwright["sync"]) | set(playwright["async"])) - covered_classes)
    if ungrouped:
        lines.extend(["### Other discovered API classes", ""])
        lines.append(
            "The following generated classes were discovered but are not assigned to a semantic group: "
            + ", ".join(f"`{name}`" for name in ungrouped)
            + "."
        )
        lines.append("")

    lines.extend(
        [
            "## Python exports",
            "",
            "The package-root inventory below comes from `rustwright.__all__`; sync and async comparison "
            "counts come from each implementation module's `__all__`.",
            "",
            "| Module | Matching Playwright exports | Playwright exports | Missing | Rustwright-only/extra |",
            "| --- | ---: | ---: | ---: | ---: |",
            f"| `sync_api` | {sync_exports[0]} | {sync_exports[1]} | {len(sync_exports[2])} | {len(sync_exports[3])} |",
            f"| `async_api` | {async_exports[0]} | {async_exports[1]} | {len(async_exports[2])} | "
            f"{len(async_exports[3])} |",
            "",
            f"Missing sync exports: {', '.join(f'`{name}`' for name in sync_exports[2]) or 'none'}.",
            "",
            f"Missing async exports: {', '.join(f'`{name}`' for name in async_exports[2]) or 'none'}.",
            "",
            "### `rustwright` package-root exports",
            "",
            "| Export | Also in `sync_api` | Also in `async_api` |",
            "| --- | --- | --- |",
        ]
    )
    sync_export_set = set(rustwright["sync_exports"])
    async_export_set = set(rustwright["async_exports"])
    for name in rustwright["root_exports"]:
        lines.append(
            f"| `{name}` | {'yes' if name in sync_export_set else 'no'} | "
            f"{'yes' if name in async_export_set else 'no'} |"
        )

    lines.extend(["", "## Documented limitations", ""])
    lines.extend(f"- {item}" for item in limitations)

    lines.extend(
        [
            "",
            "## Node.js subset",
            "",
            "This table is parsed from `node/README.md`; it does not infer capabilities from the native binding.",
            "",
            "| Surface | Status |",
            "| --- | --- |",
        ]
    )
    lines.extend(f"| `{name}` | ✅ bridged |" for name in node_bridged)
    lines.extend(f"| {name} | ❌ not yet bridged |" for name in node_gaps)

    source_digest = hashlib.sha256(
        b"\0".join(
            path.read_bytes() for path in (CASES_PATH, SUITE_PATH, LIMITATIONS_PATH, NODE_README_PATH)
        )
    ).hexdigest()[:16]
    lines.extend(
        [
            "",
            "## Methodology",
            "",
            f"- The reference surface was introspected from real Playwright Python {playwright['version']}. "
            f"When Playwright is unavailable to the invoking interpreter, the generator uses the ignored "
            f"`.venv-parity` environment and installs the pinned fallback `playwright=={PINNED_PLAYWRIGHT}`. "
            "It never installs into `.venv`.",
            "- API classes are discovered from `sync_api`, `async_api`, and their generated modules. Public "
            "methods and properties declared on those classes are compared with matching Rustwright classes. "
            "Inherited event-emitter implementation helpers are excluded. Names are converted to snake_case "
            "before exact matching; normalization collisions fail generation.",
            "- Method totals count methods, not properties. Properties remain in the detailed tables because "
            "they are part of the usable API and because their return annotations make chained receiver "
            "types resolvable.",
            f"- The exercised state starts only from the {len(cases)} functions actually present in the `CASES` "
            "registry. The generator verifies that `tests/test_playwright_parity_cases.py` imports that registry, "
            "runs `_run_parity` for a `playwright` parameter, and checks `len(CASES)`.",
            "- Exercise detection is conservative static analysis. It propagates `Page` and `Playwright` case "
            "parameters through assignments, reference return annotations, property chains, collection indexing, "
            "local helper calls, callback annotations, and literal event names. A member becomes green only when "
            "its receiver resolves to one reference class. Dynamic `getattr`, aliases returned through untyped "
            "helpers, and callbacks with ambiguous receiver types remain yellow even if a case reaches them "
            "at runtime.",
            f"- {ambiguous_calls} call site(s) had more than one plausible reference receiver class and were "
            "left uncredited rather than guessed.",
            "- A green mark means the shared suite invokes or reads the member while running the same registered "
            "case against real Playwright and Rustwright. It does not prove all options, errors, events, browser "
            "engines, or edge cases match.",
            "- The limitations and Node.js sections are parsed from `LIMITATIONS.md` and `node/README.md` on each run.",
            f"- Source digest (case registry, parity test, limitations, Node README): `{source_digest}`.",
            "",
        ]
    )
    return "\n".join(lines), major


def print_totals(
    playwright: dict[str, Any],
    rustwright: dict[str, Any],
    exercised: set[tuple[str, str]],
    cases: list[str],
) -> None:
    present, tested, missing, total = all_counts(playwright["sync"], rustwright["sync_available"], exercised)
    ap, _, am, at = all_counts(playwright["async"], rustwright["async_available"], set())
    print(f"Playwright {playwright['version']}; parity cases: {len(cases)}")
    print(f"sync methods: {present}/{total} present; {tested} exercised; {missing} missing")
    print(f"async methods: {ap}/{at} present; exercise not measured; {am} missing")
    for name in MAJOR_CLASSES:
        p, e, m, t = class_counts(playwright["sync"], rustwright["sync_available"], exercised, name)
        print(f"{name}: {p}/{t} present; {e} exercised; {m} missing")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="write docs/PARITY.md")
    mode.add_argument("--check", action="store_true", help="fail if docs/PARITY.md is stale")
    args = parser.parse_args()

    validate_suite_uses_real_playwright_registry()
    rust_python = rustwright_interpreter()
    reference_python = reference_interpreter()
    playwright = introspect(reference_python, "playwright")
    rustwright = introspect(rust_python, "rustwright")
    cases = registry_names(rust_python)
    case_tree = ast.parse(CASES_PATH.read_text(encoding="utf-8"), filename=str(CASES_PATH))
    analyzer = ExerciseAnalyzer(case_tree, cases, playwright["sync"])
    exercised, ambiguous_calls = analyzer.analyze()
    document, _ = render(playwright, rustwright, cases, exercised, ambiguous_calls)
    print_totals(playwright, rustwright, exercised, cases)

    if args.write:
        DOC_PATH.write_text(document, encoding="utf-8")
        print(f"wrote {DOC_PATH.relative_to(ROOT)}")
        return 0
    current = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else ""
    if current != document:
        print(f"stale: {DOC_PATH.relative_to(ROOT)}", file=sys.stderr)
        return 1
    print(f"current: {DOC_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
