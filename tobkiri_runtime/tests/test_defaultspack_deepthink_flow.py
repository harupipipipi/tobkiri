from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def _response_text(response):
    return "\n".join(
        str(block.get("text") or "")
        for block in response.get("content", [])
        if isinstance(block, dict)
    )


def test_deepthink_runs_declarative_flow_and_revises_until_approved(
    tmp_path,
    monkeypatch,
):
    from domain.ai_client.rumi_process_runner import RumiProcessRunner
    from domain.ai_client import deepthink_extensions
    from domain.flow import FlowEngine

    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_FLOW_RUN_STORE",
        str(tmp_path / "flow-runs.json"),
    )
    FlowEngine.reset_instance()
    monkeypatch.setattr(
        deepthink_extensions,
        "deepthink_extension_contract",
        lambda: {
            "discovery_tools": [],
            "phases": [
                {
                    "id": "legal_review",
                    "label": "法務確認",
                    "prompt": "Check legal constraints.",
                    "source_pack_id": "test_pack",
                }
            ],
            "perspectives": [],
            "presentation": {
                "schema_version": 1,
                "id": "test.deepthink.v1",
                "title": "Test DeepThink",
            },
        },
    )
    calls = []
    activity_events = []
    review_round = 0

    def complete(model, messages, tools, params):
        nonlocal review_round
        del params
        system = str(messages[0]["content"])
        calls.append(
            {
                "model": model,
                "system": system,
                "tools": list(tools),
                "user": str(messages[1]["content"]) if len(messages) > 1 else "",
            }
        )
        if "Plan the response before writing it" in system:
            text = json.dumps(
                {
                    "structure": ["Answer"],
                    "key_points": ["Be precise"],
                    "risks": ["Ambiguity"],
                }
            )
        elif "Assess the actual selected model's task-specific limits" in system:
            text = json.dumps(
                {
                    "evidence": [
                        {
                            "kind": "past_artifact",
                            "reference": "conversation output",
                            "finding": "Visual layout needs checking",
                        }
                    ],
                    "limitations": ["Visual layout needs checking"],
                    "uncertainties": [],
                    "evidence_sufficient": True,
                    "method": {
                        "approach": "host_tool",
                        "reason": "Inspect the result",
                        "fallback": "Request feedback",
                    },
                }
            )
        elif "Write one visible pseudo DeepThinking step" in system:
            text = json.dumps(
                {"thinking": "Check explicit constraints.", "output": "Use evidence."}
            )
        elif "stateless third-party reviewer" in system.lower():
            review_round += 1
            text = json.dumps(
                {
                    "pass": review_round >= 2,
                    "score": 100 if review_round >= 2 else 70,
                    "issues": [] if review_round >= 2 else ["Add detail"],
                    "required_changes": [] if review_round >= 2 else ["Add detail"],
                }
            )
        elif "stand alone" in system.lower():
            text = "Final answer revised" if review_round else "Final answer"
        elif "section" in system.lower():
            text = "Section draft"
        else:
            text = "Final answer"
        return {
            "content": [{"type": "text", "text": text}],
            "usage": {
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 5,
            },
        }

    process = {
        "trace_id": "trace-test",
        "deepthink_enabled": True,
        "events": [],
        "watchdog": {},
    }
    runner = RumiProcessRunner(
        complete=complete,
        response_text=_response_text,
        error_kind=lambda exc: type(exc).__name__,
    )
    response = runner.run_review_chain(
        composite={"budget": {"deepthink_max_sections": 1}},
        generator_member={"metadata": {"thinking_level": "medium"}},
        reviewer_member={"metadata": {"thinking_level": "medium"}},
        generator_model="demo/conversation-model",
        reviewer_model="demo/conversation-model",
        messages=[{"role": "user", "content": "Give a robust answer"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "host_search",
                    "description": "Host-side discovery",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        params={"deepthink_enabled": True},
        context={
            "mode": "deepthink",
            "harness_tool_selection": {},
            "activity_event_callback": activity_events.append,
        },
        process=process,
        max_reviews=2,
    )

    assert response["finish_reason"] == "stop"
    assert response["content"][0]["text"] == "Final answer revised"
    metadata = response["metadata"]["rumi_process"]
    assert metadata["review"]["approved"] is True
    assert metadata["flow"]["flow_id"] == "defaultspack.deepthink"
    assert metadata["flow"]["status"] == "completed"
    phases = [event["phase"] for event in metadata["events"]]
    assert "deepthink_planner" in phases
    assert "deepthink_capability_assessment" in phases
    assert "deepthink_notes" in phases
    assert "deepthink_synthesizing" in phases
    assert phases.count("deepthink_reviewing") == 2
    assert "deepthink_revising" in phases
    assert all(event.get("output_preview", "") == "" for event in metadata["events"])
    assert {call["model"] for call in calls} == {"demo/conversation-model"}
    planner_call = next(
        call for call in calls if "Plan the response before writing it" in call["system"]
    )
    assert planner_call["tools"] == []
    integration_call = next(
        call for call in calls if "Plan which host-provided tools" in call["system"]
    )
    assert json.loads(integration_call["user"])["capability_assessment"]["method"][
        "approach"
    ] == "host_tool"
    assert [event["deepthink_phase"] for event in activity_events] == [
        "preflight",
        "planning",
        "capability_assessment",
        "integrations",
        "integrations",
        "legal_review",
        "evidence",
        "drafting",
        "synthesizing",
        "reviewing",
        "revising",
        "reviewing",
        "completed",
    ]
    assert all("output" not in event and "output_preview" not in event for event in activity_events)
    assert {
        event.get("presentation_template_id") for event in activity_events
    } == {"test.deepthink.v1"}
    assert activity_events[0]["presentation"]["id"] == "test.deepthink.v1"
    assert all(
        "presentation" not in event for event in activity_events[1:]
    )
    assert metadata["deepthink"]["profile_phase_outputs"][0]["id"] == "legal_review"
    assert metadata["deepthink"]["capability_assessment"]["model"] == (
        "demo/conversation-model"
    )
    assert metadata["deepthink"]["capability_assessment"]["method"]["approach"] == (
        "host_tool"
    )

    run = FlowEngine().get_run(metadata["flow"]["run_id"])
    assert run["status"] == "completed"
    assert run["budget"]["used_tokens"] == len(calls) * 5
    assert any(item["phase"] == "review_loop" for item in run["checkpoints"])


def test_deepthink_flow_recovers_when_reviewer_and_json_repair_are_malformed(
    tmp_path,
    monkeypatch,
):
    from domain.ai_client.rumi_process_runner import RumiProcessRunner
    from domain.ai_client import deepthink_extensions
    from domain.flow import FlowEngine

    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_FLOW_RUN_STORE",
        str(tmp_path / "flow-runs.json"),
    )
    FlowEngine.reset_instance()
    monkeypatch.setattr(
        deepthink_extensions,
        "deepthink_extension_contract",
        lambda: {
            "discovery_tools": [],
            "phases": [],
            "perspectives": [],
            "presentation": {},
        },
    )
    review_round = 0

    def complete(model, messages, tools, params):
        nonlocal review_round
        del model, tools, params
        system = str(messages[0]["content"])
        if "Repair malformed JSON" in system:
            text = "still not JSON"
        elif "Plan the response before writing it" in system:
            text = json.dumps(
                {"structure": ["Answer"], "key_points": ["Precise"], "risks": []}
            )
        elif "Write one visible pseudo DeepThinking step" in system:
            text = json.dumps({"thinking": "Check.", "output": "Proceed."})
        elif "stateless third-party reviewer" in system.lower():
            review_round += 1
            text = (
                "not JSON"
                if review_round == 1
                else json.dumps(
                    {
                        "pass": True,
                        "score": 90,
                        "issues": [],
                        "required_changes": [],
                    }
                )
            )
        elif "section only" in system.lower():
            text = "Section draft"
        elif "stand alone" in system.lower():
            text = "Revised answer" if review_round else "Initial answer"
        else:
            text = "Initial answer"
        return {"content": [{"type": "text", "text": text}], "usage": {}}

    process = {
        "trace_id": "trace-review-recovery",
        "deepthink_enabled": True,
        "events": [],
        "watchdog": {},
    }
    runner = RumiProcessRunner(
        complete=complete,
        response_text=_response_text,
        error_kind=lambda exc: type(exc).__name__,
    )
    response = runner.run_review_chain(
        composite={"budget": {"deepthink_max_sections": 1}},
        generator_member={"metadata": {}},
        reviewer_member={"metadata": {}},
        generator_model="demo/model",
        reviewer_model="demo/model",
        messages=[{"role": "user", "content": "Give a robust answer"}],
        tools=[],
        params={"deepthink_enabled": True},
        context={"mode": "deepthink", "harness_tool_selection": {}},
        process=process,
        max_reviews=2,
    )

    metadata = response["metadata"]["rumi_process"]
    assert response["finish_reason"] == "stop"
    assert response["content"][0]["text"] == "Revised answer"
    assert metadata["review"]["approved"] is True
    assert any(
        event["phase"] == "review_json_fallback"
        and event["metadata"]["fail_closed"] is True
        for event in metadata["events"]
    )


def test_deepthink_flow_contract_has_safe_bounded_review_loop():
    from domain.ai_client.rumi_process import default_rumi_model_pack
    from domain.flow import FlowEngine

    FlowEngine.reset_instance()
    engine = FlowEngine()
    flow = engine.get_flow("defaultspack.deepthink")
    default_pack = default_rumi_model_pack(base_model="demo/model")

    assert engine.validate_flow("defaultspack.deepthink") == []
    assert flow["durable"] is True
    assert flow["budgets"]["max_tokens"] == 300000
    assert default_pack["budget"]["timeout_seconds"] == 600
    assert default_pack["budget"]["deepthink_timeout_seconds"] == 21600
    assert [step["id"] for step in flow["steps"]] == [
        "preflight",
        "plan",
        "capability_assessment",
        "integrations",
        "profile_phases",
        "evidence",
        "section_drafts",
        "synthesize",
        "review_loop",
        "final",
    ]
    loop = flow["steps"][8]
    assert loop["max_iterations"] == 8
    assert loop["until"] == "{{review.pass}}"
    assert loop["checkpoint_each_iteration"] is True
    assert loop["dedupe_key"] == "{{review.feedback_hash}}"
    review_input = loop["steps"][0]["input"]
    assert review_input["evidence"] == "{{evidence}}"
    assert review_input["integrations"] == "{{integrations}}"
    assert review_input["profile_phase_outputs"] == "{{profile_phase_outputs}}"
    assert review_input["capability_assessment"] == "{{capability_assessment}}"


def test_capability_assessment_does_not_promote_model_claims_to_verified():
    from domain.ai_client.deepthink_capability import normalize_capability_assessment

    assessment = normalize_capability_assessment(
        {
            "evidence": [
                {
                    "kind": "benchmark",
                    "reference": "unverified score",
                    "finding": "Some result",
                    "verification": "verified",
                }
            ],
            "limitations": ["Weak visual editing"],
            "evidence_sufficient": True,
            "method": {"approach": "software", "reason": "Inspect visually"},
        },
        model="selected/provider-model",
    )
    assert assessment["model"] == "selected/provider-model"
    assert assessment["evidence"][0]["verification"] == "model_reported"
    assert assessment["method"]["approach"] == "software"
    unknown = normalize_capability_assessment("malformed", model="selected/model")
    assert unknown["evidence_sufficient"] is False
    assert unknown["method"]["approach"] == "clarify"


def test_deepthink_runtime_can_shrink_but_not_widen_review_loop():
    from domain.flow.context import FlowContext
    from domain.flow.engine import FlowEngine

    engine = FlowEngine()
    step = {
        "id": "review_loop",
        "type": "loop",
        "max_iterations": 8,
        "until": False,
        "dedupe_key": "{{loop.iteration}}",
        "on_exhausted": "return_last",
        "steps": [],
    }
    shrunk = FlowContext(
        flow_id="test",
        trigger_input={},
        flow_config={},
        execution_id="shrink",
        parent_context={"_flow_loop_max_iterations": {"review_loop": 2}},
    )
    widened = FlowContext(
        flow_id="test",
        trigger_input={},
        flow_config={},
        execution_id="widen",
        parent_context={"_flow_loop_max_iterations": {"review_loop": 20}},
    )

    shrunk_result = engine._execute_loop_step("test", step, {}, {}, shrunk)
    widened_result = engine._execute_loop_step("test", step, {}, {}, widened)

    assert shrunk_result["data"]["iterations"] == 2
    assert widened_result["data"]["iterations"] == 8


def test_deepthink_plan_segments_preserve_every_planned_section():
    from domain.ai_client.rumi_process import deepthink_plan_segments

    sections = [f"Section {index}" for index in range(1, 8)]
    grouped = deepthink_plan_segments({"structure": sections}, max_sections=3)

    assert len(grouped) == 3
    for section in sections:
        assert sum(section in group for group in grouped) == 1


def test_deepthink_review_cannot_pass_with_required_changes():
    from domain.ai_client.rumi_process import sanitize_deepthink_review

    review = sanitize_deepthink_review(
        {
            "pass": True,
            "score": 95,
            "issues": ["Needs evidence"],
            "required_changes": ["Add exact file evidence"],
        }
    )

    assert review["pass"] is False
    assert review["required_changes"] == ["Add exact file evidence"]


def test_deepthink_review_cannot_pass_below_score_threshold():
    from domain.ai_client.rumi_process import sanitize_deepthink_review

    review = sanitize_deepthink_review(
        {
            "pass": True,
            "score": 10,
            "issues": [],
            "required_changes": [],
        }
    )

    assert review["pass"] is False
    assert review["score"] == 10


def test_deepthink_model_calls_raise_short_timeouts_to_long_running_floor():
    from domain.ai_client.rumi_process_runner import RumiProcessRunner

    params = RumiProcessRunner._review_chain_params(
        {"metadata": {}},
        {"request_timeout": 120},
        {"mode": "deepthink"},
    )

    assert params["request_timeout"] == 1800.0
    assert params["request_retries"] == 3


def test_deepthink_chat_routing_preserves_conversation_model_as_base():
    from domain.chat.run_request import _route_deepthink_model

    params = {
        "deepthink_enabled": True,
        "deepthink_model_source": "conversation",
    }

    routed = _route_deepthink_model(
        "opencode-zen/deepseek-v4-flash-free",
        params,
    )

    assert routed == "modelpack/rumi"
    assert (
        params["rumi_base_model_override"]
        == "opencode-zen/deepseek-v4-flash-free"
    )


def test_deepthink_bypasses_direct_provider_compiler():
    from types import SimpleNamespace

    from domain.chat.stream_engine import ChatRunEngine

    prepared = SimpleNamespace(params={"deepthink_enabled": True})
    gateway = SimpleNamespace(resolve_provider=lambda model: (object(), model))

    assert ChatRunEngine._use_provider_compiler(prepared, gateway) is False


def test_deepthink_model_is_provider_neutral_and_respects_settings_selection(
    monkeypatch,
):
    from domain.ai_client.client import AIClient
    from domain.ai_client.rumi_process_runner import RumiProcessRunner

    captured = []

    def fake_run(self, **kwargs):
        del self
        captured.append(
            (
                kwargs["generator_model"],
                kwargs["reviewer_model"],
                kwargs["process"]["deepthink_model_selection"],
            )
        )
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(RumiProcessRunner, "run_review_chain", fake_run)
    monkeypatch.setattr(
        AIClient,
        "_resolve_rumi_member_model",
        lambda self, model, params=None: model,
    )
    client = AIClient()
    members = [
        {"model": "demo/conversation", "metadata": {"role": "generator"}},
        {"model": "demo/reviewer", "metadata": {"role": "reviewer"}},
    ]
    client._complete_review_chain(
        {},
        members,
        [{"role": "user", "content": "hello"}],
        [],
        {
            "deepthink_enabled": True,
            "deepthink_model_source": "conversation",
        },
    )
    client._complete_review_chain(
        {},
        members,
        [{"role": "user", "content": "hello"}],
        [],
        {
            "deepthink_enabled": True,
            "deepthink_model_source": "selected",
            "deepthink_model": "another-provider/selected-model",
        },
    )

    assert captured == [
        (
            "demo/conversation",
            "demo/conversation",
            {
                "source": "conversation",
                "model": "demo/conversation",
                "provider_neutral": True,
            },
        ),
        (
            "another-provider/selected-model",
            "another-provider/selected-model",
            {
                "source": "selected",
                "model": "another-provider/selected-model",
                "provider_neutral": True,
            },
        ),
    ]


def test_deepthink_background_continuations_are_detected_for_suppression():
    from domain.chat.run_request import _is_background_deepthink_trigger

    assert _is_background_deepthink_trigger(
        {"source": "scheduler"},
        {},
    )
    assert _is_background_deepthink_trigger(
        {"source": "ci_completion_followup"},
        {},
    )
    assert not _is_background_deepthink_trigger(
        {"source": "user"},
        {},
    )


def test_deepthink_returns_mixed_comment_and_tool_call_to_chat_approval_loop(
    tmp_path,
    monkeypatch,
):
    from domain.ai_client.rumi_process_runner import RumiProcessRunner
    from domain.flow import FlowEngine

    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_FLOW_RUN_STORE",
        str(tmp_path / "flow-runs.json"),
    )
    FlowEngine.reset_instance()
    seen_tool_sets = []

    def complete(model, messages, tools, params):
        del model, params
        system = str(messages[0]["content"])
        if "Plan the response before writing it" in system:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "structure": ["Answer"],
                                "key_points": ["Use current evidence"],
                                "risks": [],
                            }
                        ),
                    }
                ]
            }
        seen_tool_sets.append([tool["function"]["name"] for tool in tools])
        return {
            "content": [
                {
                    "type": "text",
                    "text": "利用可能なskillを確認します。",
                },
                {
                    "type": "tool_use",
                    "id": "call-skill-search",
                    "name": "skill_search",
                    "input": {"query": ""},
                },
            ],
            "finish_reason": "tool_use",
        }

    runner = RumiProcessRunner(
        complete=complete,
        response_text=_response_text,
        error_kind=lambda exc: type(exc).__name__,
    )
    response = runner.run_review_chain(
        composite={},
        generator_member={"metadata": {}},
        reviewer_member={"metadata": {}},
        generator_model="demo/conversation-model",
        reviewer_model="demo/conversation-model",
        messages=[{"role": "user", "content": "Use the best skill"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "skill_search",
                    "description": "Search skills",
                    "parameters": {"type": "object"},
                },
            }
        ],
        params={"deepthink_enabled": True},
        context={"mode": "deepthink", "activity_event_callback": lambda event: None},
        process={
            "trace_id": "trace-tool",
            "deepthink_enabled": True,
            "events": [],
            "watchdog": {},
        },
        max_reviews=2,
    )

    assert response["finish_reason"] == "tool_use", json.dumps(
        {
            "review": response["metadata"]["rumi_process"]["review"],
            "seen_tool_sets": seen_tool_sets,
        },
        sort_keys=True,
    )
    assert response["content"][0]["text"] == "利用可能なskillを確認します。"
    assert response["content"][1]["name"] == "skill_search"
    assert seen_tool_sets == [["skill_search"]]
    process = response["metadata"]["rumi_process"]
    assert process["flow"]["status"] == "paused"
    assert process["review"]["reason"] == "tool_execution_requested"
