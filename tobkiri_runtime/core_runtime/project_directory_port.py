"""Host-private activation-owned directory ticket operations."""

from pathlib import Path
from typing import Callable, Mapping
from tobkiri_host.directory_picker import CapturedDirectoryPicker
from tobkiri_host.directory_selections import (
    DirectorySelections,
    DirectorySelectionScope,
)


class ProjectDirectoryPort:
    """Keep roots private while picker and prepare share exact captured scope."""

    def __init__(
        self, picker: CapturedDirectoryPicker, selections: DirectorySelections
    ) -> None:
        self._picker = picker
        self._selections = selections

    def acquire(
        self,
        payload: Mapping[str, object],
        *,
        scope: DirectorySelectionScope,
        assert_current: Callable[[], None],
    ) -> dict[str, object]:
        """Issue an opaque ticket after a current trusted native selection."""
        return self._picker.acquire(payload, scope=scope, assert_current=assert_current)

    def consume(
        self, token: str, scope: DirectorySelectionScope
    ) -> tuple[Path, tuple[int, int]]:
        """Redeem one selection at most once for its captured owner."""
        return self._selections.consume_identity(token, scope)

    def close(self) -> None:
        """Revoke all selections when the captured activation retires."""
        self._picker.close()
