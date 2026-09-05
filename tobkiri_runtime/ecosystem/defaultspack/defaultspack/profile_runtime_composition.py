"""Defaultspack implementation of the generic Host Profile runtime port."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from core_runtime.profile_runtime_port import (
    SetupActivationDecision,
    register_profile_runtime,
)
from tobkiri_protocol.canonical import canonical_digest

if TYPE_CHECKING:
    from ecosystem.defaultspack.domain.runtime_v4.service import (
        ActivationStore,
        ActiveDefaultProfile,
        BundledCatalog,
    )


def defaultspack_profile_bundle_root() -> Path:
    """Return the installed Defaultspack Profile bundle without env fallback."""

    return Path(__file__).resolve().parents[1] / "v4"


def defaultspack_host_resource_root() -> Path:
    """Return the runtime root containing Defaultspack's packaged resources."""

    return Path(__file__).resolve().parents[3]


class DefaultspackProfileRuntime:
    """Bind Defaultspack's sealed Profile records into Host-owned checks."""

    def bundled_profile_root(self) -> Path:
        """Return Defaultspack's installed and sealed Profile bundle root."""

        return defaultspack_profile_bundle_root()

    def host_resource_root(self) -> Path:
        """Return the Host root which binds Defaultspack packaged resources."""

        return defaultspack_host_resource_root()

    def bootstrap_profile_id(self) -> str:
        """Return Defaultspack's explicit first-run Profile identity."""

        return "defaults"

    def setup_profile_preview(self, catalog: Any) -> Mapping[str, Any]:
        """Build Defaultspack's exact setup projection from its sealed catalog."""

        from ecosystem.defaultspack.domain.runtime_v4.service import BundledCatalog

        if not isinstance(catalog, BundledCatalog):
            raise self.denied("bundled setup Profile catalog is unavailable")
        profile_id = self.bootstrap_profile_id()
        profile = catalog.profiles.get(profile_id)
        if not isinstance(profile, Mapping):
            raise self.denied("bundled setup Profile is unavailable")
        base = profile.get("base")
        shell = profile.get("shell")
        selected = profile.get("packs")
        requested_edges = profile.get("requested_edges")
        if (
            not isinstance(base, Mapping)
            or not isinstance(shell, Mapping)
            or not isinstance(selected, list)
            or not isinstance(requested_edges, list)
        ):
            raise self.denied("bundled setup Profile is malformed")
        pack_ids = [
            str(item["pack_id"])
            for item in selected
            if isinstance(item, Mapping) and isinstance(item.get("pack_id"), str)
        ]
        if len(pack_ids) != len(selected) or len(pack_ids) != len(set(pack_ids)):
            raise self.denied("bundled setup Profile has invalid Pack selection")
        if any(pack_id not in catalog.packs for pack_id in pack_ids):
            raise self.denied("bundled setup Profile selects an unavailable Pack")
        base_pack = base.get("pack_id")
        shell_provider = shell.get("provider_id")
        shell_contract = shell.get("contract_id")
        if not all(isinstance(value, str) for value in (base_pack, shell_provider, shell_contract)):
            raise self.denied("bundled setup Profile has invalid base or shell")
        conversation_edges = [
            edge
            for edge in requested_edges
            if isinstance(edge, Mapping)
            and edge.get("contract_id") == "conversation.turn.v1"
            and isinstance(edge.get("target_provider_id"), str)
        ]
        if len(conversation_edges) != 1:
            raise self.denied("bundled setup Profile must select exactly one conversation provider")
        return {
            "available": True,
            "profile_id": profile_id,
            "name": str(profile.get("display_name") or "Tobkiri Defaults"),
            "base_pack": base_pack,
            "shell": {
                "provider_id": shell_provider,
                "contract_id": shell_contract,
            },
            "pack_ids": pack_ids,
            "packs": [
                {
                    "pack_id": pack_id,
                    "display_name": str(catalog.packs[pack_id]["pack"]["display_name"]),
                }
                for pack_id in pack_ids
            ],
            "conversation_provider": str(conversation_edges[0]["target_provider_id"]),
        }

    def _confirmation(
        self,
        *,
        resolved: Any,
        profile_id: str,
        authority_snapshot_digest: str,
        security_epoch: int,
        confirmation_api_version: str,
        operation_id: str,
    ) -> Mapping[str, Any]:
        """Build one Defaultspack confirmation from Host-captured facts."""

        confirmation = {
            "confirmation_api_version": confirmation_api_version,
            "operation_id": operation_id,
            "profile_id": profile_id,
            "catalog_revision": resolved.profile["catalog_revision"],
            "profile_revision": resolved.plan["profile_revision"],
            "plan_digest": resolved.plan["plan_digest"],
            "authority_snapshot_digest": authority_snapshot_digest,
            "security_epoch": security_epoch,
            "base": dict(resolved.plan["base"]),
            "shell": dict(resolved.plan["shell"]),
            "bindings": [dict(binding) for binding in resolved.plan["bindings"]],
        }
        confirmation["confirmation_digest"] = canonical_digest(confirmation)
        return confirmation

    def bootstrap_confirmation(
        self,
        *,
        resolved: Any,
        profile_id: str,
        authority_snapshot_digest: str,
        security_epoch: int,
    ) -> Mapping[str, Any]:
        """Build Defaultspack's explicit bootstrap activation confirmation."""

        return self._confirmation(
            resolved=resolved,
            profile_id=profile_id,
            authority_snapshot_digest=authority_snapshot_digest,
            security_epoch=security_epoch,
            confirmation_api_version="io.tobkiri.defaults-confirmation.v1",
            operation_id="defaults.activate",
        )

    def profile_confirmation(
        self,
        *,
        resolved: Any,
        profile_id: str,
        authority_snapshot_digest: str,
        security_epoch: int,
    ) -> Mapping[str, Any]:
        """Build Defaultspack's named-Profile activation confirmation."""

        return self._confirmation(
            resolved=resolved,
            profile_id=profile_id,
            authority_snapshot_digest=authority_snapshot_digest,
            security_epoch=security_epoch,
            confirmation_api_version="io.tobkiri.profile-confirmation.v1",
            operation_id="profile.activate",
        )

    def setup_listing(
        self,
        catalog: Any,
        confirmation: Mapping[str, Any],
        *,
        active: bool,
        activation_denied: bool,
        denial_diagnostic: str | None,
    ) -> Mapping[str, Any]:
        """Return Defaultspack's complete, validated setup presentation."""

        preview = self.setup_profile_preview(catalog)
        if preview.get("profile_id") != confirmation.get("profile_id"):
            raise self.denied("Defaultspack setup confirmation is mismatched")
        state = (
            "active" if active else "activation_denied" if activation_denied else "review_required"
        )
        payload = {
            "setup_api_version": "io.tobkiri.setup-state.v4",
            "state": state,
            "denial_diagnostic": denial_diagnostic,
            "packs": preview["packs"],
            "recommended_default_profile": {**preview, "confirmation": dict(confirmation)},
            "required_transaction": [
                "catalog.verify",
                "profile.resolve",
                "authority.snapshot",
                "activation.prepare",
                "activation.commit",
                "runtime.capture",
            ],
        }
        from ecosystem.defaultspack.defaultspack.setup_contract import (
            validate_defaults_setup_payload,
        )

        return validate_defaults_setup_payload(payload)

    def setup_preview(self, listing: Mapping[str, Any]) -> Mapping[str, Any]:
        """Extract Defaultspack's recommended Profile preview from a listing."""

        recommended = listing.get("recommended_default_profile")
        if not isinstance(recommended, Mapping):
            raise self.denied("Defaultspack setup listing is malformed")
        return dict(recommended)

    def setup_activation_decision(
        self,
        body: Mapping[str, Any],
        listing: Mapping[str, Any] | None,
    ) -> SetupActivationDecision:
        """Validate the exact Defaultspack activation request before Host writes."""

        expected_keys = {
            "setup_api_version",
            "operation_id",
            "confirmed",
            "confirmation",
        }
        if set(body) != expected_keys:
            return SetupActivationDecision(response=self.retired_setup_response())
        if (
            body.get("setup_api_version") != "io.tobkiri.setup-state.v4"
            or body.get("operation_id") != "defaults.activate"
        ):
            return SetupActivationDecision(response=self.retired_setup_response())
        if body.get("confirmed") is not True:
            return SetupActivationDecision(
                response={
                    "error": "Defaults Profile requires explicit confirmation",
                    "status_code": 409,
                    "state": "confirmation_required",
                    "write_set": [],
                }
            )
        confirmation = body.get("confirmation")
        if listing is None:
            if not isinstance(confirmation, Mapping):
                return SetupActivationDecision(
                    response={
                        "error": "Defaults Profile confirmation is stale or tampered",
                        "status_code": 409,
                        "state": "review_required",
                        "write_set": [],
                    }
                )
            return SetupActivationDecision(confirmation=dict(confirmation))
        recommended = listing.get("recommended_default_profile")
        expected_confirmation = (
            recommended.get("confirmation") if isinstance(recommended, Mapping) else None
        )
        if (
            not isinstance(confirmation, Mapping)
            or not isinstance(expected_confirmation, Mapping)
            or dict(confirmation) != dict(expected_confirmation)
        ):
            return SetupActivationDecision(
                response={
                    "error": "Defaults Profile confirmation is stale or tampered",
                    "status_code": 409,
                    "state": "review_required",
                    "write_set": [],
                }
            )
        return SetupActivationDecision(confirmation=dict(confirmation))

    def setup_activation_success(
        self,
        active: Any,
        audit_receipt: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Project the Defaultspack activation receipt returned to setup UI."""

        return {
            "setup_api_version": "io.tobkiri.setup-state.v4",
            "state": "active",
            "profile_id": active.resolved.profile["profile_id"],
            "profile_revision": active.resolved.plan["profile_revision"],
            "plan_digest": active.resolved.plan["plan_digest"],
            "activation_id": active.activation["activation_id"],
            "security_epoch": active.activation["security_epoch"],
            "fencing_token": active.activation["fencing_token"],
            "authority_snapshot_digest": active.activation["profile_authority_snapshot_digest"],
            "audit_receipt": dict(audit_receipt),
            "restart_required": False,
        }

    def setup_activation_failure(self) -> Mapping[str, Any]:
        """Return Defaultspack's no-write response for a failed commit."""

        return {
            "error": "Defaults Profile activation rejected",
            "status_code": 409,
            "state": "activation_rejected",
            "write_set": [],
        }

    def retired_setup_response(
        self,
        *,
        route: str | None = None,
    ) -> Mapping[str, Any]:
        """Return Defaultspack's retired legacy setup response."""

        response: dict[str, Any] = {
            "error": "Legacy setup-pack authority is retired; activate Defaults v4",
            "status_code": 410,
            "state": "legacy_setup_retired",
            "action": "install_defaults_profile",
        }
        if route is not None:
            response.update(
                {
                    "setup_api_version": "io.tobkiri.setup-state.v4",
                    "retired_route": route,
                    "write_set": [],
                }
            )
        return response

    def load_catalog(self, root: Path) -> BundledCatalog:
        """Load the exact Defaultspack Profile bundle."""

        from ecosystem.defaultspack.domain.runtime_v4.service import BundledCatalog

        return BundledCatalog.load(root)

    def catalog_with_packs(
        self,
        catalog: BundledCatalog,
        packs: Mapping[str, Any],
    ) -> BundledCatalog:
        """Preserve Defaultspack metadata while adding admitted Pack catalogs."""

        from ecosystem.defaultspack.domain.runtime_v4.service import BundledCatalog
        from core_runtime.external_pack_catalog_v4 import (
            load_admitted_external_executable_catalog,
        )

        executable_catalogs = dict(catalog.executable_catalogs)
        external_pack_ids = set(packs) - set(catalog.packs)
        for pack_id in sorted(external_pack_ids):
            manifest = packs[pack_id]
            if not isinstance(manifest, Mapping):
                raise self.denied("external Pack manifest is invalid")
            executable_catalogs[pack_id] = (
                load_admitted_external_executable_catalog(pack_id, manifest)
            )
        return BundledCatalog(
            root=catalog.root,
            packs=dict(packs),
            bases=catalog.bases,
            shells=catalog.shells,
            profiles=catalog.profiles,
            artifact_root=catalog.artifact_root,
            executable_catalogs=executable_catalogs,
        )

    def catalog_with_profiles(
        self,
        catalog: BundledCatalog,
        profiles: Mapping[str, Any],
    ) -> BundledCatalog:
        """Preserve the sealed Pack inventory while replacing Profile definitions."""

        from ecosystem.defaultspack.domain.runtime_v4.service import BundledCatalog

        return BundledCatalog(
            root=catalog.root,
            packs=catalog.packs,
            bases=catalog.bases,
            shells=catalog.shells,
            profiles=dict(profiles),
            artifact_root=catalog.artifact_root,
            executable_catalogs=catalog.executable_catalogs,
        )

    def resolve_profile(
        self,
        catalog: BundledCatalog,
        profile_id: str,
        **kwargs: Any,
    ) -> Any:
        """Resolve a Defaultspack Profile with Host-captured authority facts."""

        from ecosystem.defaultspack.domain.runtime_v4.service import (
            resolve_default_profile,
        )

        return resolve_default_profile(catalog, profile_id, **kwargs)

    def dynamic_profile_edges(
        self,
        catalog: BundledCatalog,
        profile_id: str,
        pack_ids: tuple[str, ...],
    ) -> tuple[Mapping[str, Any], ...]:
        """Return Defaultspack optional Pack operation edges."""

        from ecosystem.defaultspack.domain.runtime_v4.service import (
            dynamic_profile_edges,
        )

        return dynamic_profile_edges(catalog, profile_id, pack_ids)

    def activation_store(self, **kwargs: Any) -> ActivationStore:
        """Construct Defaultspack's authenticated activation store."""

        from ecosystem.defaultspack.domain.runtime_v4.service import ActivationStore

        state_root = kwargs.pop("root")
        workspace_root = kwargs.pop("workspace")
        return ActivationStore(state_root, workspace_root, **kwargs)

    def denied(self, message: str) -> Exception:
        """Return the canonical Defaultspack Profile denial."""

        from ecosystem.defaultspack.domain.runtime_v4.service import (
            ProfileResolutionDenied,
        )

        return ProfileResolutionDenied(message)

    def is_reconfirmation_required(self, error: BaseException) -> bool:
        """Classify Defaultspack stale-reconfirmation failures."""

        from ecosystem.defaultspack.domain.runtime_v4.service import (
            ProfileReconfirmationRequired,
        )

        return isinstance(error, ProfileReconfirmationRequired)

    def is_resolution_denied(self, error: BaseException) -> bool:
        """Classify Defaultspack Profile resolution failures."""

        from ecosystem.defaultspack.domain.runtime_v4.service import (
            ProfileResolutionDenied,
        )

        return isinstance(error, ProfileResolutionDenied)

    def active_profile(
        self,
        resolved: Any,
        activation: Mapping[str, Any],
    ) -> ActiveDefaultProfile:
        """Rebuild a Defaultspack active envelope for the Host cache."""

        from ecosystem.defaultspack.domain.runtime_v4.service import (
            ActiveDefaultProfile,
        )

        return ActiveDefaultProfile(resolved=resolved, activation=dict(activation))


_DEFAULTSPACK_PROFILE_RUNTIME = DefaultspackProfileRuntime()


def install_defaultspack_profile_runtime() -> DefaultspackProfileRuntime:
    """Register Defaultspack Profile composition for the current process."""

    installed = register_profile_runtime(_DEFAULTSPACK_PROFILE_RUNTIME)
    if installed is not _DEFAULTSPACK_PROFILE_RUNTIME:
        raise RuntimeError("a different Profile runtime composition is installed")
    return _DEFAULTSPACK_PROFILE_RUNTIME


__all__ = [
    "DefaultspackProfileRuntime",
    "defaultspack_host_resource_root",
    "defaultspack_profile_bundle_root",
    "install_defaultspack_profile_runtime",
]
