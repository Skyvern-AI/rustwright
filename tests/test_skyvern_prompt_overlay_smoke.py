from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tools" / "run_skyvern_prompt_overlay_smoke.py"


def write_module(root: Path, dotted_name: str, source: str) -> str:
    parts = dotted_name.split(".")
    directory = root
    for part in parts[:-1]:
        directory /= part
        directory.mkdir(exist_ok=True)
        init = directory / "__init__.py"
        if not init.exists():
            init.write_text("", encoding="utf-8")
    target = directory / f"{parts[-1]}.py"
    target.write_text(source, encoding="utf-8")
    return dotted_name


def run_smoke(tmp_path: Path, module: str, *extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--source",
            str(tmp_path),
            "--python",
            sys.executable,
            "--module",
            module,
            *extra_args,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


def test_prompt_overlay_smoke_passes_with_rustwright_aliases_first(tmp_path):
    module = write_module(
        tmp_path,
        "prompt_eval_fake.extract_action",
        "\n".join(
            [
                "from pathlib import Path",
                "import playwright.async_api as api",
                "ALIAS_PATH = str(Path(api.__file__).resolve())",
            ]
        ),
    )

    result = run_smoke(tmp_path, module)
    report = json.loads(result.stdout)

    assert result.returncode == 0
    assert report["status"] == "passed"
    assert report["preflight"]["status"] == "ok"
    imported = report["module_results"][0]["imported"]
    assert imported["module"] == module
    preflight_paths = [
        item.get("file") or ""
        for item in report["preflight"]["modules"]
        if item["module"] in {"playwright.async_api", "patchright.async_api", "cloakbrowser"}
    ]
    assert any(str(ROOT / "python") in path for path in preflight_paths)


def test_prompt_overlay_smoke_allows_environment_blockers(tmp_path):
    module = write_module(
        tmp_path,
        "prompt_eval_fake.blocked",
        "import definitely_missing_skyvern_prompt_dependency\n",
    )

    result = run_smoke(tmp_path, module, "--allow-environment-blockers")
    report = json.loads(result.stdout)

    assert result.returncode == 0
    assert report["status"] == "environment_blocked"
    assert report["module_results"][0]["classification"] == "environment_dependency"
    assert report["module_results"][0]["missing_module"] == "definitely_missing_skyvern_prompt_dependency"


def test_prompt_overlay_smoke_fails_alias_related_imports(tmp_path):
    module = write_module(
        tmp_path,
        "prompt_eval_fake.bad_alias",
        "from playwright.async_api import DefinitelyNotARealPlaywrightSymbol\n",
    )

    result = run_smoke(tmp_path, module)
    report = json.loads(result.stdout)

    assert result.returncode == 1
    assert report["status"] == "alias_failed"
    assert report["module_results"][0]["classification"] == "alias_related"
