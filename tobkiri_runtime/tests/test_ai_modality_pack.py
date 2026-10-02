"""External-QA-oriented modality gateway specifications."""

from __future__ import annotations

import pytest

from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from ecosystem.rumi_ai_modality_pack.runtime.gateway import (
    create_embedding_operation,
)


class FakeClient:
    def __init__(self, providers, value=None):
        self._providers = providers
        self._value = value

    def providers(self, contract_id):
        del contract_id
        return self._providers

    def invoke(self, contract_id, operation, payload, *, provider_instance_id=None):
        del contract_id, operation, payload, provider_instance_id
        return self._value


def test_embedding_normalizes_finite_vectors() -> None:
    operation = create_embedding_operation(
        FakeClient(
            ({"provider_instance_id": "embedding.fixture"},),
            {"vectors": [[1, 2.5]], "usage": {"input_tokens": 2}},
        )
    )

    result = operation("embed", {"input": ["hello"]})
    assert result["vectors"] == [[1.0, 2.5]]
    assert result["provider_instance_id"] == "embedding.fixture"


def test_missing_provider_is_not_collapsed_to_empty_result() -> None:
    operation = create_embedding_operation(FakeClient(()))

    with pytest.raises(GlobalContractInvocationError) as captured:
        operation("embed", {"input": ["hello"]})

    assert captured.value.code == "missing_provider"

