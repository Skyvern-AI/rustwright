#!/usr/bin/env python3
"""Explicit remote-CDP entry point for the shared form-fill workload."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager

from fill_form import main
from harness.skyvern_session import SkyvernSession


@contextmanager
def skyvern_remote_environment() -> Iterator[None]:
    """Install an ephemeral session in the workload environment."""
    previous_url = os.environ.get("CDP_URL")
    previous_headers = os.environ.get("CDP_CONNECT_HEADERS")
    with SkyvernSession() as session:
        assert session.browser_address is not None
        os.environ["CDP_URL"] = session.browser_address
        os.environ["CDP_CONNECT_HEADERS"] = json.dumps(
            session.cdp_headers, separators=(",", ":")
        )
        try:
            yield
        finally:
            if previous_url is None:
                os.environ.pop("CDP_URL", None)
            else:
                os.environ["CDP_URL"] = previous_url
            if previous_headers is None:
                os.environ.pop("CDP_CONNECT_HEADERS", None)
            else:
                os.environ["CDP_CONNECT_HEADERS"] = previous_headers


if __name__ == "__main__":
    cdp_url = os.environ.get("CDP_URL", "").strip()
    skyvern_session = os.environ.get("SKYVERN_SESSION", "0")
    if skyvern_session not in {"0", "1"}:
        raise ValueError("SKYVERN_SESSION must be 0 or 1")
    if cdp_url:
        main()
    elif skyvern_session == "1":
        with skyvern_remote_environment():
            main()
    else:
        raise ValueError(
            "CDP_URL is required unless SKYVERN_SESSION=1 provisions a session"
        )
