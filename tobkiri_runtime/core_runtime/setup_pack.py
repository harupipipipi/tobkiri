"""
setup_pack.py - setup-pack discovery / install / grants
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .dependency_resolver import validate_dependencies, version_satisfies
from .pack_boundary import finite_files
from .paths import BASE_DIR, USER_DATA_DIR, discover_pack_locations
from .setup_pack_metadata import (
    as_dict as _as_dict,
    normalize_dependency_specs as _normalize_dependency_specs,
    normalize_pack_ref_specs as _normalize_pack_ref_specs,
    validate_setup_pack_metadata,
    validate_setup_pack_schema,
)

logger = logging.getLogger(__name__)

SETUP_PACK_ROOT = BASE_DIR / "ecosystem" / "setup_pack"
SETUP_PACK_SELECTION_FILE = (
    USER_DATA_DIR / "settings" / "setup_pack_selection.json"
)
SETUP_PACK_ALL_OK_PERMISSIONS = [
    "function.call",
    "pack.update",
    "pack.install",
    "pack.uninstall",
    "pack.migrate",
    "setup_pack.module.manage",
]

def _current_python_version() -> str:
    return "{}.{}.{}".format(sys.version_info.major, sys.version_info.minor, sys.version_info.micro)


def _platform_aliases() -> set[str]:
    aliases = {sys.platform}
    if sys.platform.startswith("win"):
        aliases.update({"win", "windows", "win32"})
    elif sys.platform == "darwin":
        aliases.update({"mac", "macos", "darwin"})
    elif sys.platform.startswith("linux"):
        aliases.update({"linux", "linux2"})
    return aliases


@dataclass(frozen=True)
class SetupPackDefinition:
    pack_id: str
    display_name: str
    description: str
    target_pack_id: str
    version: str = ""
    recommended: bool = False
    risk_level: str = "medium"
    # supports_all_ok is trusted repository metadata. Upstream only treats
    # maintainer-reviewed ecosystem/setup_pack/* definitions as trusted; forks
    # may add their own definitions, which is equivalent to changing trusted
    # source in that fork rather than crossing a runtime trust boundary.
    supports_all_ok: bool = False
    depends_on: List[Dict[str, str]] = field(default_factory=list)
    conflicts_with: List[Dict[str, str]] = field(default_factory=list)
    overlap_policy: Dict[str, Any] = field(default_factory=dict)
    base_pack_promotion: Dict[str, Any] = field(default_factory=dict)
    compatibility: Dict[str, Any] = field(default_factory=dict)
    marketplace: Dict[str, Any] = field(default_factory=dict)
    signing: Dict[str, Any] = field(default_factory=dict)
    schema_issues: List[Dict[str, Any]] = field(default_factory=list)
    required_permissions: List[str] = field(default_factory=list)
    install_surfaces: List[str] = field(default_factory=list)
    install_prompt: Dict[str, Any] = field(default_factory=dict)
    source_path: str = ""


class SetupPackManager:
    def __init__(
        self,
        root: Path | None = None,
        selection_file: Path | None = None,
        ecosystem_dir: Path | None = None,
    ) -> None:
        self.root = Path(root or SETUP_PACK_ROOT)
        self.selection_file = Path(selection_file or SETUP_PACK_SELECTION_FILE)
        self.ecosystem_dir = Path(ecosystem_dir) if ecosystem_dir is not None else self.root.parent

    def _definition_files(self) -> List[Path]:
        if not self.root.is_dir():
            return []
        return [
            path
            for path in finite_files(self.root, (".json",), recursive=True)
            if path.name == "pack.json" and path.parent.parent == self.root
        ]

    def _load_definitions(self) -> Dict[str, SetupPackDefinition]:
        result: Dict[str, SetupPackDefinition] = {}
        for path in self._definition_files():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Failed to parse setup_pack file %s: %s", path, exc)
                continue
            if isinstance(loaded, dict):
                raw = loaded
                fallback_pack_id = path.parent.name
                pack_id = str(raw.get("pack_id") or fallback_pack_id)
                target_pack_id = str(raw.get("target_pack_id") or pack_id)
            else:
                raw = {}
                pack_id = path.parent.name
                target_pack_id = pack_id
            schema_issues = validate_setup_pack_schema(
                loaded,
                fallback_pack_id=pack_id,
                fallback_target_pack_id=target_pack_id,
            )
            compatibility = _as_dict(raw.get("compatibility"))
            for source_key, compatibility_key in (
                ("target_pack_version", "target_pack_version"),
                ("target_version", "target_pack_version"),
                ("python_requires", "python"),
                ("platforms", "platforms"),
            ):
                if source_key in raw and compatibility_key not in compatibility:
                    compatibility[compatibility_key] = raw[source_key]
            result[pack_id] = SetupPackDefinition(
                pack_id=pack_id,
                display_name=str(raw.get("display_name", pack_id)),
                description=str(raw.get("description", "")),
                target_pack_id=target_pack_id,
                version=str(raw.get("version", "")),
                recommended=bool(raw.get("recommended", False)),
                risk_level=str(raw.get("risk_level", "medium")),
                supports_all_ok=bool(raw.get("supports_all_ok", False)),
                depends_on=_normalize_dependency_specs(
                    raw.get("depends_on", raw.get("dependencies", []))
                ),
                conflicts_with=_normalize_pack_ref_specs(raw.get("conflicts_with", [])),
                overlap_policy=_as_dict(raw.get("overlap_policy")),
                base_pack_promotion=_as_dict(raw.get("base_pack_promotion")),
                compatibility=compatibility,
                marketplace=_as_dict(raw.get("marketplace")),
                signing=_as_dict(raw.get("signing")),
                schema_issues=schema_issues,
                required_permissions=[
                    str(item)
                    for item in raw.get("required_permissions", [])
                    if str(item).strip()
                ] if isinstance(raw.get("required_permissions"), list) else [],
                install_surfaces=[
                    str(item)
                    for item in raw.get("install_surfaces", [])
                    if str(item).strip()
                ] if isinstance(raw.get("install_surfaces"), list) else [],
                install_prompt=_as_dict(raw.get("install_prompt")),
                source_path=str(path.parent.resolve()),
            )
        return result

    def list_packs(self) -> Dict[str, Any]:
        definitions = self._load_definitions()
        selection = self.get_selection()
        selected_setup_pack_ids: List[str] = []
        unknown_selected_setup_pack_ids: List[str] = []
        selected_ids = set()
        unknown_selected_ids = set()

        def add_selected(value: Any) -> None:
            raw_items = [value] if isinstance(value, str) else value
            if not isinstance(raw_items, list):
                return
            for item in raw_items:
                setup_pack_id = str(item).strip()
                if not setup_pack_id:
                    continue
                if setup_pack_id not in definitions:
                    if setup_pack_id not in unknown_selected_ids:
                        unknown_selected_setup_pack_ids.append(setup_pack_id)
                        unknown_selected_ids.add(setup_pack_id)
                    continue
                if setup_pack_id not in selected_ids:
                    selected_setup_pack_ids.append(setup_pack_id)
                    selected_ids.add(setup_pack_id)

        add_selected(selection.get("setup_pack_ids"))
        add_selected(selection.get("setup_pack_id"))

        active_setup_pack_id = str(selection.get("active_setup_pack_id") or "").strip()
        active_target_pack_id = str(selection.get("active_target_pack_id") or "").strip()
        if active_setup_pack_id not in definitions:
            active_setup_pack_id = ""
            active_target_pack_id = ""
        packs = []
        for pack_id in sorted(definitions):
            item = definitions[pack_id]
            packs.append({
                "pack_id": item.pack_id,
                "display_name": item.display_name,
                "description": item.description,
                "target_pack_id": item.target_pack_id,
                "version": item.version,
                "recommended": item.recommended,
                "risk_level": item.risk_level,
                "supports_all_ok": item.supports_all_ok,
                "depends_on": list(item.depends_on),
                "conflicts_with": list(item.conflicts_with),
                "overlap_policy": dict(item.overlap_policy),
                "base_pack_promotion": dict(item.base_pack_promotion),
                "compatibility": dict(item.compatibility),
                "marketplace": dict(item.marketplace),
                "signing": dict(item.signing),
                "schema_issues": list(item.schema_issues),
                "required_permissions": list(item.required_permissions),
                "install_surfaces": list(item.install_surfaces),
                "install_prompt": dict(item.install_prompt),
                "source_path": item.source_path,
                "selected": item.pack_id in selected_ids,
            })
        return {
            "packs": packs,
            "count": len(packs),
            "selected_setup_pack_id": selection.get("setup_pack_id"),
            "selected_setup_pack_ids": selected_setup_pack_ids,
            "unknown_selected_setup_pack_ids": unknown_selected_setup_pack_ids,
            "active_setup_pack_id": active_setup_pack_id or None,
            "active_target_pack_id": active_target_pack_id or None,
        }

    def get_selection(self) -> Dict[str, Any]:
        if not self.selection_file.is_file():
            return {}
        try:
            data = json.loads(self.selection_file.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse setup_pack selection file %s: %s", self.selection_file, exc)
            return {}
        return data if isinstance(data, dict) else {}

    def _selected_setup_pack_ids(self) -> set[str]:
        selection = self.get_selection()
        selected: set[str] = set()
        for setup_pack_id in selection.get("setup_pack_ids") or []:
            normalized = str(setup_pack_id or "").strip()
            if normalized:
                selected.add(normalized)
        for key in ("setup_pack_id", "active_setup_pack_id"):
            normalized = str(selection.get(key) or "").strip()
            if normalized:
                selected.add(normalized)
        return selected

    def get_completed_selection_status(self) -> Dict[str, Any]:
        selection = self.get_selection()
        if not selection:
            return {
                "completed": False,
                "reason": "setup_pack_selection_not_found",
            }

        definitions = self._load_definitions()

        selected_ids: List[str] = []
        seen: set[str] = set()
        raw_selected = selection.get("setup_pack_ids")
        if isinstance(raw_selected, list):
            raw_items = raw_selected
        else:
            raw_items = []
        for item in raw_items:
            setup_pack_id = str(item or "").strip()
            if setup_pack_id and setup_pack_id not in seen:
                selected_ids.append(setup_pack_id)
                seen.add(setup_pack_id)

        legacy_setup_pack_id = str(selection.get("setup_pack_id") or "").strip()
        if legacy_setup_pack_id and legacy_setup_pack_id not in seen:
            selected_ids.append(legacy_setup_pack_id)
            seen.add(legacy_setup_pack_id)

        active_setup_pack_id = str(selection.get("active_setup_pack_id") or "").strip()
        if not active_setup_pack_id:
            active_setup_pack_id = legacy_setup_pack_id
        active_target_pack_id = str(selection.get("active_target_pack_id") or "").strip()
        if not active_target_pack_id:
            active_target_pack_id = str(selection.get("target_pack_id") or "").strip()

        if not active_setup_pack_id:
            return {
                "completed": False,
                "reason": "missing_active_setup_pack_id",
            }
        if active_setup_pack_id not in selected_ids:
            return {
                "completed": False,
                "reason": "active_setup_pack_not_selected",
                "active_setup_pack_id": active_setup_pack_id,
            }

        unknown_selected = [
            setup_pack_id
            for setup_pack_id in selected_ids
            if setup_pack_id not in definitions
        ]
        if unknown_selected:
            return {
                "completed": False,
                "reason": "unknown_selected_setup_pack",
                "unknown_selected_setup_pack_ids": unknown_selected,
            }

        active_definition = definitions.get(active_setup_pack_id)
        if active_definition is None:
            return {
                "completed": False,
                "reason": "unknown_active_setup_pack",
                "active_setup_pack_id": active_setup_pack_id,
            }

        expected_target_pack_id = active_definition.target_pack_id
        if active_target_pack_id != expected_target_pack_id:
            return {
                "completed": False,
                "reason": "active_target_pack_mismatch",
                "active_setup_pack_id": active_setup_pack_id,
                "active_target_pack_id": active_target_pack_id,
                "expected_target_pack_id": expected_target_pack_id,
            }

        locations = {
            loc.pack_id: loc for loc in discover_pack_locations(str(self.ecosystem_dir))
        }
        if active_target_pack_id not in locations:
            return {
                "completed": False,
                "reason": "active_target_pack_not_found",
                "active_setup_pack_id": active_setup_pack_id,
                "active_target_pack_id": active_target_pack_id,
            }

        return {
            "completed": True,
            "reason": "setup_pack_selection_valid",
            "active_setup_pack_id": active_setup_pack_id,
            "active_target_pack_id": active_target_pack_id,
            "selected_setup_pack_ids": selected_ids,
        }

    def _log_system_event(
        self,
        action: str,
        success: bool,
        details: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        try:
            from .audit_logger import get_audit_logger

            audit = get_audit_logger()
            audit.log_system_event(
                event_type=action,
                success=success,
                details=details or {},
                error=error or "",
            )
        except Exception:
            logger.debug("Failed to audit setup_pack system event", exc_info=True)

    def _log_permission_event(
        self,
        action: str,
        success: bool,
        principal_id: str,
        permission_id: str,
        details: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        try:
            from .audit_logger import get_audit_logger

            audit = get_audit_logger()
            audit.log_permission_event(
                pack_id=principal_id,
                permission_type=permission_id,
                action=action,
                success=success,
                details=details or {},
                rejection_reason=error or "",
            )
        except Exception:
            logger.debug("Failed to audit setup_pack permission event", exc_info=True)

    @staticmethod
    def _normalize_setup_pack_ids(setup_pack_ids: str | List[str]) -> List[str]:
        if isinstance(setup_pack_ids, str):
            raw_items = [setup_pack_ids]
        elif isinstance(setup_pack_ids, list):
            raw_items = setup_pack_ids
        else:
            return []
        normalized: List[str] = []
        seen = set()
        for item in raw_items:
            setup_pack_id = str(item).strip()
            if setup_pack_id and setup_pack_id not in seen:
                normalized.append(setup_pack_id)
                seen.add(setup_pack_id)
        return normalized

    @staticmethod
    def _read_target_manifest(location: Any) -> Dict[str, Any]:
        try:
            path = Path(location.ecosystem_json_path)
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _definition_version(definition: SetupPackDefinition, target_manifest: Dict[str, Any]) -> str:
        return definition.version or str(target_manifest.get("version", ""))

    def _validate_metadata_contract(self, definition: SetupPackDefinition) -> List[Dict[str, Any]]:
        return validate_setup_pack_metadata(
            pack_id=definition.pack_id,
            target_pack_id=definition.target_pack_id,
            marketplace=definition.marketplace,
            signing=definition.signing,
        )

    def _validate_compatibility_contract(
        self,
        definition: SetupPackDefinition,
        location: Any,
    ) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []
        compatibility = definition.compatibility or {}
        target_manifest = self._read_target_manifest(location)
        target_version = str(target_manifest.get("version", ""))
        target_constraint = (
            compatibility.get("target_pack_version")
            or compatibility.get("target_version")
            or compatibility.get("target_pack_versions")
        )
        if target_constraint and not version_satisfies(target_version, str(target_constraint)):
            issues.append(
                {
                    "setup_pack_id": definition.pack_id,
                    "target_pack_id": definition.target_pack_id,
                    "reason": "target_version_mismatch",
                    "required": str(target_constraint),
                    "actual": target_version,
                    "error": "Target pack version does not satisfy setup pack constraint",
                }
            )

        python_constraint = compatibility.get("python") or compatibility.get("python_version")
        if python_constraint and not version_satisfies(_current_python_version(), str(python_constraint)):
            issues.append(
                {
                    "setup_pack_id": definition.pack_id,
                    "target_pack_id": definition.target_pack_id,
                    "reason": "python_version_mismatch",
                    "required": str(python_constraint),
                    "actual": _current_python_version(),
                    "error": "Python version does not satisfy setup pack constraint",
                }
            )

        platforms = compatibility.get("platforms")
        if isinstance(platforms, str):
            platforms = [platforms]
        if isinstance(platforms, list) and platforms:
            allowed = {str(item).strip().lower() for item in platforms if str(item).strip()}
            if allowed and not (_platform_aliases() & allowed):
                issues.append(
                    {
                        "setup_pack_id": definition.pack_id,
                        "target_pack_id": definition.target_pack_id,
                        "reason": "platform_mismatch",
                        "required": sorted(allowed),
                        "actual": sys.platform,
                        "error": "Current platform does not satisfy setup pack constraint",
                    }
                )
        return issues

    def _validate_install_contracts(
        self,
        definitions: List[SetupPackDefinition],
        locations: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        if not definitions:
            return []

        manifests: Dict[str, Dict[str, Any]] = {}
        referenced_dependencies: set[str] = set()
        for definition in definitions:
            target_manifest = self._read_target_manifest(locations.get(definition.target_pack_id))
            manifests[definition.pack_id] = {
                "version": self._definition_version(definition, target_manifest),
                "depends_on": list(definition.depends_on),
            }
            if definition.target_pack_id and definition.target_pack_id != definition.pack_id:
                manifests.setdefault(
                    definition.target_pack_id,
                    {"version": str(target_manifest.get("version", ""))},
                )
            for dependency in definition.depends_on:
                dependency_pack_id = str(dependency.get("pack_id") or "").strip()
                if dependency_pack_id:
                    referenced_dependencies.add(dependency_pack_id)

        for dependency_pack_id in sorted(referenced_dependencies):
            if dependency_pack_id in manifests:
                continue
            location = locations.get(dependency_pack_id)
            if location is None:
                continue
            dependency_manifest = self._read_target_manifest(location)
            manifests[dependency_pack_id] = {
                "version": str(dependency_manifest.get("version", "")),
            }

        issues: List[Dict[str, Any]] = []
        for definition in definitions:
            issues.extend(definition.schema_issues)
        for issue in validate_dependencies(manifests):
            setup_pack_id = str(issue.get("pack_id") or "")
            issues.append(
                {
                    "setup_pack_id": setup_pack_id,
                    "target_pack_id": next(
                        (
                            definition.target_pack_id
                            for definition in definitions
                            if definition.pack_id == setup_pack_id
                        ),
                        "",
                    ),
                    "reason": "dependency_" + str(issue.get("type", "invalid")),
                    "dependency_issue": issue,
                    "error": "Setup pack dependency validation failed",
                }
            )

        for definition in definitions:
            issues.extend(self._validate_metadata_contract(definition))
            location = locations.get(definition.target_pack_id)
            if location is not None:
                issues.extend(self._validate_compatibility_contract(definition, location))
        selected_ids = {definition.pack_id for definition in definitions}
        selected_target_ids = {definition.target_pack_id for definition in definitions}
        for definition in definitions:
            for conflict in definition.conflicts_with:
                conflict_id = str(conflict.get("pack_id") or "")
                if conflict_id not in selected_ids and conflict_id not in selected_target_ids:
                    continue
                issues.append(
                    {
                        "setup_pack_id": definition.pack_id,
                        "target_pack_id": definition.target_pack_id,
                        "reason": "setup_pack_conflict",
                        "conflicts_with": conflict_id,
                        "conflict": conflict,
                        "resolution": conflict.get("resolution") or "choose_one_pack",
                        "error": "Selected setup packs declare overlapping responsibility",
                    }
                )
        return issues

    @staticmethod
    def _choose_active_definition(
        definitions: List[SetupPackDefinition],
    ) -> SetupPackDefinition:
        for definition in definitions:
            if definition.recommended:
                return definition
        return definitions[0]

    def _write_selection(
        self,
        definitions: List[SetupPackDefinition],
        active_definition: SetupPackDefinition,
    ) -> Dict[str, Any]:
        selection = {
            "setup_pack_id": active_definition.pack_id,
            "target_pack_id": active_definition.target_pack_id,
            "display_name": active_definition.display_name,
            "setup_pack_ids": [definition.pack_id for definition in definitions],
            "target_pack_ids": [definition.target_pack_id for definition in definitions],
            "active_setup_pack_id": active_definition.pack_id,
            "active_target_pack_id": active_definition.target_pack_id,
            "display_names": {
                definition.pack_id: definition.display_name
                for definition in definitions
            },
        }
        self.selection_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.selection_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(selection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.selection_file)
        return selection

    def _snapshot_selection_file(self) -> Dict[str, Any]:
        try:
            return {
                "exists": self.selection_file.is_file(),
                "content": self.selection_file.read_text(encoding="utf-8")
                if self.selection_file.is_file()
                else None,
            }
        except OSError:
            logger.debug("Failed to snapshot setup pack selection file", exc_info=True)
            return {"exists": False, "content": None}

    def _restore_selection_file(self, snapshot: Dict[str, Any]) -> None:
        try:
            if snapshot.get("exists"):
                self.selection_file.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.selection_file.with_suffix(".tmp")
                tmp.write_text(str(snapshot.get("content") or ""), encoding="utf-8")
                tmp.replace(self.selection_file)
            elif self.selection_file.exists():
                self.selection_file.unlink()
        except OSError:
            logger.debug("Failed to restore setup pack selection file", exc_info=True)

    def _snapshot_active_pack_identity(self) -> Dict[str, Any]:
        try:
            from backend_core.ecosystem.active_ecosystem import get_active_ecosystem_manager

            active = get_active_ecosystem_manager()
            return {
                "available": True,
                "active_pack_identity": getattr(active, "active_pack_identity", None),
            }
        except Exception:
            logger.debug("Failed to snapshot active_pack_identity", exc_info=True)
            return {"available": False, "active_pack_identity": None}

    def _restore_active_pack_identity(self, snapshot: Dict[str, Any]) -> None:
        if not snapshot.get("available"):
            return
        try:
            from backend_core.ecosystem.active_ecosystem import get_active_ecosystem_manager

            active = get_active_ecosystem_manager()
            active.active_pack_identity = snapshot.get("active_pack_identity")
        except Exception:
            logger.debug("Failed to restore active_pack_identity", exc_info=True)

    def _grant_all_ok_for_definition(
        self,
        definition: SetupPackDefinition,
        *,
        rollback_on_failure: bool = False,
    ) -> Dict[str, Any]:
        from .capability_grant_manager import get_capability_grant_manager

        gm = get_capability_grant_manager()
        grant_snapshot = self._snapshot_capability_grant(gm, definition.target_pack_id)
        result = gm.batch_grant([
            {
                "principal_id": definition.target_pack_id,
                "permission_id": permission_id,
                "config": {
                    "mode": "all_ok",
                    "source": "setup_pack",
                    "setup_pack_id": definition.pack_id,
                },
            }
            for permission_id in SETUP_PACK_ALL_OK_PERMISSIONS
        ])
        self._log_permission_event(
            "grant_all_ok",
            result.failed_count == 0,
            principal_id=definition.target_pack_id,
            permission_id="*",
            details={
                "setup_pack_id": definition.pack_id,
                "granted_count": result.granted_count,
                "failed_count": result.failed_count,
            },
            error=None if result.failed_count == 0 else "partial_grant_failure",
        )
        revoked_count = 0
        if rollback_on_failure and result.failed_count:
            revoked_count = self._restore_all_ok_for_definition(definition, {
                "grant_snapshot_available": grant_snapshot.get("available", False),
                "grant_snapshot": grant_snapshot.get("grant"),
                "rollback_reason": "setup_pack_grant_all_ok_rollback",
            })
        return {
            "granted": result.failed_count == 0,
            "principal_id": definition.target_pack_id,
            "permission_ids": list(SETUP_PACK_ALL_OK_PERMISSIONS),
            "granted_count": result.granted_count,
            "failed_count": result.failed_count,
            "rolled_back": bool(rollback_on_failure and result.failed_count),
            "revoked_count": revoked_count,
            "grant_snapshot_available": grant_snapshot.get("available", False),
            "grant_snapshot": grant_snapshot.get("grant"),
        }

    @staticmethod
    def _snapshot_capability_grant(manager: Any, principal_id: str) -> Dict[str, Any]:
        get_grant = getattr(manager, "get_grant", None)
        if not callable(get_grant):
            return {"available": False, "grant": None}
        grant = get_grant(principal_id)
        if grant is None:
            return {"available": True, "grant": None}
        to_dict = getattr(grant, "to_dict", None)
        if callable(to_dict):
            return {"available": True, "grant": to_dict()}
        return {"available": False, "grant": None}

    @staticmethod
    def _restore_capability_grant(
        manager: Any,
        principal_id: str,
        snapshot: Dict[str, Any] | None,
    ) -> None:
        if snapshot is None:
            delete_grant = getattr(manager, "delete_grant", None)
            if callable(delete_grant):
                delete_grant(principal_id)
            return

        from .capability_grant_manager import CapabilityGrant

        grant = CapabilityGrant.from_dict(snapshot)
        grants = getattr(manager, "_grants", None)
        if isinstance(grants, dict):
            grants[principal_id] = grant
        save_grant = getattr(manager, "_save_grant", None)
        if callable(save_grant):
            save_grant(grant)

    @staticmethod
    def _approval_snapshot(manager: Any, target_pack_id: str) -> Dict[str, Any]:
        get_approval = getattr(manager, "get_approval", None)
        if not callable(get_approval):
            return {"available": False, "approval": None}
        approval = get_approval(target_pack_id)
        if approval is None:
            return {"available": True, "approval": None}
        to_dict = getattr(approval, "to_dict", None)
        if callable(to_dict):
            return {"available": True, "approval": to_dict()}
        return {"available": False, "approval": None}

    def _approve_target_pack(self, target_pack_id: str) -> Dict[str, Any]:
        try:
            from .approval_manager import get_approval_manager

            am = get_approval_manager()
            if not getattr(am, "_initialized", False):
                return {
                    "approved": False,
                    "skipped": True,
                    "reason": "approval_manager_uninitialized",
                }
            scan_packs = getattr(am, "scan_packs", None)
            if callable(scan_packs):
                scan_packs()
            previous_status = None
            get_status = getattr(am, "get_status", None)
            if callable(get_status):
                status = get_status(target_pack_id)
                previous_status = getattr(status, "value", status)
            approval_before = self._approval_snapshot(am, target_pack_id)
            result = am.approve(target_pack_id)
            if not getattr(result, "success", False):
                return {
                    "approved": False,
                    "skipped": False,
                    "reason": "target_pack_approval_failed",
                    "error": getattr(result, "error", "approval_failed"),
                }
            approval_after = self._approval_snapshot(am, target_pack_id)
            changed = previous_status != "approved"
            if approval_before.get("available") and approval_after.get("available"):
                changed = approval_before.get("approval") != approval_after.get("approval")
            return {
                "approved": True,
                "skipped": False,
                "reason": None,
                "changed": changed,
                "approval_snapshot_available": approval_before.get("available", False),
                "approval_snapshot": approval_before.get("approval"),
            }
        except Exception as exc:
            logger.debug("Failed to auto-approve setup pack target", exc_info=True)
            return {
                "approved": False,
                "skipped": False,
                "reason": "target_pack_approval_failed",
                "error": str(exc),
            }

    def _verify_target_pack_approval(self, target_pack_id: str) -> Dict[str, Any]:
        try:
            from .approval_manager import get_approval_manager

            am = get_approval_manager()
            if not getattr(am, "_initialized", False):
                return {
                    "approved": False,
                    "reason": "approval_manager_uninitialized",
                }
            scan_packs = getattr(am, "scan_packs", None)
            if callable(scan_packs):
                scan_packs()

            verify = getattr(am, "is_pack_approved_and_verified", None)
            if callable(verify):
                approved, reason = verify(target_pack_id)
                return {
                    "approved": bool(approved),
                    "reason": None if approved else reason or "not_approved",
                }

            get_status = getattr(am, "get_status", None)
            if not callable(get_status):
                return {
                    "approved": False,
                    "reason": "approval_status_unavailable",
                }
            status = get_status(target_pack_id)
            status_value = getattr(status, "value", status)
            if status_value != "approved":
                return {
                    "approved": False,
                    "reason": "not_approved",
                    "status": status_value,
                }

            verify_hash = getattr(am, "verify_hash", None)
            if callable(verify_hash) and not verify_hash(target_pack_id, use_cache=False):
                return {
                    "approved": False,
                    "reason": "hash_mismatch",
                }

            return {
                "approved": True,
                "reason": None,
            }
        except Exception as exc:
            logger.debug("Failed to verify setup pack target approval", exc_info=True)
            return {
                "approved": False,
                "reason": "target_pack_approval_check_failed",
                "error": str(exc),
            }

    def _rollback_target_pack_approval(
        self,
        target_pack_id: str,
        approval_result: Dict[str, Any],
    ) -> None:
        if not approval_result.get("changed"):
            return
        try:
            from .approval_manager import PackApproval, get_approval_manager

            am = get_approval_manager()
            if approval_result.get("approval_snapshot_available"):
                snapshot = approval_result.get("approval_snapshot")
                restore_approval = getattr(am, "restore_approval", None)
                if callable(restore_approval):
                    restore_approval(target_pack_id, snapshot)
                    return
                if snapshot is None:
                    remove_approval = getattr(am, "remove_approval", None)
                    if callable(remove_approval):
                        remove_approval(target_pack_id)
                    return
                approval = PackApproval.from_dict(snapshot)
                approvals = getattr(am, "_approvals", None)
                if isinstance(approvals, dict):
                    approvals[target_pack_id] = approval
                save_grant = getattr(am, "_save_grant", None)
                if callable(save_grant):
                    save_grant(approval)
                return

            remove_approval = getattr(am, "remove_approval", None)
            if callable(remove_approval):
                remove_approval(target_pack_id)
        except Exception:
            logger.debug("Failed to roll back setup pack target approval", exc_info=True)

    def _revoke_all_ok_for_definition(self, definition: SetupPackDefinition) -> int:
        try:
            from .capability_grant_manager import get_capability_grant_manager

            gm = get_capability_grant_manager()
            revoked = [
                gm.revoke_permission(definition.target_pack_id, permission_id)
                for permission_id in SETUP_PACK_ALL_OK_PERMISSIONS
            ]
            revoked_count = sum(1 for item in revoked if item)
            self._log_permission_event(
                "revoke_all_ok",
                True,
                principal_id=definition.target_pack_id,
                permission_id="*",
                details={
                    "setup_pack_id": definition.pack_id,
                    "revoked_count": revoked_count,
                    "reason": "setup_pack_install_rollback",
                },
            )
            return revoked_count
        except Exception:
            logger.debug("Failed to roll back setup pack all_ok grants", exc_info=True)
            return 0

    def _restore_all_ok_for_definition(
        self,
        definition: SetupPackDefinition,
        grant_result: Dict[str, Any],
    ) -> int:
        if grant_result.get("grant_snapshot_available"):
            try:
                from .capability_grant_manager import get_capability_grant_manager

                gm = get_capability_grant_manager()
                self._restore_capability_grant(
                    gm,
                    definition.target_pack_id,
                    grant_result.get("grant_snapshot"),
                )
                self._log_permission_event(
                    "restore_all_ok",
                    True,
                    principal_id=definition.target_pack_id,
                    permission_id="*",
                    details={
                        "setup_pack_id": definition.pack_id,
                        "reason": grant_result.get("rollback_reason")
                        or "setup_pack_install_rollback",
                    },
                )
                return int(grant_result.get("granted_count") or 0)
            except Exception:
                logger.debug("Failed to restore setup pack all_ok grant snapshot", exc_info=True)
        return self._revoke_all_ok_for_definition(definition)

    def _rollback_install_side_effects(
        self,
        definitions_to_revoke: List[SetupPackDefinition],
        approved_definitions: List[SetupPackDefinition],
        approval_results: Dict[str, Dict[str, Any]],
        grant_results: Dict[str, Dict[str, Any]] | None = None,
    ) -> None:
        for definition in reversed(definitions_to_revoke):
            if definition.supports_all_ok:
                self._restore_all_ok_for_definition(
                    definition,
                    (grant_results or {}).get(definition.pack_id, {}),
                )
        for definition in reversed(approved_definitions):
            self._rollback_target_pack_approval(
                definition.target_pack_id,
                approval_results.get(definition.pack_id, {}),
            )

    def _set_active_pack_identity(self, ecosystem_json_path: Path) -> Optional[str]:
        try:
            from backend_core.ecosystem.active_ecosystem import get_active_ecosystem_manager

            eco = json.loads(ecosystem_json_path.read_text(encoding="utf-8"))
            pack_identity = eco.get("pack_identity")
            if not isinstance(pack_identity, str) or not pack_identity.strip():
                return None
            active = get_active_ecosystem_manager()
            active.active_pack_identity = pack_identity
            return pack_identity
        except Exception:
            logger.debug("Failed to switch active_pack_identity during setup_pack install", exc_info=True)
            return None

    def _expand_requested_setup_pack_ids(
        self,
        requested_ids: List[str],
        definitions: Dict[str, SetupPackDefinition],
    ) -> List[str]:
        expanded_ids: List[str] = []
        seen: set[str] = set()
        visiting: set[str] = set()

        def visit(setup_pack_id: str) -> None:
            if setup_pack_id in seen or setup_pack_id in visiting:
                return
            definition = definitions.get(setup_pack_id)
            if definition is None:
                return
            visiting.add(setup_pack_id)
            for dependency in definition.depends_on:
                dependency_id = str(dependency.get("pack_id") or "").strip()
                if dependency_id and dependency_id in definitions:
                    visit(dependency_id)
            visiting.remove(setup_pack_id)
            seen.add(setup_pack_id)
            expanded_ids.append(setup_pack_id)

        for setup_pack_id in requested_ids:
            visit(setup_pack_id)
        return expanded_ids

    def install(self, setup_pack_ids: str | List[str]) -> Dict[str, Any]:
        definitions = self._load_definitions()
        requested_ids = self._normalize_setup_pack_ids(setup_pack_ids)
        if not requested_ids:
            return {
                "success": False,
                "error": "setup_pack_id or setup_pack_ids is required",
                "status_code": 400,
            }

        locations = {
            loc.pack_id: loc for loc in discover_pack_locations(str(self.ecosystem_dir))
        }
        expanded_requested_ids = self._expand_requested_setup_pack_ids(
            requested_ids,
            definitions,
        )

        ordered_definitions = [
            definitions[pack_id]
            for pack_id in expanded_requested_ids
            if pack_id in definitions
        ]
        errors: List[Dict[str, Any]] = []

        for setup_pack_id in requested_ids:
            if setup_pack_id not in definitions:
                errors.append({
                    "setup_pack_id": setup_pack_id,
                    "error": f"Unknown setup_pack: {setup_pack_id}",
                    "reason": "unknown_setup_pack",
                })
                self._log_system_event(
                    "setup_pack.install",
                    False,
                    details={"setup_pack_id": setup_pack_id},
                    error="unknown_setup_pack",
                )

        installable_definitions: List[SetupPackDefinition] = []
        for definition in ordered_definitions:
            if definition.target_pack_id not in locations:
                errors.append({
                    "setup_pack_id": definition.pack_id,
                    "target_pack_id": definition.target_pack_id,
                    "error": f"Target pack not found: {definition.target_pack_id}",
                    "reason": "target_pack_not_found",
                })
                self._log_system_event(
                    "setup_pack.install",
                    False,
                    details={
                        "setup_pack_id": definition.pack_id,
                        "target_pack_id": definition.target_pack_id,
                    },
                    error="target_pack_not_found",
                )
                continue
            installable_definitions.append(definition)

        if errors:
            return {
                "success": False,
                "installed": False,
                "installed_setup_pack_ids": [],
                "installed_target_pack_ids": [],
                "installed_setup_target_map": {},
                "granted_all_ok_target_pack_ids": [],
                "skipped_all_ok_setup_pack_ids": [],
                "active_setup_pack_id": None,
                "active_target_pack_id": None,
                "selection": {},
                "errors": errors,
                "error": "Setup pack selection contains unavailable packs",
                "status_code": 400,
            }

        contract_errors = self._validate_install_contracts(installable_definitions, locations)
        if contract_errors:
            errors.extend(contract_errors)
            self._log_system_event(
                "setup_pack.install",
                False,
                details={
                    "setup_pack_ids": [definition.pack_id for definition in installable_definitions],
                    "target_pack_ids": [definition.target_pack_id for definition in installable_definitions],
                    "errors": contract_errors,
                },
                error="setup_pack_contract_validation_failed",
            )
            return {
                "success": False,
                "installed": False,
                "installed_setup_pack_ids": [],
                "installed_target_pack_ids": [],
                "installed_setup_target_map": {},
                "granted_all_ok_target_pack_ids": [],
                "skipped_all_ok_setup_pack_ids": [],
                "active_setup_pack_id": None,
                "active_target_pack_id": None,
                "selection": {},
                "errors": errors,
                "error": "Setup pack compatibility validation failed",
                "status_code": 400,
            }

        approval_results: Dict[str, Dict[str, Any]] = {}
        for definition in installable_definitions:
            approval_result = self._approve_target_pack(definition.target_pack_id)
            approval_results[definition.pack_id] = approval_result
            if not approval_result.get("approved") and not approval_result.get("skipped"):
                errors.append({
                    "setup_pack_id": definition.pack_id,
                    "target_pack_id": definition.target_pack_id,
                    "error": approval_result.get("error") or "Failed to approve target pack",
                    "reason": "target_pack_approval_failed",
                    "approval": approval_result,
                })

        if errors:
            self._rollback_install_side_effects([], installable_definitions, approval_results)
            self._log_system_event(
                "setup_pack.install",
                False,
                details={
                    "setup_pack_ids": [definition.pack_id for definition in installable_definitions],
                    "target_pack_ids": [definition.target_pack_id for definition in installable_definitions],
                    "errors": errors,
                },
                error="target_pack_approval_failed",
            )
            return {
                "success": False,
                "installed": False,
                "installed_setup_pack_ids": [],
                "installed_target_pack_ids": [],
                "installed_setup_target_map": {},
                "granted_all_ok_target_pack_ids": [],
                "skipped_all_ok_setup_pack_ids": [],
                "active_setup_pack_id": None,
                "active_target_pack_id": None,
                "selection": {},
                "errors": errors,
                "error": "Setup pack target approval failed",
                "status_code": 400,
            }

        installed_definitions: List[SetupPackDefinition] = []
        grant_results: Dict[str, Dict[str, Any]] = {}
        granted_targets: List[str] = []
        skipped_all_ok_setup_pack_ids: List[str] = []
        for definition in installable_definitions:
            if definition.supports_all_ok:
                grant_result = self._grant_all_ok_for_definition(definition)
                grant_results[definition.pack_id] = grant_result
                if not grant_result.get("granted"):
                    errors.append({
                        "setup_pack_id": definition.pack_id,
                        "target_pack_id": definition.target_pack_id,
                        "error": "Failed to grant all OK permissions",
                        "reason": "all_ok_grant_failed",
                        "grant": grant_result,
                    })
                    self._rollback_install_side_effects(
                        [*installed_definitions, definition],
                        installable_definitions,
                        approval_results,
                        grant_results,
                    )
                    self._log_system_event(
                        "setup_pack.install",
                        False,
                        details={
                            "setup_pack_ids": [
                                item.pack_id for item in installable_definitions
                            ],
                            "target_pack_ids": [
                                item.target_pack_id for item in installable_definitions
                            ],
                            "errors": errors,
                        },
                        error="all_ok_grant_failed",
                    )
                    return {
                        "success": False,
                        "installed": False,
                        "installed_setup_pack_ids": [],
                        "installed_target_pack_ids": [],
                        "installed_setup_target_map": {},
                        "granted_all_ok_target_pack_ids": [],
                        "skipped_all_ok_setup_pack_ids": [],
                        "active_setup_pack_id": None,
                        "active_target_pack_id": None,
                        "selection": {},
                        "errors": errors,
                        "error": "Setup pack grant failed",
                        "status_code": 400,
                    }
                if definition.target_pack_id not in granted_targets:
                    granted_targets.append(definition.target_pack_id)
            else:
                skipped_all_ok_setup_pack_ids.append(definition.pack_id)
                self._log_permission_event(
                    "grant_all_ok",
                    False,
                    principal_id=definition.target_pack_id,
                    permission_id="*",
                    details={"setup_pack_id": definition.pack_id},
                    error="unsupported_all_ok",
                )
            installed_definitions.append(definition)

        if not installed_definitions:
            return {
                "success": False,
                "installed": False,
                "installed_setup_pack_ids": [],
                "installed_target_pack_ids": [],
                "installed_setup_target_map": {},
                "granted_all_ok_target_pack_ids": [],
                "skipped_all_ok_setup_pack_ids": [],
                "active_setup_pack_id": None,
                "active_target_pack_id": None,
                "selection": {},
                "errors": errors,
                "error": "No setup packs were installed",
                "status_code": 400,
            }

        active_definition = self._choose_active_definition(installed_definitions)
        active_target = locations[active_definition.target_pack_id]
        active_identity_snapshot = self._snapshot_active_pack_identity()
        selection_snapshot = self._snapshot_selection_file()
        active_pack_identity = self._set_active_pack_identity(active_target.ecosystem_json_path)

        if active_pack_identity is None:
            errors.append({
                "setup_pack_id": active_definition.pack_id,
                "target_pack_id": active_definition.target_pack_id,
                "error": "Failed to set active pack identity",
                "reason": "active_pack_switch_failed",
            })
            self._rollback_install_side_effects(
                installed_definitions,
                installable_definitions,
                approval_results,
                grant_results,
            )
            self._restore_active_pack_identity(active_identity_snapshot)
            self._restore_selection_file(selection_snapshot)
            self._log_system_event(
                "setup_pack.install",
                False,
                details={
                    "setup_pack_ids": [definition.pack_id for definition in installed_definitions],
                    "target_pack_ids": [definition.target_pack_id for definition in installed_definitions],
                    "active_setup_pack_id": active_definition.pack_id,
                    "active_target_pack_id": active_definition.target_pack_id,
                    "errors": errors,
                },
                error="active_pack_switch_failed",
            )
            return {
                "success": False,
                "installed": False,
                "installed_setup_pack_ids": [],
                "installed_target_pack_ids": [],
                "installed_setup_target_map": {},
                "granted_all_ok_target_pack_ids": [],
                "skipped_all_ok_setup_pack_ids": [],
                "active_setup_pack_id": None,
                "active_target_pack_id": None,
                "active_pack_identity": None,
                "selection": {},
                "errors": errors,
                "error": "Setup pack active identity switch failed",
                "status_code": 500,
            }

        try:
            selection = self._write_selection(installed_definitions, active_definition)
        except Exception as exc:
            errors.append({
                "setup_pack_id": active_definition.pack_id,
                "target_pack_id": active_definition.target_pack_id,
                "error": str(exc) or "Failed to persist setup pack selection",
                "reason": "selection_write_failed",
            })
            self._rollback_install_side_effects(
                installed_definitions,
                installable_definitions,
                approval_results,
                grant_results,
            )
            self._restore_active_pack_identity(active_identity_snapshot)
            self._restore_selection_file(selection_snapshot)
            self._log_system_event(
                "setup_pack.install",
                False,
                details={
                    "setup_pack_ids": [definition.pack_id for definition in installed_definitions],
                    "target_pack_ids": [definition.target_pack_id for definition in installed_definitions],
                    "active_setup_pack_id": active_definition.pack_id,
                    "active_target_pack_id": active_definition.target_pack_id,
                    "errors": errors,
                },
                error="selection_write_failed",
            )
            return {
                "success": False,
                "installed": False,
                "installed_setup_pack_ids": [],
                "installed_target_pack_ids": [],
                "installed_setup_target_map": {},
                "granted_all_ok_target_pack_ids": [],
                "skipped_all_ok_setup_pack_ids": [],
                "active_setup_pack_id": None,
                "active_target_pack_id": None,
                "active_pack_identity": None,
                "selection": {},
                "errors": errors,
                "error": "Setup pack selection write failed",
                "status_code": 500,
            }
        self._log_system_event(
            "setup_pack.install",
            True,
            details={
                "setup_pack_ids": [definition.pack_id for definition in installed_definitions],
                "target_pack_ids": [definition.target_pack_id for definition in installed_definitions],
                "active_setup_pack_id": active_definition.pack_id,
                "active_target_pack_id": active_definition.target_pack_id,
            },
            error=None,
        )
        return {
            "success": True,
            "installed": True,
            "installed_setup_pack_ids": [definition.pack_id for definition in installed_definitions],
            "installed_target_pack_ids": [
                definition.target_pack_id for definition in installed_definitions
            ],
            "installed_setup_target_map": {
                definition.pack_id: definition.target_pack_id for definition in installed_definitions
            },
            "granted_all_ok_target_pack_ids": granted_targets,
            "skipped_all_ok_setup_pack_ids": skipped_all_ok_setup_pack_ids,
            "active_setup_pack_id": active_definition.pack_id,
            "active_target_pack_id": active_definition.target_pack_id,
            "active_pack_identity": active_pack_identity,
            "selection": selection,
            "errors": errors,
        }

    def grant_all_ok(self, setup_pack_id: str) -> Dict[str, Any]:
        definitions = self._load_definitions()
        definition = definitions.get(setup_pack_id)
        if definition is None:
            self._log_permission_event(
                "grant_all_ok",
                False,
                principal_id="",
                permission_id="*",
                details={"setup_pack_id": setup_pack_id},
                error="unknown_setup_pack",
            )
            return {"error": f"Unknown setup_pack: {setup_pack_id}", "status_code": 404}
        if not definition.supports_all_ok:
            self._log_permission_event(
                "grant_all_ok",
                False,
                principal_id=definition.target_pack_id,
                permission_id="*",
                details={"setup_pack_id": setup_pack_id},
                error="unsupported_all_ok",
            )
            return {
                "error": f"setup_pack does not support all_ok: {setup_pack_id}",
                "status_code": 400,
                "reason": "unsupported_all_ok",
            }
        locations = {
            loc.pack_id: loc for loc in discover_pack_locations(str(self.ecosystem_dir))
        }
        if definition.target_pack_id not in locations:
            self._log_permission_event(
                "grant_all_ok",
                False,
                principal_id=definition.target_pack_id,
                permission_id="*",
                details={
                    "setup_pack_id": setup_pack_id,
                    "target_pack_id": definition.target_pack_id,
                },
                error="target_pack_not_found",
            )
            return {
                "error": f"Target pack not found: {definition.target_pack_id}",
                "status_code": 404,
                "reason": "target_pack_not_found",
                "principal_id": definition.target_pack_id,
            }
        contract_errors = self._validate_install_contracts([definition], locations)
        if contract_errors:
            self._log_permission_event(
                "grant_all_ok",
                False,
                principal_id=definition.target_pack_id,
                permission_id="*",
                details={
                    "setup_pack_id": setup_pack_id,
                    "target_pack_id": definition.target_pack_id,
                    "errors": contract_errors,
                },
                error="setup_pack_contract_validation_failed",
            )
            return {
                "error": "Setup pack compatibility validation failed",
                "status_code": 400,
                "reason": "setup_pack_contract_validation_failed",
                "errors": contract_errors,
                "principal_id": definition.target_pack_id,
            }
        if setup_pack_id not in self._selected_setup_pack_ids():
            self._log_permission_event(
                "grant_all_ok",
                False,
                principal_id=definition.target_pack_id,
                permission_id="*",
                details={
                    "setup_pack_id": setup_pack_id,
                    "target_pack_id": definition.target_pack_id,
                },
                error="setup_pack_not_selected",
            )
            return {
                "error": f"Setup pack is not selected: {setup_pack_id}",
                "status_code": 409,
                "reason": "setup_pack_not_selected",
                "principal_id": definition.target_pack_id,
            }
        approval_result = self._verify_target_pack_approval(definition.target_pack_id)
        if not approval_result.get("approved"):
            self._log_permission_event(
                "grant_all_ok",
                False,
                principal_id=definition.target_pack_id,
                permission_id="*",
                details={
                    "setup_pack_id": setup_pack_id,
                    "target_pack_id": definition.target_pack_id,
                    "approval": approval_result,
                },
                error="target_pack_not_approved",
            )
            return {
                "error": f"Target pack is not approved: {definition.target_pack_id}",
                "status_code": 403,
                "reason": "target_pack_not_approved",
                "approval": approval_result,
                "principal_id": definition.target_pack_id,
            }
        return self._grant_all_ok_for_definition(definition, rollback_on_failure=True)

    def revoke_all_ok(self, setup_pack_id: str) -> Dict[str, Any]:
        definitions = self._load_definitions()
        definition = definitions.get(setup_pack_id)
        if definition is None:
            self._log_permission_event(
                "revoke_all_ok",
                False,
                principal_id="",
                permission_id="*",
                details={"setup_pack_id": setup_pack_id},
                error="unknown_setup_pack",
            )
            return {"error": f"Unknown setup_pack: {setup_pack_id}", "status_code": 404}
        if not definition.supports_all_ok:
            self._log_permission_event(
                "revoke_all_ok",
                False,
                principal_id=definition.target_pack_id,
                permission_id="*",
                details={"setup_pack_id": setup_pack_id},
                error="unsupported_all_ok",
            )
            return {
                "error": f"setup_pack does not support all_ok: {setup_pack_id}",
                "status_code": 400,
                "reason": "unsupported_all_ok",
            }
        from .capability_grant_manager import get_capability_grant_manager

        gm = get_capability_grant_manager()
        revoked = []
        for permission_id in SETUP_PACK_ALL_OK_PERMISSIONS:
            revoked.append(gm.revoke_permission(definition.target_pack_id, permission_id))
        self._log_permission_event(
            "revoke_all_ok",
            True,
            principal_id=definition.target_pack_id,
            permission_id="*",
            details={
                "setup_pack_id": definition.pack_id,
                "revoked_count": sum(1 for item in revoked if item),
            },
        )
        return {
            "revoked": True,
            "principal_id": definition.target_pack_id,
            "permission_ids": list(SETUP_PACK_ALL_OK_PERMISSIONS),
            "revoked_count": sum(1 for item in revoked if item),
        }


_global_setup_pack_manager: SetupPackManager | None = None


def get_setup_pack_manager() -> SetupPackManager:
    global _global_setup_pack_manager
    if _global_setup_pack_manager is None:
        _global_setup_pack_manager = SetupPackManager()
    return _global_setup_pack_manager
