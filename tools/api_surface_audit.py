#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


DEFAULT_CLASSES = [
    "APIRequest",
    "APIRequestContext",
    "APIResponse",
    "APIResponseAssertions",
    "APIResponseAssertionsImpl",
    "Browser",
    "BrowserContext",
    "BrowserType",
    "CDPSession",
    "ConsoleMessage",
    "Dialog",
    "Download",
    "ElementHandle",
    "Expect",
    "FileChooser",
    "Frame",
    "FrameLocator",
    "JSHandle",
    "Keyboard",
    "Locator",
    "LocatorAssertions",
    "LocatorAssertionsImpl",
    "Mouse",
    "Page",
    "PageAssertions",
    "PageAssertionsImpl",
    "Playwright",
    "PlaywrightContextManager",
    "Request",
    "Response",
    "Route",
    "Selectors",
    "Touchscreen",
    "Video",
    "WebError",
    "WebSocket",
    "WebSocketRoute",
    "Worker",
]

DEFAULT_MODULE_MEMBERS = [
    "APIResponseAssertions",
    "APIResponseAssertionsImpl",
    "BrowserBindResult",
    "ChromiumBrowserContext",
    "Cookie",
    "DebuggerLocation",
    "DebuggerPausedDetails",
    "Error",
    "FilePayload",
    "FloatRect",
    "Geolocation",
    "HttpCredentials",
    "LocatorAssertions",
    "LocatorAssertionsImpl",
    "PageAssertions",
    "PageAssertionsImpl",
    "PdfMargins",
    "PlaywrightContextManager",
    "Position",
    "ProxySettings",
    "ResourceTiming",
    "SourceLocation",
    "StorageState",
    "StorageStateCookie",
    "TimeoutError",
    "ViewportSize",
    "async_playwright",
    "expect",
    "sync_playwright",
]


def _load_module(name: str, extra_paths: list[str]) -> ModuleType:
    for path in reversed(extra_paths):
        if path and path not in sys.path:
            sys.path.insert(0, path)
    return importlib.import_module(name)


def _public_members(obj: Any) -> list[str]:
    members: list[str] = []
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            value = inspect.getattr_static(obj, name)
        except AttributeError:
            continue
        if inspect.ismodule(value):
            continue
        members.append(name)
    return sorted(set(members))


def _load_classifications(path: str | None) -> dict[str, dict[str, str]]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("classification file must contain a JSON object")
    normalized: dict[str, dict[str, str]] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            normalized[str(key)] = {"status": value, "reason": ""}
        elif isinstance(value, dict):
            normalized[str(key)] = {
                "status": str(value.get("status") or "pending"),
                "reason": str(value.get("reason") or ""),
            }
        else:
            raise ValueError(f"classification for {key!r} must be a string or object")
    return normalized


def _classify_missing(names: list[str], classifications: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    return {
        name: classifications.get(name, {"status": "unclassified", "reason": ""})
        for name in names
    }


def _actionable_missing_count(names: list[str], classifications: dict[str, dict[str, str]]) -> int:
    return sum(1 for item in names if classifications.get(item, {}).get("status") != "implemented")


def _signature_for(value: Any) -> inspect.Signature | None:
    try:
        return inspect.signature(value)
    except (TypeError, ValueError):
        return None


def _signature_parameters(signature: inspect.Signature) -> tuple[list[inspect.Parameter], bool]:
    parameters = list(signature.parameters.values())[1:]
    accepts_var_keyword = any(parameter.kind == parameter.VAR_KEYWORD for parameter in parameters)
    comparable = [
        parameter
        for parameter in parameters
        if parameter.kind not in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD}
    ]
    return comparable, accepts_var_keyword


def _signature_diffs(reference_method: Any, candidate_method: Any) -> list[dict[str, str]]:
    reference_signature = _signature_for(reference_method)
    candidate_signature = _signature_for(candidate_method)
    if reference_signature is None or candidate_signature is None:
        return []

    reference_parameters, reference_accepts_var_keyword = _signature_parameters(reference_signature)
    candidate_parameters, candidate_accepts_var_keyword = _signature_parameters(candidate_signature)
    reference_by_name = {parameter.name: parameter for parameter in reference_parameters}
    candidate_by_name = {parameter.name: parameter for parameter in candidate_parameters}
    diffs: list[dict[str, str]] = []

    if candidate_accepts_var_keyword and not reference_accepts_var_keyword:
        diffs.append(
            {
                "kind": "candidate_var_keyword",
                "parameter": "**kwargs",
                "reference": str(reference_signature),
                "candidate": str(candidate_signature),
            }
        )

    if reference_accepts_var_keyword and not candidate_accepts_var_keyword:
        diffs.append(
            {
                "kind": "missing_var_keyword",
                "parameter": "**kwargs",
                "reference": str(reference_signature),
                "candidate": str(candidate_signature),
            }
        )

    for parameter_name, reference_parameter in reference_by_name.items():
        candidate_parameter = candidate_by_name.get(parameter_name)
        if candidate_parameter is None:
            if not candidate_accepts_var_keyword:
                diffs.append(
                    {
                        "kind": "missing_parameter",
                        "parameter": parameter_name,
                        "reference": str(reference_signature),
                        "candidate": str(candidate_signature),
                    }
                )
            continue
        if candidate_parameter.kind != reference_parameter.kind:
            diffs.append(
                {
                    "kind": "kind_mismatch",
                    "parameter": parameter_name,
                    "reference": str(reference_signature),
                    "candidate": str(candidate_signature),
                }
            )
        if (candidate_parameter.default is inspect.Signature.empty) != (
            reference_parameter.default is inspect.Signature.empty
        ):
            diffs.append(
                {
                    "kind": "required_mismatch",
                    "parameter": parameter_name,
                    "reference": str(reference_signature),
                    "candidate": str(candidate_signature),
                }
            )

    if not reference_accepts_var_keyword:
        for parameter_name in candidate_by_name:
            if parameter_name not in reference_by_name:
                diffs.append(
                    {
                        "kind": "extra_parameter",
                        "parameter": parameter_name,
                        "reference": str(reference_signature),
                        "candidate": str(candidate_signature),
                    }
                )

    return diffs


def build_report(
    reference: ModuleType,
    candidate: ModuleType,
    *,
    classes: list[str],
    module_members: list[str],
    classifications: dict[str, dict[str, str]],
    compare_signatures: bool = False,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "reference_module": reference.__name__,
        "candidate_module": candidate.__name__,
        "module": {},
        "classes": {},
        "summary": {
            "missing_classes": 0,
            "missing_members": 0,
            "extra_members": 0,
            "unclassified_missing_members": 0,
            "signature_diffs": 0,
        },
    }

    reference_module_members = sorted(member for member in module_members if hasattr(reference, member))
    candidate_module_members = sorted(member for member in module_members if hasattr(candidate, member))
    missing_module_members = sorted(set(reference_module_members) - set(candidate_module_members))
    extra_module_members = sorted(set(candidate_module_members) - set(reference_module_members))
    report["summary"]["extra_members"] += len(extra_module_members)
    report["module"] = {
        "reference": reference_module_members,
        "candidate": candidate_module_members,
        "missing": _classify_missing(
            [f"module.{name}" for name in missing_module_members],
            classifications,
        ),
        "extra": extra_module_members,
    }

    for class_name in classes:
        reference_class = getattr(reference, class_name, None)
        candidate_class = getattr(candidate, class_name, None)
        if reference_class is None:
            continue
        if candidate_class is None:
            reference_members = _public_members(reference_class)
            missing = [f"{class_name}.{name}" for name in reference_members]
            report["classes"][class_name] = {
                "missing_class": True,
                "reference": reference_members,
                "candidate": [],
                "missing": _classify_missing(missing, classifications),
                "extra": [],
            }
            report["summary"]["missing_classes"] += 1
            report["summary"]["missing_members"] += _actionable_missing_count(missing, classifications)
            report["summary"]["unclassified_missing_members"] += sum(
                1 for item in missing if classifications.get(item, {}).get("status") not in {"implemented", "unsupported", "pending"}
            )
            continue

        reference_members = _public_members(reference_class)
        candidate_members = _public_members(candidate_class)
        missing_names = sorted(set(reference_members) - set(candidate_members))
        extra_names = sorted(set(candidate_members) - set(reference_members))
        report["summary"]["extra_members"] += len(extra_names)
        missing_keys = [f"{class_name}.{name}" for name in missing_names]
        report["classes"][class_name] = {
            "missing_class": False,
            "reference": reference_members,
            "candidate": candidate_members,
            "missing": _classify_missing(missing_keys, classifications),
            "extra": extra_names,
            "signature_diffs": {},
        }
        report["summary"]["missing_members"] += _actionable_missing_count(missing_keys, classifications)
        report["summary"]["unclassified_missing_members"] += sum(
            1 for item in missing_keys if classifications.get(item, {}).get("status") not in {"implemented", "unsupported", "pending"}
        )
        if compare_signatures:
            common_callable_names = [
                name
                for name in sorted(set(reference_members) & set(candidate_members))
                if callable(getattr(reference_class, name, None)) and callable(getattr(candidate_class, name, None))
            ]
            for name in common_callable_names:
                diffs = _signature_diffs(
                    getattr(reference_class, name),
                    getattr(candidate_class, name),
                )
                if diffs:
                    key = f"{class_name}.{name}"
                    report["classes"][class_name]["signature_diffs"][key] = diffs
                    report["summary"]["signature_diffs"] += len(diffs)

    report["summary"]["missing_members"] += _actionable_missing_count(
        [f"module.{name}" for name in missing_module_members],
        classifications,
    )
    report["summary"]["unclassified_missing_members"] += sum(
        1
        for name in missing_module_members
        if classifications.get(f"module.{name}", {}).get("status") not in {"implemented", "unsupported", "pending"}
    )
    return report


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# API Surface Audit",
        "",
        f"- Reference: `{report['reference_module']}`",
        f"- Candidate: `{report['candidate_module']}`",
        f"- Missing classes: `{report['summary']['missing_classes']}`",
        f"- Missing members: `{report['summary']['missing_members']}`",
        f"- Extra members: `{report['summary'].get('extra_members', 0)}`",
        f"- Unclassified missing members: `{report['summary']['unclassified_missing_members']}`",
        f"- Signature diffs: `{report['summary'].get('signature_diffs', 0)}`",
        "",
    ]

    module_missing = report["module"]["missing"]
    if module_missing:
        lines.extend(["## Module Members", ""])
        for name, info in module_missing.items():
            reason = f" - {info['reason']}" if info.get("reason") else ""
            lines.append(f"- `{name}`: {info['status']}{reason}")
        lines.append("")

    for class_name, payload in report["classes"].items():
        missing = payload["missing"]
        signature_diffs = payload.get("signature_diffs") or {}
        if not missing and not signature_diffs and not payload.get("missing_class"):
            continue
        lines.extend([f"## {class_name}", ""])
        if payload.get("missing_class"):
            lines.append("- missing class")
        for name, info in missing.items():
            reason = f" - {info['reason']}" if info.get("reason") else ""
            lines.append(f"- `{name}`: {info['status']}{reason}")
        for name, diffs in signature_diffs.items():
            lines.append(f"- `{name}` signature differs:")
            for diff in diffs:
                lines.append(
                    f"  - {diff['kind']} `{diff['parameter']}`: "
                    f"reference `{diff['reference']}`, candidate `{diff['candidate']}`"
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare Rustwright's Python API surface with a reference module.")
    parser.add_argument("--reference-module", default="playwright.sync_api")
    parser.add_argument("--candidate-module", default="rustwright.sync_api")
    parser.add_argument("--pythonpath", action="append", default=[], help="Extra import path. Can be passed multiple times.")
    parser.add_argument("--classes", default=",".join(DEFAULT_CLASSES), help="Comma-separated class names to compare.")
    parser.add_argument("--module-members", default=",".join(DEFAULT_MODULE_MEMBERS), help="Comma-separated module members to compare.")
    parser.add_argument("--classifications", help="JSON file mapping Class.member to implemented/unsupported/pending.")
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown")
    parser.add_argument("--output", help="Write report to this path instead of stdout.")
    parser.add_argument("--compare-signatures", action="store_true", help="Include inspectable callable signature differences.")
    parser.add_argument("--fail-on-missing", action="store_true", help="Exit nonzero if any missing members are found.")
    parser.add_argument("--fail-on-extra", action="store_true", help="Exit nonzero if any extra public members are found.")
    parser.add_argument("--fail-on-unclassified", action="store_true", help="Exit nonzero if missing members lack classifications.")
    parser.add_argument("--fail-on-signature-diffs", action="store_true", help="Exit nonzero if signature differences are found.")
    args = parser.parse_args(argv)

    reference = _load_module(args.reference_module, args.pythonpath)
    candidate = _load_module(args.candidate_module, args.pythonpath)
    report = build_report(
        reference,
        candidate,
        classes=_split_csv(args.classes),
        module_members=_split_csv(args.module_members),
        classifications=_load_classifications(args.classifications),
        compare_signatures=args.compare_signatures,
    )

    if args.format == "json":
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    else:
        rendered = _markdown(report)

    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")

    if args.fail_on_unclassified and report["summary"]["unclassified_missing_members"]:
        return 2
    if args.fail_on_signature_diffs and report["summary"]["signature_diffs"]:
        return 3
    if args.fail_on_extra and report["summary"]["extra_members"]:
        return 4
    if args.fail_on_missing and report["summary"]["missing_members"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
