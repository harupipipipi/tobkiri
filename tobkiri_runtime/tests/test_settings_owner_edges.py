"""Edge coverage for threading the explicit settings owner through defaultspack.

Each test binds a recording ``SettingsOwnerPort`` (delegating to an isolated
``FrontendSettingsStore`` under ``tmp_path``) and asserts that the owner
reaches the settings store / executor boundary without ``RuntimeError`` from
``FrontendSettingsStore._require_owner``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


class RecordingOwner:
    """Minimal ``SettingsOwnerPort`` recording every owner-bound call.

    Delegates to the real isolated store so owner arrival is a real storage
    operation rather than a stub.
    """

    def __init__(self, path: Path) -> None:
        from ecosystem.tobkiri_ui_settings_pack.runtime.store import (
            FrontendSettingsStore,
        )

        self._delegate = FrontendSettingsStore(path)
        self.calls: list[str] = []

    def read(self, *, preserve_corrupt: bool = False) -> dict[str, Any]:
        self.calls.append("read")
        return self._delegate.read(preserve_corrupt=preserve_corrupt)

    def read_snapshot(self) -> dict[str, Any]:
        self.calls.append("read_snapshot")
        return self._delegate.read_snapshot()

    def compare_and_swap_document(
        self, document: Mapping[str, Any], *, expected_revision: int
    ) -> dict[str, Any]:
        self.calls.append("compare_and_swap_document")
        return self._delegate.compare_and_swap_document(
            document, expected_revision=expected_revision
        )

    def compare_and_swap_state(
        self,
        state_ref: str,
        document: Mapping[str, Any],
        result: Mapping[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append("compare_and_swap_state")
        return self._delegate.compare_and_swap_state(
            state_ref, document, result, **kwargs
        )

    def adopt_legacy_document(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("adopt_legacy_document")
        return self._delegate.adopt_legacy_document(**kwargs)


def _owner(tmp_path: Path) -> RecordingOwner:
    return RecordingOwner(tmp_path / "settings" / "frontend_settings.json")


def _external_event_dict(provider: str = "discord") -> dict[str, Any]:
    return {
        "provider": provider,
        "workspace": {"type": "workspace", "id": "w1"},
        "scope": {"type": "channel", "id": "c1"},
        "actor": {"type": "user", "id": "u1"},
        "conversation": {"type": "channel", "id": "c1"},
        "event": {"id": "e1", "type": "message"},
        "payload": {"content": "hello"},
        "verified": True,
    }


def _fake_executor(captured: list[Any]):
    class FakeExecutor:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured.append(kwargs.get("settings_owner"))

        def execute(self, tool_name: str, arguments: dict, context: dict) -> dict:
            return {"result": "ok", "is_error": False, "widget": None}

    return FakeExecutor


# ---------------------------------------------------------------------------
# Edge 1: UI catalog's ownerless FrontendRegistry
# ---------------------------------------------------------------------------


def test_ui_catalog_block_binds_explicit_settings_owner(tmp_path):
    import blocks.ui.catalog as catalog_block  # noqa: E402

    owner = _owner(tmp_path)
    result = catalog_block.run({}, {}, settings_owner=owner)

    assert result["status"] == "ok"
    assert "read" in owner.calls


def test_ui_catalog_block_binds_settings_owner_from_context(tmp_path):
    import blocks.ui.catalog as catalog_block  # noqa: E402

    owner = _owner(tmp_path)
    result = catalog_block.run({}, {"_settings_owner_port": owner})

    assert result["status"] == "ok"
    assert "read" in owner.calls


def test_ui_catalog_dispatcher_threads_context_owner(tmp_path):
    from domain.function_runtime.dispatcher import run_defaultspack_function  # noqa: E402

    owner = _owner(tmp_path)
    result = run_defaultspack_function(
        "ui_catalog", {}, {"_settings_owner_port": owner}
    )

    assert result["status"] == "ok"
    assert "read" in owner.calls


def test_ui_setup_route_handler_injects_captured_owner(tmp_path):
    import blocks.ui.setup as ui_setup  # noqa: E402

    owner = _owner(tmp_path)
    captured: dict[str, Any] = {}

    class FakeRegistry:
        def register(self, kind: str, spec: dict[str, Any], meta: dict | None = None):
            if spec.get("pattern") == "/api/ui/catalog":
                captured["handler"] = spec.get("handler")

    ui_setup.run(
        {"interface_registry": FakeRegistry(), "_settings_owner_port": owner}
    )

    handler = captured["handler"]
    result = handler({}, {})
    assert result["status"] == "ok"
    assert "read" in owner.calls


# ---------------------------------------------------------------------------
# Edge 2: external event pipeline callers (slack / discord / p2p)
# ---------------------------------------------------------------------------


def test_dispatch_external_event_forwards_settings_owner(tmp_path, monkeypatch):
    from domain.external import pipeline  # noqa: E402
    from domain.external.event import ExternalEvent  # noqa: E402
    from domain.external.input_profile import InputProfile  # noqa: E402

    owner = _owner(tmp_path)
    profile = InputProfile.from_dict(
        {
            "id": "test.edge2",
            "provider": "discord",
            "version": 1,
            "display_name": "Edge 2",
            "match": {"event_type": "message"},
            "input": {"role": "user", "content": "$.content"},
        }
    )

    class FakeRegistry:
        def get(self, _profile_id):
            return profile

    captured: dict[str, Any] = {}

    def fake_submit_input(envelope, context, *, settings_owner=None):
        captured["settings_owner"] = settings_owner
        return {"status": "ok", "assistant_text": "done"}

    monkeypatch.setattr(pipeline, "InputProfileRegistry", FakeRegistry)
    monkeypatch.setattr(pipeline, "submit_input", fake_submit_input)

    result = pipeline.dispatch_external_event(
        ExternalEvent.from_dict(_external_event_dict()),
        input_profile_id="test.edge2",
        settings_owner=owner,
    )

    assert result["status"] == "ok"
    assert captured["settings_owner"] is owner
    # TriggerDecisionService.from_profile reads frontend trigger config
    # through the bound owner port.
    assert "read_snapshot" in owner.calls


def test_slack_inbound_forwards_settings_owner(tmp_path, monkeypatch):
    from domain.integrations.slack import inbound as slack_inbound  # noqa: E402

    owner = _owner(tmp_path)
    captured: dict[str, Any] = {}

    def fake_dispatch(event, **kwargs):
        captured["settings_owner"] = kwargs.get("settings_owner")
        return {
            "status": "ok",
            "response_plan": {
                "metadata": {"response_action_plan": {"external_reply": False}}
            },
        }

    monkeypatch.setattr(
        slack_inbound, "_verify_slack", lambda headers, raw: {"ok": True, "verified": True, "reason": ""}
    )
    monkeypatch.setattr(slack_inbound, "dispatch_external_event", fake_dispatch)

    payload = {
        "type": "event_callback",
        "event": {"type": "message", "text": "hi", "user": "U1", "channel": "C1"},
    }
    result = slack_inbound.run(payload, {}, settings_owner=owner)

    assert result["status"] == "ok"
    assert captured["settings_owner"] is owner


def test_slack_inbound_reads_settings_owner_from_context(tmp_path, monkeypatch):
    from domain.integrations.slack import inbound as slack_inbound  # noqa: E402

    owner = _owner(tmp_path)
    captured: dict[str, Any] = {}

    def fake_dispatch(event, **kwargs):
        captured["settings_owner"] = kwargs.get("settings_owner")
        return {
            "status": "ok",
            "response_plan": {
                "metadata": {"response_action_plan": {"external_reply": False}}
            },
        }

    monkeypatch.setattr(
        slack_inbound, "_verify_slack", lambda headers, raw: {"ok": True, "verified": True, "reason": ""}
    )
    monkeypatch.setattr(slack_inbound, "dispatch_external_event", fake_dispatch)

    payload = {
        "type": "event_callback",
        "event": {"type": "message", "text": "hi", "user": "U1", "channel": "C1"},
    }
    result = slack_inbound.run(payload, {"_settings_owner_port": owner})

    assert result["status"] == "ok"
    assert captured["settings_owner"] is owner


def test_discord_inbound_interaction_forwards_settings_owner(tmp_path, monkeypatch):
    from domain.integrations.discord import inbound as discord_inbound  # noqa: E402

    owner = _owner(tmp_path)
    captured: dict[str, Any] = {}

    def fake_dispatch(event, **kwargs):
        captured["settings_owner"] = kwargs.get("settings_owner")
        return {"status": "ok", "assistant_text": "done"}

    monkeypatch.setattr(
        discord_inbound, "_verify_discord", lambda headers, raw: {"ok": True, "verified": True, "reason": ""}
    )
    monkeypatch.setattr(discord_inbound, "dispatch_external_event", fake_dispatch)

    payload = {
        "type": 2,  # DISCORD_APPLICATION_COMMAND
        "id": "i1",
        "channel_id": "c1",
        "guild_id": "g1",
        "member": {"user": {"id": "u1"}},
        "data": {"name": "ask", "options": [{"value": "ping"}]},
    }
    result = discord_inbound.run(payload, {}, settings_owner=owner)

    assert captured["settings_owner"] is owner
    assert result.get("type") in {4, 5}


def test_discord_inbound_message_create_forwards_settings_owner(tmp_path, monkeypatch):
    from domain.integrations.discord import inbound as discord_inbound  # noqa: E402

    owner = _owner(tmp_path)
    captured: dict[str, Any] = {}

    def fake_dispatch(event, **kwargs):
        captured["settings_owner"] = kwargs.get("settings_owner")
        return {
            "status": "ok",
            "response_plan": {
                "metadata": {"response_action_plan": {"external_reply": False}}
            },
        }

    monkeypatch.setattr(
        discord_inbound, "_verify_discord", lambda headers, raw: {"ok": True, "verified": True, "reason": ""}
    )
    monkeypatch.setattr(discord_inbound, "dispatch_external_event", fake_dispatch)

    payload = {
        "t": "MESSAGE_CREATE",
        "d": {
            "id": "m1",
            "channel_id": "c1",
            "guild_id": "g1",
            "content": "hello",
            "author": {"id": "u1"},
        },
    }
    result = discord_inbound.run(payload, {}, settings_owner=owner)

    assert result["status"] == "ok"
    assert captured["settings_owner"] is owner


def test_p2p_inbound_forwards_settings_owner(tmp_path, monkeypatch):
    import blocks.integrations.p2p as p2p_block  # noqa: E402

    owner = _owner(tmp_path)
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        p2p_block,
        "handle_inbound_envelope",
        lambda *args, **kwargs: {
            "status": "ok",
            "event": _external_event_dict(provider="p2p"),
        },
    )

    def fake_dispatch(event, **kwargs):
        captured["settings_owner"] = kwargs.get("settings_owner")
        return {"status": "ok"}

    monkeypatch.setattr(p2p_block, "dispatch_external_event", fake_dispatch)

    result = p2p_block.run(
        {"p2p": {"enabled": True, "store_path": str(tmp_path / "p2p")}},
        {},
        settings_owner=owner,
    )

    assert result["status"] == "ok"
    assert captured["settings_owner"] is owner


def test_p2p_inbound_reads_settings_owner_from_context(tmp_path, monkeypatch):
    import blocks.integrations.p2p as p2p_block  # noqa: E402

    owner = _owner(tmp_path)
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        p2p_block,
        "handle_inbound_envelope",
        lambda *args, **kwargs: {
            "status": "ok",
            "event": _external_event_dict(provider="p2p"),
        },
    )

    def fake_dispatch(event, **kwargs):
        captured["settings_owner"] = kwargs.get("settings_owner")
        return {"status": "ok"}

    monkeypatch.setattr(p2p_block, "dispatch_external_event", fake_dispatch)

    result = p2p_block.run(
        {"p2p": {"enabled": True, "store_path": str(tmp_path / "p2p")}},
        {"_settings_owner_port": owner},
    )

    assert result["status"] == "ok"
    assert captured["settings_owner"] is owner


# ---------------------------------------------------------------------------
# Edge 3: ambient input submission
# ---------------------------------------------------------------------------


def _ambient_router(tmp_path, monkeypatch):
    from domain.ambient.router import AmbientTriggerRouter  # noqa: E402
    from domain.ambient.store import AmbientStore  # noqa: E402
    from domain.ambient.audit import AmbientAuditLog  # noqa: E402

    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_AMBIENT_STORE_PATH",
        str(tmp_path / "ambient-state.json"),
    )
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_AMBIENT_AUDIT_PATH",
        str(tmp_path / "ambient-audit.jsonl"),
    )
    store = AmbientStore(tmp_path / "ambient-state.json")
    store.start_monitor()
    store.grant_permission("ambient.trigger.dispatch")
    return AmbientTriggerRouter(
        store=store, audit=AmbientAuditLog(tmp_path / "ambient-audit.jsonl")
    )


def test_ambient_submit_event_dispatches_with_settings_owner(tmp_path, monkeypatch):
    import domain.ambient.router as router_module  # noqa: E402

    owner = _owner(tmp_path)
    router = _ambient_router(tmp_path, monkeypatch)
    captured: dict[str, Any] = {}

    def fake_submit_input(envelope, context, *, settings_owner=None):
        captured["settings_owner"] = settings_owner
        return {"status": "ok", "conversation_id": "conv-edge"}

    monkeypatch.setattr(router_module, "submit_input", fake_submit_input)

    result = router.submit_event(
        {
            "source": "hook",
            "trigger": "external_hook",
            "mode": "ai_send",
            "input_text": "hello ambient",
        },
        {},
        settings_owner=owner,
    )

    assert result["status"] == "ok"
    assert captured["settings_owner"] is owner


def test_ambient_event_submit_block_forwards_settings_owner(tmp_path, monkeypatch):
    import blocks.ambient.event_submit as event_submit  # noqa: E402
    import domain.ambient.router as router_module  # noqa: E402

    owner = _owner(tmp_path)
    _ambient_router(tmp_path, monkeypatch)  # prepares isolated store state
    captured: dict[str, Any] = {}

    def fake_submit_input(envelope, context, *, settings_owner=None):
        captured["settings_owner"] = settings_owner
        return {"status": "ok", "conversation_id": "conv-edge"}

    monkeypatch.setattr(router_module, "submit_input", fake_submit_input)

    result = event_submit.run(
        {
            "source": "hook",
            "trigger": "external_hook",
            "mode": "ai_send",
            "input_text": "hello ambient",
        },
        {},
        settings_owner=owner,
    )

    assert result["status"] == "ok"
    assert captured["settings_owner"] is owner


def test_ambient_pending_approval_reuses_queued_settings_owner(tmp_path, monkeypatch):
    import blocks.ambient.approval as approval_block  # noqa: E402
    import domain.ambient.router as router_module  # noqa: E402
    from domain.ambient.store import AmbientStore  # noqa: E402

    owner = _owner(tmp_path)
    router = _ambient_router(tmp_path, monkeypatch)
    AmbientStore(tmp_path / "ambient-state.json").update_routing(
        {"ai_send_approval_required": True}
    )
    captured: dict[str, Any] = {}

    def fake_submit_input(envelope, context, *, settings_owner=None):
        captured["settings_owner"] = settings_owner
        return {"status": "ok"}

    monkeypatch.setattr(router_module, "submit_input", fake_submit_input)

    queued = router.submit_event(
        {
            "source": "hook",
            "trigger": "external_hook",
            "mode": "ai_send",
            "input_text": "queued ambient send",
        },
        {},
        settings_owner=owner,
    )
    assert queued["status"] == "approval_required"
    request_id = str(queued.get("approval_request_id") or "")
    assert request_id
    assert not captured  # nothing dispatched before approval

    # The approval block builds a fresh router; the queued owner must survive.
    result = approval_block.run(
        {"action": "approve", "request_id": request_id}, {}
    )

    assert result["status"] == "ok"
    assert captured["settings_owner"] is owner


# ---------------------------------------------------------------------------
# Edge 4: dispatch_external_message chat bridge
# ---------------------------------------------------------------------------


def test_dispatch_external_message_forwards_settings_owner(tmp_path, monkeypatch):
    from domain.integrations import chat_bridge  # noqa: E402

    owner = _owner(tmp_path)
    captured: dict[str, Any] = {}

    def fake_submit_input(envelope, context, *, settings_owner=None):
        captured["settings_owner"] = settings_owner
        return {"status": "ok", "assistant_text": "done"}

    monkeypatch.setattr(chat_bridge, "submit_input", fake_submit_input)

    result = chat_bridge.dispatch_external_message(
        provider="generic",
        text="hello",
        external_key="ext-1",
        title="External",
        event_id="evt-1",
        settings_owner=owner,
    )

    assert result["status"] == "ok"
    assert captured["settings_owner"] is owner


# ---------------------------------------------------------------------------
# Edge 5: subagent orchestration and direct callers
# ---------------------------------------------------------------------------


def test_run_subagent_threads_owner_to_model_settings(tmp_path):
    from domain.agent.subagent_orchestrator import run_subagent  # noqa: E402

    owner = _owner(tmp_path)

    def fake_call_handler(name, payload):
        return {"status": "ok", "data": {"content": '{"recommended_tools": []}'}}

    result = run_subagent(
        "tool_selector",
        {"candidate_tools": []},
        model="stub/default",
        call_handler=fake_call_handler,
        settings_owner=owner,
    )

    assert result["role_id"] == "tool_selector"
    # ModelRuntimeSettingsService resolved model settings through the owner.
    assert "read" in owner.calls


def test_run_subagent_compat_delegate_forwards_settings_owner(tmp_path, monkeypatch):
    import domain.agent.subagent_orchestrator as orchestrator  # noqa: E402
    import domain.input.dispatcher as input_dispatcher  # noqa: E402

    owner = _owner(tmp_path)
    captured: dict[str, Any] = {}

    def fake_dispatch_input(envelope, context, *, settings_owner=None):
        captured["settings_owner"] = settings_owner
        return {"status": "ok", "assistant_text": "delegated"}

    monkeypatch.setattr(input_dispatcher, "dispatch_input", fake_dispatch_input)

    result = orchestrator.run_subagent_compat(
        "delegate",
        {"task": "do something"},
        settings_owner=owner,
    )

    assert result["status"] == "ok"
    assert captured["settings_owner"] is owner


def test_run_subagent_block_forwards_settings_owner(tmp_path, monkeypatch):
    import blocks.agent.run_subagent as run_subagent_block  # noqa: E402

    owner = _owner(tmp_path)
    captured: dict[str, Any] = {}

    def fake_compat(role_id, payload, **kwargs):
        captured["settings_owner"] = kwargs.get("settings_owner")
        return {"status": "ok", "role_id": role_id}

    monkeypatch.setattr(run_subagent_block, "run_subagent_compat", fake_compat)

    result = run_subagent_block.run(
        {"role_id": "delegate", "payload": {"task": "work"}},
        {},
        settings_owner=owner,
    )

    assert result["status"] == "ok"
    assert captured["settings_owner"] is owner


def test_run_subagent_block_reads_owner_from_context(tmp_path, monkeypatch):
    import blocks.agent.run_subagent as run_subagent_block  # noqa: E402

    owner = _owner(tmp_path)
    captured: dict[str, Any] = {}

    def fake_compat(role_id, payload, **kwargs):
        captured["settings_owner"] = kwargs.get("settings_owner")
        return {"status": "ok", "role_id": role_id}

    monkeypatch.setattr(run_subagent_block, "run_subagent_compat", fake_compat)

    result = run_subagent_block.run(
        {"role_id": "delegate", "payload": {"task": "work"}},
        {"_settings_owner_port": owner},
    )

    assert result["status"] == "ok"
    assert captured["settings_owner"] is owner


def test_subagent_tool_backend_uses_context_settings_owner(tmp_path, monkeypatch):
    from domain.tool.ui_compiler_runtime.subagent_backend import (  # noqa: E402
        SubagentToolBackend,
    )
    from domain.ui_compiler.generation_models import UIAgentTask  # noqa: E402
    import domain.agent.subagent_orchestrator as orchestrator  # noqa: E402

    owner = _owner(tmp_path)
    captured: dict[str, Any] = {}

    def fake_compat(role_id, payload, **kwargs):
        captured["settings_owner"] = kwargs.get("settings_owner")
        out_dir = Path(str(payload.get("output_dir") or ""))
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "bundle.json").write_text("{}", encoding="utf-8")
        return {"status": "ok", "role_id": role_id}

    monkeypatch.setattr(orchestrator, "run_subagent_compat", fake_compat)

    task = UIAgentTask(
        task_id="t1",
        run_id="r1",
        node_id="n1",
        candidate_id="c1",
        kind="implementation",
        prompt="build it",
        output_dir=str(tmp_path / "out"),
    )
    result = SubagentToolBackend().run_task(
        task, {"_settings_owner_port": owner}
    )

    assert captured["settings_owner"] is owner
    assert result.status == "ok"


# ---------------------------------------------------------------------------
# Edge 6: ownerless ToolExecutor construction sites
# ---------------------------------------------------------------------------


def test_tool_broker_binds_settings_owner(tmp_path):
    from domain.tool.broker import ToolBroker  # noqa: E402

    owner = _owner(tmp_path)
    broker = ToolBroker(settings_owner=owner)

    assert broker._executor._settings_owner is owner


def test_tool_orchestrator_binds_executor_settings_owner(tmp_path, monkeypatch):
    import domain.tool.executor as executor_module  # noqa: E402
    from domain.tool_policy.orchestrator import ToolOrchestrator  # noqa: E402

    owner = _owner(tmp_path)
    constructed: list[Any] = []
    monkeypatch.setattr(
        executor_module, "ToolExecutor", _fake_executor(constructed)
    )

    class FakeRegistry:
        def get(self, name):
            return {"name": "memo_search", "handler": "fake:handler"}

        def list_tools(self):
            return [{"name": "memo_search", "handler": "fake:handler"}]

    orchestrator = ToolOrchestrator(
        registry=FakeRegistry(), settings_owner=owner
    )
    result = orchestrator.run("memo_search", {"query": "x"}, {})

    assert constructed == [owner]
    assert result["status"] == "ok"


def test_workflow_run_binds_executor_settings_owner(tmp_path, monkeypatch):
    import domain.tool.workflow_tools as workflow_tools  # noqa: E402

    owner = _owner(tmp_path)
    constructed: list[Any] = []
    monkeypatch.setattr(
        workflow_tools, "ToolExecutor", _fake_executor(constructed)
    )

    result = workflow_tools.workflow_run(
        {"steps": [{"id": "s1", "tool": "memo_search", "args": {}}]},
        {"artifact_root": str(tmp_path / "artifacts")},
        settings_owner=owner,
    )

    assert constructed == [owner]
    assert result["is_error"] is False


def test_workflow_retry_forwards_settings_owner(tmp_path, monkeypatch):
    import domain.tool.workflow_tools as workflow_tools  # noqa: E402

    owner = _owner(tmp_path)
    captured: dict[str, Any] = {}

    def fake_workflow_run(arguments, context=None, *, settings_owner=None):
        captured["settings_owner"] = settings_owner
        return {"status": "ok"}

    monkeypatch.setattr(workflow_tools, "workflow_run", fake_workflow_run)

    workflow_tools.workflow_retry(
        {"workflow_id": "wf-1"}, {}, settings_owner=owner
    )

    assert captured["settings_owner"] is owner


def test_send_prefocus_binds_executor_settings_owner(tmp_path, monkeypatch):
    import blocks.chat.send as send  # noqa: E402
    import domain.tool.executor as executor_module  # noqa: E402
    from domain.tool_policy.internal_context import (  # noqa: E402
        mark_tool_server_approval_context,
    )

    owner = _owner(tmp_path)
    constructed: list[Any] = []
    monkeypatch.setattr(
        executor_module, "ToolExecutor", _fake_executor(constructed)
    )
    monkeypatch.setattr(
        send,
        "connected_tool_names",
        lambda tools, runtime_profile=None, agent_id=None: {"browser_computer"},
    )
    monkeypatch.setattr(
        send,
        "build_tool_execution_context",
        lambda base_context, tool_name, connected: {
            "tool_name": tool_name,
            "connected_tools": sorted(connected),
        },
    )

    base_context = mark_tool_server_approval_context(
        {
            "user_requested_computer_use": True,
            "computer_use_target_app": "Google Chrome",
        }
    )
    send._prefocus_computer_use_target_window(
        available_tools=[{"name": "browser_computer"}],
        base_context=base_context,
        settings_owner=owner,
    )

    assert constructed == [owner]


def test_send_prefocus_reads_owner_from_base_context(tmp_path, monkeypatch):
    import blocks.chat.send as send  # noqa: E402
    import domain.tool.executor as executor_module  # noqa: E402
    from domain.tool_policy.internal_context import (  # noqa: E402
        mark_tool_server_approval_context,
    )

    owner = _owner(tmp_path)
    constructed: list[Any] = []
    monkeypatch.setattr(
        executor_module, "ToolExecutor", _fake_executor(constructed)
    )
    monkeypatch.setattr(
        send,
        "connected_tool_names",
        lambda tools, runtime_profile=None, agent_id=None: {"browser_computer"},
    )
    monkeypatch.setattr(
        send,
        "build_tool_execution_context",
        lambda base_context, tool_name, connected: {
            "tool_name": tool_name,
            "connected_tools": sorted(connected),
        },
    )

    base_context = mark_tool_server_approval_context(
        {
            "user_requested_computer_use": True,
            "computer_use_target_app": "Google Chrome",
            "_settings_owner_port": owner,
        }
    )
    send._prefocus_computer_use_target_window(
        available_tools=[{"name": "browser_computer"}],
        base_context=base_context,
    )

    assert constructed == [owner]


def test_run_request_prefocus_binds_prepared_settings_owner(tmp_path, monkeypatch):
    import domain.tool.executor as executor_module  # noqa: E402
    from domain.chat.run_request import (  # noqa: E402
        PreparedChatRun,
        prefocus_computer_use_target_window,
    )
    from domain.tool_policy.internal_context import (  # noqa: E402
        mark_tool_server_approval_context,
    )

    owner = _owner(tmp_path)
    constructed: list[Any] = []
    monkeypatch.setattr(
        executor_module, "ToolExecutor", _fake_executor(constructed)
    )

    prepared = PreparedChatRun(
        conversation_id="conv",
        conversation={},
        input_data={},
        request_id="run",
        content=[],
        metadata={},
        user_message={},
        model="stub/default",
        params={},
        request_context={
            "user_requested_computer_use": True,
            "computer_use_target_app": "Google Chrome",
        },
        tool_context=mark_tool_server_approval_context({}),
        standard_messages=[],
        user_text="",
        system_prompt="",
        enrich_info={},
        raw_tools=[],
        provider_tools=[],
        tools_called=[],
        connected_tool_names={"browser_computer"},
        call_handler=None,
        model_routing={},
        settings_owner=owner,
    )
    prefocus_computer_use_target_window(prepared)

    assert constructed == [owner]


def test_complete_with_tools_binds_executor_settings_owner(tmp_path, monkeypatch):
    import blocks.chat.send as send  # noqa: E402
    import domain.tool.executor as executor_module  # noqa: E402

    owner = _owner(tmp_path)
    constructed: list[Any] = []
    monkeypatch.setattr(
        executor_module, "ToolExecutor", _fake_executor(constructed)
    )

    responses = iter(
        [
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "memo_search",
                        "input": {"query": "x"},
                    }
                ],
                "finish_reason": "tool_use",
            },
            {
                "content": [{"type": "text", "text": "done"}],
                "finish_reason": "stop",
            },
        ]
    )
    monkeypatch.setattr(
        send,
        "_call_ai_complete_with_retry",
        lambda *args, **kwargs: next(responses),
    )

    result = send._complete_with_tools(
        "stub/default",
        [{"role": "user", "content": "hi"}],
        [{"name": "memo_search"}],
        {},
        None,
        {},
        settings_owner=owner,
    )

    assert constructed == [owner]
    assert result["content"] == [{"type": "text", "text": "done"}]


def test_agent_engine_execute_tool_binds_settings_owner(tmp_path, monkeypatch):
    import domain.tool_policy.orchestrator as orchestrator_module  # noqa: E402
    from domain.agent.engine import AgentEngine  # noqa: E402

    owner = _owner(tmp_path)
    captured: dict[str, Any] = {}

    class FakeOrchestrator:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["settings_owner"] = kwargs.get("settings_owner")

        def run(self, tool_name, arguments, context):
            return {"status": "ok", "data": {"tool_name": tool_name}}

    monkeypatch.setattr(
        orchestrator_module, "ToolOrchestrator", FakeOrchestrator
    )

    result = AgentEngine(settings_owner=owner)._execute_tool(
        "memo_search", {}, {}
    )

    assert captured["settings_owner"] is owner
    assert result["status"] == "ok"
