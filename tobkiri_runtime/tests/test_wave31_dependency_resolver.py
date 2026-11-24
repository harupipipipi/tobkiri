"""
test_wave31_dependency_resolver.py - Wave 31 依存関係解決テスト

resolve_load_order() の 11 ケースを網羅する。
"""

import logging

import pytest

from core_runtime.dependency_resolver import (
    CircularDependencyError,
    DependencyError,
    MissingDependencyError,
    resolve_load_order,
    validate_dependencies,
)


# --------------------------------------------------------------------------- #
# 1. 線形依存: A→B→C  →  [C, B, A]
# --------------------------------------------------------------------------- #
class TestLinearDependency:
    def test_linear_chain(self):
        packs = {
            "A": {"depends_on": [{"pack_id": "B"}]},
            "B": {"depends_on": [{"pack_id": "C"}]},
            "C": {},
        }
        order = resolve_load_order(packs)
        assert order == ["C", "B", "A"]

    def test_linear_ordering_respected(self):
        """依存先は必ず依存元より前に来る。"""
        packs = {
            "A": {"depends_on": [{"pack_id": "B"}]},
            "B": {"depends_on": [{"pack_id": "C"}]},
            "C": {},
        }
        order = resolve_load_order(packs)
        assert order.index("C") < order.index("B") < order.index("A")


# --------------------------------------------------------------------------- #
# 2. 並列依存: A→B, A→C  →  B,C が先、A が後
# --------------------------------------------------------------------------- #
class TestParallelDependency:
    def test_parallel_deps(self):
        packs = {
            "A": {"depends_on": [{"pack_id": "B"}, {"pack_id": "C"}]},
            "B": {},
            "C": {},
        }
        order = resolve_load_order(packs)
        assert order == ["B", "C", "A"]

    def test_a_is_last(self):
        packs = {
            "A": {"depends_on": [{"pack_id": "B"}, {"pack_id": "C"}]},
            "B": {},
            "C": {},
        }
        order = resolve_load_order(packs)
        assert order[-1] == "A"
        assert "B" in order[:-1]
        assert "C" in order[:-1]


# --------------------------------------------------------------------------- #
# 3. 依存なし: A, B, C  →  アルファベット順
# --------------------------------------------------------------------------- #
class TestNoDependency:
    def test_alphabetical(self):
        packs = {
            "C": {},
            "A": {},
            "B": {},
        }
        order = resolve_load_order(packs)
        assert order == ["A", "B", "C"]


# --------------------------------------------------------------------------- #
# 4. 循環検出: A→B→C→A  →  CircularDependencyError
# --------------------------------------------------------------------------- #
class TestCircularDependency:
    def test_simple_cycle(self):
        packs = {
            "A": {"depends_on": [{"pack_id": "B"}]},
            "B": {"depends_on": [{"pack_id": "C"}]},
            "C": {"depends_on": [{"pack_id": "A"}]},
        }
        with pytest.raises(CircularDependencyError) as exc_info:
            resolve_load_order(packs)
        assert "Circular dependency" in str(exc_info.value)

    def test_cycle_reports_involved_packs(self):
        packs = {
            "A": {"depends_on": [{"pack_id": "B"}]},
            "B": {"depends_on": [{"pack_id": "A"}]},
            "C": {},
        }
        with pytest.raises(CircularDependencyError) as exc_info:
            resolve_load_order(packs)
        msg = str(exc_info.value)
        assert "A" in msg
        assert "B" in msg

    def test_circular_is_dependency_error_subclass(self):
        """CircularDependencyError は DependencyError のサブクラス。"""
        assert issubclass(CircularDependencyError, DependencyError)


# --------------------------------------------------------------------------- #
# 5. 依存先不在 (strict=False): A→X  →  警告してスキップ、A はロード
# --------------------------------------------------------------------------- #
class TestMissingDependencyNonStrict:
    def test_missing_dep_skipped(self):
        packs = {
            "A": {"depends_on": [{"pack_id": "X"}]},
        }
        order = resolve_load_order(packs, strict=False)
        assert order == ["A"]

    def test_missing_dep_logs_warning(self, caplog):
        packs = {
            "A": {"depends_on": [{"pack_id": "X"}]},
        }
        with caplog.at_level(logging.WARNING):
            resolve_load_order(packs, strict=False)
        assert any("X" in record.message and "not installed" in record.message
                    for record in caplog.records)


# --------------------------------------------------------------------------- #
# 6. 依存先不在 (strict=True): A→X  →  MissingDependencyError
# --------------------------------------------------------------------------- #
class TestMissingDependencyStrict:
    def test_missing_dep_raises(self):
        packs = {
            "A": {"depends_on": [{"pack_id": "X"}]},
        }
        with pytest.raises(MissingDependencyError) as exc_info:
            resolve_load_order(packs, strict=True)
        msg = str(exc_info.value)
        assert "A" in msg
        assert "X" in msg
        assert "not installed" in msg

    def test_missing_is_dependency_error_subclass(self):
        assert issubclass(MissingDependencyError, DependencyError)


# --------------------------------------------------------------------------- #
# 7. 空辞書  →  空リスト
# --------------------------------------------------------------------------- #
class TestEmptyInput:
    def test_empty_dict(self):
        assert resolve_load_order({}) == []


# --------------------------------------------------------------------------- #
# 8. 単一 Pack  →  ["A"]
# --------------------------------------------------------------------------- #
class TestSinglePack:
    def test_single(self):
        assert resolve_load_order({"A": {}}) == ["A"]


# --------------------------------------------------------------------------- #
# 9. ダイヤモンド依存: A→B, A→C, B→D, C→D  →  D 最初、A 最後
# --------------------------------------------------------------------------- #
class TestDiamondDependency:
    def test_diamond(self):
        packs = {
            "A": {"depends_on": [{"pack_id": "B"}, {"pack_id": "C"}]},
            "B": {"depends_on": [{"pack_id": "D"}]},
            "C": {"depends_on": [{"pack_id": "D"}]},
            "D": {},
        }
        order = resolve_load_order(packs)
        assert order == ["D", "B", "C", "A"]

    def test_diamond_ordering_constraints(self):
        packs = {
            "A": {"depends_on": [{"pack_id": "B"}, {"pack_id": "C"}]},
            "B": {"depends_on": [{"pack_id": "D"}]},
            "C": {"depends_on": [{"pack_id": "D"}]},
            "D": {},
        }
        order = resolve_load_order(packs)
        assert order.index("D") < order.index("B")
        assert order.index("D") < order.index("C")
        assert order.index("B") < order.index("A")
        assert order.index("C") < order.index("A")


# --------------------------------------------------------------------------- #
# 10. depends_on が文字列形式
# --------------------------------------------------------------------------- #
class TestStringDependency:
    def test_string_format(self):
        packs = {
            "pack_a": {"depends_on": ["pack_b"]},
            "pack_b": {},
        }
        order = resolve_load_order(packs)
        assert order == ["pack_b", "pack_a"]


# --------------------------------------------------------------------------- #
# 11. depends_on が dict 形式 (version 付き)
# --------------------------------------------------------------------------- #
class TestDictDependencyWithVersion:
    def test_dict_with_version(self):
        packs = {
            "pack_a": {"depends_on": [{"pack_id": "pack_b", "version": ">=1.0"}]},
            "pack_b": {},
        }
        order = resolve_load_order(packs)
        assert order == ["pack_b", "pack_a"]

    def test_version_field_is_non_strict_compatible(self):
        """non-strict mode keeps ordering compatibility for mismatched versions."""
        packs_with_ver = {
            "pack_a": {"depends_on": [{"pack_id": "pack_b", "version": ">=99.0"}]},
            "pack_b": {},
        }
        packs_without_ver = {
            "pack_a": {"depends_on": [{"pack_id": "pack_b"}]},
            "pack_b": {},
        }
        assert resolve_load_order(packs_with_ver) == resolve_load_order(packs_without_ver)

    def test_version_mismatch_is_reported_by_validation(self):
        packs = {
            "pack_a": {"depends_on": [{"pack_id": "pack_b", "version": ">=99.0"}]},
            "pack_b": {"version": "1.2.3"},
        }

        issues = validate_dependencies(packs)

        assert issues == [
            {
                "type": "version_mismatch",
                "pack_id": "pack_a",
                "depends_on": "pack_b",
                "required": ">=99.0",
                "actual": "1.2.3",
            }
        ]
