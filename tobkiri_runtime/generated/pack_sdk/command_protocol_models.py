"""Generated from command-protocol-v1.schema.json; do not edit."""

from typing import Any, Literal, TypedDict
from typing_extensions import NotRequired

CommandMode = Literal['chat', 'coding', 'agent']

class CommandInvocationRequest(TypedDict):
    command_ref: str
    args: dict[str, Any]
    invocation_id: str
    mode: CommandMode
    conversation_id: NotRequired[str]
    profile_id: NotRequired[str]
    catalog_revision: NotRequired[str]
    expected_revision: NotRequired[int]
    idempotency_key: NotRequired[str]
    client_sequence: NotRequired[int]
    approval_token: NotRequired[str]
    authority_request_id: NotRequired[str]
    authority_approval_token: NotRequired[str]

class CommandProtocolError(TypedDict):
    code: str
    message: str

class CommandInvocationResult(TypedDict):
    api_version: Literal['tobkiri.commands/v1']
    operation_id: str
    status: Literal['succeeded', 'failed', 'approval_required', 'cancelled']
    command_ref: str
    state_changes: list[dict[str, Any]]
    error: NotRequired[CommandProtocolError]
