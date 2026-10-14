"""Existing-slot admission uses genuine isolated Authority and canary signatures."""

from __future__ import annotations

from dataclasses import replace
import json
import os
import shutil
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from core_runtime import external_pack_catalog_v4 as external
from core_runtime import existing_host_extension_admission_v4 as existing
from core_runtime import native_pack_onboarding as onboarding
from core_runtime import native_pack_onboarding_http as native_http
from core_runtime.active_profile_store_v4 import ActiveProfileStore
from core_runtime.bootstrap.profile_capture import runtime_user_data_scope
from core_runtime.bundled_artifact_retention_v4 import (
    BundledArtifactRetentionDenied,
    capture_retention_origin,
)
from core_runtime.bundled_host_baseline_v4 import (
    RetainedBundledHostBaseline,
    current_bundled_host_baseline_read,
)
from core_runtime.host_extension_authoring import build_host_extension_successor
from core_runtime.native_pack_key_authorization import NativePackKeyVersionCeremony
from core_runtime.pack_artifact_integrity import (
    read_host_policy_snapshot,
    write_host_install_record,
)
from core_runtime.pack_signature import (
    SIGNED_MANIFEST_RELATIVE,
    build_signed_manifest,
    sign_manifest,
)
from core_runtime.profile_pack_versions import (
    profile_pack_versions,
    select_profile_pack_version,
)
from tests.test_bundled_artifact_retention_v4 import PACK_ID, make_retention_fixture
from tests.test_bundled_host_baseline_v4 import _publish
from tests.test_host_extension_dispatch_update_v4 import (
    _approve_gateway_profile_revision,
)
from tests.test_host_extension_update_v4 import _refresh
from tests.test_live_production_v4_dispatch import (
    _deny_production_test_internet,  # noqa: F401 - explicitly reused fixture
)
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_protocol.canonical import canonical_digest, canonical_json
from tobkiri_protocol.validation import validate_file

pytestmark = [
    pytest.mark.usefixtures("_deny_production_test_internet"),
    pytest.mark.skipif(os.name == "nt", reason="POSIX immutable baseline publication"),
]


def _stage_baseline(
    published: RetainedBundledHostBaseline,
    target: Path,
) -> Path:
    """Stage every verified public byte with normal caller-owned authoring modes."""
    before = {
        path.relative_to(published.root).as_posix(): path.read_bytes()
        for path in published.root.rglob("*")
        if path.is_file()
    }
    source_modes = {
        path.relative_to(published.root).as_posix(): path.stat().st_mode
        for path in published.root.rglob("*")
        if path.is_file()
    }
    shutil.copytree(published.root, target, copy_function=shutil.copyfile)
    for path in target.rglob("*"):
        if path.is_file():
            path.chmod(0o644)  # Normalize only the new caller-owned public input.
    after = {
        path.relative_to(target).as_posix(): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert source_modes == {
        path.relative_to(published.root).as_posix(): path.stat().st_mode
        for path in published.root.rglob("*")
        if path.is_file()
    }
    assert all(
        path.stat().st_mode & 0o777 == 0o644
        for path in target.rglob("*")
        if path.is_file()
    )
    assert compile_pack_root(target).artifact.digest == published.artifact_digest
    assert compile_pack_root(published.root).artifact.digest == published.artifact_digest
    return target


def _release(previous: Path, target: Path, version: str, key: Any) -> Path:
    source = build_host_extension_successor(
        target,
        predecessor_root=previous,
        expected_predecessor_digest=compile_pack_root(previous).artifact.digest,
        version=version,
        replacements={
            "runtime/adapter.py": (previous / "runtime/adapter.py").read_bytes()
            + b"\n# isolated existing-slot successor\n"
        },
    )
    manifest = validate_file(source / "pack.v4.json", "pack")
    executable = validate_file(source / "executables.v4.json", "executable_catalog")
    signed = sign_manifest(
        build_signed_manifest(
            source,
            pack_id=PACK_ID,
            version=version,
            publisher_id="tobkiri.repository",
            core_compatibility=">=0",
            contract_versions={
                op["contract_id"]: op["contract_version"]
                for variant in executable["variants"]
                for op in variant["operations"]
            },
            requested_capabilities=manifest["requirements"]["capabilities"],
        ),
        key,
    )
    path = source / SIGNED_MANIFEST_RELATIVE
    path.parent.mkdir()
    path.write_bytes(canonical_json(signed))
    return source


def _install_policy(source: Path, trust: Path, key: Any) -> None:
    signed = json.loads((source / SIGNED_MANIFEST_RELATIVE).read_bytes())
    write_host_install_record(
        trust,
        pack_id=PACK_ID,
        install_path=source,
        record={
            "signature_required": True,
            "publisher_id": signed["publisher_id"],
            "key_id": signed["signature"]["key_id"],
            "installed_version": signed["version"],
            "signed_manifest_path": SIGNED_MANIFEST_RELATIVE,
            "contract_versions": signed["contract_versions"],
            "requested_capabilities": signed["requested_capabilities"],
        },
        publisher_record={
            "public_key_pem": key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode(),
            "allowed_pack_namespaces": [PACK_ID],
            "revoked_key_ids": [],
        },
    )


@pytest.fixture
def owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    fixture = make_retention_fixture(tmp_path, monkeypatch)
    cache = _publish(fixture)
    key = Ed25519PrivateKey.generate()
    selected = _release(
        _stage_baseline(cache, tmp_path / "sdk-baseline-input"),
        tmp_path / "selected",
        "99.0.0",
        key,
    )
    trust = tmp_path / "host-policy/publisher-trust.json"
    trust.parent.mkdir(mode=0o700)
    _install_policy(selected, trust, key)
    admitted = external.admit_signed_pack_version(
        selected,
        trust_store_path=trust,
        expected_predecessor_digest=fixture.pin,
        expected_catalog_revision=external.control_catalog_revision(),
    )
    view = profile_pack_versions("defaults")
    current = next(row for row in view["packs"] if row["pack_id"] == PACK_ID)
    select_profile_pack_version(
        {
            "profile_id": "defaults",
            "pack_id": PACK_ID,
            "artifact_digest": admitted["artifact_digest"],
            "expected_selected_digest": current["selected_digest"],
            "expected_profile_revision": view["profile_definition_revision"],
            "expected_store_generation": view["store_generation"],
            "expected_catalog_revision": view["catalog_revision"],
        }
    )
    _approve_gateway_profile_revision(fixture.owner, "defaults")
    origin = capture_retention_origin(fixture.owner)
    candidate = _release(selected, tmp_path / "candidate", "100.0.0", key)
    public = tmp_path / "publisher.pem"
    public.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    return SimpleNamespace(
        fixture=fixture,
        origin=origin,
        cache=cache,
        selected=selected,
        admitted=admitted,
        candidate=candidate,
        public=public,
        trust=trust,
        key=key,
        root=tmp_path,
    )


def _preview(owner: Any) -> dict[str, Any]:
    return onboarding.preview_signed_existing_host_extension(
        owner.candidate,
        owner.public,
        origin=owner.origin,
        bundle_root=owner.fixture.bundle,
        trust_store_path=owner.trust,
    )


def _commit(owner: Any, preview: dict[str, Any]) -> dict[str, Any]:
    return onboarding.commit_signed_existing_host_extension(
        owner.candidate,
        owner.public,
        origin=owner.origin,
        bundle_root=owner.fixture.bundle,
        trust_store_path=owner.trust,
        expected_preview_digest=preview["preview_digest"],
    )


def test_dual_frozen_and_selected_proof_survives_source_drift_without_ambient_scope(
    owner: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real SDK preserves historical Contracts and leaves selection untouched."""
    import shutil

    drift = owner.root / "drift"
    shutil.copytree(owner.fixture.source, drift)
    contracts = json.loads((drift / "contracts.v4.json").read_bytes())
    contracts["contracts"][0]["provenance"]["source_digest"] = "sha256:" + "f" * 64
    (drift / "contracts.v4.json").write_bytes(canonical_json(contracts))
    _refresh(drift)
    original = external.resolve_pack_root
    monkeypatch.setattr(
        external,
        "resolve_pack_root",
        lambda pack_id: drift if pack_id == PACK_ID else original(pack_id),
    )
    with pytest.raises(external.ExternalPackCatalogDenied):
        onboarding.preview_signed_pack(owner.candidate, owner.public)
    observed: list[str] = []
    verifier = existing.verify_host_extension_successor

    def verify(*args: Any, **kwargs: Any) -> Any:
        assert current_bundled_host_baseline_read() is None
        observed.append(kwargs["expected_predecessor_digest"])
        return verifier(*args, **kwargs)

    monkeypatch.setattr(existing, "verify_host_extension_successor", verify)
    pointer = ActiveProfileStore(owner.fixture.owner).path.read_bytes()
    preview = _preview(owner)
    assert preview["predecessor_artifact_digest"] == owner.admitted["artifact_digest"]
    assert owner.fixture.pin in observed and owner.admitted["artifact_digest"] in observed
    assert set(preview) == {
        "pack_id",
        "version",
        "publisher_id",
        "key_id",
        "key_fingerprint",
        "requested_capabilities",
        "contract_versions",
        "artifact_digest",
        "kind",
        "execution_boundary",
        "predecessor_artifact_digest",
        "catalog_revision",
        "preview_digest",
    }
    admitted = _commit(owner, preview)
    assert admitted["state"] == "committed" and not admitted.get("reconciliation_needed")
    assert ActiveProfileStore(owner.fixture.owner).path.read_bytes() == pointer
    assert dict(owner.origin.pack_pins)[PACK_ID] == owner.admitted["artifact_digest"]
    assert current_bundled_host_baseline_read() is None
    with pytest.raises(external.ExternalPackCatalogDenied):
        external.load_external_pack_catalog()


def test_proof_denies_forged_foreign_closed_thread_and_new_slots(
    owner: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authenticated slot evidence cannot escape its owner or lexical scope."""
    for forged in (
        replace(owner.origin, pack_pins=((PACK_ID, owner.fixture.pin),)),
        replace(owner.origin, security_epoch=999),
    ):
        with pytest.raises(BundledArtifactRetentionDenied):
            onboarding.preview_signed_existing_host_extension(
                owner.candidate,
                owner.public,
                origin=forged,
                bundle_root=owner.fixture.bundle,
                trust_store_path=owner.trust,
            )
    with existing._existing_slot_proof(
        owner.origin,
        bundle_root=owner.fixture.bundle,
        pack_id=PACK_ID,
    ) as proof:
        failures: list[Exception] = []

        def foreign_thread() -> None:
            try:
                proof.resolve(PACK_ID)
            except Exception as error:
                failures.append(error)

        thread = Thread(target=foreign_thread)
        thread.start()
        thread.join()
        assert len(failures) == 1 and "foreign" in str(failures[0])
        with monkeypatch.context() as patch:
            foreign = owner.root / "foreign-owner"
            foreign.mkdir()

            def forbid_authority(_origin: Any) -> None:
                raise AssertionError("foreign owner reached Authority")

            patch.setattr(existing.baseline, "assert_retention_origin_current", forbid_authority)
            with (
                runtime_user_data_scope(foreign),
                pytest.raises(BundledArtifactRetentionDenied, match="another Host owner"),
            ):
                proof.validate()
    with pytest.raises(ValueError, match="closed"):
        proof.validate()
    with pytest.raises(ValueError, match="unknown"):
        with existing._existing_slot_proof(
            owner.origin,
            bundle_root=owner.fixture.bundle,
            pack_id="unknown.host",
        ):
            pass
    # An Origin from the earlier activation cannot authorize this slot.
    with pytest.raises(BundledArtifactRetentionDenied, match="pointer"):
        with existing._existing_slot_proof(
            owner.fixture.origin,
            bundle_root=owner.fixture.bundle,
            pack_id=PACK_ID,
        ):
            pass


def test_initial_bundled_slot_is_not_an_existing_admitted_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = make_retention_fixture(tmp_path, monkeypatch)
    _publish(fixture)
    with pytest.raises(ValueError, match="already admitted"):
        with existing._existing_slot_proof(
            fixture.origin,
            bundle_root=fixture.bundle,
            pack_id=PACK_ID,
        ):
            pass


def test_unselected_newer_revision_is_a_version_floor(owner: Any) -> None:
    """A lower active pin cannot hide a newer authenticated retained revision."""
    higher = _release(owner.selected, owner.root / "higher", "101.0.0", owner.key)
    _install_policy(higher, owner.trust, owner.key)
    external.admit_signed_pack_version(
        higher,
        trust_store_path=owner.trust,
        expected_predecessor_digest=owner.admitted["artifact_digest"],
        expected_catalog_revision=external.control_catalog_revision(),
    )
    with pytest.raises(ValueError, match="all retained versions"):
        _preview(owner)


def test_raw_signed_manifest_and_contract_change_invalidate_review(owner: Any) -> None:
    """Even equivalent envelope whitespace cannot reuse an exact review."""
    preview = _preview(owner)
    before = owner.trust.read_bytes()
    path = owner.candidate / SIGNED_MANIFEST_RELATIVE
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="confirmation"):
        _commit(owner, preview)
    assert owner.trust.read_bytes() == before
    contracts = json.loads((owner.candidate / "contracts.v4.json").read_bytes())
    contracts["contracts"][0]["provenance"]["source_digest"] = "sha256:" + "f" * 64
    (owner.candidate / "contracts.v4.json").write_bytes(canonical_json(contracts))
    _refresh(owner.candidate)
    with pytest.raises(ValueError):
        _preview(owner)


def test_late_cache_failure_reports_committed_reconciliation_needed(owner: Any) -> None:
    """An actual cache mutation after commit cannot manufacture a rollback."""
    _install_policy(owner.candidate, owner.trust, owner.key)
    signed = json.loads((owner.candidate / SIGNED_MANIFEST_RELATIVE).read_bytes())
    leaf = owner.cache.root / "pack.v4.json"
    original = leaf.read_bytes()

    def fail_after_publication(point: str) -> None:
        if point == "committed":
            leaf.chmod(0o600)
            leaf.write_bytes(original + b"\n")
            leaf.chmod(0o400)

    with existing._existing_slot_proof(
        owner.origin,
        bundle_root=owner.fixture.bundle,
        pack_id=PACK_ID,
    ) as proof:
        entry = proof.admit(
            owner.candidate,
            trust_store_path=owner.trust,
            expected_predecessor_digest=owner.admitted["artifact_digest"],
            expected_catalog_revision=proof.revision,
            expected_artifact_digest=compile_pack_root(owner.candidate).artifact.digest,
            expected_signed_manifest_digest=canonical_digest(signed),
            fault_injector=fail_after_publication,
        )
    assert entry["state"] == "committed" and entry["reconciliation_needed"] is True
    catalog = external._read_authenticated_catalog(
        external._catalog_path(owner.fixture.owner),
        allow_missing=False,
    )
    retained = next(
        row
        for row in catalog["entries"].values()
        if row["artifact_digest"] == entry["artifact_digest"]
    )
    assert retained["state"] == "committed" and "reconciliation_needed" not in retained


class _NativeHandler:
    def __init__(self, owner: Any, body: dict[str, Any], ceremony: Any) -> None:
        self.headers: dict[str, str] = {}
        self.body = body
        self.owner = owner
        self._native_pack_key_ceremony = ceremony
        self._runtime_refresh = None
        self.status = 0
        self.data: Any = None

    def _native_pack_launcher_authenticated(self) -> bool:
        return True

    def _native_pack_trust_store(self) -> Path:
        return self.owner.trust

    def _parse_object_body(self) -> dict[str, Any]:
        return self.body

    def _discard_request_body(self) -> None:
        pass

    def _send_response(self, response: Any, status: int = 200) -> None:
        self.status = status
        self.data = response.data
        self.response = response


def test_native_v1_exact_new_key_requires_existing_normal_review(owner: Any) -> None:
    """The same wire format authorizes one exact canary tuple, never an echo flag."""
    new_key = Ed25519PrivateKey.generate()
    owner.candidate = _release(owner.selected, owner.root / "new-key-candidate", "100.0.0", new_key)
    owner.public = owner.root / "new-key-public.pem"
    owner.public.write_bytes(
        new_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    ceremony = NativePackKeyVersionCeremony()
    fields = {"source_root": str(owner.candidate), "public_key_path": str(owner.public)}
    handler = _NativeHandler(owner, fields, ceremony)
    native_http.handle_native_pack_onboarding(handler, commit=False)
    assert handler.status == 200
    preview = handler.data
    section = preview["key_version_authorization"]
    assert section["predecessor_artifact_digest"] == owner.admitted["artifact_digest"]
    policy_before = owner.trust.read_bytes()
    denied = _NativeHandler(
        owner, {**fields, "preview_digest": preview["preview_digest"]}, ceremony
    )
    native_http.handle_native_pack_onboarding(denied, commit=True)
    assert denied.status == 409 and owner.trust.read_bytes() == policy_before
    confirmed = _NativeHandler(
        owner,
        {
            **fields,
            "preview_digest": preview["preview_digest"],
            "key_version_authorization_challenge": section["challenge"],
            "key_version_authorization_digest": section["authorization_digest"],
        },
        ceremony,
    )
    pointer = ActiveProfileStore(owner.fixture.owner).path.read_bytes()
    native_http.handle_native_pack_onboarding(confirmed, commit=True)
    assert confirmed.status == 200
    assert confirmed.data["artifact_digest"] == preview["artifact_digest"]
    assert ActiveProfileStore(owner.fixture.owner).path.read_bytes() == pointer
    policy = read_host_policy_snapshot(owner.trust)
    assert policy["publishers"]["tobkiri.repository"]["allowed_pack_namespaces"] == [PACK_ID]
    scopes = policy["publisher_key_versions"]["publishers"]["tobkiri.repository"]
    assert len(scopes[preview["key_id"]]["scopes"]) == 1
    replay = _NativeHandler(owner, confirmed.body, ceremony)
    native_http.handle_native_pack_onboarding(replay, commit=True)
    assert replay.status == 409


def test_atomic_writer_failure_after_commit_preserves_committed_cas(
    owner: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The atomic writer may fail after bytes are durable but before returning."""
    preview = _preview(owner)
    writer = external._write_authenticated_catalog

    def write_then_fail(path: Path, entries: Any, journal: Any) -> None:
        writer(path, entries, journal)
        if any(
            row.get("state") == "committed" and row.get("version_string") == "100.0.0"
            for row in entries.values()
        ):
            raise OSError("isolated postwrite fault")

    monkeypatch.setattr(external, "_write_authenticated_catalog", write_then_fail)
    entry = _commit(owner, preview)
    assert entry["state"] == "committed" and entry["reconciliation_needed"] is True
    assert external._entry_root(entry, owner.fixture.owner).is_dir()


def test_unprovable_atomic_writer_outcome_preserves_cas_for_reconciliation(
    owner: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable evidence cannot justify destructive rollback or a denied claim."""
    preview = _preview(owner)
    writer = external._write_authenticated_catalog

    def write_then_corrupt(path: Path, entries: Any, journal: Any) -> None:
        writer(path, entries, journal)
        if any(
            row.get("state") == "committed" and row.get("version_string") == "100.0.0"
            for row in entries.values()
        ):
            path.write_bytes(b"invalid isolated catalog evidence")
            raise OSError("isolated unproved postwrite fault")

    monkeypatch.setattr(external, "_write_authenticated_catalog", write_then_corrupt)
    with pytest.raises(external.ExternalPackAdmissionOutcomeUnknown):
        _commit(owner, preview)
    digest = preview["artifact_digest"].removeprefix("sha256:")
    assert (external._artifact_store(owner.fixture.owner) / digest / PACK_ID).is_dir()


@pytest.mark.parametrize("reader", ["signature", "public-key"])
def test_native_selection_reader_rejects_real_fifo_without_blocking(
    tmp_path: Path,
    reader: str,
) -> None:
    """Untrusted Native-selected files cannot stall before normal verification."""
    import subprocess
    import sys

    path = tmp_path / "canary-fifo"
    os.mkfifo(path)
    pack = tmp_path / "pack"
    pack.mkdir()
    code = """
import sys
from pathlib import Path
from core_runtime.external_pack_catalog_v4 import _read_json_nofollow
from core_runtime.native_pack_onboarding import _read_public_key
try:
    if sys.argv[1] == 'signature':
        _read_json_nofollow(Path(sys.argv[2]), 1024)
    else:
        _read_public_key(Path(sys.argv[2]), Path(sys.argv[3]))
except (ValueError, RuntimeError):
    pass
else:
    raise SystemExit(1)
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", code, reader, str(path), str(pack)],
        timeout=5,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()


@pytest.mark.parametrize("mutation", ["pointer", "policy"])
def test_precommit_mutation_cannot_publish_a_stale_existing_slot(
    owner: Any,
    mutation: str,
) -> None:
    """Real isolated pointer/policy changes between promotion and commit deny."""
    from core_runtime import pack_artifact_integrity as integrity

    _install_policy(owner.candidate, owner.trust, owner.key)
    signed = json.loads((owner.candidate / SIGNED_MANIFEST_RELATIVE).read_bytes())
    catalog_path = external._catalog_path(owner.fixture.owner)
    catalog_before = catalog_path.read_bytes()

    def invalidate_after_promotion(point: str) -> None:
        if point != "promoted":
            return
        if mutation == "pointer":
            ActiveProfileStore(owner.fixture.owner).path.write_bytes(b"invalid isolated pointer")
        else:
            policy = read_host_policy_snapshot(owner.trust)
            policy["publishers"]["tobkiri.repository"]["revoked_key_ids"] = [
                signed["signature"]["key_id"]
            ]
            policy["policy_generation"] += 1
            policy["policy_digest"] = integrity._policy_digest(policy)
            owner.trust.write_bytes(canonical_json(policy))

    with existing._existing_slot_proof(
        owner.origin,
        bundle_root=owner.fixture.bundle,
        pack_id=PACK_ID,
    ) as proof:
        with pytest.raises((ValueError, RuntimeError)):
            proof.admit(
                owner.candidate,
                trust_store_path=owner.trust,
                expected_predecessor_digest=owner.admitted["artifact_digest"],
                expected_catalog_revision=proof.revision,
                expected_artifact_digest=compile_pack_root(owner.candidate).artifact.digest,
                expected_signed_manifest_digest=canonical_digest(signed),
                fault_injector=invalidate_after_promotion,
            )
    assert catalog_path.read_bytes() == catalog_before
    digest = compile_pack_root(owner.candidate).artifact.digest.removeprefix("sha256:")
    assert not (external._artifact_store(owner.fixture.owner) / digest / PACK_ID).exists()


def test_consumption_rejects_unissued_duck_subclass_and_mutated_proofs(
    owner: Any,
) -> None:
    """Only the exact issued immutable capture reaches admission or Authority."""
    fake = SimpleNamespace(verify_candidate=lambda *_: None, _snapshot=SimpleNamespace(versions={}))
    unissued = object.__new__(existing._ExistingHostExtensionProof)
    for value in (fake, unissued):
        with pytest.raises(ValueError, match="issued proof"):
            existing._require_issued_existing_slot_proof(value)
        with pytest.raises(ValueError, match="issued proof"):
            external._admit_signed_external_pack_locked(
                Path("/nonexistent-proof-must-stop-before-source"),
                trust_store_path=owner.trust,
                user_data=owner.fixture.owner,
                catalog_path=external._catalog_path(owner.fixture.owner),
                trust_policy_lock=None,
                fault_injector=None,
                existing_slot_proof=value,
            )

    class Subclass(existing._ExistingHostExtensionProof):
        pass

    with pytest.raises(ValueError, match="issued"):
        Subclass(existing._TOKEN, owner.origin, owner.fixture.bundle, PACK_ID)
    with existing._existing_slot_proof(
        owner.origin,
        bundle_root=owner.fixture.bundle,
        pack_id=PACK_ID,
    ) as proof:
        with pytest.raises(AttributeError):
            proof.verify_candidate = lambda *_: None
        for field, invalid in (
            ("pack_id", "other.host"),
            ("predecessor_digest", owner.fixture.pin),
            ("_selected_root", owner.candidate),
            ("revision", "sha256:" + "0" * 64),
        ):
            original = getattr(proof, field)
            setattr(proof, field, invalid)
            with pytest.raises(ValueError, match="captured proof changed"):
                existing._require_issued_existing_slot_proof(proof)
            setattr(proof, field, original)
        assert existing._require_issued_existing_slot_proof(proof) is proof


def test_resolve_cannot_reseal_mutation_during_stored_verification(
    owner: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reentrant cache verifier cannot launder changes to the issued capture."""
    with existing._existing_slot_proof(
        owner.origin,
        bundle_root=owner.fixture.bundle,
        pack_id=PACK_ID,
    ) as proof:
        original = existing.baseline._verify_stored
        revision = proof.revision

        def mutate_after_verification(*args: Any, **kwargs: Any) -> Path:
            root = original(*args, **kwargs)
            proof.revision = "sha256:" + "0" * 64
            return root

        monkeypatch.setattr(existing.baseline, "_verify_stored", mutate_after_verification)
        with pytest.raises(ValueError, match="captured proof changed"):
            proof.resolve(PACK_ID)
        proof.revision = revision
        assert existing._require_issued_existing_slot_proof(proof) is proof


def test_captured_compile_rejects_actual_postcapture_metadata_fifo(owner: Any) -> None:
    """Every dedicated compiler callback uses bounded nonblocking capture."""
    import subprocess
    import sys

    script = """
import os, sys
from pathlib import Path
from core_runtime import host_extension_update_v4 as update
from core_runtime.existing_host_extension_admission_v4 import _compile_captured_pack
original = update.compile_pack_root
root = Path(sys.argv[1])
def injected(path, **kwargs):
    assert kwargs.get('read_artifact') is not None
    leaf = root / 'pack.v4.json'
    leaf.unlink()
    os.mkfifo(leaf)
    return original(path, **kwargs)
update.compile_pack_root = injected
try:
    _compile_captured_pack(root)
except (OSError, ValueError, RuntimeError):
    pass
else:
    raise SystemExit(1)
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(owner.candidate)],
        timeout=5,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()


def test_committed_reconciliation_is_visible_to_frozen_native_v1(
    owner: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1 ignores error on success; committed reconciliation must use its error path."""
    entry = {**owner.admitted, "reconciliation_needed": True}
    monkeypatch.setattr(onboarding, "commit_native_signed_pack", lambda *_a, **_k: entry)
    handler = _NativeHandler(
        owner,
        {
            "source_root": str(owner.candidate),
            "public_key_path": str(owner.public),
            "preview_digest": "sha256:" + "a" * 64,
        },
        None,
    )
    native_http.handle_native_pack_onboarding(handler, commit=True)
    # Model lib.rs: error is consumed only for failed HTTP or failed API envelope.
    envelope = handler.response
    if not (200 <= handler.status < 300) or not envelope.success:
        native_error = envelope.error
    else:
        native_error = None  # Native v1 discards envelope.error in this branch.
    assert handler.status == 409
    assert native_error == "Pack admission committed; catalog reconciliation is required"
    assert envelope.data["artifact_digest"] == entry["artifact_digest"]


def test_native_first_known_bundled_update_retains_canonical_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first bundled Host update still uses the existing canonical producer port."""
    fixture = make_retention_fixture(tmp_path, monkeypatch)
    cache = _publish(fixture)
    key = Ed25519PrivateKey.generate()
    candidate = _release(
        _stage_baseline(cache, tmp_path / "sdk-baseline-input"),
        tmp_path / "first-successor",
        "99.0.0",
        key,
    )
    public = tmp_path / "publisher.pem"
    public.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    trust = tmp_path / "policy/publisher-trust.json"
    trust.parent.mkdir(mode=0o700)
    assert onboarding._native_existing_slot(candidate) is False
    preview = onboarding.preview_native_signed_pack(
        candidate,
        public,
        trust_store_path=trust,
    )
    assert preview["predecessor_artifact_digest"] == fixture.pin
    entry = onboarding.commit_native_signed_pack(
        candidate,
        public,
        trust_store_path=trust,
        expected_preview_digest=preview["preview_digest"],
    )
    assert entry["artifact_digest"] == preview["artifact_digest"]
    assert capture_retention_origin(fixture.owner).pack_pins == fixture.origin.pack_pins
    assert current_bundled_host_baseline_read() is None


def test_native_unknown_host_slot_denies_before_generic_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown Host slot cannot enter either canonical or frozen Native path."""
    source = tmp_path / "unknown-host"
    source.mkdir()
    (source / "pack.v4.json").write_bytes(
        canonical_json(
            {
                "pack": {"id": "new.unissued.host", "kind": "host_extension"},
            }
        )
    )

    def forbidden_generic(*args: Any, **kwargs: Any) -> None:
        pytest.fail("unknown Host slot reached generic admission")

    monkeypatch.setattr(onboarding, "preview_signed_pack", forbidden_generic)
    with pytest.raises(ValueError, match="slot is unknown"):
        onboarding.preview_native_signed_pack(source, tmp_path / "key.pem")


def test_native_existing_slot_never_falls_back_after_trust_denial(
    owner: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active admitted slot cannot downgrade routing when exact trust denies."""
    from core_runtime import pack_artifact_integrity as integrity

    signed = json.loads((owner.selected / SIGNED_MANIFEST_RELATIVE).read_bytes())
    policy = read_host_policy_snapshot(owner.trust)
    policy["publishers"]["tobkiri.repository"]["revoked_key_ids"] = [signed["signature"]["key_id"]]
    policy["policy_generation"] += 1
    policy["policy_digest"] = integrity._policy_digest(policy)
    owner.trust.write_bytes(canonical_json(policy))

    def forbidden_generic(*args: Any, **kwargs: Any) -> None:
        pytest.fail("selected admitted Host slot must never fall back to generic")

    monkeypatch.setattr(onboarding, "preview_signed_pack", forbidden_generic)
    with pytest.raises((ValueError, RuntimeError)):
        onboarding.preview_native_signed_pack(
            owner.candidate,
            owner.public,
            trust_store_path=owner.trust,
        )


def test_closed_or_foreign_proof_cannot_touch_admission_locks(
    owner: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The issued-current owner guard precedes even policy/catalog lock lookup."""

    def forbidden_lock(*args: Any, **kwargs: Any) -> None:
        pytest.fail("invalid proof touched admission lock paths")

    monkeypatch.setattr(external, "_admission_policy_locks", forbidden_lock)
    with existing._existing_slot_proof(
        owner.origin,
        bundle_root=owner.fixture.bundle,
        pack_id=PACK_ID,
    ) as proof:
        with runtime_user_data_scope(owner.root / "foreign-owner"):
            with pytest.raises((ValueError, RuntimeError)):
                proof.admit(owner.candidate)
    with pytest.raises(ValueError, match="issued proof"):
        proof.admit(owner.candidate)


def test_source_known_but_frozen_absent_host_slot_cannot_use_generic_native(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutable Source knowledge cannot create a new frozen Native Host slot."""
    make_retention_fixture(tmp_path, monkeypatch)
    pack_id = "new.source.only.host"
    source = tmp_path / "source-only-host"
    source.mkdir()
    (source / "pack.v4.json").write_bytes(
        canonical_json(
            {
                "pack": {"id": pack_id, "kind": "host_extension"},
            }
        )
    )
    catalog = onboarding.load_pack_catalog()
    monkeypatch.setattr(
        onboarding,
        "load_pack_catalog",
        lambda: {
            **catalog,
            pack_id: {"pack_id": pack_id, "kind": "host_extension"},
        },
    )

    def forbidden_generic(*args: Any, **kwargs: Any) -> None:
        pytest.fail("new Source-only Host slot reached canonical Native admission")

    monkeypatch.setattr(onboarding, "preview_signed_pack", forbidden_generic)
    with pytest.raises((ValueError, RuntimeError)):
        onboarding.preview_native_signed_pack(source, tmp_path / "key.pem")
