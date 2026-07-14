#!/usr/bin/env python3
"""Explicit remote-CDP entry point for the shared form-fill workload."""

from __future__ import annotations

import os

from fill_form import main


if __name__ == "__main__":
    if not os.environ.get("CDP_URL", "").strip():
        raise ValueError("CDP_URL is required for the remote workload")
    main()
