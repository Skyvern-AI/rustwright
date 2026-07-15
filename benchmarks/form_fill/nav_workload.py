import json
import os
import time
from pathlib import Path

from fill_form import parse_cdp_connect_headers, make_remote_context
from harness.library_probe import run_library_probes


backend = os.environ.get("BACKEND", "playwright")
if backend == "rustwright":
    from rustwright.sync_api import sync_playwright
else:
    from playwright.sync_api import sync_playwright


def load_case():
    case_json = os.environ.get("NAV_CASE_JSON")
    case_file = os.environ.get("NAV_CASE_FILE")
    if case_json:
        case = json.loads(case_json)
    elif case_file:
        with open(case_file, encoding="utf-8") as file:
            case = json.load(file)
    else:
        raise SystemExit("NAV_CASE_JSON or NAV_CASE_FILE is required")

    if not isinstance(case, dict) or not isinstance(case.get("nav_steps"), list):
        raise SystemExit("navigation case must be an object with a nav_steps list")
    return case


def main():
    if not os.environ.get("CDP_URL"):
        raise SystemExit("CDP_URL is required; this navigation workload is remote-only")

    case = load_case()
    out_dir = Path(os.environ["OUT_DIR"])
    out_dir.mkdir(parents=True, exist_ok=True)

    total_start = time.monotonic()
    timeline = []
    timeline_cursor_s = 0.0

    def finish_interval(label, kind, end_s=None):
        nonlocal timeline_cursor_s
        if end_s is None:
            end_s = time.monotonic() - total_start
        interval = {
            "label": label,
            "kind": kind,
            "t0_s": timeline_cursor_s,
            "t1_s": end_s,
        }
        timeline.append(interval)
        timeline_cursor_s = end_s
        return interval

    step_timings = []
    probe = None
    with sync_playwright() as sync_playwright_instance:
        cdp_url = os.environ["CDP_URL"]
        opts = {"timeout": 120000}
        h = parse_cdp_connect_headers(os.environ.get("CDP_CONNECT_HEADERS", ""))
        if h:
            opts["headers"] = h
        browser = sync_playwright_instance.chromium.connect_over_cdp(cdp_url, **opts)
        context = make_remote_context(browser)
        page = context.pages[0] if context.pages else context.new_page()
        finish_interval("library startup and remote CDP connect", "library")

        try:
            for index, step in enumerate(case["nav_steps"], start=1):
                if not isinstance(step, dict):
                    raise ValueError(f"navigation step {index} must be an object")

                op = step.get("op")
                if op == "goto":
                    kind = "navigation"
                    page.goto(
                        step["url"],
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                elif op == "wait":
                    kind = "page_wait"
                    page.wait_for_selector(step["selector"], timeout=30000)
                elif op == "click":
                    kind = "library"
                    page.click(step["selector"], timeout=30000)
                elif op == "back":
                    kind = "navigation"
                    page.go_back(wait_until="commit")
                elif op == "forward":
                    kind = "navigation"
                    page.go_forward(wait_until="commit")
                elif op == "eval":
                    kind = "library"
                    page.evaluate(step["expression"])
                else:
                    raise ValueError(f"unsupported navigation operation at step {index}")

                interval = finish_interval(f"step {index}: {op}", kind)
                step_timings.append(
                    {
                        "op": op,
                        "kind": kind,
                        "ms": round(
                            (interval["t1_s"] - interval["t0_s"]) * 1000,
                            3,
                        ),
                    }
                )

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

    launch_ms = (timeline[0]["t1_s"] - timeline[0]["t0_s"]) * 1000
    navigation_ms = sum(
        (interval["t1_s"] - interval["t0_s"]) * 1000
        for interval in timeline
        if interval["kind"] == "navigation"
    )
    library_latency_ms = sum(
        (interval["t1_s"] - interval["t0_s"]) * 1000
        for interval in timeline
        if interval["kind"] == "library"
    )
    page_wait_ms = sum(
        (interval["t1_s"] - interval["t0_s"]) * 1000
        for interval in timeline
        if interval["kind"] == "page_wait"
    )
    timings = {
        "launch_ms": round(launch_ms, 3),
        "navigation_ms": round(navigation_ms, 3),
        "page_wait_ms": round(page_wait_ms, 3),
        "library_latency_ms": round(library_latency_ms, 3),
        "action_ms": round(library_latency_ms, 3),
        "total_ms": round(timeline[-1]["t1_s"] * 1000, 3),
        "scripted_pause_ms": 0,
        "steps": step_timings,
        "steps_requested": len(case["nav_steps"]),
        "steps_completed": len(step_timings),
        "reached_final_step": len(step_timings) == len(case["nav_steps"]),
        "library_probe": probe,
        "ok": True,
    }

    with open(out_dir / "timeline.json", "w", encoding="utf-8") as file:
        json.dump({"intervals": timeline}, file, separators=(",", ":"))
    with open(out_dir / "timings.json", "w", encoding="utf-8") as file:
        json.dump(timings, file, separators=(",", ":"))
    print(json.dumps(timings, separators=(",", ":")))


if __name__ == "__main__":
    main()
