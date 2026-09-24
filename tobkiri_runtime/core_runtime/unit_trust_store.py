"""
unit_trust_store.py - 実行系ユニットの sha256 trust allowlist

実行系ユニット（kind=python/binary）は Trust（sha256 allowlist）必須。
unit_id + version + sha256 を記録。

保存先: user_data/units/trust/trusted_units.json

F-2 追加:
  - TrustedUnit.kind フィールド ("python" | "binary"、デフォルト "python")
  - add_trust() / is_trusted() / list_trusted() に kind フィルタ

Wave 6-D additions:
  - A-20: add_trust() input validation (ValueError on bad input)
  - A-15: hot-reload via mtime check + auto_reload flag
  - Bugfix: load() failure no longer increments _cache_version
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional


DEFAULT_TRUST_DIR = "user_data/units/trust"
TRUST_FILE_NAME = "trusted_units.json"

_SHA256_RE = re.compile(r"[0-9a-f]{64}")

VALID_TRUST_KINDS = frozenset({"python", "binary"})


@dataclass
class TrustedUnit:
    unit_id: str
    version: str
    sha256: str
    note: str = ""
    kind: str = "python"


@dataclass
class UnitTrustCheckResult:
    trusted: bool
    reason: str
    unit_id: str
    version: str
    expected_sha256: Optional[str] = None
    actual_sha256: Optional[str] = None


class UnitTrustStore:
    """Trust allowlist for executable units.

    Parameters:
        trust_dir: Override directory for trusted_units.json.
        auto_reload: When True, ``is_trusted()`` automatically calls
            ``reload_if_modified()`` before each check.  Can also be
            activated via the ``RUMI_TRUST_AUTO_RELOAD=1`` env-var.
    """

    def __init__(
        self,
        trust_dir: Optional[str] = None,
        auto_reload: bool = False,
    ):
        self._trust_dir = Path(trust_dir or DEFAULT_TRUST_DIR)
        self._trust_file = self._trust_dir / TRUST_FILE_NAME
        self._lock = threading.RLock()
        self._trusted: Dict[tuple, TrustedUnit] = {}
        self._loaded = False
        self._load_error: Optional[str] = None
        self._load_warnings: List[str] = []
        self._cache_version: int = 0
        self._last_mtime: float = 0.0
        self._auto_reload: bool = (
            auto_reload
            or os.environ.get("RUMI_TRUST_AUTO_RELOAD") == "1"
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _now_ts() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _validate_trust_input(
        unit_id: Any,
        version: Any,
        sha256: Any,
    ) -> None:
        """Validate ``add_trust`` arguments; raise ``ValueError`` on failure."""
        if not isinstance(unit_id, str) or not unit_id:
            raise ValueError(
                f"unit_id must be a non-empty string, got {unit_id!r}"
            )
        if not isinstance(version, str) or not version:
            raise ValueError(
                f"version must be a non-empty string, got {version!r}"
            )
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256.lower()):
            raise ValueError(
                f"sha256 must be a 64-character hex string, got {sha256!r}"
            )

    def _log_audit(
        self,
        event_type: str,
        severity: Literal["info", "warning", "error", "critical"],
        description: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Best-effort audit logging — never raises."""
        try:
            from core_runtime.audit_logger import get_audit_logger
            get_audit_logger().log_security_event(
                event_type=event_type,
                severity=severity,
                description=description,
                details=details or {},
            )
        except Exception:
            pass

    def _get_file_mtime(self) -> float:
        """Return mtime of the trust file, or ``0.0`` if it does not exist."""
        try:
            return self._trust_file.stat().st_mtime
        except OSError:
            return 0.0

    # ------------------------------------------------------------------
    # Load / reload
    # ------------------------------------------------------------------

    def load(self) -> bool:
        with self._lock:
            self._trusted.clear()
            self._load_warnings.clear()
            self._loaded = False
            self._load_error = None

            if not self._trust_file.exists():
                self._loaded = True
                self._cache_version += 1
                self._last_mtime = 0.0
                return True

            try:
                with open(self._trust_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                self._load_error = f"Failed to parse trust file: {e}"
                return False

            if not isinstance(data, dict):
                self._load_error = "Trust file must be a JSON object"
                return False

            for entry in data.get("trusted", []):
                if not isinstance(entry, dict):
                    self._load_warnings.append(
                        "Skipped non-dict entry in trusted list"
                    )
                    continue

                uid = entry.get("unit_id", "")
                ver = entry.get("version", "")
                sha = entry.get("sha256", "")

                if not isinstance(uid, str) or not uid:
                    self._load_warnings.append(
                        f"Skipped entry: invalid unit_id={uid!r}"
                    )
                    continue
                if not isinstance(ver, str) or not ver:
                    self._load_warnings.append(
                        f"Skipped entry: invalid version={ver!r} (unit_id={uid!r})"
                    )
                    continue
                if not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha.lower()):
                    self._load_warnings.append(
                        f"Skipped entry: invalid sha256={sha!r} (unit_id={uid!r}, version={ver!r})"
                    )
                    continue

                # F-2: kind field with validation
                kind_val = entry.get("kind", "python")
                if kind_val not in VALID_TRUST_KINDS:
                    self._load_warnings.append(
                        f"Invalid kind={kind_val!r} for unit_id={uid!r}, "
                        f"defaulting to 'python'"
                    )
                    kind_val = "python"

                self._trusted[(uid, ver)] = TrustedUnit(
                    unit_id=uid,
                    version=ver,
                    sha256=sha.lower(),
                    note=entry.get("note", ""),
                    kind=kind_val,
                )

            self._loaded = True
            self._cache_version += 1
            self._last_mtime = self._get_file_mtime()
            return True

    def _check_file_modified(self) -> bool:
        """Return ``True`` if trust file mtime differs from last recorded."""
        current = self._get_file_mtime()
        return current != self._last_mtime

    def reload_if_modified(self) -> bool:
        """Reload trust data if the backing file was modified on disk.

        Returns:
            ``True`` if a reload was performed (regardless of load success),
            ``False`` if the file was unchanged.
        """
        with self._lock:
            if not self._check_file_modified():
                return False
            success = self.load()
            if success:
                self._log_audit(
                    "trust_store_reloaded",
                    "info",
                    "Trust store reloaded due to file modification",
                    details={"cache_version": self._cache_version},
                )
            return True

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def is_trusted(
        self,
        unit_id: str,
        version: str,
        actual_sha256: str,
        kind: Optional[str] = None,
    ) -> UnitTrustCheckResult:
        """
        Check if a unit is trusted.

        Args:
            unit_id: The unit identifier.
            version: The unit version.
            actual_sha256: The actual SHA-256 hash of the unit entrypoint.
            kind: Optional kind filter. If specified, the trusted entry must
                  match this kind. If None (default), any kind matches.
                  This maintains backward compatibility.

        Returns:
            UnitTrustCheckResult with trust status and details.
        """
        with self._lock:
            if self._auto_reload:
                self.reload_if_modified()

            if not self._loaded:
                return UnitTrustCheckResult(
                    trusted=False,
                    reason="Unit trust store not loaded",
                    unit_id=unit_id,
                    version=version,
                    actual_sha256=actual_sha256,
                )
            entry = self._trusted.get((unit_id, version))
            if entry is None:
                return UnitTrustCheckResult(
                    trusted=False,
                    reason=f"Unit '{unit_id}' version '{version}' not in trust list",
                    unit_id=unit_id,
                    version=version,
                    actual_sha256=actual_sha256,
                )
            # F-2: kind filter
            if kind is not None and entry.kind != kind:
                return UnitTrustCheckResult(
                    trusted=False,
                    reason=(
                        f"Unit '{unit_id}' version '{version}' is "
                        f"kind='{entry.kind}', expected kind='{kind}'"
                    ),
                    unit_id=unit_id,
                    version=version,
                    actual_sha256=actual_sha256,
                )
            actual_lower = actual_sha256.lower()
            if entry.sha256 != actual_lower:
                return UnitTrustCheckResult(
                    trusted=False,
                    reason=f"SHA-256 mismatch for unit '{unit_id}' version '{version}'",
                    unit_id=unit_id,
                    version=version,
                    expected_sha256=entry.sha256,
                    actual_sha256=actual_lower,
                )
            return UnitTrustCheckResult(
                trusted=True,
                reason="Trusted",
                unit_id=unit_id,
                version=version,
                expected_sha256=entry.sha256,
                actual_sha256=actual_lower,
            )

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add_trust(
        self,
        unit_id: str,
        version: str,
        sha256: str,
        note: str = "",
        kind: str = "python",
    ) -> bool:
        """
        Add a trusted unit entry.

        Args:
            unit_id: The unit identifier.
            version: The unit version.
            sha256: The SHA-256 hash of the unit entrypoint.
            note: Optional human-readable note.
            kind: The unit kind, either "python" or "binary".
                  Defaults to "python" for backward compatibility.

        Returns:
            True if the entry was saved successfully.

        Raises:
            ValueError: If any input is invalid.
        """
        # --- input validation (before lock – no state mutation) ---
        try:
            self._validate_trust_input(unit_id, version, sha256)
        except ValueError as exc:
            self._log_audit(
                "trust_add_rejected",
                "warning",
                str(exc),
                details={
                    "unit_id": repr(unit_id),
                    "version": repr(version),
                    "sha256": repr(sha256),
                },
            )
            raise

        # F-2: kind validation
        if kind not in VALID_TRUST_KINDS:
            invalid_kind_error = ValueError(
                f"kind must be one of {sorted(VALID_TRUST_KINDS)}, got {kind!r}"
            )
            self._log_audit(
                "trust_add_rejected",
                "warning",
                str(invalid_kind_error),
                details={
                    "unit_id": unit_id,
                    "version": version,
                    "kind": kind,
                },
            )
            raise invalid_kind_error

        with self._lock:
            self._trusted[(unit_id, version)] = TrustedUnit(
                unit_id=unit_id,
                version=version,
                sha256=sha256.lower(),
                note=note or "",
                kind=kind,
            )
            self._cache_version += 1
            return self._save()

    def remove_trust(self, unit_id: str, version: str) -> bool:
        with self._lock:
            key = (unit_id, version)
            if key not in self._trusted:
                return False
            del self._trusted[key]
            self._cache_version += 1
            return self._save()

    def list_trusted(self, kind: Optional[str] = None) -> List[TrustedUnit]:
        """
        List all trusted units, optionally filtered by kind.

        Args:
            kind: If specified, only return entries matching this kind.
                  If None (default), return all entries.
                  Backward compatible: existing callers with no arguments
                  get all entries.

        Returns:
            List of TrustedUnit entries.
        """
        with self._lock:
            if kind is None:
                return list(self._trusted.values())
            return [t for t in self._trusted.values() if t.kind == kind]

    def is_loaded(self) -> bool:
        with self._lock:
            return self._loaded

    @property
    def load_warnings(self) -> List[str]:
        with self._lock:
            return list(self._load_warnings)

    def invalidate_cache(self) -> None:
        with self._lock:
            self._loaded = False
            self._cache_version += 1

    @property
    def cache_version(self) -> int:
        with self._lock:
            return self._cache_version

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> bool:
        try:
            self._trust_dir.mkdir(parents=True, exist_ok=True)
            data = {
                "version": "1.0",
                "updated_at": self._now_ts(),
                "trusted": [
                    {
                        "unit_id": t.unit_id,
                        "version": t.version,
                        "sha256": t.sha256,
                        "note": t.note,
                        "kind": t.kind,
                    }
                    for t in self._trusted.values()
                ],
            }
            with open(self._trust_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False


# ------------------------------------------------------------------
# Global singleton helpers
# ------------------------------------------------------------------

_global_unit_trust: Optional[UnitTrustStore] = None
_unit_trust_lock = threading.Lock()


def get_unit_trust_store() -> UnitTrustStore:
    global _global_unit_trust
    if _global_unit_trust is None:
        with _unit_trust_lock:
            if _global_unit_trust is None:
                _global_unit_trust = UnitTrustStore()
    return _global_unit_trust


def reset_unit_trust_store(trust_dir: str | None = None) -> UnitTrustStore:
    global _global_unit_trust
    with _unit_trust_lock:
        _global_unit_trust = UnitTrustStore(trust_dir)
    return _global_unit_trust
