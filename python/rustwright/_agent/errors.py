"""Stable errors shared by agent-facing interfaces."""

from typing import Dict, Optional


class AgentError(Exception):
    """An error with a stable machine-readable code and process exit category."""

    def __init__(self, code: str, message: str, hint: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    @property
    def exit_code(self) -> int:
        # A well-formed ref that is stale or ambiguous is a ref failure (5); a
        # malformed ref *string* is an argument/syntax error (2).
        if self.code in {"stale_ref", "ref_integrity_error"}:
            return 5
        if self.code == "timeout":
            return 4
        if self.code in {"session_busy", "session_lost", "session_not_found"}:
            return 3
        if self.code in {"invalid_argument", "invalid_request", "invalid_ref", "unsupported_platform"}:
            return 2
        return 1

    def to_dict(self) -> Dict[str, Optional[str]]:
        return {"code": self.code, "message": self.message, "hint": self.hint}
