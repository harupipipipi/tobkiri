from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class RunStatus(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_USER_INPUT = "waiting_user_input"
    COMPACTING = "compacting"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"
    RESUMABLE = "resumable"
    PLANNED = "planned"
    ERROR = "error"


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def json_loads(value: Any, fallback: Any = None) -> Any:
    if value in (None, ""):
        return fallback
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


@dataclass
class RunConfig:
    model: str = "default"
    system_prompt: Optional[str] = None
    runtime_profile_key: Optional[str] = None
    runtime_profile: dict[str, Any] = field(default_factory=dict)
    capability_graph: dict[str, Any] = field(default_factory=dict)
    tools: list[Any] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentRun:
    run_id: str
    session_key: str
    task: str
    status: str = RunStatus.CREATED.value
    conversation_id: Optional[str] = None
    agent_id: Optional[str] = None
    model: str = "default"
    system_prompt_id: Optional[str] = None
    system_prompt_hash: Optional[str] = None
    runtime_profile_key: Optional[str] = None
    runtime_profile_json: dict[str, Any] = field(default_factory=dict)
    capability_graph_json: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    parent_run_id: Optional[str] = None
    root_run_id: Optional[str] = None
    root_scope_id: Optional[str] = None
    agent_kind: str = "subagent"
    runtime_kind: str = "agent_run"
    subagent_role: Optional[str] = None
    placement_id: Optional[str] = None
    placement_revision: Optional[str] = None
    placement_map_id: Optional[str] = None
    effective_plan_hash: Optional[str] = None
    protocol_membership_json: list[str] = field(default_factory=list)
    current_transcript_id: Optional[str] = None
    compaction_count: int = 0
    heartbeat_at: Optional[str] = None
    error: Optional[str] = None
    result_json: Any = None
    execution_json: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["execution_id"] = self.run_id
        return data


@dataclass
class AgentRunStep:
    run_id: str
    step_no: int
    step_type: str
    status: str = "completed"
    content_json: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentMessage:
    run_id: str
    transcript_id: str
    role: str
    content_json: Any
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    token_estimate: int = 0
    created_at: str = ""


@dataclass
class ToolCallRecord:
    tool_call_id: str
    run_id: str
    tool_name: str
    arguments_json: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    approval_id: Optional[str] = None
    result_json: Any = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


@dataclass
class ToolResultRecord:
    tool_call_id: str
    run_id: str
    tool_name: str
    result_json: Any
    is_error: bool = False
    completed_at: Optional[str] = None


@dataclass
class ApprovalRequest:
    approval_id: str
    run_id: str
    tool_call_id: str
    reviewer: str = "user"
    status: str = "pending"
    reason: str = ""
    requested_at: str = ""
    decided_at: Optional[str] = None
    decision_json: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeProfileSnapshot:
    runtime_profile_key: Optional[str] = None
    runtime_profile_json: dict[str, Any] = field(default_factory=dict)
    capability_graph_json: dict[str, Any] = field(default_factory=dict)
