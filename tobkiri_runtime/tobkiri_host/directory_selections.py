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
        """Capture one trusted result, preserving the original public projection."""
        return self.capture_many([root], scope)[0]

    def capture_many(
        self, roots: list[Path], scope: DirectorySelectionScope
    ) -> list[dict[str, object]]:
        """Validate every root before publishing any bounded private tickets."""
        if not isinstance(roots, list) or not 1 <= len(roots) <= 32:
            raise ValueError("picker directory count is invalid")
        captured_roots = []
        identities = set()
        for root in roots:
            if not isinstance(root, Path) or not root.is_absolute():
                raise ValueError("picker directory is invalid")
            try:
                initial = os.stat(root, follow_symlinks=False)
                canonical = root.resolve(strict=True)
                current = os.stat(canonical, follow_symlinks=False)
                identity = (current.st_dev, current.st_ino)
                if (
                    not stat.S_ISDIR(initial.st_mode)
                    or not stat.S_ISDIR(current.st_mode)
                    or (initial.st_dev, initial.st_ino) != identity
                ):
                    raise ValueError("picker directory changed")
                if identity in identities:
                    raise ValueError("duplicate picker directory")
                identities.add(identity)
                captured_roots.append((canonical, identity))
            except PermissionError:
                raise PermissionError("picker directory access denied") from None
            except OSError:
                raise ValueError("picker directory is unavailable") from None
        with self._lock:
            self._require_open()
            self._purge()
            if len(self._entries) + len(roots) > self._capacity:
                raise ValueError("directory selection quota exceeded")
            deadline = self._now() + self._ttl
            result = []
            for canonical, identity in captured_roots:
                token = secrets.token_urlsafe(32)
                self._entries[token] = _Selection(scope, canonical, deadline, identity)
                result.append(
                    {
                        "selection_id": token,
                        "display_name": canonical.name or "Project folder",
                        "expires_in_ms": int(self._ttl * 1000),
                        "cancelled": False,
                    }
                )
            return result

    def consume(self, token: str, scope: DirectorySelectionScope) -> Path:
        """Return one private root without granting filesystem authority."""
        return self.consume_identity(token, scope)[0]

    def consume_identity(
        self, token: str, scope: DirectorySelectionScope
    ) -> tuple[Path, tuple[int, int]]:
        """Return one immutable identity through atomic batch validation."""
        return self.consume_many([token], scope)[0]

    def consume_many(
        self, tokens: list[str], scope: DirectorySelectionScope
    ) -> list[tuple[Path, tuple[int, int]]]:
        """Redeem all tickets once only after every owner, expiry and root check."""
        if (
            not isinstance(tokens, list)
            or not 1 <= len(tokens) <= 32
            or any(not isinstance(t, str) or not t for t in tokens)
            or len(set(tokens)) != len(tokens)
        ):
            raise PermissionError("directory selection is unavailable")
        with self._lock:
            self._require_open()
            self._purge()
            selections = []
            for token in tokens:
                selected = self._entries.get(token)
                if selected is None or selected.scope != scope:
                    raise PermissionError("directory selection is unavailable")
                try:
                    current = os.stat(selected.root, follow_symlinks=False)
                    if (
                        not stat.S_ISDIR(current.st_mode)
                        or (current.st_dev, current.st_ino) != selected.identity
                    ):
                        raise PermissionError("directory selection changed")
                except OSError:
                    raise PermissionError(
                        "directory selection is unavailable"
                    ) from None
                selections.append(selected)
            if len({s.identity for s in selections}) != len(selections):
                raise PermissionError("duplicate directory selection")
            now = self._now()
            if any(selected.deadline <= now for selected in selections):
                raise PermissionError("directory selection expired during validation")
            for token in tokens:
                del self._entries[token]
            return [(s.root, s.identity) for s in selections]

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
