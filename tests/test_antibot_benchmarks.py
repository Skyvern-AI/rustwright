from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_antibot_benchmarks",
    ROOT / "tools" / "run_antibot_benchmarks.py",
)
assert SPEC is not None
run_antibot_benchmarks = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(run_antibot_benchmarks)


def clean_observed():
    return {
        "webdriver": None,
        "webdriverInNavigator": False,
        "userAgent": "TestBrowser/1.0",
        "language": "en-US",
        "languages": ["en-US", "en"],
        "userAgentData": {"platform": "macOS", "mobile": False},
        "automationGlobals": [],
    }


def clean_headers():
    return {
        "User-Agent": "TestBrowser/1.0",
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua-platform": '"macOS"',
        "sec-ch-ua-mobile": "?0",
    }


def clean_navigation_headers():
    headers = clean_headers()
    headers.update(
        {
            "Accept": "text/html,application/xhtml+xml",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
    )
    return headers


def test_smoke_analysis_flags_malformed_language_identity():
    observed = {
        "webdriver": None,
        "webdriverInNavigator": True,
        "userAgent": "TestBrowser/1.0",
        "language": "en-US",
        "languages": ["en-US", "en;q=0.9"],
        "userAgentData": {"platform": "macOS", "mobile": False},
        "automationGlobals": [],
    }
    headers = {
        "User-Agent": "TestBrowser/1.0",
        "Accept-Language": "en-US,en;q=0.9;q=0.9",
        "sec-ch-ua-platform": '"macOS"',
        "sec-ch-ua-mobile": "?0",
    }

    signals = run_antibot_benchmarks.analyze_smoke_signals(observed, headers)

    assert signals["malformed_accept_language"]
    assert signals["malformed_navigator_languages"]
    assert not signals["passed"]


def test_smoke_analysis_accepts_consistent_language_identity():
    observed = {
        "webdriver": None,
        "webdriverInNavigator": False,
        "userAgent": "TestBrowser/1.0",
        "language": "en-US",
        "languages": ["en-US", "en"],
        "userAgentData": {"platform": "macOS", "mobile": False},
        "automationGlobals": [],
    }
    headers = {
        "User-Agent": "TestBrowser/1.0",
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua-platform": '"macOS"',
        "sec-ch-ua-mobile": "?0",
    }

    signals = run_antibot_benchmarks.analyze_smoke_signals(observed, headers)

    assert signals["failed_signals"] == []
    assert signals["passed"]


def test_smoke_analysis_flags_webdriver_property_presence():
    observed = {
        "webdriver": None,
        "webdriverInNavigator": True,
        "userAgent": "TestBrowser/1.0",
        "language": "en-US",
        "languages": ["en-US", "en"],
        "userAgentData": {"platform": "macOS", "mobile": False},
        "automationGlobals": [],
    }
    headers = {
        "User-Agent": "TestBrowser/1.0",
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua-platform": '"macOS"',
        "sec-ch-ua-mobile": "?0",
    }

    signals = run_antibot_benchmarks.analyze_smoke_signals(observed, headers)

    assert "webdriver_property_present" in signals["failed_signals"]
    assert not signals["passed"]


def test_network_analysis_accepts_consistent_navigation_headers():
    sample = {
        "headers": clean_navigation_headers(),
        "header_order": list(clean_navigation_headers()),
        "request_version": "HTTP/1.1",
        "observed": clean_observed(),
        "elapsed_ms": 10.0,
    }

    signals = run_antibot_benchmarks.analyze_network_signals(sample)

    assert signals["failed_signals"] == []
    assert signals["passed"]
    assert signals["http1_local_capture"]


def test_network_analysis_flags_headless_and_missing_header_mismatch():
    observed = clean_observed()
    observed["userAgent"] = "HeadlessChrome/1.0"
    sample = {
        "headers": {"User-Agent": "HeadlessChrome/1.0"},
        "header_order": ["User-Agent"],
        "request_version": "HTTP/1.1",
        "observed": observed,
        "elapsed_ms": 10.0,
    }

    signals = run_antibot_benchmarks.analyze_network_signals(sample)

    assert "headless_ua" in signals["failed_signals"]
    assert "missing_navigation_headers" in signals["failed_signals"]
    assert not signals["passed"]


def test_network_result_aggregates_signal_failures():
    observed = clean_observed()
    observed["userAgent"] = "HeadlessChrome/1.0"
    result = run_antibot_benchmarks.build_network_result(
        "playwright",
        [
            {
                "iteration": 0,
                "elapsed_ms": 10.0,
                "headers": {"User-Agent": "HeadlessChrome/1.0"},
                "header_order": ["User-Agent"],
                "request_version": "HTTP/1.1",
                "observed": observed,
            }
        ],
    )

    assert result["suite"] == "network"
    assert result["status"] == "failed"
    assert result["signal_fail_counts"]["headless_ua"] == 1


def test_fingerprint_target_selection_supports_sampling():
    targets = run_antibot_benchmarks.select_fingerprint_targets(None, 2)

    assert targets == ["sannysoft", "creepjs"]


def test_fingerprint_text_adapter_flags_sannysoft_hard_failures():
    diagnostic = run_antibot_benchmarks.analyze_fingerprint_text(
        "sannysoft",
        """
        WebDriver present failed
        Plugins Length (Old) 0
        Permissions denied
        HEADCHR_PLUGINS warning
        """,
        "https://bot.sannysoft.com/",
    )

    assert diagnostic["failures"] == 1
    assert not diagnostic["blocked_or_challenged"]


def test_fingerprint_text_adapter_flags_challenge_pages():
    diagnostic = run_antibot_benchmarks.analyze_fingerprint_text(
        "browserscan",
        "Access Denied Reference #18. Please verify you are human.",
        "https://errors.edgesuite.net/",
    )

    assert diagnostic["blocked_or_challenged"]
    assert "access_denied" in diagnostic["challenge_indicators"]


def test_creepjs_adapter_flags_high_headless_and_worker_leaks():
    diagnostic = run_antibot_benchmarks.analyze_fingerprint_text(
        "creepjs",
        """
        81% like headless
        ServiceWorkerGlobalScope
        userAgent: Mozilla/5.0 HeadlessChrome/148.0.0.0
        userAgentData: HeadlessChrome 148
        """,
        "https://abrahamjuliot.github.io/creepjs/",
    )

    failed_checks = run_antibot_benchmarks.fingerprint_diagnostic_failures("creepjs", diagnostic)

    assert diagnostic["headless_percent"] == 81.0
    assert diagnostic["headless_chrome_text"]
    assert diagnostic["worker_headless_ua"]
    assert "creepjs_headless_percent" in failed_checks
    assert "creepjs_worker_headless_ua" in failed_checks


def test_deviceandbrowserinfo_adapter_extracts_positive_json_details():
    diagnostic = run_antibot_benchmarks.analyze_fingerprint_text(
        "deviceandbrowserinfo",
        """
        {
          "isBot": true,
          "details": {
            "hasBotUserAgent": false,
            "isAutomatedWithCDP": true,
            "hasInconsistentWorkerValues": true
          }
        }
        """,
        "https://deviceandbrowserinfo.com/are_you_a_bot",
    )

    failed_checks = run_antibot_benchmarks.fingerprint_diagnostic_failures(
        "deviceandbrowserinfo",
        diagnostic,
    )

    assert diagnostic["is_bot"] is True
    assert diagnostic["positive_details"] == ["hasInconsistentWorkerValues", "isAutomatedWithCDP"]
    assert "deviceandbrowserinfo_is_bot" in failed_checks
    assert "deviceandbrowserinfo_positive_details" in failed_checks


def test_browserscan_adapter_accepts_normal_report_with_educational_terms():
    text = """
    Test Results:
    Normal
    WebDriver
    Normal
    WebDriver Advance
    Normal
    Headless Chrome
    Normal
    CDP
    Normal
    Dev Tool
    Normal
    BrowserScan explains Cloudflare Turnstile, hCaptcha, and reCAPTCHA as bot defenses.
    """
    diagnostic = run_antibot_benchmarks.analyze_fingerprint_text(
        "browserscan",
        text,
        "https://www.browserscan.net/bot-detection",
    )
    result = run_antibot_benchmarks.build_fingerprint_result(
        "rustwright",
        [
            {
                "target": "browserscan",
                "iteration": 0,
                "elapsed_ms": 1.0,
                "url": "https://www.browserscan.net/bot-detection",
                "text": text,
                "observed": clean_observed(),
                "headers": clean_headers(),
            }
        ],
        ["browserscan"],
    )

    assert not diagnostic["blocked_or_challenged"]
    assert diagnostic["statuses"]["test_results"] == "Normal"
    assert diagnostic["statuses"]["cdp"] == "Normal"
    assert diagnostic["bad_statuses"] == {}
    assert result["status"] == "passed"


def test_fingerprint_result_fails_creepjs_diagnostics_even_with_clean_smoke_signals():
    result = run_antibot_benchmarks.build_fingerprint_result(
        "rustwright",
        [
            {
                "target": "creepjs",
                "iteration": 0,
                "elapsed_ms": 1.0,
                "url": "https://abrahamjuliot.github.io/creepjs/",
                "text": "81% like headless\nworker userAgent HeadlessChrome/148.0.0.0",
                "observed": clean_observed(),
                "headers": clean_headers(),
            }
        ],
        ["creepjs"],
    )

    assert result["status"] == "failed"
    assert result["diagnostic_fail_counts"]["creepjs_headless_percent"] == 1


def test_matrix_result_aggregates_fresh_and_warm_profiles():
    fresh = run_antibot_benchmarks.build_smoke_result(
        "rustwright",
        [
            {
                "profile": "fresh_profile",
                "iteration": 0,
                "elapsed_ms": 10.0,
                "headers": clean_headers(),
                "observed": clean_observed(),
            }
        ],
    )
    warm = run_antibot_benchmarks.build_smoke_result(
        "rustwright",
        [
            {
                "profile": "persistent_warm_profile",
                "iteration": 0,
                "elapsed_ms": 15.0,
                "headers": clean_headers(),
                "observed": clean_observed(),
            }
        ],
    )

    result = run_antibot_benchmarks.build_matrix_result(
        "rustwright",
        {"fresh_profile": fresh, "persistent_warm_profile": warm},
    )

    assert result["suite"] == "matrix"
    assert result["status"] == "passed"
    assert result["passed_iterations"] == 2
    assert result["profile_names"] == ["fresh_profile", "persistent_warm_profile"]
    assert result["profile_deltas"]["persistent_warm_minus_fresh_p50_ms"] == 5.0


def test_matrix_result_keeps_profile_signal_fail_counts():
    failing_observed = clean_observed()
    failing_observed["webdriverInNavigator"] = True
    fresh = run_antibot_benchmarks.build_smoke_result(
        "playwright",
        [
            {
                "profile": "fresh_profile",
                "iteration": 0,
                "elapsed_ms": 10.0,
                "headers": clean_headers(),
                "observed": failing_observed,
            }
        ],
    )
    warm = run_antibot_benchmarks.build_smoke_result(
        "playwright",
        [
            {
                "profile": "persistent_warm_profile",
                "iteration": 0,
                "elapsed_ms": 11.0,
                "headers": clean_headers(),
                "observed": clean_observed(),
            }
        ],
    )

    result = run_antibot_benchmarks.build_matrix_result(
        "playwright",
        {"fresh_profile": fresh, "persistent_warm_profile": warm},
    )

    assert result["status"] == "failed"
    assert result["passed_iterations"] == 1
    assert result["signal_fail_counts"]["webdriver_property_present"] == 1
