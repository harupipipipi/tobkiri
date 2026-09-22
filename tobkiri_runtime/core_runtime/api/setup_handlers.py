"""Canonical Defaults Profile v4 setup HTTP handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict


class SetupHandlersMixin:
    """Expose one finite setup transaction with no legacy Registry authority."""

    _dispatch_session: Any = None

    @staticmethod
    def _recommended_default_profile_preview() -> Dict[str, Any]:
        """Return the exact integrity-checked Defaults Profile selection."""

        from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog

        bundle_root = (
            Path(__file__).resolve().parents[2]
            / "ecosystem"
            / "defaultspack"
            / "v4"
        )
        catalog = BundledCatalog.load(bundle_root)
        from ..bootstrap.profile_capture import prepare_default_profile_confirmation

        confirmation = prepare_default_profile_confirmation()
        profile = catalog.profiles["defaults"]
        base = profile["base"]
        shell = profile["shell"]
        selected = profile["packs"]
        pack_ids = [str(item["pack_id"]) for item in selected]
        if (
            base["pack_id"] != "defaults-basepack"
            or shell["provider_id"] != "shell.tauri.default"
            or len(pack_ids) != len(set(pack_ids))
            or any(pack_id not in catalog.packs for pack_id in pack_ids)
        ):
            raise ValueError("Defaults Profile selection is not exact")
        conversation_edges = [
            edge
            for edge in profile["requested_edges"]
            if edge["contract_id"] == "conversation.turn.v1"
        ]
        if len(conversation_edges) != 1:
            raise ValueError(
                "Defaults Profile must select exactly one conversation provider"
            )
        return {
            "available": True,
            "profile_id": "defaults",
            "name": str(profile.get("display_name") or "Tobkiri Defaults"),
            "base_pack": "defaults-basepack",
            "shell": {
                "provider_id": "shell.tauri.default",
                "contract_id": "app.shell.v1",
            },
            "pack_ids": pack_ids,
            "packs": [
                {
                    "pack_id": pack_id,
                    "display_name": str(
                        catalog.packs[pack_id]["pack"]["display_name"]
                    ),
                }
                for pack_id in pack_ids
            ],
            "conversation_provider": str(
                conversation_edges[0]["target_provider_id"]
            ),
            "confirmation": confirmation,
        }

    def _setup_list_packs(self) -> Dict[str, Any]:
        """Return the sole canonical setup candidate and its typed state."""

        from ..bootstrap.profile_capture import (
            active_default_profile_exists,
            capture_default_profile,
        )
        from ecosystem.defaultspack.domain.runtime_v4 import (
            ProfileReconfirmationRequired,
            ProfileResolutionDenied,
        )

        preview = self._recommended_default_profile_preview()
        state = "review_required"
        denial_diagnostic: str | None = None
        if active_default_profile_exists():
            try:
                capture_default_profile()
                state = "active"
            except ProfileReconfirmationRequired as error:
                denial_diagnostic = str(error)
            except ProfileResolutionDenied as error:
                state = "activation_denied"
                denial_diagnostic = str(error)
        payload = {
            "setup_api_version": "io.tobkiri.setup-state.v4",
            "state": state,
            "denial_diagnostic": denial_diagnostic,
            "packs": preview["packs"],
            "recommended_default_profile": preview,
            "required_transaction": [
                "catalog.verify",
                "profile.resolve",
                "authority.snapshot",
                "activation.prepare",
                "activation.commit",
                "runtime.capture",
            ],
        }
        from .defaults_setup_contract import validate_defaults_setup_payload

        return validate_defaults_setup_payload(payload)

    def _setup_install_pack(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Complete the explicitly confirmed Defaults v4 activation transaction."""

        expected_keys = {
            "setup_api_version",
            "operation_id",
            "confirmed",
            "confirmation",
        }
        if set(body) != expected_keys:
            return self._retired_state()
        if (
            body.get("setup_api_version") != "io.tobkiri.setup-state.v4"
            or body.get("operation_id") != "defaults.activate"
        ):
            return self._retired_state()
        preview = self._recommended_default_profile_preview()
        confirmation = body.get("confirmation")
        if not isinstance(confirmation, dict) or confirmation != preview["confirmation"]:
            return {
                "error": "Defaults Profile confirmation is stale or tampered",
                "status_code": 409,
                "state": "review_required",
                "write_set": [],
            }
        if body.get("confirmed") is not True:
            return {
                "error": "Defaults Profile requires explicit confirmation",
                "status_code": 409,
                "state": "confirmation_required",
                "write_set": [],
            }
        from ..bootstrap.profile_capture import (
            activation_audit_receipt,
            capture_default_profile,
        )

        lifecycle = getattr(self.__class__, "app_lifecycle_manager", None)
        try:
            if lifecycle is not None and hasattr(
                lifecycle, "activate_default_profile"
            ):
                activated = lifecycle.activate_default_profile(confirmation)
                if not isinstance(activated, tuple) or len(activated) != 2:
                    raise RuntimeError("Defaults activation result is invalid")
                active, dispatch_session = activated
                self.__class__._dispatch_session = dispatch_session
            else:
                active = capture_default_profile(confirmation=confirmation)
            audit_receipt = activation_audit_receipt(active)
        except Exception:
            return {
                "error": "Defaults Profile activation rejected",
                "status_code": 409,
                "state": "activation_rejected",
                "write_set": [],
            }
        return {
            "setup_api_version": "io.tobkiri.setup-state.v4",
            "state": "active",
            "profile_id": active.resolved.profile["profile_id"],
            "profile_revision": active.resolved.plan["profile_revision"],
            "plan_digest": active.resolved.plan["plan_digest"],
            "activation_id": active.activation["activation_id"],
            "security_epoch": active.activation["security_epoch"],
            "fencing_token": active.activation["fencing_token"],
            "authority_snapshot_digest": active.activation[
                "profile_authority_snapshot_digest"
            ],
            "audit_receipt": audit_receipt,
            "restart_required": False,
        }

    @staticmethod
    def _retired_state() -> Dict[str, Any]:
        return {
            "error": "Legacy setup-pack authority is retired; activate Defaults v4",
            "status_code": 410,
            "state": "legacy_setup_retired",
            "action": "install_defaults_profile",
        }

    @classmethod
    def _retired_setup_complete_state(cls) -> Dict[str, Any]:
        """Return the no-write contract for the retired setup completion route."""

        return {
            **cls._retired_state(),
            "setup_api_version": "io.tobkiri.setup-state.v4",
            "retired_route": "/api/setup/complete",
            "write_set": [],
        }

    def _setup_grant_all_ok(self, _setup_pack_id: str) -> Dict[str, Any]:
        """Reject the retired blanket approval surface."""

        return self._retired_state()

    def _setup_revoke_all_ok(self, _setup_pack_id: str) -> Dict[str, Any]:
        """Reject the retired blanket approval surface."""

        return self._retired_state()

    def _setup_get_migration_status(self) -> Dict[str, Any]:
        """Report that legacy executable migration is not a runtime operation."""

        return self._retired_state()


__all__ = ["SetupHandlersMixin"]
