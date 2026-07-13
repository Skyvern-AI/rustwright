from __future__ import annotations

import argparse
from contextlib import contextmanager
import importlib
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

SMOKE_JS = r"""
() => {
  const uaData = navigator.userAgentData ? {
    brands: Array.from(navigator.userAgentData.brands || []),
    mobile: navigator.userAgentData.mobile,
    platform: navigator.userAgentData.platform,
  } : null;
  const screenInfo = {
    width: screen.width,
    height: screen.height,
    availWidth: screen.availWidth,
    availHeight: screen.availHeight,
    colorDepth: screen.colorDepth,
    pixelDepth: screen.pixelDepth,
  };
  return {
    webdriver: navigator.webdriver,
    webdriverInNavigator: "webdriver" in navigator,
    userAgent: navigator.userAgent,
    appVersion: navigator.appVersion,
    platform: navigator.platform,
    language: navigator.language,
    languages: Array.from(navigator.languages || []),
    userAgentData: uaData,
    hardwareConcurrency: navigator.hardwareConcurrency,
    deviceMemory: navigator.deviceMemory || null,
    maxTouchPoints: navigator.maxTouchPoints,
    cookieEnabled: navigator.cookieEnabled,
    doNotTrack: navigator.doNotTrack,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    screen: screenInfo,
    viewport: {
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      outerWidth: window.outerWidth,
      outerHeight: window.outerHeight,
      devicePixelRatio: window.devicePixelRatio,
    },
    chromeRuntime: !!(window.chrome && window.chrome.runtime),
    automationGlobals: Object.getOwnPropertyNames(window)
      .filter(name => /rustwright|playwright/i.test(name))
      .sort(),
  };
}
"""

FINGERPRINT_TARGETS: dict[str, dict[str, str]] = {
    "sannysoft": {
        "url": "https://bot.sannysoft.com/",
        "description": "SannySoft browser fingerprint results",
    },
    "creepjs": {
        "url": "https://abrahamjuliot.github.io/creepjs/",
        "description": "CreepJS browser fingerprint analysis",
    },
    "deviceandbrowserinfo": {
        "url": "https://deviceandbrowserinfo.com/are_you_a_bot",
        "description": "DeviceAndBrowserInfo bot classification",
    },
    "browserscan": {
        "url": "https://www.browserscan.net/bot-detection",
        "description": "BrowserScan bot detection",
    },
}

@contextmanager
def header_capture_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            pass

        def do_GET(self) -> None:
            payload = {
                "method": self.command,
                "path": self.path,
                "request_version": self.request_version,
                "headers": {name: value for name, value in self.headers.items()},
                "header_order": [name for name, _ in self.headers.items()],
            }
            body = (
                "<!doctype html><title>Anti-bot smoke</title>"
                "<pre id='payload'>"
                + json.dumps(payload, sort_keys=True)
                + "</pre>"
            ).encode("utf-8")
            self.send_response(200, "OK")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _load_sync_playwright(implementation: str, reference_path: str | None) -> Callable[..., Any]:
    if implementation == "rustwright":
        from rustwright.sync_api import sync_playwright

        return sync_playwright

    if implementation != "playwright":
        raise ValueError(f"unknown implementation: {implementation}")

    if reference_path:
        path = str(Path(reference_path).resolve())
        if path not in sys.path:
            sys.path.insert(0, path)
    for name in list(sys.modules):
        if name == "playwright" or name.startswith("playwright."):
            del sys.modules[name]
    module = importlib.import_module("playwright.sync_api")
    module_path = Path(getattr(module, "__file__", "")).resolve()
    if PYTHON_ROOT in module_path.parents or "rustwright" in str(module_path):
        raise RuntimeError(
            "The local drop-in playwright alias is shadowing real Playwright. "
            "Pass --reference-path .audit-playwright or run outside the editable repo environment."
        )
    return module.sync_playwright


def _launch_chromium(playwright: Any) -> Any:
    try:
        return playwright.chromium.launch(headless=True)
    except Exception as exc:
        message = str(exc)
        if (
            "mach_port_rendezvous" not in message
            and "bootstrap_check_in" not in message
            and "sandbox_parameters_mac" not in message
        ):
            raise
        return playwright.chromium.launch(headless=True, args=["--single-process"])


def _launch_persistent_context(chromium: Any, user_data_dir: str) -> Any:
    try:
        return chromium.launch_persistent_context(user_data_dir, headless=True)
    except Exception as exc:
        message = str(exc)
        if (
            "mach_port_rendezvous" not in message
            and "bootstrap_check_in" not in message
            and "sandbox_parameters_mac" not in message
        ):
            raise
        return chromium.launch_persistent_context(user_data_dir, headless=True, args=["--single-process"])


def _safe_close(target: Any) -> None:
    try:
        target.close()
    except Exception:
        pass


def _lower_headers(headers: dict[str, Any]) -> dict[str, str]:
    return {str(name).lower(): str(value) for name, value in headers.items()}


def _contains_token(value: Any, token: str) -> bool:
    return token.lower() in str(value or "").lower()


def _normalize_ch_header(value: str | None) -> str:
    return (value or "").strip().strip('"').lower()


def _has_malformed_accept_language(value: str) -> bool:
    if not value:
        return False
    return any(part.lower().count(";q=") > 1 for part in value.split(","))


def _has_malformed_navigator_languages(values: Any) -> bool:
    if not isinstance(values, list):
        return False
    return any(";q=" in str(value).lower() for value in values)


def analyze_smoke_signals(observed: dict[str, Any], headers: dict[str, Any]) -> dict[str, Any]:
    lower_headers = _lower_headers(headers)
    header_values = " ".join(lower_headers.values())
    header_ua = lower_headers.get("user-agent", "")
    sec_ch_ua = lower_headers.get("sec-ch-ua", "")
    sec_ch_platform = _normalize_ch_header(lower_headers.get("sec-ch-ua-platform"))
    sec_ch_mobile = _normalize_ch_header(lower_headers.get("sec-ch-ua-mobile"))
    accept_language = lower_headers.get("accept-language", "")
    js_ua = str(observed.get("userAgent") or "")
    ua_data = observed.get("userAgentData") or {}
    ua_platform = _normalize_ch_header(ua_data.get("platform") if isinstance(ua_data, dict) else None)
    language = str(observed.get("language") or "").lower()
    globals_ = [str(name) for name in observed.get("automationGlobals") or []]

    identity_mismatch_count = 0
    if header_ua and js_ua and header_ua != js_ua:
        identity_mismatch_count += 1
    if bool(_contains_token(header_ua, "HeadlessChrome")) != bool(_contains_token(js_ua, "HeadlessChrome")):
        identity_mismatch_count += 1
    if sec_ch_platform and ua_platform and sec_ch_platform != ua_platform:
        identity_mismatch_count += 1
    if sec_ch_mobile in {"?0", "?1"} and isinstance(ua_data, dict) and ua_data.get("mobile") is not None:
        if (sec_ch_mobile == "?1") != bool(ua_data.get("mobile")):
            identity_mismatch_count += 1
    if accept_language and language and not accept_language.lower().startswith(language):
        identity_mismatch_count += 1

    signals = {
        "missing_observed": not bool(observed),
        "webdriver_exposed": observed.get("webdriver") is not None,
        "webdriver_property_present": bool(observed.get("webdriverInNavigator")),
        "headless_ua": _contains_token(js_ua, "HeadlessChrome")
        or _contains_token(header_ua, "HeadlessChrome")
        or _contains_token(sec_ch_ua, "HeadlessChrome"),
        "playwright_header": _contains_token(header_values, "Playwright"),
        "rustwright_header": _contains_token(header_values, "Rustwright"),
        "playwright_global": any(_contains_token(name, "Playwright") for name in globals_),
        "rustwright_global": any(_contains_token(name, "Rustwright") for name in globals_),
        "malformed_accept_language": _has_malformed_accept_language(accept_language),
        "malformed_navigator_languages": _has_malformed_navigator_languages(observed.get("languages")),
        "identity_mismatch_count": identity_mismatch_count,
    }
    failed = [name for name, value in signals.items() if _signal_failed(name, value)]
    signals["failed_signals"] = failed
    signals["passed"] = not failed
    return signals


def analyze_network_signals(sample: dict[str, Any]) -> dict[str, Any]:
    headers = sample.get("headers") or {}
    observed = sample.get("observed") or {}
    lower_headers = _lower_headers(headers)
    header_order = [str(name).lower() for name in sample.get("header_order") or []]
    header_ua = lower_headers.get("user-agent", "")
    sec_ch_ua = lower_headers.get("sec-ch-ua", "")
    sec_ch_platform = _normalize_ch_header(lower_headers.get("sec-ch-ua-platform"))
    sec_ch_mobile = _normalize_ch_header(lower_headers.get("sec-ch-ua-mobile"))
    accept_language = lower_headers.get("accept-language", "")
    js_ua = str(observed.get("userAgent") or "")
    language = str(observed.get("language") or "").lower()
    ua_data = observed.get("userAgentData") or {}
    ua_platform = _normalize_ch_header(ua_data.get("platform") if isinstance(ua_data, dict) else None)
    protocol = str(sample.get("request_version") or "")

    identity_mismatches: list[str] = []
    if header_ua and js_ua and header_ua != js_ua:
        identity_mismatches.append("header_user_agent_vs_navigator_user_agent")
    if bool(_contains_token(header_ua, "HeadlessChrome")) != bool(_contains_token(js_ua, "HeadlessChrome")):
        identity_mismatches.append("headless_token_header_vs_js")
    if sec_ch_platform and ua_platform and sec_ch_platform != ua_platform:
        identity_mismatches.append("sec_ch_ua_platform_vs_navigator_user_agent_data")
    if sec_ch_mobile in {"?0", "?1"} and isinstance(ua_data, dict) and ua_data.get("mobile") is not None:
        if (sec_ch_mobile == "?1") != bool(ua_data.get("mobile")):
            identity_mismatches.append("sec_ch_ua_mobile_vs_navigator_user_agent_data")
    if accept_language and language and not accept_language.lower().startswith(language):
        identity_mismatches.append("accept_language_vs_navigator_language")

    expected_navigation_headers = {
        "accept",
        "accept-language",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
        "sec-fetch-user",
        "upgrade-insecure-requests",
        "user-agent",
    }
    missing_headers = sorted(name for name in expected_navigation_headers if name not in lower_headers)
    bad_sec_fetch = []
    if lower_headers.get("sec-fetch-dest") not in {None, "document"}:
        bad_sec_fetch.append("sec-fetch-dest")
    if lower_headers.get("sec-fetch-mode") not in {None, "navigate"}:
        bad_sec_fetch.append("sec-fetch-mode")
    if lower_headers.get("sec-fetch-site") not in {None, "none"}:
        bad_sec_fetch.append("sec-fetch-site")
    if lower_headers.get("sec-fetch-user") not in {None, "?1"}:
        bad_sec_fetch.append("sec-fetch-user")

    signals: dict[str, Any] = {
        "request_protocol": protocol,
        "http1_local_capture": protocol.startswith("HTTP/1."),
        "automation_tokens_in_headers": any(
            _contains_token(value, "Playwright") or _contains_token(value, "Rustwright")
            for value in lower_headers.values()
        ),
        "headless_ua": _contains_token(header_ua, "HeadlessChrome")
        or _contains_token(sec_ch_ua, "HeadlessChrome")
        or _contains_token(js_ua, "HeadlessChrome"),
        "malformed_accept_language": _has_malformed_accept_language(accept_language),
        "missing_navigation_headers": missing_headers,
        "bad_sec_fetch_headers": bad_sec_fetch,
        "identity_mismatches": identity_mismatches,
        "header_order": header_order,
    }
    failed = []
    if signals["automation_tokens_in_headers"]:
        failed.append("automation_tokens_in_headers")
    if signals["headless_ua"]:
        failed.append("headless_ua")
    if signals["malformed_accept_language"]:
        failed.append("malformed_accept_language")
    if missing_headers:
        failed.append("missing_navigation_headers")
    if bad_sec_fetch:
        failed.append("bad_sec_fetch_headers")
    if identity_mismatches:
        failed.append("identity_mismatches")
    signals["failed_signals"] = failed
    signals["passed"] = not failed
    return signals


def _signal_failed(name: str, value: Any) -> bool:
    if name == "identity_mismatch_count":
        return int(value) > 0
    return bool(value)


def text_excerpt(text: str, limit: int = 1200) -> str:
    compact = " ".join((text or "").split())
    return compact[:limit]


def challenge_indicators(text: str, url: str) -> list[str]:
    lower_url = (url or "").lower()
    lower_text = (text or "").lower()
    lower = f"{lower_url}\n{lower_text}"
    indicators = []
    if (
        "errors.edgesuite.net" in lower_url
        or ("access denied" in lower_text and ("reference #" in lower_text or "you don't have permission" in lower_text))
    ):
        indicators.append("access_denied")
    if any(
        token in lower_text
        for token in (
            "please verify you are human",
            "verify you are human",
            "complete the captcha",
            "solve the captcha",
            "i am not a robot",
            "captcha required",
            "hcaptcha challenge",
            "recaptcha challenge",
        )
    ):
        indicators.append("captcha")
    if (
        "cdn-cgi/challenge-platform" in lower_url
        or "checking your browser before accessing" in lower_text
        or "checking if the site connection is secure" in lower_text
        or ("cloudflare" in lower_text and "ray id" in lower_text)
    ):
        indicators.append("cloudflare")
    if "403 forbidden" in lower_text or lower_text.strip() == "forbidden":
        indicators.append("forbidden")
    if "too many requests" in lower_text or "rate limit exceeded" in lower_text:
        indicators.append("rate_limited")
    return indicators


def analyze_fingerprint_text(target: str, text: str, url: str = "") -> dict[str, Any]:
    lower = (text or "").lower()
    indicators = challenge_indicators(text, url)
    result: dict[str, Any] = {
        "target": target,
        "summary": text_excerpt(text),
        "challenge_indicators": indicators,
        "blocked_or_challenged": bool(indicators),
        "missing_text": not bool((text or "").strip()),
    }
    if target == "sannysoft":
        ignored = ("plugins length (old)", "plugins is of type pluginarray", "permissions", "headchr_plugins")
        failure_lines = []
        for line in text.splitlines():
            normalized = " ".join(line.lower().split())
            if not normalized or any(token in normalized for token in ignored):
                continue
            if (
                "\tfail" in line.lower()
                or " fail " in f" {normalized} "
                or " failed" in f" {normalized}"
                or "(failed)" in normalized
                or '"webdriver": true' in normalized
                or '"webdriver":true' in normalized
            ):
                failure_lines.append(line.strip())
        result["failures"] = len(failure_lines)
        result["failure_excerpt"] = failure_lines[:8]
    elif target == "creepjs":
        result["headless_percent"] = _first_percent_after(lower, ("headless", "headless rating"))
        result["stealth_percent"] = _first_percent_after(lower, ("stealth",))
        result["headless_chrome_text"] = "headlesschrome" in lower
        result["worker_headless_ua"] = bool(
            re.search(r"(?:serviceworker|sharedworker|worker)[\s\S]{0,1200}headlesschrome", lower)
        )
    elif target == "deviceandbrowserinfo":
        is_bot = None
        parsed = _extract_first_json_object(text)
        if isinstance(parsed, dict):
            raw_is_bot = parsed.get("isBot")
            if isinstance(raw_is_bot, bool):
                is_bot = raw_is_bot
            details = parsed.get("details")
            if isinstance(details, dict):
                result["positive_details"] = sorted(
                    str(name) for name, value in details.items() if value is True
                )
                result["detail_count"] = len(result["positive_details"])
        if is_bot is None and re.search(r'"isBot"\s*:\s*true', text or "", re.IGNORECASE):
            is_bot = True
        if is_bot is None and re.search(r'"isBot"\s*:\s*false', text or "", re.IGNORECASE):
            is_bot = False
        if "you are not a bot" in lower or "not a bot" in lower:
            is_bot = False
        if "you are a bot" in lower or "is a bot" in lower:
            is_bot = True
        result["is_bot"] = is_bot
    elif target == "browserscan":
        result["abnormal_count"] = _first_int_near(lower, "abnormal")
        statuses = {
            "test_results": _status_after_label(text, "Test Results"),
            "webdriver": _status_after_label(text, "WebDriver"),
            "webdriver_advance": _status_after_label(text, "WebDriver Advance"),
            "headless_chrome": _status_after_label(text, "Headless Chrome"),
            "cdp": _status_after_label(text, "CDP"),
            "dev_tool": _status_after_label(text, "Dev Tool"),
        }
        result["statuses"] = {name: value for name, value in statuses.items() if value is not None}
        result["bad_statuses"] = {
            name: value
            for name, value in result["statuses"].items()
            if str(value).lower() != "normal"
        }
    return result


def fingerprint_diagnostic_failures(target: str, diagnostic: dict[str, Any]) -> list[str]:
    failures = []
    if target == "sannysoft" and diagnostic.get("failures", 0) > 0:
        failures.append("sannysoft_failures")
    elif target == "creepjs":
        headless_percent = diagnostic.get("headless_percent")
        if isinstance(headless_percent, (int, float)) and headless_percent >= 50:
            failures.append("creepjs_headless_percent")
        if diagnostic.get("headless_chrome_text"):
            failures.append("creepjs_headless_chrome_text")
        if diagnostic.get("worker_headless_ua"):
            failures.append("creepjs_worker_headless_ua")
    elif target == "deviceandbrowserinfo":
        if diagnostic.get("is_bot") is True:
            failures.append("deviceandbrowserinfo_is_bot")
        if diagnostic.get("positive_details"):
            failures.append("deviceandbrowserinfo_positive_details")
    elif target == "browserscan":
        abnormal_count = diagnostic.get("abnormal_count")
        if isinstance(abnormal_count, int) and abnormal_count > 0:
            failures.append("browserscan_abnormal_count")
        for name in sorted((diagnostic.get("bad_statuses") or {}).keys()):
            failures.append(f"browserscan_{name}")
    return failures


def _extract_first_json_object(text: str) -> Any:
    decoder = json.JSONDecoder()
    source = text or ""
    for match in re.finditer(r"\{", source):
        try:
            value, _ = decoder.raw_decode(source[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _status_after_label(text: str, label: str) -> str | None:
    statuses = {"normal", "abnormal", "failed", "fail", "risk", "risky", "warning", "suspicious", "bot"}
    lines = [" ".join(line.strip().split()) for line in (text or "").splitlines()]
    label_lower = label.lower()
    for index, line in enumerate(lines):
        if not line:
            continue
        lower = line.lower()
        remainder = None
        if lower == label_lower or lower == f"{label_lower}:":
            for next_line in lines[index + 1 :]:
                if next_line:
                    remainder = next_line
                    break
        elif lower.startswith(f"{label_lower}:"):
            remainder = line[len(label) :].strip(" :")
        elif lower.startswith(f"{label_lower} "):
            possible = line[len(label) :].strip(" :")
            if possible.split(" ", 1)[0].lower() in statuses:
                remainder = possible
        if not remainder:
            continue
        first_word = remainder.split(" ", 1)[0].lower()
        if first_word in statuses:
            return first_word.title()
    return None


def _first_percent_after(text: str, labels: tuple[str, ...]) -> float | None:
    for label in labels:
        index = text.find(label)
        if index < 0:
            continue
        window = text[max(0, index - 80) : index + 120]
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", window)
        if match:
            return float(match.group(1))
    return None


def _first_int_near(text: str, label: str) -> int | None:
    index = text.find(label)
    if index < 0:
        return None
    window = text[max(0, index - 80) : index + 120]
    match = re.search(r"\b(\d+)\b", window)
    return int(match.group(1)) if match else None


def select_fingerprint_targets(targets: list[str] | None, sample: int | None) -> list[str]:
    selected = list(FINGERPRINT_TARGETS) if not targets or "all" in targets else targets
    unknown = [target for target in selected if target not in FINGERPRINT_TARGETS]
    if unknown:
        raise ValueError(f"unknown fingerprint target(s): {', '.join(unknown)}")
    if sample is not None:
        if sample < 1:
            raise ValueError("--sample must be >= 1")
        selected = selected[:sample]
    return selected


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    index = (len(ordered) - 1) * pct
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def build_smoke_result(implementation: str, samples: list[dict[str, Any]]) -> dict[str, Any]:
    analyzed = []
    for sample in samples:
        signals = analyze_smoke_signals(sample["observed"], sample["headers"])
        analyzed.append({**sample, "signals": signals})
    timings = [float(sample["elapsed_ms"]) for sample in analyzed]
    passed = sum(1 for sample in analyzed if sample["signals"]["passed"])
    signal_fail_counts: dict[str, int] = {}
    for sample in analyzed:
        for signal in sample["signals"]["failed_signals"]:
            signal_fail_counts[signal] = signal_fail_counts.get(signal, 0) + 1
    return {
        "implementation": implementation,
        "suite": "smoke",
        "iterations": len(samples),
        "status": "passed" if passed == len(samples) else "failed",
        "passed_iterations": passed,
        "clean_signal_rate": passed / len(samples) if samples else 0.0,
        "challenge_free_rate": passed / len(samples) if samples else 0.0,
        "block_rate": 0.0,
        "captcha_rate": 0.0,
        "p50_ms": statistics.median(timings) if timings else None,
        "p95_ms": percentile(timings, 0.95),
        "signal_fail_counts": signal_fail_counts,
        "samples": analyzed,
    }


def build_network_result(implementation: str, samples: list[dict[str, Any]]) -> dict[str, Any]:
    analyzed = []
    for sample in samples:
        signals = analyze_network_signals(sample)
        analyzed.append({**sample, "network_signals": signals})
    timings = [float(sample["elapsed_ms"]) for sample in analyzed]
    passed = sum(1 for sample in analyzed if sample["network_signals"]["passed"])
    signal_fail_counts: dict[str, int] = {}
    for sample in analyzed:
        for signal in sample["network_signals"]["failed_signals"]:
            signal_fail_counts[signal] = signal_fail_counts.get(signal, 0) + 1
    return {
        "implementation": implementation,
        "suite": "network",
        "iterations": len(samples),
        "status": "passed" if passed == len(samples) else "failed",
        "passed_iterations": passed,
        "clean_signal_rate": passed / len(samples) if samples else 0.0,
        "challenge_free_rate": 1.0,
        "block_rate": 0.0,
        "captcha_rate": 0.0,
        "p50_ms": statistics.median(timings) if timings else None,
        "p95_ms": percentile(timings, 0.95),
        "signal_fail_counts": signal_fail_counts,
        "samples": analyzed,
        "scope": "local_l7_header_and_client_hint_consistency",
        "limitations": [
            "Uses a local HTTP capture endpoint, so TLS/JA3/JA4 and HTTP/2 settings are not measured yet.",
            "Intended as a Tier 2 regression baseline, not an undetectability claim.",
        ],
    }


def build_matrix_result(implementation: str, profile_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    profiles = []
    all_timings = []
    passed_iterations = 0
    total_iterations = 0
    signal_fail_counts: dict[str, int] = {}
    for profile, result in profile_results.items():
        profile_result = {**result, "profile": profile}
        profiles.append(profile_result)
        passed_iterations += int(result.get("passed_iterations") or 0)
        total_iterations += int(result.get("iterations") or 0)
        for sample in result.get("samples") or []:
            if "elapsed_ms" in sample:
                all_timings.append(float(sample["elapsed_ms"]))
        for signal, count in (result.get("signal_fail_counts") or {}).items():
            signal_fail_counts[signal] = signal_fail_counts.get(signal, 0) + int(count)

    status = "passed" if total_iterations and passed_iterations == total_iterations else "failed"
    deltas: dict[str, Any] = {}
    fresh = profile_results.get("fresh_profile")
    warm = profile_results.get("persistent_warm_profile")
    if fresh and warm and fresh.get("p50_ms") is not None and warm.get("p50_ms") is not None:
        deltas["persistent_warm_minus_fresh_p50_ms"] = float(warm["p50_ms"]) - float(fresh["p50_ms"])
    return {
        "implementation": implementation,
        "suite": "matrix",
        "profiles": profiles,
        "profile_names": list(profile_results),
        "iterations": total_iterations,
        "status": status,
        "passed_iterations": passed_iterations,
        "clean_signal_rate": passed_iterations / total_iterations if total_iterations else 0.0,
        "challenge_free_rate": passed_iterations / total_iterations if total_iterations else 0.0,
        "block_rate": 0.0,
        "captcha_rate": 0.0,
        "p50_ms": statistics.median(all_timings) if all_timings else None,
        "p95_ms": percentile(all_timings, 0.95),
        "signal_fail_counts": signal_fail_counts,
        "profile_deltas": deltas,
    }


def build_fingerprint_result(
    implementation: str,
    samples: list[dict[str, Any]],
    targets: list[str],
) -> dict[str, Any]:
    analyzed = []
    for sample in samples:
        signals = analyze_smoke_signals(sample.get("observed") or {}, sample.get("headers") or {})
        diagnostic = analyze_fingerprint_text(sample["target"], sample.get("text", ""), sample.get("url", ""))
        failed_checks = fingerprint_diagnostic_failures(sample["target"], diagnostic)
        diagnostic["failed_checks"] = failed_checks
        collection_failed = bool(sample.get("error")) and (not sample.get("text") or not sample.get("observed"))
        sample_passed = (
            signals["passed"]
            and not diagnostic["blocked_or_challenged"]
            and not diagnostic["missing_text"]
            and not failed_checks
        )
        if collection_failed:
            sample_passed = False
        analyzed.append({**sample, "signals": signals, "diagnostic": diagnostic, "passed": sample_passed})
    timings = [float(sample["elapsed_ms"]) for sample in analyzed]
    passed = sum(1 for sample in analyzed if sample["passed"])
    signal_fail_counts: dict[str, int] = {}
    challenge_counts: dict[str, int] = {}
    diagnostic_fail_counts: dict[str, int] = {}
    collection_error_count = 0
    challenged = 0
    for sample in analyzed:
        if sample.get("error") and (not sample.get("text") or not sample.get("observed")):
            collection_error_count += 1
        for signal in sample["signals"]["failed_signals"]:
            signal_fail_counts[signal] = signal_fail_counts.get(signal, 0) + 1
        if sample["diagnostic"]["blocked_or_challenged"]:
            challenged += 1
        for indicator in sample["diagnostic"]["challenge_indicators"]:
            challenge_counts[indicator] = challenge_counts.get(indicator, 0) + 1
        for check in sample["diagnostic"].get("failed_checks", []):
            diagnostic_fail_counts[check] = diagnostic_fail_counts.get(check, 0) + 1
    return {
        "implementation": implementation,
        "suite": "fingerprint",
        "targets": targets,
        "iterations": len(samples),
        "status": "passed" if passed == len(samples) else "failed",
        "passed_iterations": passed,
        "clean_signal_rate": passed / len(samples) if samples else 0.0,
        "challenge_free_rate": 1.0 - (challenged / len(samples) if samples else 0.0),
        "block_rate": challenge_counts.get("access_denied", 0) / len(samples) if samples else 0.0,
        "captcha_rate": challenge_counts.get("captcha", 0) / len(samples) if samples else 0.0,
        "p50_ms": statistics.median(timings) if timings else None,
        "p95_ms": percentile(timings, 0.95),
        "signal_fail_counts": signal_fail_counts,
        "challenge_counts": challenge_counts,
        "diagnostic_fail_counts": diagnostic_fail_counts,
        "collection_error_count": collection_error_count,
        "samples": analyzed,
    }


def run_playwright_like_smoke(
    implementation: str,
    iterations: int,
    reference_path: str | None = None,
    evidence_dir: Path | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    sync_playwright = _load_sync_playwright(implementation, reference_path)
    samples = []
    with header_capture_server() as server_url:
        with sync_playwright() as p:
            browser = _launch_chromium(p)
            try:
                for index in range(iterations):
                    page = browser.new_page()
                    started = time.perf_counter()
                    try:
                        sample = _collect_playwright_smoke_sample(
                            page,
                            implementation=implementation,
                            profile=profile,
                            iteration=index,
                            server_url=server_url,
                            started=started,
                            evidence_dir=evidence_dir,
                        )
                        samples.append(sample)
                    finally:
                        _safe_close(page)
            finally:
                _safe_close(browser)
    result = build_smoke_result(implementation, samples)
    if evidence_dir is not None:
        result["evidence_dir"] = str(evidence_dir)
    return result


def run_playwright_like_network(
    implementation: str,
    iterations: int,
    reference_path: str | None = None,
    evidence_dir: Path | None = None,
) -> dict[str, Any]:
    sync_playwright = _load_sync_playwright(implementation, reference_path)
    samples = []
    with header_capture_server() as server_url:
        with sync_playwright() as p:
            browser = _launch_chromium(p)
            try:
                for index in range(iterations):
                    page = browser.new_page()
                    started = time.perf_counter()
                    try:
                        samples.append(
                            _collect_playwright_smoke_sample(
                                page,
                                implementation=implementation,
                                profile=None,
                                iteration=index,
                                server_url=server_url,
                                started=started,
                                evidence_dir=evidence_dir,
                            )
                        )
                    finally:
                        _safe_close(page)
            finally:
                _safe_close(browser)
    result = build_network_result(implementation, samples)
    if evidence_dir is not None:
        result["evidence_dir"] = str(evidence_dir)
    return result


def run_playwright_like_matrix(
    implementation: str,
    iterations: int,
    reference_path: str | None = None,
    evidence_dir: Path | None = None,
) -> dict[str, Any]:
    sync_playwright = _load_sync_playwright(implementation, reference_path)
    profile_results: dict[str, dict[str, Any]] = {}
    with header_capture_server() as server_url:
        with sync_playwright() as p:
            fresh_samples = []
            browser = _launch_chromium(p)
            try:
                for index in range(iterations):
                    page = browser.new_page()
                    started = time.perf_counter()
                    try:
                        fresh_samples.append(
                            _collect_playwright_smoke_sample(
                                page,
                                implementation=implementation,
                                profile="fresh_profile",
                                iteration=index,
                                server_url=server_url,
                                started=started,
                                evidence_dir=evidence_dir / "fresh_profile" if evidence_dir is not None else None,
                            )
                        )
                    finally:
                        _safe_close(page)
            finally:
                _safe_close(browser)
            profile_results["fresh_profile"] = build_smoke_result(implementation, fresh_samples)

            warm_samples = []
            with tempfile.TemporaryDirectory(prefix=f"{implementation}-warm-antibot-") as user_data_dir:
                context = _launch_persistent_context(p.chromium, user_data_dir)
                try:
                    warmup_page = context.new_page()
                    try:
                        warmup_page.goto(f"{server_url}/headers?warmup=1")
                    finally:
                        _safe_close(warmup_page)
                    for index in range(iterations):
                        page = context.new_page()
                        started = time.perf_counter()
                        try:
                            warm_samples.append(
                                _collect_playwright_smoke_sample(
                                    page,
                                    implementation=implementation,
                                    profile="persistent_warm_profile",
                                    iteration=index,
                                    server_url=server_url,
                                    started=started,
                                    evidence_dir=(
                                        evidence_dir / "persistent_warm_profile" if evidence_dir is not None else None
                                    ),
                                )
                            )
                        finally:
                            _safe_close(page)
                finally:
                    _safe_close(context)
            profile_results["persistent_warm_profile"] = build_smoke_result(implementation, warm_samples)
    result = build_matrix_result(implementation, profile_results)
    if evidence_dir is not None:
        result["evidence_dir"] = str(evidence_dir)
    return result


def run_playwright_like_fingerprint(
    implementation: str,
    targets: list[str],
    iterations: int,
    reference_path: str | None = None,
    evidence_dir: Path | None = None,
    settle_ms: int = 2500,
    navigation_timeout_ms: int = 15000,
) -> dict[str, Any]:
    sync_playwright = _load_sync_playwright(implementation, reference_path)
    samples = []
    with sync_playwright() as p:
        browser = _launch_chromium(p)
        try:
            index = 0
            for target_iteration in range(iterations):
                for target in targets:
                    target_url = FINGERPRINT_TARGETS[target]["url"]
                    page = browser.new_page()
                    started = time.perf_counter()
                    error = None
                    try:
                        try:
                            page.goto(target_url, wait_until="domcontentloaded", timeout=navigation_timeout_ms)
                            if settle_ms:
                                page.wait_for_timeout(settle_ms)
                        except Exception as exc:
                            error = str(exc)
                        sample = _collect_playwright_page_sample(
                            page,
                            implementation=implementation,
                            suite="fingerprint",
                            target=target,
                            iteration=index,
                            started=started,
                            error=error,
                            evidence_dir=evidence_dir,
                        )
                        sample["target_iteration"] = target_iteration
                        samples.append(sample)
                        index += 1
                    finally:
                        _safe_close(page)
        finally:
            _safe_close(browser)
    result = build_fingerprint_result(implementation, samples, targets)
    if evidence_dir is not None:
        result["evidence_dir"] = str(evidence_dir)
    return result


def _collect_playwright_smoke_sample(
    page: Any,
    *,
    implementation: str,
    profile: str | None,
    iteration: int,
    server_url: str,
    started: float,
    evidence_dir: Path | None,
) -> dict[str, Any]:
    page.goto(f"{server_url}/headers?iteration={iteration}")
    headers_payload = page.evaluate("() => JSON.parse(document.querySelector('#payload').textContent)")
    observed = page.evaluate(SMOKE_JS)
    sample = {
        "iteration": iteration,
        "elapsed_ms": (time.perf_counter() - started) * 1000,
        "url": page.url,
        "headers": headers_payload["headers"],
        "header_order": headers_payload.get("header_order") or [],
        "request_version": headers_payload.get("request_version"),
        "observed": observed,
    }
    if profile is not None:
        sample["profile"] = profile
    if evidence_dir is not None:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        profile_suffix = f"-{profile}" if profile else ""
        sample_path = evidence_dir / f"{implementation}{profile_suffix}-smoke-{iteration}.json"
        sample_path.write_text(json.dumps(sample, indent=2, sort_keys=True), encoding="utf-8")
        try:
            page.screenshot(path=str(evidence_dir / f"{implementation}{profile_suffix}-smoke-{iteration}.png"))
        except Exception:
            pass
        sample["evidence"] = str(sample_path)
    return sample


def _collect_playwright_page_sample(
    page: Any,
    *,
    implementation: str,
    suite: str,
    target: str,
    iteration: int,
    started: float,
    error: str | None,
    evidence_dir: Path | None,
) -> dict[str, Any]:
    observed: dict[str, Any] = {}
    title = ""
    text = ""
    url = ""
    try:
        url = str(page.url)
    except Exception:
        url = FINGERPRINT_TARGETS[target]["url"]
    try:
        title = str(page.title())
    except Exception:
        pass
    try:
        text = str(page.evaluate("() => document.body ? document.body.innerText : ''") or "")
    except Exception as exc:
        if error is None:
            error = str(exc)
    try:
        observed = page.evaluate(SMOKE_JS)
    except Exception as exc:
        if error is None:
            error = str(exc)
    sample = {
        "target": target,
        "iteration": iteration,
        "elapsed_ms": (time.perf_counter() - started) * 1000,
        "url": url,
        "title": title,
        "text": text,
        "text_excerpt": text_excerpt(text),
        "observed": observed,
    }
    if error is not None:
        sample["error"] = error
    if evidence_dir is not None:
        target_dir = evidence_dir / target
        target_dir.mkdir(parents=True, exist_ok=True)
        sample_path = target_dir / f"{implementation}-{suite}-{iteration}.json"
        sample_path.write_text(json.dumps(sample, indent=2, sort_keys=True), encoding="utf-8")
        try:
            page.screenshot(path=str(target_dir / f"{implementation}-{suite}-{iteration}.png"), full_page=True)
        except Exception:
            pass
        sample["evidence"] = str(sample_path)
    return sample


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    evidence_dir = Path(args.evidence_dir) if args.evidence_dir else None
    if args.impl == "all":
        results = []
        for implementation in ("rustwright", "playwright"):
            try:
                impl_evidence = evidence_dir / implementation if evidence_dir is not None else None
                results.append(
                    run_playwright_like_smoke(
                        implementation,
                        args.iterations,
                        reference_path=args.reference_path,
                        evidence_dir=impl_evidence,
                    )
                )
            except Exception as error:
                results.append({"implementation": implementation, "suite": "smoke", "status": "skipped", "reason": str(error)})
        return {
            "implementation": "all",
            "suite": "smoke",
            "iterations": args.iterations,
            "results": results,
        }
    return run_playwright_like_smoke(
        args.impl,
        args.iterations,
        reference_path=args.reference_path,
        evidence_dir=evidence_dir,
    )


def run_network(args: argparse.Namespace) -> dict[str, Any]:
    evidence_dir = Path(args.evidence_dir) if args.evidence_dir else None
    if args.impl == "all":
        results = []
        for implementation in ("rustwright", "playwright"):
            try:
                impl_evidence = evidence_dir / implementation if evidence_dir is not None else None
                results.append(
                    run_playwright_like_network(
                        implementation,
                        args.iterations,
                        reference_path=args.reference_path,
                        evidence_dir=impl_evidence,
                    )
                )
            except Exception as error:
                results.append({"implementation": implementation, "suite": "network", "status": "skipped", "reason": str(error)})
        return {
            "implementation": "all",
            "suite": "network",
            "iterations": args.iterations,
            "results": results,
            "scope": "local_l7_header_and_client_hint_consistency",
        }
    return run_playwright_like_network(
        args.impl,
        args.iterations,
        reference_path=args.reference_path,
        evidence_dir=evidence_dir,
    )


def run_matrix(args: argparse.Namespace) -> dict[str, Any]:
    evidence_dir = Path(args.evidence_dir) if args.evidence_dir else None
    if args.impl == "all":
        results = []
        for implementation in ("rustwright", "playwright"):
            try:
                impl_evidence = evidence_dir / implementation if evidence_dir is not None else None
                results.append(
                    run_playwright_like_matrix(
                        implementation,
                        args.iterations,
                        reference_path=args.reference_path,
                        evidence_dir=impl_evidence,
                    )
                )
            except Exception as error:
                results.append({"implementation": implementation, "suite": "matrix", "status": "skipped", "reason": str(error)})
        return {
            "implementation": "all",
            "suite": "matrix",
            "iterations": args.iterations * 2,
            "profiles": ["fresh_profile", "persistent_warm_profile"],
            "results": results,
        }
    return run_playwright_like_matrix(
        args.impl,
        args.iterations,
        reference_path=args.reference_path,
        evidence_dir=evidence_dir,
    )


def run_fingerprint(args: argparse.Namespace) -> dict[str, Any]:
    targets = select_fingerprint_targets(args.target, args.sample)
    evidence_dir = Path(args.evidence_dir) if args.evidence_dir else None
    if args.impl == "all":
        results = []
        for implementation in ("rustwright", "playwright"):
            try:
                impl_evidence = evidence_dir / implementation if evidence_dir is not None else None
                results.append(
                    run_playwright_like_fingerprint(
                        implementation,
                        targets,
                        args.iterations,
                        reference_path=args.reference_path,
                        evidence_dir=impl_evidence,
                        settle_ms=args.settle_ms,
                        navigation_timeout_ms=args.navigation_timeout_ms,
                    )
                )
            except Exception as error:
                results.append(
                    {
                        "implementation": implementation,
                        "suite": "fingerprint",
                        "targets": targets,
                        "status": "skipped",
                        "reason": str(error),
                    }
                )
        return {
            "implementation": "all",
            "suite": "fingerprint",
            "targets": targets,
            "iterations": len(targets) * args.iterations,
            "results": results,
        }
    return run_playwright_like_fingerprint(
        args.impl,
        targets,
        args.iterations,
        reference_path=args.reference_path,
        evidence_dir=evidence_dir,
        settle_ms=args.settle_ms,
        navigation_timeout_ms=args.navigation_timeout_ms,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Rustwright anti-bot and fingerprint benchmark subsets.")
    parser.add_argument("--suite", choices=["smoke", "network", "fingerprint", "matrix"], default="smoke")
    parser.add_argument("--impl", choices=["rustwright", "playwright", "all"], default="rustwright")
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument(
        "--target",
        action="append",
        choices=[*FINGERPRINT_TARGETS.keys(), "all"],
        help="Fingerprint target to run. Repeatable. Defaults to all targets for --suite fingerprint.",
    )
    parser.add_argument("--sample", type=int, help="Run only the first N fingerprint targets.")
    parser.add_argument("--settle-ms", type=int, default=2500, help="Extra wait after public fingerprint page load.")
    parser.add_argument(
        "--navigation-timeout-ms",
        type=int,
        default=15000,
        help="Navigation timeout for public fingerprint pages.",
    )
    parser.add_argument(
        "--reference-path",
        default=str(ROOT / ".audit-playwright"),
        help="Path containing a real Playwright installation for --impl playwright or --impl all.",
    )
    parser.add_argument("--evidence-dir", help="Optional directory for per-run JSON and screenshots.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.iterations < 1:
        parser.error("--iterations must be >= 1")
    if args.settle_ms < 0:
        parser.error("--settle-ms must be >= 0")
    if args.navigation_timeout_ms < 1:
        parser.error("--navigation-timeout-ms must be >= 1")
    try:
        if args.suite == "smoke":
            result = run_smoke(args)
        elif args.suite == "network":
            result = run_network(args)
        elif args.suite == "fingerprint":
            result = run_fingerprint(args)
        else:
            result = run_matrix(args)
    except ValueError as error:
        parser.error(str(error))
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.impl == "all":
        print(f"anti-bot {args.suite} comparison across {result['iterations']} iteration(s)")
        for item in result["results"]:
            if item.get("status") == "skipped":
                print(f"{item['implementation']:16s} skipped: {item['reason']}")
            else:
                print(
                    f"{item['implementation']:16s} {item['status']:7s} "
                    f"{item['passed_iterations']}/{item['iterations']} clean "
                    f"p50={item['p50_ms']:.2f} ms"
                )
                if item["signal_fail_counts"]:
                    print(f"{'':16s} failures: {item['signal_fail_counts']}")
                if item.get("diagnostic_fail_counts"):
                    print(f"{'':16s} diagnostics: {item['diagnostic_fail_counts']}")
                if item.get("collection_error_count"):
                    print(f"{'':16s} collection errors: {item['collection_error_count']}")
    else:
        print(
            f"{result['implementation']} {args.suite}: {result['status']} "
            f"{result['passed_iterations']}/{result['iterations']} clean "
            f"p50={result['p50_ms']:.2f} ms p95={result['p95_ms']:.2f} ms"
        )
        if result["signal_fail_counts"]:
            print(f"failures: {result['signal_fail_counts']}")
        if result.get("diagnostic_fail_counts"):
            print(f"diagnostics: {result['diagnostic_fail_counts']}")
        if result.get("collection_error_count"):
            print(f"collection errors: {result['collection_error_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
