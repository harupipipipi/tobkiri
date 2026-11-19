"""Verify ordinary startup and explicit Host policy selection."""

from dataclasses import replace

import pytest

from core_runtime.native_generation_budget import (
    owner_isolated_generation_budget_template,
    owner_startup_generation_budget_template,
)


def test_general_runtime_requires_explicit_isolated_host_template() -> None:
    assert owner_startup_generation_budget_template() is None
    policy = owner_isolated_generation_budget_template()
    assert owner_startup_generation_budget_template(policy) == policy
    with pytest.raises(PermissionError):
        owner_startup_generation_budget_template(
            replace(policy, max_prompt_price_microusd_per_million=1_000_001)
        )


@pytest.mark.parametrize(
    "client_value", [True, {}, {"approved": True}, {"max_requests": 999}]
)
def test_general_runtime_rejects_renderer_flags_as_host_policy(
    client_value: object,
) -> None:
    with pytest.raises(PermissionError):
        owner_startup_generation_budget_template(client_value)
