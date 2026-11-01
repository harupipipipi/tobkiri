"""
test_wave17b_auth_hardening.py - Wave 17-B: Auth Hardening Tests

Tests (22 total):
  1-3:   TrustStore HMAC (save / load / tamper)
  4-5:   SharedStoreManager HMAC (save / tamper)
  6-7:   CapabilityInstaller index HMAC (save / tamper)
  8-9:   CapabilityInstaller blocked HMAC (save / tamper)
  10-13: Legacy unsigned backward compat (4 file types)
  14:    RUMI_REQUIRE_HMAC=1 rejects unsigned
  15-16: rollback_to_version hash mismatch / match
  17:    verify_hash use_cache=False
  18-19: principal_id spoofing / None pack
  20-22: PermissionManager mode (warning / env / explicit)
"""

from __future__ import annotations

import json
import os
import tempfile
import shutil
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_dir():
    """Create a temp directory and clean up after test."""
    d = tempfile.mkdtemp(prefix="w17b_test_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1-3. HMAC: CapabilityTrustStore
# ---------------------------------------------------------------------------

class TestTrustStoreHMAC:
    def test_save_generates_hmac_signature(self, tmp_dir):
        """_save() writes _hmac_signature into trusted_handlers.json."""
        from core_runtime.capability_trust_store import CapabilityTrustStore

        store = CapabilityTrustStore(trust_dir=str(tmp_dir))
        store.load()
        sha = "a" * 64
        assert store.add_trust("h1", sha, "test")

        trust_file = tmp_dir / "trusted_handlers.json"
        assert trust_file.exists()
        data = json.loads(trust_file.read_text("utf-8"))
        assert "_hmac_signature" in data
        assert isinstance(data["_hmac_signature"], str)
        assert len(data["_hmac_signature"]) == 64  # sha256 hex

    def test_load_verifies_hmac_signature(self, tmp_dir):
        """load() succeeds when HMAC matches."""
        from core_runtime.capability_trust_store import CapabilityTrustStore

        store = CapabilityTrustStore(trust_dir=str(tmp_dir))
        store.load()
        sha = "b" * 64
        store.add_trust("h2", sha, "test")

        store2 = CapabilityTrustStore(trust_dir=str(tmp_dir))
        assert store2.load() is True
        result = store2.is_trusted("h2", sha)
        assert result.trusted is True

    def test_load_rejects_tampered_file(self, tmp_dir):
        """load() fails when file content is tampered."""
        from core_runtime.capability_trust_store import CapabilityTrustStore

        store = CapabilityTrustStore(trust_dir=str(tmp_dir))
        store.load()
        sha = "c" * 64
        store.add_trust("h3", sha, "test")

        trust_file = tmp_dir / "trusted_handlers.json"
        data = json.loads(trust_file.read_text("utf-8"))
        data["trusted"][0]["note"] = "TAMPERED"
        trust_file.write_text(json.dumps(data), "utf-8")

        store2 = CapabilityTrustStore(trust_dir=str(tmp_dir))
        assert store2.load() is False
        assert store2.list_trusted() == []


# ---------------------------------------------------------------------------
# 4-5. HMAC: SharedStoreManager
# ---------------------------------------------------------------------------

class TestSharingManagerHMAC:
    def test_save_generates_hmac_signature(self, tmp_dir):
        """_save() writes _hmac_signature into sharing.json."""
        from core_runtime.store_sharing_manager import SharedStoreManager

        index_path = str(tmp_dir / "sharing.json")
        mgr = SharedStoreManager(index_path=index_path)
        mgr.approve_sharing("provA", "consB", "store1")

        data = json.loads(Path(index_path).read_text("utf-8"))
        assert "_hmac_signature" in data

    def test_load_rejects_tampered_file(self, tmp_dir):
        """_load() clears entries when HMAC fails."""
        from core_runtime.store_sharing_manager import SharedStoreManager

        index_path = str(tmp_dir / "sharing.json")
        mgr = SharedStoreManager(index_path=index_path)
        mgr.approve_sharing("provA", "consB", "store1")
        assert mgr.is_sharing_approved("consB", "store1") is True

        data = json.loads(Path(index_path).read_text("utf-8"))
        data["version"] = "TAMPERED"
        Path(index_path).write_text(json.dumps(data), "utf-8")

        mgr2 = SharedStoreManager(index_path=index_path)
        assert mgr2.is_sharing_approved("consB", "store1") is False


# ---------------------------------------------------------------------------
# 6-7. HMAC: CapabilityInstaller index.json
# ---------------------------------------------------------------------------

class TestInstallerIndexHMAC:
    def test_save_index_generates_hmac(self, tmp_dir):
        """_save_index() writes _hmac_signature into index.json."""
        from core_runtime.capability_installer import CapabilityInstaller

        inst = CapabilityInstaller(
            requests_dir=str(tmp_dir / "requests"),
            handlers_dest_dir=str(tmp_dir / "handlers"),
        )
        inst._save_index()

        index_file = tmp_dir / "requests" / "index.json"
        assert index_file.exists()
        data = json.loads(index_file.read_text("utf-8"))
        assert "_hmac_signature" in data

    def test_load_index_rejects_tampered(self, tmp_dir):
        """_load_index() clears items when HMAC fails."""
        from core_runtime.capability_installer import CapabilityInstaller

        req_dir = str(tmp_dir / "requests")
        inst = CapabilityInstaller(
            requests_dir=req_dir,
            handlers_dest_dir=str(tmp_dir / "handlers"),
        )
        inst._save_index()

        index_file = Path(req_dir) / "index.json"
        data = json.loads(index_file.read_text("utf-8"))
        data["version"] = "TAMPERED"
        index_file.write_text(json.dumps(data), "utf-8")

        inst2 = CapabilityInstaller(
            requests_dir=req_dir,
            handlers_dest_dir=str(tmp_dir / "handlers"),
        )
        assert inst2.list_items() == []


# ---------------------------------------------------------------------------
# 8-9. HMAC: CapabilityInstaller blocked.json
# ---------------------------------------------------------------------------

class TestInstallerBlockedHMAC:
    def test_save_blocked_generates_hmac(self, tmp_dir):
        """_save_blocked() writes _hmac_signature into blocked.json."""
        from core_runtime.capability_installer import CapabilityInstaller

        inst = CapabilityInstaller(
            requests_dir=str(tmp_dir / "requests"),
            handlers_dest_dir=str(tmp_dir / "handlers"),
        )
        inst._blocked = {"key1": {"blocked_at": "2025-01-01T00:00:00Z"}}
        inst._save_blocked()

        blocked_file = tmp_dir / "requests" / "blocked.json"
        assert blocked_file.exists()
        data = json.loads(blocked_file.read_text("utf-8"))
        assert "_hmac_signature" in data

    def test_load_blocked_rejects_tampered(self, tmp_dir):
        """_load_blocked() clears blocked when HMAC fails."""
        from core_runtime.capability_installer import CapabilityInstaller

        req_dir = str(tmp_dir / "requests")
        inst = CapabilityInstaller(
            requests_dir=req_dir,
            handlers_dest_dir=str(tmp_dir / "handlers"),
        )
        inst._blocked = {"key1": {"blocked_at": "2025-01-01T00:00:00Z"}}
        inst._save_blocked()

        blocked_file = Path(req_dir) / "blocked.json"
        data = json.loads(blocked_file.read_text("utf-8"))
        data["blocked"]["key1"]["blocked_at"] = "TAMPERED"
        blocked_file.write_text(json.dumps(data), "utf-8")

        inst2 = CapabilityInstaller(
            requests_dir=req_dir,
            handlers_dest_dir=str(tmp_dir / "handlers"),
        )
        assert inst2.list_blocked() == {}


# ---------------------------------------------------------------------------
# 10-13. Legacy (unsigned) file backward compatibility
# ---------------------------------------------------------------------------

class TestLegacyBackwardCompat:
    def test_trust_store_loads_unsigned_file(self, tmp_dir):
        """TrustStore loads legacy file without HMAC (warning only)."""
        from core_runtime.capability_trust_store import CapabilityTrustStore

        trust_file = tmp_dir / "trusted_handlers.json"
        legacy_data = {
            "version": "1.0",
            "trusted_at": "2025-01-01T00:00:00Z",
            "trusted": [
                {"handler_id": "h_legacy", "sha256": "d" * 64, "note": "legacy"}
            ],
        }
        trust_file.write_text(json.dumps(legacy_data), "utf-8")

        with mock.patch.dict(os.environ, {"RUMI_REQUIRE_HMAC": "0"}):
            store = CapabilityTrustStore(trust_dir=str(tmp_dir))
            assert store.load() is True
            result = store.is_trusted("h_legacy", "d" * 64)
            assert result.trusted is True

    def test_sharing_manager_loads_unsigned_file(self, tmp_dir):
        """SharedStoreManager loads legacy file without HMAC."""
        index_path = tmp_dir / "sharing.json"
        legacy_data = {
            "version": "1.0",
            "updated_at": "2025-01-01T00:00:00Z",
            "entries": {
                "prov:cons:sid": {
                    "provider_pack_id": "prov",
                    "consumer_pack_id": "cons",
                    "store_id": "sid",
                    "approved_at": "2025-01-01T00:00:00Z",
                }
            },
        }
        index_path.write_text(json.dumps(legacy_data), "utf-8")

        from core_runtime.store_sharing_manager import SharedStoreManager
        with mock.patch.dict(os.environ, {"RUMI_REQUIRE_HMAC": "0"}):
            mgr = SharedStoreManager(index_path=str(index_path))
            assert mgr.is_sharing_approved("cons", "sid") is True

    def test_installer_index_loads_unsigned_file(self, tmp_dir):
        """CapabilityInstaller loads legacy index.json without HMAC."""
        req_dir = tmp_dir / "requests"
        req_dir.mkdir(parents=True)
        index_file = req_dir / "index.json"
        legacy_data = {
            "version": "1.0",
            "updated_at": "2025-01-01T00:00:00Z",
            "cooldown_seconds": 3600,
            "reject_threshold": 3,
            "items": {},
        }
        index_file.write_text(json.dumps(legacy_data), "utf-8")

        from core_runtime.capability_installer import CapabilityInstaller
        with mock.patch.dict(os.environ, {"RUMI_REQUIRE_HMAC": "0"}):
            inst = CapabilityInstaller(
                requests_dir=str(req_dir),
                handlers_dest_dir=str(tmp_dir / "handlers"),
            )
            assert inst.list_items() == []

    def test_installer_blocked_loads_unsigned_file(self, tmp_dir):
        """CapabilityInstaller loads legacy blocked.json without HMAC."""
        req_dir = tmp_dir / "requests"
        req_dir.mkdir(parents=True)
        blocked_file = req_dir / "blocked.json"
        legacy_data = {
            "version": "1.0",
            "updated_at": "2025-01-01T00:00:00Z",
            "blocked": {"key_old": {"blocked_at": "2025-01-01T00:00:00Z"}},
        }
        blocked_file.write_text(json.dumps(legacy_data), "utf-8")

        from core_runtime.capability_installer import CapabilityInstaller
        with mock.patch.dict(os.environ, {"RUMI_REQUIRE_HMAC": "0"}):
            inst = CapabilityInstaller(
                requests_dir=str(req_dir),
                handlers_dest_dir=str(tmp_dir / "handlers"),
            )
            assert "key_old" in inst.list_blocked()


# ---------------------------------------------------------------------------
# 14. RUMI_REQUIRE_HMAC=1 rejects unsigned files
# ---------------------------------------------------------------------------

class TestRequireHMAC:
    def test_trust_store_rejects_unsigned_when_required(self, tmp_dir):
        """TrustStore rejects unsigned file when RUMI_REQUIRE_HMAC=1."""
        from core_runtime.capability_trust_store import CapabilityTrustStore

        trust_file = tmp_dir / "trusted_handlers.json"
        legacy_data = {
            "version": "1.0",
            "trusted_at": "2025-01-01T00:00:00Z",
            "trusted": [
                {"handler_id": "h_req", "sha256": "e" * 64, "note": ""}
            ],
        }
        trust_file.write_text(json.dumps(legacy_data), "utf-8")

        with mock.patch.dict(os.environ, {"RUMI_REQUIRE_HMAC": "1"}):
            store = CapabilityTrustStore(trust_dir=str(tmp_dir))
            assert store.load() is False
            assert store.list_trusted() == []


# ---------------------------------------------------------------------------
# 15-16. rollback_to_version hash verification
# ---------------------------------------------------------------------------

class TestRollbackHashVerification:
    def _make_approval_manager(self, tmp_dir):
        from core_runtime.approval_manager import ApprovalManager
        packs_dir = tmp_dir / "ecosystem"
        grants_dir = tmp_dir / "grants"
        packs_dir.mkdir(parents=True)
        grants_dir.mkdir(parents=True)
        am = ApprovalManager(
            packs_dir=str(packs_dir),
            grants_dir=str(grants_dir),
            secret_key="test_secret_key_for_hmac_32chars!",
        )
        return am, packs_dir, grants_dir

    def test_rollback_rejects_hash_mismatch(self, tmp_dir):
        """rollback_to_version fails when current files don't match target."""
        from core_runtime.approval_manager import PackApproval, PackStatus

        am, packs_dir, _ = self._make_approval_manager(tmp_dir)
        pack_dir = packs_dir / "test_pack"
        pack_dir.mkdir()
        (pack_dir / "file.py").write_text("original content", "utf-8")

        approval = PackApproval(
            pack_id="test_pack",
            status=PackStatus.APPROVED,
            created_at="2025-01-01T00:00:00Z",
            approved_at="2025-01-01T00:00:00Z",
            file_hashes={"file.py": "sha256:abc123"},
            version_history=[{
                "version": 1,
                "timestamp": "2025-01-01T00:00:00Z",
                "action": "approve",
                "file_hashes": {"file.py": "sha256:different_hash"},
            }],
        )
        am._approvals["test_pack"] = approval
        am._pack_locations["test_pack"] = mock.MagicMock(pack_dir=pack_dir)

        result = am.rollback_to_version("test_pack", 0)
        assert result.success is False
        assert "do not match" in result.error

    def test_rollback_accepts_hash_match(self, tmp_dir):
        """rollback_to_version succeeds when current files match target."""
        from core_runtime.approval_manager import PackApproval, PackStatus

        am, packs_dir, _ = self._make_approval_manager(tmp_dir)
        pack_dir = packs_dir / "test_pack2"
        pack_dir.mkdir()
        (pack_dir / "file.py").write_text("matching content", "utf-8")
        real_hash = am._compute_file_hash(pack_dir / "file.py")

        approval = PackApproval(
            pack_id="test_pack2",
            status=PackStatus.MODIFIED,
            created_at="2025-01-01T00:00:00Z",
            file_hashes={},
            version_history=[{
                "version": 1,
                "timestamp": "2025-01-01T00:00:00Z",
                "action": "approve",
                "file_hashes": {"file.py": real_hash},
            }],
        )
        am._approvals["test_pack2"] = approval
        am._pack_locations["test_pack2"] = mock.MagicMock(pack_dir=pack_dir)

        result = am.rollback_to_version("test_pack2", 0)
        assert result.success is True
        assert am._approvals["test_pack2"].status == PackStatus.APPROVED


# ---------------------------------------------------------------------------
# 17. verify_hash use_cache=False
# ---------------------------------------------------------------------------

class TestVerifyHashNoCache:
    def test_verify_hash_no_cache_bypasses_cache(self, tmp_dir):
        """verify_hash(use_cache=False) detects file changes ignored by cache."""
        from core_runtime.approval_manager import ApprovalManager, PackApproval, PackStatus

        packs_dir = tmp_dir / "ecosystem"
        grants_dir = tmp_dir / "grants"
        packs_dir.mkdir(parents=True)
        grants_dir.mkdir(parents=True)

        am = ApprovalManager(
            packs_dir=str(packs_dir),
            grants_dir=str(grants_dir),
            secret_key="test_secret_key_for_hmac_32chars!",
        )

        pack_dir = packs_dir / "cache_test_pack"
        pack_dir.mkdir()
        (pack_dir / "mod.py").write_text("v1", "utf-8")
        real_hash = am._compute_file_hash(pack_dir / "mod.py")

        approval = PackApproval(
            pack_id="cache_test_pack",
            status=PackStatus.APPROVED,
            created_at="2025-01-01T00:00:00Z",
            file_hashes={"mod.py": real_hash},
        )
        am._approvals["cache_test_pack"] = approval
        am._pack_locations["cache_test_pack"] = mock.MagicMock(pack_dir=pack_dir)

        # Warm cache
        assert am.verify_hash("cache_test_pack", use_cache=True) is True

        # Modify file — cache still holds old hash
        (pack_dir / "mod.py").write_text("v2_MODIFIED", "utf-8")

        # With cache: stale cache → still passes
        assert am.verify_hash("cache_test_pack", use_cache=True) is True

        # Without cache: fresh read → detects mismatch
        assert am.verify_hash("cache_test_pack", use_cache=False) is False


# ---------------------------------------------------------------------------
# 18-19. principal_id spoofing detection
# ---------------------------------------------------------------------------

class TestPrincipalIdValidation:
    def test_principal_id_spoofing_detected(self, tmp_dir):
        """Modifier with mismatched principal_id gets overridden."""
        from core_runtime.flow_modifier_loader import FlowModifierLoader

        loader = FlowModifierLoader()
        mod_file = tmp_dir / "test.modifier.yaml"

        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML not installed")

        mod_content = {
            "modifier_id": "spoofed_mod",
            "target_flow_id": "some_flow",
            "phase": "execution",
            "action": "append",
            "step": {
                "id": "spoofed_step",
                "type": "tool_call",
                "principal_id": "evil_pack",
            },
            "priority": 100,
        }
        mod_file.write_text(yaml.dump(mod_content), "utf-8")

        result = loader.load_modifier_file(mod_file, pack_id="legit_pack")
        assert result.success is True
        assert result.modifier_def.step["principal_id"] == "legit_pack"

    def test_principal_id_none_pack_passes(self, tmp_dir):
        """Shared modifier (pack_id=None) keeps principal_id unchanged."""
        from core_runtime.flow_modifier_loader import FlowModifierLoader

        loader = FlowModifierLoader()
        mod_file = tmp_dir / "shared.modifier.yaml"

        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML not installed")

        mod_content = {
            "modifier_id": "shared_mod",
            "target_flow_id": "some_flow",
            "phase": "execution",
            "action": "append",
            "step": {
                "id": "shared_step",
                "type": "tool_call",
                "principal_id": "any_value",
            },
            "priority": 100,
        }
        mod_file.write_text(yaml.dump(mod_content), "utf-8")

        result = loader.load_modifier_file(mod_file, pack_id=None)
        assert result.success is True
        assert result.modifier_def.step["principal_id"] == "any_value"


# ---------------------------------------------------------------------------
# 20-22. PermissionManager mode
# ---------------------------------------------------------------------------

class TestPermissionManagerMode:
    def test_permissive_mode_warning(self, caplog):
        """The retired PermissionManager cannot select a permissive mode."""
        del caplog
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.permission_manager")

    def test_env_var_mode_selection(self):
        """Environment mode cannot replace the captured v4 security epoch."""
        from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot

        assert_profile_resolver_requires_authority_snapshot()

    def test_explicit_mode_overrides_env(self):
        """Caller-supplied mode cannot override Host-captured authority."""
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.permission_manager")
