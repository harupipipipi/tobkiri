"""Caller-scoped regression tests for optional Pack dependency projection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog, dynamic_profile_edges


def _manifest(
    pack_id: str,
    contracts: dict[str, str],
    *,
    required: tuple[str, ...] = (),
    dependencies: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "pack": {"id": pack_id},
        "contracts": [
            {"contract_id": contract, "operations": [operation]}
            for contract, operation in contracts.items()
        ],
        "functions": [
            {"id": f"{pack_id}.{contract}", "operations": [operation]}
            for contract, operation in contracts.items()
        ],
        "requirements": {
            "contract_dependencies": [
                {"contract_id": contract, "optional": False} for contract in required
            ],
            "pack_dependencies": {dependency: ">=1,<2" for dependency in dependencies},
        },
    }


def _catalog(
    *,
    b_contract: str = "c.one",
    static_edges: tuple[dict[str, str], ...] = (),
    consumer: bool = False,
) -> BundledCatalog:
    packs = {
        "shell": {
            "functions": [{"id": "shell.main", "role": "brokered"}],
        },
        "a": _manifest(
            "a", {"a.own": "a.op"}, required=("c.one",), dependencies=("d",)
        ),
        "b": _manifest(
            "b", {"b.own": "b.op"}, required=(b_contract,), dependencies=("d",)
        ),
        "d": _manifest(
            "d",
            {"c.one": "d.one", "c.two": "d.two"},
            required=("a.own",) if consumer else (),
        ),
    }
    return BundledCatalog(
        root=Path("."),
        packs=packs,
        bases={},
        shells={"shell.main": {"pack_id": "shell"}},
        profiles={
            "defaults": {
                "shell": {"provider_id": "shell.main"},
                "requested_edges": list(static_edges),
            }
        },
    )


def _keys(edges: tuple[dict[str, Any], ...]) -> set[tuple[str, str, str, str]]:
    return {
        (
            edge["caller_function_id"],
            edge["target_provider_id"],
            edge["contract_id"],
            edge["operation_id"],
        )
        for edge in edges
    }


def _static(caller: str, target: str, contract: str, operation: str) -> dict[str, str]:
    return {
        "caller_function_id": caller,
        "target_provider_id": target,
        "contract_id": contract,
        "operation_id": operation,
    }


def test_static_shell_edge_does_not_suppress_another_caller() -> None:
    """An existing Shell binding cannot replace a new Pack's caller grant."""

    static = _static("shell.main", "d.c.one", "c.one", "d.one")
    keys = _keys(dynamic_profile_edges(_catalog(static_edges=(static,)), "defaults", ("a",)))

    assert ("a.a.own", "d.c.one", "c.one", "d.one") in keys
    assert ("shell.main", "d.c.one", "c.one", "d.one") not in keys


def test_two_roots_can_share_one_exact_provider_and_contract() -> None:
    """A shared provider retains a separate grant for each signed caller."""

    keys = _keys(dynamic_profile_edges(_catalog(), "defaults", ("a", "b")))

    assert ("a.a.own", "d.c.one", "c.one", "d.one") in keys
    assert ("b.b.own", "d.c.one", "c.one", "d.one") in keys


def test_shared_provider_does_not_cross_product_distinct_contracts() -> None:
    """Each root receives only the Contract it declared as required."""

    keys = _keys(
        dynamic_profile_edges(_catalog(b_contract="c.two"), "defaults", ("a", "b"))
    )

    assert ("a.a.own", "d.c.one", "c.one", "d.one") in keys
    assert ("b.b.own", "d.c.two", "c.two", "d.two") in keys
    assert ("a.a.own", "d.c.two", "c.two", "d.two") not in keys
    assert ("b.b.own", "d.c.one", "c.one", "d.one") not in keys


def test_selected_root_can_also_serve_as_another_roots_direct_dependency() -> None:
    """Selecting a dependency itself keeps both exact callers without widening A."""

    for selected in (("a", "d"), ("d", "a")):
        keys = _keys(dynamic_profile_edges(_catalog(), "defaults", selected))

        assert ("shell.main", "d.c.one", "c.one", "d.one") in keys
        assert ("shell.main", "d.c.two", "c.two", "d.two") in keys
        assert ("a.a.own", "d.c.one", "c.one", "d.one") in keys
        assert ("a.a.own", "d.c.two", "c.two", "d.two") not in keys


def test_static_edge_does_not_suppress_consumer_link() -> None:
    """A dependency's caller grant survives an identical Shell operation."""

    static = _static("shell.main", "a.a.own", "a.own", "a.op")
    keys = _keys(
        dynamic_profile_edges(
            _catalog(static_edges=(static,), consumer=True), "defaults", ("a",)
        )
    )

    assert ("d.c.one", "a.a.own", "a.own", "a.op") in keys
    assert ("shell.main", "a.a.own", "a.own", "a.op") not in keys
