"""Defaultspack's fail-closed setup response contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.errors import SchemaValidationError
from tobkiri_protocol.validation import validate_document


def _fail(message: str) -> None:
    raise SchemaValidationError(
        "defaults setup semantic validation failed",
        diagnostics=(message,),
    )


def validate_defaults_setup_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one complete Defaultspack setup response.

    JSON Schema owns the exact serialized field/type contract. These semantic
    checks bind its projections and digests to one finite Profile candidate.
    """

    document = validate_document(payload, "defaults_setup")
    profile = document["recommended_default_profile"]
    confirmation = profile["confirmation"]

    confirmation_body = {
        key: value for key, value in confirmation.items() if key != "confirmation_digest"
    }
    if confirmation["confirmation_digest"] != canonical_digest(confirmation_body):
        _fail("confirmation_digest does not bind the confirmation body")

    pack_ids = profile["pack_ids"]
    projected_pack_ids = [item["pack_id"] for item in profile["packs"]]
    if projected_pack_ids != pack_ids:
        _fail("recommended Profile Pack projection is stale or reordered")
    if document["packs"] != profile["packs"]:
        _fail("top-level Pack projection does not match the recommended Profile")

    seen_bindings: set[tuple[str, str, str, str]] = set()
    conversation_bindings: list[dict[str, Any]] = []
    for binding in confirmation["bindings"]:
        principal = binding["function_principal"]
        if principal["parent_artifact_digest"] != binding["artifact_digest"]:
            _fail("function principal parent artifact does not match its Pack")
        if principal["operation_id"] != binding["operation_id"]:
            _fail("function principal operation does not match its binding")
        identity = (
            binding["caller_function_id"],
            principal["function_id"],
            binding["contract_id"],
            binding["operation_id"],
        )
        if identity in seen_bindings:
            _fail("duplicate resolved binding identity")
        seen_bindings.add(identity)
        if (
            binding["contract_id"] == "conversation.turn.v1"
            and binding["operation_id"] == "complete"
        ):
            conversation_bindings.append(binding)

    if len(conversation_bindings) != 1:
        _fail("exactly one conversation provider binding is required")
    conversation = conversation_bindings[0]
    principal = conversation["function_principal"]
    if (
        conversation["pack_id"] not in pack_ids
        or conversation["caller_function_id"] != profile["shell"]["provider_id"]
        or conversation["domain_kind"] != "pack_vm"
        or principal["function_id"] != profile["conversation_provider"]
    ):
        _fail("conversation binding does not match the recommended Profile")

    if document["state"] == "active" and document["denial_diagnostic"] is not None:
        _fail("active setup state cannot carry a denial diagnostic")
    if document["state"] == "activation_denied" and not document["denial_diagnostic"]:
        _fail("activation_denied requires a denial diagnostic")
    return document


__all__ = ["validate_defaults_setup_payload"]
