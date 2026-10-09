"""Host-only encrypted durable claims for saved tool admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads


@dataclass(frozen=True)
class AdmissionReservation:
    """Private durable CAS identity, never returned to a guest or browser."""

    record_id: str
    revision: int
    binding: Mapping[str, Any]


class HostSavedToolAdmissionStore:
    """Reserve once across instances/processes using encrypted AuthorityStore CAS.

    Reserved, admitted and claimed rows are permanent replay tombstones. No TTL
    or failed retry deletes them. Capture rotation naturally creates a new key;
    the previous captured invocation cannot become valid again.
    """

    def __init__(self, persistence: Any) -> None:
        self._persistence = persistence

    def reserve(self, binding: Mapping[str, Any]) -> AdmissionReservation:
        """Persist one stable owner/turn/tool-call claim before native approval."""
        exact = strict_loads(canonical_json(dict(binding)))
        required = {
            "capture_digest",
            "turn_id",
            "tool_call_id",
            "owner_principal",
            "owner_session",
            "operation_digest",
        }
        if set(exact) != required or not all(
            isinstance(value, str) and value for value in exact.values()
        ):
            raise PermissionError("saved tool admission binding is unavailable")
        key_binding = {key: value for key, value in exact.items() if key != "operation_digest"}
        record_id = "saved-tool-admission-v1-" + canonical_digest(key_binding).removeprefix(
            "sha256:"
        )
        payload = {
            "effect_id": record_id,
            "record_kind": "saved_tool_admission_v1",
            "state": "reserved",
            "binding": exact,
        }
        try:
            revision = self._persistence.create_host_pending_effect(record_id, payload)
        except Exception as error:
            if self._persistence.get_host_pending_effect(record_id) is not None:
                raise PermissionError("saved tool admission was already claimed") from error
            raise PermissionError("saved tool admission could not be persisted") from error
        return AdmissionReservation(record_id, revision, exact)

    def admit(
        self, reservation: AdmissionReservation, proof: Mapping[str, Any]
    ) -> AdmissionReservation:
        """Record successful exact consent Broker return without exposing a token."""
        current = self._record(reservation, "reserved")
        exact = strict_loads(canonical_json(dict(proof)))
        if set(exact) != {
            "retained_operation_digest",
            "consent_request_id",
            "consent_request_digest",
            "consent_target",
        } or not all(isinstance(value, str) and value for value in exact.values()):
            raise PermissionError("saved tool consent proof is unavailable")
        revision = self._persistence.compare_and_swap_host_pending_effect(
            reservation.record_id,
            expected_revision=reservation.revision,
            payload={**current, "state": "admitted", "proof": exact},
        )
        return AdmissionReservation(reservation.record_id, revision, reservation.binding)

    def admit_policy(
        self, reservation: AdmissionReservation, proof: Mapping[str, Any]
    ) -> AdmissionReservation:
        """Record explicit exact policy settlement without native-consent fiction."""
        current = self._record(reservation, "reserved")
        exact = strict_loads(canonical_json(dict(proof)))
        if (
            set(exact)
            != {
                "retained_operation_digest",
                "selection_id",
                "grant_id",
                "operation_capture_digest",
                "mode",
            }
            or exact["mode"] not in {"agent", "full"}
            or not all(isinstance(value, str) and value for value in exact.values())
        ):
            raise PermissionError("saved tool policy proof is unavailable")
        revision = self._persistence.compare_and_swap_host_pending_effect(
            reservation.record_id,
            expected_revision=reservation.revision,
            payload={**current, "state": "admitted", "proof": exact},
        )
        return AdmissionReservation(reservation.record_id, revision, reservation.binding)

    def admit_selected_policy(
        self, reservation: AdmissionReservation, proof: Mapping[str, Any]
    ) -> AdmissionReservation:
        """Admit an outer provider while inner effects still require authority."""
        current = self._record(reservation, "reserved")
        exact = strict_loads(canonical_json(dict(proof)))
        if (
            set(exact)
            != {
                "retained_operation_digest",
                "selection_id",
                "native_boundary_digest",
                "mode",
            }
            or exact["mode"] not in {"agent", "full"}
            or not all(isinstance(value, str) and value for value in exact.values())
        ):
            raise PermissionError("saved tool selected policy proof is unavailable")
        revision = self._persistence.compare_and_swap_host_pending_effect(
            reservation.record_id,
            expected_revision=reservation.revision,
            payload={**current, "state": "admitted", "proof": exact},
        )
        return AdmissionReservation(reservation.record_id, revision, reservation.binding)

    def claim(self, reservation: AdmissionReservation, retained_operation_digest: str) -> None:
        """Consume the admission receipt by durable CAS before any actual read."""
        current = self._record(reservation, "admitted")
        if current.get("proof", {}).get("retained_operation_digest") != retained_operation_digest:
            raise PermissionError("saved tool admission operation changed")
        self._persistence.compare_and_swap_host_pending_effect(
            reservation.record_id,
            expected_revision=reservation.revision,
            payload={**current, "state": "claimed"},
        )

    def _record(self, reservation: AdmissionReservation, state: str) -> dict[str, Any]:
        current = self._persistence.get_host_pending_effect(reservation.record_id)
        if current is None or current[0] != reservation.revision:
            raise PermissionError("saved tool admission revision changed")
        payload = dict(current[1])
        if (
            payload.get("record_kind") != "saved_tool_admission_v1"
            or payload.get("state") != state
            or payload.get("binding") != reservation.binding
        ):
            raise PermissionError("saved tool admission was already claimed or changed")
        return payload
