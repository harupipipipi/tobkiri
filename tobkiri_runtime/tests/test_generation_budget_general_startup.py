"""Host template defaults and rejection of renderer-supplied budget flags."""

import pytest

from core_runtime.native_generation_budget import (
    NativeGenerationBudgetTemplate,
    owner_startup_generation_budget_template,
)

# The separately reviewed signed test variant changes this expectation only.
_EXPECTED_STARTUP_TEMPLATE = NativeGenerationBudgetTemplate("defaults", "provider.main")


def test_general_runtime_defaults_to_reviewed_host_template():
    assert owner_startup_generation_budget_template() == _EXPECTED_STARTUP_TEMPLATE
    policy = NativeGenerationBudgetTemplate("defaults", "provider.main")
    assert owner_startup_generation_budget_template(policy) == policy


@pytest.mark.parametrize(
    "client_value", [True, {}, {"approved": True}, {"max_requests": 999}]
)
def test_general_runtime_rejects_renderer_flags_as_host_policy(client_value):
    with pytest.raises(PermissionError):
        owner_startup_generation_budget_template(client_value)
