from __future__ import annotations

import argparse
import base64
from html import escape
import io
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Sequence
from urllib.parse import urlparse
from urllib import request as url_request
import zipfile

from .sync_api import sync_playwright


SUPPORTED_INSTALL_BROWSERS = {"chromium", "chrome", "msedge"}
UNSUPPORTED_INSTALL_BROWSERS = {"firefox", "ff", "webkit", "wk"}
BRANDED_INSTALL_BROWSERS = {"chrome": "Chrome", "msedge": "Microsoft Edge"}
AGENT_VERBS = {
    "open",
    "navigate",
    "back",
    "reload",
    "snapshot",
    "click",
    "fill",
    "type",
    "select",
    "hover",
    "press",
    "wait",
    "tabs",
    "screenshot",
    "eval",
    "status",
    "close",
}
_AGENT_GLOBAL_VALUE_FLAGS = {
    "--session",
    "--timeout-ms",
    "--navigation-timeout-ms",
    "--executable-path",
    "--browser-arg",
}
_AGENT_GLOBAL_BOOLEAN_FLAGS = {"--json", "--headed", "--allow-eval"}
_SCREENSHOT_VALUE_FLAGS = _AGENT_GLOBAL_VALUE_FLAGS | {
    "-b",
    "--browser",
    "--channel",
    "--color-scheme",
    "--device",
    "--geolocation",
    "--load-storage",
    "--lang",
    "--proxy-server",
    "--proxy-bypass",
    "--save-har",
    "--save-har-glob",
    "--save-storage",
    "--timezone",
    "--timeout",
    "--user-agent",
    "--user-data-dir",
    "--viewport-size",
    "--wait-for-selector",
    "--wait-for-timeout",
    "--ref",
    "--type",
    "--quality",
}
CHROME_FOR_TESTING_URL = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"
LINUX_TOOLS_DEPS = [
    "xvfb",
    "fonts-noto-color-emoji",
    "fonts-unifont",
    "libfontconfig1",
    "libfreetype6",
    "xfonts-scalable",
    "fonts-liberation",
    "fonts-ipafont-gothic",
    "fonts-wqy-zenhei",
    "fonts-tlwg-loma-otf",
    "fonts-freefont-ttf",
]
LINUX_CHROMIUM_DEPS = [
    "libatk-bridge2.0-0",
    "libatk1.0-0",
    "libatspi2.0-0",
    "libcairo2",
    "libcups2",
    "libdbus-1-3",
    "libdrm2",
    "libgbm1",
    "libglib2.0-0",
    "libnspr4",
    "libnss3",
    "libpango-1.0-0",
    "libx11-6",
    "libxcb1",
    "libxcomposite1",
    "libxdamage1",
    "libxext6",
    "libxfixes3",
    "libxkbcommon0",
    "libxrandr2",
]
LINUX_CHROMIUM_DEPS_UBUNTU_24 = [
    "libasound2t64",
    "libatk-bridge2.0-0t64",
    "libatk1.0-0t64",
    "libatspi2.0-0t64",
    "libcups2t64",
    "libglib2.0-0t64",
]
LINUX_CHROMIUM_DEPS_LEGACY_AUDIO = ["libasound2"]


def _version() -> str:
    try:
        return metadata.version("rustwright")
    except metadata.PackageNotFoundError:
        return "0.1.1"


def _normal_browser_name(name: str) -> str:
    aliases = {"cr": "chromium"}
    return aliases.get(name, name)


def _default_program_name() -> str:
    candidate = Path(sys.argv[0]).stem.lower()
    if candidate in {"playwright", "patchright", "rustwright"}:
        return candidate
    return "playwright"


def _install_parser(program: str = "playwright") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{program} install", add_help=True)
    parser.add_argument("browsers", nargs="*", help="Browser names to check, defaults to chromium.")
    parser.add_argument("--with-deps", action="store_true", help="Accepted for Playwright CLI compatibility.")
    parser.add_argument("--dry-run", action="store_true", help="Print the resolved browser path without launching a download.")
    parser.add_argument("--force", action="store_true", help="Accepted for Playwright CLI compatibility.")
    parser.add_argument("--only-shell", action="store_true", help="Accepted for Playwright CLI compatibility.")
    parser.add_argument("--no-shell", action="store_true", help="Accepted for Playwright CLI compatibility.")
    return parser


def _install_deps_parser(program: str = "playwright") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{program} install-deps", add_help=True)
    parser.add_argument("browsers", nargs="*", help="Browser names to check, defaults to chromium.")
    parser.add_argument("--dry-run", action="store_true", help="Accepted for Playwright CLI compatibility.")
    return parser


def _uninstall_parser(program: str = "playwright") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{program} uninstall",
        description=(
            "Removes Rustwright-managed Chromium browser cache directories. "
            "Branded channels and system browsers are never removed."
        ),
        add_help=True,
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Accepted for Playwright CLI compatibility; Rustwright only removes its managed Chromium cache.",
    )
    return parser


def _show_trace_parser(program: str = "playwright") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{program} show-trace", add_help=False)
    parser.add_argument("trace", nargs="?", help="Trace zip file to inspect.")
    parser.add_argument("-b", "--browser", default="chromium", help="Accepted for Playwright CLI compatibility.")
    parser.add_argument("-h", "--host", help="Accepted for Playwright CLI compatibility.")
    parser.add_argument("-p", "--port", help="Accepted for Playwright CLI compatibility.")
    parser.add_argument("--stdin", action="store_true", help="Read trace paths from stdin.")
    parser.add_argument("--output", help="Write the static Rustwright trace viewer HTML to this path.")
    parser.add_argument("--help", action="help", help="Show this help message and exit.")
    return parser


def _trace_parser(program: str = "playwright") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{program} trace",
        description="inspect local Rustwright trace files from the command line",
        add_help=True,
    )
    subparsers = parser.add_subparsers(dest="trace_command")

    actions = subparsers.add_parser("actions", help="list actions in the trace")
    actions.add_argument("trace", nargs="?", help="Trace zip file to inspect.")
    actions.add_argument("--grep", help="filter actions by method, class, params, or error text")
    actions.add_argument("--errors-only", action="store_true", help="only show failed actions")

    requests = subparsers.add_parser("requests", help="show network requests")
    requests.add_argument("trace", nargs="?", help="Trace zip file to inspect.")
    requests.add_argument("--grep", help="filter by URL text")
    requests.add_argument("--method", help="filter by HTTP method")
    requests.add_argument("--status", help="filter by status code")
    requests.add_argument("--failed", action="store_true", help="only show failed requests (status >= 400)")

    errors = subparsers.add_parser("errors", help="show action errors")
    errors.add_argument("trace", nargs="?", help="Trace zip file to inspect.")

    return parser


def _add_open_options(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("-b", "--browser", default="chromium", help="browser to use, one of cr, chromium, ff, firefox, wk, webkit")
    parser.add_argument("--block-service-workers", action="store_true", help="block service workers")
    parser.add_argument("--channel", help='Chromium distribution channel, "chrome", "chrome-beta", "msedge-dev", etc')
    parser.add_argument("--color-scheme", help='emulate preferred color scheme, "light" or "dark"')
    parser.add_argument("--device", help='emulate device, for example "iPhone 11"')
    parser.add_argument("--geolocation", help='specify geolocation coordinates, for example "37.819722,-122.478611"')
    parser.add_argument("--ignore-https-errors", action="store_true", help="ignore https errors")
    parser.add_argument("--load-storage", help="load context storage state from the file, previously saved with --save-storage")
    parser.add_argument("--lang", help='specify language / locale, for example "en-GB"')
    parser.add_argument("--proxy-server", help='specify proxy server, for example "http://myproxy:3128" or "socks5://myproxy:8080"')
    parser.add_argument("--proxy-bypass", help='comma-separated domains to bypass proxy, for example ".com,chromium.org,.domain.com"')
    parser.add_argument("--save-har", help="save HAR file with all network activity at the end")
    parser.add_argument("--save-har-glob", help="filter entries in the HAR by matching url against this glob pattern")
    parser.add_argument("--save-storage", help="save context storage state at the end, for later use with --load-storage")
    parser.add_argument("--timezone", help='time zone to emulate, for example "Europe/Rome"')
    parser.add_argument("--timeout", help="timeout for Playwright actions in milliseconds, no timeout by default")
    parser.add_argument("--user-agent", help="specify user agent string")
    parser.add_argument("--user-data-dir", help="use the specified user data directory instead of a new context")
    parser.add_argument("--viewport-size", help='specify browser viewport size in pixels, for example "1280, 720"')
    return parser


def _default_codegen_target() -> str:
    requested = os.environ.get("PW_LANG_NAME")
    return requested if requested in {"python", "python-async", "python-pytest"} else "python"


def _codegen_parser(program: str = "playwright") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{program} codegen", add_help=True)
    parser.add_argument("url", nargs="?", help="URL or local file to include in the generated starter script.")
    parser.add_argument("-o", "--output", help="saves the generated script to a file")
    parser.add_argument(
        "--target",
        default=_default_codegen_target(),
        help="language to generate, one of python, python-async, python-pytest",
    )
    parser.add_argument("--test-id-attribute", help="use the specified attribute to generate data test ID selectors")
    return _add_open_options(parser)


def _screenshot_parser(program: str = "playwright") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{program} screenshot", add_help=True)
    parser.add_argument("url", help="URL or local file to capture.")
    parser.add_argument("filename", help="Screenshot output path.")
    _add_open_options(parser)
    parser.add_argument("--wait-for-selector", help="wait for selector before taking a screenshot")
    parser.add_argument("--wait-for-timeout", help="wait for timeout in milliseconds before taking a screenshot")
    parser.add_argument("--full-page", action="store_true", help="whether to take a full page screenshot")
    return parser


def _pdf_parser(program: str = "playwright") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{program} pdf", add_help=True)
    parser.add_argument("url", help="URL or local file to save as PDF.")
    parser.add_argument("filename", help="PDF output path.")
    _add_open_options(parser)
    parser.add_argument("--paper-format", help="paper format: Letter, Legal, Tabloid, Ledger, A0, A1, A2, A3, A4, A5, A6")
    parser.add_argument("--wait-for-selector", help="wait for given selector before saving as pdf")
    parser.add_argument("--wait-for-timeout", help="wait for given timeout in milliseconds before saving as pdf")
    return parser


def _requested_browsers(values: Sequence[str]) -> list[str]:
    return [_normal_browser_name(str(value)) for value in values] or ["chromium"]


def _check_supported_browsers(browsers: Sequence[str]) -> tuple[bool, str | None]:
    for browser in browsers:
        if browser in UNSUPPORTED_INSTALL_BROWSERS:
            return False, (
                f"{browser} is not implemented; Rustwright currently supports Chromium over direct CDP."
            )
        if browser not in SUPPORTED_INSTALL_BROWSERS:
            return False, f"Unknown browser for Rustwright install compatibility: {browser}"
    return True, None


def _browser_cache_dir() -> Path:
    explicit = os.environ.get("RUSTWRIGHT_BROWSERS_PATH")
    if explicit:
        return Path(explicit).expanduser()
    playwright_cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if playwright_cache and playwright_cache != "0":
        return Path(playwright_cache).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library/Caches/ms-playwright"
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        return Path(local_app_data).expanduser() / "ms-playwright" if local_app_data else home / "AppData/Local/ms-playwright"
    return home / ".cache/ms-playwright"


def _chrome_for_testing_platform() -> str:
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        return "mac-arm64" if machine in {"arm64", "aarch64"} else "mac-x64"
    if sys.platform == "win32":
        return "win32" if machine in {"x86", "i386"} else "win64"
    if sys.platform.startswith("linux"):
        if machine in {"x86_64", "amd64"}:
            return "linux64"
        raise RuntimeError(
            "Chrome for Testing downloads are only supported for linux x86_64. "
            "Set RUSTWRIGHT_CHROMIUM to a compatible Chromium executable."
        )
    raise RuntimeError(f"Chrome for Testing downloads are not known for platform {sys.platform!r}")


def _chrome_for_testing_executable(platform_name: str) -> Path:
    if platform_name == "mac-arm64":
        return Path("chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
    if platform_name == "mac-x64":
        return Path("chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
    if platform_name == "linux64":
        return Path("chrome-linux64/chrome")
    if platform_name == "win64":
        return Path("chrome-win64/chrome.exe")
    if platform_name == "win32":
        return Path("chrome-win32/chrome.exe")
    raise RuntimeError(f"Unsupported Chrome for Testing platform {platform_name!r}")


def _channel_binary_names(browser: str) -> tuple[str, ...]:
    if browser == "chrome":
        return ("google-chrome", "google-chrome-stable", "chrome")
    if browser == "msedge":
        return ("msedge", "microsoft-edge")
    return ()


def _channel_candidate_paths(browser: str) -> list[Path]:
    candidates: list[Path] = []
    if sys.platform == "darwin":
        app_name = "Google Chrome.app" if browser == "chrome" else "Microsoft Edge.app"
        binary_name = app_name.removesuffix(".app")
        for root in [Path("/Applications"), Path.home() / "Applications"]:
            candidates.append(root / app_name / "Contents/MacOS" / binary_name)
    elif sys.platform == "win32":
        relative = (
            Path("Google/Chrome/Application/chrome.exe")
            if browser == "chrome"
            else Path("Microsoft/Edge/Application/msedge.exe")
        )
        for env_key in ["PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"]:
            root = os.environ.get(env_key)
            if root:
                candidates.append(Path(root) / relative)
    else:
        for name in _channel_binary_names(browser):
            candidates.append(Path("/usr/bin") / name)
            candidates.append(Path("/snap/bin") / name)
    return candidates


def _find_channel_executable(browser: str) -> str:
    for name in _channel_binary_names(browser):
        found = shutil.which(name)
        if found:
            return found
    for candidate in _channel_candidate_paths(browser):
        if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return str(candidate)
    return ""


def _ensure_browser_executable(executable: Path) -> None:
    if os.name == "nt":
        return
    try:
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError as exc:
        raise RuntimeError(f"Could not mark Chromium executable at {executable} as executable: {exc}") from exc


def _chrome_for_testing_download(platform_name: str | None = None) -> dict[str, str]:
    platform_name = platform_name or _chrome_for_testing_platform()
    with url_request.urlopen(CHROME_FOR_TESTING_URL, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    stable = payload.get("channels", {}).get("Stable", {})
    version = str(stable.get("version") or "")
    downloads = stable.get("downloads", {}).get("chrome", [])
    for entry in downloads:
        if entry.get("platform") == platform_name and entry.get("url"):
            return {"version": version, "url": str(entry["url"]), "platform": platform_name}
    raise RuntimeError(f"Could not find a Chrome for Testing download for {platform_name}")


def _download_chromium(*, force: bool = False, dry_run: bool = False) -> dict[str, object]:
    download = _chrome_for_testing_download()
    platform_name = download["platform"]
    install_dir = _browser_cache_dir() / f"chromium-{download['version']}"
    executable = install_dir / _chrome_for_testing_executable(platform_name)
    if executable.is_file() and not force:
        _ensure_browser_executable(executable)
        return {"executable": str(executable), "downloaded": False, "url": download["url"], "dry_run": dry_run}
    if dry_run:
        return {"executable": str(executable), "downloaded": False, "url": download["url"], "dry_run": True}
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    with url_request.urlopen(download["url"], timeout=120) as response:
        archive_bytes = response.read()
    with tempfile.TemporaryDirectory(prefix="rustwright-chromium-", dir=str(install_dir.parent)) as tmp:
        tmp_dir = Path(tmp)
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            archive.extractall(tmp_dir)
        if force and install_dir.exists():
            shutil.rmtree(install_dir)
        if install_dir.exists():
            shutil.rmtree(tmp_dir)
        else:
            tmp_dir.rename(install_dir)
    if not executable.is_file():
        raise RuntimeError(f"Downloaded Chrome for Testing but did not find executable at {executable}")
    _ensure_browser_executable(executable)
    return {"executable": str(executable), "downloaded": True, "url": download["url"], "dry_run": False}


def _read_linux_os_release() -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        return data
    for line in text.splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        data[key] = value.strip().strip('"')
    return data


def _linux_dependency_packages(os_release: dict[str, str] | None = None) -> list[str]:
    os_release = os_release if os_release is not None else _read_linux_os_release()
    distro = (os_release.get("ID") or "").lower()
    version = os_release.get("VERSION_ID") or ""
    packages = list(LINUX_TOOLS_DEPS)
    chromium = list(LINUX_CHROMIUM_DEPS)
    if distro == "ubuntu" and version.startswith("24."):
        replaced = {"libatk-bridge2.0-0", "libatk1.0-0", "libatspi2.0-0", "libcups2", "libglib2.0-0"}
        chromium = [package for package in chromium if package not in replaced]
        chromium.extend(LINUX_CHROMIUM_DEPS_UBUNTU_24)
    else:
        chromium.extend(LINUX_CHROMIUM_DEPS_LEGACY_AUDIO)
        if distro == "ubuntu" and version.startswith("22."):
            chromium.append("libwayland-client0")
    return sorted(dict.fromkeys(packages + chromium))


def _dependency_install_commands(packages: Sequence[str]) -> list[list[str]]:
    return [
        ["apt-get", "update"],
        ["apt-get", "install", "-y", "--no-install-recommends", *packages],
    ]


def _command_text(command: Sequence[str], *, use_sudo: bool) -> str:
    parts = ["sudo", *command] if use_sudo else list(command)
    return " ".join(parts)


def _install_linux_deps(*, dry_run: bool) -> int:
    packages = _linux_dependency_packages()
    use_sudo = hasattr(os, "geteuid") and os.geteuid() != 0
    commands = _dependency_install_commands(packages)
    if dry_run:
        for command in commands:
            print(_command_text(command, use_sudo=use_sudo))
        return 0
    executable = shutil.which("apt-get")
    if executable is None:
        print("Rustwright install-deps currently supports apt-get based Linux distributions.", file=sys.stderr)
        return 1
    for command in commands:
        runnable = command
        if use_sudo:
            if shutil.which("sudo") is None:
                print("Installing browser dependencies requires root or sudo.", file=sys.stderr)
                return 1
            runnable = ["sudo", *command]
        subprocess.check_call(runnable)
    return 0


def _chromium_executable_path() -> str:
    with sync_playwright() as playwright:
        return playwright.chromium.executable_path


def _print_branded_install_dry_run(browser: str, *, fallback: str) -> None:
    display_name = BRANDED_INSTALL_BROWSERS[browser]
    print(f"{display_name} (rustwright {browser} channel compatibility)")
    print("  Install location:    <system>")
    executable = _find_channel_executable(browser)
    if executable:
        print(f"  Existing executable: {executable}")
    else:
        print("  Existing executable: <not found>")
        if fallback:
            print(f"  Rustwright fallback: {fallback}")
        print("  Note: stable channel launch falls back to Rustwright Chromium when the branded browser is absent.")


def _install_branded_browser(browser: str, args: argparse.Namespace) -> int:
    display_name = BRANDED_INSTALL_BROWSERS[browser]
    fallback = _chromium_executable_path()
    if args.dry_run:
        _print_branded_install_dry_run(browser, fallback=fallback)
        return 0
    executable = _find_channel_executable(browser)
    if executable and not args.force:
        print(f"Rustwright found {display_name} executable: {executable}")
        return 0
    if fallback and not args.force:
        print(
            f"Rustwright did not find a system {display_name} executable; "
            f"stable channel launch will use Rustwright Chromium fallback: {fallback}"
        )
        return 0
    try:
        result = _download_chromium(force=bool(args.force), dry_run=False)
    except Exception as exc:
        print(
            f"Could not prepare a Chromium fallback for {display_name}. Install {display_name} manually "
            "or set executable_path/RUSTWRIGHT_CHROMIUM. "
            f"Details: {exc}",
            file=sys.stderr,
        )
        return 1
    print(
        f"Rustwright prepared Chromium fallback for {display_name} channel compatibility: "
        f"{result['executable']}"
    )
    return 0


def _parse_viewport_size(value: str) -> dict[str, int]:
    try:
        width_text, height_text = value.split(",", 1)
        width = int(width_text.strip())
        height = int(height_text.strip())
    except (TypeError, ValueError) as exc:
        raise ValueError('Invalid viewport size format: use "width,height", for example --viewport-size="800,600"') from exc
    if width <= 0 or height <= 0:
        raise ValueError('Invalid viewport size format: use "width,height", for example --viewport-size="800,600"')
    return {"width": width, "height": height}


def _parse_geolocation(value: str) -> dict[str, float]:
    try:
        latitude_text, longitude_text = value.split(",", 1)
        latitude = float(latitude_text.strip())
        longitude = float(longitude_text.strip())
    except (TypeError, ValueError) as exc:
        raise ValueError('Invalid geolocation format, should be "lat,long". For example --geolocation="37.819722,-122.478611"') from exc
    return {"latitude": latitude, "longitude": longitude}


def _parse_cli_timeout(value: str | None) -> float:
    if value in {None, ""}:
        return 0.0
    try:
        timeout = float(str(value))
    except ValueError as exc:
        raise ValueError("Invalid timeout value; expected milliseconds") from exc
    if timeout < 0:
        raise ValueError("Invalid timeout value; expected milliseconds")
    return timeout


def _is_chromium_browser_name(browser_name: str) -> bool:
    return browser_name in {"cr", "chromium", "chrome", "msedge"}


def _open_url(value: str | None) -> str | None:
    if not value:
        return None
    if Path(value).exists():
        return Path(value).resolve().as_uri()
    if value.startswith(("http", "file://", "about:", "data:")):
        return value
    return f"http://{value}"


def _open_launch_and_context_options(
    args: argparse.Namespace,
    playwright: object,
    *,
    headless: bool | None = None,
) -> tuple[dict[str, object], dict[str, object], float]:
    browser_name = str(args.browser or "chromium")
    context_options: dict[str, object] = {}
    if args.device:
        devices = getattr(playwright, "devices", {})
        if args.device not in devices:
            available = "\n".join(f'  "{name}"' for name in sorted(devices))
            raise ValueError(f"Device descriptor not found: '{args.device}', available devices are:\n{available}")
        device_options = dict(devices[args.device])
        context_options.update(device_options)
        browser_name = str(device_options.get("default_browser_type") or browser_name)
    if not _is_chromium_browser_name(browser_name):
        raise ValueError(f"{browser_name} is not implemented; Rustwright currently supports Chromium over direct CDP.")
    if args.color_scheme:
        if args.color_scheme not in {"light", "dark"}:
            raise ValueError('Invalid color scheme, should be one of "light", "dark"')
        context_options["color_scheme"] = args.color_scheme
    if args.block_service_workers:
        context_options["service_workers"] = "block"
    if args.viewport_size:
        context_options["viewport"] = _parse_viewport_size(args.viewport_size)
    if args.geolocation:
        context_options["geolocation"] = _parse_geolocation(args.geolocation)
        context_options["permissions"] = ["geolocation"]
    if args.user_agent:
        context_options["user_agent"] = args.user_agent
    if args.lang:
        context_options["locale"] = args.lang
    if args.timezone:
        context_options["timezone_id"] = args.timezone
    if args.load_storage:
        context_options["storage_state"] = args.load_storage
    if args.ignore_https_errors:
        context_options["ignore_https_errors"] = True
    if args.save_har:
        context_options["record_har_path"] = str(Path(args.save_har).resolve())
        context_options["record_har_mode"] = "minimal"
        context_options["service_workers"] = "block"
        if args.save_har_glob:
            context_options["record_har_url_filter"] = args.save_har_glob
    launch_headless = bool(os.environ.get("PWTEST_CLI_HEADLESS")) if headless is None else bool(headless)
    launch_options: dict[str, object] = {"headless": launch_headless}
    executable_path = os.environ.get("PWTEST_CLI_EXECUTABLE_PATH")
    if executable_path:
        launch_options["executable_path"] = executable_path
    if args.channel:
        launch_options["channel"] = args.channel
    if args.proxy_server:
        proxy: dict[str, str] = {"server": args.proxy_server}
        if args.proxy_bypass:
            proxy["bypass"] = args.proxy_bypass
        launch_options["proxy"] = proxy
    return launch_options, context_options, _parse_cli_timeout(args.timeout)


def _open_page(context: object, url: str | None) -> object:
    pages = list(getattr(context, "pages", []) or [])
    page = pages[0] if pages else context.new_page()
    normalized_url = _open_url(url)
    if normalized_url:
        page.goto(normalized_url)
    return page


def _close_open_browser(browser: object, context: object, *, save_storage: str | None, close_context: bool) -> None:
    if save_storage:
        context.storage_state(path=save_storage)
    if close_context:
        context.close()
    browser.close()


def _launch_cli_context(args: argparse.Namespace, playwright: object, *, headless: bool | None) -> tuple[object, object]:
    launch_options, context_options, timeout = _open_launch_and_context_options(args, playwright, headless=headless)
    if args.user_data_dir:
        context = playwright.chromium.launch_persistent_context(
            args.user_data_dir,
            **launch_options,
            **context_options,
        )
        browser = context.browser
        if browser is None:
            raise RuntimeError("Rustwright open could not resolve the launched browser.")
    else:
        browser = playwright.chromium.launch(**launch_options)
        context = browser.new_context(**context_options)
    context.set_default_timeout(timeout)
    context.set_default_navigation_timeout(timeout)
    return browser, context


def _wait_for_capture_options(page: object, args: argparse.Namespace) -> None:
    if getattr(args, "wait_for_selector", None):
        print(f"Waiting for selector {args.wait_for_selector}...")
        page.wait_for_selector(args.wait_for_selector)
    if getattr(args, "wait_for_timeout", None):
        wait_for_timeout = _parse_cli_timeout(args.wait_for_timeout)
        print(f"Waiting for timeout {int(wait_for_timeout)}...")
        page.wait_for_timeout(wait_for_timeout)


def _codegen_options(args: argparse.Namespace, playwright: object) -> tuple[dict[str, object], dict[str, object], float]:
    launch_options, context_options, timeout = _open_launch_and_context_options(args, playwright, headless=False)
    context_options = {key: value for key, value in context_options.items() if key != "default_browser_type"}
    return launch_options, context_options, timeout


def _assignment_lines(name: str, values: dict[str, object], *, indent: str = "    ") -> list[str]:
    if values:
        return [f"{indent}{name} = {values!r}"]
    return [f"{indent}{name} = {{}}"]


def _generated_sync_code(
    *,
    launch_options: dict[str, object],
    context_options: dict[str, object],
    timeout: float,
    url: str | None,
    user_data_dir: str | None,
    save_storage: str | None,
    test_id_attribute: str | None,
) -> str:
    lines = ["from playwright.sync_api import sync_playwright", "", "", "with sync_playwright() as p:"]
    if test_id_attribute:
        lines.append(f"    p.selectors.set_test_id_attribute({test_id_attribute!r})")
    lines.extend(_assignment_lines("launch_options", launch_options))
    lines.extend(_assignment_lines("context_options", context_options))
    if user_data_dir:
        lines.append(
            f"    context = p.chromium.launch_persistent_context({str(user_data_dir)!r}, **launch_options, **context_options)"
        )
        lines.append("    page = context.pages[0] if context.pages else context.new_page()")
    else:
        lines.append("    browser = p.chromium.launch(**launch_options)")
        lines.append("    context = browser.new_context(**context_options)")
        lines.append("    page = context.new_page()")
    if timeout:
        lines.append(f"    context.set_default_timeout({timeout!r})")
        lines.append(f"    context.set_default_navigation_timeout({timeout!r})")
    if url:
        lines.append(f"    page.goto({url!r})")
    if save_storage:
        lines.append(f"    context.storage_state(path={str(save_storage)!r})")
    lines.append("    context.close()")
    if not user_data_dir:
        lines.append("    browser.close()")
    return "\n".join(lines) + "\n"


def _generated_async_code(
    *,
    launch_options: dict[str, object],
    context_options: dict[str, object],
    timeout: float,
    url: str | None,
    user_data_dir: str | None,
    save_storage: str | None,
    test_id_attribute: str | None,
) -> str:
    lines = [
        "import asyncio",
        "from playwright.async_api import async_playwright",
        "",
        "",
        "async def main():",
        "    async with async_playwright() as p:",
    ]
    if test_id_attribute:
        lines.append(f"        p.selectors.set_test_id_attribute({test_id_attribute!r})")
    lines.extend(_assignment_lines("launch_options", launch_options, indent="        "))
    lines.extend(_assignment_lines("context_options", context_options, indent="        "))
    if user_data_dir:
        lines.append(
            f"        context = await p.chromium.launch_persistent_context({str(user_data_dir)!r}, **launch_options, **context_options)"
        )
        lines.append("        page = context.pages[0] if context.pages else await context.new_page()")
    else:
        lines.append("        browser = await p.chromium.launch(**launch_options)")
        lines.append("        context = await browser.new_context(**context_options)")
        lines.append("        page = await context.new_page()")
    if timeout:
        lines.append(f"        context.set_default_timeout({timeout!r})")
        lines.append(f"        context.set_default_navigation_timeout({timeout!r})")
    if url:
        lines.append(f"        await page.goto({url!r})")
    if save_storage:
        lines.append(f"        await context.storage_state(path={str(save_storage)!r})")
    lines.append("        await context.close()")
    if not user_data_dir:
        lines.append("        await browser.close()")
    lines.extend(["", "", "asyncio.run(main())"])
    return "\n".join(lines) + "\n"


def _generated_pytest_code(
    *,
    context_options: dict[str, object],
    url: str | None,
    save_storage: str | None,
    test_id_attribute: str | None,
) -> str:
    lines = ["import pytest", "from playwright.sync_api import Page, Playwright", ""]
    if context_options:
        lines.append(f"@pytest.mark.browser_context_args(**{context_options!r})")
    fixture_args = "page: Page, playwright: Playwright" if test_id_attribute else "page: Page"
    lines.append(f"def test_codegen({fixture_args}) -> None:")
    if test_id_attribute:
        lines.append(f"    playwright.selectors.set_test_id_attribute({test_id_attribute!r})")
    if url:
        lines.append(f"    page.goto({url!r})")
    else:
        lines.append("    page.goto('about:blank')")
    if save_storage:
        lines.append(f"    page.context.storage_state(path={str(save_storage)!r})")
    return "\n".join(lines) + "\n"


def _generate_codegen_source(args: argparse.Namespace, playwright: object) -> str:
    target = str(args.target or _default_codegen_target())
    if target not in {"python", "python-async", "python-pytest"}:
        raise ValueError(
            f"Rustwright codegen currently supports Python targets only; unsupported target: {target}"
        )
    launch_options, context_options, timeout = _codegen_options(args, playwright)
    url = _open_url(args.url)
    if target == "python":
        return _generated_sync_code(
            launch_options=launch_options,
            context_options=context_options,
            timeout=timeout,
            url=url,
            user_data_dir=args.user_data_dir,
            save_storage=args.save_storage,
            test_id_attribute=args.test_id_attribute,
        )
    if target == "python-async":
        return _generated_async_code(
            launch_options=launch_options,
            context_options=context_options,
            timeout=timeout,
            url=url,
            user_data_dir=args.user_data_dir,
            save_storage=args.save_storage,
            test_id_attribute=args.test_id_attribute,
        )
    return _generated_pytest_code(
        context_options=context_options,
        url=url,
        save_storage=args.save_storage,
        test_id_attribute=args.test_id_attribute,
    )


def install(argv: Sequence[str], *, program: str = "playwright") -> int:
    args = _install_parser(program).parse_args(list(argv))
    browsers = _requested_browsers(args.browsers)
    ok, message = _check_supported_browsers(browsers)
    if not ok:
        print(message, file=sys.stderr)
        return 1
    if any(browser in BRANDED_INSTALL_BROWSERS for browser in browsers):
        status = 0
        for browser in browsers:
            if browser == "chromium":
                executable = _chromium_executable_path()
                if executable and not args.force:
                    if args.dry_run:
                        print(executable)
                    else:
                        print(f"Rustwright found Chromium executable: {executable}")
                    continue
                try:
                    result = _download_chromium(force=bool(args.force), dry_run=bool(args.dry_run))
                except Exception as exc:
                    print(
                        "Could not install Chromium. Set RUSTWRIGHT_CHROMIUM, CHROME, or CHROMIUM, "
                        f"or install Chrome/Chromium manually. Details: {exc}",
                        file=sys.stderr,
                    )
                    status = 1
                    continue
                if args.dry_run:
                    print(f"Rustwright would download Chromium from: {result['url']}")
                    print(result["executable"])
                else:
                    verb = "installed" if result["downloaded"] else "found"
                    print(f"Rustwright {verb} Chromium executable: {result['executable']}")
            else:
                status = max(status, _install_branded_browser(browser, args))
        return status
    executable = _chromium_executable_path()
    if executable and not args.force:
        if args.with_deps:
            print("Rustwright does not install OS packages; using the existing Chromium executable.")
        if args.dry_run:
            print(executable)
        else:
            print(f"Rustwright found Chromium executable: {executable}")
        return 0
    try:
        result = _download_chromium(force=bool(args.force), dry_run=bool(args.dry_run))
    except Exception as exc:
        print(
            "Could not install Chromium. Set RUSTWRIGHT_CHROMIUM, CHROME, or CHROMIUM, "
            f"or install Chrome/Chromium manually. Details: {exc}",
            file=sys.stderr,
        )
        return 1
    if args.with_deps:
        print("Rustwright does not install OS packages; ensure Chromium runtime dependencies are present.")
    if args.dry_run:
        print(f"Rustwright would download Chromium from: {result['url']}")
        print(result["executable"])
    else:
        verb = "installed" if result["downloaded"] else "found"
        print(f"Rustwright {verb} Chromium executable: {result['executable']}")
    return 0


def install_deps(argv: Sequence[str], *, program: str = "playwright") -> int:
    args = _install_deps_parser(program).parse_args(list(argv))
    browsers = _requested_browsers(args.browsers)
    ok, message = _check_supported_browsers(browsers)
    if not ok:
        print(message, file=sys.stderr)
        return 1
    if sys.platform.startswith("linux"):
        return _install_linux_deps(dry_run=bool(args.dry_run))
    if sys.platform == "darwin":
        print("Rustwright install-deps: macOS does not require extra browser OS packages.")
        return 0
    if sys.platform == "win32":
        print("Rustwright install-deps: Windows browser dependencies are bundled with Chrome for Testing.")
        return 0
    print(f"Rustwright install-deps does not know platform {sys.platform!r}.", file=sys.stderr)
    return 1


def _rustwright_managed_browser_dirs(cache_dir: Path) -> list[Path]:
    try:
        entries = list(cache_dir.iterdir())
    except OSError:
        return []
    managed: list[Path] = []
    for entry in entries:
        if entry.is_dir() and entry.name.lower().startswith("chromium-"):
            managed.append(entry)
    return sorted(managed)


def uninstall(argv: Sequence[str], *, program: str = "playwright") -> int:
    _uninstall_parser(program).parse_args(list(argv))
    cache_dir = _browser_cache_dir()
    targets = _rustwright_managed_browser_dirs(cache_dir)
    if not targets:
        print(f"Rustwright found no managed Chromium browser cache directories in {cache_dir}.")
        return 0
    for target in targets:
        shutil.rmtree(target)
        print(f"Removed Rustwright managed browser cache: {target}")
    return 0


def codegen(argv: Sequence[str], *, program: str = "playwright") -> int:
    args = _codegen_parser(program).parse_args(list(argv))
    try:
        with sync_playwright() as playwright:
            source = _generate_codegen_source(args, playwright)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(source, encoding="utf-8")
        print(f"Rustwright generated starter code to {output}")
    else:
        print(source, end="")
    return 0


def screenshot(argv: Sequence[str], *, program: str = "playwright") -> int:
    args = _screenshot_parser(program).parse_args(list(argv))
    try:
        with sync_playwright() as playwright:
            browser, context = _launch_cli_context(args, playwright, headless=True)
            page = None
            try:
                print(f"Navigating to {args.url}")
                page = _open_page(context, args.url)
                _wait_for_capture_options(page, args)
                print(f"Capturing screenshot into {args.filename}")
                page.screenshot(path=args.filename, full_page=bool(args.full_page))
            finally:
                if page is not None:
                    page.close()
                _close_open_browser(
                    browser,
                    context,
                    save_storage=args.save_storage,
                    close_context=bool(args.save_har),
                )
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def pdf(argv: Sequence[str], *, program: str = "playwright") -> int:
    args = _pdf_parser(program).parse_args(list(argv))
    try:
        with sync_playwright() as playwright:
            browser, context = _launch_cli_context(args, playwright, headless=True)
            page = None
            try:
                print(f"Navigating to {args.url}")
                page = _open_page(context, args.url)
                _wait_for_capture_options(page, args)
                print(f"Saving as pdf into {args.filename}")
                page.pdf(path=args.filename, format=args.paper_format)
            finally:
                if page is not None:
                    page.close()
                _close_open_browser(
                    browser,
                    context,
                    save_storage=args.save_storage,
                    close_context=bool(args.save_har),
                )
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _read_trace_events(archive: zipfile.ZipFile, name: str) -> list[dict[str, object]]:
    if name not in archive.namelist():
        return []
    events: list[dict[str, object]] = []
    for line in archive.read(name).decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _trace_resource_mime_type(name: str) -> str:
    lower = name.lower()
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".gif"):
        return "image/gif"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith(".png"):
        return "image/png"
    return "application/octet-stream"


def _trace_resource_data_uri(archive: zipfile.ZipFile, name: str) -> str:
    archive_name = name if name.startswith("resources/") else f"resources/{name}"
    if archive_name not in archive.namelist():
        return ""
    data = archive.read(archive_name)
    if len(data) > 2_000_000:
        return ""
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{_trace_resource_mime_type(archive_name)};base64,{encoded}"


def _trace_preview_text(value: object, *, limit: int = 4000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n..."


def _trace_summary(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise FileNotFoundError(f"Trace file does not exist: {path}")
    if not zipfile.is_zipfile(path):
        raise ValueError(f"Trace file is not a zip archive: {path}")
    with zipfile.ZipFile(path) as archive:
        trace_events = _read_trace_events(archive, "trace.trace")
        network_events = _read_trace_events(archive, "trace.network")
        resources = [name for name in archive.namelist() if name.startswith("resources/")]
        snapshots = []
        screenshots = []
        for event in trace_events:
            if event.get("type") == "frame-snapshot":
                snapshot = event.get("snapshot") if isinstance(event.get("snapshot"), dict) else {}
                viewport = snapshot.get("viewport") if isinstance(snapshot.get("viewport"), dict) else {}
                width = viewport.get("width", "")
                height = viewport.get("height", "")
                viewport_text = f"{width}x{height}" if width or height else ""
                snapshots.append(
                    {
                        "callId": snapshot.get("callId", ""),
                        "snapshotName": snapshot.get("snapshotName", ""),
                        "frameUrl": snapshot.get("frameUrl", ""),
                        "viewport": viewport_text,
                        "html": _trace_preview_text(snapshot.get("html", "")),
                    }
                )
            elif event.get("type") == "screencast-frame":
                sha1 = str(event.get("sha1") or "")
                screenshots.append(
                    {
                        "callId": event.get("callId", ""),
                        "sha1": sha1,
                        "width": event.get("width", ""),
                        "height": event.get("height", ""),
                        "dataUri": _trace_resource_data_uri(archive, sha1) if sha1 else "",
                    }
                )
    metadata = next((event for event in trace_events if event.get("type") == "context-options"), {})
    before_by_call = {
        str(event.get("callId")): event
        for event in trace_events
        if event.get("type") == "before" and event.get("callId") is not None
    }
    actions = []
    for event in trace_events:
        if event.get("type") != "after" or event.get("callId") is None:
            continue
        call_id = str(event.get("callId"))
        before = before_by_call.get(call_id, {})
        actions.append(
            {
                "callId": call_id,
                "class": before.get("class", ""),
                "method": before.get("method", ""),
                "params": before.get("params", {}),
                "startTime": before.get("startTime", ""),
                "endTime": event.get("endTime", ""),
                "error": event.get("error"),
            }
        )
    network = []
    for event in network_events:
        snapshot = event.get("snapshot")
        if not isinstance(snapshot, dict):
            continue
        request = snapshot.get("request") if isinstance(snapshot.get("request"), dict) else {}
        response = snapshot.get("response") if isinstance(snapshot.get("response"), dict) else {}
        network.append(
            {
                "method": request.get("method", ""),
                "url": request.get("url", ""),
                "status": response.get("status", ""),
                "statusText": response.get("statusText", ""),
            }
        )
    return {
        "path": str(path),
        "metadata": metadata,
        "actions": actions,
        "network": network,
        "snapshots": snapshots,
        "screenshots": screenshots,
        "resourceCount": len(resources),
    }


def _render_trace_html(summaries: list[dict[str, object]]) -> str:
    sections = []
    for summary in summaries:
        metadata = summary.get("metadata") if isinstance(summary.get("metadata"), dict) else {}
        actions = summary.get("actions") if isinstance(summary.get("actions"), list) else []
        network = summary.get("network") if isinstance(summary.get("network"), list) else []
        snapshots = summary.get("snapshots") if isinstance(summary.get("snapshots"), list) else []
        screenshots = summary.get("screenshots") if isinstance(summary.get("screenshots"), list) else []
        action_rows = []
        for action in actions:
            item = action if isinstance(action, dict) else {}
            error = item.get("error")
            error_text = ""
            if isinstance(error, dict):
                error_text = str(error.get("message") or error.get("name") or "")
            action_rows.append(
                "<tr>"
                f"<td>{escape(str(item.get('callId', '')))}</td>"
                f"<td>{escape(str(item.get('class', '')))}</td>"
                f"<td>{escape(str(item.get('method', '')))}</td>"
                f"<td><code>{escape(json.dumps(item.get('params', {}), sort_keys=True))}</code></td>"
                f"<td>{escape(error_text)}</td>"
                "</tr>"
            )
        network_rows = []
        for request in network:
            item = request if isinstance(request, dict) else {}
            network_rows.append(
                "<tr>"
                f"<td>{escape(str(item.get('method', '')))}</td>"
                f"<td>{escape(str(item.get('status', '')))}</td>"
                f"<td>{escape(str(item.get('url', '')))}</td>"
                f"<td>{escape(str(item.get('statusText', '')))}</td>"
                "</tr>"
            )
        snapshot_rows = []
        for snapshot in snapshots:
            item = snapshot if isinstance(snapshot, dict) else {}
            snapshot_rows.append(
                "<tr>"
                f"<td>{escape(str(item.get('callId', '')))}</td>"
                f"<td>{escape(str(item.get('snapshotName', '')))}</td>"
                f"<td>{escape(str(item.get('frameUrl', '')))}</td>"
                f"<td>{escape(str(item.get('viewport', '')))}</td>"
                f"<td><pre>{escape(str(item.get('html', '')))}</pre></td>"
                "</tr>"
            )
        screenshot_rows = []
        for frame in screenshots:
            item = frame if isinstance(frame, dict) else {}
            data_uri = str(item.get("dataUri") or "")
            preview = (
                f"<img class=\"trace-frame\" src=\"{escape(data_uri, quote=True)}\" "
                f"alt=\"{escape(str(item.get('sha1', '')), quote=True)}\">"
                if data_uri
                else escape(str(item.get("sha1", "")))
            )
            size = ""
            if item.get("width") or item.get("height"):
                size = f"{item.get('width', '')}x{item.get('height', '')}"
            screenshot_rows.append(
                "<tr>"
                f"<td>{escape(str(item.get('callId', '')))}</td>"
                f"<td>{escape(str(item.get('sha1', '')))}</td>"
                f"<td>{escape(size)}</td>"
                f"<td>{preview}</td>"
                "</tr>"
            )
        title = metadata.get("title") or Path(str(summary.get("path", "trace"))).name
        action_body = "".join(action_rows) or '<tr><td colspan="5">No action rows recorded.</td></tr>'
        snapshot_body = "".join(snapshot_rows) or '<tr><td colspan="5">No DOM snapshots recorded.</td></tr>'
        screenshot_body = "".join(screenshot_rows) or '<tr><td colspan="4">No screenshot frames recorded.</td></tr>'
        network_body = "".join(network_rows) or '<tr><td colspan="4">No network rows recorded.</td></tr>'
        sections.append(
            "<section>"
            f"<h2>{escape(str(title))}</h2>"
            f"<p><strong>Trace:</strong> {escape(str(summary.get('path', '')))}</p>"
            f"<p><strong>Browser:</strong> {escape(str(metadata.get('browserName', '')))} "
            f"<strong>SDK:</strong> {escape(str(metadata.get('sdkLanguage', '')))} "
            f"<strong>Resources:</strong> {escape(str(summary.get('resourceCount', 0)))}</p>"
            "<h3>Actions</h3>"
            "<table><thead><tr><th>Call</th><th>Class</th><th>Method</th><th>Params</th><th>Error</th></tr></thead>"
            f"<tbody>{action_body}</tbody></table>"
            "<h3>DOM Snapshots</h3>"
            "<table><thead><tr><th>Call</th><th>Name</th><th>URL</th><th>Viewport</th><th>HTML Preview</th></tr></thead>"
            f"<tbody>{snapshot_body}</tbody></table>"
            "<h3>Screenshot Frames</h3>"
            "<table><thead><tr><th>Call</th><th>Resource</th><th>Size</th><th>Preview</th></tr></thead>"
            f"<tbody>{screenshot_body}</tbody></table>"
            "<h3>Network</h3>"
            "<table><thead><tr><th>Method</th><th>Status</th><th>URL</th><th>Status Text</th></tr></thead>"
            f"<tbody>{network_body}</tbody></table>"
            "</section>"
        )
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>Rustwright Trace Viewer</title>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:24px;color:#1f2937}"
        "section{margin-bottom:32px}table{border-collapse:collapse;width:100%;margin:12px 0 24px}"
        "th,td{border:1px solid #d1d5db;padding:8px;text-align:left;vertical-align:top}"
        "th{background:#f3f4f6}code,pre{white-space:pre-wrap}pre{max-height:240px;overflow:auto;margin:0}"
        ".trace-frame{max-width:320px;max-height:220px;border:1px solid #d1d5db}</style></head><body>"
        "<h1>Rustwright Trace Viewer</h1>"
        f"{''.join(sections)}"
        "</body></html>"
    )


def _default_trace_output(trace_paths: list[Path]) -> Path:
    if len(trace_paths) == 1:
        return trace_paths[0].with_suffix(".html")
    return Path.cwd() / "rustwright-trace-viewer.html"


def show_trace(argv: Sequence[str], *, program: str = "playwright") -> int:
    args = _show_trace_parser(program).parse_args(list(argv))
    trace_values = [args.trace] if args.trace else []
    if args.stdin:
        trace_values.extend(line.strip() for line in sys.stdin if line.strip())
    if not trace_values:
        print("playwright show-trace requires a trace zip path.", file=sys.stderr)
        return 1
    trace_paths: list[Path] = []
    for value in trace_values:
        parsed = urlparse(str(value))
        if parsed.scheme and parsed.scheme not in {"", "file"}:
            print("Rustwright show-trace currently supports local trace zip files only.", file=sys.stderr)
            return 1
        trace_paths.append(Path(parsed.path if parsed.scheme == "file" else str(value)).expanduser().resolve())
    try:
        summaries = [_trace_summary(path) for path in trace_paths]
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    output = Path(args.output).expanduser().resolve() if args.output else _default_trace_output(trace_paths)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render_trace_html(summaries), encoding="utf-8")
    if args.host or args.port:
        print("Rustwright wrote a static trace viewer instead of starting a long-running server.")
    print(f"Rustwright trace viewer written to {output}")
    return 0


def _trace_command_path(value: str | None) -> Path:
    if not value:
        raise ValueError("trace command requires a trace zip path")
    parsed = urlparse(str(value))
    if parsed.scheme and parsed.scheme not in {"", "file"}:
        raise ValueError("Rustwright trace currently supports local trace zip files only.")
    return Path(parsed.path if parsed.scheme == "file" else str(value)).expanduser().resolve()


def _trace_error_text(error: object) -> str:
    if isinstance(error, dict):
        message = str(error.get("message") or "")
        stack = str(error.get("stack") or "")
        return "\n".join(part for part in [message, stack] if part)
    return "" if error is None else str(error)


def _trace_json_text(value: object) -> str:
    if value is None or value == "":
        return ""
    try:
        return json.dumps(value, sort_keys=True)
    except TypeError:
        return str(value)


def _trace_print_actions(summary: dict[str, object], *, grep: str | None, errors_only: bool) -> None:
    actions = summary.get("actions") if isinstance(summary.get("actions"), list) else []
    pattern = grep.lower() if grep else None
    for action in actions:
        if not isinstance(action, dict):
            continue
        error_text = _trace_error_text(action.get("error"))
        if errors_only and not error_text:
            continue
        row_text = " ".join(
            [
                str(action.get("callId") or ""),
                str(action.get("class") or ""),
                str(action.get("method") or ""),
                _trace_json_text(action.get("params")),
                error_text,
            ]
        )
        if pattern and pattern not in row_text.lower():
            continue
        status = "error" if error_text else "ok"
        print(
            "\t".join(
                [
                    str(action.get("callId") or ""),
                    status,
                    str(action.get("class") or ""),
                    str(action.get("method") or ""),
                    _trace_json_text(action.get("params")),
                    error_text.splitlines()[0] if error_text else "",
                ]
            )
        )


def _trace_print_requests(
    summary: dict[str, object],
    *,
    grep: str | None,
    method: str | None,
    status: str | None,
    failed: bool,
) -> None:
    network = summary.get("network") if isinstance(summary.get("network"), list) else []
    pattern = grep.lower() if grep else None
    expected_method = method.upper() if method else None
    expected_status = str(status) if status is not None else None
    for request in network:
        if not isinstance(request, dict):
            continue
        request_method = str(request.get("method") or "")
        request_url = str(request.get("url") or "")
        request_status = str(request.get("status") or "")
        try:
            status_number = int(request_status)
        except ValueError:
            status_number = 0
        if pattern and pattern not in request_url.lower():
            continue
        if expected_method and request_method.upper() != expected_method:
            continue
        if expected_status is not None and request_status != expected_status:
            continue
        if failed and status_number < 400:
            continue
        print("\t".join([request_method, request_status, request_url, str(request.get("statusText") or "")]))


def _trace_print_errors(summary: dict[str, object]) -> None:
    actions = summary.get("actions") if isinstance(summary.get("actions"), list) else []
    for action in actions:
        if not isinstance(action, dict):
            continue
        error_text = _trace_error_text(action.get("error"))
        if not error_text:
            continue
        print(
            "\t".join(
                [
                    str(action.get("callId") or ""),
                    str(action.get("class") or ""),
                    str(action.get("method") or ""),
                    error_text,
                ]
            )
        )


def trace(argv: Sequence[str], *, program: str = "playwright") -> int:
    args = _trace_parser(program).parse_args(list(argv))
    if not args.trace_command:
        _trace_parser(program).print_help()
        return 0
    try:
        summary = _trace_summary(_trace_command_path(getattr(args, "trace", None)))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.trace_command == "actions":
        _trace_print_actions(summary, grep=args.grep, errors_only=bool(args.errors_only))
        return 0
    if args.trace_command == "requests":
        _trace_print_requests(
            summary,
            grep=args.grep,
            method=args.method,
            status=args.status,
            failed=bool(args.failed),
        )
        return 0
    if args.trace_command == "errors":
        _trace_print_errors(summary)
        return 0
    print(f"Unknown Rustwright trace command: {args.trace_command}", file=sys.stderr)
    return 1


def unsupported_tool(command: str, *, program: str = "playwright") -> int:
    print(
        f"{program} {command} is not implemented in Rustwright yet; "
        "Rustwright currently focuses on the Python automation API and Chromium CDP runtime.",
        file=sys.stderr,
    )
    return 1


def _agent_main(argv: Sequence[str]) -> int:
    from rustwright import _agent

    agent_cli = __import__(f"{_agent.__name__}.cli", fromlist=["main"])
    return agent_cli.main(list(argv))


def _mcp_main(argv: Sequence[str], *, program: str) -> int:
    try:
        from rustwright_mcp import server
    except ModuleNotFoundError as exc:
        if exc.name not in {"rustwright_mcp", "rustwright_mcp.server"}:
            raise
        print(
            "rustwright mcp requires the separately installed rustwright-mcp package; "
            "install it with: pip install rustwright-mcp\n"
            "or uvx --from 'git+https://github.com/Skyvern-AI/rustwright#subdirectory=mcp' rustwright-mcp",
            file=sys.stderr,
        )
        return 1

    original_argv = sys.argv
    sys.argv = [f"{program} mcp", *argv]
    try:
        exit_code = server.main()
    finally:
        sys.argv = original_argv
    return 0 if exit_code is None else exit_code


def _leading_agent_command_index(args: Sequence[str]) -> int | None:
    index = 0
    while index < len(args):
        value = args[index]
        if value in _AGENT_GLOBAL_BOOLEAN_FLAGS:
            index += 1
            continue
        if value in _AGENT_GLOBAL_VALUE_FLAGS:
            if index + 1 >= len(args):
                return len(args)
            index += 2
            continue
        if any(value.startswith(flag + "=") for flag in _AGENT_GLOBAL_VALUE_FLAGS):
            index += 1
            continue
        return index if index > 0 else None
    return index if index > 0 else None


def _screenshot_positional_count(args: Sequence[str]) -> int:
    count = 0
    index = 0
    positional_only = False
    while index < len(args):
        value = args[index]
        if positional_only:
            count += 1
            index += 1
            continue
        if value == "--":
            positional_only = True
            index += 1
            continue
        if value in _SCREENSHOT_VALUE_FLAGS:
            index += 2
            continue
        if any(value.startswith(flag + "=") for flag in _SCREENSHOT_VALUE_FLAGS if flag.startswith("--")):
            index += 1
            continue
        if value.startswith("-b") and value != "-b":
            index += 1
            continue
        if value.startswith("-"):
            index += 1
            continue
        count += 1
        index += 1
    return count


def _print_screenshot_help(program: str) -> None:
    print(
        f"usage: {program} screenshot [file] [--full] [--ref REF]\n"
        f"       {program} screenshot <url> <file> [options]\n\n"
        "Session form:\n"
        f"  {program} screenshot [file] [--full] [--ref REF]\n\n"
        "Playwright-compatible one-shot form:\n"
        f"  {program} screenshot <url> <file> [options]"
    )


def _print_mcp_help(program: str) -> None:
    print(
        f"usage: {program} mcp [args...]\n\n"
        "Run the MCP stdio server (requires rustwright-mcp).\n"
        "Install with: pip install rustwright-mcp"
    )


def _unsupported_browser_alias(name: str) -> int:
    print(
        f"{name} is not implemented; Rustwright currently supports Chromium over direct CDP.",
        file=sys.stderr,
    )
    return 1


def main(argv: Sequence[str] | None = None, *, program: Optional[str] = None) -> int:
    program = program or _default_program_name()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(
            f"usage: {program} <command> [options]\n\n"
            "Browser session (persistent):\n"
            "  open [url]         start or attach a session (--headed, --session NAME)\n"
            "  snapshot           accessibility tree with refs (e1, e2, ...)\n"
            "  click <ref>        click an element\n"
            "  fill <ref> <text>  clear and type into an element\n"
            "  type / press / select / hover / wait / back / reload / eval\n"
            "  tabs [list|new|use|close]\n"
            "  screenshot [file]  screenshot the session's current page\n"
            "  status / close     session lifecycle\n"
            "  mcp                run the MCP server (requires rustwright-mcp)\n\n"
            "Playwright-compatible tools:\n"
            "  install / install-deps / uninstall / codegen\n"
            "  screenshot <url> <file>   one-shot capture (two-argument form)\n"
            "  pdf <url> <file>          one-shot PDF\n"
            "  trace / show-trace\n"
        )
        return 0
    if args[0] in {"-V", "--version"}:
        print(_version())
        return 0
    if args[0].startswith("-"):
        agent_command_index = _leading_agent_command_index(args)
        if agent_command_index is not None:
            agent_globals = args[:agent_command_index]
            if agent_command_index == len(args):
                return _agent_main(args)
            agent_command = args[agent_command_index]
            agent_rest = args[agent_command_index + 1 :]
            if agent_command in {"-h", "--help"}:
                return main(["--help"], program=program)
            if agent_command == "help":
                if not agent_rest:
                    return main(["--help"], program=program)
                help_command = agent_rest[0]
                if help_command in {"open", "cr", "chromium"}:
                    return _agent_main([*agent_globals, "open", "--help"])
                if help_command in {"ff", "firefox", "wk", "webkit"}:
                    if "--json" in agent_globals:
                        return _agent_main([*agent_globals, "open", "--browser", help_command])
                    return _unsupported_browser_alias(help_command)
                if help_command == "screenshot":
                    _print_screenshot_help(program)
                    return 0
                if help_command in AGENT_VERBS:
                    return _agent_main([*agent_globals, help_command, "--help"])
                if "--json" in agent_globals:
                    return _agent_main([*agent_globals, help_command, "--help"])
                return main([help_command, "--help"], program=program)
            if agent_command in {"cr", "chromium"}:
                return _agent_main(
                    [*agent_globals, "open", "--browser", "chromium", *agent_rest]
                )
            if agent_command in {"ff", "firefox", "wk", "webkit"}:
                if "--json" in agent_globals:
                    return _agent_main(
                        [*agent_globals, "open", "--browser", agent_command, *agent_rest]
                    )
                return _unsupported_browser_alias(agent_command)
            if agent_command != "screenshot":
                if agent_command in AGENT_VERBS or "--json" in agent_globals:
                    return _agent_main(args)
                print(f"Unknown Rustwright CLI command: {agent_command}", file=sys.stderr)
                return 1
            screenshot_args = agent_rest
            if any(value in {"-h", "--help"} for value in screenshot_args):
                _print_screenshot_help(program)
                return 0
            if _screenshot_positional_count(screenshot_args) < 2:
                return _agent_main(args)
    command, rest = args[0], args[1:]
    if command == "help":
        if not rest:
            return main(["--help"], program=program)
        help_command = rest[0]
        if help_command in {"open", "cr", "chromium"}:
            return _agent_main(["open", "--help"])
        if help_command in {"ff", "firefox", "wk", "webkit"}:
            return _unsupported_browser_alias(help_command)
        if help_command == "screenshot":
            _print_screenshot_help(program)
            return 0
        if help_command == "mcp":
            _print_mcp_help(program)
            return 0
        if help_command in AGENT_VERBS:
            return _agent_main([help_command, "--help"])
        return main([help_command, "--help"], program=program)
    if command == "install":
        return install(rest, program=program)
    if command == "install-deps":
        return install_deps(rest, program=program)
    if command == "uninstall":
        return uninstall(rest, program=program)
    if command == "show-trace":
        return show_trace(rest, program=program)
    if command == "trace":
        return trace(rest, program=program)
    if command == "mcp":
        return _mcp_main(rest, program=program)
    if command in AGENT_VERBS - {"screenshot"}:
        return _agent_main(args)
    if command in {"cr", "chromium"}:
        return _agent_main(["open", "--browser", "chromium", *rest])
    if command in {"ff", "firefox", "wk", "webkit"}:
        return _unsupported_browser_alias(command)
    if command == "screenshot":
        if any(value in {"-h", "--help"} for value in rest):
            _print_screenshot_help(program)
            return 0
        if _screenshot_positional_count(rest) >= 2:
            return screenshot(rest, program=program)
        return _agent_main(args)
    if command == "pdf":
        return pdf(rest, program=program)
    if command == "codegen":
        return codegen(rest, program=program)
    print(f"Unknown Rustwright CLI command: {command}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(program="rustwright"))
