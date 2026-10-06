"""Narrow captured Host dialog boundary; an OS adapter must be explicitly bound."""

from pathlib import Path
from threading import Lock
from typing import Callable, Mapping, Protocol

from .directory_selections import DirectorySelections, DirectorySelectionScope


class CapturedDirectoryPickerPort(Protocol):
    """Host-private adapter; never accepts paths, approval flags, or caller scope."""

    def pick_directory(self) -> Path | None:
        """Obtain a directory from a trusted OS dialog, or return cancellation."""


class CapturedDirectoryPicker:
    """Activation-owned picker with exact post-dialog freshness verification."""

    def __init__(
        self, port: CapturedDirectoryPickerPort | None, selections: DirectorySelections
    ) -> None:
        """Bind only an explicitly supplied trusted dialog adapter."""
        self._port = port
        self._selections = selections
        self._dialog_lock = Lock()
        self._closed = False

    def acquire(
        self,
        payload: Mapping[str, object],
        *,
        scope: DirectorySelectionScope,
        assert_current: Callable[[], None],
    ) -> dict[str, object]:
        """Issue a private ticket after selected V4 admission and any approval."""
        if not isinstance(payload, Mapping) or payload:
            raise ValueError("directory picker accepts no client arguments")
        assert_current()
        if self._closed or self._port is None:
            raise NotImplementedError("directory picker is unavailable")
        if not self._dialog_lock.acquire(blocking=False):
            raise RuntimeError("directory picker is busy")
        try:
            batch_picker = getattr(self._port, "pick_directories", None)
            roots = (
                batch_picker()
                if callable(batch_picker)
                else self._port.pick_directory()
            )
            if isinstance(roots, Path):
                roots = [roots]
        except PermissionError:
            raise PermissionError("directory picker access denied") from None
        except TimeoutError:
            raise TimeoutError("directory picker timed out") from None
        except NotImplementedError:
            raise NotImplementedError("directory picker is unavailable") from None
        except Exception:
            raise RuntimeError("directory picker failed") from None
        finally:
            self._dialog_lock.release()
        assert_current()
        if roots is None:
            return {"cancelled": True, "selection_id": None}
        selections = self._selections.capture_many(roots, scope)
        return {**selections[0], "selections": selections}

    def close(self) -> None:
        """Retire all selections together with their captured Provider."""
        self._closed = True
        self._selections.close()
