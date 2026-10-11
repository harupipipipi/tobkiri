"""
test_wave11_wildcard.py - Wave 11: Wildcard modifier approval control tests

Tests for target_flow_id='*' modifier authorization logic.
"""
from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from core_runtime.flow_modifier import FlowModifierLoader


def _write_modifier_yaml(directory: Path, modifier_id: str, target_flow_id: str = "*") -> Path:
    """Helper: write a minimal .modifier.yaml file."""
    content = textwrap.dedent(f"""\
        modifier_id: "{modifier_id}"
        target_flow_id: "{target_flow_id}"
        phase: "main"
        priority: 100
        action: "append"
        step:
          id: "injected_step"
          type: "noop"
    """)
    file_path = directory / f"{modifier_id}.modifier.yaml"
    file_path.write_text(content, encoding="utf-8")
    return file_path


def _write_ecosystem_json(directory: Path, allow_wildcard: bool = False) -> Path:
    """Helper: write a minimal ecosystem.json."""
    data = {"pack_id": directory.name}
    if allow_wildcard:
        data["allow_wildcard_modifiers"] = True
    eco_path = directory / "ecosystem.json"
    eco_path.write_text(json.dumps(data), encoding="utf-8")
    return eco_path


class TestWildcardModifierSkip:
    """Wildcard Modifier is skipped without explicit approval."""

    def test_wildcard_skipped_without_approval(self, tmp_path):
        """No env var + no ecosystem.json flag -> skip."""
        mod_dir = tmp_path / "modifiers"
        mod_dir.mkdir()
        _write_modifier_yaml(mod_dir, "wc_mod_1", target_flow_id="*")

        loader = FlowModifierLoader()
        loader._load_directory_modifiers(mod_dir, "test_pack")

        assert "wc_mod_1" not in loader._loaded_modifiers
        assert len(loader._skipped_modifiers) == 1
        assert loader._skipped_modifiers[0].reason == "wildcard_modifier_not_allowed"

    def test_wildcard_allowed_by_env_var(self, tmp_path, monkeypatch):
        """Env var RUMI_ALLOW_WILDCARD_MODIFIERS=true -> load."""
        monkeypatch.setenv("RUMI_ALLOW_WILDCARD_MODIFIERS", "true")

        mod_dir = tmp_path / "modifiers"
        mod_dir.mkdir()
        _write_modifier_yaml(mod_dir, "wc_mod_2", target_flow_id="*")

        loader = FlowModifierLoader()
        loader._load_directory_modifiers(mod_dir, "test_pack")

        assert "wc_mod_2" in loader._loaded_modifiers
        assert len(loader._skipped_modifiers) == 0

    def test_wildcard_allowed_by_ecosystem_json(self, tmp_path):
        """ecosystem.json allow_wildcard_modifiers: true -> load."""
        pack_dir = tmp_path / "ecosystem" / "test_pack"
        pack_dir.mkdir(parents=True)
        _write_ecosystem_json(pack_dir, allow_wildcard=True)

        mod_dir = tmp_path / "modifiers"
        mod_dir.mkdir()
        _write_modifier_yaml(mod_dir, "wc_mod_3", target_flow_id="*")

        loader = FlowModifierLoader()
        # Simulate pre-cache (as _load_pack_modifiers_via_discovery would do)
        loader._wildcard_flags["test_pack"] = True
        loader._load_directory_modifiers(mod_dir, "test_pack")

        assert "wc_mod_3" in loader._loaded_modifiers
        assert len(loader._skipped_modifiers) == 0

    def test_wildcard_always_allowed_for_shared(self, tmp_path):
        """shared modifier (pack_id=None) -> always load."""
        mod_dir = tmp_path / "modifiers"
        mod_dir.mkdir()
        _write_modifier_yaml(mod_dir, "wc_mod_4", target_flow_id="*")

        loader = FlowModifierLoader()
        loader._load_directory_modifiers(mod_dir, None)

        assert "wc_mod_4" in loader._loaded_modifiers
        assert len(loader._skipped_modifiers) == 0

    def test_non_wildcard_unaffected(self, tmp_path):
        """Non-wildcard modifier -> no impact from wildcard control."""
        mod_dir = tmp_path / "modifiers"
        mod_dir.mkdir()
        _write_modifier_yaml(mod_dir, "normal_mod", target_flow_id="specific_flow")

        loader = FlowModifierLoader()
        loader._load_directory_modifiers(mod_dir, "test_pack")

        assert "normal_mod" in loader._loaded_modifiers
        assert len(loader._skipped_modifiers) == 0


class TestIsWildcardModifierAllowed:
    """Unit tests for _is_wildcard_modifier_allowed."""

    def test_shared_always_allowed(self):
        loader = FlowModifierLoader()
        assert loader._is_wildcard_modifier_allowed(None) is True

    def test_env_var_allows(self, monkeypatch):
        monkeypatch.setenv("RUMI_ALLOW_WILDCARD_MODIFIERS", "true")
        loader = FlowModifierLoader()
        assert loader._is_wildcard_modifier_allowed("any_pack") is True

    def test_env_var_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("RUMI_ALLOW_WILDCARD_MODIFIERS", "True")
        loader = FlowModifierLoader()
        assert loader._is_wildcard_modifier_allowed("any_pack") is True

    def test_env_var_false_denies(self, monkeypatch):
        monkeypatch.setenv("RUMI_ALLOW_WILDCARD_MODIFIERS", "false")
        loader = FlowModifierLoader()
        with patch("core_runtime.flow_modifier_loader.resolve_pack_locations", return_value=[]):
            assert loader._is_wildcard_modifier_allowed("some_pack") is False

    def test_cache_hit_true(self):
        loader = FlowModifierLoader()
        loader._wildcard_flags["cached_pack"] = True
        assert loader._is_wildcard_modifier_allowed("cached_pack") is True

    def test_cache_hit_false(self):
        loader = FlowModifierLoader()
        loader._wildcard_flags["denied_pack"] = False
        assert loader._is_wildcard_modifier_allowed("denied_pack") is False

    def test_ecosystem_json_not_found(self):
        """Pack not in discovery -> False."""
        loader = FlowModifierLoader()
        with patch("core_runtime.flow_modifier_loader.resolve_pack_locations", return_value=[]):
            result = loader._is_wildcard_modifier_allowed("missing_pack")
        assert result is False
        assert loader._wildcard_flags["missing_pack"] is False

    def test_ecosystem_json_broken(self, tmp_path):
        """Broken ecosystem.json -> False."""
        eco_path = tmp_path / "broken.json"
        eco_path.write_text("{invalid json", encoding="utf-8")

        result = FlowModifierLoader._read_wildcard_flag_from_ecosystem(eco_path)
        assert result is False

    def test_ecosystem_json_flag_true(self, tmp_path):
        """allow_wildcard_modifiers: true -> True."""
        eco_path = tmp_path / "ecosystem.json"
        eco_path.write_text(json.dumps({"allow_wildcard_modifiers": True}), encoding="utf-8")

        result = FlowModifierLoader._read_wildcard_flag_from_ecosystem(eco_path)
        assert result is True

    def test_ecosystem_json_flag_missing(self, tmp_path):
        """No allow_wildcard_modifiers key -> False."""
        eco_path = tmp_path / "ecosystem.json"
        eco_path.write_text(json.dumps({"pack_id": "test"}), encoding="utf-8")

        result = FlowModifierLoader._read_wildcard_flag_from_ecosystem(eco_path)
        assert result is False

    def test_ecosystem_json_flag_explicitly_false(self, tmp_path):
        """allow_wildcard_modifiers: false -> False."""
        eco_path = tmp_path / "ecosystem.json"
        eco_path.write_text(
            json.dumps({"allow_wildcard_modifiers": False}), encoding="utf-8"
        )

        result = FlowModifierLoader._read_wildcard_flag_from_ecosystem(eco_path)
        assert result is False

    def test_ecosystem_json_nonexistent(self, tmp_path):
        """File does not exist -> False."""
        eco_path = tmp_path / "nonexistent.json"

        result = FlowModifierLoader._read_wildcard_flag_from_ecosystem(eco_path)
        assert result is False
