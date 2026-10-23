"""Palette selection cannot imply access to unrelated Profile operations."""
from types import SimpleNamespace

from core_runtime.workflow_v4.integration import _ResolvedCatalog


def binding(name):
    return SimpleNamespace(
        operation=SimpleNamespace(input_schema={"type": "object"}, output_schema={"type": "object"},
            contract_id=f"example.{name}.v1", revision_digest="sha256:" + "1" * 64,
            operation_id=name, effect_class=SimpleNamespace(value="operation.invoke")),
        principal_ref=SimpleNamespace(value=f"principal.{name}"),
        function=SimpleNamespace(function_id=f"provider.{name}"),
    )


def test_palette_uses_only_actual_outgoing_plan_edges():
    allowed, unrelated = binding("allowed"), binding("unrelated")
    context = SimpleNamespace(catalog_bindings=(allowed, unrelated), outbound_catalog_bindings=(allowed,),
                              security_epoch=1, activation={"activation_id": "activation:fixture"})
    catalog = _ResolvedCatalog(context).snapshot()
    assert [row["operation_id"] for row in catalog["operations"]] == ["allowed"]
    assert len(context.catalog_bindings) == 2


def test_explicit_empty_outgoing_edges_do_not_fall_back_to_all_profile_operations():
    context = SimpleNamespace(catalog_bindings=(binding("unrelated"),), outbound_catalog_bindings=(),
                              security_epoch=1, activation={"activation_id": "activation:fixture"})
    assert _ResolvedCatalog(context).snapshot()["operations"] == []
