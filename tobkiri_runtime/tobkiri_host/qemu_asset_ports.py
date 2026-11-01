"""Product-independent verified asset boundary for authenticated QEMU drivers."""

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol


class VerifiedQemuAsset(Protocol):
    """Read-only identity of one already admitted runtime input."""

    @property
    def path(self) -> Path:
        """Return its canonical path."""
        ...

    @property
    def digest(self) -> str:
        """Return its externally pinned content digest."""
        ...


class VerifiedQemuAssets(Protocol):
    """Host-facing verification port supplied by the owning provisioner.

    Implementations must remeasure every pinned asset and the closed bundle.
    This port does not make an untrusted guest-supplied object authoritative.
    """

    @property
    def manifest_digest(self) -> str:
        """Return the independently trusted manifest digest."""
        ...

    @property
    def files(self) -> Mapping[str, VerifiedQemuAsset]:
        """Return immutable asset slots used by this driver."""
        ...

    def verify(self) -> None:
        """Reject any changed, missing, or unauthorized runtime input."""
        ...
