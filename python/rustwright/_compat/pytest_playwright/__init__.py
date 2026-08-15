"""Compatibility surface for the optional pytest plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .pytest_playwright import *

if TYPE_CHECKING:
    from .pytest_playwright import CreateContextCallback as CreateContextCallback
