"""Host-only acceptance receipt ledger tests."""

from __future__ import annotations

import pytest

from tobkiri_host.acceptance_receipts import AcceptanceReceiptPort
from tobkiri_host.models import OpaqueAuthorityRef, RuntimeEvidence

_DIGEST = "sha256:" + "a" * 64
_REQUEST_DIGEST = "sha256:" + "b" * 64
_GUEST_DIGEST = "sha256:" + "c" * 64
_ATTESTATION = "sha256:" + "d" * 64
_NONCE = "e" * 64


def _port(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> AcceptanceReceiptPort:
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    monkeypatch.setenv("TOBKIRI_PACKVM_ACCEPTANCE_ENABLE", "1")
    monkeypatch.setenv("TOBKIRI_PACKVM_ACCEPTANCE_PACK_DIGEST", _DIGEST)
    monkeypatch.setenv("TOBKIRI_CI_E2E_APP_DATA_ROOT", str(tmp_path))
    return AcceptanceReceiptPort.from_host_environment(
        app_identifier="dev.tobkiri.launcher.ci-e2e",
        user_data_root=user_data,
    )


def _evidence() -> RuntimeEvidence:
    return RuntimeEvidence(
        domain_ref=OpaqueAuthorityRef("packvm-domain"),
        executable_digest="sha256:" + "f" * 64,
        backend_digest="sha256:" + "1" * 64,
        authenticated_channel=True,
        nonce_fresh=True,
        platform="macos-vz-arm64",
        isolation_profile="packvm.default",
        attestation_digest=_ATTESTATION,
        domain_lease_id="lease",
        resource_reservation_id="reservation",
        guest_artifact_identity=_GUEST_DIGEST,
        guest_execution_boundary="linux-packvm-guest",
    )


def _begin(port: AcceptanceReceiptPort, scenario: str, request_id: str = "request-1") -> None:
    port.begin(
        request_id=request_id,
        pack_id="tobkiri_packvm_sandbox_qa_pack",
        pack_digest=_DIGEST,
        operation_id=f"tobkiri_packvm_sandbox_qa_pack.{scenario}",
        nonce=_NONCE,
        evidence=_evidence(),
        request_digest=_REQUEST_DIGEST,
        original_deadline_ns=1,
    )


def test_receipt_port_is_unavailable_to_normal_production(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("TOBKIRI_PACKVM_ACCEPTANCE_ENABLE", "1")
    monkeypatch.setenv("TOBKIRI_PACKVM_ACCEPTANCE_PACK_DIGEST", _DIGEST)
    monkeypatch.setenv("TOBKIRI_CI_E2E_APP_DATA_ROOT", str(tmp_path))
    (tmp_path / "user_data").mkdir()
    with pytest.raises(PermissionError):
        AcceptanceReceiptPort.from_host_environment(
            app_identifier="dev.rumiai.app",
            user_data_root=tmp_path,
        )
    with pytest.raises(PermissionError):
        AcceptanceReceiptPort.from_host_environment(
            app_identifier="dev.tobkiri.launcher.ci-e2e",
            user_data_root=tmp_path / "user_data" / "other",
        )


def test_exact_digest_and_typed_guest_evidence_are_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    port = _port(monkeypatch, tmp_path)
    with pytest.raises(PermissionError):
        port.begin(
            request_id="request-1",
            pack_id="tobkiri_packvm_sandbox_qa_pack",
            pack_digest="sha256:" + "9" * 64,
            operation_id="tobkiri_packvm_sandbox_qa_pack.cancel_hold",
            nonce=_NONCE,
            evidence=_evidence(),
            request_digest=_REQUEST_DIGEST,
            original_deadline_ns=1,
        )


def test_cancel_requires_authenticated_ack_reap_and_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    port = _port(monkeypatch, tmp_path)
    _begin(port, "cancel_hold")
    with pytest.raises(PermissionError):
        port.take("request-1", _NONCE)
    port.record_authenticated_cancel("request-1", deadline=False)
    with pytest.raises(PermissionError):
        port.take("request-1", _NONCE)
    port.record_invocation_reaped("request-1")
    port.record_resources_released(
        "request-1", reservation=True, materialization=True
    )
    receipt = port.take("request-1", _NONCE)
    assert receipt.termination == "cancelled"
    assert receipt.authenticated_cancel_ack is True


def test_receipt_binds_provisioned_instance_not_domain_launch_attestation(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """Observations must carry the stable provisioned-instance attestation."""
    port = _port(monkeypatch, tmp_path)
    _begin(port, "cancel_hold")
    port.record_authenticated_cancel("request-1", deadline=False)
    port.record_invocation_reaped("request-1")
    port.record_resources_released(
        "request-1", reservation=True, materialization=True
    )
    receipt = port.take("request-1", _NONCE)
    # Stable across scenario domains of one provisioned instance; the
    # per-domain launch attestation must not reach the observation.
    assert receipt.attestation_digest == "sha256:" + "1" * 64
    assert receipt.attestation_digest != _ATTESTATION


def test_deadline_and_abnormal_exit_are_finite_typed_facts(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    port = _port(monkeypatch, tmp_path)
    _begin(port, "deadline_hold", "deadline-request")
    port.record_authenticated_cancel("deadline-request", deadline=True)
    port.record_invocation_reaped("deadline-request")
    port.record_resources_released(
        "deadline-request", reservation=True, materialization=True
    )
    assert port.take("deadline-request", _NONCE).termination == "deadline_expired"

    _begin(port, "abnormal_exit", "abnormal-request")
    with pytest.raises(ValueError):
        port.record_abnormal_exit("abnormal-request", 0)
    port.record_abnormal_exit("abnormal-request", 73)
    port.record_invocation_reaped("abnormal-request")
    port.record_resources_released(
        "abnormal-request", reservation=True, materialization=True
    )
    receipt = port.take("abnormal-request", _NONCE)
    assert receipt.termination == "abnormal_exit"
    assert receipt.exit_code == 73


@pytest.mark.parametrize(
    "termination",
    ["input_limit_rejected", "output_limit_rejected", "error_limit_rejected"],
)
def test_limit_rejections_are_typed_host_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    termination: str,
) -> None:
    port = _port(monkeypatch, tmp_path)
    scenario = {
        "input_limit_rejected": "stdin_overflow",
        "output_limit_rejected": "stdout_overflow",
        "error_limit_rejected": "stderr_overflow",
    }[termination]
    _begin(port, scenario)
    port.record_limit_rejected("request-1", termination)
    port.record_invocation_reaped("request-1")
    port.record_resources_released(
        "request-1",
        reservation=True,
        materialization=True,
    )
    assert port.take("request-1", _NONCE).termination == termination


def test_pack_output_client_approval_and_http_disconnect_are_not_receipt_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    port = _port(monkeypatch, tmp_path)
    _begin(port, "cancel_hold")
    public_methods = {name for name in dir(port) if not name.startswith("_")}
    assert "record_pack_output" not in public_methods
    assert "record_http_disconnect" not in public_methods
    assert "approve" not in public_methods
    with pytest.raises(PermissionError):
        port.take("request-1", _NONCE)
