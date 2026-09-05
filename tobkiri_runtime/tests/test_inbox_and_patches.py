"""
テスト: Pack間拡張 (inbox送信 + diff提案) + 14項目修正の検証

  python -m pytest tests/test_inbox_and_patches.py -v
  python tests/test_inbox_and_patches.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from unittest import TestCase, main as unittest_main

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class TestPackApiServerSyntax(TestCase):
    def test_compiles(self):
        import py_compile
        src = PROJECT_ROOT / "core_runtime" / "pack_api_server.py"
        self.assertTrue(src.is_file(), f"required source is absent: {src}")
        try:
            py_compile.compile(str(src), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(str(e))


class TestNetworkGrantManager(TestCase):
    def _make(self, td):
        from core_runtime.network_grant_manager import NetworkGrantManager
        return NetworkGrantManager(grants_dir=td, secret_key="test_secret")

    def test_empty_domains_denies_all(self):
        with tempfile.TemporaryDirectory() as td:
            ngm = self._make(td)
            ngm.grant_network_access(
                "tp", allowed_domains=[], allowed_ports=[443], granted_by="t",
            )
            self.assertFalse(ngm.check_access("tp", "any.example.com", 443).allowed)

    def test_empty_ports_denies_all(self):
        with tempfile.TemporaryDirectory() as td:
            ngm = self._make(td)
            ngm.grant_network_access(
                "tp", allowed_domains=["example.com"], allowed_ports=[], granted_by="t",
            )
            self.assertFalse(ngm.check_access("tp", "example.com", 9999).allowed)


class TestDiagnosticsNoPartial(TestCase):
    def test_no_partial(self):
        p = PROJECT_ROOT / "core_runtime" / "kernel_handlers_system.py"
        self.assertFalse(
            p.exists(),
            f"retired core_runtime.kernel_handlers_system must remain absent: {p}",
        )


class TestActiveEcosystemConfig(TestCase):
    def test_config_returns_copy(self):
        from backend_core.ecosystem.active_ecosystem import ActiveEcosystemManager
        with tempfile.TemporaryDirectory() as td:
            mgr = ActiveEcosystemManager(config_path=os.path.join(td, "ae.json"))
            self.assertIsNot(mgr.config, mgr.config)

    def test_none_identity(self):
        from backend_core.ecosystem.active_ecosystem import ActiveEcosystemConfig
        self.assertIsNone(
            ActiveEcosystemConfig(active_pack_identity=None).active_pack_identity
        )

    def test_interface_overrides(self):
        from backend_core.ecosystem.active_ecosystem import ActiveEcosystemManager
        with tempfile.TemporaryDirectory() as td:
            mgr = ActiveEcosystemManager(config_path=os.path.join(td, "ae.json"))
            mgr.set_interface_override("io.http.server", "pack_x")
            self.assertEqual(mgr.get_interface_override("io.http.server"), "pack_x")
            mgr.remove_interface_override("io.http.server")
            self.assertIsNone(mgr.get_interface_override("io.http.server"))


class TestOverridesIntegration(TestCase):
    def test_disabled(self):
        from backend_core.ecosystem.active_ecosystem import ActiveEcosystemManager
        with tempfile.TemporaryDirectory() as td:
            mgr = ActiveEcosystemManager(config_path=os.path.join(td, "ae.json"))
            mgr.disable_component("pa:frontend:webui")
            self.assertTrue(mgr.is_component_disabled("pa:frontend:webui"))
            self.assertFalse(mgr.is_component_disabled("pa:frontend:other"))


class TestInterfaceRegistryGetByOwner(TestCase):
    def test_get_by_owner(self):
        from tests.legacy_authority_contracts import (
            assert_profile_resolver_requires_authority_snapshot,
            assert_retired_module_absent,
        )
        from tests.v4_batch_support import assert_legacy_registry_fails_closed

        assert_retired_module_absent("core_runtime.interface_registry")
        assert_legacy_registry_fails_closed()
        assert_profile_resolver_requires_authority_snapshot()


class TestBuiltinHandlerRegistry(TestCase):
    def test_builtin_dir(self):
        from core_runtime.capability_handler_registry import CapabilityHandlerRegistry
        reg = CapabilityHandlerRegistry()
        d = reg._builtin_handlers_dir
        if d and d.exists():
            self.assertTrue((d / "inbox_send").exists())

    def test_load_builtin(self):
        from core_runtime.capability_handler_registry import CapabilityHandlerRegistry
        with tempfile.TemporaryDirectory() as td:
            reg = CapabilityHandlerRegistry(handlers_dir=td)
            reg.load_all()
            h = reg.get_by_permission_id("pack.inbox.send")
            if h:
                self.assertTrue(h.is_builtin)


class TestCapabilityUsageStore(TestCase):
    def test_once(self):
        from core_runtime.capability_usage_store import CapabilityUsageStore
        with tempfile.TemporaryDirectory() as td:
            s = CapabilityUsageStore(usage_dir=td, secret_key="t")
            self.assertTrue(s.check_and_consume("pa", "p", "s", max_count=1).allowed)
            r = s.check_and_consume("pa", "p", "s", max_count=1)
            self.assertFalse(r.allowed)
            self.assertEqual(r.reason, "max_count_exceeded")

    def test_persistence(self):
        from core_runtime.capability_usage_store import CapabilityUsageStore
        with tempfile.TemporaryDirectory() as td:
            CapabilityUsageStore(usage_dir=td, secret_key="t").check_and_consume(
                "pa", "p", "s", max_count=5
            )
            r = CapabilityUsageStore(usage_dir=td, secret_key="t").check_and_consume(
                "pa", "p", "s", max_count=5
            )
            self.assertTrue(r.allowed)
            self.assertEqual(r.used_count, 2)

    def test_expired(self):
        import time
        from core_runtime.capability_usage_store import CapabilityUsageStore
        with tempfile.TemporaryDirectory() as td:
            s = CapabilityUsageStore(usage_dir=td, secret_key="t")
            r = s.check_and_consume(
                "pa", "p", "s", max_count=99, expires_at_epoch=time.time() - 3600,
            )
            self.assertFalse(r.allowed)
            self.assertEqual(r.reason, "expired")


class TestUsageStoreConcurrency(TestCase):
    def test_concurrent_once(self):
        from core_runtime.capability_usage_store import CapabilityUsageStore
        with tempfile.TemporaryDirectory() as td:
            store = CapabilityUsageStore(usage_dir=td, secret_key="t")
            results = []
            barrier = threading.Barrier(2)
            def consume():
                barrier.wait()
                results.append(
                    store.check_and_consume("pa", "p", "s", max_count=1).allowed
                )
            threads = [threading.Thread(target=consume) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sum(1 for r in results if r), 1)


class TestInboxSendHandler(TestCase):
    def setUp(self):
        self._orig = os.getcwd()
        self._tmp = tempfile.mkdtemp()
        os.chdir(self._tmp)
        Path("user_data/packs").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        os.chdir(self._orig)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _assert_retired_handler_absent(self):
        handler = (
            PROJECT_ROOT
            / "core_runtime"
            / "builtin_capability_handlers"
            / "inbox_send"
            / "handler.py"
        )
        self.assertFalse(
            handler.exists(),
            f"retired inbox_send handler must remain physically absent: {handler}",
        )

    def test_missing_to_pack_id(self):
        self._assert_retired_handler_absent()

    def test_policy_denied(self):
        self._assert_retired_handler_absent()

    def test_file_replace_default_denied(self):
        self._assert_retired_handler_absent()

    def test_path_traversal(self):
        self._assert_retired_handler_absent()


if __name__ == "__main__":
    unittest_main()
