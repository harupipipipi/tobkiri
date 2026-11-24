"""Fail-closed validation for live PackVM sandbox acceptance observations."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Protocol

_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_SCENARIOS = (
    "probe_isolation",
    "stdin_overflow",
    "stdout_overflow",
    "stderr_overflow",
    "original_deadline",
    "cancel",
    "abnormal_exit",
    "resource_cleanup",
)
_EXPECTED = {
    "probe_isolation": "denied",
    "stdin_overflow": "input_limit_rejected",
    "stdout_overflow": "output_limit_rejected",
    "stderr_overflow": "error_limit_rejected",
    "original_deadline": "deadline_expired",
    "cancel": "cancelled",
    "abnormal_exit": "execution_failed",
    "resource_cleanup": "released",
}


class LivePackVMAcceptancePort(Protocol):
    """Native adapter implemented beside the authenticated Broker caller."""

    def run_scenario(self, scenario: str, nonce: str) -> Mapping[str, Any]:
        """Run one scenario and return its authenticated guest observation."""


@dataclass(frozen=True)
class AcceptanceReport:
    """Canonical result that cannot be produced from Host-pipe observations."""

    guest_artifact_identity: str
    attestation_digest: str
    observations: tuple[dict[str, Any], ...]
    report_digest: str


def run_live_acceptance(
    port: LivePackVMAcceptancePort,
    *,
    nonce_seed: bytes,
) -> AcceptanceReport:
    """Run every finite scenario through an authenticated native adapter."""

    if len(nonce_seed) < 32:
        raise ValueError("PackVM acceptance nonce seed is too short")
    observations = []
    for index, scenario in enumerate(_SCENARIOS):
        nonce = hashlib.sha256(
            nonce_seed + b"\0" + str(index).encode("ascii") + b"\0" + scenario.encode()
        ).hexdigest()
        observations.append(_validate_observation(port.run_scenario(scenario, nonce), scenario, nonce))
    guest_identities = {item["guest_artifact_identity"] for item in observations}
    attestations = {item["attestation_digest"] for item in observations}
    if len(guest_identities) != 1 or len(attestations) != 1:
        raise ValueError("PackVM acceptance observations changed guest identity")
    canonical = json.dumps(observations, sort_keys=True, separators=(",", ":")).encode()
    return AcceptanceReport(
        guest_artifact_identity=guest_identities.pop(),
        attestation_digest=attestations.pop(),
        observations=tuple(observations),
        report_digest="sha256:" + hashlib.sha256(canonical).hexdigest(),
    )


def _validate_observation(
    value: Mapping[str, Any],
    scenario: str,
    nonce: str,
) -> dict[str, Any]:
    required = {
        "kind",
        "scenario",
        "nonce",
        "outcome",
        "execution_boundary",
        "transport",
        "guest_artifact_identity",
        "attestation_digest",
        "request_digest",
        "original_deadline_ns",
        "finished_ns",
        "resource_cleanup_confirmed",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("PackVM acceptance observation fields are invalid")
    if (
        value["kind"] != "tobkiri.packvm.sandbox-acceptance.v1"
        or value["scenario"] != scenario
        or value["nonce"] != nonce
        or value["outcome"] != _EXPECTED[scenario]
        or value["execution_boundary"] != "linux-packvm-guest"
        or value["transport"] != "authenticated-vsock-signed-guest-envelope"
        or value["resource_cleanup_confirmed"] is not True
    ):
        raise ValueError("PackVM acceptance observation is not live guest evidence")
    for field in ("guest_artifact_identity", "attestation_digest", "request_digest"):
        if not isinstance(value[field], str) or _DIGEST.fullmatch(value[field]) is None:
            raise ValueError(f"PackVM acceptance {field} is invalid")
    deadline = value["original_deadline_ns"]
    finished = value["finished_ns"]
    if type(deadline) is not int or type(finished) is not int or deadline <= 0 or finished <= 0:
        raise ValueError("PackVM acceptance timing evidence is invalid")
    if scenario == "original_deadline" and finished < deadline:
        raise ValueError("PackVM acceptance renewed or pre-empted the original deadline")
    return {key: value[key] for key in sorted(required)}


__all__ = ["AcceptanceReport", "LivePackVMAcceptancePort", "run_live_acceptance"]
