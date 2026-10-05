#!/usr/bin/env python3
"""Collect genuine Host runtime receipts for Pack migration release evidence.

This harness drives the real Host execution path for canonical bundled Packs
and writes ``pack_migration_runtime_receipts.v1`` records that the B5
``migration_evidence`` gate consumes through
``scripts/quality/migration_release_evidence.py``.

Every receipt field is produced by actual Host execution in the collector's
own scratch state:

* ``admission`` — a plan-bound ``_PlanAdmission`` reservation minted by the
  Host's durable ``ResourceLedger``/``FairAdmissionQueue`` for the exact Pack.
* ``install`` — the captured ``CapturedPackControlSession`` ``pack.install``
  operation, the durable install record, and a signed Pack approval that is
  re-verified by ``capture_valid_pack_approval``.
* ``isolated-conformance`` — a separate interpreter process that re-bootstraps
  a fresh Host instance, re-runs catalog/boundary/schema/artifact compilation,
  performs its own admission and install, and reports Host-minted digests the
  parent cross-checks against its own measurements.

Nothing in this file invents Host state: if any step cannot be established by
real execution the collector fails closed for that Pack and reports why.

The collector itself lives under ``tests/conformance_support/`` with the
other pack-composition support harnesses: outside the sealed packaged-source
closure declared by ``scripts/generator_source_manifest.py``, and inside the
test-support tree where wiring the product pack's composition is permitted.

Usage::

    python -B tests/conformance_support/collect_pack_runtime_receipts.py \
        --packs rumi_agent_continuity_pack,rumi_api_toolsmith_pack \
        --work-dir /tmp/b5h-work
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

_RUNTIME_DIR = Path(__file__).resolve().parents[2]
if str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))


def canonical_digest(value: Any) -> str:
    """Return the evidence ledger's canonical digest (lazy runtime import)."""

    from scripts.quality.migration_release_evidence import (
        canonical_digest as evidence_digest,
    )

    return evidence_digest(value)


def _load_curated_reviews(path: Path) -> dict[str, Mapping[str, Any]]:
    """Load curated reviews after ``--runtime-dir`` has been resolved."""

    from scripts.quality.migration_release_evidence import (
        load_curated_reviews,
    )

    return load_curated_reviews(path)

PACK_QUARTET = (
    ("pack.v4.json", "pack"),
    ("contracts.v4.json", "pack_contract_catalog"),
    ("artifact-index.v4.json", "pack_artifact_index"),
    ("executables.v4.json", "executable_catalog"),
)

CONFORMANCE_SUITE_ID = "io.tobkiri.quality.pack-isolated-conformance.v1"
CONFORMANCE_CHECKS = (
    "catalog-record",
    "pack-boundary",
    "pack-document-quartet",
    "artifact-compilation",
    "plan-admission",
    "pack-install",
    "signed-approval",
    "operation-inventory",
)

RECEIPT_LEDGER_SCHEMA = "io.tobkiri.quality.pack-migration-runtime-receipts.v1"
RECEIPT_LEDGER_AUTHORITY = "host-runtime-receipts"


def _evidence_dir(runtime_dir: Path) -> Path:
    """Return the evidence directory inside the active runtime tree."""

    return runtime_dir / "scripts" / "quality" / "evidence"

PROBE_TIMEOUT_SECONDS = 240.0


class CollectorError(RuntimeError):
    """Raised when a genuine Host receipt cannot be established."""


def _sha256_bytes(data: bytes) -> str:
    """Return the ``sha256:`` digest of raw bytes."""

    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    """Return the ``sha256:`` digest of one regular file's bytes."""

    return _sha256_bytes(path.read_bytes())


def _utc_now() -> str:
    """Return the receipt observation timestamp in RFC 3339 form."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _this_file_digest() -> str:
    """Digest this collector so the executed suite is self-identifying."""

    return _sha256_file(Path(__file__).resolve())


def _git(repository_dir: Path, *args: str) -> str:
    """Run one read-only Git query inside the checkout being witnessed."""

    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repository_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise CollectorError(
            f"git {' '.join(args)} failed inside {repository_dir}: {error}"
        ) from error
    return result.stdout.strip()


def _require_clean_checkout(repository_dir: Path) -> tuple[str, str]:
    """Return HEAD commit/tree only when the checkout is exactly clean.

    The sealed packaged-source provenance may only claim
    ``source_clean: true``; the convention mirrors the test fixture, which
    treats every untracked file as a change.  Run the collector copy from
    outside the checkout when collecting in a scratch clone.
    """

    commit = _git(repository_dir, "rev-parse", "--verify", "HEAD^{commit}")
    tree = _git(repository_dir, "rev-parse", "--verify", "HEAD^{tree}")
    status = _git(
        repository_dir, "status", "--porcelain=v1", "--untracked-files=all"
    )
    if status:
        raise CollectorError(
            "checkout is not clean; source_clean would be a false claim: "
            + "; ".join(status.splitlines()[:8])
        )
    return commit, tree


def build_verified_bundle(work_dir: Path, runtime_dir: Path) -> Path:
    """Materialize a sealed packaged-Profile bundle for this checkout.

    Uses the repository's own preverified-snapshot packaging path so the
    activated Profile is bound to the exact tracked source bytes.
    """

    from tests.conformance_support.packaged_profile import (
        build_packaged_profile_bundle,
        create_test_source_provenance,
    )

    repository_dir = runtime_dir.parent
    commit, tree = _require_clean_checkout(repository_dir)
    fixture_root = Path(
        tempfile.mkdtemp(prefix="packaged-fixture-", dir=work_dir)
    )
    provenance = create_test_source_provenance(
        runtime_dir,
        fixture_root,
        provenance_record={
            "source_commit": commit,
            "source_tree": tree,
            "source_clean": True,
        },
    )
    return build_packaged_profile_bundle(
        runtime_dir / "ecosystem" / "defaultspack" / "v4",
        fixture_root,
        source_provenance_file=provenance,
    )


def _install_composition(bundle_root: Path) -> None:
    """Bind this process to the packaged bundle the Host must load."""

    from core_runtime.bootstrap import profile_capture
    from core_runtime.bootstrap import runtime as bootstrap_runtime
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        install_defaultspack_profile_runtime,
    )

    install_defaultspack_profile_runtime()

    def provider(_base_dir: Path | None = None) -> Path:
        return bundle_root

    profile_capture._bundle_root = provider  # noqa: SLF001
    bootstrap_runtime._bundle_root = provider  # noqa: SLF001


@dataclass
class HostInstance:
    """One activated Host instance owned by this collector process."""

    user_data: Path
    bundle_root: Path
    active: Any
    profile_id: str
    activation_id: str
    control: Any
    admission: Any

    @property
    def host_instance_id(self) -> str:
        """Return the PID-bound identity of this collector Host process."""

        return f"host-pid:{os.getpid()}"


def bootstrap_host_instance(user_data: Path, bundle_root: Path) -> HostInstance:
    """Activate a real Profile in ``user_data`` and open Host control."""

    os.environ["TOBKIRI_USER_DATA"] = str(user_data)
    _install_composition(bundle_root)

    from core_runtime.bootstrap.profile_capture import (
        capture_default_profile,
        prepare_default_profile_confirmation,
    )
    from core_runtime.bootstrap.production_v4 import _PlanAdmission
    from core_runtime.pack_control_v4 import capture_pack_control_session
    from ecosystem.defaultspack.domain.runtime_surface_v4 import (
        create_runtime_surface_services,
    )

    active = capture_default_profile(
        confirmation=prepare_default_profile_confirmation()
    )
    control = capture_pack_control_session(
        active=active,
        bundle_root=bundle_root,
        runtime_surface_factory=create_runtime_surface_services,
    )
    profile_id = str(active.resolved.profile["profile_id"])
    activation_id = str(active.activation["activation_id"])
    state_path = (
        user_data
        / "workspaces"
        / profile_id
        / "admission"
        / "reservations.json"
    )
    admission = _PlanAdmission(
        profile_id=profile_id,
        activation_id=activation_id,
        plan=active.resolved.plan,
        state_path=state_path,
    )
    return HostInstance(
        user_data=user_data,
        bundle_root=bundle_root,
        active=active,
        profile_id=profile_id,
        activation_id=activation_id,
        control=control,
        admission=admission,
    )


def _admission_estimate(pack_id: str, target_digest: str) -> Any:
    """Build the measured admission charge for one Pack conformance request."""

    from tobkiri_host.admission import AdmissionEstimate

    request = {
        "schema": "io.tobkiri.quality.pack-receipt-admission.v1",
        "pack_id": pack_id,
        "target_digest": target_digest,
    }
    measured = len(
        json.dumps(request, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    )
    upper_bound = max(measured, 4096)
    return AdmissionEstimate(
        measured_p95_bytes=measured,
        declared_minimum_bytes=measured,
        runtime_floor_bytes=min(max(measured, 4096), upper_bound),
        profile_reservation_bytes=measured,
        backend_overhead_bytes=0,
        concurrency=1,
        disk_bytes=0,
        declared_upper_bound_bytes=upper_bound,
        detached=False,
    )


def admit_pack(
    host: HostInstance,
    pack_id: str,
    target_digest: str,
) -> tuple[Mapping[str, Any], Any]:
    """Acquire a real plan-bound admission reservation for ``pack_id``.

    Returns the observed reservation facts and the live ticket; the caller
    must release the ticket after recording the step.
    """

    from tobkiri_host.admission import QueueScope

    scope = QueueScope(
        profile_id=host.profile_id,
        caller_id="tobkiri.quality.pack-receipt-collector",
        pack_id=pack_id,
        binding_id=target_digest,
        priority="foreground",
    )
    ticket = host.admission.acquire(
        scope, _admission_estimate(pack_id, target_digest), 30.0
    )
    reservation = ticket.reservation
    return {
        "reservation_id": reservation.reservation_id,
        "profile_id": reservation.profile_id,
        "owner_pid": reservation.owner_pid,
        "owner_identity": reservation.owner_identity,
        "detached": reservation.detached,
        "amount": {
            "memory_bytes": reservation.amount.memory_bytes,
            "disk_bytes": reservation.amount.disk_bytes,
            "process_slots": reservation.amount.process_slots,
            "start_slots": reservation.amount.start_slots,
        },
        "journal_path": host.admission._ledger._state_path,  # noqa: SLF001
    }, ticket


def _host_instance_id(reservation_facts: Mapping[str, Any]) -> str:
    """Bind the receipt step to the Host process that minted it."""

    owner_identity = reservation_facts.get("owner_identity")
    identity = (
        f"pid:{reservation_facts['owner_pid']}:start:{owner_identity}"
        if owner_identity
        else f"pid:{reservation_facts['owner_pid']}"
    )
    return f"host-process:{identity}"


def collect_admission_step(
    host: HostInstance,
    pack_id: str,
    target_digest: str,
) -> dict[str, Any]:
    """Record a real admission reservation as the ``admission`` step."""

    facts, ticket = admit_pack(host, pack_id, target_digest)
    try:
        observed_at = _utc_now()
        journal_path = Path(facts["journal_path"])
        journal_digest = _sha256_file(journal_path)
        plan = host.active.resolved.plan
        step = {
            "kind": "admission",
            "status": "verified",
            "pack_id": pack_id,
            "target_digest": target_digest,
            "host_instance_id": _host_instance_id(facts),
            "observed_at": observed_at,
            "admission_id": str(facts["reservation_id"]),
            "policy_digest": canonical_digest(
                {
                    "schema": "io.tobkiri.quality.pack-admission-policy.v1",
                    "profile_id": host.profile_id,
                    "activation_id": host.activation_id,
                    "plan_digest": str(plan["plan_digest"]),
                    "profile_revision": str(plan["profile_revision"]),
                    "admission_policy": dict(
                        plan.get("admission_policy") or {}
                    ),
                }
            ),
            "reservation_profile_id": str(facts["profile_id"]),
            "owner_pid": int(facts["owner_pid"]),
            "owner_identity": facts["owner_identity"],
            "amount": dict(facts["amount"]),
            "reservation_journal_digest": journal_digest,
        }
    finally:
        host.admission.release(ticket)
    step["receipt_digest"] = canonical_digest(step)
    return step


def collect_install_step(
    host: HostInstance,
    pack_id: str,
    target_digest: str,
    admission_step: Mapping[str, Any],
) -> dict[str, Any]:
    """Record the real control-surface install + signed approval."""

    from core_runtime.pack_control_v4 import (
        PACK_CONTROL_CONTRACT,
        _read_control_state,  # noqa: SLF001
        capture_valid_pack_approval,
    )

    session_id = f"session.receipts.{secrets.token_hex(16)}"
    install_result = host.control.invoke(
        PACK_CONTROL_CONTRACT,
        "pack.install",
        {"pack_id": pack_id, "_session_id": session_id},
    )
    if install_result.get("installed") is not True:
        raise CollectorError(f"pack.install did not install {pack_id}")
    installed_state = _read_control_state(host.profile_id, read_only=True)
    install_record = installed_state.get(pack_id)
    if not isinstance(install_record, Mapping):
        raise CollectorError(f"install record for {pack_id} is missing")
    if install_record.get("pack_artifact_digest") != target_digest:
        raise CollectorError(
            f"installed artifact digest for {pack_id} differs from review"
        )

    candidate = host.control.invoke(
        PACK_CONTROL_CONTRACT,
        "approval.candidate",
        {"pack_id": pack_id, "_session_id": session_id},
    )
    approval = host.control.invoke(
        PACK_CONTROL_CONTRACT,
        "approval.approve",
        {
            "pack_id": pack_id,
            "candidate_id": candidate["candidate_id"],
            "_session_id": session_id,
        },
    )
    if approval.get("approved") is not True:
        raise CollectorError(f"approval.approve did not approve {pack_id}")
    # Re-verify the signed approval record the Host just persisted.
    verified_approval = capture_valid_pack_approval(pack_id)
    installation_id = str(verified_approval["approval_revision"])
    step = {
        "kind": "install",
        "status": "verified",
        "pack_id": pack_id,
        "target_digest": target_digest,
        "host_instance_id": host.host_instance_id,
        "observed_at": _utc_now(),
        "installation_id": installation_id,
        "admission_receipt_digest": admission_step["receipt_digest"],
        "content_digest": str(install_record["content_digest"]),
        "install_record_digest": canonical_digest(dict(install_record)),
        "catalog_revision": str(install_record["catalog_revision"]),
        "approval_record_digest": _sha256_bytes(
            json.dumps(
                dict(verified_approval), separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        ),
    }
    step["receipt_digest"] = canonical_digest(step)
    return step


def conformance_suite_digest(pack_id: str, target_digest: str) -> str:
    """Digest the exact isolated-conformance suite executed for this Pack."""

    return canonical_digest(
        {
            "suite": CONFORMANCE_SUITE_ID,
            "collector_source_digest": _this_file_digest(),
            "checks": list(CONFORMANCE_CHECKS),
            "inputs": {"pack_id": pack_id, "target_digest": target_digest},
        }
    )


def run_conformance_probe(
    *,
    host_script: Path,
    runtime_dir: Path,
    work_dir: Path,
    pack_id: str,
    bundle_root: Path,
    timeout: float,
) -> tuple[dict[str, Any], str]:
    """Run the isolated conformance probe in a separate interpreter process.

    The child bootstraps its own Host instance under its own user-data root,
    so every reservation, approval, and digest it reports was minted by a
    different process — genuine isolation rather than shared state.
    """

    probe_dir = work_dir / "probe" / pack_id
    probe_user_data = probe_dir / "user-data"
    result_path = probe_dir / "probe-result.json"
    probe_dir.mkdir(parents=True, exist_ok=True)
    result_path.unlink(missing_ok=True)
    command = [
        sys.executable,
        "-B",
        str(host_script),
        "--probe",
        pack_id,
        "--runtime-dir",
        str(runtime_dir),
        "--user-data",
        str(probe_user_data),
        "--bundle-root",
        str(bundle_root),
        "--result-out",
        str(result_path),
    ]
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["TOBKIRI_USER_DATA"] = str(probe_user_data)
    completed = subprocess.run(
        command,
        cwd=runtime_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0 or not result_path.is_file():
        raise CollectorError(
            f"isolated conformance probe failed for {pack_id}: "
            + (completed.stderr or completed.stdout).strip()[-800:]
        )
    raw = result_path.read_bytes()
    result = json.loads(raw)
    if not isinstance(result, Mapping):
        raise CollectorError(f"probe result for {pack_id} is not an object")
    return dict(result), _sha256_bytes(raw)


def collect_conformance_step(
    *,
    probe_result: Mapping[str, Any],
    result_digest: str,
    pack_id: str,
    target_digest: str,
    semantic_kind: str,
    expected_operation_count: int | None,
    parent_content_digest: str,
    install_step: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the ``isolated-conformance`` step from verified probe facts."""

    checks = probe_result.get("checks")
    if not isinstance(checks, list):
        raise CollectorError(f"probe result for {pack_id} lacks checks")
    failed = [item for item in checks if item.get("status") != "verified"]
    if failed:
        names = ", ".join(str(item.get("check")) for item in failed)
        raise CollectorError(
            f"isolated conformance checks failed for {pack_id}: {names}"
        )
    child_content_digest = probe_result.get("install", {}).get(
        "content_digest"
    )
    if child_content_digest != parent_content_digest:
        raise CollectorError(
            f"isolated install content digest disagrees for {pack_id}"
        )
    if probe_result.get("pack_id") != pack_id or probe_result.get(
        "target_digest"
    ) != target_digest:
        raise CollectorError(f"probe result identity mismatched: {pack_id}")
    operation_count = int(
        probe_result.get("operation_inventory", {}).get("count", -1)
    )
    if operation_count < 0:
        raise CollectorError(f"probe result lacks operation inventory: {pack_id}")
    if expected_operation_count is not None and (
        operation_count != expected_operation_count
    ):
        raise CollectorError(
            f"probe counted {operation_count} operations for {pack_id} but "
            f"the reviewed semantic record declares "
            f"{expected_operation_count}"
        )

    step: dict[str, Any] = {
        "kind": "isolated-conformance",
        "pack_id": pack_id,
        "target_digest": target_digest,
        "host_instance_id": str(probe_result["host_instance_id"]),
        "observed_at": str(probe_result["observed_at"]),
        "install_receipt_digest": install_step["receipt_digest"],
        "probe_result_digest": result_digest,
        "operation_count": operation_count,
    }
    if semantic_kind == "admission-only":
        step["status"] = "not-applicable"
        step["reason"] = (
            "admission-only Pack declares zero operations; the isolated Host "
            "probe verified an empty executable operation inventory"
        )
    else:
        step["status"] = "verified"
        step["test_suite_digest"] = conformance_suite_digest(
            pack_id, target_digest
        )
        step["result_digest"] = result_digest
    step["receipt_digest"] = canonical_digest(step)
    return step


def collect_pack_receipt(
    host: HostInstance,
    pack_id: str,
    review: Mapping[str, Any],
    *,
    host_script: Path,
    runtime_dir: Path,
    work_dir: Path,
    bundle_root: Path,
    probe_timeout: float,
) -> dict[str, Any]:
    """Run the real admission→install→conformance chain for one Pack."""

    from core_runtime.external_pack_catalog_v4 import (
        load_admitted_pack_catalog,
        resolve_admitted_pack_root,
    )
    from tobkiri_protocol.validation import validate_file

    catalog = load_admitted_pack_catalog()
    if pack_id not in catalog:
        raise CollectorError(f"{pack_id} is absent from the admitted catalog")
    root = resolve_admitted_pack_root(pack_id)
    manifest = validate_file(root / "pack.v4.json", "pack")
    target_digest = str(manifest["pack"]["artifact_digest"])
    if review.get("target_digest") != target_digest:
        raise CollectorError(
            f"review target digest does not match manifest for {pack_id}"
        )
    semantic = review.get("semantic_record")
    # Only an embedded, digest-pinned admission-only semantic record waives
    # executable conformance.  Reviews that reference their semantic record
    # by digest alone always require the verified isolated-conformance path.
    semantic_kind = (
        "admission-only"
        if isinstance(semantic, Mapping)
        and semantic.get("kind") == "admission-only"
        else "operation-mapping"
    )
    expected_operation_count: int | None = None
    if isinstance(semantic, Mapping):
        inventory = semantic.get("operation_inventory")
        if isinstance(inventory, Mapping) and isinstance(
            inventory.get("v4_count"), int
        ):
            expected_operation_count = int(inventory["v4_count"])

    admission_step = collect_admission_step(host, pack_id, target_digest)
    install_step = collect_install_step(
        host, pack_id, target_digest, admission_step
    )
    probe_result, result_digest = run_conformance_probe(
        host_script=host_script,
        runtime_dir=runtime_dir,
        work_dir=work_dir,
        pack_id=pack_id,
        bundle_root=bundle_root,
        timeout=probe_timeout,
    )
    conformance_step = collect_conformance_step(
        probe_result=probe_result,
        result_digest=result_digest,
        pack_id=pack_id,
        target_digest=target_digest,
        semantic_kind=semantic_kind,
        expected_operation_count=expected_operation_count,
        parent_content_digest=str(install_step["content_digest"]),
        install_step=install_step,
    )
    receipt = {
        "pack_id": pack_id,
        "target_digest": target_digest,
        "semantic_record_digest": review["semantic_record_digest"],
        "review_attestation_digest": review["review_attestation_digest"],
        "admission": admission_step,
        "install": install_step,
        "isolated-conformance": conformance_step,
    }
    receipt["release_receipt_digest"] = canonical_digest(receipt)
    return receipt


def _probe_check(
    checks: list[dict[str, Any]], name: str
) -> dict[str, Any]:
    """Start one named conformance check record."""

    record: dict[str, Any] = {"check": name, "status": "failed"}
    checks.append(record)
    return record


def run_conformance_probe_in_process(
    pack_id: str,
    user_data: Path,
    bundle_root: Path,
) -> dict[str, Any]:
    """Execute the isolated conformance checks inside this process.

    Invoked only through ``--probe`` so the process, user-data root, and Host
    instance are genuinely independent from the collector's.
    """

    from core_runtime.bootstrap.production_v4 import _PlanAdmission
    from core_runtime.external_pack_catalog_v4 import (
        load_admitted_pack_catalog,
        resolve_admitted_pack_root,
    )
    from core_runtime.pack_control_v4 import (
        PACK_CONTROL_CONTRACT,
        _read_control_state,  # noqa: SLF001
        capture_pack_control_session,
        capture_valid_pack_approval,
    )
    from core_runtime.bootstrap.profile_capture import (
        capture_default_profile,
        prepare_default_profile_confirmation,
    )
    from ecosystem.defaultspack.domain.runtime_surface_v4 import (
        create_runtime_surface_services,
    )
    from tobkiri_host.admission import QueueScope
    from tobkiri_host.artifact_compiler import compile_pack_root
    from tobkiri_protocol.validation import validate_file

    os.environ["TOBKIRI_USER_DATA"] = str(user_data)
    _install_composition(bundle_root)

    checks: list[dict[str, Any]] = []
    details: dict[str, Any] = {}

    check = _probe_check(checks, "catalog-record")
    catalog = load_admitted_pack_catalog()
    record = catalog.get(pack_id)
    if record is None:
        raise CollectorError(f"{pack_id} absent from admitted catalog")
    check["status"] = "verified"

    check = _probe_check(checks, "pack-boundary")
    root = resolve_admitted_pack_root(pack_id)
    check["status"] = "verified"

    check = _probe_check(checks, "pack-document-quartet")
    quartet_digests = {}
    manifest = None
    for filename, schema_name in PACK_QUARTET:
        document = validate_file(root / filename, schema_name)
        quartet_digests[filename] = _sha256_file(root / filename)
        if filename == "pack.v4.json":
            manifest = document
    if manifest is None:
        raise CollectorError(f"{pack_id} manifest missing")
    target_digest = str(manifest["pack"]["artifact_digest"])
    details["quartet_digests"] = quartet_digests
    check["status"] = "verified"

    check = _probe_check(checks, "artifact-compilation")
    compiled = compile_pack_root(root)
    if compiled.artifact.pack_id != pack_id:
        raise CollectorError(f"{pack_id} compiled identity mismatch")
    if compiled.artifact.digest != target_digest:
        raise CollectorError(f"{pack_id} compiled digest mismatch")
    operations = [
        {
            "contract_id": operation.contract_id,
            "operation_id": operation.operation_id,
            "effect_class": operation.effect_class.value,
        }
        for function in compiled.artifact.functions
        for operation in function.operations
    ]
    details["compiled_artifact_digest"] = compiled.artifact.digest
    details["operation_inventory"] = {
        "count": len(operations),
        "operations": operations,
        "routes": {
            f"{key[0]}::{key[1]}": {
                "execution_kind": value["execution_kind"],
                "backend": value["backend"],
                "domain_kind": value["domain_kind"],
            }
            for key, value in compiled.routes.items()
        },
    }
    check["status"] = "verified"

    active = capture_default_profile(
        confirmation=prepare_default_profile_confirmation()
    )
    profile_id = str(active.resolved.profile["profile_id"])
    activation_id = str(active.activation["activation_id"])

    check = _probe_check(checks, "plan-admission")
    admission = _PlanAdmission(
        profile_id=profile_id,
        activation_id=activation_id,
        plan=active.resolved.plan,
        state_path=user_data
        / "workspaces"
        / profile_id
        / "admission"
        / "reservations.json",
    )
    ticket = admission.acquire(
        QueueScope(
            profile_id=profile_id,
            caller_id="tobkiri.quality.pack-receipt-collector",
            pack_id=pack_id,
            binding_id=target_digest,
        ),
        _admission_estimate(pack_id, target_digest),
        30.0,
    )
    reservation = ticket.reservation
    admission_facts = {
        "reservation_id": reservation.reservation_id,
        "owner_pid": reservation.owner_pid,
        "owner_identity": reservation.owner_identity,
        "profile_id": reservation.profile_id,
    }
    journal_digest = _sha256_file(
        Path(admission._ledger._state_path)  # noqa: SLF001
    )
    admission.release(ticket)
    details["admission"] = {**admission_facts, "journal_digest": journal_digest}
    check["status"] = "verified"

    check = _probe_check(checks, "pack-install")
    control = capture_pack_control_session(
        active=active,
        bundle_root=bundle_root,
        runtime_surface_factory=create_runtime_surface_services,
    )
    session_id = f"session.probe.{secrets.token_hex(16)}"
    install_result = control.invoke(
        PACK_CONTROL_CONTRACT,
        "pack.install",
        {"pack_id": pack_id, "_session_id": session_id},
    )
    if install_result.get("installed") is not True:
        raise CollectorError(f"probe pack.install failed for {pack_id}")
    installed = _read_control_state(profile_id, read_only=True).get(pack_id)
    if not isinstance(installed, Mapping):
        raise CollectorError(f"probe install record missing for {pack_id}")
    details["install"] = {
        "content_digest": str(installed["content_digest"]),
        "pack_artifact_digest": str(installed["pack_artifact_digest"]),
        "catalog_revision": str(installed["catalog_revision"]),
    }
    check["status"] = "verified"

    check = _probe_check(checks, "signed-approval")
    candidate = control.invoke(
        PACK_CONTROL_CONTRACT,
        "approval.candidate",
        {"pack_id": pack_id, "_session_id": session_id},
    )
    control.invoke(
        PACK_CONTROL_CONTRACT,
        "approval.approve",
        {
            "pack_id": pack_id,
            "candidate_id": candidate["candidate_id"],
            "_session_id": session_id,
        },
    )
    verified_approval = capture_valid_pack_approval(pack_id)
    details["approval"] = {
        "approval_revision": str(verified_approval["approval_revision"]),
        "record_digest": _sha256_bytes(
            json.dumps(
                dict(verified_approval),
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ),
    }
    check["status"] = "verified"

    check = _probe_check(checks, "operation-inventory")
    details["operation_inventory"]["verified_count"] = len(operations)
    check["status"] = "verified"

    return {
        "schema": "io.tobkiri.quality.pack-isolated-conformance-result.v1",
        "pack_id": pack_id,
        "target_digest": target_digest,
        "profile_id": profile_id,
        "activation_id": activation_id,
        "host_instance_id": (
            f"host-process:pid:{os.getpid()}:start:"
            f"{admission_facts['owner_identity']}"
            if admission_facts["owner_identity"]
            else f"host-process:pid:{os.getpid()}"
        ),
        "observed_at": _utc_now(),
        "checks": checks,
        "operation_inventory": details["operation_inventory"],
        "admission": details["admission"],
        "install": details["install"],
        "approval": details["approval"],
    }


def _load_receipt_ledger(path: Path) -> dict[str, Any]:
    """Load the existing receipt ledger, preserving unknown receipts."""

    if not path.is_file():
        return {
            "schema": RECEIPT_LEDGER_SCHEMA,
            "authority": RECEIPT_LEDGER_AUTHORITY,
            "generator_id": None,
            "receipts": {},
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != RECEIPT_LEDGER_SCHEMA
        or payload.get("authority") != RECEIPT_LEDGER_AUTHORITY
        or payload.get("generator_id") is not None
        or not isinstance(payload.get("receipts"), dict)
    ):
        raise CollectorError(f"existing receipt ledger is invalid: {path}")
    return payload


def _selected_packs(
    args: argparse.Namespace, reviews: Mapping[str, Any]
) -> list[str]:
    """Resolve which canonical Packs to collect receipts for."""

    if args.packs:
        requested = [item.strip() for item in args.packs.split(",") if item]
    else:
        requested = sorted(reviews)
    missing = [pack_id for pack_id in requested if pack_id not in reviews]
    if missing:
        raise CollectorError(
            "no independent semantic review for: " + ", ".join(missing)
        )
    return sorted(dict.fromkeys(requested))


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect genuine Host admission/install/isolated-conformance "
            "runtime receipts for the Pack migration release gate."
        )
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=_RUNTIME_DIR,
        help="tobkiri_runtime checkout to execute (default: this file's tree)",
    )
    parser.add_argument(
        "--packs",
        default="",
        help="comma-separated canonical Pack IDs (default: all reviewed)",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="scratch root for bundle/user-data/probes (default: mkdtemp)",
    )
    parser.add_argument(
        "--reviews",
        type=Path,
        default=None,
        help=(
            "curated review ledger used to bind receipts "
            "(default: <runtime-dir>/scripts/quality/evidence/…)"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "runtime receipt ledger to merge into "
            "(default: <runtime-dir>/scripts/quality/evidence/…)"
        ),
    )
    parser.add_argument(
        "--probe-timeout",
        type=float,
        default=PROBE_TIMEOUT_SECONDS,
        help="seconds to allow each isolated conformance probe",
    )
    parser.add_argument(
        "--probe",
        metavar="PACK_ID",
        default=None,
        help="internal: run the isolated conformance probe for one Pack",
    )
    parser.add_argument("--user-data", type=Path, default=None)
    parser.add_argument("--bundle-root", type=Path, default=None)
    parser.add_argument("--result-out", type=Path, default=None)
    return parser.parse_args(argv)


def _probe_main(args: argparse.Namespace) -> int:
    """Entry point for the isolated child-process conformance probe."""

    runtime_dir = args.runtime_dir.resolve()
    if str(runtime_dir) not in sys.path:
        sys.path.insert(0, str(runtime_dir))
    if args.user_data is None or args.bundle_root is None or (
        args.result_out is None
    ):
        raise CollectorError("--probe requires --user-data/--bundle-root/--result-out")
    result = run_conformance_probe_in_process(
        str(args.probe),
        args.user_data.resolve(),
        args.bundle_root.resolve(),
    )
    args.result_out.parent.mkdir(parents=True, exist_ok=True)
    args.result_out.write_bytes(
        json.dumps(result, indent=2, sort_keys=True).encode("utf-8")
    )
    return 0


def _collect_main(args: argparse.Namespace) -> int:
    """Collect receipts for the selected Pack set."""

    runtime_dir = args.runtime_dir.resolve()
    if str(runtime_dir) not in sys.path:
        sys.path.insert(0, str(runtime_dir))
    reviews_path = args.reviews or (
        _evidence_dir(runtime_dir) / "pack_migration_reviews.v1.json"
    )
    output_path = args.output or (
        _evidence_dir(runtime_dir) / "pack_migration_runtime_receipts.v1.json"
    )
    reviews = _load_curated_reviews(reviews_path)
    pack_ids = _selected_packs(args, reviews)
    if not pack_ids:
        raise CollectorError("no reviewed Packs selected")

    work_dir = args.work_dir
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="b5h-receipts-"))
    work_dir = work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    print(f"[collect] building verified packaged bundle in {work_dir}")
    bundle_root = build_verified_bundle(work_dir, runtime_dir)
    user_data = Path(tempfile.mkdtemp(prefix="host-user-data-", dir=work_dir))
    print(f"[collect] bootstrapping Host instance under {user_data}")
    host = bootstrap_host_instance(user_data, bundle_root)
    print(
        f"[collect] active profile={host.profile_id} "
        f"activation={host.activation_id}"
    )

    ledger = _load_receipt_ledger(output_path)
    collected: list[str] = []
    failures: dict[str, str] = {}
    host_script = Path(__file__).resolve()
    for pack_id in pack_ids:
        try:
            receipt = collect_pack_receipt(
                host,
                pack_id,
                reviews[pack_id],
                host_script=host_script,
                runtime_dir=runtime_dir,
                work_dir=work_dir,
                bundle_root=bundle_root,
                probe_timeout=args.probe_timeout,
            )
        except Exception as error:  # fail closed per Pack, keep others going
            failures[pack_id] = str(error)
            print(f"[collect] {pack_id}: FAILED {error}", file=sys.stderr)
            continue
        ledger["receipts"][pack_id] = receipt
        collected.append(pack_id)
        print(f"[collect] {pack_id}: receipt {receipt['release_receipt_digest']}")

    if collected:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(
            (json.dumps(ledger, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
        )
    print(
        f"[collect] wrote {len(collected)} receipts to {output_path}; "
        f"{len(failures)} failed"
    )
    if failures:
        print("[collect] failures:", json.dumps(failures, indent=2))
        return 2
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch between collector and isolated probe modes."""

    args = _parse_args(argv)
    try:
        if args.probe is not None:
            return _probe_main(args)
        return _collect_main(args)
    except CollectorError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
