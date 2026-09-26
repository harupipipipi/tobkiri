"""Provider-neutral agent planning, tool loop, and lifecycle runtime."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
import uuid
from typing import Any, Callable, Mapping

AUTHORITY = "rumi.service.host.authorize.v1"
AGENT_STATE_RESOURCE = "rumi.resource.agent.state.v1"
AGENT_STATE_ACTION = "rumi.action.agent.state.v1"
CONVERSATION_RESOURCE = "rumi.resource.conversation.v1"
MESSAGE_ACTION = "rumi.action.message.manage.v1"
TURN_RESOURCE = "rumi.resource.turn.v1"
TURN_ACTION = "rumi.action.turn.lifecycle.v1"
CONTEXT = "rumi.service.context.v1"
AI_GENERATE = "rumi.service.ai.generate.v1"
TOOL_INVOKE = "rumi.service.tool.invoke.v1"
SERVICE_PACK_ID = "rumi_agent_runtime_service_pack"
STATE_PACK_ID = "rumi_agent_state_store_pack"
_EPHEMERAL_TOOL_PAYLOADS: dict[str, tuple[float, dict[str, Any]]] = {}
_EPHEMERAL_TTL_SECONDS = 10 * 60


class AgentRuntime:
    """Execute bounded agent runs only through selected global contracts."""

    def __init__(self, client: Any, profile_id: str) -> None:
        self.client = client
        self.profile_id = profile_id
        self.lock = threading.RLock()
        self.active: set[str] = set()

    def status(self) -> dict[str, Any]:
        """Return process state without owning canonical agent run data."""

        with self.lock:
            return {
                "profile_id": self.profile_id,
                "active_run_ids": sorted(self.active),
                "state_owner": STATE_PACK_ID,
            }

    def control(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Apply one receipt-gated agent control operation."""

        arguments = _control_arguments(name, payload)
        self._redeem(payload, name, arguments)
        if name == "resume":
            arguments = self._resume_arguments(arguments)
        if name in {"execute", "resume", "spawn"}:
            return self._execute(arguments)
        if name == "cancel":
            return self._cancel(arguments)
        if name == "steer":
            return self._steer(arguments)
        return self._handoff(arguments)

    def _execute(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        run_id = str(arguments["run_id"])
        with self.lock:
            if run_id in self.active:
                return {"status": "running", "run_id": run_id, "deduplicated": True}
            self.active.add(run_id)
        turn: dict[str, Any] | None = None
        try:
            profile = self._profile(str(arguments["agent_profile_id"]))
            conversation = self._conversation(str(arguments["conversation_id"]))
            expected_conversation_revision = int(arguments["conversation_revision"])
            if (
                int(conversation["conversation_revision"])
                != expected_conversation_revision
            ):
                raise RuntimeError("agent conversation revision is stale")
            turn = self._turn(arguments, conversation)
            began = self._state_action(
                "run.begin",
                {
                    "run_id": run_id,
                    "idempotency_key": arguments["idempotency_key"],
                    "agent_profile_id": profile["id"],
                    "conversation_id": conversation["id"],
                    "turn_id": turn["id"],
                    "parent_run_id": arguments["parent_run_id"],
                },
            )
            run = began["run"]
            if run["status"] in {"completed", "failed", "cancelled"}:
                return {"status": run["status"], "run": run, "deduplicated": True}
            run = self._transition(run_id, "planning", 0, {})["run"]
            context = self.client.invoke(
                CONTEXT,
                "materialize",
                {
                    "profile_id": self.profile_id,
                    "conversation_id": conversation["id"],
                    "conversation_revision": expected_conversation_revision,
                    "query": _latest_user_text(conversation),
                    "token_budget": int(profile["context_token_budget"]),
                    "system_items": [
                        {"role": "system", "content": profile["system_prompt"]}
                    ]
                    if profile["system_prompt"]
                    else [],
                },
            )
            messages = _context_messages(context)
            run = self._transition(run_id, "running", 0, {})["run"]
            turn = self._turn_transition(turn, "running", {})
            outcome = self._tool_loop(
                run,
                profile,
                messages,
                conversation,
                dict(arguments.get("tool_approvals") or {}),
                list(arguments.get("pending_tool_intents") or []),
                list(arguments.get("prior_tool_results") or []),
            )
            if outcome["status"] == "waiting":
                payload_receipt = _stash_ephemeral_tool_payload(
                    {
                        "pending_tool_intents": outcome["pending_tool_intents"],
                        "tool_results": outcome["tool_results"],
                    }
                )
                waiting = self._transition(
                    run_id,
                    "waiting",
                    int(outcome["step"]),
                    {
                        "tool_payload_receipt": payload_receipt,
                        "pending_count": len(outcome["pending_tool_intents"]),
                        "tool_result_count": len(outcome["tool_results"]),
                    },
                )["run"]
                turn = self._turn_transition(
                    turn,
                    "waiting",
                    {"reason": "tool_approval_required"},
                )
                return {"status": "waiting", "run": waiting, **outcome}
            if outcome["status"] == "cancelled":
                return {"status": "cancelled", "run": self._run(run_id)}
            if outcome["status"] != "completed":
                raise RuntimeError(str(outcome.get("error") or "agent run failed"))
            appended = self.client.invoke(
                MESSAGE_ACTION,
                "append",
                {
                    "profile_id": self.profile_id,
                    "conversation_id": conversation["id"],
                    "expected_conversation_revision": expected_conversation_revision,
                    "message": {
                        "id": f"agent-{_short(run_id)}",
                        "role": "assistant",
                        "content": outcome["content"],
                        "metadata": {
                            "agent_run_id": run_id,
                            "tool_result_receipts": [
                                _hashed_receipt(item)
                                for item in outcome["tool_results"]
                            ],
                            "tool_result_count": len(outcome["tool_results"]),
                        },
                    },
                },
            )
            completed = self._transition(
                run_id,
                "completed",
                int(outcome["step"]),
                {
                    "result_reference": {
                        "conversation_id": conversation["id"],
                        "message_id": appended["message"]["id"],
                    },
                    "terminal_projection": {
                        "turn_id": turn["id"],
                        "status": "completed",
                    },
                },
            )["run"]
            reconciliation_required = False
            try:
                turn = self._turn_transition(
                    turn,
                    "completed",
                    {"result_reference": completed["result_reference"]},
                )
                completed = self._state_action(
                    "run.reconcile",
                    {
                        "run_id": run_id,
                        "projection_receipt": _hashed_receipt(
                            {
                                "turn_id": turn["id"],
                                "status": turn["status"],
                                "result_reference": completed[
                                    "result_reference"
                                ],
                            }
                        ),
                    },
                )["run"]
            except Exception:
                # Agent State is the terminal authority. Turn state is a
                # projection and is reconciled without rewriting the terminal
                # run/event pair.
                reconciliation_required = True
            else:
                reconciliation_required = bool(
                    completed.get("reconciliation_required")
                )
            return {
                "status": "completed",
                "run": completed,
                "turn": turn,
                "reconciliation_required": reconciliation_required,
                "message": appended["message"],
                "tool_results": outcome["tool_results"],
            }
        except Exception as exc:
            self._fail(run_id, turn, str(exc))
            return {"status": "failed", "run_id": run_id, "error": str(exc)}
        finally:
            with self.lock:
                self.active.discard(run_id)

    def _tool_loop(
        self,
        run: Mapping[str, Any],
        profile: Mapping[str, Any],
        messages: list[dict[str, Any]],
        conversation: Mapping[str, Any],
        tool_approvals: Mapping[str, Any],
        pending_tool_intents: list[Any],
        prior_tool_results: list[Any],
    ) -> dict[str, Any]:
        tool_results = [
            dict(item) for item in prior_tool_results if isinstance(item, Mapping)
        ]
        for item in tool_results:
            intent = item.get("intent")
            intent = intent if isinstance(intent, Mapping) else {}
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(intent.get("intent_id") or ""),
                    "content": json.dumps(item.get("result"), ensure_ascii=False),
                }
            )
        max_steps = int(profile["max_steps"])
        if pending_tool_intents:
            still_pending = []
            for pending in pending_tool_intents:
                pending = pending if isinstance(pending, Mapping) else {}
                intent = pending.get("intent")
                if not isinstance(intent, Mapping):
                    continue
                result = self._invoke_tool(run, intent, tool_approvals)
                if isinstance(result, Mapping) and result.get("approval"):
                    still_pending.append(
                        {"intent": dict(intent), "approval": result["approval"]}
                    )
                    continue
                item = {"intent": dict(intent), "result": _bounded(result)}
                tool_results.append(item)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(intent.get("intent_id") or ""),
                        "content": json.dumps(item["result"], ensure_ascii=False),
                    }
                )
            if still_pending:
                return {
                    "status": "waiting",
                    "step": int(run.get("step") or 1),
                    "pending_tool_intents": still_pending,
                    "tool_results": tool_results,
                }
        start_step = max(1, int(run.get("step") or 0) + bool(pending_tool_intents))
        for step in range(start_step, max_steps + 1):
            current = self._run(str(run["id"]))
            if current.get("cancel_requested") or current.get("status") == "cancelled":
                return {"status": "cancelled", "error": "agent run was cancelled"}
            response = self.client.invoke(
                AI_GENERATE,
                "generate",
                {
                    "request_id": f"{run['id']}:{step}",
                    "idempotency_key": f"{run['id']}:{step}",
                    "conversation_id": conversation["id"],
                    "model_profile_id": profile.get("model_profile_id") or None,
                    "messages": messages,
                    "tools": [{"name": item} for item in profile.get("tools") or []],
                    "requirements": {
                        "modalities": ["text"],
                        "tool_calling": bool(profile.get("tools")),
                        "request_surface": "agent",
                    },
                    "allow_failover": not bool(tool_results),
                },
            )
            intents = (
                response.get("tool_intents")
                if isinstance(response, Mapping)
                else []
            )
            intents = intents if isinstance(intents, list) else []
            if not intents:
                return {
                    "status": "completed",
                    "step": step,
                    "content": _assistant_content(response),
                    "tool_results": tool_results,
                }
            pending = []
            for intent in intents:
                if not isinstance(intent, Mapping):
                    continue
                result = self._invoke_tool(run, intent, tool_approvals)
                if isinstance(result, Mapping) and result.get("approval"):
                    pending.append(
                        {"intent": dict(intent), "approval": result["approval"]}
                    )
                    continue
                item = {"intent": dict(intent), "result": _bounded(result)}
                tool_results.append(item)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(intent.get("intent_id") or ""),
                        "content": json.dumps(item["result"], ensure_ascii=False),
                    }
                )
            if pending:
                return {
                    "status": "waiting",
                    "step": step,
                    "pending_tool_intents": pending,
                    "tool_results": tool_results,
                }
        return {"status": "failed", "error": "agent step limit reached"}

    def _invoke_tool(
        self,
        run: Mapping[str, Any],
        intent: Mapping[str, Any],
        tool_approvals: Mapping[str, Any],
    ) -> Any:
        approval = tool_approvals.get(str(intent.get("intent_id") or ""))
        approval = (
            dict(approval)
            if isinstance(approval, Mapping)
            else {"approval_token": str(approval or "")}
        )
        tool_payload = {
                "tool_id": str(intent.get("operation") or ""),
                "tool_call_id": str(intent.get("intent_id") or uuid.uuid4()),
                "caller_id": f"agent:{run['id']}",
                "arguments": dict(intent.get("arguments") or {}),
                "deadline": time.time() + 60,
                "approval_token": approval.get("approval_token"),
                "approval_request_id": approval.get("approval_request_id"),
            }
        executor_token = secrets.token_urlsafe(32)
        effect_receipt = _hashed_receipt(tool_payload)
        self._state_action(
            "run.effect.begin",
            {
                "run_id": run["id"],
                "executor_token": executor_token,
                "effect_receipt": effect_receipt,
            },
        )
        try:
            return self.client.invoke(TOOL_INVOKE, "invoke", tool_payload)
        finally:
            self._state_action(
                "run.effect.end",
                {
                    "run_id": run["id"],
                    "executor_token": executor_token,
                    "effect_receipt": effect_receipt,
                },
            )

    def _cancel(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        result = self._state_action(
            "run.cancel",
            {"run_id": arguments["run_id"], "reason": arguments["reason"]},
        )
        run = result["run"]
        if result.get("too_late"):
            return {
                "status": "too_late",
                "cancel_requested": True,
                "effect_committing": True,
                **result,
            }
        turn = self._turn_get(str(run.get("turn_id") or ""))
        if turn and turn["status"] not in {"completed", "failed", "cancelled"}:
            self._turn_transition(turn, "cancelled", {"reason": arguments["reason"]})
        return {"status": "cancelled", **result}

    def _resume_arguments(
        self,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        run = self._run(str(arguments["run_id"]))
        if run["status"] != "waiting":
            raise RuntimeError("only a waiting agent run can be resumed")
        conversation = self._conversation(str(run["conversation_id"]))
        waiting = next(
            (
                item
                for item in reversed(list(run.get("events") or []))
                if isinstance(item, Mapping) and item.get("name") == "agent.run.waiting"
            ),
            {},
        )
        details = waiting.get("details") if isinstance(waiting, Mapping) else {}
        details = details if isinstance(details, Mapping) else {}
        ephemeral = _load_ephemeral_tool_payload(
            str(details.get("tool_payload_receipt") or "")
        )
        return {
            "run_id": run["id"],
            "idempotency_key": run["idempotency_key"],
            "agent_profile_id": run["agent_profile_id"],
            "conversation_id": run["conversation_id"],
            "conversation_revision": conversation["conversation_revision"],
            "turn_id": run["turn_id"],
            "parent_run_id": run["parent_run_id"],
            "tool_approvals": dict(arguments["tool_approvals"]),
            "pending_tool_intents": list(
                ephemeral.get("pending_tool_intents") or []
            ),
            "prior_tool_results": list(ephemeral.get("tool_results") or []),
        }

    def _steer(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        result = self._state_action(
            "run.steer",
            {"run_id": arguments["run_id"], "guidance": arguments["guidance"]},
        )
        run = result["run"]
        turn = self._turn_get(str(run.get("turn_id") or ""))
        if turn:
            self.client.invoke(
                TURN_ACTION,
                "steer",
                {
                    "profile_id": self.profile_id,
                    "turn_id": turn["id"],
                    "expected_revision": turn["revision"],
                    "guidance": arguments["guidance"],
                },
            )
        return {"status": "ok", **result}

    def _handoff(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        result = self._state_action(
            "run.handoff",
            {"run_id": arguments["run_id"], "target": arguments["target"]},
        )
        run = result["run"]
        turn = self._turn_get(str(run.get("turn_id") or ""))
        if turn:
            self.client.invoke(
                TURN_ACTION,
                "handoff",
                {
                    "profile_id": self.profile_id,
                    "turn_id": turn["id"],
                    "expected_revision": turn["revision"],
                    "target": arguments["target"],
                },
            )
        return {"status": "ok", **result}

    def _profile(self, agent_profile_id: str) -> dict[str, Any]:
        value = self.client.invoke(
            AGENT_STATE_RESOURCE,
            "profile.get",
            {"profile_id": self.profile_id, "agent_profile_id": agent_profile_id},
        )
        if not isinstance(value, Mapping):
            raise KeyError("agent profile is unknown")
        return dict(value)

    def _conversation(self, conversation_id: str) -> dict[str, Any]:
        value = self.client.invoke(
            CONVERSATION_RESOURCE,
            "get",
            {"profile_id": self.profile_id, "conversation_id": conversation_id},
        )
        if not isinstance(value, Mapping):
            raise KeyError("agent conversation is unknown")
        return dict(value)

    def _run(self, run_id: str) -> dict[str, Any]:
        value = self.client.invoke(
            AGENT_STATE_RESOURCE,
            "run.get",
            {"profile_id": self.profile_id, "run_id": run_id},
        )
        if not isinstance(value, Mapping):
            raise KeyError("agent run is unknown")
        return dict(value)

    def _state_action(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = self.client.invoke(
            AGENT_STATE_RESOURCE,
            "run.list",
            {"profile_id": self.profile_id},
        )
        exact = {"expected_revision": int(state.get("revision") or 0), **arguments}
        scope = {
            "service_pack_id": STATE_PACK_ID,
            "operation": f"agent.state.{name}",
            "authority": "agent.state.manage",
            "caller_id": "agent.runtime",
            "caller_pack_id": SERVICE_PACK_ID,
            "caller_function_id": f"agent.runtime.{name}",
            "profile_id": self.profile_id,
            "workspace_id": "",
            "session_id": "",
            "arguments": exact,
            "approval_required": False,
        }
        issued = self.client.invoke(AUTHORITY, "authorize", scope)
        if not issued.get("authorized"):
            raise PermissionError(str(issued.get("reason") or "agent state denied"))
        return self.client.invoke(
            AGENT_STATE_ACTION,
            name,
            {
                **exact,
                "profile_id": self.profile_id,
                "authority_receipt": str(issued.get("receipt") or ""),
                "caller_id": scope["caller_id"],
                "caller_pack_id": SERVICE_PACK_ID,
                "caller_function_id": scope["caller_function_id"],
                "session_id": "",
            },
        )

    def _transition(
        self,
        run_id: str,
        status: str,
        step: int,
        details: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._state_action(
            "run.transition",
            {"run_id": run_id, "status": status, "step": step, "details": details},
        )

    def _turn(
        self,
        arguments: Mapping[str, Any],
        conversation: Mapping[str, Any],
    ) -> dict[str, Any]:
        turn_id = str(arguments.get("turn_id") or "")
        if turn_id:
            turn = self._turn_get(turn_id)
            if turn is None:
                raise KeyError("agent turn is unknown")
            return turn
        return self.client.invoke(
            TURN_ACTION,
            "begin",
            {
                "profile_id": self.profile_id,
                "turn_id": str(uuid.uuid4()),
                "request_id": arguments["idempotency_key"],
                "conversation_id": conversation["id"],
                "conversation_revision": conversation["conversation_revision"],
            },
        )

    def _turn_get(self, turn_id: str) -> dict[str, Any] | None:
        if not turn_id:
            return None
        value = self.client.invoke(
            TURN_RESOURCE,
            "get",
            {"profile_id": self.profile_id, "turn_id": turn_id},
        )
        return dict(value) if isinstance(value, Mapping) else None

    def _turn_transition(
        self,
        turn: Mapping[str, Any],
        status: str,
        details: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self.client.invoke(
            TURN_ACTION,
            "transition",
            {
                "profile_id": self.profile_id,
                "turn_id": turn["id"],
                "expected_revision": turn["revision"],
                "status": status,
                "details": dict(details),
            },
        )

    def _fail(
        self,
        run_id: str,
        turn: Mapping[str, Any] | None,
        message: str,
    ) -> None:
        try:
            run = self._run(run_id)
            if run["status"] not in {"completed", "failed", "cancelled"}:
                self._transition(run_id, "failed", int(run["step"]), {"error": message})
        except Exception:
            pass
        if turn and turn.get("status") not in {"completed", "failed", "cancelled"}:
            try:
                current = self._turn_get(str(turn["id"]))
                if current and current["status"] not in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    self._turn_transition(current, "failed", {"error": message[:1000]})
            except Exception:
                pass

    def _redeem(
        self,
        payload: Mapping[str, Any],
        name: str,
        arguments: Mapping[str, Any],
    ) -> None:
        result = self.client.invoke(
            AUTHORITY,
            "redeem",
            {
                "receipt": str(payload.get("authority_receipt") or ""),
                "service_pack_id": SERVICE_PACK_ID,
                "operation": f"agent.runtime.{name}",
                "authority": "agent.run.control",
                "caller_id": str(payload.get("caller_id") or ""),
                "caller_pack_id": str(payload.get("caller_pack_id") or ""),
                "caller_function_id": str(payload.get("caller_function_id") or ""),
                "profile_id": self.profile_id,
                "workspace_id": "",
                "session_id": str(payload.get("session_id") or ""),
                "arguments": dict(arguments),
            },
        )
        if not result.get("authorized"):
            raise PermissionError(str(result.get("reason") or "agent control denied"))


_RUNTIMES: dict[str, AgentRuntime] = {}
_LOCK = threading.Lock()


def create_agent_runtime_resource(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create agent runtime process status operations."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name != "status":
            raise ValueError(f"unknown agent runtime resource operation: {name}")
        return _runtime(client, payload).status()

    return operation


def create_agent_control(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated agent runtime controls."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return _runtime(client, payload).control(name, payload)

    return operation


def create_agent_job_adapter(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create the `agent.turn` global job adapter."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        job_payload = payload.get("payload")
        job_payload = dict(job_payload) if isinstance(job_payload, Mapping) else {}
        key = str(payload.get("idempotency_key") or "")
        run_id = f"job-{_short(key)}"
        runtime = _runtime(client, payload)
        if name == "cancel":
            arguments = {"run_id": run_id, "reason": "job_cancelled"}
            return _internal_control(runtime, client, "cancel", arguments)
        if name != "dispatch":
            raise ValueError(f"unknown agent job adapter operation: {name}")
        arguments = {
            "run_id": run_id,
            "idempotency_key": key,
            "agent_profile_id": str(job_payload.get("agent_profile_id") or "default"),
            "conversation_id": str(job_payload.get("conversation_id") or ""),
            "conversation_revision": int(job_payload.get("conversation_revision") or 0),
            "turn_id": str(job_payload.get("turn_id") or ""),
            "parent_run_id": str(job_payload.get("parent_run_id") or ""),
        }
        return _internal_control(runtime, client, "execute", arguments)

    return operation


def _internal_control(
    runtime: AgentRuntime,
    client: Any,
    name: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    scope = {
        "service_pack_id": SERVICE_PACK_ID,
        "operation": f"agent.runtime.{name}",
        "authority": "agent.run.control",
        "caller_id": "agent.job.adapter",
        "caller_pack_id": SERVICE_PACK_ID,
        "caller_function_id": "agent.turn",
        "profile_id": runtime.profile_id,
        "workspace_id": "",
        "session_id": "",
        "arguments": dict(arguments),
        "approval_required": False,
    }
    issued = client.invoke(AUTHORITY, "authorize", scope)
    if not issued.get("authorized"):
        raise PermissionError(str(issued.get("reason") or "agent job denied"))
    return runtime.control(
        name,
        {
            **dict(arguments),
            "authority_receipt": str(issued.get("receipt") or ""),
            "caller_id": scope["caller_id"],
            "caller_pack_id": SERVICE_PACK_ID,
            "caller_function_id": scope["caller_function_id"],
            "session_id": "",
        },
    )


def _runtime(client: Any, payload: Mapping[str, Any]) -> AgentRuntime:
    profile_id = str(payload.get("profile_id") or "default")
    with _LOCK:
        return _RUNTIMES.setdefault(profile_id, AgentRuntime(client, profile_id))


def _control_arguments(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if name not in {
        "execute",
        "resume",
        "spawn",
        "cancel",
        "steer",
        "handoff",
    }:
        raise ValueError(f"unknown agent control operation: {name}")
    if name == "resume":
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            raise ValueError("agent resume run_id is required")
        return {
            "run_id": run_id,
            "tool_approvals": dict(_mapping(payload.get("tool_approvals"))),
        }
    if name in {"execute", "spawn"}:
        arguments = {
            "run_id": str(payload.get("run_id") or uuid.uuid4()),
            "idempotency_key": str(payload.get("idempotency_key") or ""),
            "agent_profile_id": str(payload.get("agent_profile_id") or "default"),
            "conversation_id": str(payload.get("conversation_id") or ""),
            "conversation_revision": int(payload.get("conversation_revision") or 0),
            "turn_id": str(payload.get("turn_id") or ""),
            "parent_run_id": str(payload.get("parent_run_id") or ""),
            "tool_approvals": {},
            "pending_tool_intents": [],
            "prior_tool_results": [],
        }
        if not arguments["idempotency_key"] or not arguments["conversation_id"]:
            raise ValueError("agent idempotency_key and conversation_id are required")
        if name == "spawn" and not arguments["parent_run_id"]:
            raise ValueError("subagent parent_run_id is required")
        return arguments
    arguments = {"run_id": str(payload.get("run_id") or "")}
    if not arguments["run_id"]:
        raise ValueError("agent run_id is required")
    if name == "cancel":
        arguments["reason"] = str(payload.get("reason") or "")[:1000]
    elif name == "steer":
        arguments["guidance"] = dict(_mapping(payload.get("guidance")))
    else:
        arguments["target"] = dict(_mapping(payload.get("target")))
    return arguments


def _context_messages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise ValueError("agent context is invalid")
    messages: list[dict[str, Any]] = []
    for section in value.get("sections") or []:
        if not isinstance(section, Mapping):
            continue
        kind = str(section.get("kind") or "context")
        for item in section.get("items") or []:
            if isinstance(item, Mapping) and item.get("role"):
                messages.append(
                    {
                        "role": str(item["role"]),
                        "content": item.get("content") or item.get("text") or "",
                    }
                )
            else:
                messages.append(
                    {
                        "role": "system",
                        "content": f"[{kind}] " + json.dumps(item, ensure_ascii=False),
                    }
                )
    return messages


def _latest_user_text(conversation: Mapping[str, Any]) -> str:
    for message in reversed(list(conversation.get("messages") or [])):
        if isinstance(message, Mapping) and message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _assistant_content(value: Any) -> Any:
    if not isinstance(value, Mapping):
        raise ValueError("AI response is invalid")
    output = value.get("output")
    if isinstance(output, Mapping):
        return output.get("content") or output.get("text") or output
    return output if output is not None else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("object payload is required")
    return value


def _bounded(value: Any) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if len(encoded.encode("utf-8")) > 64 * 1024:
        return {
            "status": "truncated",
            "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        }
    return json.loads(encoded)


def _hashed_receipt(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _stash_ephemeral_tool_payload(value: Mapping[str, Any]) -> str:
    now = time.time()
    for key, (expires_at, _) in list(_EPHEMERAL_TOOL_PAYLOADS.items()):
        if expires_at <= now:
            _EPHEMERAL_TOOL_PAYLOADS.pop(key, None)
    receipt = _hashed_receipt(secrets.token_urlsafe(32))
    _EPHEMERAL_TOOL_PAYLOADS[receipt] = (
        now + _EPHEMERAL_TTL_SECONDS,
        json.loads(json.dumps(dict(value), ensure_ascii=False, default=str)),
    )
    return receipt


def _load_ephemeral_tool_payload(receipt: str) -> dict[str, Any]:
    record = _EPHEMERAL_TOOL_PAYLOADS.pop(str(receipt or ""), None)
    if record is None or record[0] <= time.time():
        raise RuntimeError("ephemeral tool payload expired; resume fails closed")
    return record[1]


def _short(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
