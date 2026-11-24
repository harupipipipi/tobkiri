"""Bounded DATA adapter for Defaultspack's original capability projection."""

import json
import math

from core_runtime.global_contracts.http_contract_dispatch import (
    HTTPContractBinding,
    HTTPContractTarget,
)
from ecosystem.defaultspack.defaultspack.http_dynamic_targets import (
    defaultspack_dynamic_capability_targets,
)

MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024

_ENVELOPE_FIELDS = frozenset({"binding", "catalog"})
_BINDING_FIELDS = frozenset({"method", "path", "presentation", "targets"})
_TARGET_FIELDS = frozenset({
    "contribution_id",
    "contract_id",
    "operation_id",
    "provider_id",
    "function_id",
})


class ProjectionError(RuntimeError):
    """A bounded, safe error returned by the capability projection boundary."""

    def __init__(self, code: str, message: str) -> None:
        if code not in {"invalid-request", "projection-failed"}:
            raise ValueError("unsupported projection error code")
        self.code = code
        self.message = message
        super().__init__(message)


def _invalid_request() -> None:
    """Raise the shared safe invalid-request error."""
    raise ProjectionError("invalid-request", "invalid capability projection request")


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object while rejecting every repeated key."""
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _reject_nonfinite_number(token: str) -> None:
    """Reject non-standard JSON constants such as NaN and Infinity."""
    raise ValueError("non-finite JSON number")


def _parse_finite_float(token: str) -> float:
    """Parse a JSON float and reject exponent overflow to infinity."""
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    return value


def _is_exact_string_fields(value: object, fields: frozenset[str]) -> bool:
    """Return whether a plain object has exactly the named string fields."""
    return (
        type(value) is dict
        and value.keys() == fields
        and all(type(value[field]) is str for field in fields)
    )


def _decode_request(payload: str) -> tuple[HTTPContractBinding, dict[str, object]]:
    """Parse the exact public DATA envelope without admitting Host state."""
    if type(payload) is not str:
        _invalid_request()
    try:
        if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            _invalid_request()
        value = json.loads(
            payload,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_nonfinite_number,
            parse_float=_parse_finite_float,
        )
    except (UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
        _invalid_request()

    if type(value) is not dict or value.keys() != _ENVELOPE_FIELDS:
        _invalid_request()
    binding_data = value["binding"]
    catalog = value["catalog"]
    if type(binding_data) is not dict or binding_data.keys() != _BINDING_FIELDS:
        _invalid_request()
    if any(
        type(binding_data[field]) is not str
        for field in ("method", "path", "presentation")
    ):
        _invalid_request()
    if type(binding_data["targets"]) is not list or type(catalog) is not dict:
        _invalid_request()

    targets: list[HTTPContractTarget] = []
    for target_data in binding_data["targets"]:
        if not _is_exact_string_fields(target_data, _TARGET_FIELDS):
            _invalid_request()
        targets.append(HTTPContractTarget(
            contribution_id=target_data["contribution_id"],
            contract_id=target_data["contract_id"],
            operation_id=target_data["operation_id"],
            provider_id=target_data["provider_id"],
            function_id=target_data["function_id"],
        ))

    binding = HTTPContractBinding(
        method=binding_data["method"],
        path=binding_data["path"],
        presentation=binding_data["presentation"],
        targets=tuple(targets),
    )
    return binding, catalog


def _target_data(target: HTTPContractTarget) -> dict[str, object]:
    """Return all projected target DATA, preserving the original schema bytes."""
    return {
        "contribution_id": target.contribution_id,
        "contract_id": target.contract_id,
        "operation_id": target.operation_id,
        "provider_id": target.provider_id,
        "function_id": target.function_id,
        "allowed_payload_keys": sorted(target.allowed_payload_keys),
        "owner_pack_id": target.owner_pack_id,
        "artifact_digest": target.artifact_digest,
        "input_schema": target.input_schema.decode("utf-8"),
        "read_only": target.read_only,
    }


def project(payload: str) -> str:
    """Project capability target DATA through the unchanged original function."""
    try:
        binding, catalog = _decode_request(payload)
        targets = defaultspack_dynamic_capability_targets(binding, catalog=catalog)
        output = json.dumps(
            {"targets": [_target_data(target) for target in targets]},
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        )
        if len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ProjectionError(
                "projection-failed", "capability projection output exceeded its bound",
            )
        return output
    except ProjectionError:
        raise
    except Exception:
        raise ProjectionError(
            "projection-failed", "capability projection failed",
        ) from None
