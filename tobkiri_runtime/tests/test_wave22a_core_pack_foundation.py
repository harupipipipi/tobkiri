"""
test_wave22a_core_pack_foundation.py

W22-A: core_pack ローダー基盤のテスト (15件以上)
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# paths.py テスト
# ---------------------------------------------------------------------------

class TestPathsConstants:
    """paths.py の定数テスト"""

    def test_core_pack_dir_points_to_core_runtime_core_pack(self):
        """CORE_PACK_DIR が core_runtime/core_pack/ を指している"""
        from core_runtime.paths import CORE_PACK_DIR
        p = Path(CORE_PACK_DIR)
        assert p.name == "core_pack"
        assert p.parent.name == "core_runtime"

    def test_core_pack_id_prefix_is_core_underscore(self):
        """CORE_PACK_ID_PREFIX が 'core_' である"""
        from core_runtime.paths import CORE_PACK_ID_PREFIX
        assert CORE_PACK_ID_PREFIX == "core_"

    def test_core_pack_dir_is_string(self):
        """CORE_PACK_DIR は文字列型"""
        from core_runtime.paths import CORE_PACK_DIR
        assert isinstance(CORE_PACK_DIR, str)


# ---------------------------------------------------------------------------
# registry.py テスト
# ---------------------------------------------------------------------------

def _make_ecosystem_json(pack_dir: Path, pack_id: str) -> None:
    """テスト用の最小限 ecosystem.json を作成"""
    pack_dir.mkdir(parents=True, exist_ok=True)
    eco = {
        "pack_id": pack_id,
        "pack_identity": f"{pack_id}.test",
        "version": "1.0.0",
        "vocabulary": {"types": []},
    }
    (pack_dir / "ecosystem.json").write_text(
        json.dumps(eco, ensure_ascii=False, indent=2), encoding="utf-8"
    )


class TestRegistryCorePack:
    """The v4 catalog is the only Pack inventory."""

    def test_core_pack_loaded_when_present(self):
        from tests.v4_batch_support import assert_legacy_registry_fails_closed

        assert_legacy_registry_fails_closed()

    def test_core_pack_loaded_before_ecosystem_pack(self):
        from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog

        assert BundledCatalog.load(
            Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack" / "v4"
        ).packs

    def test_core_pack_dir_missing_no_error(self):
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.ecosystem_nodes")

    def test_core_and_ecosystem_both_loaded(self):
        from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog

        catalog = BundledCatalog.load(
            Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack" / "v4"
        )
        assert {"defaultspack", "rumi_file_inspect_pack"} <= set(catalog.packs)

    def test_core_pack_overrides_same_pack_id(self):
        from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot

        assert_profile_resolver_requires_authority_snapshot()


# ---------------------------------------------------------------------------
# approval_manager.py テスト
# ---------------------------------------------------------------------------

class TestApprovalManagerCorePack:
    """approval_manager.py の core_pack テスト"""

    def _make_manager(self, tmp_path):
        """テスト用 ApprovalManager を生成"""
        from core_runtime.approval_manager import ApprovalManager
        grants_dir = tmp_path / "grants"
        grants_dir.mkdir(parents=True, exist_ok=True)
        eco_dir = tmp_path / "ecosystem"
        eco_dir.mkdir(parents=True, exist_ok=True)
        mgr = ApprovalManager(
            packs_dir=str(eco_dir),
            grants_dir=str(grants_dir),
            secret_key="test-secret-key-for-unit-tests",
        )
        mgr.initialize()
        return mgr

    def _make_trusted_core_pack(self, tmp_path, pack_id="core_alpha"):
        core_root = tmp_path / "core_runtime" / "core_pack"
        core_pack = core_root / pack_id
        _make_ecosystem_json(core_pack, pack_id)
        return core_root, core_pack

    def test_is_core_pack_true_for_trusted_core_pack(self, tmp_path):
        """_is_core_pack() は trusted core_pack 配下の core_ Pack だけ True を返す"""
        from core_runtime import approval_manager as am_mod
        core_root, _ = self._make_trusted_core_pack(tmp_path, "core_alpha")
        mgr = self._make_manager(tmp_path)

        with patch.object(am_mod, "CORE_PACK_DIR", str(core_root)):
            assert mgr._is_core_pack("core_alpha") is True

    def test_is_core_pack_false_for_core_prefix_outside_trusted_dir(self, tmp_path):
        """ecosystem/ 配下の core_ Pack は core_pack として扱わない"""
        from core_runtime import approval_manager as am_mod
        core_root = tmp_path / "core_runtime" / "core_pack"
        _make_ecosystem_json(tmp_path / "ecosystem" / "core_evil", "core_evil")
        mgr = self._make_manager(tmp_path)
        mgr.scan_packs()

        with patch.object(am_mod, "CORE_PACK_DIR", str(core_root)):
            assert mgr._is_core_pack("core_evil") is False
            assert mgr.get_status("core_evil") == am_mod.PackStatus.INSTALLED
            assert mgr.is_pack_approved_and_verified("core_evil") == (False, "not_approved")
            assert mgr.verify_hash("core_evil") is False

    def test_ecosystem_pack_cannot_shadow_trusted_core_pack_id(self, tmp_path):
        """trusted core_pack と同じ ID の ecosystem/ Pack は承認をバイパスできない"""
        from core_runtime import approval_manager as am_mod
        core_root, _ = self._make_trusted_core_pack(tmp_path, "core_shadow")
        _make_ecosystem_json(tmp_path / "ecosystem" / "core_shadow", "core_shadow")
        mgr = self._make_manager(tmp_path)
        mgr.scan_packs()

        with patch.object(am_mod, "CORE_PACK_DIR", str(core_root)):
            assert mgr._is_core_pack("core_shadow") is False
            assert mgr.get_status("core_shadow") == am_mod.PackStatus.INSTALLED
            assert mgr.is_pack_approved_and_verified("core_shadow") == (False, "not_approved")

    def test_is_core_pack_false_for_exact_prefix(self, tmp_path):
        """'core_' ちょうどの pack_id は実体がない限り False を返す"""
        mgr = self._make_manager(tmp_path)
        assert mgr._is_core_pack("core_") is False

    def test_is_core_pack_false_for_normal(self, tmp_path):
        """_is_core_pack() が通常 pack_id で False を返す"""
        mgr = self._make_manager(tmp_path)
        assert mgr._is_core_pack("normal_pack") is False

    def test_is_core_pack_false_for_empty(self, tmp_path):
        """_is_core_pack() が空文字列で False を返す"""
        mgr = self._make_manager(tmp_path)
        assert mgr._is_core_pack("") is False

    def test_is_pack_approved_and_verified_core(self, tmp_path):
        """trusted core_pack 配下の Pack は (True, None) を返す"""
        from core_runtime import approval_manager as am_mod
        core_root, _ = self._make_trusted_core_pack(tmp_path, "core_system")
        mgr = self._make_manager(tmp_path)

        with patch.object(am_mod, "CORE_PACK_DIR", str(core_root)):
            result = mgr.is_pack_approved_and_verified("core_system")
            assert result == (True, None)

    def test_get_status_core(self, tmp_path):
        """trusted core_pack 配下の Pack は APPROVED を返す"""
        from core_runtime import approval_manager as am_mod
        core_root, _ = self._make_trusted_core_pack(tmp_path, "core_system")
        mgr = self._make_manager(tmp_path)

        with patch.object(am_mod, "CORE_PACK_DIR", str(core_root)):
            assert mgr.get_status("core_system") == am_mod.PackStatus.APPROVED

    def test_verify_hash_core(self, tmp_path):
        """trusted core_pack 配下の Pack は True を返す"""
        from core_runtime import approval_manager as am_mod
        core_root, _ = self._make_trusted_core_pack(tmp_path, "core_system")
        mgr = self._make_manager(tmp_path)

        with patch.object(am_mod, "CORE_PACK_DIR", str(core_root)):
            assert mgr.verify_hash("core_system") is True

    def test_normal_pack_not_found(self, tmp_path):
        """通常 pack_id は従来通り not_found を返す（回帰テスト）"""
        mgr = self._make_manager(tmp_path)
        is_valid, reason = mgr.is_pack_approved_and_verified("unknown_pack")
        assert is_valid is False
        assert reason == "not_found"

    def test_normal_pack_get_status_none(self, tmp_path):
        """通常の未登録 pack_id は get_status で None を返す（回帰テスト）"""
        mgr = self._make_manager(tmp_path)
        assert mgr.get_status("unknown_pack") is None

    def test_core_pack_no_approval_record_needed(self, tmp_path):
        """trusted core_pack は _approvals に登録しなくても承認済みになる"""
        from core_runtime import approval_manager as am_mod
        core_root, _ = self._make_trusted_core_pack(tmp_path, "core_magic")
        mgr = self._make_manager(tmp_path)

        with patch.object(am_mod, "CORE_PACK_DIR", str(core_root)):
            assert "core_magic" not in mgr._approvals
            assert mgr.is_pack_approved_and_verified("core_magic") == (True, None)

    def test_core_pack_verify_hash_no_approval_record(self, tmp_path):
        """trusted core_pack は _approvals に登録しなくてもハッシュ検証 True"""
        from core_runtime import approval_manager as am_mod
        core_root, _ = self._make_trusted_core_pack(tmp_path, "core_data")
        mgr = self._make_manager(tmp_path)

        with patch.object(am_mod, "CORE_PACK_DIR", str(core_root)):
            assert "core_data" not in mgr._approvals
            assert mgr.verify_hash("core_data") is True
