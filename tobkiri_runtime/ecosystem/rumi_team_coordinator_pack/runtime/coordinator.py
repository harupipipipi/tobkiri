"""Contract-only Team routing, task dispatch, and supervisor coordination."""

from __future__ import annotations

import hashlib
import re
import threading
from typing import Any, Callable, Mapping

AUTHORITY = "rumi.service.host.authorize.v1"
TEAM_RESOURCE = "tobkiri.resource.team.v1"
TEAM_ACTION = "tobkiri.action.team.state.v1"
TEAM_WORK = "tobkiri.action.team.work.v1"
SERVICE_PACK_ID = "rumi_team_coordinator_pack"
STATE_PACK_ID = "rumi_team_state_store_pack"
_MENTION = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_.:-]{1,100})")


class TeamCoordinator:
    """Coordinate Team work without importing target implementations."""

    def __init__(self, client: Any, profile_id: str) -> None:
        self.client = client
        self.profile_id = profile_id
        self.lock = threading.RLock()
        self.active_tasks: set[str] = set()

    def status(self) -> dict[str, Any]:
        """Return non-authoritative process coordination state."""

        with self.lock:
            return {
                "profile_id": self.profile_id,
                "active_task_ids": sorted(self.active_tasks),
                "state_owner": STATE_PACK_ID,
            }

    def control(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Apply one receipt-gated coordinator control."""

        arguments = _control_arguments(name, payload)
        self._redeem(payload, name, arguments)
        if name == "route_inbound":
            return self._route_inbound(arguments)
        if name == "dispatch_task":
            return self._dispatch_task(arguments["team_id"], arguments["task_id"])
        if name == "supervisor_tick":
            return self._tick(arguments["team_id"], arguments["limit"])
        return self._cancel(arguments["team_id"], arguments["task_id"])

    def _route_inbound(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        team = self._team(str(arguments["team_id"]))
        inbound = next(
            (
                item
                for item in team.get("inbound") or []
                if isinstance(item, Mapping) and item.get("id") == arguments["inbound_id"]
            ),
            None,
        )
        if inbound is None:
            raise KeyError("Team inbound record is unknown")
        member, reason = _select_member(team, inbound)
        if member is None:
            return {
                "status": "unassigned",
                "team_id": team["id"],
                "inbound_id": inbound["id"],
                "reason": reason,
            }
        task_id = "inbound-" + _hash(
            f"{team['id']}\0{inbound['id']}\0{member['id']}"
        )[:40]
        upserted = self._state_action(
            "task.upsert",
            {
                "team_id": team["id"],
                "record": {
                    "id": task_id,
                    "title": f"Inbound: {str(inbound.get('text') or '')[:120]}",
                    "description": str(inbound.get("text") or ""),
                    "status": "assigned",
                    "assignee_member_id": member["id"],
                    "channel_id": str(inbound.get("channel_id") or ""),
                    "idempotency_key": task_id,
                    "metadata": {
                        "source": "team_inbound",
                        "inbound_id": inbound["id"],
                        "routing_reason": reason,
                    },
                },
            },
        )
        dispatched = self._dispatch_task(team["id"], task_id)
        return {
            "status": "routed",
            "member": member,
            "task": upserted["task"],
            "dispatch": dispatched,
        }

    def _dispatch_task(self, team_id: str, task_id: str) -> dict[str, Any]:
        team = self._team(team_id)
        task = _required(team.get("tasks"), task_id, "Team task")
        member_id = str(task.get("assignee_member_id") or "")
        member = _required(team.get("members"), member_id, "Team member")
        if not member.get("enabled"):
            raise PermissionError("Team member is disabled")
        role = _required(team.get("roles"), str(member["role_id"]), "Team role")
        work_type = str(role.get("work_type") or "agent")
        provider = _provider(self.client.providers(TEAM_WORK), work_type)
        with self.lock:
            if task_id in self.active_tasks:
                return {"status": "running", "task_id": task_id, "deduplicated": True}
            self.active_tasks.add(task_id)
        try:
            if task["status"] == "queued":
                task = self._state_action(
                    "task.transition",
                    {
                        "team_id": team_id,
                        "task_id": task_id,
                        "status": "assigned",
                        "details": {"assignee_member_id": member_id},
                    },
                )["task"]
            if task["status"] in {"assigned", "waiting"}:
                task = self._state_action(
                    "task.transition",
                    {
                        "team_id": team_id,
                        "task_id": task_id,
                        "status": "running",
                        "details": {},
                    },
                )["task"]
            if task["status"] != "running":
                return {"status": task["status"], "task": task}
            result = self.client.invoke(
                TEAM_WORK,
                "dispatch",
                {
                    "profile_id": self.profile_id,
                    "team": _public_team(team),
                    "task": task,
                    "member": member,
                    "role": role,
                },
                provider_instance_id=str(provider["provider_instance_id"]),
            )
            target, details = _result_transition(result)
            finished = self._state_action(
                "task.transition",
                {
                    "team_id": team_id,
                    "task_id": task_id,
                    "status": target,
                    "details": details,
                },
            )["task"]
            return {"status": target, "task": finished, "work": result}
        except Exception as exc:
            try:
                failed = self._state_action(
                    "task.transition",
                    {
                        "team_id": team_id,
                        "task_id": task_id,
                        "status": "failed",
                        "details": {"error": str(exc)[:1000]},
                    },
                )["task"]
            except Exception:
                failed = task
            return {"status": "failed", "task": failed, "error": str(exc)}
        finally:
            with self.lock:
                self.active_tasks.discard(task_id)

    def _tick(self, team_id: str, limit: int) -> dict[str, Any]:
        team = self._team(team_id)
        candidates = sorted(
            (
                task
                for task in team.get("tasks", {}).values()
                if task["status"] in {"queued", "assigned"}
                and task.get("assignee_member_id")
            ),
            key=lambda task: (-int(task.get("priority") or 0), str(task["id"])),
        )[:limit]
        results = [self._dispatch_task(team_id, str(task["id"])) for task in candidates]
        return {"status": "ok", "count": len(results), "results": results}

    def _cancel(self, team_id: str, task_id: str) -> dict[str, Any]:
        team = self._team(team_id)
        task = _required(team.get("tasks"), task_id, "Team task")
        member = _required(
            team.get("members"),
            str(task.get("assignee_member_id") or ""),
            "Team member",
        )
        role = _required(team.get("roles"), str(member["role_id"]), "Team role")
        provider = _provider(
            self.client.providers(TEAM_WORK),
            str(role.get("work_type") or "agent"),
        )
        projected = self.client.invoke(
            TEAM_WORK,
            "cancel",
            {
                "profile_id": self.profile_id,
                "team": _public_team(team),
                "task": task,
                "member": member,
                "role": role,
            },
            provider_instance_id=str(provider["provider_instance_id"]),
        )
        cancelled = self._state_action(
            "task.transition",
            {
                "team_id": team_id,
                "task_id": task_id,
                "status": "cancelled",
                "details": {},
            },
        )["task"]
        return {"status": "cancelled", "task": cancelled, "work": projected}

    def _team(self, team_id: str) -> dict[str, Any]:
        value = self.client.invoke(
            TEAM_RESOURCE,
            "get",
            {"profile_id": self.profile_id, "team_id": team_id},
        )
        if not isinstance(value, Mapping):
            raise KeyError("Team is unknown")
        return dict(value)

    def _state_action(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = self.client.invoke(
            TEAM_RESOURCE,
            "list",
            {"profile_id": self.profile_id},
        )
        exact = {"expected_revision": int(state.get("revision") or 0), **arguments}
        scope = {
            "service_pack_id": STATE_PACK_ID,
            "operation": f"team.state.{name}",
            "authority": "team.state.manage",
            "caller_id": "team.coordinator",
            "caller_pack_id": SERVICE_PACK_ID,
            "caller_function_id": f"team.coordinator.{name}",
            "profile_id": self.profile_id,
            "workspace_id": "",
            "session_id": "",
            "arguments": exact,
            "approval_required": False,
        }
        issued = self.client.invoke(AUTHORITY, "authorize", scope)
        if not issued.get("authorized"):
            raise PermissionError(str(issued.get("reason") or "Team state denied"))
        return self.client.invoke(
            TEAM_ACTION,
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
                "operation": f"team.coordinator.{name}",
                "authority": "team.coordinate",
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
            raise PermissionError(str(result.get("reason") or "Team control denied"))


_RUNTIMES: dict[str, TeamCoordinator] = {}
_LOCK = threading.Lock()


def create_team_runtime_resource(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create Team coordinator process status."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name != "status":
            raise ValueError(f"unknown Team runtime resource operation: {name}")
        return _runtime(client, payload).status()

    return operation


def create_team_control(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated Team coordinator controls."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return _runtime(client, payload).control(name, payload)

    return operation


def create_team_job_adapter(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create the `team.supervisor` scheduler job adapter."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name == "cancel":
            return {"status": "cancelled", "active_process": False}
        if name != "dispatch":
            raise ValueError(f"unknown Team job adapter operation: {name}")
        value = payload.get("payload")
        value = value if isinstance(value, Mapping) else {}
        arguments = {
            "team_id": str(value.get("team_id") or ""),
            "limit": max(1, min(100, int(value.get("limit") or 20))),
        }
        runtime = _runtime(client, payload)
        return _internal_control(runtime, client, "supervisor_tick", arguments)

    return operation


# Sunset compatibility names.  These are aliases to the same Team runtime;
# they do not create a second coordinator or authority path.
CompanyCoordinator = TeamCoordinator
create_company_runtime_resource = create_team_runtime_resource
create_company_control = create_team_control
create_company_job_adapter = create_team_job_adapter


def _internal_control(
    runtime: TeamCoordinator,
    client: Any,
    name: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    scope = {
        "service_pack_id": SERVICE_PACK_ID,
        "operation": f"team.coordinator.{name}",
        "authority": "team.coordinate",
        "caller_id": "team.job.adapter",
        "caller_pack_id": SERVICE_PACK_ID,
        "caller_function_id": "team.supervisor",
        "profile_id": runtime.profile_id,
        "workspace_id": "",
        "session_id": "",
        "arguments": dict(arguments),
        "approval_required": False,
    }
    issued = client.invoke(AUTHORITY, "authorize", scope)
    if not issued.get("authorized"):
        raise PermissionError(str(issued.get("reason") or "Team job denied"))
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


def _runtime(client: Any, payload: Mapping[str, Any]) -> TeamCoordinator:
    profile_id = str(payload.get("profile_id") or "default")
    with _LOCK:
        return _RUNTIMES.setdefault(profile_id, TeamCoordinator(client, profile_id))


def _control_arguments(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if name not in {"route_inbound", "dispatch_task", "supervisor_tick", "cancel"}:
        raise ValueError(f"unknown Team coordinator operation: {name}")
    team_id = str(payload.get("team_id") or "")
    if not team_id:
        raise ValueError("team_id is required")
    if name == "route_inbound":
        inbound_id = str(payload.get("inbound_id") or "")
        if not inbound_id:
            raise ValueError("inbound_id is required")
        return {"team_id": team_id, "inbound_id": inbound_id}
    if name == "supervisor_tick":
        return {
            "team_id": team_id,
            "limit": max(1, min(100, int(payload.get("limit") or 20))),
        }
    task_id = str(payload.get("task_id") or "")
    if not task_id:
        raise ValueError("task_id is required")
    return {"team_id": team_id, "task_id": task_id}


def _select_member(
    team: Mapping[str, Any],
    inbound: Mapping[str, Any],
) -> tuple[Mapping[str, Any] | None, str]:
    members = team.get("members")
    members = members if isinstance(members, Mapping) else {}
    enabled = {
        str(key): value
        for key, value in members.items()
        if isinstance(value, Mapping) and value.get("enabled")
    }
    mention_tokens = {item.casefold() for item in _MENTION.findall(str(inbound.get("text") or ""))}
    mentioned = [
        member
        for member in enabled.values()
        if mention_tokens.intersection(set(member.get("mentions") or []))
    ]
    if len(mentioned) == 1:
        return mentioned[0], "explicit_mention"
    if len(mentioned) > 1:
        return None, "ambiguous_mention"
    matched_routes = []
    for route_id in sorted(team.get("routes") or {}):
        route = team["routes"][route_id]
        if _route_matches(route, inbound):
            matched_routes.append(route)
    if len(matched_routes) == 1:
        member = enabled.get(str(matched_routes[0].get("target_member_id") or ""))
        return (member, "routing_rule") if member else (None, "route_target_unavailable")
    if len(matched_routes) > 1:
        return None, "ambiguous_routing_rule"
    settings = team.get("settings")
    settings = settings if isinstance(settings, Mapping) else {}
    fallback = enabled.get(str(settings.get("fallback_member_id") or ""))
    return (fallback, "explicit_fallback") if fallback else (None, "no_route")


def _route_matches(route: Mapping[str, Any], inbound: Mapping[str, Any]) -> bool:
    conditions = route.get("conditions")
    conditions = conditions if isinstance(conditions, Mapping) else {}
    metadata = inbound.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    actual = {
        "adapter_id": metadata.get("adapter_id"),
        "connector_id": metadata.get("connector_id"),
        "channel_id": inbound.get("channel_id"),
        "type": inbound.get("type"),
    }
    return all(str(actual.get(key) or "") == str(value) for key, value in conditions.items())


def _provider(
    providers: tuple[dict[str, Any], ...],
    instance_key: str,
) -> Mapping[str, Any]:
    matches = [
        item for item in providers if str(item.get("instance_key") or "") == instance_key
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one selected Team work adapter for {instance_key}; "
            f"found {len(matches)}"
        )
    return matches[0]


def _required(value: Any, key: str, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not isinstance(value.get(key), Mapping):
        raise KeyError(f"{label} is unknown")
    return dict(value[key])


def _result_transition(result: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(result, Mapping):
        return "failed", {"error": "Team work returned an invalid result"}
    agent = result.get("agent")
    agent = agent if isinstance(agent, Mapping) else result
    status = str(agent.get("status") or result.get("status") or "")
    if status in {"completed", "delivered"}:
        return "completed", {"result_reference": _result_reference(result)}
    if status in {"accepted", "running", "waiting"}:
        return "waiting", {"result_reference": _result_reference(result)}
    return "failed", {"error": str(agent.get("error") or "Team work failed")}


def _result_reference(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in ("conversation_id", "message_id", "delivery_id")
        if result.get(key)
    }


def _public_team(team: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": team["id"],
        "name": team["name"],
        "status": team["status"],
        "settings": team.get("settings") or {},
    }


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

