"""Pack v4 replacement for legacy Kernel handler manifest tests."""

from pathlib import Path

from tests.legacy_authority_contracts import (
    assert_authority_kernel_rejects_payload_substitution,
    assert_retired_module_absent,
)


def test_legacy_kernel_module_is_absent() -> None:
    """The retired handler manifest cannot be used as runtime authority."""
    assert_retired_module_absent("core_runtime.kernel")


def test_authority_kernel_rejects_payload_substitution(tmp_path: Path) -> None:
    """The canonical Kernel binds authorization to captured context."""
    assert_authority_kernel_rejects_payload_substitution(tmp_path)
