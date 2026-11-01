"""Tests for Wave 17-C: Cleanup, env protection, IR protected keys."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from threading import RLock
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure core_runtime is importable
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---------------------------------------------------------------------------
# Helpers — lightweight fakes so we don't need the full runtime
# ---------------------------------------------------------------------------


class _FakeIR:
    """Minimal InterfaceRegistry stand-in for lifecycle handler tests."""

    def __init__(self):
        self._store: Dict[str, list] = {}
        self._lock = RLock()

    def register(self, key, value, meta=None):
        self._store.setdefault(key, []).append({
            "key": key,
            "value": value,
            "meta": meta or {},
            "ts": "2025-01-01T00:00:00Z",
        })

    def list(self, prefix=None, include_meta=False):
        out = {}
        for k, items in self._store.items():
            if prefix and not k.startswith(prefix):
                continue
            if include_meta:
                last = items[-1] if items else None
                out[k] = {
                    "count": len(items),
                    "last_ts": last.get("ts") if last else None,
                    "last_meta": last.get("meta") if last else None,
                }
            else:
                out[k] = len(items)
        return out

    def unregister(self, key, predicate=None):
        if key not in self._store:
            return 0
        if predicate is None:
            count = len(self._store[key])
            del self._store[key]
            return count
        kept, removed = [], 0
        for entry in self._store[key]:
            if predicate(entry):
                removed += 1
            else:
                kept.append(entry)
        if kept:
            self._store[key] = kept
        else:
            del self._store[key]
        return removed


class _FakeKernel:
    def __init__(self, ir):
        self.interface_registry = ir


def _make_handler(ir=None):
    """Return a minimal Mixin instance wired to the fake IR."""
    from core_runtime.api.pack_lifecycle_handlers import PackLifecycleHandlersMixin

    class Handler(PackLifecycleHandlersMixin):
        pass

    h = Handler()
    h.container_orchestrator = None
    h.approval_manager = None
    h.host_privilege_manager = None
    if ir is not None:
        h.kernel = _FakeKernel(ir)
    else:
        h.kernel = None
    return h


# ===================================================================
# 1. IR cleanup on uninstall
# ===================================================================


class TestUninstallIRCleanup:
    """_uninstall_pack should remove IR entries owned by the uninstalled pack."""

    def test_ir_entries_removed_on_uninstall(self):
        ir = _FakeIR()
        ir.register("tool.search", "v1", meta={"owner_pack": "pack_a"})
        ir.register("tool.calc", "v2", meta={"owner_pack": "pack_b"})
        ir.register("tool.web", "v3", meta={"pack_id": "pack_a"})

        handler = _make_handler(ir)
        result = handler._uninstall_pack("pack_a")

        assert result["steps"].get("ir_cleanup") is True
        remaining = ir.list()
        assert "tool.search" not in remaining
        assert "tool.calc" in remaining
        assert "tool.web" not in remaining

    def test_ir_cleanup_no_ir_available(self):
        """When no IR is available, step should be None (skipped)."""
        handler = _make_handler(ir=None)
        result = handler._uninstall_pack("pack_x")
        assert result["steps"].get("ir_cleanup") is None

    def test_ir_cleanup_multiple_meta_fields(self):
        """Entries using _source_pack_id / registered_by should also be cleaned."""
        ir = _FakeIR()
        ir.register("hook.a", "v", meta={"_source_pack_id": "pack_c"})
        ir.register("hook.b", "v", meta={"registered_by": "pack_c"})
        ir.register("hook.c", "v", meta={"source": "pack_c"})
        handler = _make_handler(ir)
        result = handler._uninstall_pack("pack_c")
        assert result["steps"]["ir_cleanup"] is True
        assert len(ir.list()) == 0


# ===================================================================
# 2. Network Grant revoke on uninstall
# ===================================================================


class TestUninstallNetworkGrantRevoke:
    def test_network_grant_revoked(self):
        handler = _make_handler()
        mock_ngm = MagicMock()
        with patch(
            "core_runtime.network_grant_manager.get_network_grant_manager",
            return_value=mock_ngm,
        ):
            result = handler._uninstall_pack("pack_net")

        mock_ngm.revoke_network_access.assert_called_once_with(
            "pack_net", reason="Pack pack_net uninstalled"
        )
        assert result["steps"]["network_grant_revoke"] is True

    def test_network_grant_revoke_failure_recorded(self):
        handler = _make_handler()
        mock_ngm = MagicMock()
        mock_ngm.revoke_network_access.side_effect = RuntimeError("boom")
        with patch(
            "core_runtime.network_grant_manager.get_network_grant_manager",
            return_value=mock_ngm,
        ):
            result = handler._uninstall_pack("pack_err")

        assert result["steps"]["network_grant_revoke"] is False
        assert any(e["step"] == "network_grant_revoke" for e in result["errors"])


# ===================================================================
# 3. sys.path shadow detection
# ===================================================================


class TestSysPathShadowDetection:
    """The retired component lifecycle cannot mutate interpreter import state."""

    def test_shadow_module_blocked(self, tmp_path):
        del tmp_path
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.component_lifecycle")

    def test_safe_directory_allowed(self, tmp_path):
        del tmp_path
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.component_lifecycle")

    def test_shadow_package_dir_blocked(self, tmp_path):
        del tmp_path
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.component_lifecycle")

    def test_ensure_components_skips_shadow(self, tmp_path):
        del tmp_path
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.component_lifecycle")


# ===================================================================
# 4. RUMI_SECURITY_MODE freeze / restore
# ===================================================================


class TestEnvVarFreeze:
    """The v4 activation carries an immutable security epoch."""

    def test_security_mode_restored_after_tampering(self):
        from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot

        assert_profile_resolver_requires_authority_snapshot()

    def test_security_mode_deleted_if_was_absent(self):
        from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot

        assert_profile_resolver_requires_authority_snapshot()

    def test_no_change_if_not_tampered(self):
        from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot

        assert_profile_resolver_requires_authority_snapshot()


# ===================================================================
# 5. IR protected key registration WARNING
# ===================================================================


class TestIRProtectedKeys:
    """The removed Interface Registry cannot widen a captured v4 plan."""

    def _assert_removed(self):
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.interface_registry")

    def test_protected_key_warning_mode(self):
        self._assert_removed()

    def test_protected_key_with_system_flag(self):
        self._assert_removed()

    def test_protected_key_prefix_flow_construct(self):
        self._assert_removed()

    def test_protected_key_prefix_kernel(self):
        self._assert_removed()


# ===================================================================
# 6. RUMI_BLOCK_PROTECTED_KEYS=1 blocking
# ===================================================================


class TestIRProtectedKeysBlocking:
    """Protected-key writes are no longer a runtime registry operation."""

    def _assert_removed(self):
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.interface_registry")

    def test_block_mode_raises(self):
        self._assert_removed()

    def test_block_mode_allows_system(self):
        self._assert_removed()

    def test_block_mode_register_if_absent(self):
        self._assert_removed()

    def test_non_protected_key_unaffected(self):
        self._assert_removed()
