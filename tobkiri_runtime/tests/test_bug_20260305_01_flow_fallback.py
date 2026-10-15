"""Regression coverage for the retired legacy flow/dependency authority."""

from __future__ import annotations

import unittest
from pathlib import Path

import pytest


def _assert_v4_flow_boundary() -> None:
    """Exercise the current fail-closed boundary replacing legacy flow loading."""
    from tempfile import TemporaryDirectory

    from tests.legacy_authority_contracts import (
        assert_profile_resolver_requires_authority_snapshot,
    )
    from tests.v4_batch_support import assert_payload_mutations_denied, harness

    assert not (
        Path(__file__).resolve().parents[1] / "core_runtime" / "kernel_core.py"
    ).exists()
    assert_profile_resolver_requires_authority_snapshot()
    with TemporaryDirectory() as root:
        assert_payload_mutations_denied(harness(Path(root)))


def _assert_current_entrypoint() -> None:
    """Require the v4 entrypoint to expose no legacy dependency guard."""
    import app

    assert not hasattr(app, "_check_critical_dependencies")
    help_text = app._parser().format_help()
    assert "--permissive" not in help_text
    with pytest.raises(SystemExit) as exc:
        app._parser().parse_args(["--permissive"])
    assert exc.value.code == 2


class TestParseFlowText(unittest.TestCase):
    """The removed flow text loader has a v4 fail-closed boundary."""

    def test_import_error_chains_cause(self):
        _assert_v4_flow_boundary()

    def test_yaml_not_dict_no_import_cause(self):
        _assert_v4_flow_boundary()

    def test_normal_yaml_dict(self):
        _assert_v4_flow_boundary()


class TestLoadFlowStderr(unittest.TestCase):
    """Legacy flow loading cannot become a runtime authority."""

    def test_import_error_stderr(self):
        _assert_v4_flow_boundary()

    def test_value_error_stderr(self):
        _assert_v4_flow_boundary()

    def test_generic_error_stderr(self):
        _assert_v4_flow_boundary()

    def test_flow_degraded_after_fallback(self):
        _assert_v4_flow_boundary()


class TestLogFallbackWarning(unittest.TestCase):
    """Legacy fallback warning surfaces are absent from the v4 host."""

    def test_stderr_only(self):
        _assert_v4_flow_boundary()


class TestCheckCriticalDependencies(unittest.TestCase):
    """The removed dependency guard cannot be imported or invoked."""

    def test_yaml_missing(self):
        _assert_current_entrypoint()

    def test_cryptography_missing(self):
        _assert_current_entrypoint()

    def test_all_present(self):
        _assert_current_entrypoint()


class TestMinimalFallbackFlow(unittest.TestCase):
    """The removed minimal fallback has no production authority."""

    def test_three_steps_only(self):
        _assert_v4_flow_boundary()


if __name__ == "__main__":
    unittest.main()
