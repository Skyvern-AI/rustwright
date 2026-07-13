#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, timeout: float | None = None) -> dict[str, Any]:
    try:
        proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        return {
            "command": command,
            "returncode": 127,
            "stdout": "",
            "stderr": str(exc),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "returncode": 124,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or f"timed out after {timeout}s",
        }
    return {
        "command": command,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def normalize_workflow(value: str) -> str:
    prefix = ".github/workflows/"
    if value.startswith(prefix):
        return value[len(prefix) :]
    return value


def repo_from_origin(origin: str) -> str | None:
    patterns = [
        r"^git@github\.com:(?P<repo>.+?)(?:\.git)?$",
        r"^https://github\.com/(?P<repo>.+?)(?:\.git)?$",
    ]
    for pattern in patterns:
        match = re.match(pattern, origin)
        if match:
            return match.group("repo")
    return None


def blacksmith_installation_report(repo: str | None, *, timeout: float | None, output_tail: int) -> dict[str, Any]:
    if repo is None:
        return {
            "returncode": None,
            "stdout": "",
            "stderr": "skipped because origin is not a GitHub repo",
            "installed": None,
        }

    owner = repo.split("/", 1)[0]
    result = run(["gh", "api", f"orgs/{owner}/installations"], timeout=timeout)
    report = trim_command_result(result, output_tail)
    report["installed"] = None
    if result["returncode"] != 0:
        return report

    try:
        payload = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        report["parse_error"] = str(exc)
        return report

    installation = next(
        (
            item
            for item in payload.get("installations", [])
            if isinstance(item, dict) and item.get("app_slug") == "blacksmith-sh"
        ),
        None,
    )
    report["stdout"] = ""
    if installation is None:
        report["installed"] = False
        return report

    report.update(
        {
            "installed": True,
            "id": installation.get("id"),
            "app_slug": installation.get("app_slug"),
            "repository_selection": installation.get("repository_selection"),
            "permissions": installation.get("permissions"),
            "updated_at": installation.get("updated_at"),
        }
    )
    return report


def classify(report: dict[str, Any]) -> str:
    if not report["local_workflow"]["exists"]:
        return "local_workflow_missing"
    if report["origin"]["repo"] is None:
        return "github_origin_missing"
    if report["github"]["contents"]["returncode"] not in (0, None):
        return "github_workflow_unavailable"
    if report["blacksmith"]["version"]["returncode"] == 127:
        return "blacksmith_cli_missing"
    warmup = report.get("warmup")
    if isinstance(warmup, dict):
        combined = f"{warmup.get('stdout', '')}\n{warmup.get('stderr', '')}"
        if warmup.get("returncode") == 0:
            return "warmup_started"
        if "Could not fetch .github/workflows/" in combined:
            return "blacksmith_repo_visibility_blocked"
        if "no workflows with jobs found" in combined:
            return "blacksmith_workflow_scan_empty"
        if warmup.get("returncode") == 124:
            return "warmup_timeout"
        return "warmup_failed"
    return "preflight_ok"


def diagnose(report: dict[str, Any]) -> dict[str, str]:
    warmup = report.get("warmup")
    combined = ""
    if isinstance(warmup, dict):
        combined = f"{warmup.get('stdout', '')}\n{warmup.get('stderr', '')}"

    if (
        "Could not fetch .github/workflows/" in combined
        and report["github"]["contents"]["returncode"] == 0
    ):
        installation = report["github"].get("blacksmith_app_installation", {})
        if isinstance(installation, dict) and installation.get("repository_selection") == "selected":
            return {
                "probable_root_cause": "blacksmith_github_app_selected_repo_access",
                "recommended_action": (
                    "Add Skyvern-AI/rustwright to the selected repositories for the blacksmith-sh GitHub App, "
                    "then rerun this probe."
                ),
            }
        return {
            "probable_root_cause": "blacksmith_github_app_repo_visibility",
            "recommended_action": (
                "Confirm the blacksmith-sh GitHub App can read Skyvern-AI/rustwright and rerun this probe."
            ),
        }

    if report["github"]["contents"]["returncode"] not in (0, None):
        return {
            "probable_root_cause": "github_workflow_unavailable_to_current_token",
            "recommended_action": "Push the workflow to the requested ref or use a ref visible to gh.",
        }

    return {
        "probable_root_cause": report["status"],
        "recommended_action": "No Testbox-specific remediation inferred from this preflight.",
    }


def trim_command_result(result: dict[str, Any], limit: int) -> dict[str, Any]:
    trimmed = dict(result)
    for key in ("stdout", "stderr"):
        value = trimmed.get(key)
        if isinstance(value, str) and len(value) > limit:
            trimmed[key] = value[-limit:]
            trimmed[f"{key}_truncated"] = True
    return trimmed


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    workflow = normalize_workflow(args.workflow)
    local_path = ROOT / ".github" / "workflows" / workflow
    origin_result = run(["git", "remote", "get-url", "origin"])
    origin_url = origin_result["stdout"].strip() if origin_result["returncode"] == 0 else ""
    repo = repo_from_origin(origin_url)

    github_contents: dict[str, Any] = {
        "returncode": None,
        "stdout": "",
        "stderr": "skipped because origin is not a GitHub repo",
    }
    github_workflows: dict[str, Any] = {
        "returncode": None,
        "stdout": "",
        "stderr": "skipped because origin is not a GitHub repo",
    }
    if repo:
        github_contents = run(
            [
                "gh",
                "api",
                f"repos/{repo}/contents/.github/workflows/{workflow}?ref={args.ref}",
                "--jq",
                ".path + \" sha=\" + .sha",
            ],
            timeout=args.command_timeout,
        )
        github_workflows = run(
            ["gh", "workflow", "list", "--repo", repo, "--all"],
            timeout=args.command_timeout,
        )

    blacksmith_version = run(["blacksmith", "--version"], timeout=args.command_timeout)
    blacksmith_auth = run(["blacksmith", "auth", "status"], timeout=args.command_timeout)

    report: dict[str, Any] = {
        "workflow": workflow,
        "workflow_path": f".github/workflows/{workflow}",
        "ref": args.ref,
        "job": args.job,
        "local_workflow": {
            "exists": local_path.is_file(),
            "path": str(local_path.relative_to(ROOT)),
        },
        "origin": {
            "url": origin_url,
            "repo": repo,
            "returncode": origin_result["returncode"],
            "stderr": origin_result["stderr"],
        },
        "github": {
            "contents": trim_command_result(github_contents, args.output_tail),
            "workflows": trim_command_result(github_workflows, args.output_tail),
            "blacksmith_app_installation": blacksmith_installation_report(
                repo,
                timeout=args.command_timeout,
                output_tail=args.output_tail,
            ),
        },
        "blacksmith": {
            "version": trim_command_result(blacksmith_version, args.output_tail),
            "auth_status": trim_command_result(blacksmith_auth, args.output_tail),
        },
    }

    if args.probe_warmup:
        command = [
            "blacksmith",
            "testbox",
            "warmup",
            workflow,
            "--ref",
            args.ref,
            "--idle-timeout",
            str(args.idle_timeout),
        ]
        if args.job:
            command.extend(["--job", args.job])
        report["warmup"] = trim_command_result(
            run(command, timeout=args.warmup_timeout),
            args.output_tail,
        )

    report["status"] = classify(report)
    report["diagnosis"] = diagnose(report)
    report["ok"] = report["status"] in {"preflight_ok", "warmup_started"}
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Check whether Blacksmith Testbox can see the benchmark workflow.")
    parser.add_argument("--workflow", default="benchmark-testbox.yml")
    parser.add_argument("--ref", default="main")
    parser.add_argument("--job", default="benchmark")
    parser.add_argument("--probe-warmup", action="store_true", help="Actually call blacksmith testbox warmup.")
    parser.add_argument("--idle-timeout", type=int, default=5)
    parser.add_argument("--command-timeout", type=float, default=30.0)
    parser.add_argument("--warmup-timeout", type=float, default=120.0)
    parser.add_argument("--output-tail", type=int, default=4000)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = build_report(args)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"status={report['status']}")
        print(f"workflow={report['workflow_path']}")
        print(f"repo={report['origin']['repo'] or '<unavailable>'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
