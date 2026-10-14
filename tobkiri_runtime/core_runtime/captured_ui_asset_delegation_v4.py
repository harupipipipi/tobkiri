"""Capture exact signed UI-only Packs without creating executable principals."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from tobkiri_protocol.canonical import canonical_digest, strict_loads
from tobkiri_protocol.validation import validate_document

from .captured_pack_ui_assets_v4 import (
    CapturedPackUIAssetDenied,
    CapturedPackUIAssets,
    UIAssetDelegation,
    _directory_identity,
    _open_root,
)
from .external_pack_catalog_v4 import (
    _read_regular_from_root_descriptor,
    _verify_signed_file_inventory,
    load_external_pack_catalog,
)
from .pack_artifact_integrity import host_policy_identity, read_host_policy_snapshot
from .pack_signature import SIGNED_MANIFEST_RELATIVE, verify_signed_manifest
from .publisher_key_versions import is_legacy_publisher_key, resolve_publisher_key


def _read_document(root: Path, relative: str) -> dict[str, Any]:
    descriptor = _open_root(root, _directory_identity(root))
    try:
        raw, _ = _read_regular_from_root_descriptor(
            descriptor, relative, max_file_bytes=16 * 1024 * 1024
        )
        value = strict_loads(raw)
        if not isinstance(value, dict):
            raise CapturedPackUIAssetDenied("UI delegation document is invalid")
        return value
    finally:
        os.close(descriptor)


def _require_asset_only(manifest: Mapping[str, Any]) -> None:
    requirements = manifest["requirements"]
    if (
        manifest["pack"]["kind"] != "normal_sandbox"
        or any(
            manifest[field]
            for field in (
                "functions",
                "contracts",
                "operation_catalog",
                "provider_catalog",
            )
        )
        or any(
            requirements[field]
            for field in (
                "capabilities",
                "secrets",
                "pack_dependencies",
                "contract_dependencies",
            )
        )
        or requirements["execution_boundary"] not in {"sandbox", "declarative_only"}
        or any(requirements["network"].values())
        or any(
            item["kind"] not in {"asset", "sidecar", "schema"}
            for item in manifest["artifacts"]
        )
    ):
        raise CapturedPackUIAssetDenied(
            "UI delegation requires an asset-only Normal Pack"
        )


class CapturedAssetOnlyUIPacks:
    """Resolve optional trusted UI owners from one exact effective set.

    These roots are separate from the executable binding roots and never enter
    the Broker catalog. A missing selected owner always raises; only an absent
    effective-set identity returns None. Publisher metadata is authenticated by
    admission, and the current nonlegacy exact signing scope is checked again
    for every use of the capture. No key or authorization is created here.
    """

    def __init__(self, *, artifact_pins: Mapping[str, str], owner_root: Path) -> None:
        self._pins = dict(artifact_pins)
        self._owner_root = Path(owner_root).absolute()
        self._assets: dict[tuple[str, str], CapturedPackUIAssets] = {}
        self._policy_identities: dict[str, tuple[int, str]] = {}
        self._trust_path = self._owner_root / "pack_control/publisher-trust.json"

    def _authorization(
        self,
        pack_id: str,
        root: Path,
        artifact_digest: str,
    ) -> tuple[UIAssetDelegation, tuple[int, str]]:
        signed = _read_document(root, SIGNED_MANIFEST_RELATIVE)
        policy = read_host_policy_snapshot(self._trust_path)
        identity = host_policy_identity(policy)
        if not identity[1]:
            raise CapturedPackUIAssetDenied("UI delegation policy is unauthenticated")
        publisher_id = str(signed.get("publisher_id") or "")
        signature = signed.get("signature")
        if not isinstance(signature, Mapping):
            raise CapturedPackUIAssetDenied("UI delegation signature is invalid")
        key_id = str(signature.get("key_id") or "")
        installs = policy.get("install_records")
        install = installs.get(pack_id) if isinstance(installs, Mapping) else None
        if (
            not isinstance(install, Mapping)
            or install.get("signature_required") is not True
            or install.get("publisher_id") != publisher_id
        ):
            raise CapturedPackUIAssetDenied("UI delegation install policy differs")
        if is_legacy_publisher_key(policy, publisher_id=publisher_id, key_id=key_id):
            raise CapturedPackUIAssetDenied(
                "UI delegation requires an exact nonlegacy scope"
            )
        signed_digest = canonical_digest(signed)
        resolve_publisher_key(
            policy,
            publisher_id=publisher_id,
            key_id=key_id,
            pack_id=pack_id,
            artifact_digest=artifact_digest,
            signed_manifest_digest=signed_digest,
        )
        return UIAssetDelegation(publisher_id, key_id, signed_digest), identity

    def assert_current(self) -> None:
        """Fence changed roots, revoked keys, changed policy and signed tuples."""
        for (pack_id, _index), assets in tuple(self._assets.items()):
            if _directory_identity(assets.pack_root) != assets.root_identity:
                raise CapturedPackUIAssetDenied("UI delegation root changed")
            delegation, policy_identity = self._authorization(
                pack_id, assets.pack_root, assets.artifact_digest
            )
            if (
                delegation != assets.delegation
                or policy_identity != self._policy_identities[pack_id]
            ):
                raise CapturedPackUIAssetDenied("UI delegation authorization changed")

    def capture(
        self,
        pack_id: str,
        index_file: str,
        *,
        assert_current: Callable[[], None],
    ) -> CapturedPackUIAssets | None:
        """Capture a selected admitted asset-only root, or an absent optional slot."""
        if pack_id not in self._pins:
            return None
        key = pack_id, index_file
        if key in self._assets:
            self._assets[key]._assert_current()
            return self._assets[key]
        pin = self._pins[pack_id]
        from .bootstrap.profile_capture import runtime_user_data_scope

        with runtime_user_data_scope(self._owner_root):
            admitted = load_external_pack_catalog({pack_id: pin})
        root = admitted.roots.get(pack_id)
        entry = admitted.entries.get(pack_id)
        if root is None or entry is None or entry["artifact_digest"] != pin:
            raise CapturedPackUIAssetDenied("selected UI delegation is not admitted")
        admitted_identity = entry["root_device"], entry["root_inode"]
        if _directory_identity(root) != admitted_identity:
            raise CapturedPackUIAssetDenied("UI delegation admission root changed")
        manifest = validate_document(_read_document(root, "pack.v4.json"), "pack")
        _require_asset_only(manifest)
        delegation, policy_identity = self._authorization(pack_id, root, pin)
        signed = _read_document(root, SIGNED_MANIFEST_RELATIVE)
        if (
            entry["publisher_id"] != delegation.publisher_id
            or entry["key_id"] != delegation.key_id
            or manifest["pack"].get("publisher_id", delegation.publisher_id)
            != delegation.publisher_id
            or entry["content_digest"]
            != canonical_digest(
                {
                    "signed_manifest": signed,
                    "artifact_digest": pin,
                }
            )
        ):
            raise CapturedPackUIAssetDenied("UI delegation admission identity differs")
        policy = read_host_policy_snapshot(self._trust_path)
        public_key = resolve_publisher_key(
            policy,
            publisher_id=delegation.publisher_id,
            key_id=delegation.key_id,
            pack_id=pack_id,
            artifact_digest=pin,
            signed_manifest_digest=delegation.signed_manifest_digest,
        )
        verify_signed_manifest(
            signed,
            public_key,
            expected_pack_id=pack_id,
            expected_publisher_id=delegation.publisher_id,
            expected_key_id=delegation.key_id,
            expected_version=manifest["pack"]["version"],
            expected_contract_versions={},
            expected_capabilities=[],
        )
        _verify_signed_file_inventory(root, signed)

        def current() -> None:
            assert_current()
            self.assert_current()

        assets = replace(
            CapturedPackUIAssets.capture(
                pack_id=pack_id,
                artifact_digest=pin,
                pack_root=root,
                owner_root=self._owner_root,
                index_file=index_file,
                assert_current=current,
            ),
            delegation=delegation,
        )
        if assets.root_identity != admitted_identity:
            raise CapturedPackUIAssetDenied("UI delegation admission root changed")
        self._assets[key] = assets
        self._policy_identities[pack_id] = policy_identity
        assets._assert_current()
        return assets
