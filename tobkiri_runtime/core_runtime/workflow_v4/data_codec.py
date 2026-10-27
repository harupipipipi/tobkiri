"""Explicit Workflow data-field policies; all other envelope fields stay strict.

Never replace models.digest: schemas, catalogs, capabilities and request IDs
remain authority-canonical. These helpers cover only runtime-owned records.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tobkiri_protocol.data_codec import CodecError, Path, digest_record


def _step_paths(document: Any, field: str, prefix: Path = ()) -> tuple[Path, ...]:
    if not isinstance(document, Mapping) or type(document.get("steps")) is not list:
        return ()
    steps = document["steps"]
    # A record can nominate at most 4,096 data leaves. Reject prior to walking
    # arbitrary step counts, and retain the codec's whole-record node/byte cap.
    if len(steps) > 4096:
        raise CodecError("workflow exceeds data path limit")
    return tuple(
        prefix + ("steps", index, field, "input")
        for index, step in enumerate(steps)
        if isinstance(step, Mapping)
        and isinstance(step.get(field), Mapping) and "input" in step[field]
    )


def record_paths(record: Mapping[str, Any], kind: str) -> tuple[Path, ...]:
    """Select data fields from a trusted record kind, never a wire discriminator."""
    if kind == "document":
        return _step_paths(record, "request")
    if kind == "compiled":
        return _step_paths(record, "contract_request")
    if kind == "definition":
        return (
            _step_paths(record.get("document"), "request", ("document",))
            + _step_paths(record.get("compiled"), "contract_request", ("compiled",))
        )
    if kind == "run":
        return (("inputs",),) if "inputs" in record else ()
    if kind == "request":
        return (("input",),) if "input" in record else ()
    if kind == "outcome":
        return (("output",),) if "output" in record else ()
    if kind == "attempt":
        paths = (("request", "input"),) if (
            isinstance(record.get("request"), Mapping) and "input" in record["request"]
        ) else ()
        return paths + ((("outcome",),) if "outcome" in record else ())
    if kind == "checkpoint":
        return ()
    raise CodecError("unknown trusted workflow record kind")


def _digest(record: Mapping[str, Any], kind: str) -> str:
    value = dict(record)
    return digest_record(value, value_paths=record_paths(value, kind))


def document_digest(document: Mapping[str, Any]) -> str:
    """Validate/hash a definition document with data-only request inputs."""
    return _digest(document, "document")


def definition_digest(record: Mapping[str, Any]) -> str:
    """Hash one revision record with document/compiled input data selected."""
    return _digest(record, "definition")


def compiled_digest(record: Mapping[str, Any]) -> str:
    """Hash a compiled plan without loosening schema or catalog metadata."""
    return _digest(record, "compiled")


def request_digest(record: Mapping[str, Any]) -> str:
    """Hash the Flow request envelope with only input designated as data."""
    return _digest(record, "request")


def outcome_digest(record: Mapping[str, Any]) -> str:
    """Hash a provider outcome while keeping its control status strict."""
    return _digest(record, "outcome")
