"""Profile-scoped agent profiles, runs, deferred steers, and audit state."""

from __future__ import annotations

import json
import hashlib
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from core_runtime.paths import USER_DATA_DIR
from core_runtime.profile_workspace import validate_profile_id
from core_runtime.runtime_locks import NamedLock

AUTHORITY = "rumi.service.host.authorize.v1"
SERVICE_PACK_ID = "rumi_agent_state_store_pack"
VERSION = "rumi.agent-state.v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_TERMINAL = {"cancelled", "completed", "failed"}
_DEFERRED_TERMINAL = {"completed", "dismissed", "expired"}
_DEFERRED_STATUSES = {
    "queued",
    "ready",
    "applied",
    "completed",
    "dismissed",
    "expired",
    "failed",
}
_DEFERRED_SCOPES = {"execution", "conversation", "goal", "workspace"}
_DEFERRED_CHECKPOINTS = {
    "after_subtask",
    "after_turn",
    "after_execution",
    "manual_only",
}
_DEFERRED_SOURCES = {"ai", "user", "pack", "system"}
_DEFERRED_REFERENCE_KINDS = {
    "artifact",
    "conversation_node",
    "file",
    "issue",
    "run",
    "tool_result",
}
_MAX_ACTIVE_DEFERRED_STEERS = 128
_MAX_DEFERRED_HISTORY = 512
_TRANSITIONS = {
    "queued": {"planning", "running", "cancelled", "failed"},
    "planning": {"running", "waiting", "cancelled", "failed"},
    "running": {"waiting", "completed", "cancelled", "failed"},
    "waiting": {"planning", "running", "cancelled", "failed"},
}


class AgentStateConflict(RuntimeError):
    """Raised for stale revisions or invalid lifecycle transitions."""


class AgentStateStore:
    """Own canonical agent profiles and persistent run state."""

    def __init__(self, profile_id: str, *, root: Path | None = None) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self.root = (
            Path(root or USER_DATA_DIR) / "packs" / SERVICE_PACK_ID / "profiles" / self.profile_id
        )
        self.path = self.root / "agent-state.json"
        self.lock_root = self.root / "locks"

    def snapshot(self, kind: str) -> dict[str, Any]:
        """Return deterministic profile, run, or deferred-steer snapshots."""

        state = self._read()
        key = {
            "profile": "profiles",
            "run": "runs",
            "deferred": "deferred_steers",
        }.get(kind)
        if key is None:
            raise ValueError("agent state snapshot kind is invalid")
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "revision": state["revision"],
            key: [state[key][item] for item in sorted(state[key])],
        }

    def get(self, kind: str, value_id: str) -> dict[str, Any] | None:
        """Return one agent profile or run by exact ID."""

        state = self._read()
        key = {
            "profile": "profiles",
            "run": "runs",
            "deferred": "deferred_steers",
        }.get(kind)
        if key is None:
            raise ValueError("agent state record kind is invalid")
        value = state[key].get(_identifier(value_id))
        return _copy(value) if isinstance(value, Mapping) else None

    def list_deferred(
        self,
        *,
        scope_type: str = "",
        scope_id: str = "",
        statuses: set[str] | None = None,
    ) -> dict[str, Any]:
        """List bounded deferred steers using deterministic creation order."""

        state = self._read()
        values = [
            _copy(value)
            for value in state["deferred_steers"].values()
            if (not scope_type or value["scope"]["type"] == scope_type)
            and (not scope_id or value["scope"]["id"] == scope_id)
            and (statuses is None or value["status"] in statuses)
        ]
        values.sort(key=lambda item: (int(item["created_at_ms"]), item["id"]))
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "revision": state["revision"],
            "deferred_steers": values[:512],
        }

    def apply(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Apply one revision-bound agent state transition."""

        with NamedLock(self.lock_root, "agent-state"):
            state = self._read()
            if name == "deferred.register":
                known = _known_deferred_registration(state, arguments)
                if known is not None:
                    return {
                        "deferred_steer": _copy(known),
                        "deduplicated": True,
                        "revision": state["revision"],
                    }
            _assert_revision(state, int(arguments["expected_revision"]))
            result = self._transition(state, name, arguments)
            if name == "deferred.checkpoint" and not result.get("ready_count"):
                return {**result, "revision": state["revision"]}
            state["revision"] += 1
            self._write(state)
            return {**result, "revision": state["revision"]}

    def _transition(
        self,
        state: dict[str, Any],
        name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        now_ms = _now_ms()
        if name.startswith("deferred."):
            return self._deferred_transition(state, name, arguments, now_ms)
        if name == "profile.upsert":
            value = _agent_profile(arguments["profile"])
            current = state["profiles"].get(value["id"])
            value["created_at_ms"] = current["created_at_ms"] if current else now_ms
            value["updated_at_ms"] = now_ms
            state["profiles"][value["id"]] = value
            return {"agent_profile": _copy(value)}
        if name == "profile.delete":
            profile_id = _identifier(arguments["agent_profile_id"])
            if any(
                run["agent_profile_id"] == profile_id and run["status"] not in _TERMINAL
                for run in state["runs"].values()
            ):
                raise AgentStateConflict("agent profile has active runs")
            if state["profiles"].pop(profile_id, None) is None:
                raise KeyError("agent profile is unknown")
            return {"deleted_agent_profile_id": profile_id}
        if name == "run.begin":
            run_id = _identifier(arguments["run_id"])
            known = state["runs"].get(run_id)
            if known is not None:
                if known["idempotency_key"] != arguments["idempotency_key"]:
                    raise AgentStateConflict("agent run ID is already bound")
                return {"run": _copy(known), "deduplicated": True}
            agent_profile_id = _identifier(arguments["agent_profile_id"])
            if agent_profile_id not in state["profiles"]:
                raise KeyError("agent profile is unknown")
            parent_run_id = str(arguments["parent_run_id"] or "")
            if parent_run_id:
                parent = state["runs"].get(_identifier(parent_run_id))
                if parent is None or parent["status"] in _TERMINAL:
                    raise AgentStateConflict("parent run is unavailable")
                parent_profile = state["profiles"][parent["agent_profile_id"]]
                if not parent_profile["allow_subagents"]:
                    raise PermissionError("agent profile denies subagents")
                if len(parent["child_run_ids"]) >= int(parent_profile["max_children"]):
                    raise AgentStateConflict("subagent child limit reached")
            run = {
                "id": run_id,
                "idempotency_key": _identifier(arguments["idempotency_key"]),
                "agent_profile_id": agent_profile_id,
                "conversation_id": str(arguments["conversation_id"]),
                "turn_id": str(arguments["turn_id"]),
                "parent_run_id": parent_run_id,
                "child_run_ids": [],
                "status": "queued",
                "step": 0,
                "cancel_requested": False,
                "effect_committing": False,
                "effect_executor_token_hash": "",
                "guidance": [],
                "handoff": None,
                "result_reference": None,
                "terminal_projection": None,
                "reconciliation_required": False,
                "error": "",
                "events": [],
                "created_at_ms": now_ms,
                "updated_at_ms": now_ms,
            }
            _event(run, "agent.run.queued", {})
            state["runs"][run_id] = run
            if parent_run_id:
                state["runs"][parent_run_id]["child_run_ids"].append(run_id)
                _event(
                    state["runs"][parent_run_id],
                    "agent.subagent.started",
                    {"child_run_id": run_id},
                )
            return {"run": _copy(run), "deduplicated": False}
        run_id = _identifier(arguments["run_id"])
        run = state["runs"].get(run_id)
        if run is None:
            raise KeyError("agent run is unknown")
        if name == "run.transition":
            target = str(arguments["status"])
            if target not in _TRANSITIONS.get(run["status"], set()):
                raise AgentStateConflict("agent run transition is invalid")
            run["status"] = target
            run["step"] = max(int(run["step"]), int(arguments["step"]))
            details = dict(arguments["details"])
            if target == "completed":
                run["result_reference"] = details.get("result_reference")
                run["terminal_projection"] = details.get("terminal_projection")
                run["reconciliation_required"] = bool(run["terminal_projection"])
            if target == "failed":
                run["error"] = str(details.get("error") or "")[:1000]
            _event(run, f"agent.run.{target}", details)
        elif name == "run.reconcile":
            if run["status"] not in _TERMINAL:
                raise AgentStateConflict("only terminal runs can be reconciled")
            if not run.get("reconciliation_required"):
                return {"run": _copy(run), "already_reconciled": True}
            run["reconciliation_required"] = False
            _event(
                run,
                "agent.run.terminal_reconciled",
                {"projection_receipt": str(arguments["projection_receipt"])},
            )
        elif name == "run.effect.begin":
            if run["status"] != "running" or run.get("cancel_requested"):
                raise AgentStateConflict("agent effect cannot begin")
            if run.get("effect_committing"):
                raise AgentStateConflict("agent effect is already committing")
            run["effect_committing"] = True
            token_hash = hashlib.sha256(
                str(arguments["executor_token"]).encode("utf-8")
            ).hexdigest()
            run["effect_executor_token_hash"] = token_hash
            _event(
                run,
                "agent.run.effect_committing",
                {"effect_receipt": str(arguments["effect_receipt"])},
            )
        elif name == "run.effect.end":
            if not run.get("effect_committing") or (
                run.get("effect_executor_token_hash")
                != hashlib.sha256(str(arguments["executor_token"]).encode("utf-8")).hexdigest()
            ):
                raise AgentStateConflict("agent effect executor token is invalid")
            run["effect_committing"] = False
            run["effect_executor_token_hash"] = ""
            _event(
                run,
                "agent.run.effect_committed",
                {"effect_receipt": str(arguments["effect_receipt"])},
            )
        elif name == "run.cancel":
            if run["status"] in _TERMINAL:
                return {"run": _copy(run), "already_terminal": True}
            if run.get("effect_committing"):
                run["cancel_requested"] = True
                _event(run, "agent.run.cancel_requested", {"reason": arguments["reason"]})
                return {
                    "run": _copy(run),
                    "too_late": True,
                    "effect_committing": True,
                }
            run["cancel_requested"] = True
            run["status"] = "cancelled"
            _event(run, "agent.run.cancelled", {"reason": arguments["reason"]})
        elif name == "run.steer":
            if run["status"] in _TERMINAL:
                raise AgentStateConflict("terminal agent run cannot be steered")
            guidance = {
                "id": str(uuid.uuid4()),
                "value": _copy(arguments["guidance"]),
                "status": "queued",
                "created_at_ms": now_ms,
            }
            run["guidance"].append(guidance)
            _event(run, "agent.run.steered", {"guidance_id": guidance["id"]})
        elif name == "run.handoff":
            if run["status"] in _TERMINAL:
                raise AgentStateConflict("terminal agent run cannot hand off")
            run["handoff"] = _copy(arguments["target"])
            _event(run, "agent.run.handoff", {"target": run["handoff"]})
        else:
            raise ValueError(f"unknown agent state action: {name}")
        run["updated_at_ms"] = now_ms
        return {"run": _copy(run)}

    def _deferred_transition(
        self,
        state: dict[str, Any],
        name: str,
        arguments: Mapping[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        """Apply one closed deferred-steer lifecycle transition."""

        if name == "deferred.register":
            _prune_deferred_history(state)
            active_count = sum(
                value["status"] not in _DEFERRED_TERMINAL
                for value in state["deferred_steers"].values()
            )
            if active_count >= _MAX_ACTIVE_DEFERRED_STEERS:
                raise AgentStateConflict("deferred steer capacity reached")
            record = _deferred_record(arguments, now_ms)
            state["deferred_steers"][record["id"]] = record
            state["deferred_idempotency"][record["idempotency_key"]] = {
                "deferred_steer_id": record["id"],
                "payload_hash": _deferred_payload_hash(record),
            }
            return {"deferred_steer": _copy(record), "deduplicated": False}

        if name == "deferred.checkpoint":
            checkpoint = _closed_value(
                arguments.get("checkpoint"),
                _DEFERRED_CHECKPOINTS - {"manual_only"},
                "deferred steer checkpoint",
            )
            scope = _deferred_scope(arguments)
            ready: list[dict[str, Any]] = []
            for record in state["deferred_steers"].values():
                if (
                    record["status"] == "queued"
                    and record["checkpoint"] == checkpoint
                    and record["scope"] == scope
                ):
                    record["status"] = "ready"
                    record["revision"] += 1
                    record["ready_at_ms"] = now_ms
                    record["updated_at_ms"] = now_ms
                    _deferred_event(record, "deferred.ready", {"checkpoint": checkpoint})
                    ready.append(_copy(record))
            return {"deferred_steers": ready, "ready_count": len(ready)}

        steer_id = _identifier(arguments.get("deferred_steer_id"))
        record = state["deferred_steers"].get(steer_id)
        if record is None:
            raise KeyError("deferred steer is unknown")
        expected = int(arguments.get("expected_steer_revision") or 0)
        if int(record["revision"]) != expected:
            raise AgentStateConflict("deferred steer revision is stale")

        details: dict[str, Any]
        if name == "deferred.update":
            if record["status"] not in {"queued", "ready", "failed"}:
                raise AgentStateConflict("deferred steer can no longer be edited")
            updates = _deferred_updates(arguments.get("updates"))
            record.update(updates)
            event_name = "deferred.updated"
            details = {"fields": sorted(updates)}
        elif name == "deferred.defer":
            if record["status"] not in {"queued", "ready", "failed"}:
                raise AgentStateConflict("deferred steer can no longer be deferred")
            checkpoint = _closed_value(
                arguments.get("checkpoint") or record["checkpoint"],
                _DEFERRED_CHECKPOINTS,
                "deferred steer checkpoint",
            )
            record["checkpoint"] = checkpoint
            record["status"] = "queued"
            record["ready_at_ms"] = None
            event_name = "deferred.deferred"
            details = {"checkpoint": checkpoint}
        elif name == "deferred.apply":
            if record["status"] not in {"queued", "ready", "failed"}:
                raise AgentStateConflict("deferred steer cannot be applied")
            reference = _application_reference(arguments.get("application_reference"))
            record["status"] = "applied"
            record["application_reference"] = reference
            record["applied_at_ms"] = now_ms
            event_name = "deferred.applied"
            details = {"application_reference": reference}
        elif name == "deferred.complete":
            if record["status"] != "applied":
                raise AgentStateConflict("only an applied deferred steer can complete")
            record["status"] = "completed"
            record["completed_at_ms"] = now_ms
            event_name = "deferred.completed"
            details = {}
        elif name == "deferred.dismiss":
            if record["status"] in _DEFERRED_TERMINAL:
                raise AgentStateConflict("deferred steer is already terminal")
            record["status"] = "dismissed"
            record["dismissed_at_ms"] = now_ms
            event_name = "deferred.dismissed"
            details = {"reason": str(arguments.get("reason") or "")[:1000]}
        elif name == "deferred.expire":
            if record["status"] in _DEFERRED_TERMINAL:
                raise AgentStateConflict("deferred steer is already terminal")
            record["status"] = "expired"
            record["expired_at_ms"] = now_ms
            event_name = "deferred.expired"
            details = {"reason": str(arguments.get("reason") or "")[:1000]}
        elif name == "deferred.fail":
            if record["status"] in _DEFERRED_TERMINAL:
                raise AgentStateConflict("terminal deferred steer cannot fail")
            record["status"] = "failed"
            record["error"] = str(arguments.get("error") or "")[:1000]
            event_name = "deferred.failed"
            details = {"error": record["error"]}
        else:
            raise ValueError(f"unknown agent state action: {name}")

        record["revision"] += 1
        record["updated_at_ms"] = now_ms
        _deferred_event(record, event_name, details)
        return {"deferred_steer": _copy(record)}

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "version": VERSION,
                "profile_id": self.profile_id,
                "revision": 0,
                "profiles": {"default": _default_profile()},
                "runs": {},
                "deferred_steers": {},
                "deferred_idempotency": {},
            }
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, Mapping)
            or value.get("version") != VERSION
            or value.get("profile_id") != self.profile_id
        ):
            raise ValueError("agent state is invalid")
        if not isinstance(value.get("profiles"), Mapping) or not isinstance(
            value.get("runs"), Mapping
        ):
            raise ValueError("agent state records are invalid")
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "revision": max(0, int(value.get("revision") or 0)),
            "profiles": _copy(value["profiles"]),
            "runs": _copy(value["runs"]),
            "deferred_steers": _copy(value.get("deferred_steers") or {}),
            "deferred_idempotency": _copy(value.get("deferred_idempotency") or {}),
        }

    def _write(self, value: Mapping[str, Any]) -> None:
        _atomic_json(self.path, value)


def create_agent_resource(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create agent profile and run read operations."""

    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        store = AgentStateStore(_profile(payload))
        if name in {"profile.list", "run.list"}:
            return store.snapshot(name.split(".", 1)[0])
        if name == "deferred.list":
            statuses = _deferred_status_filter(payload.get("statuses"))
            return store.list_deferred(
                scope_type=str(payload.get("scope_type") or ""),
                scope_id=str(payload.get("scope_id") or ""),
                statuses=statuses,
            )
        if name == "profile.get":
            return store.get("profile", str(payload.get("agent_profile_id") or ""))
        if name == "run.get":
            return store.get("run", str(payload.get("run_id") or ""))
        if name == "deferred.get":
            return store.get("deferred", str(payload.get("deferred_steer_id") or ""))
        raise ValueError(f"unknown agent resource operation: {name}")

    return operation


def create_agent_state_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated agent state transitions."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        arguments = _arguments(name, payload)
        _redeem(client, payload, name, arguments)
        return AgentStateStore(_profile(payload)).apply(name, arguments)

    return operation


def _arguments(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if name not in {
        "profile.upsert",
        "profile.delete",
        "run.begin",
        "run.transition",
        "run.effect.begin",
        "run.effect.end",
        "run.reconcile",
        "run.cancel",
        "run.steer",
        "run.handoff",
        "deferred.register",
        "deferred.update",
        "deferred.checkpoint",
        "deferred.defer",
        "deferred.apply",
        "deferred.complete",
        "deferred.dismiss",
        "deferred.expire",
        "deferred.fail",
    }:
        raise ValueError(f"unknown agent state action: {name}")
    arguments: dict[str, Any] = {
        "expected_revision": max(0, int(payload.get("expected_revision") or 0))
    }
    if name == "deferred.register":
        arguments.update(
            {
                "deferred_steer_id": str(payload.get("deferred_steer_id") or uuid.uuid4()),
                "idempotency_key": str(payload.get("idempotency_key") or ""),
                "title": payload.get("title"),
                "instruction": payload.get("instruction"),
                "reason": payload.get("reason"),
                "scope_type": payload.get("scope_type"),
                "scope_id": payload.get("scope_id"),
                "checkpoint": payload.get("checkpoint"),
                "source": payload.get("source"),
                "source_id": payload.get("source_id"),
                "actor_id": payload.get("actor_id"),
                "related_references": payload.get("related_references"),
                "dedupe_key": payload.get("dedupe_key"),
            }
        )
        if not arguments["idempotency_key"]:
            raise ValueError("deferred steer idempotency_key is required")
    elif name == "deferred.checkpoint":
        arguments.update(
            {
                "checkpoint": payload.get("checkpoint"),
                "scope_type": payload.get("scope_type"),
                "scope_id": payload.get("scope_id"),
            }
        )
    elif name.startswith("deferred."):
        arguments.update(
            {
                "deferred_steer_id": str(
                    payload.get("deferred_steer_id") or payload.get("steer_id") or ""
                ),
                "expected_steer_revision": max(0, int(payload.get("expected_steer_revision") or 0)),
            }
        )
        if name == "deferred.update":
            arguments["updates"] = dict(_mapping(payload.get("updates")))
        elif name == "deferred.defer":
            arguments["checkpoint"] = payload.get("checkpoint")
        elif name == "deferred.apply":
            arguments["application_reference"] = dict(
                _mapping(payload.get("application_reference"))
            )
        elif name in {"deferred.dismiss", "deferred.expire"}:
            arguments["reason"] = str(payload.get("reason") or "")
        elif name == "deferred.fail":
            arguments["error"] = str(payload.get("error") or "")
    elif name == "profile.upsert":
        arguments["profile"] = dict(_mapping(payload.get("profile")))
    elif name == "profile.delete":
        arguments["agent_profile_id"] = str(payload.get("agent_profile_id") or "")
    elif name == "run.begin":
        arguments.update(
            {
                "run_id": str(payload.get("run_id") or uuid.uuid4()),
                "idempotency_key": str(payload.get("idempotency_key") or ""),
                "agent_profile_id": str(payload.get("agent_profile_id") or "default"),
                "conversation_id": str(payload.get("conversation_id") or ""),
                "turn_id": str(payload.get("turn_id") or ""),
                "parent_run_id": str(payload.get("parent_run_id") or ""),
            }
        )
        if not arguments["idempotency_key"]:
            raise ValueError("agent run idempotency_key is required")
    else:
        arguments["run_id"] = str(payload.get("run_id") or "")
        if name == "run.transition":
            arguments["status"] = str(payload.get("status") or "")
            arguments["step"] = max(0, int(payload.get("step") or 0))
            arguments["details"] = dict(_mapping(payload.get("details")))
        elif name in {"run.effect.begin", "run.effect.end"}:
            token = str(payload.get("executor_token") or "")
            if not token:
                raise ValueError("effect executor token is required")
            arguments["executor_token"] = token
            arguments["effect_receipt"] = str(payload.get("effect_receipt") or "")
        elif name == "run.reconcile":
            arguments["projection_receipt"] = str(payload.get("projection_receipt") or "")
        elif name == "run.cancel":
            arguments["reason"] = str(payload.get("reason") or "")[:1000]
        elif name == "run.steer":
            arguments["guidance"] = dict(_mapping(payload.get("guidance")))
        elif name == "run.handoff":
            arguments["target"] = dict(_mapping(payload.get("target")))
    return arguments


def _redeem(
    client: Any,
    payload: Mapping[str, Any],
    name: str,
    arguments: Mapping[str, Any],
) -> None:
    result = client.invoke(
        AUTHORITY,
        "redeem",
        {
            "receipt": str(payload.get("authority_receipt") or ""),
            "service_pack_id": SERVICE_PACK_ID,
            "operation": f"agent.state.{name}",
            "authority": "agent.state.manage",
            "caller_id": str(payload.get("caller_id") or ""),
            "caller_pack_id": str(payload.get("caller_pack_id") or ""),
            "caller_function_id": str(payload.get("caller_function_id") or ""),
            "profile_id": _profile(payload),
            "workspace_id": "",
            "session_id": str(payload.get("session_id") or ""),
            "arguments": dict(arguments),
        },
    )
    if not result.get("authorized"):
        raise PermissionError(str(result.get("reason") or "agent state denied"))


def _agent_profile(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _identifier(value.get("id") or "default"),
        "display_name": str(value.get("display_name") or "Agent")[:120],
        "system_prompt": str(value.get("system_prompt") or "")[:100_000],
        "model_profile_id": str(value.get("model_profile_id") or ""),
        "tools": [str(item) for item in value.get("tools") or []][:500],
        "max_steps": max(1, min(64, int(value.get("max_steps") or 8))),
        "context_token_budget": max(
            256, min(1_000_000, int(value.get("context_token_budget") or 8192))
        ),
        "allow_subagents": bool(value.get("allow_subagents", False)),
        "max_children": max(0, min(32, int(value.get("max_children") or 0))),
        "metadata": _copy(_mapping(value.get("metadata"))),
    }


def _deferred_record(arguments: Mapping[str, Any], now_ms: int) -> dict[str, Any]:
    scope = _deferred_scope(arguments)
    record = {
        "id": _identifier(arguments.get("deferred_steer_id")),
        "kind": "deferred",
        "idempotency_key": _identifier(arguments.get("idempotency_key")),
        "dedupe_key": str(arguments.get("dedupe_key") or "")[:255],
        "title": _bounded_text(arguments.get("title"), "title", 160),
        "instruction": _bounded_text(arguments.get("instruction"), "instruction", 10_000),
        "reason": _bounded_text(arguments.get("reason"), "reason", 4_000),
        "scope": scope,
        "checkpoint": _closed_value(
            arguments.get("checkpoint"),
            _DEFERRED_CHECKPOINTS,
            "deferred steer checkpoint",
        ),
        "source": _closed_value(
            arguments.get("source"),
            _DEFERRED_SOURCES,
            "deferred steer source",
        ),
        "source_id": str(arguments.get("source_id") or "")[:255],
        "actor_id": str(arguments.get("actor_id") or "")[:255],
        "related_references": _typed_references(arguments.get("related_references")),
        "status": "queued",
        "revision": 1,
        "application_reference": None,
        "error": "",
        "created_at_ms": now_ms,
        "updated_at_ms": now_ms,
        "ready_at_ms": None,
        "applied_at_ms": None,
        "completed_at_ms": None,
        "dismissed_at_ms": None,
        "expired_at_ms": None,
        "events": [],
    }
    _deferred_event(
        record,
        "deferred.queued",
        {
            "source": record["source"],
            "scope": record["scope"],
            "checkpoint": record["checkpoint"],
        },
    )
    return record


def _known_deferred_registration(
    state: Mapping[str, Any], arguments: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    idempotency_key = _identifier(arguments.get("idempotency_key"))
    known = state["deferred_idempotency"].get(idempotency_key)
    if isinstance(known, Mapping):
        steer = state["deferred_steers"].get(known.get("deferred_steer_id"))
        if not isinstance(steer, Mapping):
            raise AgentStateConflict("deferred steer idempotency record is invalid")
        candidate = _deferred_record(
            {**dict(arguments), "deferred_steer_id": steer["id"]},
            int(steer["created_at_ms"]),
        )
        if known.get("payload_hash") != _deferred_payload_hash(candidate):
            raise AgentStateConflict("deferred steer idempotency payload does not match")
        return steer

    dedupe_key = str(arguments.get("dedupe_key") or "")[:255]
    if not dedupe_key:
        return None
    scope = _deferred_scope(arguments)
    for steer in state["deferred_steers"].values():
        if (
            steer.get("dedupe_key") == dedupe_key
            and steer.get("scope") == scope
            and steer.get("status") not in _DEFERRED_TERMINAL
        ):
            return steer
    return None


def _deferred_payload_hash(record: Mapping[str, Any]) -> str:
    payload = {
        key: record[key]
        for key in (
            "dedupe_key",
            "title",
            "instruction",
            "reason",
            "scope",
            "checkpoint",
            "source",
            "source_id",
            "actor_id",
            "related_references",
        )
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _deferred_updates(value: Any) -> dict[str, Any]:
    updates = _mapping(value)
    unknown = set(updates) - {"title", "instruction", "reason", "checkpoint"}
    if unknown:
        raise ValueError("deferred steer update fields are invalid")
    result: dict[str, Any] = {}
    if "title" in updates:
        result["title"] = _bounded_text(updates["title"], "title", 160)
    if "instruction" in updates:
        result["instruction"] = _bounded_text(updates["instruction"], "instruction", 10_000)
    if "reason" in updates:
        result["reason"] = _bounded_text(updates["reason"], "reason", 4_000)
    if "checkpoint" in updates:
        result["checkpoint"] = _closed_value(
            updates["checkpoint"],
            _DEFERRED_CHECKPOINTS,
            "deferred steer checkpoint",
        )
    if not result:
        raise ValueError("deferred steer updates are required")
    return result


def _deferred_scope(value: Mapping[str, Any]) -> dict[str, str]:
    scope_type = _closed_value(value.get("scope_type"), _DEFERRED_SCOPES, "deferred steer scope")
    scope_id = _identifier(value.get("scope_id"))
    return {"type": scope_type, "id": scope_id}


def _typed_references(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("deferred steer references must be a bounded array")
    references: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) - {"kind", "id", "label"}:
            raise ValueError("deferred steer reference is invalid")
        kind = _closed_value(
            item.get("kind"),
            _DEFERRED_REFERENCE_KINDS,
            "deferred steer reference kind",
        )
        references.append(
            {
                "kind": kind,
                "id": _identifier(item.get("id")),
                "label": str(item.get("label") or "")[:160],
            }
        )
    return references


def _application_reference(value: Any) -> dict[str, str]:
    reference = _mapping(value)
    if set(reference) - {"kind", "conversation_id", "message_id", "task_id"}:
        raise ValueError("deferred steer application reference is invalid")
    kind = _closed_value(
        reference.get("kind"),
        {"conversation_instruction", "new_conversation", "task"},
        "deferred steer application kind",
    )
    result = {"kind": kind}
    for key in ("conversation_id", "message_id", "task_id"):
        if reference.get(key):
            result[key] = _identifier(reference[key])
    if kind in {"conversation_instruction", "new_conversation"} and not (
        result.get("conversation_id") and result.get("message_id")
    ):
        raise ValueError("deferred steer application message reference is required")
    if kind == "task" and not result.get("task_id"):
        raise ValueError("deferred steer application task reference is required")
    return result


def _deferred_status_filter(value: Any) -> set[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("deferred steer statuses must be an array")
    statuses = {str(item).strip().lower() for item in value}
    if not statuses <= _DEFERRED_STATUSES:
        raise ValueError("deferred steer status is invalid")
    return statuses


def _prune_deferred_history(state: dict[str, Any]) -> None:
    terminal = sorted(
        (
            value
            for value in state["deferred_steers"].values()
            if value.get("status") in _DEFERRED_TERMINAL
        ),
        key=lambda item: (int(item.get("updated_at_ms") or 0), str(item.get("id") or "")),
    )
    expired = terminal[: max(0, len(terminal) - _MAX_DEFERRED_HISTORY)]
    expired_ids = {str(item["id"]) for item in expired}
    if not expired_ids:
        return
    for steer_id in expired_ids:
        state["deferred_steers"].pop(steer_id, None)
    state["deferred_idempotency"] = {
        key: value
        for key, value in state["deferred_idempotency"].items()
        if value.get("deferred_steer_id") not in expired_ids
    }


def _bounded_text(value: Any, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"deferred steer {field} is required")
    if len(text) > limit:
        raise ValueError(f"deferred steer {field} is too long")
    return text


def _closed_value(value: Any, allowed: set[str], field: str) -> str:
    result = str(value or "").strip().lower()
    if result not in allowed:
        raise ValueError(f"{field} is invalid")
    return result


def _deferred_event(record: dict[str, Any], name: str, details: Mapping[str, Any]) -> None:
    record["events"].append(
        {
            "sequence": len(record["events"]),
            "name": name,
            "at_ms": _now_ms(),
            "details": _copy(details),
        }
    )


def _default_profile() -> dict[str, Any]:
    return {
        **_agent_profile(
            {
                "id": "default",
                "display_name": "Default Agent",
                "max_steps": 8,
                "allow_subagents": False,
            }
        ),
        "created_at_ms": 0,
        "updated_at_ms": 0,
    }


def _event(run: dict[str, Any], name: str, details: Mapping[str, Any]) -> None:
    run["events"].append(
        {
            "sequence": len(run["events"]),
            "name": name,
            "at_ms": _now_ms(),
            "details": _copy(details),
        }
    )


def _identifier(value: Any) -> str:
    identifier = str(value or "").strip()
    if not _ID.fullmatch(identifier):
        raise ValueError("agent identifier is invalid")
    return identifier


def _mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("object payload is required")
    return value


def _assert_revision(state: Mapping[str, Any], expected: int) -> None:
    if int(state.get("revision") or 0) != expected:
        raise AgentStateConflict("agent state revision is stale")


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".agent-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
