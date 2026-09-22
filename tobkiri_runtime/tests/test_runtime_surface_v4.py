"""Typed Launcher runtime-surface tests over canonical Protocol v4 records."""

from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
from pathlib import Path
import threading
import time

import pytest
from jsonschema import Draft202012Validator

from core_runtime.bootstrap.profile_capture import (
    capture_default_profile,
    prepare_default_profile_confirmation,
    runtime_user_data_root,
)
from core_runtime.authority.v4 import AuthorityStore
from core_runtime.runtime_surface_v4 import (
    RUNTIME_SURFACE_API_VERSION,
    RuntimeSurfaceErrorCode,
    RuntimeProfileChangeService,
    RuntimeSurfaceError,
    RuntimeSurfaceService,
)
import core_runtime.runtime_surface_v4 as runtime_surface
from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog
from ecosystem.defaultspack.domain.runtime_v4 import ProfileResolutionDenied
from ecosystem.defaultspack.domain.runtime_v4 import ResolvedDefaultProfile
from tobkiri_protocol.canonical import canonical_digest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ROOT = RUNTIME_ROOT / "ecosystem" / "defaultspack" / "v4"


def _approve_profile_process(
    candidate_id: str,
    candidate_digest: str,
    session_id: str,
    results: multiprocessing.queues.Queue,
) -> None:
    try:
        approved = RuntimeProfileChangeService().approve(
            {
                "candidate_id": candidate_id,
                "candidate_digest": candidate_digest,
            },
            session_id=session_id,
        )
        results.put(
            (
                "approved",
                approved["approval_id"],
                approved["approval_digest"],
            )
        )
    except Exception as error:  # pragma: no cover - reported to parent assertion
        results.put(("error", type(error).__name__, str(error)))


def _bundle_root() -> Path:
    from tests.conformance_support.packaged_profile import packaged_profile_bundle_root

    return packaged_profile_bundle_root()


@pytest.fixture
def active_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    return capture_default_profile(confirmation=prepare_default_profile_confirmation())


def _service(active_runtime, **kwargs) -> RuntimeSurfaceService:
    return RuntimeSurfaceService(
        snapshot_loader=lambda: active_runtime,
        catalog_loader=lambda: BundledCatalog.load(_bundle_root()),
        **kwargs,
    )


def _control_output_schema() -> dict[str, object]:
    catalog = json.loads(
        (RUNTIME_ROOT / "ecosystem" / "tobkiri_host_pack_control" / "contracts.v4.json").read_text(
            encoding="utf-8"
        )
    )
    contract = next(
        item
        for item in catalog["contracts"]
        if item["contract_id"] == "tobkiri.host.control-presentation.v4"
    )
    digest = contract["operations"][0]["output_schema_digest"]
    return contract["schema_catalog"][digest]


def _capability_snapshot(active_runtime, operations) -> dict[str, object]:
    targets = [
        {
            "contribution_id": f"pack.{row['owner_pack_id']}.{row['operation_id']}",
            "contract_id": row["contract_id"],
            "operation_id": row["operation_id"],
            "provider_id": row["target_provider_id"],
            "function_id": row["function_id"],
            "artifact_digest": row["artifact_digest"],
            "owner_pack_id": row["owner_pack_id"],
        }
        for row in operations
    ]
    digest_targets = [
        {key: target[key] for key in target if key != "owner_pack_id"} for target in targets
    ]
    profile_id = str(active_runtime.resolved.profile["profile_id"])
    plan_digest = str(active_runtime.resolved.plan["plan_digest"])
    return {
        "profile_id": profile_id,
        "plan_digest": plan_digest,
        "catalog_hash": canonical_digest(
            {
                "profile_id": profile_id,
                "plan_digest": plan_digest,
                "contributions": digest_targets,
            }
        ),
        "targets": targets,
    }


def test_profile_read_model_is_derived_from_verified_v4_graph(active_runtime) -> None:
    result = _service(active_runtime).read_profile()
    Draft202012Validator(_control_output_schema()).validate(result)

    assert result["runtime_surface_api_version"] == RUNTIME_SURFACE_API_VERSION
    assert result["surface"] == "profile"
    assert result["state"] == "ready"
    assert set(result) == {
        "runtime_surface_api_version",
        "surface",
        "state",
        "profile_id",
        "profile_revision",
        "plan_digest",
        "catalog_revision",
        "records",
        "data",
    }
    data = result["data"]
    records = result["records"]
    assert data["profile"]["profile_id"] == "defaults"
    assert data["base"] == active_runtime.resolved.plan["base"]
    assert data["shell"] == active_runtime.resolved.plan["shell"]
    assert data["application"]["role"] == "application"
    assert records["profile_lock"]["digest"] == active_runtime.resolved.lock["lock_digest"]
    assert records["resolved_plan"]["digest"] == active_runtime.resolved.plan["plan_digest"]
    assert records["activation_record"]["digest"] == canonical_digest(active_runtime.activation)
    assert records["authority_snapshot"] == {
        "digest": active_runtime.resolved.profile["profile_authority_snapshot_digest"],
        "source_ref": (
            "authority-snapshot-v4://defaults/"
            + active_runtime.resolved.profile["profile_authority_snapshot_digest"]
        ),
    }
    assert all(
        set(record) == {"digest", "source_ref"}
        and "://" in record["source_ref"]
        and not record["source_ref"].startswith("file:")
        for record in records.values()
    )
    assert {item["pack_id"] for item in data["pack_closure"]} == {
        item["identity"] for item in active_runtime.resolved.lock["effective_set"]
    }
    assert all(
        set(binding)
        >= {
            "binding_id",
            "source_principal_id",
            "target_contract_id",
            "edge_digest",
        }
        for binding in data["resolved_wiring"]["bindings"]
    )


@pytest.mark.parametrize("view", ["packs", "contracts", "operations", "principals"])
def test_advanced_views_have_exact_named_payload(active_runtime, view: str) -> None:
    result = _service(active_runtime).read_advanced(view)

    assert result["surface"] == view
    assert result["state"] == "ready"
    assert isinstance(result["data"][view], list)
    assert set(result) == {
        "runtime_surface_api_version",
        "surface",
        "state",
        "profile_id",
        "profile_revision",
        "plan_digest",
        "catalog_revision",
        "records",
        "data",
    }


def test_operation_and_principal_views_are_resolved_plan_derived(active_runtime) -> None:
    service = _service(active_runtime)
    operations = service.read_advanced("operations")["data"]["operations"]
    principals = service.read_advanced("principals")["data"]["principals"]

    assert len(operations) == len(active_runtime.resolved.plan["bindings"])
    assert len(principals) == len(active_runtime.resolved.plan["bindings"])
    assert all(item["contract_revision_digest"].startswith("sha256:") for item in operations)
    assert all(item["parent_artifact_digest"].startswith("sha256:") for item in principals)
    assert all(
        set(item)
        >= {
            "owner_pack_id",
            "contribution_id",
            "invokable",
            "catalog_digest",
        }
        for item in operations
    )
    assert all(
        item["invokable"] is False for item in operations if item["domain_kind"] == "pack_vm"
    )
    verified = [item for item in operations if item["schema"].get("input_schema")]
    assert verified
    assert all(
        item["schema"]["input_schema_digest"].startswith("sha256:")
        and item["schema"]["output_schema_digest"].startswith("sha256:")
        and item["schema"]["error_schema_digest"].startswith("sha256:")
        and isinstance(item["schema"]["effect_ceiling"], list)
        and isinstance(item["schema"]["idempotency"], dict)
        for item in verified
    )


def test_contract_routes_are_exact_digest_pinned_broker_bindings(active_runtime) -> None:
    result = _service(active_runtime).read_advanced("contracts")
    routes = result["data"]["routes"]
    catalog = BundledCatalog.load(_bundle_root())
    application = catalog.packs["runtime.tauri.application.default"]
    map_digest = next(
        item["digest"]
        for item in application["artifacts"]
        if item["path"] == "defaultspack/frontend_contract_map.v4.json"
    )

    # The map has 23 logical routes and 32 exact route-to-target bindings.
    assert len(routes) == 32
    assert all(
        set(route)
        >= {
            "route_id",
            "method",
            "logical_target",
            "contract_id",
            "operation_id",
            "security",
            "provider_id",
            "function_id",
            "function_principal_id",
            "manifest_digest",
            "frontend_map_digest",
        }
        for route in routes
    )
    assert all(route["frontend_map_digest"] == map_digest for route in routes)
    assert all(route["security"]["broker_authority_required"] is True for route in routes)
    assert all(route["security"]["csrf_required"] is (route["method"] != "GET") for route in routes)
    serialized = json.dumps(routes)
    assert str(RUNTIME_ROOT) not in serialized
    assert "session_secret" not in serialized
    assert "cookie" not in serialized


def test_contract_route_principal_mismatch_fails_closed(active_runtime) -> None:
    from core_runtime.frontend_contract_routes import load_frontend_contract_bindings

    catalog = BundledCatalog.load(_bundle_root())
    bindings = load_frontend_contract_bindings(
        RUNTIME_ROOT
        / "ecosystem"
        / "defaultspack"
        / "defaultspack"
        / "frontend_contract_map.v4.json",
        catalog.packs["runtime.tauri.application.default"],
    )
    first = bindings[0]
    forged_target = replace(first.targets[0], provider_id="forged.provider")
    forged = (replace(first, targets=(forged_target,)), *bindings[1:])

    with pytest.raises(RuntimeSurfaceError) as denied:
        _service(
            active_runtime,
            frontend_contract_bindings=forged,
        ).read_advanced("contracts")

    assert denied.value.code == RuntimeSurfaceErrorCode.DIGEST_MISMATCH


def test_packvm_invocation_requires_fresh_matching_host_attestation(
    active_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unattested = _service(active_runtime).read_advanced("operations")["data"]["operations"]
    packvm_rows = [item for item in unattested if item["domain_kind"] == "pack_vm"]
    assert packvm_rows
    assert all(item["invokable"] is False for item in packvm_rows)

    lifecycle = runtime_surface._captured_lifecycle_projection()
    for pack in lifecycle["packs"]:
        for operation in pack["operations"]:
            operation["invokable"] = True
    monkeypatch.setattr(
        runtime_surface,
        "_captured_lifecycle_projection",
        lambda: lifecycle,
    )

    attested = {
        "version": 2,
        "backend_id": "tobkiri.python-pack-v4",
        "instance": "tobkiri-packvm-v4",
        "instance_machine_id": "machine.test",
        "instance_config_hash": "sha256:" + "1" * 64,
        "instance_directory_device": 3,
        "instance_directory_inode": 4,
        "config_digest": "sha256:" + "2" * 64,
        "image_digest": "sha256:" + "3" * 64,
        "image_source": "https://images.example.test/base.img",
        "image_local_device": 5,
        "image_local_inode": 6,
        "limactl_digest": "sha256:" + "4" * 64,
        "lima_home_digest": "sha256:" + "8" * 64,
        "lima_home_device": 1,
        "lima_home_inode": 2,
        "guest_runner_digest": "sha256:" + "5" * 64,
        "host_build_digest": "sha256:" + "6" * 64,
        "ceremony_nonce_digest": "sha256:" + "7" * 64,
        "session_digest": "sha256:" + "9" * 64,
        "plan_digest": "sha256:" + "a" * 64,
        "created_unix": int(time.time()) - 1,
    }
    snapshot = {
        **attested,
        "ready": True,
        "observed_unix": int(time.time()),
        "attestation_digest": canonical_digest(attested),
    }
    ready = _service(
        active_runtime,
        packvm_readiness_reader=lambda: snapshot,
        capability_binding_reader=lambda: _capability_snapshot(
            active_runtime,
            packvm_rows,
        ),
    ).read_advanced("operations")["data"]["operations"]
    ready_packvm = [item for item in ready if item["domain_kind"] == "pack_vm"]
    assert any(item["invokable"] is True for item in ready_packvm)

    stale = {**snapshot, "observed_unix": int(time.time()) - 31}
    stale_rows = _service(
        active_runtime,
        packvm_readiness_reader=lambda: stale,
    ).read_advanced("operations")["data"]["operations"]
    assert all(
        item["invokable"] is False for item in stale_rows if item["domain_kind"] == "pack_vm"
    )

    wrong = {**snapshot, "image_digest": "sha256:" + "8" * 64}
    wrong_rows = _service(
        active_runtime,
        packvm_readiness_reader=lambda: wrong,
    ).read_advanced("operations")["data"]["operations"]
    assert all(
        item["invokable"] is False for item in wrong_rows if item["domain_kind"] == "pack_vm"
    )


def test_pack_files_are_exact_manifest_artifacts(active_runtime) -> None:
    packs = _service(active_runtime).read_advanced("packs")["data"]["packs"]
    pack = next(item for item in packs if item["pack_id"] == "tobkiri_host_pack_control")

    assert {item["path"] for item in pack["artifacts"]} == {
        "executables.v4.json",
        "runtime/catalog.py",
    }
    artifact = next(item for item in pack["artifacts"] if item["path"] == "runtime/catalog.py")
    assert artifact["entry_id"].startswith("sha256:")
    assert artifact == {
        "entry_id": artifact["entry_id"],
        "owner_pack_id": "tobkiri_host_pack_control",
        "path": "runtime/catalog.py",
        "kind": "executable",
        "artifact_digest": "sha256:0d65cfd041a191408c1cabc98191647d950e0dd24c369c0a2bccdaa07049f0c7",
    }
    executable_catalog = next(
        item for item in pack["artifacts"] if item["path"] == "executables.v4.json"
    )
    assert executable_catalog["kind"] == "sidecar"
    assert executable_catalog["artifact_digest"].startswith("sha256:")
    payload = json.dumps(_service(active_runtime).read_profile())
    assert "/Users/" not in payload
    assert "authentication" not in payload
    assert all(
        not item["path"].startswith(("/", ".."))
        for selected in packs
        for item in selected["artifacts"]
    )


def test_settings_keep_user_and_runtime_profile_scopes_separate(active_runtime) -> None:
    result = _service(
        active_runtime,
        user_settings_reader=lambda: {"locale": "ja"},
    ).read_settings()

    assert result["surface"] == "settings"
    assert result["data"]["user_settings"] == {
        "scope": "user",
        "source": "launcher_local",
        "state": "available_from_explicit_adapter",
        "mutable_via_profile_activation": False,
        "values": {"locale": "ja"},
    }
    runtime = result["data"]["runtime_profile_settings"]
    assert runtime["scope"] == "runtime_profile"
    assert runtime["mutable_via_profile_activation"] is True
    assert runtime["plan_digest"] == active_runtime.resolved.plan["plan_digest"]

    unavailable = _service(active_runtime).read_settings()["data"]["user_settings"]
    assert unavailable == {
        "scope": "user",
        "source": "launcher_local",
        "state": "unavailable_from_runtime",
        "mutable_via_profile_activation": False,
    }


def test_expected_revision_and_digest_fail_closed(active_runtime) -> None:
    service = _service(active_runtime)

    with pytest.raises(RuntimeSurfaceError) as stale:
        service.read_profile(expected_profile_revision="sha256:" + "0" * 64)
    assert stale.value.code is RuntimeSurfaceErrorCode.STALE_REVISION

    with pytest.raises(RuntimeSurfaceError) as mismatch:
        service.read_profile(expected_plan_digest="sha256:" + "0" * 64)
    assert mismatch.value.code is RuntimeSurfaceErrorCode.DIGEST_MISMATCH


def test_catalog_artifact_mismatch_fails_closed(active_runtime) -> None:
    catalog = BundledCatalog.load(_bundle_root())
    pack_id = str(active_runtime.resolved.lock["effective_set"][0]["identity"])
    manifest = dict(catalog.packs[pack_id])
    manifest["pack"] = {
        **manifest["pack"],
        "artifact_digest": "sha256:" + "0" * 64,
    }
    mismatched = replace(catalog, packs={**catalog.packs, pack_id: manifest})
    service = RuntimeSurfaceService(
        snapshot_loader=lambda: active_runtime,
        catalog_loader=lambda: mismatched,
    )

    with pytest.raises(RuntimeSurfaceError) as error:
        service.read_profile()
    assert error.value.code is RuntimeSurfaceErrorCode.DIGEST_MISMATCH


def test_read_timeout_is_a_typed_fail_closed_error(active_runtime) -> None:
    ticks = iter((0.0, 6.0))
    service = _service(active_runtime, clock=lambda: next(ticks))

    with pytest.raises(RuntimeSurfaceError) as error:
        service.read_profile()
    assert error.value.code is RuntimeSurfaceErrorCode.TIMEOUT
    assert error.value.as_dict()["write_set"] == []


def test_blocked_loader_times_out_and_late_snapshot_is_not_adopted(active_runtime) -> None:
    entered = threading.Event()
    release = threading.Event()
    catalog_calls: list[str] = []

    def blocked_snapshot():
        entered.set()
        release.wait()
        return active_runtime

    def load_catalog_after_recording():
        catalog_calls.append("catalog")
        return BundledCatalog.load(_bundle_root())

    service = RuntimeSurfaceService(
        snapshot_loader=blocked_snapshot,
        catalog_loader=load_catalog_after_recording,
        read_timeout_seconds=0.1,
    )
    started = time.monotonic()
    with pytest.raises(RuntimeSurfaceError) as error:
        service.read_profile()
    elapsed = time.monotonic() - started

    assert entered.is_set()
    assert elapsed < 0.75
    assert error.value.code is RuntimeSurfaceErrorCode.TIMEOUT
    release.set()
    deadline = time.monotonic() + 1.0
    while runtime_surface._read_executor_stats()["admitted"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert catalog_calls == []
    service.close()


def test_timeout_saturation_is_bounded_and_recovers(active_runtime) -> None:
    release = threading.Event()
    entered_lock = threading.Lock()
    entered = 0
    catalog = BundledCatalog.load(_bundle_root())

    def blocked_snapshot():
        nonlocal entered
        with entered_lock:
            entered += 1
        release.wait()
        return active_runtime

    service = RuntimeSurfaceService(
        snapshot_loader=blocked_snapshot,
        catalog_loader=lambda: catalog,
        read_timeout_seconds=1.0,
    )
    stats = runtime_surface._read_executor_stats()
    requests = stats["capacity"] + 8

    def read_once() -> RuntimeSurfaceErrorCode | None:
        try:
            service.read_profile()
        except RuntimeSurfaceError as error:
            return error.code
        return None

    with ThreadPoolExecutor(max_workers=requests) as executor:
        results = list(executor.map(lambda _index: read_once(), range(requests)))

    after_timeout = runtime_surface._read_executor_stats()
    runtime_workers = [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith(runtime_surface._READ_WORKER_NAME_PREFIX)
    ]
    assert set(results) == {RuntimeSurfaceErrorCode.TIMEOUT}
    assert after_timeout["workers"] == stats["workers"]
    assert after_timeout["live_workers"] == stats["workers"]
    assert len(runtime_workers) == stats["workers"]
    assert entered <= stats["workers"]

    release.set()
    deadline = time.monotonic() + 2.0
    while runtime_surface._read_executor_stats()["admitted"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runtime_surface._read_executor_stats()["admitted"] == 0
    assert service.read_settings()["state"] == "ready"
    service.close()


def test_read_fence_cancels_waiter_and_service_remains_restartable(active_runtime) -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocked_snapshot():
        entered.set()
        release.wait()
        return active_runtime

    service = RuntimeSurfaceService(
        snapshot_loader=blocked_snapshot,
        catalog_loader=lambda: BundledCatalog.load(_bundle_root()),
        read_timeout_seconds=2.0,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(service.read_profile)
        assert entered.wait(1.0)
        service.cancel_pending_reads()
        with pytest.raises(RuntimeSurfaceError) as error:
            pending.result(timeout=1.0)
    assert error.value.code is RuntimeSurfaceErrorCode.TIMEOUT

    release.set()
    deadline = time.monotonic() + 1.0
    while runtime_surface._read_executor_stats()["admitted"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service.read_profile()["state"] == "ready"
    service.close()


def test_profile_ceremony_is_ordered_digest_bound_and_one_shot(
    active_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    read_service = _service(active_runtime)
    monkeypatch.setattr(
        "core_runtime.pack_control_v4.resolve_profile_pack_set",
        lambda _pack_ids: active_runtime.resolved,
    )
    activated: list[object] = []

    activation_attempts = [0]

    def activate(resolved, **_bindings):
        activation_attempts[0] += 1
        if activation_attempts[0] == 1:
            raise RuntimeError("temporary activation failure")
        activated.append(resolved)
        return active_runtime.activation

    monkeypatch.setattr(
        "core_runtime.pack_control_v4.activate_resolved_profile_pack_set",
        activate,
    )
    ceremony = RuntimeProfileChangeService(surface_service=read_service)
    revision = str(active_runtime.resolved.plan["profile_revision"])
    plan_digest = str(active_runtime.resolved.plan["plan_digest"])
    resolved = ceremony.resolve(
        {
            "profile_id": "defaults",
            "expected_profile_revision": revision,
            "expected_plan_digest": plan_digest,
            "desired_pack_ids": ["defaultspack"],
        },
        session_id="session-a",
    )
    reviewed = ceremony.review(
        {
            "candidate_id": resolved["candidate_id"],
            "candidate_digest": resolved["candidate_digest"],
        },
        session_id="session-a",
    )
    real_commit = runtime_surface._commit_authority_profile_approval
    approval_attempts = [0]

    def commit_with_temporary_denial(candidate, *, session_id, approval_id, decided_at):
        approval_attempts[0] += 1
        if approval_attempts[0] == 1:
            raise RuntimeSurfaceError(
                RuntimeSurfaceErrorCode.UNAPPROVED,
                "temporary Authority denial",
            )
        return real_commit(
            candidate,
            session_id=session_id,
            approval_id=approval_id,
            decided_at=decided_at,
        )

    monkeypatch.setattr(
        runtime_surface,
        "_commit_authority_profile_approval",
        commit_with_temporary_denial,
    )
    approve_request = {
        "candidate_id": reviewed["candidate_id"],
        "candidate_digest": reviewed["candidate_digest"],
    }
    with pytest.raises(RuntimeSurfaceError):
        ceremony.approve(approve_request, session_id="session-a")
    approved = ceremony.approve(
        approve_request,
        session_id="session-a",
    )
    Draft202012Validator(_control_output_schema()).validate(approved)
    assert approved["approval_id"] == approved["authority_approval"]["approval_id"]
    with AuthorityStore(runtime_user_data_root() / "authority" / "v4.sqlite3") as authority:
        authority_approval = authority.get_approval(approved["authority_approval"]["approval_id"])
    assert authority_approval is not None
    assert authority_approval.decision == "approved"
    assert authority_approval.target.function_id == ("tobkiri.host.control-presentation")
    assert authority_approval.target.operation_id == "profile.change.approve"
    activation_request = {
        "approval_id": approved["approval_id"],
        "approval_digest": approved["approval_digest"],
    }
    with pytest.raises(RuntimeSurfaceError):
        ceremony.activate(activation_request, session_id="session-a")
    result = ceremony.activate(activation_request, session_id="session-a")

    assert result["state"] == "active"
    assert activated == [active_runtime.resolved]
    replay = ceremony.activate(
        {
            "approval_id": approved["approval_id"],
            "approval_digest": approved["approval_digest"],
        },
        session_id="session-a",
    )
    assert replay == result


def test_profile_activation_rejects_wrong_credentials_without_consuming_approval(
    active_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "core_runtime.pack_control_v4.resolve_profile_pack_set",
        lambda _pack_ids: active_runtime.resolved,
    )
    monkeypatch.setattr(
        "core_runtime.pack_control_v4.activate_resolved_profile_pack_set",
        lambda _resolved, **_bindings: active_runtime.activation,
    )
    ceremony = RuntimeProfileChangeService(surface_service=_service(active_runtime))
    resolved = ceremony.resolve(
        {
            "profile_id": "defaults",
            "expected_profile_revision": active_runtime.resolved.plan["profile_revision"],
            "expected_plan_digest": active_runtime.resolved.plan["plan_digest"],
            "desired_pack_ids": ["defaultspack"],
        },
        session_id="session-a",
    )
    reviewed = ceremony.review(
        {
            "candidate_id": resolved["candidate_id"],
            "candidate_digest": resolved["candidate_digest"],
        },
        session_id="session-a",
    )
    approved = ceremony.approve(
        {
            "candidate_id": reviewed["candidate_id"],
            "candidate_digest": reviewed["candidate_digest"],
        },
        session_id="session-a",
    )
    request = {
        "approval_id": approved["approval_id"],
        "approval_digest": approved["approval_digest"],
    }

    invalid_requests = (
        ({**request, "approval_id": "approval.profile-change.wrong"}, "session-a"),
        ({**request, "approval_digest": "sha256:" + "0" * 64}, "session-a"),
        (request, "session-b"),
    )
    for invalid_request, session_id in invalid_requests:
        with pytest.raises(RuntimeSurfaceError) as rejected:
            ceremony.activate(invalid_request, session_id=session_id)
        assert rejected.value.code is RuntimeSurfaceErrorCode.UNAPPROVED

    assert ceremony.activate(request, session_id="session-a")["state"] == "active"


def test_profile_activation_reauthenticates_immutable_authority_record(
    active_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "core_runtime.pack_control_v4.resolve_profile_pack_set",
        lambda _pack_ids: active_runtime.resolved,
    )
    ceremony = RuntimeProfileChangeService(surface_service=_service(active_runtime))
    resolved = ceremony.resolve(
        {
            "profile_id": "defaults",
            "expected_profile_revision": active_runtime.resolved.plan["profile_revision"],
            "expected_plan_digest": active_runtime.resolved.plan["plan_digest"],
            "desired_pack_ids": ["defaultspack"],
        },
        session_id="session-a",
    )
    reviewed = ceremony.review(
        {
            "candidate_id": resolved["candidate_id"],
            "candidate_digest": resolved["candidate_digest"],
        },
        session_id="session-a",
    )
    approved = ceremony.approve(
        {
            "candidate_id": reviewed["candidate_id"],
            "candidate_digest": reviewed["candidate_digest"],
        },
        session_id="session-a",
    )
    approval_id = str(approved["approval_id"])
    with AuthorityStore(runtime_user_data_root() / "authority" / "v4.sqlite3") as authority:
        record = authority.get_approval(approval_id)
        assert record is not None
        authority.put_record(replace(record, decision="denied"), replace=True)

    with pytest.raises(RuntimeSurfaceError) as rejected:
        ceremony.activate(
            {
                "approval_id": approval_id,
                "approval_digest": approved["approval_digest"],
            },
            session_id="session-a",
        )
    assert rejected.value.code is RuntimeSurfaceErrorCode.UNAPPROVED


def test_profile_ceremony_rejects_cross_session_and_expired_review(
    active_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    read_service = _service(active_runtime)
    monkeypatch.setattr(
        "core_runtime.pack_control_v4.resolve_profile_pack_set",
        lambda _pack_ids: active_runtime.resolved,
    )
    ceremony = RuntimeProfileChangeService(
        ttl_seconds=1,
        clock=lambda: now[0],
        surface_service=read_service,
    )
    resolved = ceremony.resolve(
        {
            "profile_id": "defaults",
            "expected_profile_revision": active_runtime.resolved.plan["profile_revision"],
            "expected_plan_digest": active_runtime.resolved.plan["plan_digest"],
            "desired_pack_ids": ["defaultspack"],
        },
        session_id="session-a",
    )
    with pytest.raises(RuntimeSurfaceError) as wrong_session:
        ceremony.review(
            {
                "candidate_id": resolved["candidate_id"],
                "candidate_digest": resolved["candidate_digest"],
            },
            session_id="session-b",
        )
    assert wrong_session.value.code is RuntimeSurfaceErrorCode.DIGEST_MISMATCH
    reviewed = ceremony.review(
        {
            "candidate_id": resolved["candidate_id"],
            "candidate_digest": resolved["candidate_digest"],
        },
        session_id="session-a",
    )
    assert reviewed["state"] == "reviewed"

    second = ceremony.resolve(
        {
            "profile_id": "defaults",
            "expected_profile_revision": active_runtime.resolved.plan["profile_revision"],
            "expected_plan_digest": active_runtime.resolved.plan["plan_digest"],
            "desired_pack_ids": ["defaultspack"],
        },
        session_id="session-a",
    )
    now[0] = 2.0
    with pytest.raises(RuntimeSurfaceError) as expired:
        ceremony.review(
            {
                "candidate_id": second["candidate_id"],
                "candidate_digest": second["candidate_digest"],
            },
            session_id="session-a",
        )
    assert expired.value.code is RuntimeSurfaceErrorCode.TIMEOUT


def test_profile_activation_rejects_expired_durable_approval(
    active_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    monkeypatch.setattr(
        "core_runtime.pack_control_v4.resolve_profile_pack_set",
        lambda _pack_ids: active_runtime.resolved,
    )
    ceremony = RuntimeProfileChangeService(
        ttl_seconds=1,
        clock=lambda: now[0],
        surface_service=_service(active_runtime),
    )
    resolved = ceremony.resolve(
        {
            "profile_id": "defaults",
            "expected_profile_revision": active_runtime.resolved.plan["profile_revision"],
            "expected_plan_digest": active_runtime.resolved.plan["plan_digest"],
            "desired_pack_ids": ["defaultspack"],
        },
        session_id="session-expiry",
    )
    reviewed = ceremony.review(
        {
            "candidate_id": resolved["candidate_id"],
            "candidate_digest": resolved["candidate_digest"],
        },
        session_id="session-expiry",
    )
    approved = ceremony.approve(
        {
            "candidate_id": reviewed["candidate_id"],
            "candidate_digest": reviewed["candidate_digest"],
        },
        session_id="session-expiry",
    )
    now[0] = 2.0

    with pytest.raises(RuntimeSurfaceError) as expired:
        ceremony.activate(
            {
                "approval_id": approved["approval_id"],
                "approval_digest": approved["approval_digest"],
            },
            session_id="session-expiry",
        )
    assert expired.value.code is RuntimeSurfaceErrorCode.TIMEOUT


def test_profile_activation_concurrent_retry_returns_one_durable_result(
    active_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "core_runtime.pack_control_v4.resolve_profile_pack_set",
        lambda _pack_ids: active_runtime.resolved,
    )
    monkeypatch.setattr(
        "core_runtime.pack_control_v4.activate_resolved_profile_pack_set",
        lambda _resolved, **_bindings: active_runtime.activation,
    )
    ceremony = RuntimeProfileChangeService(surface_service=_service(active_runtime))
    resolved = ceremony.resolve(
        {
            "profile_id": "defaults",
            "expected_profile_revision": active_runtime.resolved.plan["profile_revision"],
            "expected_plan_digest": active_runtime.resolved.plan["plan_digest"],
            "desired_pack_ids": ["defaultspack"],
        },
        session_id="session-a",
    )
    reviewed = ceremony.review(
        {
            "candidate_id": resolved["candidate_id"],
            "candidate_digest": resolved["candidate_digest"],
        },
        session_id="session-a",
    )
    approved = ceremony.approve(
        {
            "candidate_id": reviewed["candidate_id"],
            "candidate_digest": reviewed["candidate_digest"],
        },
        session_id="session-a",
    )
    request = {
        "approval_id": approved["approval_id"],
        "approval_digest": approved["approval_digest"],
    }

    def activate_once() -> str:
        try:
            return str(ceremony.activate(request, session_id="session-a")["state"])
        except RuntimeSurfaceError as error:
            return error.code.value

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(lambda _index: activate_once(), range(2)))

    assert outcomes == ["active", "active"]


def test_profile_ceremony_continues_across_restart_at_every_stage(
    active_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "core_runtime.pack_control_v4.resolve_profile_pack_set",
        lambda _pack_ids: active_runtime.resolved,
    )
    activations: list[object] = []

    def activate(resolved, **_bindings):
        activations.append(resolved)
        return active_runtime.activation

    monkeypatch.setattr(
        "core_runtime.pack_control_v4.activate_resolved_profile_pack_set",
        activate,
    )
    surface = _service(active_runtime)
    first = RuntimeProfileChangeService(surface_service=surface)
    resolved = first.resolve(
        {
            "profile_id": "defaults",
            "expected_profile_revision": active_runtime.resolved.plan["profile_revision"],
            "expected_plan_digest": active_runtime.resolved.plan["plan_digest"],
            "desired_pack_ids": ["defaultspack"],
        },
        session_id="session-restart",
    )
    reviewed = RuntimeProfileChangeService(surface_service=surface).review(
        {
            "candidate_id": resolved["candidate_id"],
            "candidate_digest": resolved["candidate_digest"],
        },
        session_id="session-restart",
    )
    approved = RuntimeProfileChangeService(surface_service=surface).approve(
        {
            "candidate_id": reviewed["candidate_id"],
            "candidate_digest": reviewed["candidate_digest"],
        },
        session_id="session-restart",
    )
    activated = RuntimeProfileChangeService(surface_service=surface).activate(
        {
            "approval_id": approved["approval_id"],
            "approval_digest": approved["approval_digest"],
        },
        session_id="session-restart",
    )
    replay = RuntimeProfileChangeService(surface_service=surface).activate(
        {
            "approval_id": approved["approval_id"],
            "approval_digest": approved["approval_digest"],
        },
        session_id="session-restart",
    )

    assert activated == replay
    assert activations == [active_runtime.resolved]


def test_profile_activation_recovers_commit_before_receipt_across_restart(
    active_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "core_runtime.pack_control_v4.resolve_profile_pack_set",
        lambda _pack_ids: active_runtime.resolved,
    )
    ceremony = RuntimeProfileChangeService(surface_service=_service(active_runtime))
    resolved = ceremony.resolve(
        {
            "profile_id": "defaults",
            "expected_profile_revision": active_runtime.resolved.plan["profile_revision"],
            "expected_plan_digest": active_runtime.resolved.plan["plan_digest"],
            "desired_pack_ids": ["defaultspack"],
        },
        session_id="session-commit-recovery",
    )
    reviewed = ceremony.review(
        {
            "candidate_id": resolved["candidate_id"],
            "candidate_digest": resolved["candidate_digest"],
        },
        session_id="session-commit-recovery",
    )
    approved = ceremony.approve(
        {
            "candidate_id": reviewed["candidate_id"],
            "candidate_digest": reviewed["candidate_digest"],
        },
        session_id="session-commit-recovery",
    )
    request = {
        "approval_id": approved["approval_id"],
        "approval_digest": approved["approval_digest"],
    }

    def lose_activation_receipt(*_args, **_kwargs):
        raise OSError("simulated crash after activation commit")

    monkeypatch.setattr(ceremony._store, "mark_activated", lose_activation_receipt)
    with pytest.raises(RuntimeSurfaceError) as lost:
        ceremony.activate(request, session_id="session-commit-recovery")
    assert lost.value.code is RuntimeSurfaceErrorCode.UNAPPROVED

    committed = capture_default_profile()
    assert committed.activation["activation_id"].startswith(
        "activation:defaults-profile-change-"
    )
    restarted = RuntimeProfileChangeService()

    def retry(_index: int) -> dict[str, object]:
        return restarted.activate(request, session_id="session-commit-recovery")

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(retry, range(2)))

    assert receipts[0] == receipts[1]
    assert receipts[0]["activation_id"] == committed.activation["activation_id"]
    durable = RuntimeProfileChangeService().activate(
        request,
        session_id="session-commit-recovery",
    )
    assert durable == receipts[0]

    from core_runtime.pack_control_v4 import (
        PackControlConflict,
        activate_resolved_profile_pack_set,
    )

    tampered_lock = dict(active_runtime.resolved.lock)
    tampered_lock["plan_digest"] = "sha256:" + "0" * 64
    tampered = ResolvedDefaultProfile(
        profile=active_runtime.resolved.profile,
        lock=tampered_lock,
        plan=active_runtime.resolved.plan,
    )
    with pytest.raises(PackControlConflict):
        activate_resolved_profile_pack_set(
            tampered,
            activation_id=str(committed.activation["activation_id"]),
            expected_profile_revision=str(
                active_runtime.resolved.plan["profile_revision"]
            ),
            expected_plan_digest=str(active_runtime.resolved.plan["plan_digest"]),
            expected_activation_id=str(active_runtime.activation["activation_id"]),
        )
    with pytest.raises(ProfileResolutionDenied, match="predecessor is stale"):
        activate_resolved_profile_pack_set(
            active_runtime.resolved,
            activation_id="activation:defaults-profile-change-different",
            expected_profile_revision=str(
                active_runtime.resolved.plan["profile_revision"]
            ),
            expected_plan_digest=str(active_runtime.resolved.plan["plan_digest"]),
            expected_activation_id=str(active_runtime.activation["activation_id"]),
        )
    with AuthorityStore(runtime_user_data_root() / "authority" / "v4.sqlite3") as authority:
        authority.advance_security_epoch("test exact replay security evidence")
    with pytest.raises(ProfileResolutionDenied, match="SecurityEpoch is stale"):
        activate_resolved_profile_pack_set(
            active_runtime.resolved,
            activation_id=str(committed.activation["activation_id"]),
            expected_profile_revision=str(
                active_runtime.resolved.plan["profile_revision"]
            ),
            expected_plan_digest=str(active_runtime.resolved.plan["plan_digest"]),
            expected_activation_id=str(active_runtime.activation["activation_id"]),
        )


def test_profile_approval_response_loss_retries_exact_authority_receipt(
    active_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "core_runtime.pack_control_v4.resolve_profile_pack_set",
        lambda _pack_ids: active_runtime.resolved,
    )
    surface = _service(active_runtime)
    ceremony = RuntimeProfileChangeService(surface_service=surface)
    resolved = ceremony.resolve(
        {
            "profile_id": "defaults",
            "expected_profile_revision": active_runtime.resolved.plan["profile_revision"],
            "expected_plan_digest": active_runtime.resolved.plan["plan_digest"],
            "desired_pack_ids": ["defaultspack"],
        },
        session_id="session-response-loss",
    )
    reviewed = ceremony.review(
        {
            "candidate_id": resolved["candidate_id"],
            "candidate_digest": resolved["candidate_digest"],
        },
        session_id="session-response-loss",
    )
    real_mark_approved = ceremony._store.mark_approved
    lost = [False]

    def lose_first_receipt(*args, **kwargs):
        if not lost[0]:
            lost[0] = True
            raise OSError("simulated response persistence loss")
        return real_mark_approved(*args, **kwargs)

    monkeypatch.setattr(ceremony._store, "mark_approved", lose_first_receipt)
    request = {
        "candidate_id": reviewed["candidate_id"],
        "candidate_digest": reviewed["candidate_digest"],
    }
    with pytest.raises(RuntimeSurfaceError) as failed_response:
        ceremony.approve(request, session_id="session-response-loss")
    assert failed_response.value.code is RuntimeSurfaceErrorCode.API_FAILURE

    retried = RuntimeProfileChangeService(surface_service=surface).approve(
        request,
        session_id="session-response-loss",
    )
    authority_path = runtime_user_data_root() / "authority" / "v4.sqlite3"
    with AuthorityStore(authority_path) as authority:
        record = authority.get_approval(retried["approval_id"])
        commits = [
            event
            for event in authority.audit_events()
            if event["event_type"] == "authority_records_committed"
            and any(
                item.get("record_id") == retried["approval_id"]
                for item in event["payload"].get("records", [])
            )
        ]
    assert record is not None
    assert record.digest == retried["approval_digest"]
    assert len(commits) == 1


def test_profile_approval_is_idempotent_across_processes(
    active_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "core_runtime.pack_control_v4.resolve_profile_pack_set",
        lambda _pack_ids: active_runtime.resolved,
    )
    ceremony = RuntimeProfileChangeService(surface_service=_service(active_runtime))
    resolved = ceremony.resolve(
        {
            "profile_id": "defaults",
            "expected_profile_revision": active_runtime.resolved.plan["profile_revision"],
            "expected_plan_digest": active_runtime.resolved.plan["plan_digest"],
            "desired_pack_ids": ["defaultspack"],
        },
        session_id="session-process-approval",
    )
    reviewed = ceremony.review(
        {
            "candidate_id": resolved["candidate_id"],
            "candidate_digest": resolved["candidate_digest"],
        },
        session_id="session-process-approval",
    )

    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    processes = [
        context.Process(
            target=_approve_profile_process,
            args=(
                str(reviewed["candidate_id"]),
                str(reviewed["candidate_digest"]),
                "session-process-approval",
                results,
            ),
        )
        for _index in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    outcomes = [results.get(timeout=2), results.get(timeout=2)]

    assert {item[0] for item in outcomes} == {"approved"}
    assert len({item[1:] for item in outcomes}) == 1
    approval_id = outcomes[0][1]
    with AuthorityStore(runtime_user_data_root() / "authority" / "v4.sqlite3") as authority:
        commits = [
            event
            for event in authority.audit_events()
            if event["event_type"] == "authority_records_committed"
            and any(
                item.get("record_id") == approval_id for item in event["payload"].get("records", [])
            )
        ]
    assert len(commits) == 1
