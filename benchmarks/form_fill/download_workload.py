import json
import os
import time
from pathlib import Path

from fill_form import parse_cdp_connect_headers, make_remote_context
from harness.library_probe import run_library_probes


if os.environ.get("BACKEND", "rustwright") == "rustwright":
    from rustwright.sync_api import sync_playwright
else:
    from playwright.sync_api import sync_playwright


def main() -> None:
    if not os.environ.get("CDP_URL"):
        raise RuntimeError("CDP_URL is required; download_workload.py is remote-only")
    cdp_url = os.environ["CDP_URL"]

    case_json = os.environ.get("DL_CASE_JSON") or os.environ.get("NAV_CASE_JSON")
    if not case_json:
        raise RuntimeError("DL_CASE_JSON or NAV_CASE_JSON is required")
    case = json.loads(case_json)
    trigger = case["download_trigger"]
    if trigger.get("op") != "click":
        raise ValueError("download_trigger.op must be 'click'")
    if case.get("expect_download") is not True:
        raise ValueError("expect_download must be true")

    out_dir = Path(os.environ["OUT_DIR"])
    out_dir.mkdir(parents=True, exist_ok=True)

    timeline = []
    started_at = time.perf_counter()
    cursor_s = 0.0

    def finish_interval(label: str, kind: str, end_s: float | None = None) -> None:
        nonlocal cursor_s
        if end_s is None:
            end_s = time.perf_counter() - started_at
        timeline.append(
            {"label": label, "kind": kind, "t0_s": cursor_s, "t1_s": end_s}
        )
        cursor_s = end_s

    download_ok = False
    suggested_filename = ""
    probe = None
    steps_completed = 0

    with sync_playwright() as sp:
        opts = {"timeout": 120000}
        h = parse_cdp_connect_headers(os.environ.get("CDP_CONNECT_HEADERS", ""))
        if h:
            opts["headers"] = h
        browser = sp.chromium.connect_over_cdp(cdp_url, **opts)
        context = make_remote_context(browser)
        finish_interval("library startup and remote CDP connect", "library")

        try:
            page = context.new_page()
            page.goto(case["url"], wait_until="domcontentloaded", timeout=60000)
            finish_interval("page navigation", "navigation")
            steps_completed += 1

            with page.expect_download(timeout=60000) as dl_info:
                page.click(trigger["selector"], timeout=30000)
                finish_interval("download click dispatch", "library")
                steps_completed += 1
            download = dl_info.value
            finish_interval("download event wait", "page_wait")
            steps_completed += 1
            suggested_filename = str(download.suggested_filename)
            download_ok = True

            finish_interval("teardown", "library")

            try:
                probe = run_library_probes(
                    page, reps=int(os.environ.get("BENCH_PROBE_REPS", "20"))
                )
            except Exception:
                probe = None
        finally:
            context.close()
            browser.close()

    total_ms = timeline[-1]["t1_s"] * 1000.0

    def interval_ms(kind: str) -> float:
        return sum(
            (interval["t1_s"] - interval["t0_s"]) * 1000.0
            for interval in timeline
            if interval["kind"] == kind
        )

    launch_ms = sum(
        (interval["t1_s"] - interval["t0_s"]) * 1000.0
        for interval in timeline
        if interval["label"] == "library startup and remote CDP connect"
    )
    timings = {
        "launch_ms": round(launch_ms, 3),
        "navigation_ms": round(interval_ms("navigation"), 3),
        "page_wait_ms": round(interval_ms("page_wait"), 3),
        "library_latency_ms": round(interval_ms("library"), 3),
        "action_ms": round(interval_ms("library"), 3),
        "total_ms": total_ms,
        "scripted_pause_ms": 0,
        "download_ok": bool(download_ok),
        "suggested_filename": str(suggested_filename),
        "steps_requested": 3,
        "steps_completed": steps_completed,
        "reached_final_step": steps_completed == 3,
        "library_probe": probe,
        "ok": bool(download_ok),
    }

    (out_dir / "timeline.json").write_text(
        json.dumps({"intervals": timeline}, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "timings.json").write_text(
        json.dumps(timings, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(timings, separators=(",", ":")))


if __name__ == "__main__":
    main()
