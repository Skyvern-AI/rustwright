"""Exit-code categories for agent errors."""

import pytest

from rustwright._agent.errors import AgentError


@pytest.mark.parametrize(
    "code, expected",
    [
        # A malformed ref *string* is a client argument error, not a ref
        # failure, so it shares the validation exit category.
        ("invalid_ref", 2),
        ("invalid_argument", 2),
        ("invalid_request", 2),
        # A well-formed ref that no longer resolves is a ref failure.
        ("stale_ref", 5),
        ("ref_integrity_error", 5),
        ("timeout", 4),
        ("session_busy", 3),
        ("session_lost", 3),
        ("session_not_found", 3),
        ("browser_error", 1),
    ],
)
def test_exit_code_categories(code, expected):
    assert AgentError(code, "message").exit_code == expected


def test_to_dict_shape():
    error = AgentError("stale_ref", "gone", "snapshot again")
    assert error.to_dict() == {"code": "stale_ref", "message": "gone", "hint": "snapshot again"}
