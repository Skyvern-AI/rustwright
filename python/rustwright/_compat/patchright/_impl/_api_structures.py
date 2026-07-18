"""Compatibility for imports of Patchright's private API-structures module.

The definitions live once in the playwright compat namespace; this mirrors the
re-export pattern `_errors.py` uses for sharing between the two namespaces.
"""

from rustwright._compat.playwright._impl._api_structures import *  # noqa: F401,F403
from rustwright._compat.playwright._impl._api_structures import __all__ as __all__
