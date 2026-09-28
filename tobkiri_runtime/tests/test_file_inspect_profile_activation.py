"""Pack v4 replacement for legacy file-inspect profile activation tests."""

import subprocess
import sys
from pathlib import Path

from tests.legacy_authority_contracts import (
    assert_profile_resolver_requires_authority_snapshot,
)


def test_file_inspect_activation_has_no_legacy_authority_imports() -> None:
    """File inspect activation is not assembled by deleted runtime modules."""
    runtime = Path(__file__).resolve().parents[1]
    for module_name in (
        "core_runtime.capability_binding_registration",
        "core_runtime.interface_registry",
        "core_runtime.startup_capability_bridge",
    ):
        module_path = runtime.joinpath(*module_name.split(".")).with_suffix(".py")
        assert not module_path.exists()
        # -S excludes editable installs of another checkout, which otherwise
        # can make a deleted module appear importable from this workspace.
        result = subprocess.run(
            [
                sys.executable,
                "-E",
                "-S",
                "-c",
                f"import sys; sys.path.insert(0, {str(runtime)!r}); import {module_name}",
            ],
            cwd=runtime,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode != 0
        assert f"No module named '{module_name}'" in result.stderr


def test_file_inspect_profile_requires_authority_snapshot() -> None:
    """The v4 resolver rejects activation without Kernel references."""
    assert_profile_resolver_requires_authority_snapshot()
