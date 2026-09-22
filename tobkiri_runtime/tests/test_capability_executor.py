"""Pack v4 migration contract for the retired capability executor."""

from tests.legacy_authority_contracts import assert_retired_module_absent


def test_retired_capability_executor_is_absent() -> None:
    """The deleted executor cannot be imported as a compatibility path."""
    assert_retired_module_absent("core_runtime.capability_executor")
