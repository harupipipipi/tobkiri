"""setup HTTP handlers."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict

from ..di_container import get_container
from ..dependency_resolver import extract_dependency_specs
from ..pack_function_runtime import invoke_pack_function
from ..paths import discover_pack_locations
from ..setup_pack import get_setup_pack_manager


logger = logging.getLogger(__name__)


class SetupHandlersMixin:
    def _refresh_runtime_pack_approvals(self) -> None:
        """Reload approvals written by a setup-pack install into this kernel.

        The setup-pack manager and the running API server may own different
        ApprovalManager instances.  The install transaction already performed
        the user-confirmed approval; this only makes the persisted result
        visible to the current kernel without requiring a restart.
        """
        kernel = getattr(self, "kernel", None)
        approval_manager = getattr(self, "approval_manager", None) or getattr(
            kernel, "approval_manager", None
        )
        initialize = getattr(approval_manager, "initialize", None)
        if callable(initialize):
            initialize()

    @staticmethod
    def _approve_defaults_profile_packs(pack_ids: list[str]) -> Dict[str, Any]:
        """Approve the reviewed bundled pack set for Defaults Profile.

        This is deliberately limited to the exact pack IDs shown in the
        Defaults Profile review.  It is not an implicit startup-time approval:
        callers must first validate the user's explicit review confirmation.
        """
        from ..approval_manager import get_approval_manager

        manager = get_approval_manager()
        scan_packs = getattr(manager, "scan_packs", None)
        if callable(scan_packs):
            scan_packs()

        approved: list[str] = []
        already_approved: list[str] = []
        failed: list[Dict[str, str]] = []
        verify = getattr(manager, "is_pack_approved_and_verified", None)
        for pack_id in pack_ids:
            is_verified = False
            if callable(verify):
                is_verified, _reason = verify(pack_id)
            if is_verified:
                already_approved.append(pack_id)
                continue
            result = manager.approve(pack_id)
            if getattr(result, "success", False):
                approved.append(pack_id)
                continue
            failed.append(
                {
                    "pack_id": pack_id,
                    "error": str(getattr(result, "error", "approval_failed")),
                }
            )
        return {
            "requested_pack_ids": pack_ids,
            "approved_pack_ids": approved,
            "already_approved_pack_ids": already_approved,
            "failed": failed,
        }

    @staticmethod
    def _complete_local_defaults_setup() -> Dict[str, Any]:
        """Mark the local, no-account setup path complete.

        Choosing the recommended Defaults Profile is a complete local setup
        path.  Without this marker the runtime returns to ``/setup`` on every
        restart even after it has created and approved the profile.
        """
        from ..core_pack.core_setup.save_profile import save_profile

        result = save_profile({"username": "User", "language": "ja"})
        if not result.get("success"):
            raise RuntimeError(
                "; ".join(str(error) for error in result.get("errors") or [])
                or "failed to save local setup profile"
            )
        return result

    @staticmethod
    def _pack_function_state(pack_id: str, function_id: str) -> Dict[str, Any]:
        if not pack_id:
            return {
                "exists": False,
                "registry_available": False,
                "reason": "pack_id_missing",
            }
        function_registry = get_container().get_or_none("function_registry")
        if function_registry is None:
            return {
                "exists": False,
                "registry_available": False,
                "reason": "function_registry_unavailable",
            }
        qualified_name = (
            function_id if ":" in function_id else f"{pack_id}:{function_id}"
        )
        if function_registry.get(qualified_name) is None:
            return {
                "exists": False,
                "registry_available": True,
                "reason": "function_not_registered",
            }
        return {
            "exists": True,
            "registry_available": True,
            "reason": None,
        }

    @classmethod
    def _pack_function_exists(cls, pack_id: str, function_id: str) -> bool:
        return bool(cls._pack_function_state(pack_id, function_id).get("exists"))

    def _get_pack_migration_status(self, pack_id: str) -> Dict[str, Any]:
        if not pack_id:
            return {
                "pack_id": None,
                "available": False,
                "needs_user_migration": False,
                "registry_available": False,
                "reason": "pack_id_missing",
            }
        function_state = self._pack_function_state(pack_id, "get_migration_status")
        if not function_state.get("exists"):
            return {
                "pack_id": pack_id,
                "available": False,
                "needs_user_migration": False,
                "registry_available": bool(function_state.get("registry_available")),
                "reason": function_state.get("reason"),
            }
        status = invoke_pack_function(pack_id, "get_migration_status")
        payload = dict(status) if isinstance(status, dict) else {"result": status}
        payload.setdefault("needs_user_migration", False)
        payload["pack_id"] = pack_id
        payload["available"] = True
        payload["registry_available"] = True
        payload["reason"] = None
        return payload

    def _setup_list_packs(self) -> Dict[str, Any]:
        result = self._normalize_setup_pack_selection_payload(
            get_setup_pack_manager().list_packs()
        )
        result["recommended_default_profile"] = (
            self._recommended_default_profile_preview()
        )
        result["review_revision"] = self._setup_pack_review_revision(result.get("packs"))
        return result

    @staticmethod
    def _recommended_default_profile_preview() -> Dict[str, Any]:
        """Describe the official bundled Pack set for Defaults Profile.

        Setup-pack selection is intentionally separate from a startup profile:
        selecting the recommended setup pack must not silently grant approval to
        third-party or modified Packs.  The preview therefore includes every
        repository-reviewed bundled Pack while excluding definitions that
        explicitly identify another publisher, registry, trust status, or
        signing mode.  The caller still requires one explicit review of this
        exact list before approving it.
        """
        try:
            definitions = get_setup_pack_manager().list_packs().get("packs") or []
            official = [
                item
                for item in definitions
                if isinstance(item, dict)
                and SetupHandlersMixin._is_official_bundled_setup_pack(item)
                and not item.get("schema_issues")
            ]
            by_target_id = {
                str(item.get("target_pack_id") or item.get("pack_id") or "").strip(): item
                for item in official
                if str(item.get("target_pack_id") or item.get("pack_id") or "").strip()
            }
            pack_ids = sorted(
                SetupHandlersMixin._official_pack_dependency_closure(
                    set(by_target_id)
                )
            )
            if "defaultspack" in pack_ids:
                pack_ids.remove("defaultspack")
                pack_ids.insert(0, "defaultspack")
            return {
                "profile_id": "default-profile",
                "name": "Defaults Profile",
                "base_pack": "defaultspack",
                "pack_ids": pack_ids,
                "packs": [
                    {
                        "pack_id": pack_id,
                        "display_name": str(
                            by_target_id.get(pack_id, {}).get("display_name")
                            or pack_id
                        ),
                    }
                    for pack_id in pack_ids
                ],
            }
        except Exception:
            logger.warning("Unable to preview Defaults Profile", exc_info=True)
            return {
                "profile_id": "default-profile",
                "name": "Defaults Profile",
                "base_pack": "defaultspack",
                "pack_ids": ["defaultspack"],
                "packs": [
                    {"pack_id": "defaultspack", "display_name": "Defaultspack"}
                ],
            }

    @staticmethod
    def _official_pack_dependency_closure(seed_pack_ids: set[str]) -> set[str]:
        """Include bundled runtime dependencies required by reviewed Packs."""
        locations = {
            location.pack_id: location for location in discover_pack_locations()
        }
        included = set(seed_pack_ids)
        pending = [pack_id for pack_id in included if pack_id in locations]
        while pending:
            pack_id = pending.pop()
            manifest_path = locations[pack_id].pack_subdir / "ecosystem.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for spec in extract_dependency_specs(manifest):
                dependency_id = str(spec.get("pack_id") or "").strip()
                if not dependency_id or dependency_id not in locations:
                    continue
                if dependency_id in included:
                    continue
                included.add(dependency_id)
                pending.append(dependency_id)
        return included

    @staticmethod
    def _is_official_bundled_setup_pack(pack: Dict[str, Any]) -> bool:
        """Return whether a setup definition belongs to this Tobkiri bundle."""
        marketplace = pack.get("marketplace")
        marketplace = marketplace if isinstance(marketplace, dict) else {}
        signing = pack.get("signing")
        signing = signing if isinstance(signing, dict) else {}
        return (
            str(marketplace.get("publisher") or "rumi-ai").strip().lower()
            == "rumi-ai"
            and str(marketplace.get("registry") or "bundled").strip().lower()
            in {"bundled", "rumi_local_pack_registry"}
            and str(marketplace.get("status") or "verified").strip().lower()
            == "verified"
            and str(signing.get("mode") or "repository_reviewed").strip().lower()
            == "repository_reviewed"
            and signing.get("verified") is not False
        )

    @staticmethod
    def _setup_pack_review_revision(packs: Any) -> str:
        reviewed = []
        for item in packs if isinstance(packs, list) else []:
            if not isinstance(item, dict):
                continue
            reviewed.append({
                key: item.get(key)
                for key in (
                    "pack_id", "version", "source_path", "description", "risk_level",
                    "supports_all_ok", "required_permissions", "depends_on", "conflicts_with",
                )
            })
        encoded = json.dumps(reviewed, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "setup-review-v1:" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _setup_pack_requires_confirmation(pack: Dict[str, Any]) -> bool:
        """Return whether setup needs explicit item-level confirmation."""
        return (
            str(pack.get("risk_level") or "").strip().lower() == "high"
            or bool(pack.get("required_permissions"))
        )

    @staticmethod
    def _normalize_setup_pack_selection_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(payload or {})
        packs = result.get("packs")
        if not isinstance(packs, list):
            return result
        if not any(
            key in result
            for key in (
                "selected_setup_pack_id",
                "selected_setup_pack_ids",
                "active_setup_pack_id",
                "active_target_pack_id",
            )
        ):
            return result

        pack_by_id = {
            str(item.get("pack_id") or "").strip(): item
            for item in packs
            if isinstance(item, dict) and str(item.get("pack_id") or "").strip()
        }
        selected_ids: list[str] = []
        seen_selected: set[str] = set()
        for item in result.get("selected_setup_pack_ids") or []:
            pack_id = str(item or "").strip()
            if pack_id and pack_id in pack_by_id and pack_id not in seen_selected:
                selected_ids.append(pack_id)
                seen_selected.add(pack_id)

        legacy_selected = str(result.get("selected_setup_pack_id") or "").strip()
        if legacy_selected and legacy_selected in pack_by_id and legacy_selected not in seen_selected:
            selected_ids.append(legacy_selected)
            seen_selected.add(legacy_selected)

        active_setup_pack_id = str(result.get("active_setup_pack_id") or "").strip()
        if active_setup_pack_id not in seen_selected:
            active_setup_pack_id = ""
        active_pack = pack_by_id.get(active_setup_pack_id) if active_setup_pack_id else None
        active_target_pack_id = (
            str(active_pack.get("target_pack_id") or "").strip()
            if isinstance(active_pack, dict)
            else ""
        )

        for item in packs:
            if isinstance(item, dict):
                item["selected"] = str(item.get("pack_id") or "").strip() in seen_selected

        result["selected_setup_pack_ids"] = selected_ids
        result["selected_setup_pack_id"] = active_setup_pack_id or (
            selected_ids[0] if selected_ids else None
        )
        result["active_setup_pack_id"] = active_setup_pack_id or None
        result["active_target_pack_id"] = active_target_pack_id or None
        return result

    def _setup_install_pack(self, body: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(body or {})
        setup_pack_ids = payload.get("setup_pack_ids")
        if setup_pack_ids is None:
            setup_pack_id = str(payload.get("setup_pack_id", "")).strip()
            if not setup_pack_id:
                return {"error": "setup_pack_id or setup_pack_ids is required", "status_code": 400}
            setup_pack_ids = setup_pack_id

        selected = [str(item).strip() for item in (setup_pack_ids if isinstance(setup_pack_ids, list) else [setup_pack_ids]) if str(item).strip()]
        reviewed = [str(item).strip() for item in payload.get("reviewed_pack_ids") or [] if str(item).strip()]
        current = self._normalize_setup_pack_selection_payload(get_setup_pack_manager().list_packs())
        revision = self._setup_pack_review_revision(current.get("packs"))
        if payload.get("review_revision") != revision or reviewed != selected:
            return {"error": "Setup pack review is missing or stale; refresh and review the exact install plan", "status_code": 409}
        confirmed = {str(item).strip() for item in payload.get("confirmed_privileged_pack_ids") or [] if str(item).strip()}
        by_id = {str(item.get("pack_id") or ""): item for item in current.get("packs") or [] if isinstance(item, dict)}
        privileged = {
            pack_id
            for pack_id in selected
            if self._setup_pack_requires_confirmation(by_id.get(pack_id, {}))
        }
        if not privileged.issubset(confirmed):
            return {
                "error": (
                    "Each high-risk or permission-declaring pack requires "
                    "explicit item-level confirmation"
                ),
                "status_code": 400,
            }

        install_defaults_profile = bool(payload.get("install_defaults_profile"))
        default_profile_pack_ids: list[str] = []
        if install_defaults_profile:
            default_profile = self._recommended_default_profile_preview()
            default_profile_pack_ids = [
                str(pack_id).strip()
                for pack_id in default_profile.get("pack_ids") or []
                if str(pack_id).strip()
            ]
            reviewed_default_profile_pack_ids = [
                str(pack_id).strip()
                for pack_id in payload.get("reviewed_default_profile_pack_ids") or []
                if str(pack_id).strip()
            ]
            required_setup_pack_ids = {
                str(pack_id).strip()
                for pack_id in (default_profile.get("setup_pack_ids") or ["defaultspack"])
                if str(pack_id).strip()
            }
            if reviewed_default_profile_pack_ids != default_profile_pack_ids:
                return {
                    "error": (
                        "Defaults Profile review is missing or stale; refresh "
                        "and review every included pack"
                    ),
                    "status_code": 409,
                }
            if not required_setup_pack_ids.issubset(set(selected)):
                return {
                    "error": "Defaults Profile requires its recommended setup pack",
                    "status_code": 400,
                }
            if payload.get("confirmed_defaults_profile") is not True:
                return {
                    "error": "Defaults Profile requires explicit confirmation",
                    "status_code": 400,
                }

        result = get_setup_pack_manager().install(setup_pack_ids)
        if "error" in result:
            return result

        installed_target_pack_ids = [
            str(pack_id).strip()
            for pack_id in (result.get("installed_target_pack_ids") or [])
            if str(pack_id).strip()
        ]
        if not installed_target_pack_ids:
            active_target_pack_id = str(result.get("active_target_pack_id") or "").strip()
            if active_target_pack_id:
                installed_target_pack_ids = [active_target_pack_id]

        self._refresh_runtime_pack_approvals()

        if install_defaults_profile:
            default_profile_approval = self._approve_defaults_profile_packs(
                default_profile_pack_ids
            )
            if default_profile_approval["failed"]:
                return {
                    "error": "Defaults Profile could not approve every included pack",
                    "status_code": 500,
                    "default_profile_approval": default_profile_approval,
                }
            result["default_profile_approval"] = default_profile_approval
            try:
                result["local_setup_profile"] = self._complete_local_defaults_setup()
            except Exception as error:
                return {
                    "error": "Defaults Profile was approved but local setup could not complete",
                    "status_code": 500,
                    "default_profile_approval": default_profile_approval,
                    "details": str(error),
                }

            from ..startup_profiles import StartupProfileManager

            startup_profiles = StartupProfileManager()
            default_profile_update = startup_profiles.update_profile(
                "default-profile",
                {
                    "packs": default_profile_pack_ids,
                    "metadata": {"default_profile_pack_mode": "all_available"},
                },
            )
            if default_profile_update.get("error"):
                return {
                    "error": (
                        "Defaults Profile was approved but its complete Pack set "
                        "could not be saved"
                    ),
                    "status_code": int(
                        default_profile_update.get("status_code") or 500
                    ),
                    "default_profile_approval": default_profile_approval,
                    "default_profile_update": default_profile_update,
                }
            result["default_profile_update"] = default_profile_update

            default_profile_launch = startup_profiles.launch_profile(
                "default-profile"
            )
            if default_profile_launch.get("error"):
                return {
                    "error": "Defaults Profile was approved but could not launch",
                    "status_code": int(default_profile_launch.get("status_code") or 500),
                    "default_profile_approval": default_profile_approval,
                    "default_profile_launch": default_profile_launch,
                }
            result["default_profile_launch"] = default_profile_launch

        migration_statuses: Dict[str, Dict[str, Any]] = {}
        migrations: Dict[str, Any] = {}
        for target_pack_id in installed_target_pack_ids:
            status = self._get_pack_migration_status(target_pack_id)
            if (
                status.get("available")
                and status.get("needs_user_migration")
                and self._pack_function_exists(target_pack_id, "run_migration")
            ):
                migrations[target_pack_id] = invoke_pack_function(target_pack_id, "run_migration")
                status = self._get_pack_migration_status(target_pack_id)
            migration_statuses[target_pack_id] = status
        if migration_statuses:
            result["migration_statuses"] = migration_statuses
        if migrations:
            result["migrations"] = migrations
        return result

    def _setup_grant_all_ok(self, setup_pack_id: str) -> Dict[str, Any]:
        return get_setup_pack_manager().grant_all_ok(setup_pack_id)

    def _setup_revoke_all_ok(self, setup_pack_id: str) -> Dict[str, Any]:
        return get_setup_pack_manager().revoke_all_ok(setup_pack_id)

    def _setup_get_migration_status(self) -> Dict[str, Any]:
        selection = get_setup_pack_manager().get_selection()
        active_target_pack_id = str(selection.get("active_target_pack_id") or "")
        if not active_target_pack_id:
            return {
                "pack_id": None,
                "available": False,
                "needs_user_migration": False,
                "registry_available": False,
                "reason": "active_target_not_selected",
            }
        return self._get_pack_migration_status(active_target_pack_id)
