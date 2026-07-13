#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUSTWRIGHT_PYTHON = ROOT / "python"
if str(RUSTWRIGHT_PYTHON) not in sys.path:
    sys.path.insert(0, str(RUSTWRIGHT_PYTHON))

import audit_skyvern_playwright_usage  # noqa: E402

DEFAULT_SOURCE = Path("~/Development/Skyvern-cloud").expanduser()
DEFAULT_AUDIT_OUTPUT = ROOT / ".benchmark-data" / "skyvern-replacement-smoke-audit.json"
DEFAULT_SKYVERN_MODULES = [
    "cloud.webeye.stealth_chromium_launcher",
    "cloud.webeye.utils.browser",
    "cloud.webeye.filter",
    "cloud.webeye.favicon_blocker",
    "cloud.webeye.utils.captcha",
    "cloud.webeye.setup.action_complete",
    "cloud.webeye.setup.action_upload_file",
    "cloud.webeye.setup.action_input",
    "skyvern.services.script_reviewer_v3.types",
]
REQUIRED_ALIAS_SYMBOLS = {
    "playwright.async_api": [
        "Error",
        "TimeoutError",
        "async_playwright",
        "backend_marker",
        "Browser",
        "BrowserContext",
        "Frame",
        "Locator",
        "Page",
        "Route",
    ],
    "playwright.sync_api": [
        "Error",
        "TimeoutError",
        "sync_playwright",
        "backend_marker",
        "Browser",
        "BrowserContext",
        "Frame",
        "Locator",
        "Page",
        "Route",
    ],
    "playwright._impl._errors": ["Error", "TargetClosedError", "TimeoutError"],
    "patchright.async_api": ["Error", "TimeoutError", "async_playwright", "backend_marker", "Page"],
    "patchright.sync_api": ["Error", "TimeoutError", "sync_playwright", "backend_marker", "Page"],
    "patchright._impl._errors": ["Error", "TargetClosedError", "TimeoutError"],
    "cloakbrowser": [
        "ensure_binary",
        "launch",
        "launch_async",
        "launch_persistent_context",
        "launch_persistent_context_async",
    ],
}
BACKEND_MARKER_MODULES = [
    "playwright.async_api",
    "playwright.sync_api",
    "patchright.async_api",
    "patchright.sync_api",
]
EXPECTED_BACKEND_MARKER = {
    "implementation": "rustwright",
    "package": "rustwright",
    "replacement_backend": True,
    "runtime": "rust-pyo3-extension",
    "runtime_module": "rustwright._rustwright",
    "transport": "raw-cdp",
    "cdp_first": True,
    "python_playwright_driver": False,
    "playwright_driver": "none",
}


def default_python_for_source(source: Path) -> Path:
    candidate = source / ".venv" / "bin" / "python"
    if candidate.exists():
        return candidate
    return Path(sys.executable)


def append_pythonpath(env: dict[str, str], paths: list[Path]) -> None:
    existing = env.get("PYTHONPATH", "")
    values = [str(path) for path in paths]
    if existing:
        values.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(values)


def audit_summary(report: dict[str, Any]) -> dict[str, Any]:
    alias = report.get("rustwright_alias_symbol_coverage") or {}
    method = report.get("rustwright_method_name_coverage") or {}
    typed = report.get("rustwright_typed_method_coverage") or {}
    scan = report.get("scan_scope") or {}
    return {
        "status": "ok"
        if all(item.get("status") == "ok" for item in (alias, method, typed))
        else "failed",
        "source_path": report.get("source_path"),
        "python_files_scanned": scan.get("python_files_scanned"),
        "text_files_scanned": scan.get("text_files_scanned"),
        "alias_symbol_coverage": {
            "status": alias.get("status"),
            "missing_total": alias.get("missing_total"),
            "import_error_total": alias.get("import_error_total"),
        },
        "method_name_coverage": {
            "status": method.get("status"),
            "missing_total": method.get("missing_total"),
            "import_error_total": method.get("import_error_total"),
            "method_count": method.get("method_count"),
        },
        "typed_method_coverage": {
            "status": typed.get("status"),
            "missing_total": typed.get("missing_total"),
            "import_error_total": typed.get("import_error_total"),
            "typed_call_count": typed.get("typed_call_count"),
            "receiver_method_count": typed.get("receiver_method_count"),
        },
    }


def write_audit(source: Path, output: Path | None) -> dict[str, Any]:
    import rustwright

    rustwright.enable_playwright_compat()
    report = audit_skyvern_playwright_usage.build_report(source)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def smoke_program(modules: list[str], strict_skyvern_imports: bool, enable_compat: bool = True) -> str:
    compat_setup = (
        textwrap.indent(
            "import rustwright\n"
            "rustwright.enable_playwright_compat()\n",
            "        ",
        )
        if enable_compat
        else ""
    )
    return textwrap.dedent(
        f"""
        from __future__ import annotations

        import importlib
        import json
        import pathlib
        import re
        import sys
        import traceback
{compat_setup}

        REQUIRED_ALIAS_SYMBOLS = {json.dumps(REQUIRED_ALIAS_SYMBOLS, sort_keys=True)}
        BACKEND_MARKER_MODULES = set({json.dumps(BACKEND_MARKER_MODULES)})
        EXPECTED_BACKEND_MARKER = {EXPECTED_BACKEND_MARKER!r}
        SKYVERN_MODULES = {json.dumps(modules)}
        STRICT_SKYVERN_IMPORTS = {strict_skyvern_imports!r}
        ALIAS_ROOTS = ("playwright", "patchright", "cloakbrowser", "rustwright")

        def module_error(exc: BaseException) -> dict:
            return {{
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback_tail": traceback.format_exc(limit=8).splitlines()[-20:],
            }}

        def import_module(name: str) -> dict:
            try:
                module = importlib.import_module(name)
            except Exception as exc:
                return {{"module": name, "status": "error", **module_error(exc)}}
            path = getattr(module, "__file__", None)
            return {{
                "module": name,
                "status": "ok",
                "file": str(pathlib.Path(path).resolve()) if path else None,
            }}

        def check_backend_marker(module: object, name: str) -> dict:
            marker_fn = getattr(module, "backend_marker", None)
            if not callable(marker_fn):
                return {{
                    "status": "missing",
                    "failures": ["backend_marker is missing or is not callable"],
                    "marker": None,
                }}
            try:
                marker = marker_fn(name)
            except Exception as exc:
                return {{
                    "status": "error",
                    **module_error(exc),
                    "failures": ["backend_marker raised an exception"],
                    "marker": None,
                }}
            if not isinstance(marker, dict):
                return {{
                    "status": "invalid",
                    "failures": [f"backend_marker returned {{type(marker).__name__}}, expected dict"],
                    "marker": marker,
                }}

            failures = []
            for key, expected in EXPECTED_BACKEND_MARKER.items():
                actual = marker.get(key)
                if actual != expected:
                    failures.append(f"{{key}}={{actual!r}}, expected {{expected!r}}")
            if marker.get("api_module") != name:
                failures.append(f"api_module={{marker.get('api_module')!r}}, expected {{name!r}}")
            expected_api_package = name.split(".", 1)[0]
            if marker.get("api_package") != expected_api_package:
                failures.append(
                    f"api_package={{marker.get('api_package')!r}}, expected {{expected_api_package!r}}"
                )
            return {{
                "status": "ok" if not failures else "invalid",
                "failures": failures,
                "marker": marker,
            }}

        def check_alias(name: str, symbols: list[str]) -> dict:
            result = import_module(name)
            if result["status"] != "ok":
                result["missing_symbols"] = list(symbols)
                return result
            module = sys.modules[name]
            result["missing_symbols"] = [symbol for symbol in symbols if not hasattr(module, symbol)]
            result["module_all_count"] = len(getattr(module, "__all__", []) or [])
            if name in BACKEND_MARKER_MODULES:
                result["backend_marker"] = check_backend_marker(module, name)
                if result["backend_marker"]["status"] != "ok":
                    result["status"] = "invalid_backend_marker"
            if result["missing_symbols"]:
                result["status"] = "missing_symbols"
            return result

        def failure_text(item: dict) -> str:
            return "\\n".join(
                [
                    str(item.get("error_type") or ""),
                    str(item.get("error") or ""),
                    "\\n".join(str(line) for line in item.get("traceback_tail") or []),
                ]
            )

        def missing_module_root(item: dict) -> str | None:
            match = re.search(r"No module named ['\\"]([^'\\"]+)['\\"]", str(item.get("error") or ""))
            if not match:
                return None
            return match.group(1).split(".", 1)[0]

        def classify_skyvern_import_failure(item: dict) -> str:
            root = missing_module_root(item)
            if root in ALIAS_ROOTS:
                return "alias_dependency"
            text = failure_text(item)
            alias_path_markers = tuple(f"/{{root}}/" for root in ALIAS_ROOTS)
            if any(marker in text for marker in alias_path_markers):
                return "alias_related"
            if item.get("error_type") in {{"ImportError", "AttributeError"}} and any(root in text for root in ALIAS_ROOTS):
                return "alias_related"
            return "environment_dependency"

        alias_imports = [check_alias(name, symbols) for name, symbols in REQUIRED_ALIAS_SYMBOLS.items()]
        skyvern_imports = [import_module(name) for name in SKYVERN_MODULES]
        alias_failed = [item for item in alias_imports if item["status"] != "ok"]
        skyvern_failed = [item for item in skyvern_imports if item["status"] != "ok"]
        for item in skyvern_failed:
            item["classification"] = classify_skyvern_import_failure(item)
        skyvern_alias_failed = [
            item for item in skyvern_failed if item.get("classification") in {{"alias_dependency", "alias_related"}}
        ]
        skyvern_warnings = [
            item for item in skyvern_failed if item.get("classification") == "environment_dependency"
        ]
        status = "ok"
        if alias_failed:
            status = "alias_failed"
        elif skyvern_alias_failed:
            status = "skyvern_alias_import_failed"
        elif skyvern_failed and STRICT_SKYVERN_IMPORTS:
            status = "skyvern_import_failed"

        print(json.dumps({{
            "status": status,
            "python": sys.executable,
            "sys_path_head": sys.path[:8],
            "alias_imports": alias_imports,
            "skyvern_module_imports": skyvern_imports,
            "alias_failures": alias_failed,
            "skyvern_import_failures": skyvern_failed,
            "skyvern_alias_import_failures": skyvern_alias_failed,
            "skyvern_import_warnings": skyvern_warnings,
            "strict_skyvern_imports": STRICT_SKYVERN_IMPORTS,
        }}, sort_keys=True))
        raise SystemExit(0 if status == "ok" else 1)
        """
    )


def run_subprocess_smoke(
    *,
    source: Path,
    python: Path,
    modules: list[str],
    strict_skyvern_imports: bool,
) -> dict[str, Any]:
    env = os.environ.copy()
    append_pythonpath(env, [ROOT / "python", ROOT, source])
    result = subprocess.run(
        [str(python), "-c", smoke_program(modules, strict_skyvern_imports)],
        cwd=source,
        env=env,
        text=True,
        capture_output=True,
    )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        report = {
            "status": "subprocess_output_error",
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    report["returncode"] = result.returncode
    if result.stderr:
        report["stderr"] = result.stderr
    return report


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    source = Path(args.source).expanduser().resolve()
    if not source.is_dir():
        raise SystemExit(f"source directory not found: {source}")

    python = Path(args.python).expanduser() if args.python else default_python_for_source(source)
    if not python.exists():
        raise SystemExit(f"python executable not found: {python}")

    modules = args.module or ([] if args.skip_skyvern_module_imports else list(DEFAULT_SKYVERN_MODULES))
    audit_report = None if args.skip_audit else write_audit(source, Path(args.audit_output).expanduser())
    smoke = run_subprocess_smoke(
        source=source,
        python=python,
        modules=modules,
        strict_skyvern_imports=args.strict_skyvern_imports,
    )
    smoke_status = smoke.get("status")
    audit_status = True if audit_report is None else audit_summary(audit_report)["status"] == "ok"
    status = "ok"
    if smoke_status in {"alias_failed", "skyvern_alias_import_failed"}:
        status = smoke_status
    elif smoke_status not in {"ok", "skyvern_import_failed"}:
        status = smoke_status or "smoke_failed"
    elif not audit_status:
        status = "audit_failed"
    elif smoke_status != "ok":
        status = smoke_status or "failed"

    return {
        "schema_version": 1,
        "status": status,
        "source_path": str(source),
        "python": str(python),
        "python_resolved": str(python.resolve()),
        "rustwright_python_path": str(RUSTWRIGHT_PYTHON.resolve()),
        "audit": None if audit_report is None else audit_summary(audit_report),
        "audit_output": None if args.skip_audit else str(Path(args.audit_output).expanduser()),
        "smoke": smoke,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a read-only Skyvern Cloud replacement smoke for Rustwright Playwright aliases."
    )
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="Skyvern-cloud checkout path.")
    parser.add_argument("--python", help="Python executable to use; defaults to <source>/.venv/bin/python.")
    parser.add_argument("--audit-output", default=str(DEFAULT_AUDIT_OUTPUT), help="Ignored JSON path for full audit.")
    parser.add_argument("--skip-audit", action="store_true", help="Only run runtime import smoke.")
    parser.add_argument("--skip-skyvern-module-imports", action="store_true", help="Only check alias modules.")
    parser.add_argument(
        "--strict-skyvern-imports",
        action="store_true",
        help="Fail if curated Skyvern Cloud modules fail to import for any reason.",
    )
    parser.add_argument(
        "--module",
        action="append",
        help="Skyvern Cloud module to import. May be repeated; replaces the default curated module list.",
    )
    parser.add_argument("--output", help="Optional path to write the combined JSON report.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    args = parser.parse_args()

    report = build_report(args)
    text = json.dumps(report, indent=2 if args.pretty else None, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
