"""Validate measured execution records, never compiled operation inventories.

Digests bind the recorded measurements; they do not execute an operation or
attest to the author's honesty. A runtime collector must obtain these records
from an actual Host invocation, not manufacture them from catalog metadata.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

EXECUTION_METHOD = "host-operation-invocation.v1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_IDENTITY_FIELDS = ("function_id", "contract_id", "operation_id")
_REVIEW_FIELDS = ("function_id", "v4_contract_id", "v4_operation_id")


def _identity(
    record: Mapping[str, Any], fields: tuple[str, ...]
) -> tuple[str, ...] | None:
    values: list[str] = []
    for field in fields:
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            return None
        values.append(value)
    return tuple(values)


def _observation(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if observed.tzinfo is None or observed.utcoffset() is None:
            return None
        return observed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def operation_execution_errors(
    pack_id: str,
    semantic: Mapping[str, Any] | None,
    isolated: Mapping[str, Any],
    *,
    installed_at: str | None,
) -> list[str]:
    """Require exactly one successful, identity-bound run per reviewed operation.

    This is a release-eligibility check, deliberately separate from loading the
    historical v1 ledger. Old inventory-only receipts remain readable.
    """

    executions = isolated.get("operation_executions")
    if not isinstance(executions, list) or not executions:
        return ["runtime_receipt_operation_execution_missing"]
    if semantic is None:
        return ["runtime_receipt_reviewed_operation_identities_missing"]
    mappings = semantic.get("operation_mappings")
    if not isinstance(mappings, list) or not mappings:
        return ["runtime_receipt_reviewed_operation_identities_missing"]
    expected: set[tuple[str, ...]] = set()
    for mapping in mappings:
        identity = (
            _identity(mapping, _REVIEW_FIELDS) if isinstance(mapping, Mapping) else None
        )
        if identity is None or identity in expected:
            return ["runtime_receipt_reviewed_operation_identities_invalid"]
        expected.add(identity)
    inventory = semantic.get("operation_inventory")
    if (
        not isinstance(inventory, Mapping)
        or isinstance(inventory.get("v4_count"), bool)
        or inventory.get("v4_count") != len(expected)
    ):
        return ["runtime_receipt_reviewed_operation_identities_invalid"]

    errors: list[str] = []
    observed: set[tuple[str, ...]] = set()
    invocation_ids: set[str] = set()
    install_observation = _observation(installed_at)
    isolated_observation = _observation(isolated.get("observed_at"))
    for execution in executions:
        if not isinstance(execution, Mapping):
            errors.append("runtime_receipt_operation_execution_invalid")
            continue
        identity = _identity(execution, _IDENTITY_FIELDS)
        invocation_id = execution.get("invocation_id")
        if (
            identity is None
            or execution.get("method") != EXECUTION_METHOD
            or not isinstance(invocation_id, str)
            or not invocation_id.strip()
            or not isinstance(execution.get("observed_at"), str)
            or not execution["observed_at"].strip()
            or any(
                not isinstance(execution.get(field), str)
                or _DIGEST.fullmatch(execution[field]) is None
                for field in ("input_digest", "output_digest")
            )
        ):
            errors.append("runtime_receipt_operation_execution_invalid")
            continue
        if identity in observed or invocation_id in invocation_ids:
            errors.append("runtime_receipt_operation_execution_duplicate")
        observed.add(identity)
        invocation_ids.add(invocation_id)
        if (
            execution.get("pack_id") != pack_id
            or execution.get("target_digest") != isolated.get("target_digest")
            or execution.get("host_instance_id") != isolated.get("host_instance_id")
        ):
            errors.append("runtime_receipt_operation_execution_identity_mismatch")
        observation = _observation(execution["observed_at"])
        if (
            observation is None
            or install_observation is None
            or isolated_observation is None
            or not install_observation <= observation <= isolated_observation
        ):
            errors.append("runtime_receipt_operation_execution_observation_mismatch")
        if execution.get("outcome") != "passed":
            errors.append("runtime_receipt_operation_execution_failed")
    if observed != expected or len(executions) != len(expected):
        errors.append("runtime_receipt_operation_execution_set_mismatch")
    return list(dict.fromkeys(errors))
