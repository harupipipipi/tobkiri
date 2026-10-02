"""Defaultspack's catalog-to-capability target projection.

The generic Host verifies every returned target against the captured Provider,
Function, artifact digest, Profile revision, Plan, and activation before it
can be used. This module only interprets Defaultspack's UI catalog shape.
"""

from __future__ import annotations

import json
from typing import Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from core_runtime.global_contracts.http_contract_dispatch import (
    HTTPContractBinding,
    HTTPContractTarget,
)
from .v4_view_contract import validate_public_input


def defaultspack_dynamic_capability_targets(
    binding: HTTPContractBinding,
    *,
    catalog: Mapping[str, object],
) -> tuple[HTTPContractTarget, ...]:
    """Build candidate targets for the Defaultspack capability invoke route."""

    if binding.path != "/api/ui/capability/invoke":
        return ()
    packs = catalog.get("packs")
    if not isinstance(packs, list):
        return ()
    targets: list[HTTPContractTarget] = []
    for pack in packs:
        if (
            not isinstance(pack, Mapping)
            or pack.get("enabled") is not True
            or pack.get("approved") is not True
        ):
            continue
        pack_id = str(pack.get("pack_id") or "").strip()
        artifact_digest = str(pack.get("artifact_digest") or "").strip()
        operations = pack.get("operations")
        if not pack_id or not artifact_digest or not isinstance(operations, list):
            continue
        for operation in operations:
            if not isinstance(operation, Mapping) or operation.get("invokable") is not True:
                continue
            contract_id = str(operation.get("contract_id") or "").strip()
            operation_id = str(operation.get("operation_id") or "").strip()
            provider_id = str(operation.get("provider_id") or "").strip()
            function_id = str(operation.get("function_id") or provider_id).strip()
            if not contract_id or not operation_id or not provider_id:
                continue
            input_schema = _captured_input_schema(operation.get("input_schema"))
            payload_keys = (
                frozenset(json.loads(input_schema)["properties"]) - {"profile_id"}
                if input_schema
                else _payload_keys(contract_id, operation_id)
            )
            if not input_schema and contract_id not in {
                "tobkiri.service.media.inspect.v1",
                "tobkiri.acceptance.packvm.sandbox.v1",
                "tobkiri.workflow.v4",
            }:
                continue
            targets.append(
                HTTPContractTarget(
                    contribution_id=f"pack.{pack_id}.{operation_id}",
                    contract_id=contract_id,
                    operation_id=operation_id,
                    provider_id=provider_id,
                    function_id=function_id,
                    allowed_payload_keys=payload_keys,
                    owner_pack_id=pack_id,
                    artifact_digest=artifact_digest,
                    input_schema=input_schema,
                )
            )
    # A public ID cannot choose one of several contracts/providers by order.
    identities = [target.contribution_id for target in targets]
    targets = [target for target in targets if identities.count(target.contribution_id) == 1]
    return tuple(
        sorted(
            targets,
            key=lambda target: (
                target.owner_pack_id,
                target.contract_id,
                target.operation_id,
            ),
        )
    )


def _captured_input_schema(value: object) -> bytes:
    """Admit only finite object inputs from the Host operation catalog."""
    if (
        not isinstance(value, Mapping)
        or value.get("type") != "object"
        or value.get("additionalProperties") is not False
        or not isinstance(value.get("properties"), Mapping)
        or len(value["properties"]) > 64
    ):
        return b""
    try:
        validate_public_input({key: None for key in value["properties"] if key != "profile_id"})
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
        if len(encoded) > 65536 or _has_external_reference(value):
            return b""
        Draft202012Validator.check_schema(value)
        return encoded
    except (SchemaError, TypeError, ValueError):
        return b""


def _has_external_reference(value: object) -> bool:
    if isinstance(value, Mapping):
        for key in ("$ref", "$dynamicRef", "$recursiveRef"):
            if key in value and (
                not isinstance(value[key], str) or not value[key].startswith("#/")
            ):
                return True
        return any(_has_external_reference(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_external_reference(item) for item in value)
    return False


def _payload_keys(contract_id: str, operation_id: str) -> frozenset[str]:
    """Return the finite payload schema admitted by the generic UI bridge."""

    if contract_id == "tobkiri.service.media.inspect.v1":
        return frozenset({"name", "path", "encoding", "max_bytes", "start_line", "end_line"})
    if contract_id == "tobkiri.acceptance.packvm.sandbox.v1":
        prefix = "tobkiri_packvm_sandbox_qa_pack."
        scenarios = {
            "probe_isolation",
            "stdin_overflow",
            "stdout_overflow",
            "stderr_overflow",
            "deadline_hold",
            "cancel_hold",
            "abnormal_exit",
        }
        scenario = operation_id.removeprefix(prefix)
        if operation_id.startswith(prefix) and scenario in scenarios:
            if scenario == "stdin_overflow":
                return frozenset({"nonce", "fill"})
            return frozenset({"nonce"})
    if contract_id == "tobkiri.workflow.v4":
        workflow_payloads = {
            "definition.list": frozenset(),
            "definition.get": frozenset({"definition_id"}),
            "definition.create": frozenset({"definition_id", "document"}),
            "definition.update": frozenset({"definition_id", "document", "if_match"}),
            "definition.delete": frozenset({"definition_id", "if_match"}),
            "definition.validate": frozenset({"document"}),
            "definition.publish": frozenset({"definition_id", "if_match"}),
            "operation.palette": frozenset(),
        }
        return workflow_payloads.get(operation_id, frozenset())
    return frozenset()


__all__ = ["defaultspack_dynamic_capability_targets"]
