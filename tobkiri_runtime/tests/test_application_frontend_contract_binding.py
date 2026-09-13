"""Regression coverage for Application-owned frontend contract maps."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from core_runtime.global_contracts.http_contract_dispatch import (
    HTTPContractRouteError as ContractRouteError,
    HTTPContractBinding as FrontendContractBinding,
    contract_binding_map,
    contract_route_prefix,
    is_contract_route_path,
    resolve_contract_route,
)
from ecosystem.defaultspack.defaultspack.frontend_contract_loader import (
    frontend_contract_map_artifact,
    load_frontend_contract_bindings,
    resolve_frontend_contract_map_path,
)
from tobkiri_protocol.canonical import canonical_digest


pytestmark = pytest.mark.contract


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
DEFAULTSPACK_ROOT = RUNTIME_ROOT / "ecosystem" / "defaultspack"
if str(DEFAULTSPACK_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))
CONTEXT = {
    "profile_id": "profile-a",
    "profile_revision": "sha256:" + "1" * 64,
    "activation_id": "activation:profile-a-1",
    "plan_digest": "sha256:" + "2" * 64,
}


def _map_document(application_id: str) -> dict[str, object]:
    return {
        "schema": "io.tobkiri.frontend-contract-map.v4",
        "pack_id": application_id,
        "owner": application_id,
        "application_id": application_id,
        **CONTEXT,
        "routes": [
            {
                "method": "GET",
                "path": "/api/application/health",
                "presentation": "broker_result",
                "targets": [
                    {
                        "contribution_id": "application.health",
                        "contract_id": "application.health.v1",
                        "operation_id": "health.read",
                        "provider_id": f"{application_id}.provider",
                        "function_id": f"{application_id}.provider",
                        "allowed_payload_keys": [],
                    }
                ],
            }
        ],
    }


def _write_application_map(
    root: Path,
    application_id: str = "application.b",
    *,
    artifact_path: str | None = None,
    document: dict[str, object] | None = None,
) -> tuple[Path, dict[str, object]]:
    """Write a signed-map-shaped fixture and its selected Application manifest."""

    relative = artifact_path or f"{application_id}/frontend_contract_map.v4.json"
    map_path = root.joinpath(*Path(relative).parts)
    map_path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(
        document or _map_document(application_id),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    map_path.write_bytes(raw)
    manifest: dict[str, object] = {
        "pack": {"id": application_id, "kind": "application"},
        "artifacts": [
            {
                "path": relative,
                "kind": "asset",
                "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            }
        ],
    }
    return map_path, manifest


class _CapturedSession:
    """Minimal live identity used by canonical route resolution tests."""

    profile_id = CONTEXT["profile_id"]
    profile_revision = CONTEXT["profile_revision"]
    activation_id = CONTEXT["activation_id"]
    plan_digest = CONTEXT["plan_digest"]


def test_application_b_map_path_identity_and_route_are_selected_generically(
    tmp_path: Path,
) -> None:
    map_path, manifest = _write_application_map(tmp_path)
    bindings = load_frontend_contract_bindings(
        map_path,
        manifest,
        artifact_root=tmp_path,
        **CONTEXT,
    )

    binding = bindings[0]
    assert binding.application_id == "application.b"
    assert binding.route_namespace == "application.b"
    assert binding.artifact_path == "application.b/frontend_contract_map.v4.json"
    assert binding.artifact_digest == manifest["artifacts"][0]["digest"]
    assert binding.targets[0].owner_pack_id == "application.b"

    class Server:
        _contract_routes = contract_binding_map(bindings)
        _dispatch_session = _CapturedSession()

    operation = contract_route_prefix("application.b") + quote(
        "GET /api/application/health",
        safe="",
    )
    assert is_contract_route_path(operation)
    resolved = resolve_contract_route(Server(), "GET", operation)
    assert resolved is not None
    assert resolved.path == "/api/application/health"


def test_desktop_resolver_selects_the_application_from_the_verified_plan() -> None:
    from defaultspack import desktop_app

    application_id = "application.b"
    artifact_digest = "sha256:" + "a" * 64
    executable_digest = "sha256:" + "b" * 64
    application = {
        "pack": {
            "id": application_id,
            "kind": "application",
            "artifact_digest": artifact_digest,
        },
        "artifacts": [
            {
                "path": "application.b/frontend_contract_map.v4.json",
                "kind": "asset",
                "digest": "sha256:" + "c" * 64,
            },
            {
                "path": "application.b/bin/application",
                "kind": "executable",
                "entrypoint_digest": executable_digest,
            },
        ],
    }
    application_binding = {
        "pack_id": application_id,
        "artifact_digest": artifact_digest,
        "executable_artifact_digest": executable_digest,
        "definition_digest": canonical_digest(application),
    }
    active = SimpleNamespace(
        resolved=SimpleNamespace(
            plan={
                "application": application_binding,
                "effective_set": [
                    {
                        "identity": application_id,
                        "role": "pack",
                        "artifact_digest": artifact_digest,
                    }
                ],
            },
            lock={"application": application_binding},
        )
    )
    catalog = SimpleNamespace(packs={application_id: application})

    assert desktop_app._active_application_manifest(catalog, active) is application


def test_defaults_map_still_uses_the_same_manifest_route_loader() -> None:
    map_path = (
        RUNTIME_ROOT
        / "ecosystem"
        / "defaultspack"
        / "defaultspack"
        / "frontend_contract_map.v4.json"
    )
    manifest_path = (
        RUNTIME_ROOT
        / "ecosystem"
        / "defaultspack"
        / "v4"
        / "packs"
        / "runtime.tauri.application.default.pack.v4.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bindings = load_frontend_contract_bindings(
        map_path,
        manifest,
        artifact_root=RUNTIME_ROOT / "ecosystem" / "defaultspack",
        **CONTEXT,
    )

    assert bindings[0].application_id == "runtime.tauri.application.default"
    assert bindings[0].route_namespace == "defaultspack"


def test_missing_application_map_artifact_is_unavailable(tmp_path: Path) -> None:
    manifest = {
        "pack": {"id": "application.b", "kind": "application"},
        "artifacts": [],
    }

    with pytest.raises(ContractRouteError) as error:
        frontend_contract_map_artifact(manifest)
    assert error.value.code == "CONTRACT_MAP_UNAVAILABLE"


@pytest.mark.parametrize(
    "mutation,expected_code",
    [
        ("foreign_identity", "CONTRACT_MAP_INVALID"),
        ("digest", "CONTRACT_MAP_STALE"),
    ],
)
def test_foreign_application_or_digest_mismatch_fails_closed(
    tmp_path: Path,
    mutation: str,
    expected_code: str,
) -> None:
    map_path, manifest = _write_application_map(tmp_path)
    if mutation == "foreign_identity":
        document = _map_document("application.c")
        map_path, manifest = _write_application_map(
            tmp_path,
            document=document,
        )
    else:
        manifest["artifacts"][0]["digest"] = "sha256:" + "0" * 64

    with pytest.raises(ContractRouteError) as error:
        load_frontend_contract_bindings(map_path, manifest)
    assert error.value.code == expected_code


def test_cross_profile_application_map_fails_closed(tmp_path: Path) -> None:
    document = _map_document("application.b")
    document["profile_id"] = "profile-b"
    map_path, manifest = _write_application_map(tmp_path, document=document)

    with pytest.raises(ContractRouteError) as error:
        load_frontend_contract_bindings(
            map_path,
            manifest,
            **CONTEXT,
        )
    assert error.value.code == "CONTRACT_MAP_STALE"


def test_application_artifact_path_traversal_is_rejected(tmp_path: Path) -> None:
    manifest = {
        "pack": {"id": "application.b", "kind": "application"},
        "artifacts": [
            {
                "path": "../outside/frontend_contract_map.v4.json",
                "kind": "asset",
                "digest": "sha256:" + "0" * 64,
            }
        ],
    }
    with pytest.raises(ContractRouteError) as error:
        frontend_contract_map_artifact(manifest)
    assert error.value.code == "CONTRACT_MAP_INVALID"
    with pytest.raises(ContractRouteError):
        resolve_frontend_contract_map_path(manifest, tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlink support")
def test_application_artifact_symlink_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    map_path, manifest = _write_application_map(outside)
    app_dir = tmp_path / "application.b"
    app_dir.symlink_to(outside, target_is_directory=True)
    linked_path = app_dir / map_path.name

    with pytest.raises(ContractRouteError) as error:
        load_frontend_contract_bindings(
            linked_path,
            manifest,
            artifact_root=tmp_path,
        )
    assert error.value.code == "CONTRACT_MAP_INVALID"


def test_activation_rotation_rejects_an_old_binding() -> None:
    class Server:
        _dispatch_session = type(
            "RotatedSession",
            (),
            {
                "profile_id": "profile-a",
                "profile_revision": "sha256:" + "3" * 64,
                "activation_id": "activation:profile-a-2",
                "plan_digest": "sha256:" + "4" * 64,
            },
        )()

    stale_binding = FrontendContractBinding(
        method="GET",
        path="/api/application/health",
        presentation="broker_result",
        targets=(),
        application_id="application.b",
        route_namespace="application.b",
        profile_id=CONTEXT["profile_id"],
        profile_revision=CONTEXT["profile_revision"],
        activation_id=CONTEXT["activation_id"],
        plan_digest=CONTEXT["plan_digest"],
    )
    Server._contract_routes = {
        (
            stale_binding.method,
            stale_binding.path,
        ): stale_binding
    }
    operation = contract_route_prefix("application.b") + quote(
        "GET /api/application/health",
        safe="",
    )
    with pytest.raises(ContractRouteError) as error:
        resolve_contract_route(Server(), "GET", operation)
    assert error.value.code == "CONTRACT_MAP_STALE"


def test_invalid_contract_namespace_is_caught_before_legacy_dispatch() -> None:
    assert is_contract_route_path("/api/contracts/../not-a-contract")
    with pytest.raises(ContractRouteError) as error:
        resolve_contract_route(
            type("Server", (), {"_contract_routes": {}})(),
            "GET",
            "/api/contracts/../not-a-contract",
        )
    assert error.value.code == "CONTRACT_PACK_INVALID"


def test_unbound_frontend_binding_cannot_claim_a_contract_namespace() -> None:
    binding = FrontendContractBinding(
        method="GET",
        path="/api/application/health",
        presentation="broker_result",
        targets=(),
    )

    class Server:
        _contract_routes = {(binding.method, binding.path): binding}

    operation = contract_route_prefix("application.b") + quote(
        "GET /api/application/health",
        safe="",
    )
    with pytest.raises(ContractRouteError) as error:
        resolve_contract_route(Server(), "GET", operation)
    assert error.value.code == "CONTRACT_OPERATION_UNKNOWN"


def test_desktop_contract_context_requires_the_active_profile_record_graph() -> None:
    from defaultspack import desktop_app

    profile = {"profile_id": "profile-a", "display_name": "Profile A"}
    profile_revision = canonical_digest(profile)
    plan_without_digest = {
        "profile_id": "profile-a",
        "profile_revision": profile_revision,
        "effective_set": [],
    }
    plan_digest = canonical_digest(plan_without_digest)
    plan = {**plan_without_digest, "plan_digest": plan_digest}
    lock_without_digest = {
        "profile_id": "profile-a",
        "profile_revision": profile_revision,
        "plan_digest": plan_digest,
        "effective_set": [],
    }
    lock_digest = canonical_digest(lock_without_digest)
    lock = {**lock_without_digest, "lock_digest": lock_digest}
    activation = {
        "state": "active",
        "profile_id": "profile-a",
        "profile_revision": profile_revision,
        "activation_id": "activation:profile-a-1",
        "plan_digest": plan_digest,
        "lock_digest": lock_digest,
    }
    active = SimpleNamespace(
        resolved=SimpleNamespace(profile=profile, plan=plan, lock=lock),
        activation=activation,
    )

    assert desktop_app._active_profile_contract_context(active) == {
        "profile_id": "profile-a",
        "profile_revision": profile_revision,
        "activation_id": "activation:profile-a-1",
        "plan_digest": plan_digest,
    }

    activation["state"] = "superseded"
    with pytest.raises(RuntimeError, match="active Profile identity is stale"):
        desktop_app._active_profile_contract_context(active)
