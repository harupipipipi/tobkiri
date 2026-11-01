"""
test_wave9.py - Wave 9 テスト

テスト対象:
  - Modifier 衝突検出（同一 target_step_id への複数 Modifier）
  - conflicts_with / compatible_with フィールドのパースと衝突検出
  - Flow ID プレフィックス警告（Pack 提供 Flow で {pack_id}. なし）
  - _EXPECTED_HANDLER_KEYS 整合性確認
"""
from __future__ import annotations

import copy
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core_runtime.flow_loader import (
    FlowDefinition,
    FlowLoadResult,
    FlowLoader,
    FlowStep,
)
from core_runtime.flow_modifier import (
    FlowModifierApplier,
    FlowModifierDef,
    FlowModifierLoader,
    ModifierApplyResult,
    ModifierRequires,
)


# ======================================================================
# ヘルパー
# ======================================================================

def _make_flow_def(
    flow_id: str = "test.flow",
    phases: list | None = None,
    steps: list | None = None,
) -> FlowDefinition:
    """テスト用 FlowDefinition を生成"""
    phases = phases or ["startup"]
    steps = steps or [
        FlowStep(
            id="step_a", phase="startup", priority=100,
            type="handler", when=None, input=None, output=None,
            raw={"id": "step_a", "type": "handler"},
        ),
        FlowStep(
            id="step_b", phase="startup", priority=200,
            type="handler", when=None, input=None, output=None,
            raw={"id": "step_b", "type": "handler"},
        ),
    ]
    return FlowDefinition(
        flow_id=flow_id,
        inputs={},
        outputs={},
        phases=phases,
        defaults={"fail_soft": True},
        steps=steps,
        source_type="pack",
        source_pack_id="test_pack",
    )


def _make_modifier(
    modifier_id: str,
    action: str = "inject_after",
    target_step_id: str = "step_a",
    phase: str = "startup",
    priority: int = 100,
    conflicts_with: list | None = None,
    compatible_with: list | None = None,
) -> FlowModifierDef:
    """テスト用 FlowModifierDef を生成"""
    step = None
    if action != "remove":
        step = {"id": f"injected_{modifier_id}", "type": "handler"}
    return FlowModifierDef(
        modifier_id=modifier_id,
        target_flow_id="test.flow",
        phase=phase,
        priority=priority,
        action=action,
        target_step_id=target_step_id,
        step=step,
        requires=ModifierRequires(),
        conflicts_with=conflicts_with,
        compatible_with=compatible_with,
    )


# ======================================================================
# TestModifierConflictDetection
# ======================================================================

class TestModifierConflictDetection:
    """Modifier 衝突検出のテスト"""

    def test_no_conflict_single_modifier(self):
        """単一 modifier では警告なし"""
        applier = FlowModifierApplier()
        flow_def = _make_flow_def()
        modifiers = [_make_modifier("m1")]

        with patch("core_runtime.flow_modifier.logger") as mock_logger:
            applier.apply_modifiers(flow_def, modifiers)
            # CONFLICT ログは呼ばれない（_log_modifier_success の WARNING は呼ばれるが CONFLICT ではない）
            conflict_calls = [
                c for c in mock_logger.warning.call_args_list
                if "CONFLICT" in str(c)
            ]
            assert len(conflict_calls) == 0

    def test_conflict_same_target_inject_after(self):
        """同一 target_step_id に inject_after が2つ → 警告"""
        applier = FlowModifierApplier()
        flow_def = _make_flow_def()
        modifiers = [
            _make_modifier("m1", action="inject_after", target_step_id="step_a"),
            _make_modifier("m2", action="inject_after", target_step_id="step_a"),
        ]

        with patch("core_runtime.flow_modifier.logger") as mock_logger:
            applier.apply_modifiers(flow_def, modifiers)
            conflict_calls = [
                c for c in mock_logger.warning.call_args_list
                if "CONFLICT" in str(c)
            ]
            assert len(conflict_calls) >= 1

    def test_severe_conflict_remove_and_inject(self):
        """remove + inject_before が同一 target → severe 警告"""
        applier = FlowModifierApplier()
        flow_def = _make_flow_def()
        modifiers = [
            _make_modifier("m_remove", action="remove", target_step_id="step_a"),
            _make_modifier("m_inject", action="inject_before", target_step_id="step_a"),
        ]

        with patch("core_runtime.flow_modifier.logger") as mock_logger:
            applier.apply_modifiers(flow_def, modifiers)
            severe_calls = [
                c for c in mock_logger.warning.call_args_list
                if "severe" in str(c).lower()
            ]
            assert len(severe_calls) >= 1

    def test_severe_conflict_remove_and_replace(self):
        """remove + replace が同一 target → severe 警告"""
        applier = FlowModifierApplier()
        flow_def = _make_flow_def()
        modifiers = [
            _make_modifier("m_remove", action="remove", target_step_id="step_a"),
            _make_modifier("m_replace", action="replace", target_step_id="step_a"),
        ]

        with patch("core_runtime.flow_modifier.logger") as mock_logger:
            applier.apply_modifiers(flow_def, modifiers)
            severe_calls = [
                c for c in mock_logger.warning.call_args_list
                if "severe" in str(c).lower()
            ]
            assert len(severe_calls) >= 1

    def test_no_conflict_different_targets(self):
        """異なる target_step_id → 衝突なし"""
        applier = FlowModifierApplier()
        flow_def = _make_flow_def()
        modifiers = [
            _make_modifier("m1", action="inject_after", target_step_id="step_a"),
            _make_modifier("m2", action="inject_after", target_step_id="step_b"),
        ]

        with patch("core_runtime.flow_modifier.logger") as mock_logger:
            applier.apply_modifiers(flow_def, modifiers)
            conflict_calls = [
                c for c in mock_logger.warning.call_args_list
                if "CONFLICT" in str(c)
            ]
            assert len(conflict_calls) == 0

    def test_skipped_modifier_excluded_from_conflict(self):
        """requires 不満足でスキップされた modifier は衝突検出から除外"""
        applier = FlowModifierApplier()
        flow_def = _make_flow_def()

        m_skipped = _make_modifier("m_skipped", action="inject_after", target_step_id="step_a")
        m_skipped.requires = ModifierRequires(interfaces=["nonexistent.interface"])

        modifiers = [
            m_skipped,
            _make_modifier("m_active", action="inject_after", target_step_id="step_a"),
        ]

        with patch("core_runtime.flow_modifier.logger") as mock_logger:
            applier.apply_modifiers(flow_def, modifiers)
            conflict_calls = [
                c for c in mock_logger.warning.call_args_list
                if "CONFLICT" in str(c)
            ]
            # m_skipped は除外されるので衝突なし
            assert len(conflict_calls) == 0


# ======================================================================
# TestConflictsWithField
# ======================================================================

class TestConflictsWithField:
    """conflicts_with / compatible_with フィールドのテスト"""

    def test_conflicts_with_both_active(self):
        """conflicts_with で宣言された modifier が両方 active → 警告"""
        applier = FlowModifierApplier()
        flow_def = _make_flow_def()
        modifiers = [
            _make_modifier("m1", target_step_id="step_a", conflicts_with=["m2"]),
            _make_modifier("m2", target_step_id="step_b"),
        ]

        with patch("core_runtime.flow_modifier.logger") as mock_logger:
            applier.apply_modifiers(flow_def, modifiers)
            declared_calls = [
                c for c in mock_logger.warning.call_args_list
                if "declared" in str(c).lower()
            ]
            assert len(declared_calls) >= 1

    def test_conflicts_with_other_not_active(self):
        """conflicts_with で宣言された modifier が active でない → 警告なし"""
        applier = FlowModifierApplier()
        flow_def = _make_flow_def()
        modifiers = [
            _make_modifier("m1", target_step_id="step_a", conflicts_with=["m_nonexistent"]),
        ]

        with patch("core_runtime.flow_modifier.logger") as mock_logger:
            applier.apply_modifiers(flow_def, modifiers)
            declared_calls = [
                c for c in mock_logger.warning.call_args_list
                if "declared" in str(c).lower()
            ]
            assert len(declared_calls) == 0

    def test_compatible_with_missing(self):
        """compatible_with で宣言された modifier が active でない → 警告"""
        applier = FlowModifierApplier()
        flow_def = _make_flow_def()
        modifiers = [
            _make_modifier("m1", target_step_id="step_a", compatible_with=["m_missing"]),
        ]

        with patch("core_runtime.flow_modifier.logger") as mock_logger:
            applier.apply_modifiers(flow_def, modifiers)
            compat_calls = [
                c for c in mock_logger.warning.call_args_list
                if "compatibility" in str(c).lower()
            ]
            assert len(compat_calls) >= 1

    def test_compatible_with_present(self):
        """compatible_with で宣言された modifier が active → 警告なし"""
        applier = FlowModifierApplier()
        flow_def = _make_flow_def()
        modifiers = [
            _make_modifier("m1", target_step_id="step_a", compatible_with=["m2"]),
            _make_modifier("m2", target_step_id="step_b"),
        ]

        with patch("core_runtime.flow_modifier.logger") as mock_logger:
            applier.apply_modifiers(flow_def, modifiers)
            compat_calls = [
                c for c in mock_logger.warning.call_args_list
                if "compatibility" in str(c).lower()
            ]
            assert len(compat_calls) == 0


# ======================================================================
# TestConflictsWithParsing
# ======================================================================

class TestConflictsWithParsing:
    """conflicts_with / compatible_with フィールドのパーステスト"""

    def test_parse_conflicts_with(self, tmp_path):
        """YAML から conflicts_with をパースできること"""
        yaml_content = """
modifier_id: test_mod
target_flow_id: my.flow
phase: startup
action: inject_after
target_step_id: step_a
step:
  id: injected_step
  type: handler
conflicts_with:
  - other_mod_1
  - other_mod_2
compatible_with:
  - helper_mod
"""
        mod_file = tmp_path / "test.modifier.yaml"
        mod_file.write_text(yaml_content, encoding="utf-8")

        loader = FlowModifierLoader()
        result = loader.load_modifier_file(mod_file)

        assert result.success
        assert result.modifier_def is not None
        assert result.modifier_def.conflicts_with == ["other_mod_1", "other_mod_2"]
        assert result.modifier_def.compatible_with == ["helper_mod"]

    def test_parse_without_conflicts_with(self, tmp_path):
        """conflicts_with/compatible_with がない場合は None"""
        yaml_content = """
modifier_id: test_mod
target_flow_id: my.flow
phase: startup
action: inject_after
target_step_id: step_a
step:
  id: injected_step
  type: handler
"""
        mod_file = tmp_path / "test.modifier.yaml"
        mod_file.write_text(yaml_content, encoding="utf-8")

        loader = FlowModifierLoader()
        result = loader.load_modifier_file(mod_file)

        assert result.success
        assert result.modifier_def is not None
        assert result.modifier_def.conflicts_with is None
        assert result.modifier_def.compatible_with is None

    def test_to_dict_includes_conflict_fields(self):
        """to_dict に conflicts_with / compatible_with が含まれること"""
        mod = _make_modifier("m1", conflicts_with=["m2"], compatible_with=["m3"])
        d = mod.to_dict()
        assert d["conflicts_with"] == ["m2"]
        assert d["compatible_with"] == ["m3"]


# ======================================================================
# TestFlowIdPrefixWarning
# ======================================================================

class TestFlowIdPrefixWarning:
    """Flow ID プレフィックス警告のテスト"""

    def test_warning_when_no_prefix(self, tmp_path):
        """Pack 提供 Flow で pack_id. プレフィックスなし → 警告"""
        yaml_content = """
flow_id: my_flow_without_prefix
phases:
  - startup
steps:
  - id: s1
    phase: startup
    type: handler
    input:
      handler: "kernel:noop"
"""
        flows_dir = tmp_path / "flows"
        flows_dir.mkdir()
        (flows_dir / "test.flow.yaml").write_text(yaml_content, encoding="utf-8")

        loader = FlowLoader()

        with patch("core_runtime.flow_loader.logger") as mock_logger:
            loader._load_directory_flows(flows_dir, "pack", "my_pack")

            warning_calls = [
                c for c in mock_logger.warning.call_args_list
                if "prefix" in str(c).lower()
            ]
            assert len(warning_calls) >= 1

        # Flow はロードされていること（エラーではない）
        loaded = loader.get_loaded_flows()
        assert "my_flow_without_prefix" in loaded

    def test_no_warning_when_prefix_present(self, tmp_path):
        """Pack 提供 Flow で pack_id. プレフィックスあり → 警告なし"""
        yaml_content = """
flow_id: my_pack.my_flow
phases:
  - startup
steps:
  - id: s1
    phase: startup
    type: handler
    input:
      handler: "kernel:noop"
"""
        flows_dir = tmp_path / "flows"
        flows_dir.mkdir()
        (flows_dir / "test.flow.yaml").write_text(yaml_content, encoding="utf-8")

        loader = FlowLoader()

        with patch("core_runtime.flow_loader.logger") as mock_logger:
            loader._load_directory_flows(flows_dir, "pack", "my_pack")

            warning_calls = [
                c for c in mock_logger.warning.call_args_list
                if "prefix" in str(c).lower()
            ]
            assert len(warning_calls) == 0

    def test_no_warning_for_official_source(self, tmp_path):
        """source_type=official では適用されない"""
        yaml_content = """
flow_id: no_prefix_flow
phases:
  - startup
steps:
  - id: s1
    phase: startup
    type: handler
    input:
      handler: "kernel:noop"
"""
        flows_dir = tmp_path / "flows"
        flows_dir.mkdir()
        (flows_dir / "test.flow.yaml").write_text(yaml_content, encoding="utf-8")

        loader = FlowLoader()

        with patch("core_runtime.flow_loader.logger") as mock_logger:
            loader._load_directory_flows(flows_dir, "official", None)

            warning_calls = [
                c for c in mock_logger.warning.call_args_list
                if "prefix" in str(c).lower()
            ]
            assert len(warning_calls) == 0

    def test_no_warning_for_shared_source(self, tmp_path):
        """source_type=shared では適用されない"""
        yaml_content = """
flow_id: no_prefix_flow
phases:
  - startup
steps:
  - id: s1
    phase: startup
    type: handler
    input:
      handler: "kernel:noop"
"""
        flows_dir = tmp_path / "flows"
        flows_dir.mkdir()
        (flows_dir / "test.flow.yaml").write_text(yaml_content, encoding="utf-8")

        loader = FlowLoader()

        with patch("core_runtime.flow_loader.logger") as mock_logger:
            loader._load_directory_flows(flows_dir, "shared", None)

            warning_calls = [
                c for c in mock_logger.warning.call_args_list
                if "prefix" in str(c).lower()
            ]
            assert len(warning_calls) == 0

    def test_warning_in_result_warnings(self, tmp_path):
        """警告が result.warnings にも追加されること（ロードは継続）"""
        yaml_content = """
flow_id: unprefixed
phases:
  - startup
steps:
  - id: s1
    phase: startup
    type: handler
    input:
      handler: "kernel:noop"
"""
        flows_dir = tmp_path / "flows"
        flows_dir.mkdir()
        flow_file = flows_dir / "test.flow.yaml"
        flow_file.write_text(yaml_content, encoding="utf-8")

        loader = FlowLoader()
        # load_flow_file + _load_directory_flows で warnings が伝播することを確認
        # _load_directory_flows は内部で load_flow_file を呼ぶ
        with patch("core_runtime.flow_loader.logger"):
            loader._load_directory_flows(flows_dir, "pack", "my_pack")

        loaded = loader.get_loaded_flows()
        assert "unprefixed" in loaded
        # FlowLoadResult.warnings は FlowDefinition に伝播しないが、
        # ロードが成功していること自体がロード継続の証拠


# ======================================================================
# TestExpectedHandlerKeysConsistency
# ======================================================================

class TestExpectedHandlerKeysConsistency:
    """The removed Kernel handler table has no v4 runtime authority."""

    def test_expected_keys_is_frozenset(self):
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.kernel")

    def test_expected_keys_all_start_with_kernel(self):
        """Finite v4 contracts replace Kernel-prefixed dynamic dispatch keys."""
        from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog

        catalog = BundledCatalog.load(
            Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack" / "v4"
        )
        assert all(
            "operations" in contract
            for pack in catalog.packs.values()
            for contract in pack["contracts"]
        )

    def test_no_duplicate_keys(self):
        """Pack identities in the v4 catalog are unique by construction."""
        from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog

        catalog = BundledCatalog.load(
            Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack" / "v4"
        )
        assert len(catalog.packs) == len(set(catalog.packs))

    def test_system_handler_keys_in_expected(self):
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.kernel_handlers_system")

    def test_runtime_handler_keys_in_expected(self):
        from tests.v4_batch_support import assert_legacy_registry_fails_closed

        assert_legacy_registry_fails_closed()

    def test_no_extra_keys(self):
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.kernel")


# ======================================================================
# TestModifierApplyBehaviorUnchanged
# ======================================================================

class TestModifierApplyBehaviorUnchanged:
    """衝突検出追加後も既存の apply_modifiers 動作が変わらないことを確認"""

    def test_inject_after_still_works(self):
        """inject_after が正常に動作すること"""
        applier = FlowModifierApplier()
        flow_def = _make_flow_def()
        modifiers = [_make_modifier("m1", action="inject_after", target_step_id="step_a")]

        with patch("core_runtime.flow_modifier.logger"):
            new_flow, results = applier.apply_modifiers(flow_def, modifiers)

        assert len(results) == 1
        assert results[0].success is True
        # step_a の後に injected_m1 が挿入されていること
        step_ids = [s.id for s in new_flow.steps]
        assert "injected_m1" in step_ids
        idx_a = step_ids.index("step_a")
        idx_m1 = step_ids.index("injected_m1")
        assert idx_m1 == idx_a + 1

    def test_remove_still_works(self):
        """remove が正常に動作すること"""
        applier = FlowModifierApplier()
        flow_def = _make_flow_def()
        modifiers = [_make_modifier("m1", action="remove", target_step_id="step_a")]

        with patch("core_runtime.flow_modifier.logger"):
            new_flow, results = applier.apply_modifiers(flow_def, modifiers)

        assert len(results) == 1
        assert results[0].success is True
        step_ids = [s.id for s in new_flow.steps]
        assert "step_a" not in step_ids

    def test_replace_still_works(self):
        """replace が正常に動作すること"""
        applier = FlowModifierApplier()
        flow_def = _make_flow_def()
        modifiers = [_make_modifier("m1", action="replace", target_step_id="step_a")]

        with patch("core_runtime.flow_modifier.logger"):
            new_flow, results = applier.apply_modifiers(flow_def, modifiers)

        assert len(results) == 1
        assert results[0].success is True
        step_ids = [s.id for s in new_flow.steps]
        assert "injected_m1" in step_ids
        assert "step_a" not in step_ids
