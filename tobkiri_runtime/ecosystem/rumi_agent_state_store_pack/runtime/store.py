"""Profile-scoped agent profiles, runs, lifecycle, and audit state."""

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
            Path(root or USER_DATA_DIR)
            / "packs"
            / SERVICE_PACK_ID
            / "profiles"
            / self.profile_id
        )
        self.path = self.root / "agent-state.json"
        self.lock_root = self.root / "locks"

    def snapshot(self, kind: str) -> dict[str, Any]:
        """Return deterministic profile or run snapshots."""

        state = self._read()
        key = "profiles" if kind == "profile" else "runs"
        return {
            "version": VERSION,
            "profile_id": self.profile_id,
            "revision": state["revision"],
            key: [state[key][item] for item in sorted(state[key])],
        }

    def get(self, kind: str, value_id: str) -> dict[str, Any] | None:
        """Return one agent profile or run by exact ID."""

        state = self._read()
        key = "profiles" if kind == "profile" else "runs"
        value = state[key].get(_identifier(value_id))
        return _copy(value) if isinstance(value, Mapping) else None

    def apply(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Apply one revision-bound agent state transition."""

        with NamedLock(self.lock_root, "agent-state"):
            state = self._read()
            _assert_revision(state, int(arguments["expected_revision"]))
            result = self._transition(state, name, arguments)
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
        if name == "profile.upsert":
            value = _agent_profile(arguments["profile"])
            current = state["profiles"].get(value["id"])
            value["created_at_ms"] = (
                current["created_at_ms"] if current else now_ms
            )
            value["updated_at_ms"] = now_ms
            state["profiles"][value["id"]] = value
            return {"agent_profile": _copy(value)}
        if name == "profile.delete":
            profile_id = _identifier(arguments["agent_profile_id"])
            if any(
                run["agent_profile_id"] == profile_id
                and run["status"] not in _TERMINAL
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
                if len(parent["child_run_ids"]) >= int(
                    parent_profile["max_children"]
                ):
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
                run["reconciliation_required"] = bool(
                    run["terminal_projection"]
                )
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
                != hashlib.sha256(
                    str(arguments["executor_token"]).encode("utf-8")
                ).hexdigest()
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

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "version": VERSION,
                "profile_id": self.profile_id,
                "revision": 0,
                "profiles": {"default": _default_profile()},
                "runs": {},
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
        if name == "profile.get":
            return store.get("profile", str(payload.get("agent_profile_id") or ""))
        if name == "run.get":
            return store.get("run", str(payload.get("run_id") or ""))
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
    }:
        raise ValueError(f"unknown agent state action: {name}")
    arguments: dict[str, Any] = {
        "expected_revision": max(0, int(payload.get("expected_revision") or 0))
    }
    if name == "profile.upsert":
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
            arguments["projection_receipt"] = str(
                payload.get("projection_receipt") or ""
            )
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
