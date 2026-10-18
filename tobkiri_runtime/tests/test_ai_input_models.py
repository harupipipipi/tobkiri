from __future__ import annotations

from ecosystem.defaultspack.domain.ai_input.ai_input_models import (
    edge_from_dict,
    normalize_ai_input_config,
)


def test_normalize_ai_input_config_accepts_budgets_and_vector_gate_defaults() -> None:
    config = normalize_ai_input_config(
        {
            "disabled_edges": "edge:a, edge:b",
            "gates": {
                "gate:vector": {
                    "kind": "vector_gate",
                    "threshold": 0.8,
                }
            },
            "budget": {
                "max_system_tokens": 1200,
                "max_tool_schema_tokens": 600,
                "strategy": "priority",
            },
        },
        strict=True,
    )

    assert config["disabled_edges"] == ["edge:a", "edge:b"]
    assert config["gates"]["gate:vector"]["runtime_enabled"] is False
    assert config["budgets"]["system"]["max_tokens"] == 1200
    assert config["budgets"]["tools"]["max_tokens"] == 600


def test_edge_from_dict_reads_ports_from_metadata_for_backwards_compatibility() -> None:
    edge = edge_from_dict(
        {
            "id": "edge:prompt->model",
            "from_id": "prompt:default",
            "to_id": "model_input:default",
            "kind": "contributes_to",
            "metadata": {"from_port": "output", "to_port": "system"},
        },
        strict=True,
    )

    assert edge is not None
    assert edge.from_port == "output"
    assert edge.to_port == "system"
