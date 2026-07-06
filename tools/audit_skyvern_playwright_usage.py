#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import importlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


DEFAULT_SOURCE = Path("~/Development/Skyvern-cloud").expanduser()
EXCLUDED_DIR_NAMES = {
    ".git",
    ".claude",
    ".codex",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "artifacts",
    "downloads",
    "logs",
    "node_modules",
    "prompt_evaluation",
    "traces",
    "user_data_dir",
}
PYTHON_SUFFIXES = {".py"}
TEXT_SUFFIXES = {".md", ".mdx", ".sh", ".toml", ".yaml", ".yml"}
PLAYWRIGHT_METHOD_NAMES = {
    "accessibility",
    "add_cookies",
    "add_init_script",
    "bounding_box",
    "check",
    "clear_cookies",
    "click",
    "close",
    "connect_over_cdp",
    "content",
    "cookies",
    "dblclick",
    "detach",
    "dispatch_event",
    "drag_to",
    "evaluate",
    "evaluate_handle",
    "expect_console_message",
    "expect_download",
    "expect_event",
    "expect_file_chooser",
    "expect_popup",
    "expose_binding",
    "fill",
    "frame",
    "frame_locator",
    "frames",
    "get_attribute",
    "get_by_alt_text",
    "get_by_label",
    "get_by_placeholder",
    "get_by_role",
    "get_by_test_id",
    "get_by_text",
    "goto",
    "hover",
    "inner_html",
    "inner_text",
    "input_value",
    "is_checked",
    "is_enabled",
    "is_visible",
    "launch",
    "launch_persistent_context",
    "locator",
    "new_browser_cdp_session",
    "new_cdp_session",
    "new_context",
    "new_page",
    "on",
    "once",
    "pdf",
    "press",
    "query_selector",
    "query_selector_all",
    "reload",
    "remove_listener",
    "route",
    "screenshot",
    "send",
    "select_option",
    "set_content",
    "set_extra_http_headers",
    "set_geolocation",
    "set_input_files",
    "set_viewport_size",
    "storage_state",
    "tap",
    "text_content",
    "title",
    "type",
    "uncheck",
    "unroute",
    "video",
    "wait_for_event",
    "wait_for_function",
    "wait_for_load_state",
    "wait_for_selector",
    "wait_for_url",
}
METHOD_SURFACE_CLASSES = [
    "APIRequest",
    "APIRequestContext",
    "APIResponse",
    "Browser",
    "BrowserContext",
    "BrowserType",
    "CDPSession",
    "Clock",
    "ConsoleMessage",
    "Dialog",
    "Download",
    "ElementHandle",
    "FileChooser",
    "Frame",
    "FrameLocator",
    "JSHandle",
    "Keyboard",
    "Locator",
    "Mouse",
    "Page",
    "Playwright",
    "Request",
    "Response",
    "Route",
    "Touchscreen",
    "Tracing",
    "Video",
    "Worker",
]
METHOD_RETURN_TYPES = {
    "connect_over_cdp": "Browser",
    "content_frame": "Frame",
    "element_handle": "ElementHandle",
    "frame": "Frame",
    "frame_locator": "FrameLocator",
    "launch": "Browser",
    "launch_persistent_context": "BrowserContext",
    "main_frame": "Frame",
    "new_browser_cdp_session": "CDPSession",
    "new_cdp_session": "CDPSession",
    "new_context": "BrowserContext",
    "new_page": "Page",
    "query_selector": "ElementHandle",
}
PROPERTY_RETURN_TYPES = {
    ("BrowserContext", "request"): "APIRequestContext",
    ("Page", "context"): "BrowserContext",
    ("Page", "keyboard"): "Keyboard",
    ("Page", "main_frame"): "Frame",
    ("Page", "mouse"): "Mouse",
    ("Page", "touchscreen"): "Touchscreen",
    ("Playwright", "chromium"): "BrowserType",
    ("Playwright", "firefox"): "BrowserType",
    ("Playwright", "request"): "APIRequest",
    ("Playwright", "webkit"): "BrowserType",
    ("Response", "request"): "Request",
}
LOCATOR_RETURN_METHODS = {
    "filter",
    "first",
    "get_by_alt_text",
    "get_by_label",
    "get_by_placeholder",
    "get_by_role",
    "get_by_test_id",
    "get_by_text",
    "get_by_title",
    "last",
    "locator",
    "nth",
    "or_",
}
AREA_RULES = {
    "async_api_parity": {
        "imports": {"playwright.async_api"},
        "symbols": {"async_playwright", "Page", "BrowserContext", "Frame", "Locator", "ElementHandle"},
        "methods": {"on", "once", "wait_for_event", "expect_event", "close"},
        "cdp": set(),
        "text": set(),
    },
    "browser_launch_connect": {
        "imports": set(),
        "symbols": {"Playwright", "Browser", "BrowserContext"},
        "methods": {"launch", "launch_persistent_context", "connect_over_cdp", "new_context", "new_page"},
        "cdp": {"Browser.setDownloadBehavior", "Target.setDiscoverTargets"},
        "text": {"playwright install", "playwright install chrome", "playwright install msedge"},
    },
    "remote_cdp_and_fetch_downloads": {
        "imports": set(),
        "symbols": {"CDPSession"},
        "methods": {"new_cdp_session", "new_browser_cdp_session", "connect_over_cdp"},
        "cdp": {
            "Browser.setDownloadBehavior",
            "Fetch.authRequired",
            "Fetch.continueRequest",
            "Fetch.continueResponse",
            "Fetch.continueWithAuth",
            "Fetch.disable",
            "Fetch.enable",
            "Fetch.fulfillRequest",
            "Fetch.getResponseBody",
            "Fetch.requestPaused",
            "Fetch.takeResponseBodyAsStream",
            "IO.close",
            "IO.read",
        },
        "text": set(),
    },
    "actions_and_locators": {
        "imports": set(),
        "symbols": {"Frame", "FrameLocator", "Locator", "Page"},
        "methods": {
            "bounding_box",
            "check",
            "click",
            "dblclick",
            "fill",
            "get_by_label",
            "get_by_placeholder",
            "get_by_role",
            "get_by_text",
            "hover",
            "locator",
            "press",
            "scroll_into_view_if_needed",
            "select_option",
            "set_input_files",
            "type",
            "uncheck",
        },
        "cdp": {"Input.dispatchKeyEvent", "Input.dispatchMouseEvent"},
        "text": set(),
    },
    "screenshots_pdf_artifacts": {
        "imports": set(),
        "symbols": {"Download", "FileChooser", "Page"},
        "methods": {"expect_download", "expect_file_chooser", "pdf", "screenshot", "set_input_files", "video"},
        "cdp": {"Page.screencastFrame", "Page.startScreencast", "Page.stopScreencast"},
        "text": set(),
    },
    "routing_and_api_requests": {
        "imports": set(),
        "symbols": {"Request", "Response", "Route"},
        "methods": {"route", "unroute"},
        "cdp": {"Fetch.continueRequest", "Fetch.continueResponse", "Fetch.fulfillRequest"},
        "text": set(),
    },
    "storage_context_options": {
        "imports": set(),
        "symbols": {"BrowserContext"},
        "methods": {
            "add_cookies",
            "clear_cookies",
            "cookies",
            "grant_permissions",
            "set_extra_http_headers",
            "set_geolocation",
            "storage_state",
        },
        "cdp": {"Storage.clearDataForOrigin"},
        "text": set(),
    },
    "patchright_cloakbrowser": {
        "imports": {"cloakbrowser", "patchright"},
        "symbols": set(),
        "methods": set(),
        "cdp": set(),
        "text": {"CLOAKBROWSER_BACKEND", "patchright install", "cloakbrowser[patchright"},
    },
}


def is_excluded(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    return any(part in EXCLUDED_DIR_NAMES for part in relative.parts)


def iter_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.is_dir() or is_excluded(path, root):
            continue
        if (
            path.suffix in PYTHON_SUFFIXES
            or path.suffix in TEXT_SUFFIXES
            or path.name == "uv.lock"
            or "Dockerfile" in path.name
        ):
            yield path


def rel(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


def attr_chain(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = attr_chain(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    if isinstance(node, ast.Call):
        return attr_chain(node.func)
    if isinstance(node, ast.Subscript):
        return attr_chain(node.value)
    return None


def string_arg(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def add_evidence(bucket: dict[str, set[str]], key: str, path: str, line: int | None = None) -> None:
    location = f"{path}:{line}" if line else path
    bucket[key].add(location)


def annotation_names(node: ast.AST | None) -> list[str]:
    if node is None:
        return []
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        chain = attr_chain(node)
        return [chain.rsplit(".", 1)[-1]] if chain else []
    if isinstance(node, ast.Subscript):
        return annotation_names(node.value) + annotation_names(node.slice)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return annotation_names(node.left) + annotation_names(node.right)
    if isinstance(node, (ast.Tuple, ast.List)):
        names: list[str] = []
        for item in node.elts:
            names.extend(annotation_names(item))
        return names
    return []


def imported_type_for_annotation(node: ast.AST | None, import_aliases: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    for name in annotation_names(node):
        imported = import_aliases.get(name)
        if imported is not None and imported[1] in METHOD_SURFACE_CLASSES:
            return imported
        if name in METHOD_SURFACE_CLASSES:
            return ("playwright.async_api", name)
    return None


def infer_return_type(receiver_type: tuple[str, str], method: str) -> tuple[str, str] | None:
    module_name, class_name = receiver_type
    if method in LOCATOR_RETURN_METHODS:
        return (module_name, "Locator")
    result = METHOD_RETURN_TYPES.get(method)
    if result is not None:
        return (module_name, result)
    if class_name == "ElementHandle" and method == "owner_frame":
        return (module_name, "Frame")
    return None


def typed_method_key(receiver_type: tuple[str, str], method: str) -> str:
    module_name, class_name = receiver_type
    return f"{module_name}.{class_name}.{method}"


def call_from_value(node: ast.AST) -> ast.Call | None:
    if isinstance(node, ast.Call):
        return node
    if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
        return node.value
    return None


def analyze_typed_method_calls(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    import_aliases: dict[str, tuple[str, str]],
    *,
    self_type: tuple[str, str] | None = None,
) -> Counter[str]:
    typed_calls: Counter[str] = Counter()
    scope_types: dict[str, tuple[str, str]] = {}
    args = list(function.args.posonlyargs) + list(function.args.args) + list(function.args.kwonlyargs)
    if self_type is not None and args:
        scope_types[args[0].arg] = self_type
    for arg in args:
        arg_type = imported_type_for_annotation(arg.annotation, import_aliases)
        if arg_type is not None:
            scope_types[arg.arg] = arg_type

    def receiver_type(node: ast.AST) -> tuple[str, str] | None:
        chain = attr_chain(node)
        if not chain:
            return None
        parts = chain.split(".")
        current = scope_types.get(parts[0])
        if current is None:
            return None
        for attr in parts[1:]:
            result = PROPERTY_RETURN_TYPES.get((current[1], attr))
            if result is None:
                return current
            current = (current[0], result)
        return current

    for node in ast.walk(function):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            annotated = imported_type_for_annotation(node.annotation, import_aliases)
            if annotated is not None:
                scope_types[node.target.id] = annotated
        elif isinstance(node, ast.Assign) and (assigned_call := call_from_value(node.value)) is not None and isinstance(assigned_call.func, ast.Attribute):
            source_type = receiver_type(assigned_call.func.value)
            if source_type is None:
                continue
            inferred = infer_return_type(source_type, assigned_call.func.attr)
            if inferred is None:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    scope_types[target.id] = inferred
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            method = node.func.attr
            if method not in PLAYWRIGHT_METHOD_NAMES:
                continue
            call_receiver_type = receiver_type(node.func.value)
            if call_receiver_type is not None:
                typed_calls[typed_method_key(call_receiver_type, method)] += 1
    return typed_calls


def scan_python(path: Path, root: Path) -> dict[str, Any]:
    relative = rel(path, root)
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"path": relative, "parse_error": "unicode_decode_error"}
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return {"path": relative, "parse_error": f"{exc.__class__.__name__}: {exc}"}

    imports: list[dict[str, Any]] = []
    imported_symbols: Counter[str] = Counter()
    method_calls: Counter[str] = Counter()
    typed_method_calls: Counter[str] = Counter()
    cdp_methods: Counter[str] = Counter()
    class_bases: list[dict[str, Any]] = []
    import_aliases: dict[str, tuple[str, str]] = {}
    class_method_ids: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if (
                node.module in {"playwright", "cloakbrowser"}
                or node.module.startswith(("playwright.", "patchright.", "cloakbrowser."))
            ):
                symbols = [alias.name for alias in node.names]
                imports.append({"module": node.module, "symbols": symbols, "line": node.lineno})
                imported_symbols.update(symbols)
                for alias in node.names:
                    if alias.name != "*":
                        import_aliases[alias.asname or alias.name] = (node.module, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"playwright", "cloakbrowser"} or alias.name.startswith(
                    ("playwright.", "patchright", "cloakbrowser.")
                ):
                    imports.append({"module": alias.name, "symbols": [], "line": node.lineno})
        elif isinstance(node, ast.ClassDef):
            bases = [chain for base in node.bases if (chain := attr_chain(base))]
            if any(base.rsplit(".", 1)[-1] in imported_symbols for base in bases):
                class_bases.append({"class": node.name, "bases": bases, "line": node.lineno})
            self_type = None
            for base in node.bases:
                self_type = imported_type_for_annotation(base, import_aliases)
                if self_type is not None:
                    break
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    class_method_ids.add(id(item))
                    typed_method_calls.update(analyze_typed_method_calls(item, import_aliases, self_type=self_type))
        elif isinstance(node, ast.Call):
            chain = attr_chain(node.func)
            if chain and isinstance(node.func, ast.Attribute):
                method = chain.rsplit(".", 1)[-1]
                if method in PLAYWRIGHT_METHOD_NAMES:
                    method_calls[method] += 1
            first_arg = string_arg(node.args[0]) if node.args else None
            call_method = chain.rsplit(".", 1)[-1] if chain else ""
            if (
                first_arg
                and call_method in {"on", "once", "send"}
                and re.match(r"^(?:Browser|DOM|Emulation|Fetch|IO|Input|Network|Page|Runtime|Storage|Target)\.", first_arg)
            ):
                cdp_methods[first_arg] += 1

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and id(node) not in class_method_ids:
            typed_method_calls.update(analyze_typed_method_calls(node, import_aliases))

    return {
        "path": relative,
        "imports": imports,
        "imported_symbols": dict(sorted(imported_symbols.items())),
        "method_calls": dict(sorted(method_calls.items())),
        "typed_method_calls": dict(sorted(typed_method_calls.items())),
        "cdp_methods": dict(sorted(cdp_methods.items())),
        "class_bases": class_bases,
    }


def scan_text(path: Path, root: Path) -> dict[str, Any]:
    relative = rel(path, root)
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError as exc:
        return {"path": relative, "read_error": str(exc)}
    matches = []
    for index, line in enumerate(lines, start=1):
        if re.search(r"playwright|patchright|cloakbrowser|CLOAKBROWSER_BACKEND", line, re.IGNORECASE):
            matches.append({"line": index, "text": line.strip()})
    return {"path": relative, "matches": matches}


def top_counter(counter: Counter[str], limit: int) -> list[dict[str, Any]]:
    return [{"name": key, "count": value} for key, value in counter.most_common(limit)]


def build_area_findings(
    root: Path,
    python_results: list[dict[str, Any]],
    text_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    evidence: dict[str, dict[str, set[str]]] = {
        area: defaultdict(set) for area in AREA_RULES
    }
    for result in python_results:
        path = result["path"]
        for item in result.get("imports", []):
            module = item["module"]
            for area, rules in AREA_RULES.items():
                if any(module == target or module.startswith(f"{target}.") for target in rules["imports"]):
                    add_evidence(evidence[area], "imports", path, item.get("line"))
        for symbol in result.get("imported_symbols", {}):
            for area, rules in AREA_RULES.items():
                if symbol in rules["symbols"]:
                    add_evidence(evidence[area], f"symbol:{symbol}", path)
        for method, count in result.get("method_calls", {}).items():
            for area, rules in AREA_RULES.items():
                if method in rules["methods"]:
                    evidence[area][f"method:{method}"].add(f"{path} ({count})")
        for method, count in result.get("cdp_methods", {}).items():
            for area, rules in AREA_RULES.items():
                if method in rules["cdp"]:
                    evidence[area][f"cdp:{method}"].add(f"{path} ({count})")
    for result in text_results:
        path = result["path"]
        for match in result.get("matches", []):
            text = match["text"]
            for area, rules in AREA_RULES.items():
                for needle in rules["text"]:
                    if needle.lower() in text.lower():
                        add_evidence(evidence[area], f"text:{needle}", path, match.get("line"))

    findings = []
    for area, keys in evidence.items():
        flattened = []
        files = set()
        for key, locations in keys.items():
            for location in sorted(locations):
                flattened.append({"signal": key, "location": location})
                files.add(location.split(":", 1)[0].split(" (", 1)[0])
        findings.append(
            {
                "id": area,
                "evidence_count": len(flattened),
                "file_count": len(files),
                "sample_evidence": flattened[:20],
            }
        )
    return sorted(findings, key=lambda item: (-item["evidence_count"], item["id"]))


def build_alias_symbol_coverage(imported_symbols_by_module: dict[str, Counter[str]]) -> dict[str, Any]:
    modules: dict[str, Any] = {}
    missing: list[dict[str, str]] = []
    import_errors: list[dict[str, str]] = []

    for module_name, counter in sorted(imported_symbols_by_module.items()):
        symbols = sorted(symbol for symbol in counter if symbol != "*")
        entry: dict[str, Any] = {
            "imported_symbol_count": sum(counter.values()),
            "checked_symbols": symbols,
            "wildcard_import_count": counter.get("*", 0),
            "missing_symbols": [],
        }
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # pragma: no cover - exercised by CLI integration.
            entry["status"] = "module_import_error"
            entry["error"] = f"{type(exc).__name__}: {exc}"
            import_errors.append({"module": module_name, "error": entry["error"]})
            modules[module_name] = entry
            continue

        exports = getattr(module, "__all__", None)
        if isinstance(exports, (list, tuple, set, frozenset)):
            entry["module_all_count"] = len(exports)
        module_missing = [symbol for symbol in symbols if not hasattr(module, symbol)]
        entry["missing_symbols"] = module_missing
        entry["status"] = "missing_symbols" if module_missing else "ok"
        for symbol in module_missing:
            missing.append({"module": module_name, "symbol": symbol})
        modules[module_name] = entry

    status = "ok"
    if import_errors:
        status = "module_import_error"
    elif missing:
        status = "missing_symbols"
    return {
        "status": status,
        "missing_total": len(missing),
        "import_error_total": len(import_errors),
        "missing": missing,
        "import_errors": import_errors,
        "modules": modules,
    }


def build_method_name_coverage(method_calls: Counter[str]) -> dict[str, Any]:
    modules: dict[str, Any] = {}
    import_errors: list[dict[str, str]] = []
    for module_name in ("playwright.sync_api", "playwright.async_api"):
        try:
            modules[module_name] = importlib.import_module(module_name)
        except Exception as exc:  # pragma: no cover - exercised by CLI integration.
            import_errors.append({"module": module_name, "error": f"{type(exc).__name__}: {exc}"})

    methods: dict[str, Any] = {}
    missing = []
    for method, count in sorted(method_calls.items()):
        owners = []
        for module_name, module in modules.items():
            for class_name in METHOD_SURFACE_CLASSES:
                owner = getattr(module, class_name, None)
                if owner is not None and hasattr(owner, method):
                    owners.append(f"{module_name}.{class_name}")
        entry = {
            "call_count": count,
            "owner_count": len(owners),
            "owners": owners[:20],
            "status": "ok" if owners else "missing_global_method_name",
        }
        if not owners:
            missing.append({"method": method, "call_count": count})
        methods[method] = entry

    status = "ok"
    if import_errors:
        status = "module_import_error"
    elif missing:
        status = "missing_global_method_name"
    return {
        "status": status,
        "missing_total": len(missing),
        "import_error_total": len(import_errors),
        "missing": missing,
        "import_errors": import_errors,
        "method_count": len(methods),
        "method_scope": "Global method-name coverage across common Playwright classes; this catches absent methods but does not prove receiver-specific behavioral parity.",
        "classes_checked": METHOD_SURFACE_CLASSES,
        "methods": methods,
    }


def split_typed_method_key(key: str) -> tuple[str, str, str]:
    module_prefix = None
    for prefix in ("playwright.sync_api.", "playwright.async_api.", "patchright.sync_api.", "patchright.async_api."):
        if key.startswith(prefix):
            module_prefix = prefix[:-1]
            remainder = key[len(prefix) :]
            class_name, method = remainder.split(".", 1)
            return module_prefix, class_name, method
    module_name, class_name, method = key.rsplit(".", 2)
    return module_name, class_name, method


def build_typed_method_coverage(typed_method_calls: Counter[str]) -> dict[str, Any]:
    modules: dict[str, Any] = {}
    missing = []
    import_errors: list[dict[str, str]] = []
    calls: dict[str, Any] = {}
    for key, count in sorted(typed_method_calls.items()):
        module_name, class_name, method = split_typed_method_key(key)
        module = modules.get(module_name)
        if module is None and module_name not in modules:
            try:
                module = importlib.import_module(module_name)
                modules[module_name] = module
            except Exception as exc:  # pragma: no cover - exercised by CLI integration.
                error = f"{type(exc).__name__}: {exc}"
                import_errors.append({"module": module_name, "error": error})
                calls[key] = {
                    "call_count": count,
                    "module": module_name,
                    "class": class_name,
                    "method": method,
                    "status": "module_import_error",
                    "error": error,
                }
                continue
        owner = getattr(module, class_name, None)
        exists = owner is not None and hasattr(owner, method)
        status = "ok" if exists else "missing_on_receiver"
        entry = {
            "call_count": count,
            "module": module_name,
            "class": class_name,
            "method": method,
            "status": status,
        }
        if not exists:
            missing.append({"module": module_name, "class": class_name, "method": method, "call_count": count})
        calls[key] = entry

    status = "ok"
    if import_errors:
        status = "module_import_error"
    elif missing:
        status = "missing_on_receiver"
    return {
        "status": status,
        "missing_total": len(missing),
        "import_error_total": len(import_errors),
        "typed_call_count": sum(typed_method_calls.values()),
        "receiver_method_count": len(typed_method_calls),
        "coverage_scope": "Receiver-typed method coverage inferred from Skyvern annotations, subclass bases, and simple Playwright return values; this is stronger than global method-name coverage but still not full behavioral parity.",
        "missing": missing,
        "import_errors": import_errors,
        "calls": calls,
    }


def build_report(source: Path) -> dict[str, Any]:
    root = source.resolve()
    files = list(iter_files(root))
    python_files = [path for path in files if path.suffix in PYTHON_SUFFIXES]
    text_files = [
        path for path in files if path.suffix in TEXT_SUFFIXES or path.name == "uv.lock" or "Dockerfile" in path.name
    ]
    python_results = [scan_python(path, root) for path in python_files]
    text_results = [scan_text(path, root) for path in text_files]

    import_modules: Counter[str] = Counter()
    imported_symbols: Counter[str] = Counter()
    imported_symbols_by_module: dict[str, Counter[str]] = defaultdict(Counter)
    method_calls: Counter[str] = Counter()
    typed_method_calls: Counter[str] = Counter()
    cdp_methods: Counter[str] = Counter()
    parse_errors = []
    class_bases = []
    for result in python_results:
        if result.get("parse_error"):
            parse_errors.append({"path": result["path"], "error": result["parse_error"]})
            continue
        for item in result.get("imports", []):
            import_modules[item["module"]] += 1
            imported_symbols_by_module[item["module"]].update(item.get("symbols", []))
        imported_symbols.update(result.get("imported_symbols", {}))
        method_calls.update(result.get("method_calls", {}))
        typed_method_calls.update(result.get("typed_method_calls", {}))
        cdp_methods.update(result.get("cdp_methods", {}))
        for item in result.get("class_bases", []):
            class_bases.append({"path": result["path"], **item})

    dependency_specs = []
    docker_browser_installs = []
    text_match_files = []
    for result in text_results:
        matches = result.get("matches", [])
        if matches:
            text_match_files.append({"path": result["path"], "matches": matches[:20]})
        for match in matches:
            text = match["text"]
            if re.search(r"playwright|patchright|cloakbrowser", text, re.IGNORECASE):
                if result["path"].endswith("pyproject.toml") or "uv.lock" in result["path"]:
                    dependency_specs.append({"path": result["path"], **match})
                if "Dockerfile" in result["path"] and re.search(r"install|CLOAKBROWSER_BACKEND", text, re.IGNORECASE):
                    docker_browser_installs.append({"path": result["path"], **match})

    return {
        "schema_version": 1,
        "source_path": str(root),
        "scan_scope": {
            "excluded_directory_names": sorted(EXCLUDED_DIR_NAMES),
            "python_files_scanned": len(python_files),
            "text_files_scanned": len(text_files),
        },
        "summary": {
            "playwright_import_modules": top_counter(import_modules, 25),
            "imported_async_api_symbols": top_counter(imported_symbols, 40),
            "top_playwright_method_names": top_counter(method_calls, 60),
            "top_cdp_methods": top_counter(cdp_methods, 60),
            "class_bases": class_bases[:40],
            "parse_errors": parse_errors[:20],
        },
        "rustwright_alias_symbol_coverage": build_alias_symbol_coverage(imported_symbols_by_module),
        "rustwright_method_name_coverage": build_method_name_coverage(method_calls),
        "rustwright_typed_method_coverage": build_typed_method_coverage(typed_method_calls),
        "dependency_specs": dependency_specs[:40],
        "docker_browser_installs": docker_browser_installs[:60],
        "requirement_area_findings": build_area_findings(root, python_results, text_results),
        "text_match_files": text_match_files[:80],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Skyvern Cloud Playwright/Patchright usage.")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="Skyvern-cloud checkout path.")
    parser.add_argument("--output", help="Optional path to write JSON report.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    args = parser.parse_args()

    source = Path(args.source).expanduser()
    if not source.is_dir():
        raise SystemExit(f"source directory not found: {source}")
    report = build_report(source)
    text = json.dumps(report, indent=2 if args.pretty else None, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
