#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os


BASEMARK_COMMUNITY_URL = "https://web.basemark.com/?mode=community"


def main() -> int:
    parser = argparse.ArgumentParser(description="Print guarded Basemark Web community-mode commands.")
    parser.add_argument("--print-command", action="store_true", help="Print a browser launch command after env opt-in.")
    parser.add_argument("--browser", default="chrome")
    args = parser.parse_args()

    print(BASEMARK_COMMUNITY_URL)
    if not args.print_command:
        print("Pass --print-command with BASEMARK_ALLOW_COMMUNITY_MODE=1 after reviewing Basemark terms.")
        return 0
    if os.environ.get("BASEMARK_ALLOW_COMMUNITY_MODE") != "1":
        raise SystemExit("Set BASEMARK_ALLOW_COMMUNITY_MODE=1 after reviewing Basemark community-mode terms.")
    print(f"{args.browser} {BASEMARK_COMMUNITY_URL!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
