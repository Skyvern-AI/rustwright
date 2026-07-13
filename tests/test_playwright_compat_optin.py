from __future__ import annotations

import json
import subprocess
import sys
import textwrap


def _run_probe(source: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_rustwright_import_does_not_install_legacy_aliases():
    report = _run_probe(
        """
        import importlib
        import importlib.util
        import json
        import pathlib
        import sys

        legacy_roots = ["playwright", "patchright", "cloakbrowser", "pytest_playwright"]
        before = {name: name in sys.modules for name in legacy_roots}

        import rustwright

        after_rustwright = {name: name in sys.modules for name in legacy_roots}

        def root_probe(name):
            spec = importlib.util.find_spec(name)
            if spec is None:
                return {"status": "missing", "origin": None, "rustwright_backed": False}
            paths = []
            if spec.origin:
                paths.append(pathlib.Path(spec.origin))
            for location in spec.submodule_search_locations or []:
                package = pathlib.Path(location)
                paths.extend(
                    candidate
                    for candidate in [
                        package / "__init__.py",
                        package / "sync_api.py",
                        package / "async_api.py",
                        package / "pytest_playwright.py",
                    ]
                    if candidate.exists()
                )
            rustwright_backed = False
            for path in paths:
                try:
                    if "rustwright" in path.read_text(encoding="utf-8"):
                        rustwright_backed = True
                        break
                except OSError:
                    pass
            return {
                "status": "present",
                "origin": spec.origin,
                "rustwright_backed": rustwright_backed,
            }

        probes = {name: root_probe(name) for name in legacy_roots}

        compat_sync = importlib.import_module("rustwright._compat.playwright.sync_api")
        native_sync = importlib.import_module("rustwright.sync_api")
        after_direct_compat = {name: name in sys.modules for name in legacy_roots}

        print(json.dumps({
            "before": before,
            "after_rustwright": after_rustwright,
            "after_direct_compat": after_direct_compat,
            "direct_compat_identity": compat_sync.sync_playwright is native_sync.sync_playwright,
            "probes": probes,
            "rustwright_all": sorted(name for name in rustwright.__all__ if name.endswith("playwright_compat")),
        }, sort_keys=True))
        """
    )

    assert report["before"] == {
        "playwright": False,
        "patchright": False,
        "cloakbrowser": False,
        "pytest_playwright": False,
    }
    assert report["after_rustwright"] == report["before"]
    assert report["after_direct_compat"] == report["before"]
    assert report["direct_compat_identity"] is True
    assert report["rustwright_all"] == ["disable_playwright_compat", "enable_playwright_compat"]
    assert not any(item["rustwright_backed"] for item in report["probes"].values())


def test_enable_playwright_compat_installs_and_removes_aliases():
    report = _run_probe(
        """
        import importlib
        import json
        import sys

        import rustwright
        import rustwright.async_api as native_async
        import rustwright.sync_api as native_sync

        rustwright.enable_playwright_compat()
        rustwright.enable_playwright_compat()

        playwright_sync = importlib.import_module("playwright.sync_api")
        playwright_async = importlib.import_module("playwright.async_api")
        playwright_errors = importlib.import_module("playwright._impl._errors")
        patchright_sync = importlib.import_module("patchright.sync_api")
        patchright_async = importlib.import_module("patchright.async_api")
        cloakbrowser = importlib.import_module("cloakbrowser")
        pytest_playwright = importlib.import_module("pytest_playwright.pytest_playwright")

        marker = playwright_sync.backend_marker("playwright.sync_api")
        installed = {
            "playwright": sys.modules["playwright"].__name__,
            "playwright.sync_api": sys.modules["playwright.sync_api"].__name__,
            "patchright.sync_api": sys.modules["patchright.sync_api"].__name__,
            "cloakbrowser": sys.modules["cloakbrowser"].__name__,
            "pytest_playwright.pytest_playwright": sys.modules["pytest_playwright.pytest_playwright"].__name__,
        }
        identities = {
            "playwright_sync": playwright_sync.sync_playwright is native_sync.sync_playwright,
            "playwright_async": playwright_async.async_playwright is native_async.async_playwright,
            "playwright_errors": playwright_errors.Error is native_sync.Error,
            "patchright_sync": patchright_sync.sync_playwright is native_sync.sync_playwright,
            "patchright_async": patchright_async.async_playwright is native_async.async_playwright,
            "cloakbrowser": callable(cloakbrowser.launch_async),
            "pytest_playwright": pytest_playwright.CreateContextCallback.__name__ == "CreateContextCallback",
        }

        rustwright.disable_playwright_compat()
        rustwright.disable_playwright_compat()
        after_disable = {
            name: name in sys.modules
            for name in [
                "playwright",
                "playwright.sync_api",
                "patchright",
                "patchright.sync_api",
                "cloakbrowser",
                "pytest_playwright",
                "pytest_playwright.pytest_playwright",
            ]
        }

        print(json.dumps({
            "installed": installed,
            "identities": identities,
            "marker": marker,
            "after_disable": after_disable,
        }, sort_keys=True))
        """
    )

    assert report["installed"] == {
        "playwright": "rustwright._compat.playwright",
        "playwright.sync_api": "rustwright._compat.playwright.sync_api",
        "patchright.sync_api": "rustwright._compat.patchright.sync_api",
        "cloakbrowser": "rustwright._compat.cloakbrowser",
        "pytest_playwright.pytest_playwright": "rustwright._compat.pytest_playwright.pytest_playwright",
    }
    assert all(report["identities"].values())
    assert report["marker"]["implementation"] == "rustwright"
    assert report["marker"]["api_module"] == "playwright.sync_api"
    assert report["marker"]["api_package"] == "playwright"
    assert not any(report["after_disable"].values())
