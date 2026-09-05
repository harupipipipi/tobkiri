from __future__ import annotations

import copy
import hashlib
import json
import multiprocessing
import os
import shutil
import time
from dataclasses import replace
from pathlib import Path

import pytest

import ecosystem.defaultspack.domain.runtime_v4.service as runtime_service
from core_runtime.authority.v4 import AuthorityDenied, AuthorityStore
from ecosystem.defaultspack.domain.runtime_v4 import (
    ActivationLockTimeout,
    ActivationStore,
    BundleIntegrityError,
    BundledCatalog,
    ProfileResolutionDenied,
    project_runtime_launch_selector,
    resolve_default_profile,
)
from tobkiri_protocol.canonical import canonical_digest
from tests.conformance_support.packaged_profile import (
    load_packaged_profile_catalog,
    packaged_profile_bundle_root,
)

ROOT = Path(__file__).resolve().parent.parent
BUNDLE_ROOT = ROOT / "ecosystem" / "defaultspack" / "v4"
SNAPSHOT_DIGEST = "sha256:" + "9" * 64
AUTHORITY_BINDINGS = {
    "shell.tauri.default|defaultspack.conversation|conversation.turn.v1|complete": (
        "authority-ref:conversation.default"
    ),
    (
        "shell.tauri.pack-control|tobkiri.host.pack-control|"
        "tobkiri.host.pack-control.v4|catalog.read"
    ): "authority-ref:pack.catalog.default",
    (
        "defaultspack.conversation|rumi_file_inspect_pack.file-inspect.service|"
        "tobkiri.service.file.inspect.v1|rumi_file_inspect_pack.file-inspect"
    ): "authority-ref:file.inspect.default",
}
for _operation_id in (
    "pack.install",
    "dashboard.read",
    "approval.candidate",
    "approval.approve",
    "approval.revoke",
    "pack.enable",
    "pack.disable",
    "pack.status",
    "profile.reload",
    "runtime.restart",
):
    AUTHORITY_BINDINGS[
        "shell.tauri.pack-control|tobkiri.host.pack-control|"
        f"tobkiri.host.pack-control.v4|{_operation_id}"
    ] = f"authority-ref:pack-control.{_operation_id}"


def _catalog() -> BundledCatalog:
    return load_packaged_profile_catalog()


def _approved(catalog: BundledCatalog) -> set[str]:
    return {
        str(manifest["pack"]["artifact_digest"]) for manifest in catalog.packs.values()
    }


def _edge_key(edge: dict[str, object]) -> str:
    return "|".join(
        str(edge[field])
        for field in (
            "caller_function_id",
            "target_provider_id",
            "contract_id",
            "operation_id",
        )
    )


def _resolve(catalog: BundledCatalog | None = None):
    selected_catalog = catalog or _catalog()
    authority_bindings = dict(AUTHORITY_BINDINGS)
    for edge in selected_catalog.profiles["defaults"]["requested_edges"]:
        authority_bindings.setdefault(
            _edge_key(edge),
            "authority-ref:test."
            + canonical_digest(_edge_key(edge)).removeprefix("sha256:"),
        )
    return resolve_default_profile(
        selected_catalog,
        "defaults",
        approved_artifact_digests=_approved(selected_catalog),
        authority_snapshot_digest=SNAPSHOT_DIGEST,
        authority_bindings=authority_bindings,
        security_epoch=7,
    )


def test_projected_pack_requires_exact_materialization_catalog_pin() -> None:
    """A source-bound projection cannot fall back to its derivative digest."""

    catalog = _catalog()
    pack_id = "rumi_file_inspect_pack"
    executable = copy.deepcopy(catalog.executable_catalogs[pack_id])
    executable.pop("materialization_catalog_digest")
    executable["catalog_digest"] = canonical_digest(
        {key: value for key, value in executable.items() if key != "catalog_digest"}
    )
    executables = dict(catalog.executable_catalogs)
    executables[pack_id] = executable
    altered = replace(catalog, executable_catalogs=executables)

    with pytest.raises(
        ProfileResolutionDenied,
        match="materialization executable catalog digest",
    ):
        _resolve(altered)


def _authority(path: Path) -> AuthorityStore:
    store = AuthorityStore(path)
    while store.security_epoch < 7:
        store.advance_security_epoch("test fixture epoch")
    return store


def _activation_process(
    bundle_root: str,
    state_root: str,
    workspace_root: str,
    authority_path: str,
    activation_id: str,
    committed: multiprocessing.synchronize.Event | None,
    release: multiprocessing.synchronize.Event | None,
    results: multiprocessing.queues.Queue,
) -> None:
    authority = _authority(Path(authority_path))
    catalog = BundledCatalog.load(Path(bundle_root))

    def fault(stage: str) -> None:
        if stage == "after_authority_commit" and committed is not None:
            committed.set()
            assert release is not None
            if not release.wait(timeout=15):
                raise RuntimeError("activation interleave release timed out")

    try:
        activation = ActivationStore(
            Path(state_root),
            Path(workspace_root),
            profile_id="defaults",
            authority=authority,
            fault=fault,
        ).activate(
            _resolve(catalog),
            activation_id=activation_id,
            created_at="2026-08-10T00:00:00Z",
        )
        results.put(("ok", activation_id, activation["fencing_token"]))
    except Exception as exc:  # pragma: no cover - reported to the parent assertion
        results.put(("error", activation_id, type(exc).__name__, str(exc)))
    finally:
        authority.close()


def _cas_activation_process(
    bundle_root: str,
    state_root: str,
    workspace_root: str,
    authority_path: str,
    activation_id: str,
    expected_revision: str,
    expected_plan_digest: str,
    expected_activation_id: str,
    barrier: multiprocessing.synchronize.Barrier,
    results: multiprocessing.queues.Queue,
) -> None:
    authority = _authority(Path(authority_path))
    catalog = BundledCatalog.load(Path(bundle_root))
    try:
        if barrier.wait(timeout=15) < 0:  # pragma: no cover - defensive API guard
            raise RuntimeError("activation barrier failed")
        activation = ActivationStore(
            Path(state_root),
            Path(workspace_root),
            profile_id="defaults",
            authority=authority,
        ).activate(
            _resolve(catalog),
            activation_id=activation_id,
            created_at="2026-08-10T00:01:00Z",
            expected_predecessor_profile_revision=expected_revision,
            expected_predecessor_plan_digest=expected_plan_digest,
            expected_predecessor_activation_id=expected_activation_id,
        )
        results.put(("active", activation["activation_id"]))
    except ProfileResolutionDenied as error:
        state = "STALE_REVISION" if "predecessor is stale" in str(error) else "error"
        results.put((state, str(error)))
    except Exception as error:  # pragma: no cover - reported to the parent assertion
        results.put(("error", f"{type(error).__name__}: {error}"))
    finally:
        authority.close()


def test_bundle_is_protocol_v4_and_resolves_exact_dependency_closure() -> None:
    catalog = _catalog()
    resolved = _resolve(catalog)

    assert {
        "defaults-basepack",
        "defaultspack",
        "rumi_ai_gateway_pack",
        "rumi_ai_pipeline_pack",
        "rumi_ai_routing_pack",
        "rumi_ai_stream_pack",
        "rumi_ai_tool_bridge_pack",
        "rumi_ai_usage_pack",
        "rumi_command_protocol_pack",
        "rumi_file_inspect_pack",
        "rumi_git_publish_pack",
        "rumi_git_read_pack",
        "rumi_git_write_pack",
        "rumi_host_authority_bridge_pack",
        "rumi_model_catalog_pack",
        "rumi_model_registry_pack",
        "rumi_provider_adapters_pack",
        "rumi_provider_registry_pack",
        "rumi_shell_execute_pack",
        "rumi_shell_policy_pack",
        "rumi_workspace_mount_pack",
        "runtime.tauri.application.default",
        "dev.tauri.toolchain.default",
        "shell.cli.default",
        "shell.tauri.default",
        "tobkiri_host_pack_control",
    } <= set(catalog.packs)
    assert resolved.profile["profile_api_version"] == "io.tobkiri.profile.v5"
    assert resolved.profile["state"] == "resolved"
    assert resolved.profile["shell"]["provider_id"] == "shell.tauri.default"
    assert "shell.cli.default" not in {
        item["identity"] for item in resolved.lock["effective_set"]
    }
    assert resolved.profile["profile_authority_snapshot_digest"] == SNAPSHOT_DIGEST
    assert {item["pack_id"] for item in resolved.profile["packs"]} == {
        "defaultspack",
        "rumi_ai_gateway_pack",
        "rumi_ai_pipeline_pack",
        "rumi_ai_routing_pack",
        "rumi_ai_stream_pack",
        "rumi_ai_tool_bridge_pack",
        "rumi_ai_usage_pack",
        "rumi_command_protocol_pack",
        "rumi_file_inspect_pack",
        "rumi_git_publish_pack",
        "rumi_git_read_pack",
        "rumi_git_write_pack",
        "rumi_host_authority_bridge_pack",
        "rumi_model_catalog_pack",
        "rumi_model_registry_pack",
        "rumi_provider_adapters_pack",
        "rumi_provider_registry_pack",
        "rumi_shell_execute_pack",
        "rumi_shell_policy_pack",
        "rumi_workspace_mount_pack",
        "runtime.tauri.application.default",
        "tobkiri_host_pack_control",
    }
    roles = {item["pack_id"]: item["role"] for item in resolved.profile["packs"]}
    assert roles["runtime.tauri.application.default"] == "application"
    assert "dev.tauri.toolchain.default" not in {
        item["identity"] for item in resolved.lock["effective_set"]
    }
    assert [
        item["function_principal"]["function_id"] for item in resolved.plan["bindings"]
    ] == [
        "defaultspack.conversation",
        "rumi_ai_gateway_pack.ai-gateway.generate",
        "rumi_ai_gateway_pack.ai-gateway.stream",
        "rumi_ai_pipeline_pack.ai-pipeline.prepare",
        "rumi_ai_pipeline_pack.ai-pipeline.prepare",
        "rumi_provider_registry_pack.provider-registry.health",
        "rumi_provider_registry_pack.provider-registry.health",
        "rumi_model_catalog_pack.model-catalog.bundled",
        "rumi_model_catalog_pack.model-catalog.bundled",
        "rumi_model_registry_pack.model-registry.profile",
        "rumi_model_registry_pack.model-registry.profile",
        "rumi_ai_pipeline_pack.ai-pipeline.failover",
        "rumi_ai_pipeline_pack.ai-pipeline.failover",
        "rumi_provider_adapters_pack.provider.compatibility.generate",
        "rumi_provider_adapters_pack.provider.compatibility.stream",
        "rumi_ai_routing_pack.ai-routing.default",
        "rumi_ai_routing_pack.ai-routing.default",
        "rumi_ai_stream_pack.ai-stream.normalize",
        "rumi_ai_tool_bridge_pack.ai-tool-bridge.normalize",
        "rumi_ai_tool_bridge_pack.ai-tool-bridge.normalize",
        "rumi_ai_usage_pack.ai-usage.cost",
        "rumi_ai_usage_pack.ai-usage.cost",
        "rumi_provider_registry_pack.provider-registry.resource",
        "rumi_provider_registry_pack.provider-registry.resource",
        "tobkiri.host.pack-control",
        "tobkiri.host.pack-control",
        "tobkiri.host.pack-control",
        "tobkiri.host.pack-control",
        "tobkiri.host.pack-control",
        "tobkiri.host.pack-control",
        "tobkiri.host.pack-control",
        "tobkiri.host.pack-control",
        "tobkiri.host.pack-control",
        "tobkiri.host.control-presentation",
        "tobkiri.host.control-presentation",
        "tobkiri.host.control-presentation",
        "tobkiri.host.control-presentation",
        "tobkiri.host.control-presentation",
        "tobkiri.host.control-presentation",
        "tobkiri.host.control-presentation",
        "tobkiri.host.control-presentation",
        "tobkiri.host.control-presentation",
        "tobkiri.host.control-presentation",
        "tobkiri.host.control-presentation",
        "tobkiri.host.control-presentation",
        "tobkiri.host.pack-control",
        "tobkiri.host.pack-control",
        "rumi_file_inspect_pack.file-inspect.service",
        "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
        "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
        "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
        "rumi_host_authority_bridge_pack.host-authority.interactive-approval",
        "rumi_command_protocol_pack.high-risk-command.service",
        "rumi_host_authority_bridge_pack.host-authority.interactive-effect",
        "rumi_shell_execute_pack.shell-prepare.service",
        "rumi_shell_execute_pack.shell-execute.service",
        "rumi_git_write_pack.git-commit-prepare.service",
        "rumi_git_write_pack.git-commit.service",
        "rumi_git_write_pack.git-restore-prepare.service",
        "rumi_git_write_pack.git-restore.service",
        "rumi_git_write_pack.git-apply-patch-prepare.service",
        "rumi_git_write_pack.git-apply-patch.service",
        "rumi_git_publish_pack.git-push-prepare.service",
        "rumi_git_publish_pack.git-publish.service",
    ]
    assert resolved.lock["plan_digest"] == resolved.plan["plan_digest"]


def test_interactive_only_edge_is_compiled_into_hardened_defaults() -> None:
    """Interactive effects are explicit and remain signed plan bindings."""

    catalog = _catalog()
    baseline = _resolve(catalog)
    assert any(
        edge.get("authority_mode") == "interactive_only"
        for edge in baseline.profile["requested_edges"]
    )
    assert any(
        binding.get("authority_mode") == "interactive_only"
        for binding in baseline.plan["bindings"]
    )

    source = copy.deepcopy(catalog.profiles["defaults"])
    selected = source["requested_edges"][0]
    selected["authority_mode"] = "interactive_only"
    altered = replace(
        catalog,
        profiles={**catalog.profiles, "defaults": source},
    )

    resolved = _resolve(altered)
    edge = next(
        item
        for item in resolved.profile["requested_edges"]
        if _edge_key(item) == _edge_key(selected)
    )
    binding = next(
        item
        for item in resolved.plan["bindings"]
        if (
            item["caller_function_id"],
            item["contract_id"],
            item["operation_id"],
        )
        == (
            selected["caller_function_id"],
            selected["contract_id"],
            selected["operation_id"],
        )
    )
    assert edge["authority_mode"] == "interactive_only"
    assert binding["authority_mode"] == "interactive_only"
    assert resolved.plan["requested_edges_digest"] == canonical_digest(
        resolved.profile["requested_edges"]
    )


def test_profile_compiler_rejects_unknown_requested_edge_authority_mode() -> None:
    catalog = _catalog()
    source = copy.deepcopy(catalog.profiles["defaults"])
    source["requested_edges"][0]["authority_mode"] = "ambient"
    altered = replace(
        catalog,
        profiles={**catalog.profiles, "defaults": source},
    )

    with pytest.raises(ProfileResolutionDenied, match="authority mode"):
        _resolve(altered)


def test_lock_plan_and_activation_bind_the_complete_canonical_definition(
    tmp_path: Path,
) -> None:
    catalog = _catalog()
    resolved = _resolve(catalog)
    expected_bundle_digest = (
        "sha256:"
        + hashlib.sha256((catalog.root / "bundle.lock.json").read_bytes()).hexdigest()
    )
    application = catalog.packs["runtime.tauri.application.default"]

    assert resolved.lock["profile_definition_digest"] == canonical_digest(
        catalog.profiles["defaults"]
    )
    assert resolved.lock["bundle_digest"] == expected_bundle_digest
    assert resolved.lock["application"] == {
        "pack_id": "runtime.tauri.application.default",
        "artifact_digest": application["pack"]["artifact_digest"],
        "executable_artifact_digest": resolved.profile["shell"][
            "executable_artifact_digest"
        ],
        "definition_digest": canonical_digest(application),
    }
    selected_variant = catalog.shells["shell.tauri.default"]["launch"]["variants"][0]
    assert resolved.plan["launch_contribution"] == {
        "provider_id": "runtime.tauri.application.default",
        "contract_id": "runtime.tauri.application.v1",
        "operation_id": "launch",
        "platform": selected_variant["platform"],
        "architecture": selected_variant["architecture"],
        "artifact_digest": selected_variant["artifact_digest"],
        "relative_path": selected_variant["relative_path"],
        "entrypoint": selected_variant["entrypoint"],
    }
    assert "launch_contribution" not in resolved.lock
    for field in (
        "profile_definition_digest",
        "catalog_revision",
        "bundle_digest",
        "application",
        "effective_set",
        "requested_edges_digest",
        "constraints_digest",
        "closure_digest",
        "provenance_digest",
    ):
        assert resolved.plan[field] == resolved.lock[field]
    assert resolved.plan["closure_digest"] == canonical_digest(
        {
            "effective_set": resolved.plan["effective_set"],
            "content_projections": resolved.plan["content_projections"],
        }
    )
    assert resolved.plan["requested_edges_digest"] == canonical_digest(
        resolved.profile["requested_edges"]
    )
    assert resolved.plan["provenance_digest"] == canonical_digest(
        resolved.profile["provenance"]
    )
    edge_by_key = {
        _edge_key(edge): edge for edge in resolved.profile["requested_edges"]
    }
    assert len(resolved.plan["bindings"]) == len(edge_by_key)
    for binding in resolved.plan["bindings"]:
        key = "|".join(
            (
                binding["caller_function_id"],
                binding["function_principal"]["function_id"],
                binding["contract_id"],
                binding["operation_id"],
            )
        )
        edge = edge_by_key[key]
        assert binding["authority_reference"] == edge["authority_reference"]
        assert binding["requested_scope_digest"] == canonical_digest(
            edge["requested_scope_template"]
        )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = _authority(tmp_path / "authority.sqlite3")
    store = ActivationStore(
        tmp_path / "state", workspace, profile_id="defaults", authority=authority
    )
    activation = store.activate(
        resolved,
        activation_id="activation:defaults-complete",
        created_at="2026-08-10T00:00:00Z",
    )
    assert activation["profile_revision"] == resolved.plan["profile_revision"]
    assert activation["catalog_revision"] == resolved.plan["catalog_revision"]
    assert activation["bundle_digest"] == expected_bundle_digest
    assert activation["lock_digest"] == resolved.lock["lock_digest"]
    assert activation["closure_digest"] == resolved.plan["closure_digest"]
    selector = project_runtime_launch_selector(store.load_active_snapshot())
    assert selector == {
        "selector_api_version": "io.tobkiri.runtime-launch-selector.v1",
        "profile_id": resolved.plan["profile_id"],
        "profile_revision": resolved.plan["profile_revision"],
        "activation_id": activation["activation_id"],
        "plan_digest": resolved.plan["plan_digest"],
        "launch_contribution": resolved.plan["launch_contribution"],
    }


def test_application_launch_contribution_must_be_unique() -> None:
    catalog = _catalog()
    application = copy.deepcopy(catalog.packs["runtime.tauri.application.default"])
    application["functions"].append(copy.deepcopy(application["functions"][0]))
    ambiguous = replace(
        catalog,
        packs={**catalog.packs, "runtime.tauri.application.default": application},
    )

    with pytest.raises(
        ProfileResolutionDenied, match="launch contribution is ambiguous"
    ):
        _resolve(ambiguous)


def test_runtime_launch_selector_rejects_stale_active_identity(tmp_path: Path) -> None:
    resolved = _resolve()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = _authority(tmp_path / "authority.sqlite3")
    store = ActivationStore(
        tmp_path / "state", workspace, profile_id="defaults", authority=authority
    )
    store.activate(
        resolved,
        activation_id="activation:selector-stale",
        created_at="2026-08-10T00:00:00Z",
    )
    active = store.load_active_snapshot()
    stale_activation = {
        **active.activation,
        "plan_digest": "sha256:" + "0" * 64,
    }

    with pytest.raises(ProfileResolutionDenied, match="activation is stale"):
        project_runtime_launch_selector(replace(active, activation=stale_activation))

    missing_plan = dict(active.resolved.plan)
    missing_plan.pop("launch_contribution")
    missing_plan["plan_digest"] = canonical_digest(
        {key: value for key, value in missing_plan.items() if key != "plan_digest"}
    )
    missing_lock = {**active.resolved.lock, "plan_digest": missing_plan["plan_digest"]}
    missing_lock["lock_digest"] = canonical_digest(
        {key: value for key, value in missing_lock.items() if key != "lock_digest"}
    )
    missing = replace(
        active,
        resolved=replace(active.resolved, plan=missing_plan, lock=missing_lock),
        activation={
            **active.activation,
            "plan_digest": missing_plan["plan_digest"],
            "lock_digest": missing_lock["lock_digest"],
        },
    )
    with pytest.raises(ProfileResolutionDenied, match="contribution is unavailable"):
        project_runtime_launch_selector(missing)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (
            lambda profile: profile["base"].update(
                artifact_digest="sha256:" + "0" * 64
            ),
            "Base artifact pin",
        ),
        (
            lambda profile: profile["base"].update(
                definition_revision="sha256:" + "0" * 64
            ),
            "Base definition pin",
        ),
        (
            lambda profile: profile["shell"].update(pack_id="shell.cli.default"),
            "Shell Pack binding",
        ),
        (
            lambda profile: profile["shell"].update(contract_id="conversation.turn.v1"),
            "Shell Contract binding",
        ),
        (
            lambda profile: profile["packs"][0].update(
                artifact_digest="sha256:" + "0" * 64
            ),
            "Pack artifact pin",
        ),
        (
            lambda profile: profile.update(
                profile_authority_snapshot_digest="sha256:" + "0" * 64
            ),
            "must not contain resolved Authority state",
        ),
    ),
)
def test_named_profile_source_exact_pins_fail_closed(
    mutation,
    message: str,
) -> None:
    """A named Profile candidate may narrow null pins, never rewrite exact ones."""

    catalog = _catalog()
    profile = copy.deepcopy(catalog.profiles["defaults"])
    mutation(profile)
    tampered = replace(catalog, profiles={"defaults": profile})

    with pytest.raises(ProfileResolutionDenied, match=message):
        _resolve(tampered)


def test_base_shell_compatibility_and_exact_dependency_pins_fail_closed() -> None:
    catalog = _catalog()
    base = copy.deepcopy(catalog.bases["defaults-basepack"])
    base["shell_requirements"]["presentation_families"] = ["terminal"]
    incompatible = replace(
        catalog,
        bases={**catalog.bases, "defaults-basepack": base},
    )
    with pytest.raises(ProfileResolutionDenied, match="presentation family"):
        _resolve(incompatible)

    base = copy.deepcopy(catalog.bases["defaults-basepack"])
    base["dependencies"] = [
        {
            "pack_id": "rumi_host_authority_bridge_pack",
            "artifact_digest": "sha256:" + "0" * 64,
        }
    ]
    stale_dependency = replace(
        catalog,
        bases={**catalog.bases, "defaults-basepack": base},
    )
    with pytest.raises(ProfileResolutionDenied, match="Base dependency artifact"):
        _resolve(stale_dependency)


def test_duplicate_requested_edge_is_not_silently_reauthorized() -> None:
    catalog = _catalog()
    profile = copy.deepcopy(catalog.profiles["defaults"])
    profile["requested_edges"].append(copy.deepcopy(profile["requested_edges"][0]))
    duplicate = replace(catalog, profiles={"defaults": profile})

    with pytest.raises(ProfileResolutionDenied, match="duplicate requested edge"):
        _resolve(duplicate)


def test_self_consistent_plan_tamper_cannot_change_authority_binding(
    tmp_path: Path,
) -> None:
    resolved = _resolve()
    profile = copy.deepcopy(resolved.profile)
    plan = copy.deepcopy(resolved.plan)
    lock = copy.deepcopy(resolved.lock)
    plan["bindings"][0]["authority_reference"] = "authority-ref:tampered.reference"
    plan["plan_digest"] = canonical_digest(
        {key: value for key, value in plan.items() if key != "plan_digest"}
    )
    lock["plan_digest"] = plan["plan_digest"]
    lock["lock_digest"] = canonical_digest(
        {key: value for key, value in lock.items() if key != "lock_digest"}
    )
    tampered = type(resolved)(profile=profile, lock=lock, plan=plan)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = _authority(tmp_path / "authority.sqlite3")
    store = ActivationStore(
        tmp_path / "state", workspace, profile_id="defaults", authority=authority
    )

    with pytest.raises(ProfileResolutionDenied, match="Authority binding is stale"):
        store.activate(
            tampered,
            activation_id="activation:defaults-tampered",
            created_at="2026-08-10T00:00:00Z",
        )
    assert authority.incomplete_activation_reservations("defaults") == ()


def test_unreferenced_caller_cannot_piggyback_on_shared_provider_operation() -> None:
    catalog = _catalog()
    profile = copy.deepcopy(catalog.profiles["defaults"])
    shared_edge = next(
        edge
        for edge in profile["requested_edges"]
        if edge["operation_id"]
        == "rumi_model_catalog_pack.bundled-model-catalog.generate"
    )
    unreferenced_edge = {
        **shared_edge,
        "caller_function_id": "unreferenced.ai.consumer",
    }
    profile["requested_edges"].append(unreferenced_edge)
    profiles = dict(catalog.profiles)
    profiles["defaults"] = profile
    tampered = replace(catalog, profiles=profiles)
    authority_bindings = dict(AUTHORITY_BINDINGS)
    for edge in catalog.profiles["defaults"]["requested_edges"]:
        authority_bindings.setdefault(
            _edge_key(edge),
            "authority-ref:test."
            + canonical_digest(_edge_key(edge)).removeprefix("sha256:"),
        )
    authority_bindings[_edge_key(unreferenced_edge)] = (
        "authority-ref:test.unreferenced-caller"
    )

    with pytest.raises(
        ProfileResolutionDenied,
        match="caller is not in the selected Profile closure",
    ):
        resolve_default_profile(
            tampered,
            "defaults",
            approved_artifact_digests=_approved(tampered),
            authority_snapshot_digest=SNAPSHOT_DIGEST,
            authority_bindings=authority_bindings,
            security_epoch=7,
        )


def test_duplicate_pack_and_legacy_route_authorities_are_absent() -> None:
    from ecosystem.defaultspack.domain.function_runtime.compat_aliases import (
        compatibility_alias_allowed,
    )
    from ecosystem.defaultspack.transport.registry import canonical_http_route_specs

    defaultspack_root = ROOT / "ecosystem" / "defaultspack"
    defaults_root = ROOT / "ecosystem" / "defaults"
    assert {path.name for path in defaults_root.iterdir()} == {
        "artifact-index.v4.json",
        "contracts.v4.json",
        "executables.v4.json",
        "pack.v4.json",
    }
    assert not (defaultspack_root / "ecosystem.json").exists()
    assert not (defaultspack_root / "permissions.json").exists()
    assert not (defaultspack_root / "routes.json").exists()
    assert not (defaultspack_root / "compat_aliases.yaml").exists()
    assert not (defaultspack_root / "docs" / "legacy_http_routes.yaml").exists()
    assert not (defaultspack_root / "domain" / "pack_architecture").exists()

    routes = canonical_http_route_specs()
    assert routes
    assert all(route.handler_name for route in routes)
    assert not any(route.block_module for route in routes)
    assert not any(route.fallback_block_module for route in routes)
    assert not any(route.legacy_block_module for route in routes)
    assert compatibility_alias_allowed("defaults.chat.send") is False


def test_bundle_rejects_manifest_hash_drift_and_unlisted_artifacts(
    tmp_path: Path,
) -> None:
    copied = tmp_path / "v4"
    shutil.copytree(BUNDLE_ROOT, copied)
    manifest = copied / "packs" / "defaultspack.pack.v4.json"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(BundleIntegrityError, match="digest changed"):
        BundledCatalog.load(copied)

    catalog = _catalog()
    approved = _approved(catalog)
    approved.remove(catalog.packs["rumi_file_inspect_pack"]["pack"]["artifact_digest"])
    with pytest.raises(
        ProfileResolutionDenied, match="not approved: rumi_file_inspect_pack"
    ):
        resolve_default_profile(
            catalog,
            "defaults",
            approved_artifact_digests=approved,
            authority_snapshot_digest=SNAPSHOT_DIGEST,
            authority_bindings=AUTHORITY_BINDINGS,
            security_epoch=7,
        )


def test_bundle_rejects_self_consistent_lock_with_stale_definition_revision(
    tmp_path: Path,
) -> None:
    copied = tmp_path / "v4"
    shutil.copytree(BUNDLE_ROOT, copied)
    base_path = copied / "defaults-basepack.base.v1.json"
    base = json.loads(base_path.read_text(encoding="utf-8"))
    base["definition_revision"] = "sha256:" + "0" * 64
    base_path.write_text(json.dumps(base, indent=2) + "\n", encoding="utf-8")
    lock_path = copied / "bundle.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    entry = next(
        item
        for item in lock["entries"]
        if item["path"] == "defaults-basepack.base.v1.json"
    )
    entry["digest"] = "sha256:" + hashlib.sha256(base_path.read_bytes()).hexdigest()
    lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(BundleIntegrityError, match="definition revision"):
        BundledCatalog.load(copied)


def test_bundle_rejects_symlinked_locked_artifact(tmp_path: Path) -> None:
    copied = tmp_path / "v4"
    shutil.copytree(BUNDLE_ROOT, copied)
    manifest = copied / "packs" / "defaultspack.pack.v4.json"
    redirected = copied / "redirected.pack.v4.json"
    redirected.write_bytes(manifest.read_bytes())
    manifest.unlink()
    manifest.symlink_to(redirected)

    with pytest.raises(BundleIntegrityError, match="contains a symlink"):
        BundledCatalog.load(copied)


def test_foundational_conversation_provider_is_exactly_one() -> None:
    catalog = _catalog()
    missing_manifest = copy.deepcopy(catalog.packs["defaultspack"])
    missing_manifest["functions"] = []
    missing_manifest["contracts"] = []
    missing = replace(
        catalog,
        packs={**catalog.packs, "defaultspack": missing_manifest},
    )
    with pytest.raises(ProfileResolutionDenied, match="exactly once; found 0"):
        _resolve(missing)

    duplicate = copy.deepcopy(catalog.packs["defaultspack"])
    duplicate["pack"]["id"] = "duplicate-conversation"
    duplicate["pack"]["artifact_digest"] = "sha256:" + "8" * 64
    duplicate_catalog = replace(
        catalog,
        packs={**catalog.packs, "duplicate-conversation": duplicate},
    )
    with pytest.raises(ProfileResolutionDenied, match="exactly once; found 2"):
        resolve_default_profile(
            duplicate_catalog,
            "defaults",
            approved_artifact_digests=_approved(duplicate_catalog),
            authority_snapshot_digest=SNAPSHOT_DIGEST,
            authority_bindings=AUTHORITY_BINDINGS,
            security_epoch=7,
            additional_pack_ids=("duplicate-conversation",),
        )


def test_requested_pack_dependency_and_authority_references_are_mandatory() -> None:
    catalog = _catalog()
    profile = copy.deepcopy(catalog.profiles["defaults"])
    profile["packs"] = [
        item for item in profile["packs"] if item["pack_id"] != "rumi_file_inspect_pack"
    ]
    missing_dependency = replace(catalog, profiles={"defaults": profile})
    with pytest.raises(
        ProfileResolutionDenied, match="must resolve exactly once; found 0"
    ):
        _resolve(missing_dependency)

    with pytest.raises(
        ProfileResolutionDenied, match="Authority Kernel reference is missing"
    ):
        resolve_default_profile(
            catalog,
            "defaults",
            approved_artifact_digests=_approved(catalog),
            authority_snapshot_digest=SNAPSHOT_DIGEST,
            authority_bindings={},
            security_epoch=7,
        )


def test_activation_restart_is_atomic_and_stale_records_deny(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = _authority(tmp_path / "authority.sqlite3")
    store = ActivationStore(
        tmp_path / "state", workspace, profile_id="defaults", authority=authority
    )
    resolved = _resolve()
    activation = store.activate(
        resolved,
        activation_id="activation:defaults-0001",
        created_at="2026-08-05T00:00:00Z",
    )
    assert activation["state"] == "active"
    assert store.load_active().plan == resolved.plan

    pointer = json.loads(
        (tmp_path / "state" / "active.json").read_text(encoding="utf-8")
    )
    envelope_path = tmp_path / "state" / "activations" / pointer["envelope_path"]
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["lock"]["security_epoch"] = 6
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ProfileResolutionDenied, match="envelope digest changed"):
        store.load_active()


def test_new_activation_atomically_retires_the_previous_authority(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = _authority(tmp_path / "authority.sqlite3")
    store = ActivationStore(
        tmp_path / "state", workspace, profile_id="defaults", authority=authority
    )
    resolved = _resolve()

    first = store.activate(
        resolved,
        activation_id="activation:defaults-first",
        created_at="2026-08-05T00:00:00Z",
    )
    second = store.activate(
        resolved,
        activation_id="activation:defaults-second",
        created_at="2026-08-05T00:01:00Z",
    )

    assert second["fencing_token"] > first["fencing_token"]
    assert authority.active_activation_reservation(first["activation_id"]) is None
    active = authority.active_activation_reservation(second["activation_id"])
    assert active is not None
    assert active["state"] == "active"
    assert (
        store.load_active_snapshot().activation["activation_id"]
        == second["activation_id"]
    )


def test_independent_process_activations_never_publish_retired_pointer(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    authority_path = tmp_path / "authority.sqlite3"
    bootstrap = _authority(authority_path)
    bootstrap.close()
    context = multiprocessing.get_context("spawn")
    committed = context.Event()
    release = context.Event()
    results = context.Queue()
    first_id = "activation:defaults-process-first"
    second_id = "activation:defaults-process-second"
    first = context.Process(
        target=_activation_process,
        args=(
            str(packaged_profile_bundle_root()),
            str(state_root),
            str(workspace),
            str(authority_path),
            first_id,
            committed,
            release,
            results,
        ),
    )
    second = context.Process(
        target=_activation_process,
        args=(
            str(packaged_profile_bundle_root()),
            str(state_root),
            str(workspace),
            str(authority_path),
            second_id,
            None,
            None,
            results,
        ),
    )
    first.start()
    assert committed.wait(timeout=15)
    second.start()
    time.sleep(0.25)
    assert second.is_alive()
    release.set()
    first.join(timeout=20)
    second.join(timeout=20)
    assert first.exitcode == 0
    assert second.exitcode == 0
    outcomes = {results.get(timeout=2), results.get(timeout=2)}
    assert {item[0] for item in outcomes} == {"ok"}

    first_pointer = json.loads((state_root / "active.json").read_text(encoding="utf-8"))
    restarted_authority = _authority(authority_path)
    restarted = ActivationStore(
        state_root,
        workspace,
        profile_id="defaults",
        authority=restarted_authority,
    )
    active = restarted.load_active_snapshot().activation
    assert active["activation_id"] == second_id
    first_reservations = [
        event["payload"]
        for event in restarted_authority.audit_events()
        if event["event_type"] == "activation"
        and event["event_state"] == "prepared"
        and event["payload"]["activation_id"] == first_id
    ]
    second_reservations = [
        event["payload"]
        for event in restarted_authority.audit_events()
        if event["event_type"] == "activation"
        and event["event_state"] == "prepared"
        and event["payload"]["activation_id"] == second_id
    ]
    assert (
        second_reservations[0]["fencing_token"] > first_reservations[0]["fencing_token"]
    )
    assert active["security_epoch"] == restarted_authority.security_epoch

    stale_envelope = state_root / "activations" / f"{first_id[11:]}.json"
    stale_payload = json.loads(stale_envelope.read_text(encoding="utf-8"))
    first_pointer.update(
        activation_id=first_id,
        envelope_path=stale_envelope.name,
        envelope_digest=canonical_digest(stale_payload),
    )
    (state_root / "active.json").write_text(json.dumps(first_pointer), encoding="utf-8")
    with pytest.raises(
        ProfileResolutionDenied, match="authority, fence, or SecurityEpoch"
    ):
        restarted.load_active_snapshot()
    restarted_authority.close()


def test_cross_process_predecessor_cas_precedes_authority_reservation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    authority_path = tmp_path / "authority.sqlite3"
    authority = _authority(authority_path)
    resolved = _resolve()
    predecessor = ActivationStore(
        state_root,
        workspace,
        profile_id="defaults",
        authority=authority,
    ).activate(
        resolved,
        activation_id="activation:defaults-cas-predecessor",
        created_at="2026-08-10T00:00:00Z",
    )
    authority.close()

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    contenders = [
        "activation:defaults-cas-first",
        "activation:defaults-cas-second",
    ]
    processes = [
        context.Process(
            target=_cas_activation_process,
            args=(
                str(packaged_profile_bundle_root()),
                str(state_root),
                str(workspace),
                str(authority_path),
                activation_id,
                str(resolved.plan["profile_revision"]),
                str(resolved.plan["plan_digest"]),
                str(predecessor["activation_id"]),
                barrier,
                results,
            ),
        )
        for activation_id in contenders
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    outcomes = [results.get(timeout=2), results.get(timeout=2)]
    assert sorted(item[0] for item in outcomes) == ["STALE_REVISION", "active"]
    winner = next(item[1] for item in outcomes if item[0] == "active")

    restarted_authority = _authority(authority_path)
    contender_events = [
        event
        for event in restarted_authority.audit_events()
        if event["event_type"] == "activation"
        and event["payload"].get("activation_id") in contenders
    ]
    assert sum(event["event_state"] == "prepared" for event in contender_events) == 1
    assert sum(event["event_state"] == "active" for event in contender_events) == 1
    restarted_store = ActivationStore(
        state_root,
        workspace,
        profile_id="defaults",
        authority=restarted_authority,
    )
    active = restarted_store.load_active_snapshot()
    assert active.activation["activation_id"] == winner
    replay = restarted_store.activate(
        resolved,
        activation_id=str(winner),
        created_at="2026-08-10T00:02:00Z",
        expected_predecessor_profile_revision=str(resolved.plan["profile_revision"]),
        expected_predecessor_plan_digest=str(resolved.plan["plan_digest"]),
        expected_predecessor_activation_id=str(predecessor["activation_id"]),
    )
    assert replay == active.activation
    assert (
        len(
            [
                event
                for event in restarted_authority.audit_events()
                if event["event_type"] == "activation"
                and event["event_state"] == "prepared"
                and event["payload"].get("activation_id") in contenders
            ]
        )
        == 1
    )
    restarted_authority.close()


def test_workspace_traversal_symlink_escape_and_cross_workspace_restart_deny(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    authority = _authority(tmp_path / "authority.sqlite3")
    store = ActivationStore(
        tmp_path / "state", workspace, profile_id="defaults", authority=authority
    )
    assert (
        store.resolve_workspace_path("notes/item.txt")
        == workspace / "notes" / "item.txt"
    )
    with pytest.raises(ProfileResolutionDenied, match="traversal-free"):
        store.resolve_workspace_path("../other/secret.txt")
    with pytest.raises(ProfileResolutionDenied, match="traversal-free"):
        store.resolve_workspace_path(str(other / "secret.txt"))

    link = workspace / "outside"
    link.symlink_to(other, target_is_directory=True)
    with pytest.raises(ProfileResolutionDenied, match="escapes"):
        store.resolve_workspace_path("outside/secret.txt")

    store.activate(
        _resolve(),
        activation_id="activation:defaults-0002",
        created_at="2026-08-05T00:00:00Z",
    )
    other_store = ActivationStore(
        tmp_path / "state", other, profile_id="defaults", authority=authority
    )
    with pytest.raises(ProfileResolutionDenied, match="another workspace"):
        other_store.load_active()


def test_activation_state_and_pointer_symlinks_fail_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    redirected_state = tmp_path / "redirected-state"
    redirected_state.mkdir()
    state_link = tmp_path / "state-link"
    state_link.symlink_to(redirected_state, target_is_directory=True)
    authority = _authority(tmp_path / "authority.sqlite3")

    with pytest.raises(ProfileResolutionDenied, match="state_root.*symlink"):
        ActivationStore(
            state_link,
            workspace,
            profile_id="defaults",
            authority=authority,
        )

    state = tmp_path / "state"
    store = ActivationStore(
        state,
        workspace,
        profile_id="defaults",
        authority=authority,
    )
    store.activate(
        _resolve(),
        activation_id="activation:defaults-symlink",
        created_at="2026-08-05T00:00:00Z",
    )
    pointer = state / "active.json"
    redirected_pointer = tmp_path / "redirected-active.json"
    pointer.replace(redirected_pointer)
    pointer.symlink_to(redirected_pointer)

    with pytest.raises(ProfileResolutionDenied, match="active pointer.*symlink"):
        store.load_active_snapshot()


def test_activation_journal_recovers_only_authority_committed_candidate(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = _authority(tmp_path / "authority.sqlite3")

    def crash(stage: str) -> None:
        if stage == "after_authority_commit":
            raise RuntimeError("simulated crash after authority commit")

    crashing = ActivationStore(
        tmp_path / "state",
        workspace,
        profile_id="defaults",
        authority=authority,
        fault=crash,
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        crashing.activate(
            _resolve(),
            activation_id="activation:defaults-crash",
            created_at="2026-08-05T00:00:00Z",
        )
    assert (tmp_path / "state" / "pending.json").is_file()

    recovered = ActivationStore(
        tmp_path / "state",
        workspace,
        profile_id="defaults",
        authority=authority,
    ).load_active_snapshot()
    assert recovered.activation["state"] == "active"
    assert recovered.activation["state_generation"] == 4
    assert not (tmp_path / "state" / "pending.json").exists()
    states = [
        event["event_state"]
        for event in authority.audit_events()
        if event["event_type"] == "activation"
    ]
    assert states == [
        "prepared",
        "ready_without_authority",
        "committing",
        "active",
    ]


def test_activation_candidate_aborts_on_epoch_revoke_and_token_is_not_reused(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = _authority(tmp_path / "authority.sqlite3")

    def revoke(stage: str) -> None:
        if stage == "ready_without_authority":
            authority.advance_security_epoch("emergency revoke during activation")

    store = ActivationStore(
        tmp_path / "state",
        workspace,
        profile_id="defaults",
        authority=authority,
        fault=revoke,
    )
    with pytest.raises(AuthorityDenied, match="stale SecurityEpoch|state fence"):
        store.activate(
            _resolve(),
            activation_id="activation:defaults-revoked",
            created_at="2026-08-05T00:00:00Z",
        )
    assert not (tmp_path / "state" / "active.json").exists()
    assert not (tmp_path / "state" / "pending.json").exists()
    reservations = [
        event["payload"]
        for event in authority.audit_events()
        if event["event_type"] == "activation" and event["event_state"] == "prepared"
    ]
    assert reservations[0]["fencing_token"] == 1


def test_activation_root_and_lock_hardlink_fail_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = _authority(tmp_path / "authority.sqlite3")
    state = tmp_path / "state"
    store = ActivationStore(
        state,
        workspace,
        profile_id="defaults",
        authority=authority,
    )
    profile_digest = hashlib.sha256(b"defaults").hexdigest()[:24]
    outside = tmp_path / "outside-lock"
    outside.write_bytes(b"0")
    os.link(outside, state / f".activation-{profile_digest}.lock")

    with pytest.raises(ProfileResolutionDenied, match="process lock is unavailable"):
        store.recover()
    assert authority.incomplete_activation_reservations("defaults") == ()


def test_activation_ancestor_replacement_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = _authority(tmp_path / "authority.sqlite3")
    owner = tmp_path / "owner"
    state = owner / "state"
    store = ActivationStore(
        state,
        workspace,
        profile_id="defaults",
        authority=authority,
    )
    displaced = tmp_path / "displaced-owner"
    owner.rename(displaced)
    owner.mkdir()
    state.mkdir()

    with pytest.raises(ProfileResolutionDenied, match="process lock is unavailable"):
        store.recover()


def test_activation_lock_deadline_then_recovery(tmp_path: Path) -> None:
    fcntl = pytest.importorskip("fcntl")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = _authority(tmp_path / "authority.sqlite3")
    state = tmp_path / "state"
    store = ActivationStore(
        state,
        workspace,
        profile_id="defaults",
        authority=authority,
        lock_timeout_seconds=0.03,
    )
    profile_digest = hashlib.sha256(b"defaults").hexdigest()[:24]
    lock_path = state / f".activation-{profile_digest}.lock"
    descriptor = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR,
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        with pytest.raises(ActivationLockTimeout, match="deadline exceeded"):
            store.recover()
        assert time.monotonic() - started < 0.5
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    store.recover()


def test_windows_activation_lock_adapter_uses_nonblocking_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = _authority(tmp_path / "authority.sqlite3")
    store = ActivationStore(
        tmp_path / "state",
        workspace,
        profile_id="defaults",
        authority=authority,
        lock_timeout_seconds=1.0,
        retry_sleep=lambda _delay: None,
    )
    calls: list[int] = []

    class Backend:
        LK_NBLCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(_descriptor: int, mode: int, _length: int) -> None:
            calls.append(mode)
            if calls == [Backend.LK_NBLCK]:
                raise BlockingIOError()

    ticks = iter((0.0, 0.1))
    monkeypatch.setattr(store, "_lock_platform", "nt")
    monkeypatch.setattr(
        runtime_service.importlib,
        "import_module",
        lambda name: Backend if name == "msvcrt" else None,
    )
    monkeypatch.setattr(store, "_monotonic_clock", lambda: next(ticks))

    store.recover()
    assert calls == [Backend.LK_NBLCK, Backend.LK_NBLCK, Backend.LK_UNLCK]


def test_activation_persistence_failure_keeps_old_pointer_and_aborts_reservation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority = _authority(tmp_path / "authority.sqlite3")
    state = tmp_path / "state"
    first_store = ActivationStore(
        state,
        workspace,
        profile_id="defaults",
        authority=authority,
    )
    first_store.activate(
        _resolve(),
        activation_id="activation:defaults-old-pointer",
        created_at="2026-08-11T00:00:00Z",
    )
    old_pointer = (state / "active.json").read_bytes()

    def replace_pending(stage: str) -> None:
        if stage != "prepared":
            return
        pending = state / "pending.json"
        displaced = state / "pending.displaced"
        pending.rename(displaced)
        os.link(displaced, pending)

    failing = ActivationStore(
        state,
        workspace,
        profile_id="defaults",
        authority=authority,
        fault=replace_pending,
    )
    with pytest.raises(ProfileResolutionDenied, match="persistence"):
        failing.activate(
            _resolve(),
            activation_id="activation:defaults-failed-pointer",
            created_at="2026-08-11T00:01:00Z",
        )

    assert (state / "active.json").read_bytes() == old_pointer
    failed = next(
        event["payload"]
        for event in authority.audit_events()
        if event["event_type"] == "activation"
        and event["event_state"] == "prepared"
        and event["payload"]["activation_id"] == "activation:defaults-failed-pointer"
    )
    reservation = authority.activation_reservation(str(failed["reservation_id"]))
    assert reservation is not None
    assert reservation["state"] == "aborted"
    assert (
        authority.active_activation_reservation("activation:defaults-failed-pointer")
        is None
    )
