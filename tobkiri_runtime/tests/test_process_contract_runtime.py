"""Pack v4 replacement for legacy process-contract registration tests."""

import pytest

from tests.legacy_authority_contracts import assert_retired_module_absent

pytestmark = pytest.mark.contract


def test_process_contract_registration_authority_is_absent() -> None:
    """Process execution is not registered through the deleted module."""
    assert_retired_module_absent("core_runtime.capability_binding_registration")
