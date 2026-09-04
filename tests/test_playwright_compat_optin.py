from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYTHON_ROOT = _REPO_ROOT / "python"
_PYTEST_ALIASES = [
    "pytest_playwright",
    "playwright.pytest_plugin",
    "patchright.pytest_plugin",
    "pytest_playwright.pytest_playwright",
]


def _run_probe(source: str, *, no_site_packages: bool = False) -> dict[str, object]:
    command = [sys.executable]
    if no_site_packages:
        command.append("-S")
    command.extend(["-c", textwrap.dedent(source)])
    env = dict(os.environ)
    if no_site_packages:
        env["PYTHONPATH"] = str(_PYTHON_ROOT)
    result = subprocess.run(
        command,
        env=env,
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

    eps = entry_points()
    group = (
        eps.select(group="pytest11")
        if hasattr(eps, "select")
        else eps.get("pytest11", ())
    )
    if any(ep.value == "rustwright.pytest_plugin" for ep in group):
        return []
    return ["-p", "rustwright.pytest_plugin"]


def test_rustwright_import_does_not_install_legacy_aliases():
    report = _run_probe(
        """
        import importlib
        import json
        import sys

        legacy_roots = ["playwright", "patchright", "cloakbrowser", "pytest_playwright"]
        before = {name: name in sys.modules for name in legacy_roots}
        import rustwright
        after_rustwright = {name: name in sys.modules for name in legacy_roots}

        compat_sync = importlib.import_module("rustwright._compat.playwright.sync_api")
        native_sync = importlib.import_module("rustwright.sync_api")
        after_direct_compat = {name: name in sys.modules for name in legacy_roots}

        print(json.dumps({
            "before": before,
            "after_rustwright": after_rustwright,
            "after_direct_compat": after_direct_compat,
            "direct_compat_identity": compat_sync.sync_playwright is native_sync.sync_playwright,
            "rustwright_all": sorted(
                name for name in rustwright.__all__ if name.endswith("playwright_compat")
            ),
        }, sort_keys=True))
        """
    )

    expected_roots = {
        "playwright": False,
        "patchright": False,
        "cloakbrowser": False,
        "pytest_playwright": False,
    }
    assert report["before"] == expected_roots
    assert report["after_rustwright"] == expected_roots
    assert report["after_direct_compat"] == expected_roots
    assert report["direct_compat_identity"] is True
    assert report["rustwright_all"] == ["enable_playwright_compat"]


def test_clean_python_without_pytest_enables_core_aliases_only():
    report = _run_probe(
        """
        import importlib
        import importlib.util
        import json
        import sys

        assert importlib.util.find_spec("pytest") is None
        import rustwright

        result = rustwright.enable_playwright_compat()
        playwright_sync = importlib.import_module("playwright.sync_api")
        try:
            importlib.import_module("pytest_playwright")
        except ModuleNotFoundError as error:
            missing_plugin = error.name
        else:
            missing_plugin = None

        print(json.dumps({
            "core_identity": playwright_sync.sync_playwright is rustwright.sync_playwright,
            "missing_plugin": missing_plugin,
            "pytest_loaded": "pytest" in sys.modules,
            "registered_aliases": list(result.registered_aliases),
            "skipped_aliases": list(result.skipped_aliases),
            "plugin_targets_loaded": sorted(
                name for name in sys.modules
                if name.startswith("rustwright._compat.pytest_playwright")
                or name.endswith(".pytest_plugin")
            ),
        }, sort_keys=True))
        """,
        no_site_packages=True,
    )

    assert report["core_identity"] is True
    assert report["missing_plugin"] == "pytest_playwright"
    assert report["pytest_loaded"] is False
    assert report["skipped_aliases"] == _PYTEST_ALIASES
    assert "playwright.sync_api" in report["registered_aliases"]
    assert report["plugin_targets_loaded"] == []


def test_enable_leaves_sys_meta_path_untouched():
    report = _run_probe(
        """
        import json
        import sys

        import rustwright

        before = list(sys.meta_path)
        rustwright.enable_playwright_compat()
        after_enable = list(sys.meta_path)

        print(json.dumps({
            "enable_unchanged": len(before) == len(after_enable) and all(
                left is right for left, right in zip(before, after_enable)
            ),
        }, sort_keys=True))
        """
    )

    assert report == {"enable_unchanged": True}


def test_pytest_playwright_callback_type_export_is_eager_and_exact():
    report = _run_probe(
        """
        import importlib
        import json
        import sys

        import rustwright

        rustwright.enable_playwright_compat()
        package = importlib.import_module("pytest_playwright")
        implementation_name = "rustwright._compat.pytest_playwright.pytest_playwright"
        callback = package.CreateContextCallback

        from rustwright.pytest_plugin import CreateContextCallback

        print(json.dumps({
            "implementation_loaded": implementation_name in sys.modules,
            "is_exact_protocol": callback is CreateContextCallback,
            "is_protocol": callback._is_protocol,
            "has_viewport_annotation": "viewport" in callback.__call__.__annotations__,
        }, sort_keys=True))
        """
    )

    assert report == {
        "has_viewport_annotation": True,
        "implementation_loaded": True,
        "is_exact_protocol": True,
        "is_protocol": True,
    }


def test_pytest_playwright_callback_static_type_export(tmp_path, monkeypatch):
    pytest.importorskip(
        "mypy",
        reason=(
            "mypy is optional; install mypy and rerun this test to check the"
            " static type export"
        ),
    )
    from mypy import api as mypy_api

    probe = tmp_path / "callback_type_probe.py"
    probe.write_text(
        textwrap.dedent(
            """
            from rustwright._compat.pytest_playwright import CreateContextCallback

            def use_callback(callback: CreateContextCallback) -> None:
                reveal_type(callback)
                reveal_type(callback(viewport={"width": 1280, "height": 720}))
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MYPYPATH", str(_PYTHON_ROOT))

    stdout, stderr, status = mypy_api.run(
        ["--strict", "--no-error-summary", str(probe)]
    )

    assert status == 0, stdout + stderr
    revealed = [line for line in stdout.splitlines() if "Revealed type is" in line]
    assert any("CreateContextCallback" in line for line in revealed), stdout
    assert any("BrowserContext" in line for line in revealed), stdout
    assert not any("Any" in line or "builtins.object" in line for line in revealed)


def test_repeated_enable_is_idempotent_and_preserves_alias_identity():
    report = _run_probe(
        """
        import importlib
        import json
        import sys

        import rustwright

        tracked = [
            "playwright",
            "playwright.sync_api",
            "patchright",
            "patchright.async_api",
            "cloakbrowser",
            "pytest_playwright",
            "playwright.pytest_plugin",
            "patchright.pytest_plugin",
            "pytest_playwright.pytest_playwright",
        ]
        first = rustwright.enable_playwright_compat()
        child = importlib.import_module("playwright.sync_api")
        target = importlib.import_module("rustwright._compat.playwright.sync_api")
        second = rustwright.enable_playwright_compat()
        reloaded = importlib.reload(child)

        print(json.dumps({
            "all_registered": all(name in sys.modules for name in tracked),
            "child_is_target": child is target,
            "reload_identity": reloaded is child,
            "results_equal": first == second,
            "registered_result": all(
                name in second.registered_aliases for name in tracked
            ),
            "skipped": list(second.skipped_aliases),
        }, sort_keys=True))
        """
    )

    assert report == {
        "all_registered": True,
        "child_is_target": True,
        "registered_result": True,
        "reload_identity": True,
        "results_equal": True,
        "skipped": [],
    }


def test_enable_import_failure_leaves_aliases_and_state_unchanged():
    report = _run_probe(
        """
        import importlib
        import json
        import sys
        from types import ModuleType

        import pytest
        import rustwright
        import rustwright._compat as compat

        canonical_root = importlib.import_module("rustwright._compat.playwright")
        foreign_root = ModuleType("playwright")
        foreign_child = ModuleType("playwright.sync_api")
        sentinel = object()
        foreign_root.sync_api = sentinel
        sys.modules["playwright"] = foreign_root
        sys.modules["playwright.sync_api"] = foreign_child

        aliases = [
            name for name, _target in compat._CORE_ALIASES + compat._PYTEST_ALIASES
        ]
        missing = object()
        alias_snapshot = {
            name: sys.modules.get(name, missing)
            for name in aliases
        }
        state_snapshot = {
            "enabled": compat._ENABLED,
            "pytest_enabled": compat._PYTEST_ALIASES_ENABLED,
            "result": compat._LAST_ENABLE_RESULT,
        }
        real_import_module = importlib.import_module

        def failing_import_module(name, *args, **kwargs):
            if name == "rustwright._compat.playwright._impl._api_structures":
                raise RuntimeError("injected target import failure")
            return real_import_module(name, *args, **kwargs)

        compat.importlib.import_module = failing_import_module
        try:
            rustwright.enable_playwright_compat()
        except RuntimeError as error:
            failure = str(error)
        else:
            failure = None

        print(json.dumps({
            "aliases_unchanged": all(
                sys.modules.get(name, missing) is module
                for name, module in alias_snapshot.items()
            ),
            "canonical_root_preserved": (
                sys.modules.get("rustwright._compat.playwright") is canonical_root
            ),
            "failure": failure,
            "parent_unchanged": foreign_root.sync_api is sentinel,
            "partial_canonical_import_preserved": (
                "rustwright._compat.playwright.__main__" in sys.modules
            ),
            "pytest_preserved": sys.modules.get("pytest") is pytest,
            "state_unchanged": (
                compat._ENABLED is state_snapshot["enabled"]
                and compat._PYTEST_ALIASES_ENABLED is state_snapshot["pytest_enabled"]
                and compat._LAST_ENABLE_RESULT is state_snapshot["result"]
            ),
        }, sort_keys=True))
        """
    )

    assert report == {
        "aliases_unchanged": True,
        "canonical_root_preserved": True,
        "failure": "injected target import failure",
        "parent_unchanged": True,
        "partial_canonical_import_preserved": True,
        "pytest_preserved": True,
        "state_unchanged": True,
    }


def test_enable_rollback_restores_modules_and_parent_attributes():
    report = _run_probe(
        """
        import json
        import sys
        from types import ModuleType

        import rustwright
        import rustwright._compat as compat

        foreign_root = ModuleType("playwright")
        foreign_impl = ModuleType("playwright._impl")
        foreign_child = ModuleType("playwright._impl._api_structures")
        foreign_root._impl = foreign_impl
        foreign_impl._api_structures = foreign_child
        sys.modules["playwright"] = foreign_root
        sys.modules["playwright._impl"] = foreign_impl
        sys.modules["playwright._impl._api_structures"] = foreign_child
        sentinel = object()
        observed = {}

        def fail_publish(event, alias_name=None):
            if event == "enable-after-import":
                canonical_parent = sys.modules[
                    "rustwright._compat.playwright._impl"
                ]
                canonical_parent._api_structures = sentinel
                observed["parent"] = canonical_parent
                observed["target"] = sys.modules[
                    "rustwright._compat.playwright._impl._api_structures"
                ]
            if (
                event == "enable-after-alias-publish"
                and alias_name == "playwright._impl._api_structures"
            ):
                observed["child_was_published"] = (
                    sys.modules.get(alias_name) is observed["target"]
                )
                observed["parent_was_replaced"] = (
                    observed["parent"]._api_structures is observed["target"]
                )
                raise RuntimeError("injected publish failure")

        compat._compat_transaction_hook = fail_publish
        try:
            rustwright.enable_playwright_compat()
        except RuntimeError as error:
            failure = str(error)
        else:
            failure = None

        print(json.dumps({
            "child_restored": (
                sys.modules.get("playwright._impl._api_structures")
                is foreign_child
            ),
            "child_was_published": observed.get("child_was_published", False),
            "enabled": compat._ENABLED,
            "failure": failure,
            "impl_restored": sys.modules.get("playwright._impl") is foreign_impl,
            "parent_restored": observed["parent"]._api_structures is sentinel,
            "parent_was_replaced": observed.get("parent_was_replaced", False),
            "root_restored": sys.modules.get("playwright") is foreign_root,
            "unpublished_absent": "playwright.async_api" not in sys.modules,
        }, sort_keys=True))
        """
    )

    assert report == {
        "child_restored": True,
        "child_was_published": True,
        "enabled": False,
        "failure": "injected publish failure",
        "impl_restored": True,
        "parent_restored": True,
        "parent_was_replaced": True,
        "root_restored": True,
        "unpublished_absent": True,
    }


def test_concurrent_double_enable_is_deterministic():
    report = _run_probe(
        """
        import json
        import sys
        import threading

        import rustwright
        import rustwright._compat as compat

        imported = threading.Barrier(2)
        errors = []
        results = []

        def transaction_hook(event, alias_name=None):
            if event == "enable-after-import":
                imported.wait(timeout=10)

        compat._compat_transaction_hook = transaction_hook

        def enable():
            try:
                results.append(rustwright.enable_playwright_compat())
            except BaseException as error:
                errors.append(repr(error))

        threads = [threading.Thread(target=enable), threading.Thread(target=enable)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        aliases = [name for name, _target in compat._CORE_ALIASES + compat._PYTEST_ALIASES]
        enabled_consistently = (
            compat._ENABLED
            and len(results) == 2
            and results[0] == results[1]
            and all(name in sys.modules for name in aliases)
        )

        print(json.dumps({
            "all_threads_finished": not any(thread.is_alive() for thread in threads),
            "enabled_consistently": enabled_consistently,
            "errors": errors,
        }, sort_keys=True))
        """
    )

    assert report == {
        "all_threads_finished": True,
        "enabled_consistently": True,
        "errors": [],
    }


def test_reenable_upgrades_skipped_pytest_aliases_without_republishing_core():
    report = _run_probe(
        """
        import importlib.util
        import json
        import sys
        from types import ModuleType

        import rustwright
        import rustwright._compat as compat

        canonical_playwright = importlib.import_module(
            "rustwright._compat.playwright"
        )
        importlib.import_module("rustwright._compat.playwright.sync_api")
        canonical_playwright_attribute = object()
        canonical_playwright.sync_api = canonical_playwright_attribute

        foreign_playwright = ModuleType("playwright")
        foreign_playwright_child = ModuleType("playwright.sync_api")
        foreign_pytest_playwright = ModuleType("pytest_playwright")
        foreign_pytest_child = ModuleType(
            "pytest_playwright.pytest_playwright"
        )
        playwright_attribute = object()
        pytest_attribute = object()
        foreign_playwright.sync_api = playwright_attribute
        foreign_pytest_playwright.pytest_playwright = pytest_attribute
        sys.modules["playwright"] = foreign_playwright
        sys.modules["playwright.sync_api"] = foreign_playwright_child
        sys.modules["pytest_playwright"] = foreign_pytest_playwright
        sys.modules["pytest_playwright.pytest_playwright"] = foreign_pytest_child

        aliases = [
            name for name, _target in compat._CORE_ALIASES + compat._PYTEST_ALIASES
        ]
        missing = object()
        module_snapshot = {
            name: sys.modules.get(name, missing)
            for name in aliases
        }

        real_find_spec = importlib.util.find_spec
        pytest_available = False

        def conditional_find_spec(name, *args, **kwargs):
            if name == "pytest" and not pytest_available:
                return None
            return real_find_spec(name, *args, **kwargs)

        compat.importlib.util.find_spec = conditional_find_spec
        first = rustwright.enable_playwright_compat()
        core_root = sys.modules["playwright"]
        before_upgrade = {
            "core_root_replaced": core_root is not foreign_playwright,
            "core_parent_attribute_replaced": (
                canonical_playwright.sync_api is not canonical_playwright_attribute
            ),
            "pytest_loaded": "pytest" in sys.modules,
            "pytest_root_preserved": (
                sys.modules["pytest_playwright"] is foreign_pytest_playwright
            ),
            "skipped": list(first.skipped_aliases),
        }

        pytest_available = True
        second = rustwright.enable_playwright_compat()
        after_upgrade = {
            "core_root_unchanged": sys.modules["playwright"] is core_root,
            "pytest_loaded": "pytest" in sys.modules,
            "pytest_root_replaced": (
                sys.modules["pytest_playwright"] is not foreign_pytest_playwright
            ),
            "pytest_aliases_registered": all(
                sys.modules.get(name) is not module_snapshot[name]
                for name, _target in compat._PYTEST_ALIASES
            ),
            "skipped": list(second.skipped_aliases),
        }

        print(json.dumps({
            "before_upgrade": before_upgrade,
            "after_upgrade": after_upgrade,
        }, sort_keys=True))
        """
    )

    assert report == {
        "after_upgrade": {
            "core_root_unchanged": True,
            "pytest_aliases_registered": True,
            "pytest_loaded": True,
            "pytest_root_replaced": True,
            "skipped": [],
        },
        "before_upgrade": {
            "core_parent_attribute_replaced": True,
            "core_root_replaced": True,
            "pytest_loaded": False,
            "pytest_root_preserved": True,
            "skipped": _PYTEST_ALIASES,
        },
    }


def test_enable_playwright_compat_covers_private_import_paths():
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

    for identity in [
        "sync_generated_page",
        "async_generated_page",
        "patchright_generated_page",
        "viewport_size",
        "patchright_viewport_size",
        "geolocation",
        "storage_state",
        "target_closed_error",
    ]:
        assert report[identity] is True
    assert "sameSite" in report["set_cookie_param_keys"]
    assert "certPath" in report["client_certificate_keys"]


def test_browser_context_args_fixture_is_session_scoped():
    import rustwright.pytest_plugin as plugin

    fixture = plugin.browser_context_args
    marker = getattr(fixture, "_pytestfixturefunction", None) or getattr(
        fixture, "_fixture_function_marker", None
    )
    assert marker is not None, "browser_context_args is not a pytest fixture"
    assert marker.scope == "session"


def _run_compat_pytest(tmp_path, target, plugin_name, *extra_args, env=None):
    compat_env = dict(os.environ if env is None else env)
    compat_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    plugin_args = [] if plugin_name is None else ["-p", plugin_name]
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import rustwright; "
                "rustwright.enable_playwright_compat(); "
                "from pytest import console_main; "
                "raise SystemExit(console_main())"
            ),
            str(target),
            "-p",
            "no:cacheprovider",
            "-q",
            *extra_args,
            *plugin_args,
        ],
        cwd=tmp_path,
        env=compat_env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    "plugin_name",
    [
        "playwright.pytest_plugin",
        "patchright.pytest_plugin",
        "pytest_playwright.pytest_playwright",
    ],
)
def test_pytest_loads_each_eager_plugin_alias(tmp_path, plugin_name):
    test_file = tmp_path / "test_alias_plugin.py"
    test_file.write_text(
        textwrap.dedent(
            """
            def test_alias_plugin_fixture(browser_context_args):
                assert isinstance(browser_context_args, dict)
            """
        ),
        encoding="utf-8",
    )

    result = _run_compat_pytest(tmp_path, test_file, plugin_name)
    assert result.returncode == 0, f"{plugin_name}\n{result.stdout}{result.stderr}"
    assert "1 passed" in result.stdout, result.stdout


def test_pytest_loads_root_plugin_alias(tmp_path):
    test_file = tmp_path / "test_root_alias_plugin.py"
    test_file.write_text(
        textwrap.dedent(
            """
            def test_root_alias_plugin_fixture(browser_context_args):
                assert isinstance(browser_context_args, dict)
            """
        ),
        encoding="utf-8",
    )

    result = _run_compat_pytest(tmp_path, test_file, "pytest_playwright")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout, result.stdout


def test_pytest_plugin_tolerates_foreign_option_registration(tmp_path):
    foreign = tmp_path / "foreign_options_plugin.py"
    foreign.write_text(
        textwrap.dedent(
            """
            def pytest_addoption(parser):
                parser.addoption("--base-url", default=None, help="foreign base url")
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
    result = _run_compat_pytest(
        tmp_path,
        test_file,
        "patchright.pytest_plugin",
        "-p",
        "foreign_options_plugin",
        "--browser",
        "chromium",
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already added" not in result.stderr
    assert "1 passed" in result.stdout, result.stdout


def test_conftest_pytest_plugins_alias_collects(tmp_path):
    conftest = tmp_path / "conftest.py"
    conftest.write_text(
        textwrap.dedent(
            """
            import pytest

            pytest_plugins = ["playwright.pytest_plugin"]

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
    result = _run_compat_pytest(tmp_path, test_file, None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "ScopeMismatch" not in result.stdout + result.stderr



def test_opt_in_playwright_connect_uses_exact_unsupported_contract_sync_and_async():
    report = _run_probe(
        """
        import asyncio
        import json

        import rustwright

        rustwright.enable_playwright_compat()
        from playwright.async_api import async_playwright
        from playwright.sync_api import sync_playwright

        expected = (
            "BrowserType.connect: Rustwright does not support the Playwright wire protocol "
            "(playwright run-server or BrowserType.launchServer). Use "
            "chromium.connect_over_cdp() with a raw Chromium CDP endpoint such as "
            "http://browser:9222. See "
            "https://github.com/Skyvern-AI/rustwright/blob/main/docs/REMOTE_BROWSERS.md"
        )

        sync_messages = {}
        with sync_playwright() as playwright:
            for name in ("chromium", "firefox", "webkit"):
                try:
                    getattr(playwright, name).connect("ws://127.0.0.1:1/devtools/browser/test")
                except Exception as error:
                    sync_messages[name] = str(error)

        async def collect_async():
            messages = {}
            async with async_playwright() as playwright:
                for name in ("chromium", "firefox", "webkit"):
                    try:
                        await getattr(playwright, name).connect(
                            "ws://127.0.0.1:1/devtools/browser/test"
                        )
                    except Exception as error:
                        messages[name] = str(error)
            return messages

        print(json.dumps({
            "expected": expected,
            "sync": sync_messages,
            "async": asyncio.run(collect_async()),
        }, sort_keys=True))
        """
    )
    expected = report["expected"]
    assert report["sync"] == {name: expected for name in ("chromium", "firefox", "webkit")}
    assert report["async"] == {name: expected for name in ("chromium", "firefox", "webkit")}
def test_conftest_root_pytest_plugin_alias_collects(tmp_path):
    conftest = tmp_path / "conftest.py"
    conftest.write_text(
        'pytest_plugins = ["pytest_playwright"]\n',
        encoding="utf-8",
    )
    test_file = tmp_path / "test_root_scope.py"
    test_file.write_text(
        textwrap.dedent(
            """
            def test_root_fixture_available(browser_context_args):
                assert isinstance(browser_context_args, dict)
            """
        ),
        encoding="utf-8",
    )

    result = _run_compat_pytest(tmp_path, test_file, None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
