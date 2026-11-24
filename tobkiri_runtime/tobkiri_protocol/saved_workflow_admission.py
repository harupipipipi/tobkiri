"""Owner-persisted admission identity for a future saved-chat Workflow run.

This record carries no execution grant. Only the captured saved coordinator
may construct it; public lifecycle payloads cannot attach it to a turn.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


@dataclass(frozen=True)
class SavedWorkflowAdmission:
    """Immutable captured identity, separate from mutable execution status."""

    profile_id: str
    turn_id: str
    conversation_id: str
    input_digest: str
    run_id: str
    definition_id: str
    revision_digest: str
    policy_digest: str
    plan_digest: str
    activation_id: str
    activation_digest: str
    security_epoch: int
    workflow_principal_ids: tuple[str, ...]
    owner_principal_id: str
    owner_session_id: str
    admission_api_version: str = "io.tobkiri.saved-workflow-admission.v1"

    def __post_init__(self) -> None:
        if self.admission_api_version != "io.tobkiri.saved-workflow-admission.v1":
            raise ValueError("saved Workflow admission version is invalid")
        for name in ("profile_id", "turn_id", "conversation_id", "run_id", "definition_id", "activation_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or _ID.fullmatch(value) is None:
                raise ValueError("saved Workflow admission identity is invalid")
        for name in ("input_digest", "revision_digest", "policy_digest", "plan_digest", "activation_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
                raise ValueError("saved Workflow admission digest is invalid")
        if type(self.security_epoch) is not int or self.security_epoch < 1:
            raise ValueError("saved Workflow admission epoch is invalid")
        principals = self.workflow_principal_ids
        if not isinstance(principals, tuple) or not 1 <= len(principals) <= 64:
            raise ValueError("saved Workflow issuer set is invalid")
        for value in (*principals, self.owner_principal_id, self.owner_session_id):
            if not isinstance(value, str) or not value.strip() or len(value) > 1024:
                raise ValueError("saved Workflow owner identity is invalid")
        if len(set(principals)) != len(principals):
            raise ValueError("saved Workflow issuer set is ambiguous")

    def bind_input(self, profile_id: str, initial: Mapping[str, Any]) -> None:
        """Require the full original saved input and its owning runtime Profile."""
        value = validate_saved_conversation_input(initial)
        request = value["request"]
        if (self.profile_id != profile_id or self.turn_id != request["turn_id"]
                or self.conversation_id != request["conversation_id"]
                or self.input_digest != canonical_digest(value)):
            raise PermissionError("saved Workflow admission belongs to another input")

    def to_mapping(self) -> dict[str, Any]:
        """Return inert persistence data, never a caller-authorized grant."""
        return {**asdict(self), "workflow_principal_ids": list(self.workflow_principal_ids)}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SavedWorkflowAdmission:
        """Validate persisted owner data with an exact finite field set."""
        if set(value) != set(cls.__dataclass_fields__):
            raise ValueError("saved Workflow admission fields are invalid")
        principals = value.get("workflow_principal_ids")
        if not isinstance(principals, list):
            raise ValueError("saved Workflow issuer set is invalid")
        return cls(**{**value, "workflow_principal_ids": tuple(principals)})
