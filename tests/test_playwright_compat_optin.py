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


def _plugin_args() -> list[str]:
    # In an installed environment the rustwright pytest11 entry point loads the
    # plugin automatically (and passing -p as well would register the module
    # under a second name, which pluggy rejects). In a bare dev checkout the
    # entry point does not exist and -p is required.
    from importlib.metadata import entry_points

    eps = entry_points(group="pytest11")
    if any(ep.value == "rustwright.pytest_plugin" for ep in eps):
        return []
    return ["-p", "rustwright.pytest_plugin"]


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


def test_enable_playwright_compat_covers_private_import_paths():
    # Real-world libraries import Playwright's private modules directly
    # (generated sync/async classes, API structures, error types). Those paths
    # must resolve under the compat aliases with identity to Rustwright's own
    # classes, or migrations crash at import time before any test runs.
    report = _run_probe(
        """
        import json

        import rustwright
        import rustwright.async_api as native_async
        import rustwright.sync_api as native_sync

        rustwright.enable_playwright_compat()

        from playwright.sync_api._generated import Page as SyncGeneratedPage
        from playwright.async_api._generated import Page as AsyncGeneratedPage
        from playwright._impl._api_structures import (
            ClientCertificate,
            Geolocation,
            SetCookieParam,
            StorageState,
            ViewportSize,
        )
        from playwright._impl._errors import TargetClosedError
        from patchright.sync_api._generated import Page as PatchrightGeneratedPage
        from patchright._impl._api_structures import ViewportSize as PatchrightViewportSize

        print(json.dumps({
            "sync_generated_page": SyncGeneratedPage is native_sync.Page,
            "async_generated_page": AsyncGeneratedPage is native_async.Page,
            "patchright_generated_page": PatchrightGeneratedPage is native_sync.Page,
            "viewport_size": ViewportSize is native_sync.ViewportSize,
            "patchright_viewport_size": PatchrightViewportSize is native_sync.ViewportSize,
            "geolocation": Geolocation is native_sync.Geolocation,
            "storage_state": StorageState is native_sync.StorageState,
            "target_closed_error": TargetClosedError is native_sync.TargetClosedError,
            "set_cookie_param_keys": sorted(SetCookieParam.__annotations__),
            "client_certificate_keys": sorted(ClientCertificate.__annotations__),
        }, sort_keys=True))
        """
    )

    assert report["sync_generated_page"] is True
    assert report["async_generated_page"] is True
    assert report["patchright_generated_page"] is True
    assert report["viewport_size"] is True
    assert report["patchright_viewport_size"] is True
    assert report["geolocation"] is True
    assert report["storage_state"] is True
    assert report["target_closed_error"] is True
    assert "sameSite" in report["set_cookie_param_keys"]
    assert "certPath" in report["client_certificate_keys"]


def test_browser_context_args_fixture_is_session_scoped():
    # pytest-playwright's documented pattern is a session-scoped
    # browser_context_args override in conftest.py. If the plugin defines the
    # fixture function-scoped, every test using that pattern dies with
    # ScopeMismatch at collection.
    import rustwright.pytest_plugin as plugin

    fixture = plugin.browser_context_args
    # pytest < 8.4 stores the marker on the function; >= 8.4 wraps the
    # function in a FixtureFunctionDefinition carrying the marker.
    marker = getattr(fixture, "_pytestfixturefunction", None) or getattr(
        fixture, "_fixture_function_marker", None
    )
    assert marker is not None, "browser_context_args is not a pytest fixture"
    assert marker.scope == "session"


def _run_pytest(tmp_path, target, *extra_args, env=None):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(target),
            "-p",
            "no:cacheprovider",
            "-q",
            *extra_args,
            *_plugin_args(),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )


def test_pytest_plugin_tolerates_foreign_option_registration(tmp_path):
    # pytest-base-url (and pytest-playwright) register --base-url/--browser
    # too. `-p` plugins register before setuptools entry points, so a foreign
    # plugin passed with `-p` claims the option strings first — exactly the
    # load order that made rustwright's own registration abort pytest startup
    # with "option names already added". Rustwright must tolerate the
    # collision and read the surviving registration through its fallbacks.
    import os

    foreign = tmp_path / "foreign_options_plugin.py"
    foreign.write_text(
        textwrap.dedent(
            """
            def pytest_addoption(parser):
                parser.addoption("--base-url", default=None, help="foreign base url")
                # Scalar (non-append) on purpose: the fallback read must not
                # iterate a foreign string value character by character.
                parser.addoption("--browser", default=None, help="foreign browser")
            """
        ),
        encoding="utf-8",
    )
    test_file = tmp_path / "test_options.py"
    test_file.write_text(
        textwrap.dedent(
            """
            def test_fixture_fallbacks(browser_name, base_url, browser_context_args):
                assert browser_name == "chromium"
                assert base_url is None
                assert isinstance(browser_context_args, dict)
            """
        ),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path) + os.pathsep + env.get("PYTHONPATH", "")
    result = _run_pytest(
        tmp_path,
        test_file,
        "-p",
        "foreign_options_plugin",
        "--browser",
        "chromium",
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already added" not in result.stderr
    # The scalar foreign value must yield exactly one parametrization.
    assert "1 passed" in result.stdout, result.stdout


def test_session_scoped_browser_context_args_override_collects(tmp_path):
    # Regression test for the exact ScopeMismatch failure mode: a conftest
    # override declared session-scoped (pytest-playwright's documented
    # pattern) must collect and run against the plugin's fixture graph.
    conftest = tmp_path / "conftest.py"
    conftest.write_text(
        textwrap.dedent(
            """
            import pytest

            @pytest.fixture(scope="session")
            def browser_context_args(browser_context_args):
                return {**browser_context_args, "locale": "en-US"}
            """
        ),
        encoding="utf-8",
    )
    test_file = tmp_path / "test_scope.py"
    test_file.write_text(
        textwrap.dedent(
            """
            def test_override_applies(browser_context_args):
                assert browser_context_args["locale"] == "en-US"
            """
        ),
        encoding="utf-8",
    )
    result = _run_pytest(tmp_path, test_file)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ScopeMismatch" not in result.stdout + result.stderr
