"""Private foundations for accessibility-based agent operations."""

from .errors import AgentError
from .refs import RefRegistry, RefState, resolve

__all__ = ["AgentError", "RefRegistry", "RefState", "resolve"]
