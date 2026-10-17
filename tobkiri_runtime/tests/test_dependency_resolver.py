"""
test_dependency_resolver.py — Wave 31-2

dependency_resolver.py の改善版テスト（20 ケース）。

テスト構成:
  - extract_dependencies: 6 ケース
  - resolve_load_order: 10 ケース
  - validate_dependencies: 4 ケース
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the package root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core_runtime.dependency_resolver import (
    CircularDependencyError,
    MissingDependencyError,
    VersionMismatchError,
    extract_dependencies,
    extract_dependency_specs,
    resolve_load_order,
    validate_dependencies,
    version_satisfies,
)


# =========================================================================
# extract_dependencies tests (6 cases)
# =========================================================================

class TestExtractDependencies:
    """extract_dependencies のテスト"""

    def test_extract_depends_on_list(self):
        """depends_on がリスト (dict 要素) の場合"""
        pack = {
            "depends_on": [
                {"pack_id": "pack_a"},
                {"pack_id": "pack_b"},
            ]
        }
        result = extract_dependencies(pack)
        assert result == ["pack_a", "pack_b"]

    def test_extract_depends_on_dict(self):
        """depends_on が dict の場合（キーを pack_id として扱う）"""
        pack = {
            "depends_on": {
                "pack_x": {"version": ">=1.0"},
                "pack_y": {},
            }
        }
        result = extract_dependencies(pack)
        assert set(result) == {"pack_x", "pack_y"}

    def test_extract_dependencies_dict(self):
        """dependencies が dict の場合（キーを pack_id として扱う）"""
        pack = {
            "dependencies": {
                "pack_m": {},
                "pack_n": {"version": "^2.0"},
            }
        }
        result = extract_dependencies(pack)
        assert set(result) == {"pack_m", "pack_n"}

    def test_preserves_version_constraint_from_mapping_value(self):
        """legacy mapping values are normalized into one resolver spec."""
        specs = extract_dependency_specs(
            {"dependencies": {"pack_m": ">=1.0,<2.0"}}
        )
        assert specs == [{"pack_id": "pack_m", "version": ">=1.0,<2.0"}]

    def test_ignores_connectivity_contract_requirements(self):
        """connectivity.requires は Pack 依存ではなく契約として扱う。"""
        pack = {
            "connectivity": {
                "requires": ["rumi.action.browser.host.v1", "rumi.resource.workspace.v1"]
            }
        }
        result = extract_dependencies(pack)
        assert result == []

    def test_extract_all_pack_dependency_sources_combined(self):
        """明示的な Pack 依存を重複なく結合する。"""
        pack = {
            "depends_on": [{"pack_id": "pack_a"}, {"pack_id": "pack_b"}],
            "dependencies": {"pack_b": {}, "pack_c": {}},
            "connectivity": {"requires": ["rumi.action.example.v1"]},
        }
        result = extract_dependencies(pack)
        # 出現順保持、重複なし
        assert result == ["pack_a", "pack_b", "pack_c"]

    def test_extract_empty(self):
        """全てが空 / None の場合に空リストが返ること"""
        assert extract_dependencies({}) == []
        assert extract_dependencies({"depends_on": None}) == []
        assert extract_dependencies({"dependencies": None, "connectivity": None}) == []


# =========================================================================
# resolve_load_order tests (10 cases)
# =========================================================================

class TestResolveLoadOrder:
    """resolve_load_order のテスト"""

    def test_resolve_empty(self):
        """空の packs で空リストが返ること"""
        assert resolve_load_order({}) == []

    def test_resolve_no_dependencies(self):
        """依存なしの packs がアルファベット順で返ること"""
        packs = {
            "charlie": {},
            "alpha": {},
            "bravo": {},
        }
        result = resolve_load_order(packs)
        assert result == ["alpha", "bravo", "charlie"]

    def test_resolve_simple_chain(self):
        """A -> B -> C の単純チェーンが正しい順序で返ること"""
        packs = {
            "pack_a": {"depends_on": [{"pack_id": "pack_b"}]},
            "pack_b": {"depends_on": [{"pack_id": "pack_c"}]},
            "pack_c": {},
        }
        result = resolve_load_order(packs)
        assert result == ["pack_c", "pack_b", "pack_a"]

    def test_resolve_diamond(self):
        """ダイヤモンド依存（D -> B,C -> A）が正しく解決されること"""
        packs = {
            "a": {},
            "b": {"depends_on": [{"pack_id": "a"}]},
            "c": {"depends_on": [{"pack_id": "a"}]},
            "d": {"depends_on": [{"pack_id": "b"}, {"pack_id": "c"}]},
        }
        result = resolve_load_order(packs)
        assert result.index("a") < result.index("b")
        assert result.index("a") < result.index("c")
        assert result.index("b") < result.index("d")
        assert result.index("c") < result.index("d")

    def test_resolve_circular_strict(self):
        """循環依存で CircularDependencyError が発生すること"""
        packs = {
            "pack_a": {"depends_on": [{"pack_id": "pack_b"}]},
            "pack_b": {"depends_on": [{"pack_id": "pack_a"}]},
        }
        with pytest.raises(CircularDependencyError):
            resolve_load_order(packs)

    def test_resolve_circular_soft(self):
        """compatibility soft_circular flag cannot bypass fail-closed cycles."""
        packs = {
            "pack_a": {"depends_on": [{"pack_id": "pack_b"}]},
            "pack_b": {"depends_on": [{"pack_id": "pack_a"}]},
            "pack_c": {},
        }
        with pytest.raises(CircularDependencyError):
            resolve_load_order(packs, soft_circular=True)

    def test_resolve_missing_strict(self):
        """strict=True で missing 依存が MissingDependencyError を発生すること"""
        packs = {
            "pack_a": {"depends_on": [{"pack_id": "pack_missing"}]},
        }
        with pytest.raises(MissingDependencyError):
            resolve_load_order(packs, strict=True)

    def test_resolve_missing_lenient(self):
        """strict=False で missing 依存がスキップされること"""
        packs = {
            "pack_a": {"depends_on": [{"pack_id": "pack_missing"}]},
            "pack_b": {},
        }
        result = resolve_load_order(packs, strict=False)
        assert set(result) == {"pack_a", "pack_b"}

    def test_resolve_version_mismatch_strict(self):
        packs = {
            "pack_a": {"dependencies": {"pack_b": ">=2.0"}},
            "pack_b": {"version": "1.9.9"},
        }

        with pytest.raises(VersionMismatchError, match="pack_a.*pack_b.*>=2.0"):
            resolve_load_order(packs, strict=True)

    def test_resolve_invalid_version_constraint_strict(self):
        packs = {
            "pack_a": {"dependencies": {"pack_b": "^2.0"}},
            "pack_b": {"version": "2.0.0"},
        }

        with pytest.raises(VersionMismatchError):
            resolve_load_order(packs, strict=True)

    def test_resolve_version_mismatch_non_strict_preserves_ordering(self):
        packs = {
            "pack_a": {"dependencies": {"pack_b": ">=2.0"}},
            "pack_b": {"version": "1.9.9"},
        }

        assert resolve_load_order(packs) == ["pack_b", "pack_a"]

    def test_resolve_self_dependency(self):
        """自己依存が正しく処理されること（無視されてソート完了）"""
        packs = {
            "pack_a": {"depends_on": [{"pack_id": "pack_a"}]},
            "pack_b": {},
        }
        result = resolve_load_order(packs)
        assert set(result) == {"pack_a", "pack_b"}

    def test_resolve_stable_sort(self):
        """同じ依存レベルの packs がアルファベット順（heapq）で返ること"""
        packs = {
            "zebra": {"depends_on": [{"pack_id": "base"}]},
            "alpha": {"depends_on": [{"pack_id": "base"}]},
            "mango": {"depends_on": [{"pack_id": "base"}]},
            "base": {},
        }
        result = resolve_load_order(packs)
        assert result[0] == "base"
        # After base, the rest should be alphabetical
        assert result[1:] == ["alpha", "mango", "zebra"]


# =========================================================================
# validate_dependencies tests (4 cases)
# =========================================================================

class TestValidateDependencies:
    """validate_dependencies のテスト"""

    def test_validate_no_issues(self):
        """問題なしで空リストが返ること"""
        packs = {
            "pack_a": {"depends_on": [{"pack_id": "pack_b"}]},
            "pack_b": {},
        }
        issues = validate_dependencies(packs)
        assert issues == []

    def test_validate_missing(self):
        """missing 依存が検出されること"""
        packs = {
            "pack_a": {"depends_on": [{"pack_id": "pack_missing"}]},
        }
        issues = validate_dependencies(packs)
        missing_issues = [i for i in issues if i["type"] == "missing"]
        assert len(missing_issues) == 1
        assert missing_issues[0]["pack_id"] == "pack_a"
        assert missing_issues[0]["depends_on"] == "pack_missing"

    def test_validate_circular(self):
        """循環依存が検出されること"""
        packs = {
            "pack_a": {"depends_on": [{"pack_id": "pack_b"}]},
            "pack_b": {"depends_on": [{"pack_id": "pack_a"}]},
        }
        issues = validate_dependencies(packs)
        circular_issues = [i for i in issues if i["type"] == "circular"]
        assert len(circular_issues) == 1
        assert set(circular_issues[0]["packs"]) == {"pack_a", "pack_b"}

    def test_validate_self_dependency(self):
        """自己依存が検出されること"""
        packs = {
            "pack_a": {"depends_on": [{"pack_id": "pack_a"}]},
        }
        issues = validate_dependencies(packs)
        self_dep_issues = [i for i in issues if i["type"] == "self_dependency"]
        assert len(self_dep_issues) == 1
        assert self_dep_issues[0]["pack_id"] == "pack_a"


class TestVersionConstraints:
    """PEP 440 version handling must not be reimplemented locally."""

    def test_pep440_prerelease_and_normalization(self):
        assert version_satisfies("1.0", "==1.0.0")
        assert version_satisfies("2.0rc1", ">=2.0rc1")

    def test_invalid_version_or_specifier_fails_closed(self):
        assert not version_satisfies("not-a-version", ">=1.0")
        assert not version_satisfies("1.0.0", "^1.0")

    def test_mapping_style_constraint_is_validated_with_pep440(self):
        issues = validate_dependencies(
            {
                "pack_a": {"dependencies": {"pack_b": ">=2.0"}},
                "pack_b": {"version": "1.9.9"},
            }
        )
        assert issues == [
            {
                "type": "version_mismatch",
                "pack_id": "pack_a",
                "depends_on": "pack_b",
                "required": ">=2.0",
                "actual": "1.9.9",
            }
        ]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
