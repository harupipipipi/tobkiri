"""Counted reuse and mutation denial for one physical catalog read."""

from __future__ import annotations

import json
import shutil
import copy
import sys
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context, copy_context
from pathlib import Path
from typing import Iterator

import pytest

from core_runtime import _pack_catalog_read as metadata
from core_runtime import admitted_pack_read_v4 as roots_module
from core_runtime import external_pack_catalog_v4 as external
from core_runtime import pack_control_v4 as control
from core_runtime.bootstrap.profile_capture import runtime_user_data_scope
from tests.test_external_pack_catalog_v4 import PACK_ID, _signed_external_pack
from tests.test_bundled_artifact_retention_v4 import (
    make_retention_fixture,
    retain_fixture,
)


@pytest.fixture
def admitted(tmp_path: Path) -> Iterator[tuple[Path, dict]]:
    owner = tmp_path / "owner"
    owner.mkdir(mode=0o700)
    with runtime_user_data_scope(owner):
        source, policy = _signed_external_pack(tmp_path / "supplier")
        entry = external.admit_signed_external_pack(source, trust_store_path=policy)
        shutil.copy2(policy, owner / "pack_control/publisher_trust.v4.json")
        yield owner, dict(entry)


def test_metadata_scope_performs_two_full_walks_for_many_lookups(
    admitted,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, entry = admitted
    original = roots_module.load_external_pack_catalog
    calls = []

    def counted(pins=None):
        calls.append(dict(pins or {}))
        return original(pins)

    monkeypatch.setattr(roots_module, "load_external_pack_catalog", counted)
    expected = external.load_admitted_pack_catalog()
    revision = external.control_catalog_revision()
    with metadata._catalog_metadata_read(
        owner, {PACK_ID: entry["artifact_digest"]}
    ) as read:
        for _ in range(5):
            assert read.catalog() == expected
            assert read.revision == revision
            assert read.resolve(PACK_ID) == external._entry_root(entry, owner)
        detached = read.catalog()
        detached[PACK_ID]["version"] = "untrusted"
        assert read.catalog() == expected
    assert len(calls) == 2
    for getter in (read.catalog, lambda: read.revision, lambda: read.resolve(PACK_ID)):
        with pytest.raises(external.ExternalPackCatalogDenied, match="closed"):
            getter()


def test_arbitrary_snapshot_cannot_issue_metadata_scope() -> None:
    with pytest.raises(external.ExternalPackCatalogDenied, match="internally issued"):
        metadata._CatalogRead(object(), None, {}, {}, Path("/unused"), None)


@pytest.mark.parametrize(
    "borrow", ["clone", "nested", "thread", "copied", "context", "other_scope"]
)
def test_open_scope_rejects_borrowing_without_poisoning_original(
    admitted, borrow: str,
) -> None:
    owner, entry = admitted
    pins = {PACK_ID: entry["artifact_digest"]}
    with metadata._catalog_metadata_read(owner, pins) as read:
        expected = read.catalog()
        with pytest.raises(external.ExternalPackCatalogDenied):
            if borrow == "clone":
                copy.copy(read).catalog()
            elif borrow == "nested":
                with metadata._catalog_metadata_read(owner, pins):
                    pytest.fail("nested scope must never be issued")
            elif borrow == "context":
                Context().run(read.catalog)
            else:
                with ThreadPoolExecutor(max_workers=1) as worker:
                    context = copy_context()

                    def peer() -> None:
                        with runtime_user_data_scope(owner):
                            with metadata._catalog_metadata_read(owner, pins) as other:
                                assert other.catalog() == expected
                                read.catalog()

                    call = (
                        peer if borrow == "other_scope" else read.catalog
                        if borrow == "thread" else lambda: context.run(read.catalog)
                    )
                    worker.submit(call).result()
        assert read.catalog() == expected
    with metadata._catalog_metadata_read(owner, pins) as fresh:
        assert fresh.catalog() == expected


@pytest.mark.parametrize("field", ["records", "versions", "pins", "derived"])
def test_mutating_reused_metadata_denies_result(admitted, field: str) -> None:
    owner, entry = admitted
    with pytest.raises(external.ExternalPackCatalogDenied, match="changed"):
        with metadata._catalog_metadata_read(
            owner, {PACK_ID: entry["artifact_digest"]}
        ) as read:
            if field == "pins":
                read._roots._pins[PACK_ID] = "sha256:" + "f" * 64
            elif field == "derived":
                read._records = "{}"
            else:
                getattr(read._roots._snapshot, field)[PACK_ID] = {}
            read.catalog()


@pytest.mark.parametrize("mutation", ["cas", "catalog", "policy", "owner"])
def test_physical_scope_rejects_current_bytes_or_policy_drift(
    admitted,
    tmp_path: Path,
    mutation: str,
) -> None:
    owner, entry = admitted
    completed = False
    with pytest.raises((external.ExternalPackCatalogDenied, ValueError)):
        with metadata._catalog_metadata_read(
            owner, {PACK_ID: entry["artifact_digest"]}
        ) as read:
            root = read.resolve(PACK_ID)
            completed = False
            if mutation == "cas":
                path = root / "runtime/echo.py"
                path.chmod(0o600)
                path.write_bytes(path.read_bytes() + b"\n# changed\n")
                path.chmod(0o400)
            elif mutation == "catalog":
                path = external._catalog_path(owner)
                value = json.loads(path.read_bytes())
                value["signature"] = "0" * 64
                path.write_text(json.dumps(value))
            elif mutation == "policy":
                path = owner / "pack_control/publisher_trust.v4.json"
                value = json.loads(path.read_bytes())
                value["policy_digest"] = "sha256:" + "0" * 64
                path.write_text(json.dumps(value))
            else:
                other = tmp_path / "other-owner"
                other.mkdir(mode=0o700)
                with runtime_user_data_scope(other):
                    completed = True
                    read.catalog()
            completed = True
    assert completed, "mutation itself must complete, rather than fail due to seal mode"
    with pytest.raises(external.ExternalPackCatalogDenied):
        read.catalog()


def test_retained_metadata_scope_checks_full_bytes_and_epoch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core_runtime.authority.v4 import AuthorityStore

    fixture = make_retention_fixture(tmp_path, monkeypatch)
    artifact = retain_fixture(fixture)
    with runtime_user_data_scope(fixture.owner):
        with pytest.raises(external.ExternalPackCatalogDenied):
            with metadata._catalog_metadata_read(
                fixture.owner,
                dict(fixture.origin.pack_pins),
                origin=fixture.origin,
            ) as read:
                read.resolve("rumi_provider_adapters_pack")
                with AuthorityStore(
                    fixture.owner / "authority/v4.sqlite3"
                ) as authority:
                    authority.advance_security_epoch(
                        "isolated catalog drift regression"
                    )
        with pytest.raises(external.ExternalPackCatalogDenied):
            read.catalog()
    assert artifact.artifact_digest == fixture.pin


def test_reader_capture_and_payload_keep_dependency_closure_and_fresh_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property,
) -> None:
    fixture = make_retention_fixture(tmp_path, monkeypatch)
    from core_runtime.bootstrap.profile_capture import capture_active_profile

    active = capture_active_profile()
    original = roots_module.load_external_pack_catalog
    calls = []
    all_calls = []

    def all_counted(pins=None):
        all_calls.append(dict(pins or {}))
        return original(pins)

    # Replace every already imported alias, including those in dependency and
    # active-capture helpers, rather than just the physical scope's loader.
    for module in tuple(sys.modules.values()):
        if module is None or not getattr(module, "__name__", "").startswith("core_runtime"):
            continue
        for name, value in tuple(vars(module).items()):
            if value is original:
                monkeypatch.setattr(module, name, all_counted)

    def counted(pins=None):
        calls.append(dict(pins or {}))
        return all_counted(pins)

    monkeypatch.setattr(roots_module, "load_external_pack_catalog", counted)
    reader = control.capture_pack_catalog_reader()
    result = reader.read()
    assert len(calls) == 4
    assert len(all_calls) >= len(calls)
    record_property("scope_boundary_walks", len(calls))
    record_property("whole_reader_full_catalog_walks", len(all_calls))
    expected_required = control._required_profile_pack_ids(
        active.resolved.profile["profile_id"],
        active_snapshot=active,
    )
    projected = {row["pack_id"] for row in result["packs"]}
    assert {row["pack_id"] for row in result["packs"] if row["required"]} == (
        set(expected_required) & projected
    )
    assert "runtime.tauri.application.default" not in projected
    assert result["plan_digest"] == active.resolved.plan["plan_digest"]
    assert not (fixture.owner / "pack_control/.authority_key").exists()


@pytest.mark.parametrize("when", ["projection", "final_walk"])
def test_actual_optional_approval_revocation_never_escapes_physical_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str,
) -> None:
    from core_runtime.authority.v4 import AuthorityStore
    from core_runtime.bootstrap.profile_capture import capture_active_profile

    fixture = make_retention_fixture(tmp_path, monkeypatch)
    reader = control.capture_pack_catalog_reader()
    active = capture_active_profile()
    target = "rumi_model_evals_pack"
    record = control.load_pack_catalog()[target]
    assert target not in {
        row["identity"] for row in active.resolved.plan["effective_set"]
    }
    control._persist_approval(
        target, control._pack_snapshot(target, control.resolve_pack_root(target)),
        reader.binding, approval_nonce="isolated-inactive-approval",
    )
    path = control._approval_path(reader.binding.profile_id, target)
    original_bytes = path.read_bytes()
    approval = json.loads(original_bytes)
    activation = dict(active.activation)
    revoked = False
    projected = False

    def revoke() -> None:
        nonlocal revoked
        with AuthorityStore(fixture.owner / "authority/v4.sqlite3") as authority:
            epoch = authority.security_epoch
            _, grant_ids = authority.revoke_pack_approval(
                pack_id=target, approval_revision=approval["approval_revision"],
                profile_id=reader.binding.profile_id,
                activation_id=activation["activation_id"],
                artifact_digest=control._record_digest(record),
                reason="isolated physical-read regression",
            )
            assert grant_ids == ()
            assert authority.security_epoch == epoch
        revoked = True
        assert path.read_bytes() == original_bytes
        assert active.activation == activation

    original_status = control._approval_status

    def status(pack_id, *args, **kwargs):
        nonlocal projected
        result = original_status(pack_id, *args, **kwargs)
        if pack_id == target:
            assert result == (True, None)
            projected = True
            if when == "projection":
                revoke()
        return result

    monkeypatch.setattr(control, "_approval_status", status)
    if when == "final_walk":
        original_policy = metadata._policy

        def policy(root):
            if projected and not revoked:
                revoke()
            return original_policy(root)

        monkeypatch.setattr(metadata, "_policy", policy)
    with pytest.raises(control.PackControlStaleRevision):
        reader.read()
    assert projected and revoked
    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize("drift", ["active", "grant", "approval", "install"])
def test_payload_never_releases_mid_read_mutable_state_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    make_retention_fixture(tmp_path, monkeypatch)
    reader = control.capture_pack_catalog_reader()
    calls = 0
    if drift == "grant":

        def grants(_state, *, _inspection=None):
            nonlocal calls
            calls += 1
            _inspection["fresh_revocations"] = calls
            return set()

        monkeypatch.setattr(control, "_active_grant_bindings", grants)
    elif drift == "approval":

        def approvals(*_args):
            nonlocal calls
            calls += 1
            return str(calls)

        monkeypatch.setattr(control, "_approval_read_identity", approvals)
    elif drift == "install":

        def installed(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            return {} if calls == 1 else {"changed": {}}

        monkeypatch.setattr(control, "_read_control_state", installed)
    else:
        from core_runtime.bootstrap import profile_capture

        original = profile_capture.capture_active_profile

        def changed():
            nonlocal calls
            calls += 1
            value = original()
            if calls > 1:
                value.activation["fencing_token"] += 1
            return value

        monkeypatch.setattr(profile_capture, "capture_active_profile", changed)
    with pytest.raises(control.PackControlDenied):
        reader.read()
