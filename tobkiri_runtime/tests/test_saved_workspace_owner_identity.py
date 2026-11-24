"""Typed canonical owner locals reject malformed replies before construction."""

import pytest

from tests.test_saved_workspace_context import conversation, resolve


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", None),
        ("id", 123),
        ("id", "../outside"),
        ("conversation_revision", None),
        ("conversation_revision", True),
        ("conversation_revision", 0),
        ("conversation_revision", "1"),
    ],
)
def test_canonical_owner_identity_locals_reject_malformed_context(
    field: str, value: object,
) -> None:
    """Owner facts must narrow to a finite identifier and positive int."""
    source = conversation()
    source[field] = value
    with pytest.raises(ValueError, match="conversation identity"):
        resolve(source)
