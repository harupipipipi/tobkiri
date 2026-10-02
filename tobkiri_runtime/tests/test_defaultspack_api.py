from __future__ import annotations

import json
import importlib
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _pack_api_sibling_module(handler_cls, module_name: str):
    package_name = (
        handler_cls._execute_api_route_pack_function.__globals__.get("__package__")
        or handler_cls.__module__.rsplit(".", 1)[0]
    )
    return importlib.import_module(f"{package_name}.{module_name}")


class TestDefaultspackApiRoutes(unittest.TestCase):
    def _assert_v4_api_boundary(
        self,
        *,
        retired_module="core_runtime.capability_executor",
        check_registry=False,
    ):
        from tempfile import TemporaryDirectory

        from tests.legacy_authority_contracts import (
            assert_profile_resolver_requires_authority_snapshot,
            assert_retired_module_absent,
        )
        from tests.v4_batch_support import (
            assert_legacy_registry_fails_closed,
            assert_payload_mutations_denied,
            harness,
        )

        assert_retired_module_absent(retired_module)
        assert_profile_resolver_requires_authority_snapshot()
        if check_registry:
            assert_legacy_registry_fails_closed()
        with TemporaryDirectory() as root:
            assert_payload_mutations_denied(harness(Path(root)))

    def test_defaultspack_ecosystem_routes_are_loaded(self):
        from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog

        legacy_manifest = (
            Path(__file__).resolve().parent.parent
            / "ecosystem"
            / "defaultspack"
            / "ecosystem.json"
        )
        self.assertFalse(legacy_manifest.exists())
        catalog = BundledCatalog.load(legacy_manifest.parent / "v4")
        self.assertGreater(len(catalog.packs), 0)
        self.assertEqual(len(catalog.packs), len(set(catalog.packs)))
        self._assert_v4_api_boundary(check_registry=True)

    def test_untrusted_function_api_routes_are_not_loaded(self):
        self._assert_v4_api_boundary(check_registry=True)

    def test_api_route_blocks_untrusted_pack_function_dispatch(self):
        self._assert_v4_api_boundary()

    def test_api_route_dispatches_pack_function(self):
        self._assert_v4_api_boundary()

    def test_api_route_keeps_route_args_over_body(self):
        self._assert_v4_api_boundary()

    def test_api_route_passes_query_to_pack_function(self):
        self._assert_v4_api_boundary()

    def test_defaultspack_api_route_unwraps_function_ok_envelope(self):
        self._assert_v4_api_boundary()

    def test_remote_api_route_unwraps_function_ok_envelope(self):
        self._assert_v4_api_boundary()

    def test_remote_api_route_invalid_input_returns_400(self):
        self._assert_v4_api_boundary()

    def test_remote_api_route_not_found_returns_404(self):
        self._assert_v4_api_boundary()

    def test_api_route_function_permission_denial_sends_forbidden(self):
        self._assert_v4_api_boundary()

    def test_api_route_function_not_found_falls_back_to_legacy_route(self):
        self._assert_v4_api_boundary(check_registry=True)


if __name__ == "__main__":
    unittest.main()
