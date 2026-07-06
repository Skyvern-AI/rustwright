from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tools" / "run_skyvern_cloud_overlay_tests.py"


def write_test_package(root: Path, source: str) -> Path:
    tests_dir = root / "tests"
    tests_dir.mkdir()
    target = tests_dir / "test_overlay.py"
    target.write_text(source, encoding="utf-8")
    return target


def run_overlay(tmp_path: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--source",
            str(tmp_path),
            "--python",
            sys.executable,
            "--target",
            "tests/test_overlay.py",
            *extra_args,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


def test_overlay_runner_passes_with_rustwright_alias_first(tmp_path):
    write_test_package(
        tmp_path,
        "\n".join(
            [
                "from pathlib import Path",
                "from playwright.async_api import Page, async_playwright",
                "",
                "def test_alias_is_rustwright_backed():",
                "    import playwright.async_api as api",
                "    assert Page is not None",
                "    assert async_playwright is not None",
                f"    assert {str(ROOT / 'python')!r} in str(Path(api.__file__).resolve())",
                "",
            ]
        ),
    )

    result = run_overlay(tmp_path)
    report = json.loads(result.stdout)

    assert result.returncode == 0
    assert report["status"] == "passed"
    assert report["started_at"].endswith("Z")
    assert report["finished_at"].endswith("Z")
    assert report["duration_seconds"] >= 0
    assert report["preflight"]["status"] == "ok"
    preflight_modules = {item["module"]: item for item in report["preflight"]["modules"]}
    for module_name in (
        "playwright.async_api",
        "playwright.sync_api",
        "patchright.async_api",
        "patchright.sync_api",
    ):
        marker_check = preflight_modules[module_name]["backend_marker"]
        assert marker_check["status"] == "ok"
        assert marker_check["marker"]["api_module"] == module_name
        assert marker_check["marker"]["implementation"] == "rustwright"
        assert marker_check["marker"]["transport"] == "raw-cdp"
    assert report["pytest"]["status"] == "passed"
    assert report["pytest"]["summary"]["passed"] == 1


def test_overlay_runner_can_allow_environment_dependency_blockers(tmp_path):
    (tmp_path / "conftest.py").write_text("import definitely_missing_skyvern_dependency\n", encoding="utf-8")
    write_test_package(tmp_path, "def test_never_collected():\n    assert True\n")

    result = run_overlay(tmp_path, "--allow-environment-blockers")
    report = json.loads(result.stdout)

    assert result.returncode == 0
    assert report["status"] == "environment_blocked"
    assert report["pytest"]["classification"] == "environment_dependency"
    assert report["pytest"]["missing_module"] == "definitely_missing_skyvern_dependency"
    assert report["pytest"]["summary"] == {}


def test_overlay_runner_fails_alias_related_collection_errors(tmp_path):
    write_test_package(
        tmp_path,
        "from playwright.async_api import DefinitelyNotARealPlaywrightSymbol\n",
    )

    result = run_overlay(tmp_path)
    report = json.loads(result.stdout)

    assert result.returncode == 1
    assert report["status"] == "alias_failed"
    assert report["pytest"]["classification"] == "alias_related"
