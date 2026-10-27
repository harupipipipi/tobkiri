"""Opaque secret resources; plaintext is never interpolated into guest headers."""

from dataclasses import dataclass
from typing import Any
from ._binding import invoke


@dataclass(frozen=True, repr=False)
class SecretRef:
    """A generated resource, readable only by authorized core network imports."""

    resource: Any

    def __repr__(self) -> str:
        return "SecretRef(<opaque>)"


def get(name: str) -> SecretRef:
    """Get an opaque reference scoped to this Pack and Profile."""
    return SecretRef(invoke("secrets", "get", name=name))


def reveal(name: str) -> str:
    """Reveal only through the independently gated secrets.reveal permission."""
    return invoke("secrets", "reveal", name=name)
