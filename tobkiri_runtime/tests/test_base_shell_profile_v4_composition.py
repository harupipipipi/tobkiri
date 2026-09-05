from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.composition import (
    CompositionError,
    catalog_payload,
    compose_runtime_profile,
    definition_revision,
    load_verified_catalog,
    verify_profile_lock,
)


DIGEST = "sha256:" + "0" * 64
KEY_ID = "catalog.release"


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _provenance(source_path: str) -> dict[str, object]:
    return {
        "schema": "io.tobkiri.provenance.v1",
        "source_kind": "repository",
        "source_path": source_path,
        "source_digest": DIGEST,
        "repository_commit": "0" * 40,
        "repository_tree": "0" * 64,
        "generator": "composition-test",
        "generator_version": "1.0.0",
        "normative": True,
        "evidence": [],
    }


def _definition(document: dict[str, object]) -> dict[str, object]:
    document["definition_revision"] = definition_revision(document)
    return document


def _fixtures(tmp_path: Path) -> tuple[dict, dict, Path, bytes]:
    root = tmp_path / "artifacts"
    root.mkdir(parents=True)
    base_bytes = b"base foundation"
    shell_bytes = b"verified headless-test shell"
    (root / "base.pack").write_bytes(base_bytes)
    (root / "shell.bin").write_bytes(shell_bytes)

    base = _definition(
        {
            "base_api_version": "io.tobkiri.base.v4",
            "pack_id": "defaults-basepack",
            "artifact_digest": _digest(base_bytes),
            "capability_foundation": {
                "provided_contracts": ["runtime.profile.v1"],
                "required_contracts": [],
            },
            "policy_foundation": {
                "policy_digest": DIGEST,
                "network_default": "deny",
                "host_effect_default": "deny",
            },
            "dependencies": [],
            "shell_requirements": {
                "mode": "interactive",
                "presentation_families": ["terminal"],
                "required_capabilities": ["commands", "structured-stdio"],
            },
            "state_owners": ["defaults.state"],
            "provenance": _provenance("bases/defaults.json"),
        }
    )
    shell = _definition(
        {
            "shell_api_version": "io.tobkiri.shell.v4",
            "provider_id": "shell.cli.default",
            "pack_id": "shell-cli-pack",
            "artifact_digest": _digest(shell_bytes),
            "contract_id": "app.shell.v1",
            "presentation": {
                "family": "terminal",
                "kind": "terminal_stdio",
                "technology": "native-cli",
                "capabilities": ["commands", "structured-stdio"],
                "consumes_contribution_contracts": [],
            },
            "launch": {
                "prebuilt_only": True,
                "variants": [
                    {
                        "platform": "linux",
                        "architecture": "x86_64",
                        "artifact_digest": _digest(shell_bytes),
                        "entrypoint_digest": _digest(shell_bytes),
                        "relative_path": "shell.bin",
                        "entrypoint": "shell.bin",
                        "bundle_identity": "io.tobkiri.shell.cli",
                    }
                ],
            },
            "local_auth": {
                "protocol": "io.tobkiri.local-auth.v1",
                "audience": "runtime-profile",
            },
            "provenance": _provenance("shells/cli.json"),
        }
    )
    catalog = {
        "catalog_api_version": "io.tobkiri.composition-catalog.v4",
        "catalog_revision": DIGEST,
        "bases": [
            {
                "definition": base,
                "relative_path": "base.pack",
                "approval_state": "verified",
            }
        ],
        "shells": [{"definition": shell, "approval_state": "verified"}],
        "packs": [],
        "integrity": {
            "payload_digest": DIGEST,
            "signature": {
                "algorithm": "ed25519",
                "key_id": KEY_ID,
                "value": base64.b64encode(b"0" * 64).decode("ascii"),
            },
        },
    }
    profile = {
        "profile_api_version": "io.tobkiri.profile.v4",
        "profile_id": "defaults-cli",
        "state": "resolved",
        "mode": "interactive",
        "catalog_revision": DIGEST,
        "base": {
            "pack_id": base["pack_id"],
            "artifact_digest": base["artifact_digest"],
            "definition_revision": base["definition_revision"],
            "resolution": "verified",
        },
        "shell": {
            "provider_id": shell["provider_id"],
            "pack_id": shell["pack_id"],
            "artifact_digest": shell["artifact_digest"],
            "definition_revision": shell["definition_revision"],
            "contract_id": "app.shell.v1",
            "platform": "linux",
            "architecture": "x86_64",
        },
        "packs": [],
        "requested_edges": [],
        "authority_references": [],
        "profile_authority_snapshot_digest": DIGEST,
        "provenance": _provenance("profiles/defaults-cli.json"),
    }
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return catalog, profile, root, (private_key, public_key)


def _write_signed(tmp_path: Path, catalog: dict, private_key: Ed25519PrivateKey) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    digest = canonical_digest(catalog_payload(catalog))
    catalog["catalog_revision"] = digest
    catalog["integrity"]["payload_digest"] = digest
    catalog["integrity"]["signature"]["value"] = base64.b64encode(
        private_key.sign(digest.encode("ascii"))
    ).decode("ascii")
    path = tmp_path / "composition-catalog.v4.json"
    path.write_text(json.dumps(catalog), encoding="utf-8")
    return path


def _load(tmp_path: Path, catalog: dict, root: Path, keys: tuple) -> object:
    private_key, public_key = keys
    path = _write_signed(tmp_path, catalog, private_key)
    return load_verified_catalog(path, artifact_root=root, trusted_public_keys={KEY_ID: public_key})


def test_exact_verified_composition_binds_base_shell_revision_and_local_auth(
    tmp_path: Path,
) -> None:
    catalog, profile, root, keys = _fixtures(tmp_path)
    verified = _load(tmp_path, catalog, root, keys)
    profile["catalog_revision"] = verified.revision

    first = compose_runtime_profile(profile, verified, security_epoch=7)
    restarted = compose_runtime_profile(profile, verified, security_epoch=7)

    assert first == restarted
    assert first.shell_artifact_path == root / "shell.bin"
    assert first.local_auth_protocol == "io.tobkiri.local-auth.v1"
    assert first.local_auth_audience == "runtime-profile"
    assert [item["role"] for item in first.profile_lock["effective_set"]] == [
        "base",
        "shell",
    ]
    assert first.profile_lock["content_projections"] == []
    assert verify_profile_lock(first.profile_lock) == first.profile_lock


def test_catalog_digest_and_signature_tampering_fail_closed(tmp_path: Path) -> None:
    catalog, _profile, root, keys = _fixtures(tmp_path)
    private_key, public_key = keys
    path = _write_signed(tmp_path, catalog, private_key)
    catalog["bases"][0]["approval_state"] = "revoked"
    path.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(CompositionError, match="revision"):
        load_verified_catalog(path, artifact_root=root, trusted_public_keys={KEY_ID: public_key})

    path = _write_signed(tmp_path, catalog, private_key)
    catalog["integrity"]["signature"]["value"] = base64.b64encode(b"x" * 64).decode("ascii")
    path.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(CompositionError, match="signature"):
        load_verified_catalog(path, artifact_root=root, trusted_public_keys={KEY_ID: public_key})


def test_missing_duplicate_unapproved_and_stale_selection_fail_closed(
    tmp_path: Path,
) -> None:
    catalog, profile, root, keys = _fixtures(tmp_path)
    duplicate = copy.deepcopy(catalog)
    duplicate["shells"].append(copy.deepcopy(duplicate["shells"][0]))
    with pytest.raises(CompositionError, match="duplicate"):
        _load(tmp_path, duplicate, root, keys)

    unapproved = copy.deepcopy(catalog)
    unapproved["shells"][0]["approval_state"] = "unapproved"
    verified = _load(tmp_path, unapproved, root, keys)
    profile["catalog_revision"] = verified.revision
    with pytest.raises(CompositionError, match="not approved"):
        compose_runtime_profile(profile, verified, security_epoch=1)

    verified = _load(tmp_path, catalog, root, keys)
    profile["catalog_revision"] = DIGEST
    with pytest.raises(CompositionError, match="stale catalog"):
        compose_runtime_profile(profile, verified, security_epoch=1)

    profile["catalog_revision"] = verified.revision
    (root / "shell.bin").unlink()
    with pytest.raises(CompositionError, match="missing"):
        compose_runtime_profile(profile, verified, security_epoch=1)


def test_incompatible_tampered_path_escape_and_symlink_fail_closed(
    tmp_path: Path,
) -> None:
    catalog, profile, root, keys = _fixtures(tmp_path)
    incompatible = copy.deepcopy(catalog)
    incompatible["shells"][0]["definition"]["presentation"]["capabilities"] = ["commands"]
    incompatible["shells"][0]["definition"]["definition_revision"] = definition_revision(
        incompatible["shells"][0]["definition"]
    )
    verified = _load(tmp_path, incompatible, root, keys)
    profile["catalog_revision"] = verified.revision
    profile["shell"]["definition_revision"] = incompatible["shells"][0]["definition"][
        "definition_revision"
    ]
    with pytest.raises(CompositionError, match="lacks required"):
        compose_runtime_profile(profile, verified, security_epoch=1)

    catalog, profile, root, keys = _fixtures(tmp_path / "tamper")
    verified = _load(tmp_path / "tamper", catalog, root, keys)
    profile["catalog_revision"] = verified.revision
    (root / "shell.bin").write_bytes(b"tampered")
    with pytest.raises(CompositionError, match="tampered"):
        compose_runtime_profile(profile, verified, security_epoch=1)

    catalog, profile, root, keys = _fixtures(tmp_path / "symlink")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    (root / "shell.bin").unlink()
    (root / "shell.bin").symlink_to(outside)
    verified = _load(tmp_path / "symlink", catalog, root, keys)
    profile["catalog_revision"] = verified.revision
    with pytest.raises(CompositionError, match="symlink"):
        compose_runtime_profile(profile, verified, security_epoch=1)

    escaped = copy.deepcopy(catalog)
    escaped["shells"][0]["definition"]["launch"]["variants"][0]["relative_path"] = "../outside.bin"
    escaped["shells"][0]["definition"]["definition_revision"] = definition_revision(
        escaped["shells"][0]["definition"]
    )
    with pytest.raises(CompositionError, match="catalog rejected"):
        _load(tmp_path / "escaped", escaped, root, keys)


def test_lock_mutation_is_rejected(tmp_path: Path) -> None:
    catalog, profile, root, keys = _fixtures(tmp_path)
    verified = _load(tmp_path, catalog, root, keys)
    profile["catalog_revision"] = verified.revision
    binding = compose_runtime_profile(profile, verified, security_epoch=1)
    mutated = copy.deepcopy(binding.profile_lock)
    mutated["security_epoch"] = 2
    with pytest.raises(CompositionError, match="digest"):
        verify_profile_lock(mutated)


def test_headless_profile_has_no_implicit_shell_or_presentation_artifact(
    tmp_path: Path,
) -> None:
    catalog, profile, root, keys = _fixtures(tmp_path)
    base = catalog["bases"][0]["definition"]
    base["shell_requirements"] = {
        "mode": "headless",
        "presentation_families": ["headless"],
        "required_capabilities": [],
    }
    base["definition_revision"] = definition_revision(base)
    verified = _load(tmp_path, catalog, root, keys)
    profile["mode"] = "headless"
    profile["catalog_revision"] = verified.revision
    profile["base"]["definition_revision"] = base["definition_revision"]
    profile["shell"] = None

    binding = compose_runtime_profile(profile, verified, security_epoch=3)

    assert binding.shell_artifact_path is None
    assert binding.local_auth_protocol is None
    assert binding.profile_lock["shell"] is None
    assert binding.profile_lock["content_projections"] == []
    assert binding.profile_lock["effective_set"] == [
        {
            "role": "base",
            "identity": "defaults-basepack",
            "artifact_digest": base["artifact_digest"],
        }
    ]
