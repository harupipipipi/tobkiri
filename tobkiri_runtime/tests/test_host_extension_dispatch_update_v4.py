"""Exact selected Host Extension revisions retain the production boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sys
import time
from types import SimpleNamespace
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from core_runtime import external_pack_catalog_v4, pack_control_v4
from core_runtime.authority.v4 import AuthorityDenied, AuthorityStore
from core_runtime.bootstrap import production_v4
from core_runtime.bootstrap.external_profile_catalog import (
    catalog_for_verified_active_profile,
)
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile,
    prepare_default_profile_confirmation,
    runtime_user_data_root,
)
from core_runtime.host_provider_backend_v4 import HostProviderContributionV4
from core_runtime.profile_runtime_port import require_profile_runtime
from tobkiri_protocol.bundle_catalog import BundledCatalog
from tobkiri_protocol.errors import SchemaValidationError
from tobkiri_protocol.validation import validate_document, validate_file

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
PACK_ID = "rumi_ai_gateway_pack"
PACK_ROOT = RUNTIME_ROOT / "ecosystem" / PACK_ID
HOST_AUTHORITY = "host-signed-external-host-extension-v4"


@pytest.fixture
def selected_host_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Supply an authenticated-loader boundary around real Gateway metadata."""

    successor = validate_file(PACK_ROOT / "pack.v4.json", "pack")
    predecessor = deepcopy(successor)
    predecessor["pack"]["artifact_digest"] = "sha256:" + "a" * 64
    selected_digest = successor["pack"]["artifact_digest"]
    root = tmp_path / "selected"
    root.mkdir()
    manifest_path = root / "pack.v4.json"
    manifest_path.write_text(json.dumps(successor), encoding="utf-8")
    from tests.conformance_support.packaged_profile import load_packaged_profile_catalog

    bundled = load_packaged_profile_catalog()
    catalog = replace(
        bundled,
        packs={**bundled.packs, PACK_ID: predecessor},
        executable_catalogs={**bundled.executable_catalogs, PACK_ID: {"old": True}},
    )
    record = {
        "pack_id": PACK_ID,
        "kind": "host_extension",
        "authority": HOST_AUTHORITY,
        "artifact_digest": selected_digest,
    }

    def load_records(artifact_pins: Any = None) -> dict[str, Any]:
        assert artifact_pins == {PACK_ID: selected_digest}
        return {PACK_ID: record}

    def resolve_root(
        pack_id: str,
        *,
        artifact_digest: str | None = None,
    ) -> Path:
        assert pack_id == PACK_ID
        assert artifact_digest == selected_digest
        return root

    monkeypatch.setattr(pack_control_v4, "load_pack_catalog", load_records)
    monkeypatch.setattr(pack_control_v4, "resolve_pack_root", resolve_root)

    class Runtime:
        @staticmethod
        def catalog_with_packs(original: BundledCatalog, packs: Any) -> Any:
            return replace(original, packs=dict(packs))

    monkeypatch.setattr(pack_control_v4, "require_profile_runtime", Runtime)
    return SimpleNamespace(
        catalog=catalog,
        successor=successor,
        predecessor=predecessor,
        digest=selected_digest,
        record=record,
        root=root,
    )


def test_exact_authenticated_host_revision_is_selected(selected_host_revision: Any) -> None:
    """Only the explicit authenticated revision replaces its same-kind baseline."""

    selected = selected_host_revision
    merged, closure = pack_control_v4.catalog_with_admitted_pack_closure(
        selected.catalog,
        [PACK_ID],
        artifact_pins={PACK_ID: selected.digest},
    )
    assert merged.packs[PACK_ID] == selected.successor
    assert merged.profiles is selected.catalog.profiles
    assert PACK_ID in closure
    assert selected.catalog.packs[PACK_ID] == selected.predecessor


@pytest.mark.parametrize(
    "mutation",
    [
        "normal_authority",
        "unrecognized_authority",
        "record_kind",
        "record_digest",
        "new_host_id",
        "normal_predecessor",
        "predecessor_boundary",
        "successor_boundary",
    ],
)
def test_host_revision_cannot_promote_or_mix_identities(
    selected_host_revision: Any,
    mutation: str,
) -> None:
    """A selected digest cannot promote Normal Packs or add a new Host identity."""

    selected = selected_host_revision
    if mutation == "normal_authority":
        selected.record["authority"] = "host-signed-external-normal-v4"
    elif mutation == "unrecognized_authority":
        selected.record["authority"] = "client-approved"
    elif mutation == "record_kind":
        selected.record["kind"] = "normal_sandbox"
    elif mutation == "record_digest":
        selected.record["artifact_digest"] = "sha256:" + "c" * 64
    elif mutation == "new_host_id":
        del selected.catalog.packs[PACK_ID]
    elif mutation == "normal_predecessor":
        selected.predecessor["pack"]["kind"] = "normal_sandbox"
    elif mutation == "predecessor_boundary":
        selected.predecessor["requirements"]["execution_boundary"] = "sandbox"
    else:
        selected.successor["requirements"]["execution_boundary"] = "sandbox"
        (selected.root / "pack.v4.json").write_text(
            json.dumps(selected.successor),
            encoding="utf-8",
        )
    with pytest.raises(pack_control_v4.PackControlDigestMismatch):
        pack_control_v4.catalog_with_admitted_pack_closure(
            selected.catalog,
            [PACK_ID],
            artifact_pins={PACK_ID: selected.digest},
        )


def test_unselected_host_revision_retains_bundled_pin(
    selected_host_revision: Any,
) -> None:
    """An admitted newer artifact never becomes a default Host selection."""

    selected = selected_host_revision
    merged, _closure = pack_control_v4.catalog_with_admitted_pack_closure(
        selected.catalog,
        [PACK_ID],
    )
    assert merged.packs[PACK_ID] == selected.predecessor


def _active_host_revision(selected: Any) -> SimpleNamespace:
    """Build one verified active Profile's selected Host closure pins."""

    pin = {"identity": PACK_ID, "role": "pack", "artifact_digest": selected.digest}
    return SimpleNamespace(
        resolved=SimpleNamespace(
            profile={
                "base": deepcopy(selected.catalog.profiles["defaults"]["base"]),
                "shell": deepcopy(selected.catalog.profiles["defaults"]["shell"]),
                "packs": [
                    {"pack_id": PACK_ID, "role": "provider", "artifact_digest": selected.digest},
                ],
            },
            lock={"effective_set": [deepcopy(pin)]},
            plan={"effective_set": [deepcopy(pin)]},
        )
    )


def test_verified_active_host_revision_preserves_exact_graphs(
    selected_host_revision: Any,
    tmp_path: Path,
) -> None:
    """Dispatch composition accepts only the already verified exact active pin."""

    selected = selected_host_revision
    active = _active_host_revision(selected)
    merged = catalog_for_verified_active_profile(
        selected.catalog,
        active,
        user_data=tmp_path,
    )
    assert merged.packs[PACK_ID] == selected.successor
    assert active.resolved.lock["effective_set"] == active.resolved.plan["effective_set"]


@pytest.mark.parametrize(
    "mutation",
    ["application_role", "duplicate_profile", "stale_lock", "stale_plan"],
)
def test_active_host_revision_rejects_stale_or_nonprovider_pins(
    selected_host_revision: Any,
    tmp_path: Path,
    mutation: str,
) -> None:
    """An authenticated artifact does not repair or widen active graph bindings."""

    selected = selected_host_revision
    active = _active_host_revision(selected)
    rows = active.resolved.profile["packs"]
    if mutation == "application_role":
        rows[0]["role"] = "application"
    elif mutation == "duplicate_profile":
        rows.append(deepcopy(rows[0]))
    else:
        graph = active.resolved.lock if mutation == "stale_lock" else active.resolved.plan
        graph["effective_set"][0]["artifact_digest"] = "sha256:" + "c" * 64
    with pytest.raises(AuthorityDenied):
        catalog_for_verified_active_profile(selected.catalog, active, user_data=tmp_path)


def test_configured_profile_runtime_overlays_exact_host_executable_catalog(
    selected_host_revision: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured concrete port replaces executable metadata with the manifest."""

    selected = selected_host_revision
    executable = validate_file(PACK_ROOT / "executables.v4.json", "executable_catalog")
    observed = []

    def load_executable(pack_id: str, manifest: Any) -> Any:
        assert pack_id == PACK_ID
        assert manifest == selected.successor
        observed.append(manifest["pack"]["artifact_digest"])
        return executable

    monkeypatch.setattr(
        external_pack_catalog_v4,
        "load_admitted_external_executable_catalog",
        load_executable,
    )
    monkeypatch.setattr(pack_control_v4, "require_profile_runtime", require_profile_runtime)
    merged, _closure = pack_control_v4.catalog_with_admitted_pack_closure(
        selected.catalog,
        [PACK_ID],
        artifact_pins={PACK_ID: selected.digest},
    )
    assert observed == [selected.digest]
    assert merged.executable_catalogs[PACK_ID] == executable
    assert merged.executable_catalogs[PACK_ID] != selected.catalog.executable_catalogs[PACK_ID]
    assert merged.packs[PACK_ID] == selected.successor
    assert merged.bases is selected.catalog.bases
    assert merged.shells is selected.catalog.shells
    assert merged.profiles is selected.catalog.profiles
    assert merged.root == selected.catalog.root


def test_host_contribution_must_match_selected_artifact_and_domain() -> None:
    """A new implementation's contribution cannot point back to an old artifact."""

    from tests.test_production_v4_host_extension_admission import _binding

    binding = _binding()
    key = (
        binding.operation.contract_id,
        binding.operation.operation_id,
        binding.principal_ref.value,
    )
    contribution = HostProviderContributionV4(
        contract_id=key[0],
        contract_version=binding.operation.contract_version,
        operation_id=key[1],
        principal_id=key[2],
        artifact_digest=binding.artifact.digest,
        implementation_digest=binding.function.implementation_digest,
        domain_id="domain.selected-host-revision",
        invoke=lambda *_args: {},
    )
    domains = {key: contribution.domain_id}
    production_v4._validate_host_provider_contributions((binding,), (contribution,), domains)
    for field, value in (
        ("artifact_digest", "sha256:" + "9" * 64),
        ("implementation_digest", "sha256:" + "8" * 64),
        ("contract_version", "2.0.0"),
        ("domain_id", "domain.old-host-revision"),
    ):
        with pytest.raises(AuthorityDenied, match="contribution identity"):
            production_v4._validate_host_provider_contributions(
                (binding,),
                (replace(contribution, **{field: value}),),
                domains,
            )
    for contributions in ((), (contribution, contribution)):
        with pytest.raises(AuthorityDenied, match="contribution set"):
            production_v4._validate_host_provider_contributions(
                (binding,),
                contributions,
                domains,
            )


def test_production_resolves_artifacts_from_captured_authority_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambient state cannot supply a different admitted Host revision at capture."""

    from ecosystem.defaultspack.defaultspack.runtime_composition import (
        defaultspack_activation_snapshot_loader,
    )
    from tests.conformance_support.packaged_profile import packaged_profile_bundle_root

    user_data = tmp_path / "authority-owner"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    active = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "ambient-other-owner"))

    class RootsObserved(Exception):
        pass

    def resolve_roots(
        pack_ids: Any,
        ecosystem_root: Path,
        *,
        artifact_pins: Any,
    ) -> Any:
        assert runtime_user_data_root() == user_data
        assert PACK_ID in pack_ids
        assert ecosystem_root == RUNTIME_ROOT / "ecosystem"
        assert artifact_pins == {
            row["identity"]: row["artifact_digest"] for row in active.resolved.lock["effective_set"]
        }
        raise RootsObserved

    monkeypatch.setattr(production_v4, "resolve_admitted_pack_roots", resolve_roots)
    with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
        with pytest.raises(RootsObserved):
            production_v4.capture_production_dispatch(
                active,
                bundle_root=packaged_profile_bundle_root(),
                ecosystem_root=RUNTIME_ROOT / "ecosystem",
                authority_store=authority,
                activation_snapshot_loader=defaultspack_activation_snapshot_loader,
            )


@pytest.mark.parametrize("kind", ["host_extension", "base", "shell"])
def test_revision_view_keeps_only_provider_host_baselines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Base and Shell identities cannot enter the Pack revision selector."""

    from core_runtime import profile_pack_versions as versions
    from core_runtime.bootstrap.profile_capture import host_profile_catalog

    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "view-owner"))
    catalog = host_profile_catalog()
    packs = deepcopy(catalog.packs)
    packs[PACK_ID]["pack"]["kind"] = kind
    monkeypatch.setattr(versions, "host_profile_catalog", lambda: replace(catalog, packs=packs))
    view = versions.profile_pack_versions("defaults")
    host_rows = [row for row in view["packs"] if row["pack_id"] == PACK_ID]
    if kind == "host_extension":
        assert len(host_rows) == 1
        assert host_rows[0]["role"] == "provider"
        assert host_rows[0]["selected_digest"] == packs[PACK_ID]["pack"]["artifact_digest"]
        assert host_rows[0]["versions"][0]["origin"] == "bundled"
    else:
        assert host_rows == []


def _host_catalog_document() -> dict[str, Any]:
    """Build schema-only metadata, without asserting Host authentication."""

    digest = "sha256:" + "a" * 64
    record = {
        "pack_id": PACK_ID,
        "display_name": "Tobkiri Gateway fixture",
        "description": "Signed Host Extension schema fixture.",
        "version": "1.1.0",
        "kind": "host_extension",
        "artifact_digest": digest,
        "authority": HOST_AUTHORITY,
        "approval_policy": "explicit",
        "capabilities": [],
        "dependencies": {},
        "provided_contracts": [],
        "required_contracts": [],
        "runtime_artifacts": [],
        "execution_boundary": "host_brokered",
        "workspace_boundary": "host_brokered",
        "network": {},
        "secrets": [],
        "legacy_ids": [],
        "legacy_operations": [],
        "source_provenance": {
            "mode": "host-signed-external-normal-catalog",
            "publisher_id": "tobkiri.repository",
            "key_id": "fixture-key",
        },
    }
    entry = {
        "version": "io.tobkiri.external-normal-pack-catalog-entry.v4",
        "state": "committed",
        "pack_id": PACK_ID,
        "version_string": "1.1.0",
        "artifact_digest": digest,
        "content_digest": "sha256:" + "b" * 64,
        "publisher_id": "tobkiri.repository",
        "key_id": "fixture-key",
        "catalog_record": record,
        "store_token": "a" * 64 + "/" + PACK_ID,
        "admission_id": "c" * 64,
        "root_device": 1,
        "root_inode": 1,
    }
    return {
        "version": "io.tobkiri.external-normal-pack-catalog.v4",
        "entries": {f"{PACK_ID}@{digest}": entry},
        "journal": [],
        "signature": "d" * 64,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "normal_sandbox"),
        ("authority", "host-signed-external-normal-v4"),
        ("execution_boundary", "sandbox"),
        ("workspace_boundary", "sandbox"),
    ],
)
def test_external_catalog_schema_rejects_mixed_host_authority_tuple(
    field: str,
    value: str,
) -> None:
    """A schema-valid record cannot mix Host authority with Normal boundaries."""

    catalog = _host_catalog_document()
    assert validate_document(catalog, "external_pack_catalog") == catalog
    record = next(iter(catalog["entries"].values()))["catalog_record"]
    record[field] = value
    with pytest.raises(SchemaValidationError):
        validate_document(catalog, "external_pack_catalog")


def _sign_gateway(root: Path, key: Ed25519PrivateKey) -> dict[str, Any]:
    """Sign finite Gateway bytes with one fixture-owned publisher key."""

    from core_runtime.pack_signature import (
        SIGNED_MANIFEST_RELATIVE,
        build_signed_manifest,
        sign_manifest,
    )
    from tobkiri_host.artifact_compiler import compile_pack_root

    compiled = compile_pack_root(root)
    manifest = validate_file(root / "pack.v4.json", "pack")
    signed = sign_manifest(
        build_signed_manifest(
            root,
            pack_id=PACK_ID,
            version=compiled.artifact.version,
            publisher_id=compiled.artifact.publisher_lineage,
            core_compatibility=">=0",
            contract_versions={
                operation.contract_id: operation.contract_version
                for function in compiled.artifact.functions
                for operation in function.operations
            },
            requested_capabilities=list(manifest["requirements"]["capabilities"]),
        ),
        key,
    )
    path = root / SIGNED_MANIFEST_RELATIVE
    path.parent.mkdir(mode=0o700)
    path.write_text(json.dumps(signed, sort_keys=True) + "\n", encoding="utf-8")
    return signed


def _enrolled_gateway_successor(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Enroll the baseline key through real Host policy before a signed update."""

    from core_runtime.pack_artifact_integrity import write_host_install_record
    from core_runtime.pack_signature import SIGNED_MANIFEST_RELATIVE
    from tests.test_host_extension_update_v4 import _refresh

    key = Ed25519PrivateKey.generate()
    baseline = tmp_path / "trusted-baseline" / PACK_ID
    shutil.copytree(PACK_ROOT, baseline, ignore=shutil.ignore_patterns("__pycache__"))
    baseline_signed = _sign_gateway(baseline, key)
    public_key = tmp_path / "trusted-publisher.pem"
    public_key.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    policy_dir = tmp_path / "host-policy"
    policy_dir.mkdir(mode=0o700)
    policy = policy_dir / "publisher-trust.json"
    write_host_install_record(
        policy,
        pack_id=PACK_ID,
        install_path=baseline,
        record={
            "signature_required": True,
            "publisher_id": baseline_signed["publisher_id"],
            "key_id": baseline_signed["signature"]["key_id"],
            "installed_version": baseline_signed["version"],
            "signed_manifest_path": SIGNED_MANIFEST_RELATIVE,
            "contract_versions": baseline_signed["contract_versions"],
            "requested_capabilities": baseline_signed["requested_capabilities"],
        },
        publisher_record={
            "public_key_pem": public_key.read_text(encoding="utf-8"),
            "allowed_pack_namespaces": [PACK_ID],
            "revoked_key_ids": [],
        },
    )
    successor = tmp_path / "signed-successor" / PACK_ID
    shutil.copytree(PACK_ROOT, successor, ignore=shutil.ignore_patterns("__pycache__"))
    manifest_path = successor / "pack.v4.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pack"]["version"] = "1.1.0"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    runtime = successor / "runtime" / "gateway.py"
    runtime.write_text(
        runtime.read_text(encoding="utf-8")
        + '\nTOBKIRI_SIGNED_UPDATE_SENTINEL = "selected-gateway-revision-two"\n',
        encoding="utf-8",
    )
    _refresh(successor)
    _sign_gateway(successor, key)
    return successor, public_key, policy


def _approve_gateway_profile_revision(user_data: Path, profile_id: str) -> None:
    """Activate through the normal exact catalog and user-review CAS ceremony."""

    from core_runtime.active_profile_store_v4 import ActiveProfileStore
    from core_runtime.bootstrap.profile_capture import host_profile_catalog
    from core_runtime.profile_catalog_v4 import bundle_lock_digest, profile_catalog_digest
    from ecosystem.defaultspack.domain.runtime_surface_v4 import (
        RuntimeProfileChangeService,
        RuntimeSurfaceService,
    )
    from tobkiri_protocol.canonical import canonical_digest

    catalog = host_profile_catalog()
    predecessor = ActiveProfileStore(user_data).require(verify_snapshot=True)
    definition = catalog.profiles[profile_id]
    surface = RuntimeSurfaceService(catalog_loader=host_profile_catalog)
    try:
        service = RuntimeProfileChangeService(surface_service=surface, user_data_root=user_data)
        candidate = service.resolve(
            {
                "profile_id": profile_id,
                "expected_profile_revision": predecessor.profile_revision,
                "expected_plan_digest": predecessor.plan_digest,
                "desired_pack_ids": [
                    row["pack_id"]
                    for row in definition["packs"]
                    if row.get("role") != "application"
                ],
                "profile_definition_digest": canonical_digest(definition),
                "profile_catalog_digest": profile_catalog_digest(catalog),
                "bundle_lock_digest": bundle_lock_digest(catalog),
            },
            session_id="gateway-revision-session",
        )
        reviewed = service.review(
            {
                "candidate_id": candidate["candidate_id"],
                "candidate_digest": candidate["candidate_digest"],
            },
            session_id="gateway-revision-session",
        )
        approved = service.approve(
            {
                "candidate_id": reviewed["candidate_id"],
                "candidate_digest": reviewed["candidate_digest"],
            },
            session_id="gateway-revision-session",
        )
        service.activate(
            {
                "approval_id": approved["approval_id"],
                "approval_digest": approved["approval_digest"],
            },
            session_id="gateway-revision-session",
        )
    finally:
        surface.close()


def _retained_gateway_host_state(
    user_data: Path,
    active: Any,
    authority: AuthorityStore,
) -> SimpleNamespace:
    """Seed real temporary Host state and reserve a slot without any HTTP call.

    The finite interactive settlement below is a test-only Host enrollment,
    following the generation-budget fixtures. It does not exercise a live
    Native window, authorize a live provider request, or represent a VM.
    """

    from core_runtime.authority.v4_models import (
        ApprovalRecord,
        AuthorityScope,
        FunctionPrincipal,
        GrantLifetime,
        GrantRecord,
        InteractiveApprovalDecision,
        InteractiveApprovalRequest,
        authority_digest,
    )
    from core_runtime.credential_transport import CredentialTransportBinding
    from core_runtime.generation_transport_budget import (
        ENROLL_FUNCTION,
        ENROLL_OPERATION,
        GenerationBudgetPolicy,
        HostGenerationBudgetAdmission,
        PRICE_POLICY_FIELDS,
    )
    from core_runtime.native_generation_budget import owner_startup_generation_budget_template
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
    from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry
    from tobkiri_host.credential_store import HostCredentialMaterialStore, KEY_VERSION

    template = owner_startup_generation_budget_template()
    assert template is not None
    profile_id = str(active.resolved.profile["profile_id"])
    assert template.profile_id == profile_id
    witness = AuthorityStore(user_data / "credentials/generation-budget-witness.sqlite3")
    template_before = authority.get_host_pending_effect(template.effect_id)
    assert template_before == witness.get_host_pending_effect(template.effect_id)
    assert template_before is not None
    assert template_before[1]["template"] == template.to_dict()
    policy = GenerationBudgetPolicy(
        enrollment_id="gateway-update-retained-enrollment",
        profile_id=profile_id,
        consumer_pack_id=template.consumer_pack_id,
        connection_id=template.connection_id,
        credential_handle="credential:gateway-update-retained-fixture",
        credential_key_version=KEY_VERSION,
        endpoint="https://openrouter.ai/api/v1/chat/completions",
        model=template.model,
        max_requests=template.max_requests,
        max_output_tokens=template.max_output_tokens,
        **{field: getattr(template, field) for field in PRICE_POLICY_FIELDS},
    )
    provider = FunctionPrincipal.from_dict(
        next(
            row["function_principal"]
            for row in active.resolved.plan["bindings"]
            if row["contract_id"] == "tobkiri.service.ai.provider.generate.v1"
        )
    )
    caller = FunctionPrincipal.from_dict(
        next(
            row["function_principal"]
            for row in active.resolved.plan["bindings"]
            if row["contract_id"] == "tobkiri.service.ai.generate.v1"
        )
    )

    def domain_for(principal: Any) -> Any:
        suffix = principal.principal_id.removeprefix("sha256:")[:24]
        domain = authority.get_domain(
            f"domain.provider.{suffix}.{active.activation['fencing_token']}"
        )
        assert domain is not None and principal in domain.principals
        return domain

    caller_domain = domain_for(caller)
    provider_domain = domain_for(provider)
    target = replace(provider, function_id=ENROLL_FUNCTION, operation_id=ENROLL_OPERATION)
    scope = AuthorityScope(
        capability="host.generation-budget",
        semantics_digest=policy.digest,
        exact_request_digest=policy.digest,
    )
    now = time.time()
    request = InteractiveApprovalRequest(
        request_id="gateway-retained-budget-test-enrollment",
        request_digest=policy.digest,
        caller=caller,
        target=target,
        profile_id=profile_id,
        activation_id=str(active.activation["activation_id"]),
        activation_digest=authority_digest(dict(active.activation)),
        plan_digest=str(active.resolved.plan["plan_digest"]),
        profile_authority_digest=str(active.activation["profile_authority_snapshot_digest"]),
        profile_revision=str(active.resolved.plan["profile_revision"]),
        security_epoch=authority.security_epoch,
        fencing_token=int(active.activation["fencing_token"]),
        caller_domain_id=caller_domain.domain_id,
        caller_boot_epoch=caller_domain.boot_epoch,
        target_domain_id=provider_domain.domain_id,
        target_boot_epoch=provider_domain.boot_epoch,
        target_backend_digest=authority_digest("temporary-retained-budget-fixture"),
        handle_namespace="gateway-retained-budget-fixture",
        base_scope=scope,
        invocation_owner_id="gateway-retained-budget-test-owner",
        presentation_owner_principal_id=caller.principal_id,
        presentation_owner_session_id="gateway-retained-budget-test-native",
        caller_session_id="gateway-retained-budget-test-caller",
        caller_publisher_lineage="tobkiri.repository",
        target_publisher_lineage="tobkiri.repository",
        created_at=now,
        expires_at=now + 300,
    )
    approval = ApprovalRecord(
        approval_id="gateway-retained-budget-test-approval",
        snapshot_digest=request.digest,
        actor_id="temporary-fixture-user",
        decision="approved",
        decided_at=now,
        caller=caller,
        target=target,
        profile_id=profile_id,
        effect_bundle_digest=policy.digest,
        security_epoch=authority.security_epoch,
    )
    grant = GrantRecord(
        grant_id="gateway-retained-budget-test-grant",
        caller=caller,
        target=target,
        profile_id=profile_id,
        activation_id=str(active.activation["activation_id"]),
        profile_authority_digest=request.profile_authority_digest,
        caller_publisher_lineage=request.caller_publisher_lineage,
        target_publisher_lineage=request.target_publisher_lineage,
        scope=scope,
        lifetime=GrantLifetime.ONE_SHOT,
        security_epoch=authority.security_epoch,
        approval_id=approval.approval_id,
        issued_at=now,
        max_uses=1,
    )
    decision = InteractiveApprovalDecision(
        decision_id=request.request_id,
        request_id=request.request_id,
        request_snapshot_digest=request.digest,
        decision="approved",
        actor_id=approval.actor_id,
        decided_at=now,
        security_epoch=authority.security_epoch,
        ui_operator_digest=authority_digest("temporary-finite-native-fixture"),
        typed_confirmation_verified=False,
        approval_id=approval.approval_id,
        grant_id=grant.grant_id,
    )
    authority.create_interactive_approval_request(request)
    authority.settle_interactive_approval(decision, approval=approval, grant=grant)
    guard = HostGenerationBudgetAdmission(
        policy=policy,
        authority_store=authority,
        witness_store=witness,
    )
    guard.enroll_after_interactive_approval(approval_request_id=request.request_id)
    active_budget_id = template.effect_id + ":active"
    active_budget = {
        "effect_id": active_budget_id,
        "state": "enrolled",
        "policy": policy.to_dict(),
        "approval_request_id": request.request_id,
    }
    authority.create_host_pending_effect(active_budget_id, active_budget)
    witness.create_host_pending_effect(active_budget_id, active_budget)
    material = HostCredentialMaterialStore(user_data_root=user_data)
    material.create(
        secret_material={"api_key": "synthetic-retained-key-no-provider-call"},
        consumer_pack_id=policy.consumer_pack_id,
        provider_instance_id=policy.connection_id,
        scopes=["ai.generate", "ai.stream"],
        profile_id=profile_id,
        credential_handle=policy.credential_handle,
        generation_budget_binding=guard.credential_budget_binding,
    )
    providers = ProviderRegistry(profile_id, user_data_root=user_data)
    providers.save(
        {
            "provider_instance_id": policy.connection_id,
            "adapter_id": "openai-compatible",
            "endpoint": "https://openrouter.ai/api/v1",
            "credential_handle": policy.credential_handle,
            "enabled": True,
        },
        expected_revision=0,
    )
    conversations = ConversationStore(profile_id, user_data_root=user_data)
    created = conversations.create(
        {
            "id": "gateway-retained-chat",
            "title": "Retained Gateway update conversation",
        },
        expected_revision=0,
    )
    conversations.append_message(
        "gateway-retained-chat",
        {"id": "gateway-retained-message", "role": "user", "content": "Keep this existing chat."},
        expected_conversation_revision=created["conversation"]["conversation_revision"],
    )
    reservation = {
        "binding": CredentialTransportBinding(
            profile_id=profile_id,
            activation_id=str(active.activation["activation_id"]),
            security_epoch=authority.security_epoch,
            caller_principal_id=caller.principal_id,
            provider_principal_id=provider.principal_id,
            provider_function_id=provider.function_id,
            operation_id=provider.operation_id,
            target_domain_id=provider_domain.domain_id,
            target_boot_epoch=provider_domain.boot_epoch,
            request_id="gateway-retained-budget-spent-attempt",
            request_digest=authority_digest("gateway-retained-budget-spent-attempt"),
            credential_handle=policy.credential_handle,
            credential_key_version=policy.credential_key_version,
            provider_instance_id=policy.connection_id,
            credential_scope="ai.generate",
            credential_purpose="provider.invoke",
            endpoint_origin="https://openrouter.ai",
            consumer_pack_id=policy.consumer_pack_id,
        ),
        "contract_id": "tobkiri.service.ai.provider.generate.v1",
        "endpoint": policy.endpoint,
        "body": {
            "model": policy.model,
            "messages": [],
            "max_tokens": policy.max_output_tokens,
            "provider": {
                "allow_fallbacks": False,
                "max_price": {
                    "prompt": policy.max_prompt_price_microusd_per_million / 1_000_000,
                    "completion": policy.max_completion_price_microusd_per_million / 1_000_000,
                },
            },
        },
    }
    guard.reserve(**reservation)
    budget_before = authority.get_host_pending_effect(policy.effect_id)
    assert budget_before == witness.get_host_pending_effect(policy.effect_id)
    assert budget_before is not None and len(budget_before[1]["attempts"]) == 1
    protected_paths = (
        material.path,
        material.key_path,
        authority.key_path,
        witness.key_path,
        providers.path,
        conversations.path,
    )
    files_before = {path: path.read_bytes() for path in protected_paths}
    assert b"synthetic-retained-key-no-provider-call" not in files_before[material.path]
    return SimpleNamespace(
        authority=authority,
        witness=witness,
        policy=policy,
        guard=guard,
        reservation=reservation,
        budget_before=budget_before,
        template=template,
        template_before=template_before,
        active_budget_id=active_budget_id,
        active_budget_before=authority.get_host_pending_effect(active_budget_id),
        material=material,
        profile_id=profile_id,
        providers=providers,
        providers_before=providers.snapshot(),
        conversations=conversations,
        conversations_before=conversations.snapshot(),
        files_before=files_before,
    )


def _assert_retained_gateway_host_state(state: SimpleNamespace) -> None:
    """Prove bytes and spent ledger slots survive each update boundary."""

    from core_runtime.credential_transport import CredentialTransportDenied

    assert state.providers.snapshot() == state.providers_before
    assert state.conversations.snapshot() == state.conversations_before
    assert {path: path.read_bytes() for path in state.files_before} == state.files_before
    assert (
        state.authority.get_host_pending_effect(state.template.effect_id) == state.template_before
    )
    assert state.witness.get_host_pending_effect(state.template.effect_id) == state.template_before
    assert (
        state.authority.get_host_pending_effect(state.active_budget_id)
        == state.active_budget_before
    )
    assert (
        state.witness.get_host_pending_effect(state.active_budget_id) == state.active_budget_before
    )
    assert state.authority.get_host_pending_effect(state.policy.effect_id) == state.budget_before
    assert state.witness.get_host_pending_effect(state.policy.effect_id) == state.budget_before
    with pytest.raises(CredentialTransportDenied) as denied:
        state.guard.reserve(**state.reservation)
    assert denied.value.code == "generation_budget_replay"
    assert state.authority.get_host_pending_effect(state.policy.effect_id) == state.budget_before
    assert state.witness.get_host_pending_effect(state.policy.effect_id) == state.budget_before


@pytest.mark.skipif(sys.platform == "win32", reason="Host policy fixtures use POSIX descriptors")
def test_native_signed_gateway_update_reaches_same_host_production_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Select real signed bytes while retaining populated Host-owned state."""

    from core_runtime import native_pack_onboarding
    from core_runtime.bootstrap.profile_capture import (
        capture_active_profile,
    )
    from core_runtime.external_pack_catalog_v4 import resolve_admitted_pack_root
    from core_runtime.panel_auth import PanelAuthManager
    from core_runtime.profile_pack_versions import (
        profile_pack_versions,
        select_profile_pack_version,
    )
    from ecosystem.defaultspack.defaultspack.runtime_composition import (
        defaultspack_activation_snapshot_loader,
    )
    from ecosystem.defaultspack.domain.runtime_surface_v4 import create_runtime_surface_services
    from tests.conformance_support.packaged_profile import packaged_profile_bundle_root
    from tobkiri_host.credential_store import host_credential_store_factory
    from tobkiri_host.errors import InvalidArtifactError

    user_data = tmp_path / "same-host-owner"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    initial = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    profile_id = str(initial.resolved.profile["profile_id"])
    baseline_digest = next(
        row["artifact_digest"]
        for row in initial.resolved.lock["effective_set"]
        if row["identity"] == PACK_ID
    )
    pointer_path = user_data / "profiles" / "active.json"
    baseline_pointer = pointer_path.read_bytes()
    bundled_runtime = (PACK_ROOT / "runtime" / "gateway.py").read_bytes()
    bundle_root = packaged_profile_bundle_root()
    bundle_lock = (bundle_root / "bundle.lock.json").read_bytes()
    runtime_port = require_profile_runtime()
    source, public_key, policy = _enrolled_gateway_successor(tmp_path)
    preview = native_pack_onboarding.preview_signed_pack(source, public_key)
    assert preview["kind"] == "host_extension"
    assert preview["execution_boundary"] == "host_brokered"
    assert preview["predecessor_artifact_digest"] == baseline_digest
    observed = []
    observed_budget_bindings = []
    actual_loader = production_v4._load_verified_host_provider_factory
    actual_budget_capture = production_v4.capture_native_generation_budget

    def observe_budget_capture(**kwargs: Any) -> Any:
        binding = actual_budget_capture(**kwargs)
        observed_budget_bindings.append(binding)
        return binding

    def observe_factory(root: Path, function_id: str, bindings: Any) -> Any:
        result = actual_loader(root, function_id, bindings)
        factory, _backend_id = result
        if function_id == f"{PACK_ID}.ai-gateway.generate":
            module = sys.modules[type(factory).__module__]
            observed.append(
                (
                    root,
                    bindings[0].artifact.digest,
                    getattr(
                        module,
                        "TOBKIRI_SIGNED_UPDATE_SENTINEL",
                        None,
                    ),
                )
            )
        return result

    monkeypatch.setattr(production_v4, "_load_verified_host_provider_factory", observe_factory)
    monkeypatch.setattr(production_v4, "capture_native_generation_budget", observe_budget_capture)
    manager = PanelAuthManager(bootstrap_secret="gateway-revision-test-owner")
    sessions = []
    retained_state = None
    with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:

        def capture(active: Any) -> Any:
            session = production_v4.capture_production_dispatch(
                active,
                bundle_root=bundle_root,
                ecosystem_root=RUNTIME_ROOT / "ecosystem",
                authority_store=authority,
                activation_snapshot_loader=defaultspack_activation_snapshot_loader,
                runtime_surface_factory=create_runtime_surface_services,
                panel_auth_manager=manager,
                credential_store_factory=host_credential_store_factory,
            )
            sessions.append(session)
            return session

        try:
            old_session = capture(initial)
            assert observed[-1][1:] == (baseline_digest, None)
            retained_state = _retained_gateway_host_state(user_data, initial, authority)
            assert (
                observed_budget_bindings[-1].generation_admission.credential_budget_binding
                == retained_state.guard.credential_budget_binding
            )
            admitted = native_pack_onboarding.commit_signed_pack(
                source,
                public_key,
                expected_preview_digest=preview["preview_digest"],
                trust_store_path=policy,
            )
            successor_digest = str(admitted["artifact_digest"])
            assert successor_digest != baseline_digest
            assert pointer_path.read_bytes() == baseline_pointer
            cas_root = resolve_admitted_pack_root(PACK_ID, artifact_digest=successor_digest)
            assert cas_root != PACK_ROOT
            _assert_retained_gateway_host_state(retained_state)
            view = profile_pack_versions(profile_id)
            selected = next(row for row in view["packs"] if row["pack_id"] == PACK_ID)
            assert {row["artifact_digest"] for row in selected["versions"]} == {
                baseline_digest,
                successor_digest,
            }
            result = select_profile_pack_version(
                {
                    "profile_id": profile_id,
                    "pack_id": PACK_ID,
                    "artifact_digest": successor_digest,
                    "expected_selected_digest": selected["selected_digest"],
                    "expected_profile_revision": view["profile_definition_revision"],
                    "expected_store_generation": view["store_generation"],
                    "expected_catalog_revision": view["catalog_revision"],
                }
            )
            assert result["activation_required"] is True
            assert pointer_path.read_bytes() == baseline_pointer
            assert capture_active_profile().resolved.plan == initial.resolved.plan
            _approve_gateway_profile_revision(user_data, profile_id)
            updated = capture_active_profile()
            assert updated.activation["activation_id"] != initial.activation["activation_id"]
            new_session = capture(updated)
            assert observed[-1] == (
                cas_root,
                successor_digest,
                "selected-gateway-revision-two",
            )
            assert (
                old_session.owned_authority_store is new_session.owned_authority_store is authority
            )
            new_session.assert_operation_ready(
                "tobkiri.service.ai.generate.v1",
                f"{PACK_ID}.ai-gateway.generate",
            )
            selected_binding = new_session.broker._catalog.resolve_pinned(
                "tobkiri.service.ai.generate.v1",
                f"{PACK_ID}.ai-gateway.generate",
            )
            with pytest.raises(InvalidArtifactError):
                actual_loader(PACK_ROOT, selected_binding.function.function_id, (selected_binding,))
            for field in ("base", "shell", "application"):
                assert updated.resolved.lock.get(field) == initial.resolved.lock.get(field)
            _assert_retained_gateway_host_state(retained_state)
            assert (
                observed_budget_bindings[-1].generation_admission.credential_budget_binding
                == retained_state.guard.credential_budget_binding
            )
        finally:
            if retained_state is not None:
                retained_state.witness.close()
            for session in reversed(sessions):
                session.close()
    assert require_profile_runtime() is runtime_port
    assert (PACK_ROOT / "runtime" / "gateway.py").read_bytes() == bundled_runtime
    assert (bundle_root / "bundle.lock.json").read_bytes() == bundle_lock
