#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_speed_lib import run_and_capture


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit a WebPageTest run or print the API command.")
    parser.add_argument("--url", required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--location", default=None)
    parser.add_argument("--browser", default=None)
    parser.add_argument("--script", default=None, help="Optional WebPageTest script file.")
    parser.add_argument("--endpoint", default="https://www.webpagetest.org/runtest.php")
    parser.add_argument("--run", action="store_true", help="Actually submit to WebPageTest. Default is dry-run.")
    parser.add_argument("--dry-run", action="store_true", help="Print the curl command without submitting.")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    api_key = os.environ.get("WEBPAGETEST_API_KEY", "")
    params = {
        "url": args.url,
        "runs": str(args.runs),
        "f": "json",
    }
    if api_key:
        params["k"] = api_key
    if args.location:
        params["location"] = args.location
    if args.browser:
        params["browser"] = args.browser
    if args.script:
        params["script"] = Path(args.script).read_text(encoding="utf-8")

    encoded = urllib.parse.urlencode(params)
    endpoint = f"{args.endpoint}?{encoded}"
    printable = endpoint.replace(api_key, "REDACTED") if api_key else endpoint

    if args.dry_run or not args.run:
        print(f"curl {printable!r}")
        if not args.run:
            print("Pass --run and WEBPAGETEST_API_KEY to submit.")
        return 0
    if not api_key:
        raise SystemExit("WEBPAGETEST_API_KEY is required when --run is set.")

    command = [
        sys.executable,
        "-c",
        (
            "import urllib.request, sys; "
            "url = sys.argv[1]; "
            "print(urllib.request.urlopen(url, timeout=%d).read().decode())" % args.timeout
        ),
        endpoint,
    ]
    return run_and_capture(
        command,
        benchmark_id="webpagetest",
        name="submission",
        timeout=args.timeout + 5,
        metadata={
            "url": args.url,
            "runs": args.runs,
            "location": args.location,
            "browser": args.browser,
            "endpoint": args.endpoint,
            "script": args.script,
            "api_key_present": bool(api_key),
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())
