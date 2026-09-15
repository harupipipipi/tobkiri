"""Offline-only import of historical setup-pack selection state.

This module is not imported by a runtime package or entrypoint.  It creates a
reviewable legacy Profile projection with an exact backup; it never activates
the result.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class LegacyMigrationPlan:
    """Dry-run description for one-way legacy selection migration."""

    profile_id: str
    before_pack_ids: tuple[str, ...]
    imported_pack_ids: tuple[str, ...]
    after_pack_ids: tuple[str, ...]
    changed: bool
    backup_path: str | None = None


def plan_legacy_selection_migration(
    profile: Mapping[str, Any], selection: Mapping[str, Any]
) -> LegacyMigrationPlan:
    """Return an offline dry-run diff without modifying either source."""

    profile_id = str(profile.get("profile_id") or profile.get("id") or "").strip()
    before = _unique(_string_items(profile.get("packs")))
    imported = _legacy_selected_pack_ids(selection)
    after = _unique((*before, *imported))
    return LegacyMigrationPlan(
        profile_id=profile_id,
        before_pack_ids=before,
        imported_pack_ids=imported,
        after_pack_ids=after,
        changed=before != after,
    )


def apply_legacy_selection_migration(
    profile_path: Path,
    selection_path: Path,
    *,
    backup_dir: Path,
) -> LegacyMigrationPlan:
    """Write an offline projection with an exact pre-migration backup."""

    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    if not isinstance(profile, dict) or not isinstance(selection, dict):
        raise ValueError("legacy migration inputs must be JSON objects")
    plan = plan_legacy_selection_migration(profile, selection)
    if not plan.changed:
        return plan
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{profile_path.name}.{_sha256(profile_path)}.bak"
    shutil.copy2(profile_path, backup_path)
    migrated = dict(profile)
    migrated["packs"] = list(plan.after_pack_ids)
    migrated["legacy_setup_pack_selection_imported"] = True
    temporary = profile_path.with_suffix(profile_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(migrated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(profile_path)
    return LegacyMigrationPlan(
        profile_id=plan.profile_id,
        before_pack_ids=plan.before_pack_ids,
        imported_pack_ids=plan.imported_pack_ids,
        after_pack_ids=plan.after_pack_ids,
        changed=True,
        backup_path=str(backup_path),
    )


def rollback_legacy_selection_migration(
    profile_path: Path, backup_path: Path
) -> None:
    """Restore the exact pre-migration Profile in the offline workspace."""

    if not backup_path.is_file():
        raise FileNotFoundError(f"migration backup is missing: {backup_path}")
    temporary = profile_path.with_suffix(profile_path.suffix + ".rollback.tmp")
    shutil.copy2(backup_path, temporary)
    temporary.replace(profile_path)


def _legacy_selected_pack_ids(selection: Mapping[str, Any]) -> tuple[str, ...]:
    values = _string_items(selection.get("setup_pack_ids"))
    singular = str(selection.get("setup_pack_id") or "").strip()
    active = str(selection.get("active_setup_pack_id") or "").strip()
    return _unique((*values, singular, active))


def _string_items(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


__all__ = [
    "LegacyMigrationPlan",
    "apply_legacy_selection_migration",
    "plan_legacy_selection_migration",
    "rollback_legacy_selection_migration",
]
