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
from ecosystem.defaultspack.defaultspack.http_dynamic_targets import (
    defaultspack_dynamic_capability_targets,
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


def test_workflow_authoring_dynamic_targets_admit_only_the_finite_v4_payloads() -> None:
    """The generic bridge exposes authoring inputs without reviving legacy routes."""

    binding = FrontendContractBinding(
        method="POST",
        path="/api/ui/capability/invoke",
        presentation="capability_result",
        targets=(),
    )
    operation_ids = (
        "definition.list",
        "definition.get",
        "definition.create",
        "definition.update",
        "definition.delete",
        "definition.validate",
        "definition.publish",
        "definition.archive",
        "definition.compile-preview",
        "operation.palette",
        "run.create",
        "run.get",
        "run.observe",
        "run.advance",
        "run.resume",
        "run.cancel",
        "run.step.execute",
        "run.step.resume",
        "run.step.retry",
        "run.pause",
        "run.reconcile-recovery",
    )
    catalog = {
        "packs": [{
            "pack_id": "tobkiri_workflow_pack",
            "artifact_digest": "sha256:" + "a" * 64,
            "pack_artifact_digest": "sha256:" + "b" * 64,
            "enabled": True,
            "approved": True,
            "operations": [{
                "invokable": True,
                "contract_id": "tobkiri.workflow.v4",
                "operation_id": operation_id,
                "provider_id": "tobkiri.workflow.provider",
                "function_id": "tobkiri.workflow.provider",
            } for operation_id in operation_ids],
        }],
    }

    targets = defaultspack_dynamic_capability_targets(binding, catalog=catalog)
    allowed = {target.operation_id: target.allowed_payload_keys for target in targets}

    assert allowed == {
        "definition.list": frozenset(),
        "definition.get": frozenset({"definition_id"}),
        "definition.create": frozenset({"definition_id", "document"}),
        "definition.update": frozenset({"definition_id", "document", "if_match"}),
        "definition.delete": frozenset({"definition_id", "if_match"}),
        "definition.validate": frozenset({"document"}),
        "definition.publish": frozenset({"definition_id", "if_match"}),
        "definition.archive": frozenset({"definition_id", "if_match"}),
        "definition.compile-preview": frozenset({"document"}),
        "operation.palette": frozenset(),
        # Every user-facing run operation admits exactly its provider keys.
        "run.create": frozenset(
            {"definition_id", "revision_digest", "inputs", "occurrence_id", "run_id"}
        ),
        "run.get": frozenset({"run_id"}),
        "run.observe": frozenset({"run_id"}),
        "run.advance": frozenset({"run_id"}),
        "run.resume": frozenset({"run_id"}),
        "run.cancel": frozenset({"run_id"}),
        "run.step.execute": frozenset({"run_id", "step_id"}),
        "run.step.resume": frozenset({"run_id", "step_id"}),
        "run.step.retry": frozenset({"run_id", "step_id"}),
        "run.pause": frozenset({"run_id"}),
        "run.reconcile-recovery": frozenset({"run_id"}),
    }
    assert all(target.contribution_id.startswith("pack.tobkiri_workflow_pack.") for target in targets)


def test_workflow_stop_projects_to_exact_dynamic_contribution() -> None:
    """Launcher resolves pack.<pack_id>.run.stop -> stop contract/principal.

    The dynamic capability projection rewrites contribution ids to
    ``pack.<pack_id>.<operation_id>``; the Run button's stop action must
    resolve to exactly this contribution carrying the stop contract, the
    verified stop Function principal, and only the ``run_id`` payload key.
    """

    binding = FrontendContractBinding(
        method="POST",
        path="/api/ui/capability/invoke",
        presentation="capability_result",
        targets=(),
    )
    catalog = {
        "packs": [{
            "pack_id": "tobkiri_workflow_pack",
            "artifact_digest": "sha256:" + "a" * 64,
            "pack_artifact_digest": "sha256:" + "b" * 64,
            "enabled": True,
            "approved": True,
            "operations": [
                {
                    "invokable": True,
                    "contract_id": "tobkiri.workflow.v4",
                    "operation_id": "run.step.execute",
                    "provider_id": "tobkiri.workflow.provider",
                    "function_id": "tobkiri.workflow.provider",
                },
                {
                    "invokable": True,
                    "contract_id": "tobkiri.workflow.stop.v4",
                    "operation_id": "run.stop",
                    "provider_id": "tobkiri.workflow.stop.provider",
                    "function_id": "tobkiri.workflow.stop.provider",
                },
            ],
        }],
    }
    targets = {
        target.contribution_id: target
        for target in defaultspack_dynamic_capability_targets(
            binding, catalog=catalog
        )
    }
    stop = targets["pack.tobkiri_workflow_pack.run.stop"]
    assert stop.contract_id == "tobkiri.workflow.stop.v4"
    assert stop.operation_id == "run.stop"
    assert stop.provider_id == "tobkiri.workflow.stop.provider"
    assert stop.function_id == "tobkiri.workflow.stop.provider"
    assert stop.allowed_payload_keys == frozenset({"run_id"})
    # The execute principal projects to a separate contribution identity.
    execute = targets["pack.tobkiri_workflow_pack.run.step.execute"]
    assert execute.contract_id == "tobkiri.workflow.v4"
    assert execute.provider_id == "tobkiri.workflow.provider"


def test_packvm_acceptance_bridge_has_only_finite_qa_payload_keys() -> None:
    binding = FrontendContractBinding(
        method="POST",
        path="/api/ui/capability/invoke",
        presentation="capability_result",
        targets=(),
    )
    prefix = "tobkiri_packvm_sandbox_qa_pack."
    operation_ids = (
        "probe_isolation",
        "stdin_overflow",
        "stdout_overflow",
        "stderr_overflow",
        "deadline_hold",
        "cancel_hold",
        "abnormal_exit",
    )
    catalog = {
        "packs": [{
            "pack_id": "tobkiri_packvm_sandbox_qa_pack",
            "artifact_digest": "sha256:" + "a" * 64,
            "pack_artifact_digest": "sha256:" + "b" * 64,
            "enabled": True,
            "approved": True,
            "operations": [{
                "invokable": True,
                "contract_id": "tobkiri.acceptance.packvm.sandbox.v1",
                "operation_id": prefix + operation_id,
                "provider_id": "tobkiri.packvm.sandbox-acceptance.provider",
            } for operation_id in (*operation_ids, "not_admitted")],
        }],
    }

    targets = defaultspack_dynamic_capability_targets(binding, catalog=catalog)
    allowed = {target.operation_id: target.allowed_payload_keys for target in targets}

    assert allowed[prefix + "stdin_overflow"] == frozenset({"nonce", "fill"})
    assert allowed[prefix + "probe_isolation"] == frozenset({"nonce"})
    assert allowed[prefix + "not_admitted"] == frozenset()
    assert all(
        keys <= {"nonce", "fill"}
        for operation_id, keys in allowed.items()
        if operation_id.startswith(prefix)
    )


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


@pytest.mark.parametrize("pack_digest", [None, "", "sha256:" + "b" * 64])
def test_dynamic_targets_require_active_pack_artifact_not_admission_digest(pack_digest):
    binding = FrontendContractBinding(
        method="POST", path="/api/ui/capability/invoke",
        presentation="capability_result", targets=(),
    )
    pack = {
        "pack_id": "tobkiri_workflow_pack", "artifact_digest": "sha256:" + "a" * 64,
        "enabled": True, "approved": True,
        "operations": [{"invokable": True, "contract_id": "tobkiri.workflow.v4",
                        "operation_id": "definition.list", "provider_id": "tobkiri.workflow.provider",
                        "function_id": "tobkiri.workflow.provider"}],
    }
    if pack_digest is not None:
        pack["pack_artifact_digest"] = pack_digest
    targets = defaultspack_dynamic_capability_targets(binding, catalog={"packs": [pack]})
    if not pack_digest:
        assert targets == ()
        return
    assert len(targets) == 1
    assert targets[0].artifact_digest == pack_digest
    pack["artifact_digest"] = "sha256:" + "c" * 64
    assert defaultspack_dynamic_capability_targets(binding, catalog={"packs": [pack]}) == targets
    for gate in ("enabled", "approved"):
        assert defaultspack_dynamic_capability_targets(
            binding, catalog={"packs": [{**pack, gate: False}]},
        ) == ()
    pack["operations"][0]["invokable"] = False
    assert defaultspack_dynamic_capability_targets(binding, catalog={"packs": [pack]}) == ()


@pytest.mark.parametrize("fault", [None, "artifact", "profile", "activation", "plan", "duplicate", "unavailable"])
def test_active_pack_digest_keeps_host_identity_and_readiness_fences(fault):
    from core_runtime.global_contracts.capability_capture import capture_capability_binding_snapshot

    digest = "sha256:" + "b" * 64
    metadata = {
        "provider_id": "tobkiri.workflow.provider", "function_id": "tobkiri.workflow.provider",
        "operation_id": "definition.list", "profile_id": "defaults",
        "profile_revision": "revision", "activation_id": "activation", "plan_digest": "plan",
        "artifact_digest": digest,
    }
    field = {"artifact": "artifact_digest", "profile": "profile_revision",
             "activation": "activation_id", "plan": "plan_digest"}.get(fault)
    if field:
        metadata[field] = "wrong"

    def ready(*_args):
        if fault == "unavailable":
            raise RuntimeError("backend unavailable")

    session = SimpleNamespace(
        profile_id="defaults", profile_revision="revision", activation_id="activation", plan_digest="plan",
        provider_metadata=lambda _contract: [metadata] * (2 if fault == "duplicate" else 1),
        assert_operation_ready=ready,
    )
    binding = FrontendContractBinding(
        method="POST", path="/api/ui/capability/invoke", presentation="capability_result", targets=(),
    )
    catalog = {"packs": [{
        "pack_id": "tobkiri_workflow_pack", "artifact_digest": "sha256:" + "a" * 64,
        "pack_artifact_digest": digest, "enabled": True, "approved": True,
        "operations": [{"invokable": True, "contract_id": "tobkiri.workflow.v4",
                        "operation_id": "definition.list", "provider_id": "tobkiri.workflow.provider",
                        "function_id": "tobkiri.workflow.provider"}],
    }]}
    snapshot = capture_capability_binding_snapshot(
        binding, session=session, catalog=catalog,
        dynamic_target_factory=defaultspack_dynamic_capability_targets,
    )
    assert len(snapshot.targets) == (1 if fault is None else 0)
    if fault is None:
        assert snapshot.targets[0].artifact_digest == digest


@pytest.mark.parametrize("different", [None, "contract_id", "operation_id", "provider_id", "function_id"])
def test_dynamic_alias_never_shadows_an_exact_explicit_contribution(different):
    from core_runtime.global_contracts.http_contract_dispatch import HTTPContractTarget

    identity = {"contract_id": "fixture.read.v1", "operation_id": "read",
                "provider_id": "fixture.provider", "function_id": "fixture.function"}
    explicit = HTTPContractTarget(
        contribution_id="app.explicit.read", owner_pack_id="app.fixture",
        allowed_payload_keys=frozenset({"pack_id"}), **identity,
    )
    binding = FrontendContractBinding(
        method="POST", path="/api/ui/capability/invoke",
        presentation="capability_result", targets=(explicit,),
    )
    dynamic = dict(identity)
    if different:
        dynamic[different] = "different"
    catalog = {"packs": [{"pack_id": "fixture", "enabled": True, "approved": True,
                           "pack_artifact_digest": "sha256:" + "a" * 64,
                           "operations": [{**dynamic, "invokable": True}]}]}
    targets = defaultspack_dynamic_capability_targets(binding, catalog=catalog)
    assert len(targets) == (0 if different is None else 1)
    assert binding.targets == (explicit,)
    assert explicit.contribution_id == "app.explicit.read"
    assert explicit.owner_pack_id == "app.fixture"
    assert explicit.allowed_payload_keys == frozenset({"pack_id"})


@pytest.mark.parametrize("fault", [None, "stale-artifact", "not-ready"])
def test_explicit_target_is_preserved_without_dynamic_fallback(fault):
    from core_runtime.global_contracts.capability_capture import capture_capability_binding_snapshot
    from core_runtime.global_contracts.http_contract_dispatch import HTTPContractTarget

    digest = "sha256:" + "a" * 64
    identity = {"contract_id": "fixture.read.v1", "operation_id": "read",
                "provider_id": "fixture.provider", "function_id": "fixture.function"}
    target = HTTPContractTarget(
        contribution_id="app.explicit.read", owner_pack_id="app.fixture",
        allowed_payload_keys=frozenset({"pack_id"}), artifact_digest=(
            "sha256:" + "b" * 64 if fault == "stale-artifact" else digest), **identity,
    )
    binding = FrontendContractBinding(method="POST", path="/api/ui/capability/invoke",
                                     presentation="capability_result", targets=(target,))
    def ready(*_args):
        if fault == "not-ready":
            raise RuntimeError("not ready")
    session = SimpleNamespace(**CONTEXT, assert_operation_ready=ready,
                              provider_metadata=lambda _: [{**identity, **CONTEXT,
                                                            "artifact_digest": digest}])
    catalog = {"packs": [{"pack_id": "fixture", "enabled": True, "approved": True,
                           "pack_artifact_digest": digest,
                           "operations": [{**identity, "invokable": True}]}]}
    snapshot = capture_capability_binding_snapshot(
        binding, session=session, catalog=catalog,
        dynamic_target_factory=defaultspack_dynamic_capability_targets,
    )
    assert snapshot.targets == ((target,) if fault is None else ())
