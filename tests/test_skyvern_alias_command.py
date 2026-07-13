from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tools" / "run_skyvern_alias_command.py"


def run_alias_command(tmp_path: Path, code: str, *extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--source",
            str(tmp_path),
            "--python",
            sys.executable,
            *extra_args,
            "--",
            sys.executable,
            "-c",
            code,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


def test_alias_command_runner_passes_with_rustwright_aliases_first(tmp_path):
    code = "\n".join(
        [
            "from pathlib import Path",
            "import json",
            "import rustwright",
            "rustwright.enable_playwright_compat()",
            "import playwright.async_api as playwright_api",
            "import patchright.async_api as patchright_api",
            "import cloakbrowser",
            "print(json.dumps({",
            "    'playwright': str(Path(playwright_api.__file__).resolve()),",
            "    'patchright': str(Path(patchright_api.__file__).resolve()),",
            "    'cloakbrowser': str(Path(cloakbrowser.__file__).resolve()),",
            "}))",
        ]
    )

    result = run_alias_command(tmp_path, code)
    report = json.loads(result.stdout)
    command_stdout = json.loads(report["command_result"]["stdout_tail"])

    assert result.returncode == 0
    assert report["status"] == "passed"
    assert report["preflight"]["status"] == "ok"
    assert report["command_result"]["classification"] == "passed"
    assert str(ROOT / "python") in command_stdout["playwright"]
    assert str(ROOT / "python") in command_stdout["patchright"]
    assert str(ROOT / "python") in command_stdout["cloakbrowser"]


def test_alias_command_runner_can_allow_environment_dependency_blockers(tmp_path):
    result = run_alias_command(
        tmp_path,
        "import definitely_missing_skyvern_dependency\n",
        "--allow-environment-blockers",
    )
    report = json.loads(result.stdout)

    assert result.returncode == 0
    assert report["status"] == "environment_blocked"
    assert report["command_result"]["classification"] == "environment_dependency"
    assert report["command_result"]["missing_module"] == "definitely_missing_skyvern_dependency"


def test_alias_command_runner_fails_alias_related_errors(tmp_path):
    result = run_alias_command(
        tmp_path,
        "import rustwright\n"
        "rustwright.enable_playwright_compat()\n"
        "from playwright.async_api import DefinitelyNotARealPlaywrightSymbol\n",
    )
    report = json.loads(result.stdout)

    assert result.returncode == 1
    assert report["status"] == "alias_failed"
    assert report["command_result"]["classification"] == "alias_related"
