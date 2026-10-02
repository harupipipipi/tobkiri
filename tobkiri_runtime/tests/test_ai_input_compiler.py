from __future__ import annotations

from ecosystem.defaultspack.domain.ai_input.ai_input_compiler import (
    MODEL_INPUT_NODE_ID,
    compile_effective_ai_input,
)
from ecosystem.defaultspack.domain.ai_input.ai_input_models import (
    AiInputEdge,
    AiInputNode,
    AiInputSegmentRegistry,
    PromptSegment,
)


def test_condition_gate_controls_prompt_segment() -> None:
    segment = PromptSegment(
        id="prompt:browser",
        text="Browser automation rules",
        source="test",
        source_type="profile_prompt",
        tokens=8,
        metadata={"allow_disable": True},
    )
    nodes = [
        AiInputNode(id=MODEL_INPUT_NODE_ID, kind="model_input", label="Model", input_ports=["system"]),
        AiInputNode(id="prompt:browser", kind="prompt_segment", label="Browser", output_ports=["output"]),
        AiInputNode(
            id="gate:browser",
            kind="condition_gate",
            label="Browser intent",
            input_ports=["input"],
            output_ports=["pass"],
        ),
    ]
    edges = [
        AiInputEdge(
            id="edge:prompt:browser->gate:browser",
            from_id="prompt:browser",
            from_port="output",
            to_id="gate:browser",
            to_port="input",
            kind="contributes_to",
        ),
        AiInputEdge(
            id="edge:gate:browser->model_input:default.system",
            from_id="gate:browser",
            from_port="pass",
            to_id=MODEL_INPUT_NODE_ID,
            to_port="system",
            kind="gates",
        ),
    ]
    config = {
        "version": 1,
        "disabled_edges": [],
        "inserted_edges": [],
        "budgets": {},
        "gates": {
            "gate:browser": {
                "kind": "condition_gate",
                "expression": {"field": "user_intent", "op": "eq", "value": "browser_automation"},
                "default": False,
            }
        },
    }

    blocked = compile_effective_ai_input(
        profile_id="profile",
        nodes=nodes,
        edges=edges,
        segments=AiInputSegmentRegistry(prompt_segments={segment.id: segment}),
        policy={},
        ai_input_config=config,
        request_context={"message": "hello"},
    )
    allowed = compile_effective_ai_input(
        profile_id="profile",
        nodes=nodes,
        edges=edges,
        segments=AiInputSegmentRegistry(prompt_segments={segment.id: segment}),
        policy={},
        ai_input_config=config,
        request_context={"message": "ブラウザを開いて"},
    )

    assert blocked.system_segments == []
    assert blocked.disabled_segments[0]["id"] == "prompt:browser"
    assert allowed.system_segments == [segment]


def test_non_disableable_segment_rejects_disabled_edge_override() -> None:
    segment = PromptSegment(
        id="policy:profile",
        text="{}",
        source="profile.policy",
        source_type="profile_policy",
        tokens=1,
        metadata={"allow_disable": False},
    )
    edge = AiInputEdge(
        id="edge:policy:profile->model_input:default.policy",
        from_id="policy:profile",
        from_port="rules",
        to_id=MODEL_INPUT_NODE_ID,
        to_port="policy",
        kind="provides_policy",
    )
    effective = compile_effective_ai_input(
        profile_id="profile",
        nodes=[
            AiInputNode(id=MODEL_INPUT_NODE_ID, kind="model_input", label="Model", input_ports=["policy"]),
            AiInputNode(id="policy:profile", kind="profile_policy", label="Policy", output_ports=["rules"]),
        ],
        edges=[edge],
        segments=AiInputSegmentRegistry(policy_segments={segment.id: segment}),
        policy={},
        ai_input_config={
            "version": 1,
            "disabled_edges": [edge.id],
            "inserted_edges": [],
            "gates": {},
            "budgets": {},
        },
    )

    graph_edge = effective.graph["edges"][0]
    assert graph_edge["active"] is True
    assert graph_edge["metadata"]["disable_rejected"] is True
    assert effective.policy["segments"]
