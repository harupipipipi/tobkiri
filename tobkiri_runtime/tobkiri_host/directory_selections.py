"""Private, one-use directory selections; selection never grants filesystem rights."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import secrets
import math
import os
import stat
from threading import RLock
import time
from typing import Callable


@dataclass(frozen=True)
class DirectorySelectionScope:
    """Host-captured identity; values must never come from renderer payloads."""

    profile_id: str
    activation_id: str
    plan_digest: str
    security_epoch: int
    presentation_owner_principal_id: str
    presentation_owner_session_id: str

    def __post_init__(self) -> None:
        """Reject incomplete Host captures."""
        if (
            any(
                not isinstance(value, str) or not value
                for value in (
                    self.profile_id,
                    self.activation_id,
                    self.plan_digest,
                    self.presentation_owner_principal_id,
                    self.presentation_owner_session_id,
                )
            )
            or type(self.security_epoch) is not int
            or self.security_epoch <= 0
        ):
            raise ValueError("directory selection capture is incomplete")


@dataclass(frozen=True)
class _Selection:
    scope: DirectorySelectionScope
    root: Path
    deadline: float
    identity: tuple[int, int]


class DirectorySelections:
    """Bounded activation-owned ticket store for trusted OS-picker results."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        capacity: int = 128,
        ttl_seconds: float = 60,
    ) -> None:
        """Create a private store with finite lifetime and capacity."""
        if type(capacity) is not int or not 1 <= capacity <= 4096:
            raise ValueError("invalid directory selection capacity")
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(ttl_seconds)
            or not 0 < ttl_seconds <= 300
        ):
            raise ValueError("invalid directory selection lifetime")
        self._clock = clock
        self._capacity = capacity
        self._ttl = ttl_seconds
        self._lock = RLock()
        self._closed = False
        self._entries: dict[str, _Selection] = {}

    def capture(self, root: Path, scope: DirectorySelectionScope) -> dict[str, object]:
        """Capture a trusted picker result without exposing its filesystem path."""
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("picker directory is invalid")
        try:
            initial = os.stat(root, follow_symlinks=False)
            if not stat.S_ISDIR(initial.st_mode):
                raise ValueError("picker directory is invalid")
            canonical = root.resolve(strict=True)
            captured = os.stat(canonical, follow_symlinks=False)
            if not stat.S_ISDIR(captured.st_mode) or (
                initial.st_dev,
                initial.st_ino,
            ) != (captured.st_dev, captured.st_ino):
                raise ValueError("picker directory changed")
        except PermissionError:
            raise PermissionError("picker directory access denied") from None
        except OSError:
            raise ValueError("picker directory is unavailable") from None
        with self._lock:
            self._require_open()
            self._purge()
            if len(self._entries) >= self._capacity:
                raise ValueError("directory selection quota exceeded")
            token = secrets.token_urlsafe(32)
            self._entries[token] = _Selection(
                scope,
                canonical,
                self._now() + self._ttl,
                (captured.st_dev, captured.st_ino),
            )
        return {
            "selection_id": token,
            "display_name": canonical.name or "Project folder",
            "expires_in_ms": int(self._ttl * 1000),
            "cancelled": False,
        }

    def consume(self, token: str, scope: DirectorySelectionScope) -> Path:
        """Return a private root once; callers must still obtain mount approval."""
        return self.consume_identity(token, scope)[0]

    def consume_identity(
        self, token: str, scope: DirectorySelectionScope
    ) -> tuple[Path, tuple[int, int]]:
        """Return the immutable picker identity so consumers can retain its fence."""
        with self._lock:
            self._require_open()
            self._purge()
            if not isinstance(token, str) or not token:
                raise PermissionError("directory selection is unavailable")
            selected = self._entries.get(token)
            if selected is None or selected.scope != scope:
                raise PermissionError("directory selection is unavailable")
            del self._entries[token]
            # The mount consumer must repeat this check or use a retained handle:
            # returning a Path cannot eliminate the subsequent filesystem race.
            try:
                current = os.stat(selected.root, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(current.st_mode)
                    or (current.st_dev, current.st_ino) != selected.identity
                ):
                    raise PermissionError("directory selection changed")
            except OSError:
                raise PermissionError("directory selection is unavailable") from None
            return selected.root, selected.identity

    def close(self) -> None:
        """Revoke all tickets when their captured activation retires."""
        with self._lock:
            self._closed = True
            self._entries.clear()

    def _require_open(self) -> None:
        if self._closed:
            raise PermissionError("directory selection is unavailable")

    def _purge(self) -> None:
        now = self._now()
        self._entries = {
            key: value for key, value in self._entries.items() if value.deadline > now
        }

    def _now(self) -> float:
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("invalid directory selection clock")
        return value
