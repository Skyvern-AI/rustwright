"""Compatibility re-export of the Rustwright pytest plugin.

Exposes the full plugin surface (hooks, fixtures, and typing names) so that
``-p pytest_playwright.pytest_playwright`` keeps working with plugin autoload
disabled. Loading it a second time next to Rustwright's own ``pytest11`` entry
point — e.g. when a real pytest-playwright distribution's entry point resolves
here through the compat alias — is harmless: option registration skips flags
that are already taken and ``pytest_generate_tests`` parametrizes
``browser_name`` at most once per test.
"""

from rustwright.pytest_plugin import *  # noqa: F401,F403
from rustwright.pytest_plugin import CreateContextCallback  # noqa: F401
