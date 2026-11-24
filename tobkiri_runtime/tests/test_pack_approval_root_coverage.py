"""Optional Pack grants are backed by one exact signed dependency root."""

from types import SimpleNamespace

import pytest

from core_runtime.bootstrap.production_v4 import (
    _optional_pack_approval_roots,
    _packvm_approval_provenance,
)


def _coverage(graph):
    catalog = SimpleNamespace(packs={
        pack: {"requirements": {"pack_dependencies": dependencies}}
        for pack, dependencies in graph.items()
    })
    return _optional_pack_approval_roots(catalog, set(graph))


@pytest.mark.parametrize(
    ("caller", "target", "expected"),
    [
        ("a", "shared", (True, "a")),
        ("b", "shared", (True, "b")),
        ("a", "leaf", (True, "a")),
        ("a", "b", (False, None)),
        ("shared", "leaf", (False, None)),
        ("static", "a", (True, "a")),
        ("static", "shared", (False, None)),
        ("static", "static", (True, None)),
    ],
)
def test_only_unique_common_approval_root_covers_exact_edge(caller, target, expected):
    graph = {"a": ["shared"], "b": ["shared"], "shared": ["leaf"], "leaf": []}
    coverage = _coverage(graph)
    assert _packvm_approval_provenance(
        caller_artifact_digest=caller,
        target_pack_id=target,
        optional_pack_ids=set(graph),
        pack_ids_by_artifact_digest={pack: {pack} for pack in (*graph, "static")},
        approval_roots_by_pack=coverage,
    ) == expected


def test_single_root_covers_transitive_signed_dependency_edges():
    assert _coverage({"root": ["dependency"], "dependency": ["leaf"], "leaf": []}) == {
        "root": {"root"}, "dependency": {"root"}, "leaf": {"root"}
    }


def test_dependency_cycle_does_not_invent_an_approval_root():
    assert _coverage({"a": ["b"], "b": ["a"]}) == {"a": set(), "b": set()}


def test_ambiguous_caller_digest_stays_denied():
    assert _packvm_approval_provenance(
        caller_artifact_digest="same",
        target_pack_id="dependency",
        optional_pack_ids={"root", "dependency"},
        pack_ids_by_artifact_digest={"same": {"root", "dependency"}},
        approval_roots_by_pack={"root": {"root"}, "dependency": {"root"}},
    ) == (False, None)
