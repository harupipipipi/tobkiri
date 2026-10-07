"""Agent definition edits cannot rewrite settings already admitted to new runs."""
import json
from pathlib import Path

import pytest

from ecosystem.rumi_agent_state_store_pack.runtime.store import AgentStateStore


def apply(store, operation, **arguments):
    return store.apply(operation, {
        "expected_revision": store.snapshot("run")["revision"], **arguments,
    })


def begin(store, run_id="run.one", parent=""):
    return apply(store, "run.begin", run_id=run_id, idempotency_key=run_id,
                 agent_profile_id="default", conversation_id="conversation.one",
                 turn_id="turn.one", parent_run_id=parent)["run"]


def test_new_run_keeps_exact_owner_snapshot_after_edits_and_restart(tmp_path: Path):
    store = AgentStateStore("defaults", root=tmp_path)
    apply(store, "profile.upsert", profile={
        "id": "default", "system_prompt": "original", "model_profile_id": "model.one",
        "tools": ["tool.one"], "metadata": {"nested": ["original"]},
    })
    original = begin(store)
    apply(store, "profile.upsert", profile={
        "id": "default", "system_prompt": "replacement", "model_profile_id": "model.two",
        "tools": ["tool.two"], "metadata": {"nested": ["replacement"]},
    })
    restarted = AgentStateStore("defaults", root=tmp_path)
    assert begin(restarted) == original
    assert original["agent_definition_revision"] == 1
    assert original["agent_profile_snapshot"]["system_prompt"] == "original"
    assert original["agent_profile_snapshot"]["tools"] == ["tool.one"]
    assert begin(restarted, "run.two")["agent_definition_revision"] == 2
    original["agent_profile_snapshot"]["metadata"]["nested"].append("caller edit")
    assert restarted.get("run", "run.one")["agent_profile_snapshot"]["metadata"] == {"nested": ["original"]}


def test_parent_subagent_policy_is_pinned_in_same_owner_transaction(tmp_path: Path):
    store = AgentStateStore("defaults", root=tmp_path)
    begin(store, "parent.denies")
    apply(store, "profile.upsert", profile={"id": "default", "allow_subagents": True, "max_children": 2})
    with pytest.raises(PermissionError, match="denies subagents"):
        begin(store, "child.denied", "parent.denies")
    assert store.get("run", "child.denied") is None
    begin(store, "parent.allows")
    apply(store, "profile.upsert", profile={"id": "default", "allow_subagents": False})
    child = begin(store, "child.allowed", "parent.allows")
    assert child["parent_run_id"] == "parent.allows"
    # Definition policy is not a Host grant; runtime authorization still applies.


def test_legacy_runs_are_not_assigned_fabricated_historical_snapshots(tmp_path: Path):
    store = AgentStateStore("defaults", root=tmp_path)
    begin(store)
    state = store._read()
    state["runs"]["run.one"].pop("agent_profile_snapshot")
    state["runs"]["run.one"].pop("agent_definition_revision")
    store._write(state)
    apply(store, "profile.upsert", profile={"id": "default", "system_prompt": "new"})
    assert "agent_profile_snapshot" not in begin(store)


def test_runtime_uses_admitted_snapshot_across_concurrent_definition_edits(tmp_path: Path, monkeypatch):
    from ecosystem.rumi_agent_runtime_service_pack.runtime import runtime

    store = AgentStateStore("defaults", root=tmp_path)
    def replace_definition(name):
        apply(store, "profile.upsert", profile={
            "id": "default", "system_prompt": name, "model_profile_id": f"model.{name}",
            "tools": [f"tool.{name}"], "context_token_budget": 1024,
        })
    replace_definition("before")
    captured = {}
    class Client:
        def invoke(self, contract, operation, payload):
            if contract == runtime.AUTHORITY:
                return {"authorized": True, "receipt": "fixture-only"}
            if contract == runtime.AGENT_STATE_RESOURCE:
                if operation == "profile.get":
                    old = store.get("profile", payload["agent_profile_id"])
                    replace_definition("admitted")
                    return old
                if operation == "run.get":
                    return store.get("run", payload["run_id"])
                if operation == "run.list":
                    return store.snapshot("run")
            if contract == runtime.AGENT_STATE_ACTION:
                return store.apply(operation, payload)
            if contract == runtime.CONTEXT:
                captured["context"] = payload
                replace_definition("later")
                return {"sections": [{"items": payload["system_items"]}]}
            if contract == runtime.AI_GENERATE:
                captured["generation"] = payload
                return {"output": {"text": "done"}}
            if contract == runtime.MESSAGE_ACTION:
                return {"message": {"id": "message.one"}}
            raise AssertionError((contract, operation))
    service = runtime.AgentRuntime(Client(), "defaults")
    monkeypatch.setattr(service, "_conversation", lambda _: {
        "id": "conversation.one", "conversation_revision": 1,
        "messages": [{"role": "user", "content": "hello"}],
    })
    monkeypatch.setattr(service, "_turn", lambda *_: {"id": "turn.one", "status": "queued"})
    monkeypatch.setattr(service, "_turn_transition", lambda turn, status, details: {**turn, "status": status})
    result = service._execute({
        "run_id": "run.one", "idempotency_key": "run.one", "agent_profile_id": "default",
        "conversation_id": "conversation.one", "conversation_revision": 1,
        "turn_id": "turn.one", "parent_run_id": "",
    })
    assert result["status"] == "completed", result
    assert captured["context"]["system_items"] == [{"role": "system", "content": "admitted"}]
    assert captured["generation"]["model_profile_id"] == "model.admitted"
    assert captured["generation"]["tools"] == [{"name": "tool.admitted"}]
    assert store.get("profile", "default")["system_prompt"] == "later"
    assert store.get("run", "run.one")["agent_profile_snapshot"]["system_prompt"] == "admitted"


def test_parent_limit_remains_pinned_and_caller_cannot_supply_snapshot(tmp_path: Path):
    store = AgentStateStore("defaults", root=tmp_path)
    apply(store, "profile.upsert", profile={"id": "default", "allow_subagents": True, "max_children": 1})
    parent = begin(store, "parent.one")
    apply(store, "profile.upsert", profile={"id": "default", "allow_subagents": True, "max_children": 10})
    begin(store, "child.one", "parent.one")
    with pytest.raises(RuntimeError, match="child limit"):
        begin(store, "child.two", "parent.one")
    forged = apply(store, "run.begin", run_id="run.forged", idempotency_key="run.forged",
                   agent_profile_id="default", conversation_id="conversation.one",
                   turn_id="turn.one", parent_run_id="",
                   agent_profile_snapshot=parent["agent_profile_snapshot"], agent_definition_revision=-1)["run"]
    assert forged["agent_profile_snapshot"]["max_children"] == 10
    assert forged["agent_definition_revision"] == 2


@pytest.mark.parametrize("bad", [None, {}, {"id": "other"}, "not a snapshot"])
def test_malformed_parent_snapshot_never_falls_back_to_current_definition(tmp_path: Path, bad):
    store = AgentStateStore("defaults", root=tmp_path)
    apply(store, "profile.upsert", profile={"id": "default", "allow_subagents": True, "max_children": 2})
    begin(store, "parent.one")
    state = store._read()
    state["runs"]["parent.one"]["agent_profile_snapshot"] = bad
    store._write(state)
    with pytest.raises(ValueError, match="snapshot is invalid"):
        begin(store, "child.one", "parent.one")
    assert "child.one" not in json.loads(store.path.read_text())["runs"]


@pytest.mark.parametrize("change", ["missing_snapshot", "missing_revision", "revision", "policy_type", "limit"])
def test_corrupt_new_run_pin_cannot_be_reinterpreted_as_legacy(tmp_path: Path, change):
    store = AgentStateStore("defaults", root=tmp_path)
    begin(store)
    state = store._read()
    run = state["runs"]["run.one"]
    if change == "missing_snapshot":
        run.pop("agent_profile_snapshot")
    elif change == "missing_revision":
        run.pop("agent_definition_revision")
    elif change == "revision":
        run["agent_definition_revision"] += 1
    elif change == "policy_type":
        run["agent_profile_snapshot"]["allow_subagents"] = "false"
    else:
        run["agent_profile_snapshot"]["max_children"] = 999
    store._write(state)
    with pytest.raises(ValueError, match="snapshot is invalid"):
        store.get("run", "run.one")


def test_waiting_run_resumes_with_original_definition_and_step_budget(tmp_path: Path, monkeypatch):
    from ecosystem.rumi_agent_runtime_service_pack.runtime import runtime

    store = AgentStateStore("defaults", root=tmp_path)
    apply(store, "profile.upsert", profile={"id": "default", "system_prompt": "old",
          "tools": ["tool.old"], "model_profile_id": "model.old", "max_steps": 3})
    generated, contexts = [], []
    approved = False
    class Client:
        def invoke(self, contract, operation, payload):
            if contract == runtime.AUTHORITY:
                return {"authorized": True, "receipt": "fixture-only"}
            if contract == runtime.AGENT_STATE_RESOURCE:
                if operation == "profile.get":
                    return store.get("profile", payload["agent_profile_id"])
                if operation == "run.get":
                    return store.get("run", payload["run_id"])
                if operation == "run.list":
                    return store.snapshot("run")
            if contract == runtime.AGENT_STATE_ACTION:
                return store.apply(operation, payload)
            if contract == runtime.CONTEXT:
                contexts.append(payload)
                return {"sections": [{"items": payload["system_items"]}]}
            if contract == runtime.AI_GENERATE:
                generated.append(payload)
                if len(generated) == 1:
                    return {"tool_intents": [{"intent_id": "intent.one", "operation": "tool.old", "arguments": {}}]}
                return {"output": {"text": "done"}}
            if contract == runtime.TOOL_INVOKE:
                return {"status": "ok"} if approved else {"approval": {"request_id": "approval.fixture"}}
            if contract == runtime.MESSAGE_ACTION:
                return {"message": {"id": "message.one"}}
            raise AssertionError((contract, operation))
    service = runtime.AgentRuntime(Client(), "defaults")
    monkeypatch.setattr(service, "_conversation", lambda _: {
        "id": "conversation.one", "conversation_revision": 1, "messages": [{"role": "user", "content": "hello"}],
    })
    monkeypatch.setattr(service, "_turn", lambda *_: {"id": "turn.one", "status": "queued"})
    monkeypatch.setattr(service, "_turn_transition", lambda turn, status, details: {**turn, "status": status})
    first = service._execute({"run_id": "run.one", "idempotency_key": "run.one", "agent_profile_id": "default",
                             "conversation_id": "conversation.one", "conversation_revision": 1, "turn_id": "turn.one", "parent_run_id": ""})
    assert first["status"] == "waiting", first
    apply(store, "profile.upsert", profile={"id": "default", "system_prompt": "new",
          "tools": ["tool.new"], "model_profile_id": "model.new", "max_steps": 1})
    approved = True
    result = service._execute(service._resume_arguments({"run_id": "run.one", "tool_approvals": {}}))
    assert result["status"] == "completed", result
    assert len(generated) == 2
    assert generated[-1]["model_profile_id"] == "model.old"
    assert generated[-1]["tools"] == [{"name": "tool.old"}]
    assert contexts[-1]["system_items"] == [{"role": "system", "content": "old"}]


def test_new_run_from_legacy_definition_pins_revision_zero(tmp_path: Path):
    store = AgentStateStore("defaults", root=tmp_path)
    state = store._read()
    state["profiles"]["default"].pop("definition_revision")
    store._write(state)
    run = begin(store)
    assert run["agent_definition_revision"] == 0
    assert "definition_revision" not in run["agent_profile_snapshot"]
    assert store.get("run", "run.one") == run


@pytest.mark.parametrize("key", ["idempotency_key", "agent_profile_id", "conversation_id", "turn_id", "parent_run_id"])
def test_run_replay_cannot_rebind_definition_or_conversation_identity(tmp_path: Path, key):
    store = AgentStateStore("defaults", root=tmp_path)
    begin(store)
    before = store.path.read_bytes()
    arguments = {"run_id": "run.one", "idempotency_key": "run.one", "agent_profile_id": "default",
                 "conversation_id": "conversation.one", "turn_id": "turn.one", "parent_run_id": ""}
    arguments[key] = "another.identity"
    with pytest.raises(RuntimeError, match="already bound"):
        apply(store, "run.begin", **arguments)
    assert store.path.read_bytes() == before
