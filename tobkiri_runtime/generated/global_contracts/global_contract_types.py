"""Generated from schemas/global_contract_types.schema.json."""

from __future__ import annotations

from typing import Generic, Literal, TypedDict, TypeVar

from typing_extensions import NotRequired

ContractStatus = Literal[
    'ok',
    'unknown',
    'unavailable',
    'not_configured',
    'denied',
    'incompatible',
    'missing_provider',
    'stale_resolution',
    'invalid_manifest',
]

T = TypeVar("T")


class ContractResult(TypedDict, Generic[T]):
    """Generated cross-language contract result shape."""

    status: ContractStatus
    contract_id: str
    version: str
    provider_instance_id: str
    diagnostics: NotRequired[list[str]]
    value: NotRequired[T]
