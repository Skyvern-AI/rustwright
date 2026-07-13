from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from run_skyvern_replacement_smoke import smoke_program  # noqa: E402


BACKEND_MARKER_MODULES = [
    "playwright.async_api",
    "playwright.sync_api",
    "patchright.async_api",
    "patchright.sync_api",
]


def test_playwright_and_patchright_aliases_expose_rustwright_backend_marker():
    program = textwrap.dedent(
        f"""
        import importlib
        import json
        import rustwright

        rustwright.enable_playwright_compat()

        modules = {BACKEND_MARKER_MODULES!r}
        results = {{}}
        for name in modules:
            module = importlib.import_module(name)
            marker = module.backend_marker(name)
            results[name] = {{
                "status": "ok",
                "file": module.__file__,
                "marker": marker,
            }}
        print(json.dumps(results, sort_keys=True))
        """
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "python"), env.get("PYTHONPATH", "")])

    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    report = json.loads(result.stdout)

    for module_name, item in report.items():
        assert str(ROOT / "python") in item["file"]
        marker = item["marker"]
        assert marker["implementation"] == "rustwright"
        assert marker["package"] == "rustwright"
        assert marker["api_module"] == module_name
        assert marker["api_package"] == module_name.split(".", 1)[0]
        assert marker["runtime"] == "rust-pyo3-extension"
        assert marker["runtime_module"] == "rustwright._rustwright"
        assert marker["transport"] == "raw-cdp"
        assert marker["cdp_first"] is True
        assert marker["python_playwright_driver"] is False


def test_skyvern_replacement_smoke_checks_aliases_and_imports_fake_module(tmp_path):
    package = tmp_path / "sample_app"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "playwright_user.py").write_text(
        "\n".join(
            [
                "from playwright._impl._errors import TargetClosedError",
                "from playwright.async_api import Page, async_playwright",
                "from patchright.async_api import Page as PatchrightPage",
                "import cloakbrowser",
                "",
                "class UsesPage:",
                "    def __init__(self, page: Page) -> None:",
                "        self.page = page",
                "",
                "    async def run(self) -> None:",
                "        await self.page.goto('https://example.com')",
                "",
                "SYMBOLS = (TargetClosedError, async_playwright, PatchrightPage, cloakbrowser.ensure_binary)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    audit_output = tmp_path / "audit.json"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "run_skyvern_replacement_smoke.py"),
            "--source",
            str(tmp_path),
            "--python",
            sys.executable,
            "--audit-output",
            str(audit_output),
            "--module",
            "sample_app.playwright_user",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    report = json.loads(result.stdout)

    assert report["status"] == "ok"
    assert report["audit"]["status"] == "ok"
    assert report["audit"]["alias_symbol_coverage"]["missing_total"] == 0
    assert report["audit"]["typed_method_coverage"]["missing_total"] == 0
    assert report["smoke"]["status"] == "ok"
    assert report["smoke"]["alias_failures"] == []
    assert report["smoke"]["skyvern_import_failures"] == []
    imported = {item["module"]: item for item in report["smoke"]["skyvern_module_imports"]}
    assert imported["sample_app.playwright_user"]["status"] == "ok"
    alias_paths = {
        item["module"]: item["file"]
        for item in report["smoke"]["alias_imports"]
        if item["module"] in {"playwright.async_api", "patchright.async_api", "cloakbrowser"}
    }
    assert str(ROOT / "python") in alias_paths["playwright.async_api"]
    assert str(ROOT / "python") in alias_paths["patchright.async_api"]
    assert str(ROOT / "python") in alias_paths["cloakbrowser"]
    backend_markers = {
        item["module"]: item["backend_marker"]
        for item in report["smoke"]["alias_imports"]
        if item["module"] in BACKEND_MARKER_MODULES
    }
    assert set(backend_markers) == set(BACKEND_MARKER_MODULES)
    for module_name, backend_marker in backend_markers.items():
        assert backend_marker["status"] == "ok"
        assert backend_marker["marker"]["api_module"] == module_name
        assert backend_marker["marker"]["implementation"] == "rustwright"
        assert backend_marker["marker"]["transport"] == "raw-cdp"
        assert backend_marker["marker"]["python_playwright_driver"] is False
    assert audit_output.exists()


def test_skyvern_replacement_smoke_fails_on_non_rustwright_backend_marker(tmp_path):
    fake_root = tmp_path / "fake_backend"
    fake_root.mkdir()
    for package in ("playwright", "patchright"):
        package_dir = fake_root / package
        impl_dir = package_dir / "_impl"
        impl_dir.mkdir(parents=True)
        (package_dir / "__init__.py").write_text("", encoding="utf-8")
        api_source = textwrap.dedent(
            """
            class Error(Exception):
                pass

            class TimeoutError(Error):
                pass

            class Browser:
                pass

            class BrowserContext:
                pass

            class Frame:
                pass

            class Locator:
                pass

            class Page:
                pass

            class Route:
                pass

            def async_playwright():
                return object()

            def sync_playwright():
                return object()

            def backend_marker(api_module=None):
                module_name = api_module or __name__
                return {
                    "implementation": "python-playwright",
                    "package": "playwright",
                    "version": "1.99.0",
                    "api_package": module_name.split(".", 1)[0],
                    "api_module": module_name,
                    "replacement_backend": False,
                    "runtime": "python-driver",
                    "runtime_module": "playwright._impl._driver",
                    "runtime_module_file": __file__,
                    "transport": "playwright-driver",
                    "transport_protocol": "Playwright wire protocol",
                    "cdp_first": False,
                    "python_playwright_driver": True,
                    "playwright_driver": "node-driver",
                }

            __all__ = [
                "Error",
                "TimeoutError",
                "Browser",
                "BrowserContext",
                "Frame",
                "Locator",
                "Page",
                "Route",
                "async_playwright",
                "sync_playwright",
                "backend_marker",
            ]
            """
        )
        (package_dir / "async_api.py").write_text(api_source, encoding="utf-8")
        (package_dir / "sync_api.py").write_text(api_source, encoding="utf-8")
        (impl_dir / "__init__.py").write_text("", encoding="utf-8")
        (impl_dir / "_errors.py").write_text(
            textwrap.dedent(
                """
                class Error(Exception):
                    pass

                class TargetClosedError(Error):
                    pass

                class TimeoutError(Error):
                    pass
                """
            ),
            encoding="utf-8",
        )
    (fake_root / "cloakbrowser.py").write_text(
        textwrap.dedent(
            """
            def ensure_binary():
                return "fake"

            def launch():
                return object()

            async def launch_async():
                return object()

            def launch_persistent_context():
                return object()

            async def launch_persistent_context_async():
                return object()
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(fake_root)

    result = subprocess.run(
        [sys.executable, "-c", smoke_program([], False, enable_compat=False)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )
    report = json.loads(result.stdout)

    assert result.returncode == 1
    assert report["status"] == "alias_failed"
    failures = {item["module"]: item for item in report["alias_failures"]}
    assert set(BACKEND_MARKER_MODULES).issubset(failures)
    playwright_failure = failures["playwright.async_api"]
    assert playwright_failure["status"] == "invalid_backend_marker"
    marker_check = playwright_failure["backend_marker"]
    assert marker_check["status"] == "invalid"
    assert marker_check["marker"]["implementation"] == "python-playwright"
    assert marker_check["marker"]["python_playwright_driver"] is True
    assert "implementation='python-playwright', expected 'rustwright'" in marker_check["failures"]


def test_skyvern_replacement_smoke_warns_on_unrelated_skyvern_dependency(tmp_path):
    package = tmp_path / "sample_app"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "missing_dependency.py").write_text(
        "import definitely_missing_skyvern_dependency\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "run_skyvern_replacement_smoke.py"),
            "--source",
            str(tmp_path),
            "--python",
            sys.executable,
            "--skip-audit",
            "--module",
            "sample_app.missing_dependency",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    report = json.loads(result.stdout)

    assert report["status"] == "ok"
    assert report["smoke"]["status"] == "ok"
    assert report["smoke"]["skyvern_alias_import_failures"] == []
    warnings = report["smoke"]["skyvern_import_warnings"]
    assert [item["module"] for item in warnings] == ["sample_app.missing_dependency"]
    assert warnings[0]["classification"] == "environment_dependency"


def test_skyvern_replacement_smoke_fails_on_alias_related_skyvern_import_error(tmp_path):
    package = tmp_path / "sample_app"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "bad_alias_import.py").write_text(
        "from playwright.async_api import DefinitelyNotARealPlaywrightSymbol\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "run_skyvern_replacement_smoke.py"),
            "--source",
            str(tmp_path),
            "--python",
            sys.executable,
            "--skip-audit",
            "--module",
            "sample_app.bad_alias_import",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    report = json.loads(result.stdout)

    assert result.returncode == 1
    assert report["status"] == "skyvern_alias_import_failed"
    assert report["smoke"]["status"] == "skyvern_alias_import_failed"
    failures = report["smoke"]["skyvern_alias_import_failures"]
    assert [item["module"] for item in failures] == ["sample_app.bad_alias_import"]
    assert failures[0]["classification"] == "alias_related"
