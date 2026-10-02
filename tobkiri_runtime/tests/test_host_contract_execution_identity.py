"""Regression tests for the four-field Host execution contract binding."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
from urllib.parse import quote

import pytest

from core_runtime.host_contract import (
    ExecutionProfileIdentity,
    HostContractError,
    capture_launcher_bootstrap_secret,
    capture_host_contract,
    host_contract_value,
    validate_host_contract,
)
from core_runtime.pack_api_server import PackAPIServer
from core_runtime.panel_auth import (
    get_panel_auth_manager,
    reset_panel_auth_manager_for_tests,
)
from tests.conformance_support.host_contract import host_contract


pytestmark = pytest.mark.contract


PROFILE_REVISION_A = "sha256:" + "a" * 64
PROFILE_REVISION_B = "sha256:" + "b" * 64
PLAN_DIGEST_A = "sha256:" + "c" * 64
PLAN_DIGEST_B = "sha256:" + "d" * 64
ACTIVATION_A = "activation:profile-a-2026"
ACTIVATION_B = "activation:profile-a-2027"
LAUNCHER_BOOTSTRAP_REVISION = (
    "sha256:cce92a9b1d3092cdac63ba80b39e5d3a17d0905f3a716241250e8ac724095580"
)
LAUNCHER_BOOTSTRAP_PLAN = (
    "sha256:2a08fdc2de1e0d5e51d2f248b0984d4510db442e6905bcebc2984a44d23131a5"
)


def _identity_a() -> ExecutionProfileIdentity:
    return ExecutionProfileIdentity(
        profile_id="profile-a",
        profile_revision=PROFILE_REVISION_A,
        activation_id=ACTIVATION_A,
        plan_digest=PLAN_DIGEST_A,
    )


def _contract_a(secret: str = "secret-a") -> dict[str, object]:
    return host_contract(
        profile_id="profile-a",
        profile_revision=PROFILE_REVISION_A,
        activation_id=ACTIVATION_A,
        plan_digest=PLAN_DIGEST_A,
        values={"panel_bootstrap_secret": secret},
    )


def _launcher_bootstrap_contract(secret: str = "launcher-bootstrap-secret") -> dict[str, object]:
    """Build the Launcher-only temporary credential contract."""

    return host_contract(
        profile_id="defaults",
        profile_revision=LAUNCHER_BOOTSTRAP_REVISION,
        activation_id="activation:bootstrap-template",
        plan_digest=LAUNCHER_BOOTSTRAP_PLAN,
        values={"panel_bootstrap_secret": secret},
    )


@pytest.mark.parametrize(
    "missing_field",
    ("profile_id", "profile_revision", "activation_id", "plan_digest"),
)
def test_host_contract_requires_every_execution_identity_field(
    missing_field: str,
) -> None:
    """A contract without any one tuple member is not a usable authority."""

    payload = _contract_a()
    del payload[missing_field]

    with pytest.raises(HostContractError):
        validate_host_contract(payload)


def test_profile_revision_cannot_be_a_copy_of_plan_digest() -> None:
    """The revision/hash mix-up is rejected instead of silently accepted."""

    payload = _contract_a()
    payload["profile_revision"] = PLAN_DIGEST_A

    with pytest.raises(HostContractError, match="profile_revision"):
        validate_host_contract(payload)


def test_launcher_bootstrap_marker_is_credential_only_not_execution_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal contract readers cannot promote the temporary bootstrap tuple."""

    bootstrap = _launcher_bootstrap_contract()
    with pytest.raises(HostContractError, match="not execution authority"):
        validate_host_contract(bootstrap)
    with pytest.raises(HostContractError, match="not execution authority"):
        capture_host_contract(contract=bootstrap)
    assert host_contract_value("panel_bootstrap_secret", contract=bootstrap) == ""

    user_data = tmp_path / "user-data"
    user_data.mkdir(mode=0o700)
    user_data.chmod(0o700)
    contract_path = user_data / "host_contract.json"
    _write_private_contract(contract_path, bootstrap)
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    monkeypatch.setenv("TOBKIRI_HOST_CONTRACT_PATH", str(contract_path))

    # The dedicated handoff API exposes only the credential value. It does
    # not return a contract object or make the bootstrap tuple usable for
    # execution routes.
    assert capture_launcher_bootstrap_secret() == "launcher-bootstrap-secret"


def test_same_plan_activation_rotation_is_not_the_same_execution() -> None:
    """A new activation is stale even when Profile and resolved plan are equal."""

    rotated = _contract_a()
    rotated["activation_id"] = ACTIVATION_B

    with pytest.raises(HostContractError, match="execution identity"):
        capture_host_contract(expected_identity=_identity_a(), contract=rotated)


def test_foreign_profile_is_rejected_against_the_captured_identity() -> None:
    """A contract for another Profile cannot supply this execution's values."""

    foreign = _contract_a()
    foreign["profile_id"] = "profile-b"

    with pytest.raises(HostContractError, match="execution identity"):
        capture_host_contract(expected_identity=_identity_a(), contract=foreign)


def test_normal_profile_a_and_profile_b_captures_are_independent() -> None:
    """Two complete captures remain valid when each matches its own identity."""

    identity_b = ExecutionProfileIdentity(
        profile_id="profile-b",
        profile_revision=PROFILE_REVISION_B,
        activation_id="activation:profile-b-2026",
        plan_digest=PLAN_DIGEST_B,
    )
    captured_a = capture_host_contract(
        expected_identity=_identity_a(),
        contract=_contract_a(),
    )
    captured_b = capture_host_contract(
        expected_identity=identity_b,
        contract=host_contract(
            profile_id=identity_b.profile_id,
            profile_revision=identity_b.profile_revision,
            activation_id=identity_b.activation_id,
            plan_digest=identity_b.plan_digest,
            values={"panel_bootstrap_secret": "secret-b"},
        ),
    )

    assert host_contract_value("panel_bootstrap_secret", contract=captured_a) == (
        "secret-a"
    )
    assert host_contract_value("panel_bootstrap_secret", contract=captured_b) == (
        "secret-b"
    )


def _write_private_contract(path: Path, payload: dict[str, object]) -> None:
    """Write one fixture contract with the Launcher ownership permissions."""

    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def _health_challenge(port: int, challenge: str) -> dict[str, object]:
    """Read one health response carrying a desktop challenge."""

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(
        "GET",
        "/health?challenge=" + quote(challenge, safe=""),
        headers={"X-Rumi-Desktop-Health-Challenge": challenge},
    )
    response = connection.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    connection.close()
    assert response.status == 200, payload
    return payload


def test_pack_api_binds_a_fixed_snapshot_across_contract_file_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Health uses the launch snapshot, not a post-start mutable pathname."""

    user_data = tmp_path / "user-data"
    user_data.mkdir(mode=0o700)
    user_data.chmod(0o700)
    contract_path = user_data / "host_contract.json"
    _write_private_contract(contract_path, _contract_a())
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    monkeypatch.setenv("TOBKIRI_HOST_CONTRACT_PATH", str(contract_path))

    secret_a = "secret-a"
    manager = reset_panel_auth_manager_for_tests(capture_launcher_credential=True)
    assert manager is get_panel_auth_manager()
    server = PackAPIServer(port=0, panel_auth_manager=manager, host_contract=_contract_a(secret_a))
    server.start()
    try:
        challenge = "fixed-snapshot-challenge"
        before = _health_challenge(server.port, challenge)
        expected_a = hmac.new(
            secret_a.encode("utf-8"),
            challenge.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert before["data"]["desktop_challenge_response"] == expected_a

        replacement = user_data / "host_contract.replacement"
        _write_private_contract(
            replacement,
            host_contract(
                profile_id="profile-b",
                profile_revision=PROFILE_REVISION_B,
                activation_id="activation:profile-b-2027",
                plan_digest=PLAN_DIGEST_B,
                values={"panel_bootstrap_secret": "secret-b"},
            ),
        )
        os.replace(replacement, contract_path)

        after = _health_challenge(server.port, challenge)
        assert after["data"]["desktop_challenge_response"] == expected_a
        assert after["data"]["desktop_challenge_response"] != hmac.new(
            b"secret-b",
            challenge.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    finally:
        server.stop()
        reset_panel_auth_manager_for_tests()
