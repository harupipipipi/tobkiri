"""Tests for the purpose-built live PackVM sandbox acceptance fixture."""

from __future__ import annotations

import errno
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from acceptance.packvm_sandbox_acceptance import run_live_acceptance
from acceptance.packvm_sandbox_qa_pack.runtime import probe
from core_runtime.pack_signature import (
    build_signed_manifest,
    sign_manifest,
    verify_signed_pack,
)


class _LiveAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def run_scenario(self, scenario: str, nonce: str) -> dict[str, Any]:
        self.calls.append((scenario, nonce))
        deadline = 100 if scenario == "original_deadline" else 200
        outcome = {
            "probe_isolation": "denied",
            "stdin_overflow": "input_limit_rejected",
            "stdout_overflow": "output_limit_rejected",
            "stderr_overflow": "error_limit_rejected",
            "original_deadline": "deadline_expired",
            "cancel": "cancelled",
            "abnormal_exit": "execution_failed",
            "resource_cleanup": "released",
        }[scenario]
        return {
            "kind": "tobkiri.packvm.sandbox-acceptance.v1",
            "scenario": scenario,
            "nonce": nonce,
            "outcome": outcome,
            "execution_boundary": "linux-packvm-guest",
            "transport": "authenticated-vsock-signed-guest-envelope",
            "guest_artifact_identity": "sha256:" + "a" * 64,
            "attestation_digest": "sha256:" + "b" * 64,
            "request_digest": "sha256:" + "c" * 64,
            "original_deadline_ns": deadline,
            "finished_ns": 200,
            "resource_cleanup_confirmed": True,
        }


def test_live_acceptance_requires_complete_matrix_and_stable_guest() -> None:
    adapter = _LiveAdapter()
    report = run_live_acceptance(adapter, nonce_seed=b"n" * 32)
    assert len(adapter.calls) == 8
    assert len(report.observations) == 8
    assert report.guest_artifact_identity == "sha256:" + "a" * 64
    canonical = json.dumps(
        list(report.observations), sort_keys=True, separators=(",", ":")
    ).encode()
    assert report.report_digest == "sha256:" + hashlib.sha256(canonical).hexdigest()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("execution_boundary", "host-process"),
        ("transport", "host-pipe"),
        ("resource_cleanup_confirmed", False),
        ("attestation_digest", "sha256:invalid"),
    ],
)
def test_live_acceptance_rejects_non_guest_or_unverified_evidence(
    field: str,
    value: object,
) -> None:
    class Invalid(_LiveAdapter):
        def run_scenario(self, scenario: str, nonce: str) -> dict[str, Any]:
            observation = super().run_scenario(scenario, nonce)
            observation[field] = value
            return observation

    with pytest.raises(ValueError):
        run_live_acceptance(Invalid(), nonce_seed=b"n" * 32)


def test_live_acceptance_does_not_accept_renewed_deadline() -> None:
    class Renewed(_LiveAdapter):
        def run_scenario(self, scenario: str, nonce: str) -> dict[str, Any]:
            observation = super().run_scenario(scenario, nonce)
            if scenario == "original_deadline":
                observation["original_deadline_ns"] = 300
            return observation

    with pytest.raises(ValueError, match="original deadline"):
        run_live_acceptance(Renewed(), nonce_seed=b"n" * 32)


def test_acceptance_pack_refuses_direct_host_execution(capsys: pytest.CaptureFixture[str]) -> None:
    assert probe._main() == 2
    assert json.loads(capsys.readouterr().out)["code"] == "DIRECT_EXECUTION_REJECTED"


def test_acceptance_pack_reports_permission_denial_without_details() -> None:
    def denied() -> None:
        raise PermissionError(errno.EPERM, "secret Host detail")

    assert probe._expect_errno("network", denied) == {
        "denied": True,
        "reason": "permission_denied",
        "errno": errno.EPERM,
    }


def test_acceptance_pack_rejects_unknown_operation_before_probe() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        probe.tobkiri_packvm_invoke("other", {"nonce": "a" * 64})


def test_acceptance_pack_accepts_namespaced_canonical_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe.sys, "platform", "linux")
    monkeypatch.setattr(probe.os, "geteuid", lambda: 65534)
    monkeypatch.setattr(
        probe,
        "_probe_isolation",
        lambda nonce: {"scenario": "probe_isolation", "nonce": nonce},
    )
    assert probe.tobkiri_packvm_invoke(
        probe.OPERATION_PREFIX + "probe_isolation",
        {"nonce": "a" * 64},
    ) == {"scenario": "probe_isolation", "nonce": "a" * 64}


def test_acceptance_pack_source_stays_outside_production_catalog() -> None:
    root = Path(__file__).resolve().parents[1]
    fixture = root / "acceptance" / "packvm_sandbox_qa_pack"
    assert fixture.is_dir()
    assert not (root / "ecosystem" / "tobkiri_packvm_sandbox_qa_pack").exists()


def test_acceptance_pack_is_signable_without_gaining_authority() -> None:
    root = Path(__file__).resolve().parents[1]
    fixture = root / "acceptance" / "packvm_sandbox_qa_pack"
    key = Ed25519PrivateKey.generate()
    manifest = build_signed_manifest(
        fixture,
        pack_id="tobkiri_packvm_sandbox_qa_pack",
        version="1.0.0",
        publisher_id="dev.tobkiri.acceptance",
        core_compatibility=">=1.0.0",
        contract_versions={probe.CONTRACT_ID: "1.0.0"},
        requested_capabilities=[],
        created_at="2026-09-16T00:00:00+00:00",
    )
    signed = sign_manifest(manifest, key)
    verified = verify_signed_pack(
        fixture,
        signed,
        key.public_key(),
        expected_pack_id="tobkiri_packvm_sandbox_qa_pack",
        expected_publisher_id="dev.tobkiri.acceptance",
        expected_contract_versions={probe.CONTRACT_ID: "1.0.0"},
        expected_capabilities=[],
    )
    assert verified["verified"] is True
    assert verified["authority_granted"] is False
    assert signed["authority_granted"] is False
