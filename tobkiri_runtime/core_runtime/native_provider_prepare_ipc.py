"""Launcher-private IPC for the original Host's local owner operations."""

from __future__ import annotations

import ctypes
import json
import math
import os
import re
import socket
import struct
import sys
import time
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache, partial
from typing import Any, Literal, overload

from core_runtime.host_provider_backend_v4 import HostPublicationUnconfirmed
from core_runtime.process_identity import process_start_identity
from core_runtime.provider_prepare_native_v4 import (
    MINIMUM_PREPARE_MEMORY,
    NativeProviderPrepareBorrow,
    NativeProviderPreparePlanner,
    NativeProviderRegistryRequiresLegacyRead,
)
from tobkiri_host.models import RequestContext
from tobkiri_protocol.canonical import (
    MAX_CANONICAL_JSON_BYTES,
    MAX_SAFE_INTEGER,
    canonical_json,
    strict_loads,
)

_SOCKET_ENV = "TOBKIRI_NATIVE_PROVIDER_PREPARE_SOCKET"
_OWNER_ENV = "TOBKIRI_NATIVE_PROVIDER_PREPARE_OWNER"
_APPEND_ENV = "TOBKIRI_NATIVE_CONVERSATION_APPEND"
_SAVED_TURN_READ_ENV = "TOBKIRI_NATIVE_SAVED_TURN_READ"
_SAVED_TURN_START_ENV = "TOBKIRI_NATIVE_SAVED_TURN_START"
_SAVED_ORCHESTRATION_ENV = "TOBKIRI_NATIVE_SAVED_ORCHESTRATION"
_APPLICATION_PRESENTATION_ENV = "TOBKIRI_NATIVE_APPLICATION_PRESENTATION"
_SAVED_LOCAL_CHAT_ENV = "TOBKIRI_NATIVE_SAVED_LOCAL_CHAT"
_AI_ROUTING_ENV = "TOBKIRI_NATIVE_AI_ROUTING"
_AI_STREAM_RESULT_ENV = "TOBKIRI_NATIVE_AI_STREAM_RESULT"
_AI_USAGE_COST_ENV = "TOBKIRI_NATIVE_AI_USAGE_COST"
_AI_TOOL_INTENTS_ENV = "TOBKIRI_NATIVE_AI_TOOL_INTENTS"
_AI_PIPELINE_ENV = "TOBKIRI_NATIVE_AI_PIPELINE"
_AI_GATEWAY_RESULT_ENV = "TOBKIRI_NATIVE_AI_GATEWAY_RESULT"
_AI_GATEWAY_POLICY_ENV = "TOBKIRI_NATIVE_AI_GATEWAY_POLICY"
_OWNER = re.compile(r"([0-9]{1,10}):([0-9]{1,20}):([0-9]{1,6})")
_INSTANCE = re.compile(r"[0-9a-f]{32}")
_MODEL_RESOLVE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_TURN_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_TURN_INPUT_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_TURN_REFERENCE_LIMIT = 2 * 1024
_SAVED_TURN_REQUEST_ID = re.compile(r"saved-turn\.[0-9a-f]{64}")
_SAVED_MESSAGE_ID = re.compile(r"message:[0-9a-f]{64}")
_SOL_LOCAL = 0
_LOCAL_PEERPID = 2
_SOCKET_PATH_MAX = 103  # Darwin sockaddr_un.sun_path includes the terminating NUL.
_FRAME = struct.Struct("!BI")
_START, _CHECK, _LIVE = 1, 2, 3
_PLAN, _FAILURE, _CANCEL = 7, 8, 9
_INAPPLICABLE = 10
_PUBLISH_CHECK, _PUBLISH_PERMIT = 11, 12
_START_APPEND = 13
_BODY_READY, _ORCHESTRATION_INPUT = 14, 15
_SAVED_LOCAL_CHAT_INPUT, _DISPATCH_CHECK, _DISPATCH_PERMIT = 16, 17, 18
_PROJECTED_LOCAL_CHAT = 19
_JSON_LIMIT = 16 * 1024
_APPEND_START_LIMIT = 1024 * 1024
_OWNER_RESULT_LIMIT = 1024 * 1024
_TURN_START_RESULT_LIMIT = _OWNER_RESULT_LIMIT + 128
# The Host borrows the same bridge allowance as the Registry owner routes.
# The managed Launcher's Wasm instance has its own physical memory cap.
CONVERSATION_GET_MINIMUM_MEMORY = MINIMUM_PREPARE_MEMORY
CONVERSATION_APPEND_MINIMUM_MEMORY = MINIMUM_PREPARE_MEMORY
CONVERSATION_CONFIRMATION_MINIMUM_MEMORY = MINIMUM_PREPARE_MEMORY
MODEL_RESOLVE_MINIMUM_MEMORY = MINIMUM_PREPARE_MEMORY
AI_ROUTING_MINIMUM_MEMORY = MINIMUM_PREPARE_MEMORY
SAVED_RECEIPT_MINIMUM_MEMORY = MINIMUM_PREPARE_MEMORY
SAVED_TURN_READ_MINIMUM_MEMORY = MINIMUM_PREPARE_MEMORY
SAVED_TURN_START_MINIMUM_MEMORY = MINIMUM_PREPARE_MEMORY
SAVED_TURN_SETTLEMENT_MINIMUM_MEMORY = MINIMUM_PREPARE_MEMORY
SAVED_ORCHESTRATION_MINIMUM_MEMORY = MINIMUM_PREPARE_MEMORY
APPLICATION_PRESENTATION_MINIMUM_MEMORY = MINIMUM_PREPARE_MEMORY
_APPLICATION_PRESENTATION_INPUT_LIMIT = 128 * 1024
SAVED_LOCAL_CHAT_MINIMUM_MEMORY = MINIMUM_PREPARE_MEMORY
SAVED_LOCAL_CHAT_MAX_INPUT_BYTES = 1024 * 1024
SAVED_LOCAL_CHAT_MAX_RESULT_BYTES = 4 * 1024 * 1024
_AI_ROUTING_LIMIT = 1024 * 1024
AI_USAGE_COST_INPUT_FIELDS = frozenset({
    "usage", "pricing", "usage_provenance", "pricing_revision",
})
AI_TOOL_INTENTS_INPUT_FIELDS = frozenset({"request_id", "intents"})
_AI_GATEWAY_RESULT_INPUT_FIELDS = frozenset({
    "role", "value", "request_id", "candidate",
})
_AI_GATEWAY_STREAM_RESULT_FIELDS = frozenset({"tool_intents", "events", "attempts"})
_AI_GATEWAY_POLICY_INPUT_FIELDS = {
    "model-profile": frozenset({
        "phase", "identifier", "resolved_profile_id", "model_id",
        "profile_requirements", "request_requirements",
        "profile_parameters", "request_parameters",
    }),
    "route-requirement": frozenset({"phase", "requirements"}),
}
_AI_GATEWAY_REQUIREMENT_LIST_FIELDS = frozenset({"modalities", "capabilities"})
_AI_GATEWAY_REQUIREMENT_BOOL_FIELDS = frozenset({"tool_calling", "thinking"})
_AI_GATEWAY_REQUIREMENT_OPTIONAL_STR_FIELDS = frozenset({
    "data_residency", "preferred_model_id", "preferred_provider_id",
    "preferred_provider_instance_id",
})
_AI_GATEWAY_REQUIREMENT_RESULT_FIELDS = (
    _AI_GATEWAY_REQUIREMENT_LIST_FIELDS | _AI_GATEWAY_REQUIREMENT_BOOL_FIELDS
    | _AI_GATEWAY_REQUIREMENT_OPTIONAL_STR_FIELDS
    | {"minimum_context", "request_surface", "maximum_cost", "health_max_age"}
)
_AI_GATEWAY_CANDIDATE_FIELDS = frozenset({
    "model_id", "provider_instance_id", "catalog_provider_instance_id",
    "catalog_revision",
})
AI_PIPELINE_PREPARE_REQUIRED_FIELDS = frozenset({"request_id", "decision_time"})
AI_PIPELINE_FAILOVER_INPUT_FIELDS = frozenset({
    "allow_failover", "idempotency_key", "tools", "error_code", "attempt",
    "candidate_count", "deadline", "decision_time",
})
_AI_PIPELINE_PREPARE_RESULT_FIELDS = frozenset({
    "request_id", "decision_time", "deadline", "messages", "input",
    "parameters", "tools", "requirements", "credential_handle", "idempotency_key",
    "allow_failover", "policy_revision", "conversation_id", "profile_id",
    "model_profile_id", "model_reference",
})
_AI_PIPELINE_FAILOVER_CHECKS = (
    "explicitly_allowed", "idempotency_bound", "tool_free", "retryable_error",
    "candidate_available", "deadline_remaining",
)
# Reserve START identity space even if each ID character needs a JSON escape.
CONVERSATION_GET_MAX_ID_CHARS = _JSON_LIMIT // 8
SAVED_RECEIPT_MAX_ID_CHARS = _JSON_LIMIT // 8
SAVED_TURN_READ_MAX_ID_CHARS = 256
_MODEL_SAVE_MAX_NODES = 100_000
_OWNER_MAX_INTEGER_DIGITS = 4300
_FAILURES = frozenset({
    b"denied", b"approval-required", b"cancelled", b"not-found", b"invalid",
    b"limit-exceeded", b"conflict", b"unavailable", b"internal",
})
_LEGACY_READ_FAILURE = b"legacy-read-required"
_CONVERSATION_LEGACY_READ_FAILURE = b"requires-legacy-read"
_LEGACY_REQUEST_FAILURE = b"legacy-request"
_LOCAL_CHAT_POLICY_FAILURES = frozenset({
    b"policy-model-denied", b"policy-model-invalid", b"policy-parameters-invalid",
})
_PUBLICATION_UNCONFIRMED = b"publication-unconfirmed"
_INBOUND_LIMITS = {
    _CHECK: 0,
    _INAPPLICABLE: 0,
}
_POLL_SECONDS = 0.05
_CLEANUP_SECONDS = 10.0
_NANOS_PER_SECOND = 1_000_000_000
_U64_MAX = (1 << 64) - 1
_CONNECTION_ERROR = "native Provider preparation connection failed"


class NativeProviderPrepareUnavailable(RuntimeError):
    """The authenticated Launcher declined this request before any native work."""


class NativeModelSaveFailure(RuntimeError):
    """The Launcher reported a model save failure before publication permission."""

    def __init__(self, code: str) -> None:
        super().__init__(f"native Model Settings save failed ({code})")
        self.code = code


class NativeConversationGetFailure(RuntimeError):
    """The Launcher returned a typed failure from the conversation owner."""

    def __init__(self, code: str) -> None:
        super().__init__(f"native Conversation GET failed ({code})")
        self.code = code


class NativeConversationAppendFailure(RuntimeError):
    """The Launcher returned a typed append failure before publication permission."""

    def __init__(self, code: str) -> None:
        super().__init__(f"native Conversation append failed ({code})")
        self.code = code


class NativeConversationAppendPublicationUnconfirmed(RuntimeError):
    """A permitted append has no confirmed Conversation owner result."""


class NativeModelResolveFailure(RuntimeError):
    """The Launcher returned a typed failure from the Model Registry owner."""

    def __init__(self, code: str) -> None:
        super().__init__(f"native Model Resolve failed ({code})")
        self.code = code


class NativeAiRoutingFailure(RuntimeError):
    """A typed routing failure that does not permit compatibility fallback."""

    def __init__(self, code: str) -> None:
        super().__init__(f"native AI routing failed ({code})")
        self.code = code


class NativeSavedReceiptFailure(RuntimeError):
    """The Launcher returned a typed failure from the saved receipt owner."""

    def __init__(self, code: str) -> None:
        super().__init__(f"native Saved Receipt lookup failed ({code})")
        self.code = code


class NativeSavedTurnReadFailure(RuntimeError):
    """The Launcher returned a typed failure from the saved Turn owner."""

    def __init__(self, code: str) -> None:
        super().__init__(f"native saved Turn read failed ({code})")
        self.code = code


class NativeSavedTurnStartFailure(RuntimeError):
    """The Launcher returned a typed saved Turn failure before any permit."""

    def __init__(self, code: str) -> None:
        super().__init__(f"native saved Turn start failed ({code})")
        self.code = code


class NativeSavedOrchestrationFailure(RuntimeError):
    """The Launcher returned a typed failure for a pure orchestration step."""

    def __init__(self, code: str) -> None:
        super().__init__(f"native Saved Orchestration step failed ({code})")
        self.code = code


class NativeSavedLocalChatFailure(RuntimeError):
    """A typed saved local chat failure that does not permit fallback."""

    def __init__(self, code: str) -> None:
        super().__init__(f"native saved local chat failed ({code})")
        self.code = code


class NativeSavedLocalChatDispatchUnconfirmed(RuntimeError):
    """The Launcher could have dispatched a POST without confirmed cleanup."""


class NativeModelSavePublicationUnconfirmed(RuntimeError):
    """A permitted Model Registry publication has no confirmed owner result."""


class NativeSavedTurnSettlementFailure(RuntimeError):
    """The Launcher reported a saved-turn settlement failure before its permit."""

    def __init__(self, code: str) -> None:
        super().__init__(f"native saved-turn settlement failed ({code})")
        self.code = code


class NativeSavedTurnSettlementPublicationUnconfirmed(RuntimeError):
    """A permitted settlement has no confirmed Turn owner result."""


class NativeConversationConfirmationFailure(RuntimeError):
    """The Launcher reported a Conversation failure before publication permission."""

    def __init__(self, code: str) -> None:
        super().__init__(f"native Conversation confirmation failed ({code})")
        self.code = code


class NativeConversationConfirmationPublicationUnconfirmed(RuntimeError):
    """A permitted confirmation has no confirmed Conversation owner result."""


class _ProtocolError(RuntimeError):
    """A fixed transport or protocol failure without peer-supplied details."""


@dataclass(frozen=True)
class NativeModelSettingsRequest:
    """Borrow the original settings request's capture and live guard."""

    context: RequestContext
    runtime_instance: str
    deadline_monotonic: float
    assert_current: Callable[[], None]


@dataclass(frozen=True)
class NativeModelSaveRequest:
    """Borrow the signed Settings request, Provider projection, and live guard."""

    context: RequestContext
    runtime_instance: str
    deadline_monotonic: float
    assert_current: Callable[[], None]
    request: Mapping[str, Any]
    provider: Mapping[str, Any]
    mark_publication_possible: Callable[[], None]


ConversationGetCaller = Literal["shell", "saved-turn", "saved-orchestration"]


@dataclass(frozen=True)
class NativeConversationGetRequest:
    """Borrow the original Conversation GET capture and live guard."""

    context: RequestContext
    runtime_instance: str
    deadline_monotonic: float
    assert_current: Callable[[], None]
    caller: ConversationGetCaller
    conversation_id: str


@dataclass(frozen=True)
class NativeConversationAppendRequest:
    """Borrow the signed Conversation append payload and its live guard."""

    context: RequestContext
    runtime_instance: str
    deadline_monotonic: float
    assert_current: Callable[[], None]
    request: Mapping[str, Any]
    mark_publication_possible: Callable[[], None]


SavedReceiptCaller = Literal["saved-turn", "saved-reconcile"]


@dataclass(frozen=True)
class NativeSavedReceiptRequest:
    """Borrow the original saved receipt lookup's capture and live guard."""

    context: RequestContext
    runtime_instance: str
    deadline_monotonic: float
    assert_current: Callable[[], None]
    caller: SavedReceiptCaller
    turn_id: str


@dataclass(frozen=True)
class NativeSavedTurnReadRequest:
    """Borrow the original saved Turn read's capture and live guard."""

    context: RequestContext
    runtime_instance: str
    deadline_monotonic: float
    assert_current: Callable[[], None]
    caller: SavedReceiptCaller
    turn_id: str


SavedTurnStartAction = Literal["begin", "claim"]


@dataclass(frozen=True)
class NativeSavedTurnStartRequest:
    """Borrow the original saved Turn start binding and publication guard."""

    context: RequestContext
    runtime_instance: str
    deadline_monotonic: float
    assert_current: Callable[[], None]
    action: SavedTurnStartAction
    turn_id: str
    conversation_id: str
    conversation_revision: int
    input_digest: str
    source_digest: str
    mark_publication_possible: Callable[[], None]


@dataclass(frozen=True)
class NativeSavedOrchestrationRequest:
    """Borrow the signed Saved Orchestration invocation and step input."""

    context: RequestContext
    runtime_instance: str
    deadline_monotonic: float
    assert_current: Callable[[], None]
    input: Mapping[str, Any]


ApplicationPresentationRole = Literal["settings", "catalog", "commands"]


@dataclass(frozen=True)
class NativeApplicationPresentationRequest:
    """Borrow the live presentation operation selected by the Host."""

    context: RequestContext
    runtime_instance: str
    deadline_monotonic: float
    assert_current: Callable[[], None]
    role: ApplicationPresentationRole
    input: Mapping[str, Any]


SavedLocalChatRole = Literal["generate", "stream"]


@dataclass(frozen=True)
class NativeSavedLocalChatRequest:
    """Borrow the saved invocation, local chat input, and live dispatch guard."""

    context: RequestContext
    runtime_instance: str
    deadline_monotonic: float
    assert_current: Callable[[], None]
    role: SavedLocalChatRole
    connection_id: str
    record_revision: int
    endpoint: str
    input: bytes
    assert_dispatch: Callable[[], None]
    mark_dispatch_possible: Callable[[], None]
    compile_request: bool = False
    compile_policy: bool = False


@dataclass(frozen=True)
class NativeSavedTurnSettlementRequest:
    """Borrow the signed Turn invocation and its verified result reference."""

    context: RequestContext
    runtime_instance: str
    deadline_monotonic: float
    assert_current: Callable[[], None]
    caller: SavedReceiptCaller
    turn_id: str
    input_digest: str
    expected_revision: int | None
    result_reference: Mapping[str, Any]
    mark_publication_possible: Callable[[], None]


@dataclass(frozen=True)
class NativeConversationConfirmationRequest:
    """Borrow the completion invocation and its verified durable Turn projection."""

    context: RequestContext
    runtime_instance: str
    deadline_monotonic: float
    assert_current: Callable[[], None]
    caller: SavedReceiptCaller
    terminal: Mapping[str, Any]
    mark_publication_possible: Callable[[], None]


ModelResolveRole = Literal["quote-generate", "quote-stream", "generate", "stream"]
AiPipelineStep = Literal["prepare", "failover"]


@dataclass(frozen=True)
class NativeModelResolveRequest:
    """Borrow the original Model Resolve capture and live guard."""

    context: RequestContext
    runtime_instance: str
    deadline_monotonic: float
    assert_current: Callable[[], None]
    role: ModelResolveRole
    identifier: str


@dataclass(frozen=True)
class NativeAiRoutingRequest:
    """Borrow a live signed Gateway invocation or one of its children."""

    context: RequestContext
    runtime_instance: str
    deadline_monotonic: float
    assert_current: Callable[[], None]
    role: ModelResolveRole
    input: Mapping[str, Any]
    stream_result: bool = False
    usage_cost: bool = False
    tool_intents: bool = False
    pipeline: AiPipelineStep | None = None
    gateway_result: bool = False
    gateway_policy: bool = False


_NativeBorrow = (
    NativeProviderPrepareBorrow
    | NativeModelSettingsRequest
    | NativeModelSaveRequest
    | NativeConversationGetRequest
    | NativeConversationAppendRequest
    | NativeSavedReceiptRequest
    | NativeSavedTurnReadRequest
    | NativeSavedTurnStartRequest
    | NativeSavedOrchestrationRequest
    | NativeApplicationPresentationRequest
    | NativeSavedTurnSettlementRequest
    | NativeConversationConfirmationRequest
    | NativeModelResolveRequest
    | NativeAiRoutingRequest
)
_NativeOperation = Literal[
    "provider_prepare", "model_list", "model_save", "conversation_get",
    "conversation_receipt", "model_resolve", "turn_start", "turn_settlement",
    "conversation_confirmation", "conversation_append", "saved_orchestration", "ai_route",
    "application_presentation",
]


@dataclass(frozen=True)
class _Launcher:
    address: bytes
    pid: int
    start_identity: str

    def assert_parent(self) -> None:
        if os.getppid() != self.pid:
            raise PermissionError("native Provider preparation Launcher changed")

    def assert_start(self) -> None:
        self.assert_parent()
        identity = process_start_identity(self.pid)
        if identity.state != "live" or identity.identity != self.start_identity:
            raise PermissionError("native Provider preparation Launcher changed")


@dataclass(frozen=True)
class NativeSavedLocalChatExecutor:
    """Use the local chat capabilities captured from the managed Launcher."""

    _launcher: _Launcher
    _project_response: bool
    supports_request_compilation: bool
    supports_policy_compilation: bool = False

    def __call__(
        self, borrow: NativeSavedLocalChatRequest,
    ) -> bytes | dict[str, Any]:
        """Exchange one request in its negotiated input form."""
        if borrow.compile_request and not self.supports_request_compilation:
            raise NativeSavedLocalChatFailure("invalid")
        if borrow.compile_policy and not self.supports_policy_compilation:
            raise NativeSavedLocalChatFailure("invalid")
        return _invoke_saved_local_chat(
            self._launcher, borrow, project_response=self._project_response,
        )


@lru_cache(maxsize=1)
def _getpeereid() -> Any:
    getpeereid = ctypes.CDLL("/usr/lib/libSystem.B.dylib").getpeereid
    getpeereid.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    getpeereid.restype = ctypes.c_int
    return getpeereid


def _authenticate(connection: socket.socket, launcher: _Launcher) -> None:
    uid, gid = ctypes.c_uint32(), ctypes.c_uint32()
    peer_error = "native Provider preparation peer could not be verified"
    try:
        getpeereid = _getpeereid()
        result = getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid))
        pid_bytes = connection.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, 4)
        pid = struct.unpack("=i", pid_bytes)[0]
    except (AttributeError, OSError, struct.error):
        raise PermissionError(peer_error) from None
    if result != 0 or uid.value != os.geteuid() or pid != launcher.pid:
        raise PermissionError(peer_error)
    launcher.assert_start()


def _capture_launcher() -> _Launcher | None:
    if sys.platform != "darwin":
        return None
    address_env = os.environ.get(_SOCKET_ENV)
    owner_env = os.environ.get(_OWNER_ENV)
    if address_env is None and owner_env is None:
        return None
    address = os.fsencode(address_env or "")
    owner = _OWNER.fullmatch(owner_env or "")
    if (
        owner is None
        or not address.startswith(b"/")
        or len(address) > _SOCKET_PATH_MAX
        or b"\0" in address
    ):
        raise _ProtocolError("native Provider preparation launch binding is invalid")
    pid, seconds, microseconds = map(int, owner.groups())
    if not (1 < pid <= (1 << 31) - 1 and 0 < seconds <= _U64_MAX):
        raise _ProtocolError("native Provider preparation launch binding is invalid")
    return _Launcher(address, pid, f"darwin:{pid}:{seconds}:{microseconds:06d}")


def capture_launcher_provider_prepare_planner() -> NativeProviderPreparePlanner | None:
    """Capture the managed Launcher's fixed endpoint when the Host is composed."""
    launcher = _capture_launcher()
    if launcher is None:
        return None
    return NativeProviderPreparePlanner(
        MINIMUM_PREPARE_MEMORY, partial(_invoke, launcher, operation="provider_prepare"),
    )


def capture_launcher_model_settings_reader() -> (
    Callable[[NativeModelSettingsRequest], Mapping[str, Any]] | None
):
    """Capture the managed Launcher's fixed endpoint for model settings reads."""
    launcher = _capture_launcher()
    if launcher is None:
        return None
    return partial(_invoke, launcher, operation="model_list")


def capture_launcher_conversation_get_reader() -> (
    Callable[[NativeConversationGetRequest], Mapping[str, Any]] | None
):
    """Capture the managed Launcher's fixed endpoint for a conversation read."""
    launcher = _capture_launcher()
    if launcher is None:
        return None
    return partial(_invoke, launcher, operation="conversation_get")


def capture_launcher_conversation_append_writer() -> (
    Callable[[NativeConversationAppendRequest], Mapping[str, Any]] | None
):
    """Capture the managed Launcher's fixed endpoint for a saved append."""
    if os.environ.get(_APPEND_ENV) != "1":
        return None
    launcher = _capture_launcher()
    if launcher is None:
        return None
    return partial(_invoke, launcher, operation="conversation_append")


def capture_launcher_saved_receipt_reader() -> (
    Callable[[NativeSavedReceiptRequest], Mapping[str, Any]] | None
):
    """Capture the managed Launcher's fixed endpoint for a saved receipt lookup."""
    launcher = _capture_launcher()
    if launcher is None:
        return None
    return partial(_invoke, launcher, operation="conversation_receipt")


def capture_launcher_saved_turn_reader() -> (
    Callable[[NativeSavedTurnReadRequest], dict[str, Any] | None] | None
):
    """Capture the managed Launcher's opted-in endpoint for saved Turn reads."""
    if os.environ.get(_SAVED_TURN_READ_ENV) != "1":
        return None
    launcher = _capture_launcher()
    if launcher is None:
        return None

    def read(borrow: NativeSavedTurnReadRequest) -> dict[str, Any] | None:
        return _invoke(launcher, borrow, operation="turn_read")

    return read


def capture_launcher_saved_orchestration_step() -> (
    Callable[[NativeSavedOrchestrationRequest], Mapping[str, Any]] | None
):
    """Capture the managed Launcher's opted-in endpoint for pure saved steps."""
    if os.environ.get(_SAVED_ORCHESTRATION_ENV) != "1":
        return None
    launcher = _capture_launcher()
    if launcher is None:
        return None
    return partial(_invoke, launcher, operation="saved_orchestration")


def capture_launcher_application_presentation() -> (
    tuple[str, Callable[[NativeApplicationPresentationRequest], dict[str, Any]]] | None
):
    """Capture the Launcher-attested presentation manifest and authenticated port."""
    manifest = os.environ.get(_APPLICATION_PRESENTATION_ENV)
    if manifest is None or _TURN_INPUT_DIGEST.fullmatch(manifest) is None:
        return None
    launcher = _capture_launcher()
    if launcher is None:
        return None
    return manifest, partial(_invoke, launcher, operation="application_presentation")


def capture_launcher_ai_routing_evaluator() -> (
    Callable[[NativeAiRoutingRequest], Mapping[str, Any]] | None
):
    """Capture the managed Launcher's opted-in endpoint for saved AI routing."""
    if os.environ.get(_AI_ROUTING_ENV) != "1":
        return None
    launcher = _capture_launcher()
    if launcher is None:
        return None
    return partial(_invoke_ai_route, launcher, stream_result=False)


def capture_launcher_ai_stream_result_normalizer() -> (
    Callable[[NativeAiRoutingRequest], Mapping[str, Any]] | None
):
    """Capture the managed Launcher's opted-in endpoint for stream normalization."""
    if os.environ.get(_AI_STREAM_RESULT_ENV) != "1":
        return None
    launcher = _capture_launcher()
    if launcher is None:
        return None
    return partial(_invoke_ai_route, launcher, stream_result=True)


def capture_launcher_ai_usage_cost_calculator() -> (
    Callable[[NativeAiRoutingRequest], Mapping[str, Any]] | None
):
    """Capture the managed Launcher's opted-in endpoint for Usage Cost."""
    if os.environ.get(_AI_USAGE_COST_ENV) != "1":
        return None
    launcher = _capture_launcher()
    if launcher is None:
        return None
    return partial(_invoke_ai_route, launcher, stream_result=False, usage_cost=True)


def capture_launcher_ai_tool_intents_normalizer() -> (
    Callable[[NativeAiRoutingRequest], Mapping[str, Any]] | None
):
    """Capture the managed Launcher's opted-in endpoint for inert tool intents."""
    if os.environ.get(_AI_TOOL_INTENTS_ENV) != "1":
        return None
    launcher = _capture_launcher()
    if launcher is None:
        return None
    return partial(_invoke_ai_route, launcher, stream_result=False, tool_intents=True)


def capture_launcher_ai_pipeline_evaluator() -> (
    Callable[[NativeAiRoutingRequest], Mapping[str, Any]] | None
):
    """Capture the managed Launcher's opted-in endpoint for pure pipeline steps."""
    if os.environ.get(_AI_PIPELINE_ENV) != "1":
        return None
    launcher = _capture_launcher()
    if launcher is None:
        return None
    return partial(_invoke_ai_route, launcher, stream_result=False, pipeline=True)


def capture_launcher_ai_gateway_result_projector() -> (
    Callable[[NativeAiRoutingRequest], Mapping[str, Any]] | None
):
    """Capture the Launcher's opted-in endpoint for Gateway result projection."""
    if os.environ.get(_AI_GATEWAY_RESULT_ENV) != "1":
        return None
    launcher = _capture_launcher()
    if launcher is None:
        return None
    return partial(_invoke_ai_route, launcher, stream_result=False, gateway_result=True)


def capture_launcher_ai_gateway_policy_projector() -> (
    Callable[[NativeAiRoutingRequest], Mapping[str, Any]] | None
):
    """Capture the Launcher's opted-in endpoint for Gateway model and route policy."""
    if os.environ.get(_AI_GATEWAY_POLICY_ENV) != "1":
        return None
    launcher = _capture_launcher()
    if launcher is None:
        return None
    return partial(_invoke_ai_route, launcher, stream_result=False, gateway_policy=True)


def capture_launcher_saved_local_chat_executor() -> NativeSavedLocalChatExecutor | None:
    """Capture the managed Launcher's opted-in endpoint for local chat."""
    version = os.environ.get(_SAVED_LOCAL_CHAT_ENV)
    if version not in ("1", "2", "3", "4"):
        return None
    launcher = _capture_launcher()
    if launcher is None:
        return None
    return NativeSavedLocalChatExecutor(
        launcher, version in ("2", "3", "4"),
        supports_request_compilation=version in ("3", "4"),
        supports_policy_compilation=version == "4",
    )


def is_native_saved_turn_read_identifier(value: object) -> bool:
    """Check that a Turn resource identifier can cross the native wire unchanged."""
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= SAVED_TURN_READ_MAX_ID_CHARS
        or value.strip() != value
    ):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def capture_launcher_saved_turn_start_writer() -> (
    Callable[[NativeSavedTurnStartRequest], Mapping[str, Any]] | None
):
    """Capture the managed Launcher's opted-in endpoint for saved Turn start."""
    if os.environ.get(_SAVED_TURN_START_ENV) != "1":
        return None
    launcher = _capture_launcher()
    if launcher is None:
        return None
    return partial(_invoke, launcher, operation="turn_start")


def is_native_saved_turn_start_identifier(value: object) -> bool:
    """Check a Turn start identifier against the durable owner's grammar."""
    return _matches(_TURN_IDENTIFIER, value)


def capture_launcher_saved_turn_settlement_writer() -> (
    Callable[[NativeSavedTurnSettlementRequest], Mapping[str, Any]] | None
):
    """Capture the managed Launcher's fixed endpoint for final turn settlement."""
    launcher = _capture_launcher()
    if launcher is None:
        return None
    return partial(_invoke, launcher, operation="turn_settlement")


def capture_launcher_conversation_confirmation_writer() -> (
    Callable[[NativeConversationConfirmationRequest], Mapping[str, Any]] | None
):
    """Capture the managed Launcher's fixed endpoint for Conversation confirmation."""
    launcher = _capture_launcher()
    if launcher is None:
        return None
    return partial(_invoke, launcher, operation="conversation_confirmation")


def capture_launcher_model_resolve_reader() -> (
    Callable[[NativeModelResolveRequest], Mapping[str, Any]] | None
):
    """Capture the managed Launcher's fixed endpoint for a private model read."""
    launcher = _capture_launcher()
    if launcher is None:
        return None
    return partial(_invoke, launcher, operation="model_resolve")


def is_canonical_model_resolve_identifier(value: object) -> bool:
    """Check that an identifier needs no legacy coercion or trimming."""
    return (
        isinstance(value, str)
        and _MODEL_RESOLVE_IDENTIFIER.fullmatch(value) is not None
    )


def model_save_from_environment() -> (
    Callable[[NativeModelSaveRequest], Mapping[str, Any]] | None
):
    """Capture the managed Launcher's fixed endpoint for model settings saves."""
    launcher = _capture_launcher()
    if launcher is None:
        return None
    return partial(_invoke, launcher, operation="model_save")


capture_launcher_model_save_writer = model_save_from_environment


def _start_payload(
    borrow: _NativeBorrow | NativeSavedLocalChatRequest,
    deadline_ns: int,
    *,
    body_input_bytes: int = 0,
    project_local_chat_response: bool = False,
) -> bytes:
    context = borrow.context
    runtime_instance = borrow.runtime_instance
    if (
        not isinstance(runtime_instance, str)
        or _INSTANCE.fullmatch(runtime_instance) is None
        or type(context.security_epoch) is not int
        or not 0 < context.security_epoch <= _U64_MAX
    ):
        raise _ProtocolError("native Provider preparation binding is invalid")
    frame: dict[str, Any] = {
        "identity": {
            "profile_id": context.profile_id,
            "profile_revision": context.profile_revision,
            "activation_id": context.activation_id,
            "plan_digest": context.plan_digest,
        },
        "security_epoch": context.security_epoch,
        "runtime_instance": runtime_instance,
        "deadline_ns": deadline_ns,
    }
    if isinstance(borrow, NativeModelSettingsRequest):
        frame["model_list"] = {}
    elif isinstance(borrow, NativeModelSaveRequest):
        if not isinstance(borrow.request, Mapping) or not isinstance(
            borrow.provider, Mapping,
        ):
            raise _ProtocolError("native Model Settings save inputs must be objects")
        frame["model_save"] = {
            "request": dict(borrow.request),
            "provider": dict(borrow.provider),
        }
    elif isinstance(borrow, NativeConversationGetRequest):
        if borrow.caller not in (
            "shell", "saved-turn", "saved-orchestration",
        ) or not isinstance(borrow.conversation_id, str):
            raise _ProtocolError("native Conversation GET inputs are invalid")
        frame["conversation_get"] = {
            "caller": borrow.caller,
            "conversation_id": borrow.conversation_id,
        }
    elif isinstance(borrow, NativeConversationAppendRequest):
        frame["conversation_append"] = _conversation_append_payload(borrow)
    elif isinstance(borrow, NativeSavedReceiptRequest):
        if (
            type(borrow.caller) is not str
            or borrow.caller not in ("saved-turn", "saved-reconcile")
            or not isinstance(borrow.turn_id, str)
            or len(borrow.turn_id) > SAVED_RECEIPT_MAX_ID_CHARS
        ):
            raise _ProtocolError("native Saved Receipt lookup inputs are invalid")
        frame["conversation_receipt"] = {
            "caller": borrow.caller,
            "turn_id": borrow.turn_id,
        }
    elif isinstance(borrow, NativeSavedTurnReadRequest):
        if (
            type(borrow.caller) is not str
            or borrow.caller not in ("saved-turn", "saved-reconcile")
            or not is_native_saved_turn_read_identifier(borrow.turn_id)
        ):
            raise _ProtocolError("native saved Turn read inputs are invalid")
        frame["turn_read"] = {"caller": borrow.caller, "turn_id": borrow.turn_id}
    elif isinstance(borrow, NativeSavedTurnStartRequest):
        frame["turn_start"] = _turn_start_payload(borrow)
    elif isinstance(borrow, NativeSavedOrchestrationRequest):
        if not 0 < body_input_bytes <= MAX_CANONICAL_JSON_BYTES:
            raise _ProtocolError("native Saved Orchestration input size is invalid")
        frame["saved_orchestration"] = {"input_bytes": body_input_bytes}
    elif isinstance(borrow, NativeApplicationPresentationRequest):
        if (
            borrow.role not in ("settings", "catalog", "commands")
            or not 0 < body_input_bytes <= _APPLICATION_PRESENTATION_INPUT_LIMIT
        ):
            raise _ProtocolError("native Application presentation input is invalid")
        frame["application_presentation"] = {
            "role": borrow.role, "input_bytes": body_input_bytes,
        }
    elif isinstance(borrow, NativeAiRoutingRequest):
        if not 0 < body_input_bytes <= _AI_ROUTING_LIMIT:
            raise _ProtocolError("native AI routing input size is invalid")
        frame["ai_route"] = {"role": borrow.role, "input_bytes": body_input_bytes}
        if borrow.stream_result:
            frame["ai_route"]["stream_result"] = True
        elif borrow.usage_cost:
            frame["ai_route"]["usage_cost"] = True
        elif borrow.tool_intents:
            frame["ai_route"]["tool_intents"] = True
        elif borrow.pipeline is not None:
            frame["ai_route"]["pipeline"] = borrow.pipeline
        elif borrow.gateway_result:
            frame["ai_route"]["gateway_result"] = True
        elif borrow.gateway_policy:
            frame["ai_route"]["gateway_policy"] = True
    elif isinstance(borrow, NativeSavedLocalChatRequest):
        frame["saved_local_chat"] = _saved_local_chat_start(borrow)
        if project_local_chat_response:
            frame["saved_local_chat"]["project_response"] = True
        if borrow.compile_request:
            frame["saved_local_chat"]["compile_request"] = True
        if borrow.compile_policy:
            if not borrow.compile_request or not project_local_chat_response:
                raise NativeSavedLocalChatFailure("invalid")
            frame["saved_local_chat"]["compile_policy"] = True
    elif isinstance(borrow, NativeSavedTurnSettlementRequest):
        frame["turn_settlement"] = _turn_settlement_payload(borrow)
    elif isinstance(borrow, NativeConversationConfirmationRequest):
        frame["conversation_confirmation"] = _conversation_confirmation_payload(borrow)
    elif isinstance(borrow, NativeModelResolveRequest):
        if (
            type(borrow.role) is not str
            or borrow.role not in ("quote-generate", "quote-stream", "generate", "stream")
            or not is_canonical_model_resolve_identifier(borrow.identifier)
        ):
            raise _ProtocolError("native Model Resolve inputs are invalid")
        frame["model_resolve"] = {"role": borrow.role, "identifier": borrow.identifier}
    else:
        request = borrow.request
        frame["request"] = {
            "connection-name": request["connection_name"],
            "endpoint": request["endpoint"],
            "catalog-provider-id": request.get("catalog_provider_id"),
            "display-name": request.get("display_name"),
            "capabilities": request.get("capabilities"),
        }
        frozen_plan = borrow.frozen_plan
        if frozen_plan is not None:
            frame["apply"] = dict(frozen_plan)
    try:
        if isinstance(borrow, NativeConversationAppendRequest):
            return _encode_append_start(frame)
        payload = json.dumps(
            frame, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        )
        encoded = payload.encode("utf-8")
    except UnicodeEncodeError:
        raise _ProtocolError("native Provider request contains invalid UTF-8") from None
    except (TypeError, ValueError, RecursionError):
        raise _ProtocolError("native Registry request JSON is invalid") from None
    if len(encoded) > _JSON_LIMIT:
        raise _ProtocolError("native Provider preparation request exceeds its bound")
    return encoded


def _saved_local_chat_start(borrow: NativeSavedLocalChatRequest) -> dict[str, Any]:
    if (
        type(borrow.role) is not str
        or borrow.role not in ("generate", "stream")
        or type(borrow.connection_id) is not str
        or type(borrow.endpoint) is not str
        or not _bounded_revision(borrow.record_revision, 1)
        or type(borrow.input) is not bytes
        or not borrow.input
        or type(borrow.compile_request) is not bool
        or type(borrow.compile_policy) is not bool
    ):
        raise NativeSavedLocalChatFailure("invalid")
    try:
        connection_size = len(borrow.connection_id.encode("utf-8"))
        endpoint_size = len(borrow.endpoint.encode("utf-8"))
    except UnicodeEncodeError:
        raise NativeSavedLocalChatFailure("invalid") from None
    if not 0 < connection_size <= 256 or not 0 < endpoint_size <= 2048:
        raise NativeSavedLocalChatFailure("invalid")
    if len(borrow.input) > SAVED_LOCAL_CHAT_MAX_INPUT_BYTES:
        raise NativeSavedLocalChatFailure("limit-exceeded")
    return {
        "role": borrow.role,
        "connection_id": borrow.connection_id,
        "record_revision": borrow.record_revision,
        "endpoint": borrow.endpoint,
        "input_bytes": len(borrow.input),
    }


def _saved_orchestration_input(borrow: NativeSavedOrchestrationRequest) -> bytes:
    invalid = "native Saved Orchestration input is invalid"
    if not isinstance(borrow.input, Mapping):
        raise _ProtocolError(invalid)
    step_input = dict(borrow.input)
    if step_input.keys() not in ({"request"}, {"state", "outcome"}):
        raise _ProtocolError(invalid)
    try:
        return canonical_json(step_input)
    except (ValueError, RecursionError):
        raise _ProtocolError(invalid) from None


def _check_lossless_json(value: Any) -> None:
    """Reject Python values that JSON would silently coerce."""
    if type(value) is dict:
        for key, member in value.items():
            if type(key) is not str:
                raise TypeError
            _check_lossless_json(member)
    elif type(value) is list:
        for member in value:
            _check_lossless_json(member)
    elif type(value) not in (str, int, float, bool, type(None)):
        raise TypeError


def _ai_routing_input(borrow: NativeAiRoutingRequest) -> bytes:
    if (
        type(borrow.role) is not str
        or borrow.role not in ("quote-generate", "quote-stream", "generate", "stream")
        or (borrow.stream_result and borrow.role != "stream")
        or (
            borrow.usage_cost
            and (borrow.stream_result or borrow.role not in ("generate", "stream"))
        )
        or (
            borrow.tool_intents
            and (
                borrow.stream_result or borrow.usage_cost
                or borrow.role not in ("generate", "stream")
            )
        )
        or (
            borrow.pipeline is not None
            and (
                type(borrow.pipeline) is not str
                or borrow.pipeline not in ("prepare", "failover")
                or borrow.stream_result or borrow.usage_cost or borrow.tool_intents
                or (borrow.pipeline == "failover" and borrow.role != "generate")
            )
        )
        or (
            borrow.gateway_result
            and (
                borrow.role not in ("generate", "stream")
                or borrow.stream_result or borrow.usage_cost or borrow.tool_intents
                or borrow.pipeline is not None
            )
        )
        or (
            borrow.gateway_policy
            and (
                borrow.stream_result or borrow.usage_cost or borrow.tool_intents
                or borrow.pipeline is not None or borrow.gateway_result
            )
        )
        or not isinstance(borrow.input, Mapping)
        or (borrow.usage_cost and borrow.input.keys() != AI_USAGE_COST_INPUT_FIELDS)
        or (borrow.tool_intents and borrow.input.keys() != AI_TOOL_INTENTS_INPUT_FIELDS)
        or (
            borrow.pipeline == "prepare"
            and not AI_PIPELINE_PREPARE_REQUIRED_FIELDS <= borrow.input.keys()
        )
        or (
            borrow.pipeline == "failover"
            and borrow.input.keys() != AI_PIPELINE_FAILOVER_INPUT_FIELDS
        )
    ):
        raise NativeAiRoutingFailure("invalid")
    if borrow.gateway_policy:
        phase = borrow.input.get("phase")
        if (
            type(phase) is not str
            or borrow.input.keys() != _AI_GATEWAY_POLICY_INPUT_FIELDS.get(phase)
        ):
            raise NativeAiRoutingFailure("invalid")
    if borrow.gateway_result:
        fields = _AI_GATEWAY_RESULT_INPUT_FIELDS
        if borrow.role == "stream":
            fields |= _AI_GATEWAY_STREAM_RESULT_FIELDS
        candidate = borrow.input.get("candidate")
        if (
            borrow.input.keys() != fields
            or type(borrow.input["role"]) is not str
            or borrow.input["role"] != borrow.role
            or type(borrow.input["request_id"]) is not str
            or type(candidate) is not dict
            or candidate.keys() != _AI_GATEWAY_CANDIDATE_FIELDS
            or any(type(field) is not str for field in candidate.values())
        ):
            raise NativeAiRoutingFailure("invalid")
    if borrow.pipeline == "prepare":
        handle = borrow.input.get("credential_handle")
        if handle is not None and (
            type(handle) is not str or not handle.startswith(("credential:", "opaque:"))
        ):
            raise NativeProviderRegistryRequiresLegacyRead(
                "AI request prepare requires the signed credential handle check",
            )
    try:
        step_input = dict(borrow.input)
        if (
            borrow.pipeline is not None
            or borrow.gateway_result
            or borrow.gateway_policy
        ):
            _check_lossless_json(step_input)
        payload = json.dumps(
            step_input, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise NativeProviderRegistryRequiresLegacyRead(
            "AI routing input is outside the native JSON envelope",
        ) from None
    if len(payload) > _AI_ROUTING_LIMIT:
        raise NativeProviderRegistryRequiresLegacyRead(
            "AI routing input is outside the native byte limit",
        )
    return payload


def _encode_append_start(frame: dict[str, Any]) -> bytes:
    encoder = json.JSONEncoder(
        ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    )
    encoded = bytearray()
    for part in encoder.iterencode(frame):
        remaining = _APPEND_START_LIMIT - len(encoded)
        # UTF-8 never uses fewer bytes than code points.
        if len(part) > remaining:
            break
        chunk = part.encode("utf-8")
        if len(chunk) > remaining:
            break
        encoded.extend(chunk)
    else:
        return bytes(encoded)
    raise NativeProviderRegistryRequiresLegacyRead(
        "native Conversation append exceeds its request bound",
    )


def _turn_settlement_payload(borrow: NativeSavedTurnSettlementRequest) -> dict[str, Any]:
    invalid = "native saved-turn settlement inputs are invalid"
    revision = borrow.expected_revision
    if (
        type(borrow.caller) is not str
        or borrow.caller not in ("saved-turn", "saved-reconcile")
        or not isinstance(borrow.turn_id, str)
        or _TURN_IDENTIFIER.fullmatch(borrow.turn_id) is None
        or not isinstance(borrow.input_digest, str)
        or _TURN_INPUT_DIGEST.fullmatch(borrow.input_digest) is None
        or (borrow.caller == "saved-reconcile" and revision is None)
        or (
            revision is not None and (type(revision) is not int or not 0 < revision <= _U64_MAX)
        )
        or not isinstance(borrow.result_reference, Mapping)
    ):
        raise _ProtocolError(invalid)
    try:
        reference = json.dumps(
            dict(borrow.result_reference), ensure_ascii=False, separators=(",", ":"),
            allow_nan=False,
        )
        reference_size = len(reference.encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        raise _ProtocolError(invalid) from None
    if reference_size > _TURN_REFERENCE_LIMIT:
        raise _ProtocolError("native saved-turn settlement reference exceeds its envelope")
    return {
        "caller": borrow.caller,
        "turn_id": borrow.turn_id,
        "input_digest": borrow.input_digest,
        "expected_revision": revision,
        "result_reference": reference,
    }


def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _bounded_revision(value: object, minimum: int) -> bool:
    return type(value) is int and minimum <= value <= _U64_MAX


def _turn_start_payload(borrow: NativeSavedTurnStartRequest) -> dict[str, Any]:
    if (
        type(borrow.action) is not str
        or borrow.action not in ("begin", "claim")
        or not is_native_saved_turn_start_identifier(borrow.turn_id)
        or not is_native_saved_turn_start_identifier(borrow.conversation_id)
        or type(borrow.conversation_revision) is not int
        or not 1 <= borrow.conversation_revision <= MAX_SAFE_INTEGER
        or not _matches(_TURN_INPUT_DIGEST, borrow.input_digest)
        or not _matches(_TURN_INPUT_DIGEST, borrow.source_digest)
    ):
        raise _ProtocolError("native saved Turn start inputs are invalid")
    return {
        "action": borrow.action,
        "turn_id": borrow.turn_id,
        "conversation_id": borrow.conversation_id,
        "conversation_revision": borrow.conversation_revision,
        "input_digest": borrow.input_digest,
        "source_digest": borrow.source_digest,
    }


def _conversation_append_payload(
    borrow: NativeConversationAppendRequest,
) -> dict[str, Any]:
    invalid = "native Conversation append inputs are invalid"
    request = borrow.request
    if not isinstance(request, Mapping) or set(request) != {
        "conversation_id", "expected_conversation_revision", "message", "saved_input",
    }:
        raise _ProtocolError(invalid)
    conversation_id = request["conversation_id"]
    revision = request["expected_conversation_revision"]
    message = request["message"]
    saved = request["saved_input"]
    if (
        not _matches(_TURN_IDENTIFIER, conversation_id)
        or type(revision) is not int
        or revision < 1
        or not isinstance(message, Mapping)
        or not _matches(_SAVED_MESSAGE_ID, message.get("id"))
        or message.get("role") not in ("user", "assistant")
        or "content" not in message
        or not isinstance(saved, Mapping)
        or set(saved) != {"request"}
    ):
        raise _ProtocolError(invalid)
    initial = saved["request"]
    if (
        not isinstance(initial, Mapping)
        or initial.get("conversation_id") != conversation_id
        or not _matches(_TURN_IDENTIFIER, initial.get("turn_id"))
    ):
        raise _ProtocolError(invalid)
    if revision >= 1 << 63:
        raise NativeProviderRegistryRequiresLegacyRead(
            "native Conversation append revision exceeds its bound",
        )
    return {
        "request": {
            "conversation_id": conversation_id,
            "expected_conversation_revision": revision,
            "message": dict(message),
            "saved_input": {"request": dict(initial)},
        },
    }


def _conversation_confirmation_payload(
    borrow: NativeConversationConfirmationRequest,
) -> dict[str, Any]:
    invalid = "native Conversation confirmation inputs are invalid"
    if (
        type(borrow.caller) is not str
        or borrow.caller not in ("saved-turn", "saved-reconcile")
        or not isinstance(borrow.terminal, Mapping)
    ):
        raise _ProtocolError(invalid)
    terminal = dict(borrow.terminal)
    if (
        set(terminal) != {
            "id", "profile_id", "conversation_id", "request_id", "status", "revision",
            "conversation_revision", "input_digest", "result_reference",
        }
        or not _matches(_TURN_IDENTIFIER, terminal["id"])
        or terminal["profile_id"] != borrow.context.profile_id
        or not _matches(_TURN_IDENTIFIER, terminal["conversation_id"])
        or not _matches(_SAVED_TURN_REQUEST_ID, terminal["request_id"])
        or terminal["status"] != "completed"
        or not _bounded_revision(terminal["revision"], 3)
        or not _bounded_revision(terminal["conversation_revision"], 1)
        or not _matches(_TURN_INPUT_DIGEST, terminal["input_digest"])
        or not isinstance(terminal["result_reference"], Mapping)
    ):
        raise _ProtocolError(invalid)
    reference = dict(terminal["result_reference"])
    if (
        set(reference) != {
            "conversation_id", "conversation_revision", "user_message_id",
            "assistant_message_id", "outcome_digest",
        }
        or reference["conversation_id"] != terminal["conversation_id"]
        or not _bounded_revision(reference["conversation_revision"], 1)
        or reference["conversation_revision"] != terminal["conversation_revision"] + 2
        or not _matches(_SAVED_MESSAGE_ID, reference["user_message_id"])
        or not _matches(_SAVED_MESSAGE_ID, reference["assistant_message_id"])
        or not _matches(_TURN_INPUT_DIGEST, reference["outcome_digest"])
    ):
        raise _ProtocolError(invalid)
    terminal["result_reference"] = reference
    return {"caller": borrow.caller, "terminal": terminal}


def _decode_plan(payload: bytes, limit: int) -> dict[str, Any]:
    try:
        plan = strict_loads(payload, max_bytes=limit)
    except (ValueError, RecursionError):
        raise _ProtocolError("native Provider preparation plan is invalid") from None
    if not isinstance(plan, dict):
        raise _ProtocolError("native Provider preparation plan is invalid")
    return plan


def _decode_owner_result(payload: bytes, invalid: str) -> Any:
    # The guest's opaque owner result uses Python JSON, including integers
    # outside the strict-canonical range. The frame already bounds its bytes.

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError
        return result

    def integer(token: str) -> int:
        if len(token.removeprefix("-")) > _OWNER_MAX_INTEGER_DIGITS:
            raise ValueError
        return int(token)

    def finite_float(token: str) -> float:
        number = float(token)
        if not math.isfinite(number):
            raise ValueError
        return number

    try:
        return json.loads(
            payload.decode("utf-8"), object_pairs_hook=unique_object,
            parse_int=integer, parse_float=finite_float, parse_constant=finite_float,
        )
    except (ValueError, RecursionError):
        raise _ProtocolError(invalid) from None


def _decode_projected_local_chat(
    payload: bytes, role: SavedLocalChatRole,
) -> dict[str, Any]:
    invalid = "native saved local chat projection is invalid"
    result = _decode_owner_result(payload, invalid)
    keys = {"output", "tool_intents", "usage", "finish_reason"}
    if role == "stream":
        keys.add("events")
    if (
        type(result) is not dict
        or set(result) != keys
        or type(result["tool_intents"]) is not list
        or type(result["usage"]) is not dict
        or (role == "stream" and type(result["events"]) is not list)
    ):
        raise _ProtocolError(invalid)
    return result


def _decode_ai_routing(payload: bytes) -> dict[str, Any]:
    invalid = "native AI routing result is invalid"
    result = _decode_owner_result(payload, invalid)
    if (
        type(result) is not dict
        or set(result) != {"selected", "candidates", "excluded", "deterministic"}
        or result["deterministic"] is not True
        or type(result["candidates"]) is not list
        or type(result["excluded"]) is not list
        or any(type(item) is not dict for item in result["candidates"])
        or any(type(item) is not dict for item in result["excluded"])
        or result["selected"] != next(iter(result["candidates"]), None)
    ):
        raise _ProtocolError(invalid)
    return result


def _decode_ai_pipeline_prepare(
    payload: bytes, borrow: NativeAiRoutingRequest,
) -> dict[str, Any]:
    invalid = "native AI request prepare result is invalid"
    result = _decode_owner_result(payload, invalid)
    if (
        type(result) is not dict
        or result.keys() != _AI_PIPELINE_PREPARE_RESULT_FIELDS
        or type(result["request_id"]) is not str
        or not result["request_id"]
        or result["request_id"] != str(borrow.input["request_id"]).strip()
        or type(result["decision_time"]) not in (int, float)
        or type(result["deadline"]) not in (int, float)
        or result["deadline"] <= result["decision_time"]
        or type(result["messages"]) is not list
        or type(result["parameters"]) is not dict
        or type(result["tools"]) is not list
        or type(result["requirements"]) is not dict
        or result["allow_failover"] is not bool(borrow.input.get("allow_failover"))
        or type(result["policy_revision"]) is not str
    ):
        raise _ProtocolError(invalid)
    handle = result["credential_handle"]
    if handle != borrow.input.get("credential_handle") or (
        handle is not None
        and (type(handle) is not str or not handle.startswith(("credential:", "opaque:")))
    ):
        raise _ProtocolError(invalid)
    return result


def _decode_ai_pipeline_failover(payload: bytes) -> dict[str, Any]:
    invalid = "native AI failover result is invalid"
    result = _decode_owner_result(payload, invalid)
    if (
        type(result) is not dict
        or result.keys() != {"allowed", "checks", "reason"}
        or type(result["checks"]) is not dict
        or result["checks"].keys() != set(_AI_PIPELINE_FAILOVER_CHECKS)
        or any(type(value) is not bool for value in result["checks"].values())
    ):
        raise _ProtocolError(invalid)
    reason = next(
        (key for key in _AI_PIPELINE_FAILOVER_CHECKS if not result["checks"][key]),
        "replay_safe_failover",
    )
    if result["reason"] != reason or result["allowed"] is not (
        reason == "replay_safe_failover"
    ):
        raise _ProtocolError(invalid)
    return result


def _decode_ai_gateway_result(
    payload: bytes, borrow: NativeAiRoutingRequest,
) -> dict[str, Any]:
    invalid = "native AI Gateway result is invalid"
    result = _decode_owner_result(payload, invalid)
    fields = {
        "status", "request_id", "model_id", "provider_instance_id", "output",
        "tool_intents", "finish_reason",
    }
    if borrow.role == "stream":
        fields.update({"delivery_mode", "events", "attempts"})
        valid_fields = type(result) is dict and result.keys() == fields
    else:
        fields.update({
            "catalog_provider_instance_id", "catalog_revision", "pricing_revision",
            "usage", "usage_provenance",
        })
        valid_fields = type(result) is dict and (
            result.keys() in (fields, fields | {"task_state"})
        )
    candidate = borrow.input["candidate"]
    if (
        not valid_fields
        or result["status"] != "ok"
        or result["request_id"] != borrow.input["request_id"]
        or result["model_id"] != candidate["model_id"]
        or result["provider_instance_id"] != candidate["provider_instance_id"]
        or type(result["tool_intents"]) is not list
    ):
        raise _ProtocolError(invalid)
    if borrow.role == "stream":
        if type(result["events"]) is not list or type(result["attempts"]) is not list:
            raise _ProtocolError(invalid)
    elif (
        result["catalog_provider_instance_id"]
        != candidate["catalog_provider_instance_id"]
        or result["catalog_revision"] != candidate["catalog_revision"]
        or result["pricing_revision"] != candidate["catalog_revision"]
        or type(result["usage"]) is not dict
        or type(result["usage_provenance"]) is not str
        or type(result["finish_reason"]) is not str
        or ("task_state" in result and type(result["task_state"]) is not str)
    ):
        raise _ProtocolError(invalid)
    return result


def _decode_ai_gateway_policy(
    payload: bytes, borrow: NativeAiRoutingRequest,
) -> dict[str, Any]:
    invalid = "native AI Gateway policy result is invalid"
    result = _decode_owner_result(payload, invalid)
    if type(result) is not dict:
        raise _ProtocolError(invalid)
    if borrow.input["phase"] == "model-profile":
        if (
            result.keys() != {"model_profile_id", "requirements", "parameters"}
            or type(result["model_profile_id"]) is not str
            or type(result["requirements"]) is not dict
            or type(result["parameters"]) is not dict
        ):
            raise _ProtocolError(invalid)
        return result

    if (
        result.keys() != _AI_GATEWAY_REQUIREMENT_RESULT_FIELDS
        or any(
            type(result[field]) is not list
            or any(type(item) is not str for item in result[field])
            or result[field] != sorted(set(result[field]))
            for field in _AI_GATEWAY_REQUIREMENT_LIST_FIELDS
        )
        or any(
            type(result[field]) is not bool
            for field in _AI_GATEWAY_REQUIREMENT_BOOL_FIELDS
        )
        or any(
            result[field] is not None and type(result[field]) is not str
            for field in _AI_GATEWAY_REQUIREMENT_OPTIONAL_STR_FIELDS
        )
        or type(result["minimum_context"]) is not int
        or result["minimum_context"] < 0
        or type(result["request_surface"]) is not str
        or (
            result["maximum_cost"] is not None
            and type(result["maximum_cost"]) is not float
        )
        or type(result["health_max_age"]) is not float
        or result["health_max_age"] < 0
    ):
        raise _ProtocolError(invalid)
    return result


def _decode_ai_stream_result(
    payload: bytes, borrow: NativeAiRoutingRequest,
) -> dict[str, Any]:
    invalid = "native AI stream normalization result is invalid"
    result = _decode_owner_result(payload, invalid)
    if type(result) is not dict or set(result) != {"events"}:
        raise _ProtocolError(invalid)
    events = result["events"]
    if type(events) is not list or not events:
        raise _ProtocolError(invalid)
    request_id = borrow.input.get("request_id")
    provider_attempt = borrow.input.get("provider_attempt")
    if not isinstance(request_id, str) or type(provider_attempt) is not int:
        raise _ProtocolError(invalid)
    request_id = request_id.strip()
    terminal_types = {"finish", "error"}
    event_types = terminal_types | {
        "text_delta", "thinking_delta", "tool_intent_delta", "usage",
    }
    event_keys = {
        "request_id", "sequence", "type", "delta", "tool_intent", "usage",
        "finish_reason", "error_code", "provider_attempt",
    }
    for sequence, event in enumerate(events):
        if (
            type(event) is not dict
            or set(event) != event_keys
            or event["request_id"] != request_id
            or type(event["provider_attempt"]) is not int
            or event["provider_attempt"] != provider_attempt
            or type(event["sequence"]) is not int
            or event["sequence"] != sequence
            or type(event["type"]) is not str
            or event["type"] not in event_types
            or (event["type"] in terminal_types) != (sequence == len(events) - 1)
        ):
            raise _ProtocolError(invalid)
    return result


def _decode_ai_usage_cost(payload: bytes) -> dict[str, Any]:
    invalid = "native AI Usage Cost result is invalid"
    result = _decode_owner_result(payload, invalid)
    numeric_fields = {"input_tokens", "output_tokens", "total_tokens", "cost"}
    if (
        type(result) is not dict
        or result.keys() != numeric_fields | {
            "currency", "known", "usage_provenance", "pricing_revision",
        }
        or type(result["known"]) is not bool
        or type(result["usage_provenance"]) is not str
    ):
        raise _ProtocolError(invalid)
    for field in numeric_fields:
        value = result[field]
        if value is not None and (
            type(value) not in (float, int) or not 0 <= value <= sys.float_info.max
        ):
            raise _ProtocolError(invalid)
    has_tokens = result["input_tokens"] is not None and result["output_tokens"] is not None
    if (result["total_tokens"] is not None) != has_tokens:
        raise _ProtocolError(invalid)
    if result["known"]:
        if not has_tokens or result["cost"] is None or type(result["currency"]) is not str:
            raise _ProtocolError(invalid)
    elif result["cost"] is not None or result["currency"] is not None:
        raise _ProtocolError(invalid)
    return result


def _decode_ai_tool_intents(
    payload: bytes, borrow: NativeAiRoutingRequest,
) -> dict[str, Any]:
    invalid = "native AI tool intent normalization result is invalid"
    result = _decode_owner_result(payload, invalid)
    values = borrow.input["intents"]
    count = len(values) if isinstance(values, list) else 0
    if (
        type(result) is not dict
        or result.keys() != {"intents"}
        or type(result["intents"]) is not list
        or len(result["intents"]) != count
    ):
        raise _ProtocolError(invalid)
    request_id = str(borrow.input["request_id"] or "").strip()
    fields = {
        "intent_id", "request_id", "operation", "arguments",
        "authority_granted", "approved", "approval_status", "executes",
    }
    for intent in result["intents"]:
        if (
            type(intent) is not dict
            or intent.keys() != fields
            or type(intent["intent_id"]) is not str
            or intent["request_id"] != request_id
            or type(intent["operation"]) is not str
            or type(intent["arguments"]) is not dict
            or intent["authority_granted"] is not False
            or intent["approved"] is not False
            or intent["approval_status"] != "unrequested"
            or intent["executes"] is not False
        ):
            raise _ProtocolError(invalid)
    return result


def _decode_conversation_get(payload: bytes) -> dict[str, Any]:
    invalid = "native Conversation GET owner result is invalid"
    result = _decode_owner_result(payload, invalid)
    if (
        type(result) is not dict
        or set(result) != {"conversation"}
        or type(result["conversation"]) is not dict
    ):
        raise _ProtocolError(invalid)
    return result


def _decode_conversation_append(
    payload: bytes, borrow: NativeConversationAppendRequest,
) -> dict[str, Any]:
    invalid = "native Conversation append owner result is invalid"
    result = _decode_owner_result(payload, invalid)
    request = borrow.request
    if (
        type(result) is not dict
        or set(result) != {"action", "message", "conversation_revision", "store_revision"}
        or result["action"] != "message_appended"
        or not _bounded_revision(result["conversation_revision"], 2)
        or result["conversation_revision"] != request["expected_conversation_revision"] + 1
        or not _bounded_revision(result["store_revision"], 1)
        or type(result["message"]) is not dict
    ):
        raise _ProtocolError(invalid)
    message = result["message"]
    expected = request["message"]
    content = expected["content"] or ""
    if (
        message.get("id") != expected["id"]
        or message.get("role") != expected["role"]
        or type(message.get("content")) is not type(content)
        or message["content"] != content
    ):
        raise _ProtocolError(invalid)
    return result


def _decode_saved_receipt(payload: bytes) -> dict[str, Any]:
    invalid = "native Saved Receipt owner result is invalid"
    result = _decode_owner_result(payload, invalid)
    if (
        type(result) is not dict
        or set(result) != {"receipt"}
        or (result["receipt"] is not None and type(result["receipt"]) is not dict)
    ):
        raise _ProtocolError(invalid)
    return result


def _decode_saved_turn_read(
    payload: bytes, borrow: NativeSavedTurnReadRequest,
) -> dict[str, Any] | None:
    invalid = "native saved Turn read owner result is invalid"
    result = _decode_owner_result(payload, invalid)
    if result is None:
        return None
    if (
        type(result) is not dict
        or result.get("id") != borrow.turn_id
        or result.get("profile_id") != borrow.context.profile_id
    ):
        raise _ProtocolError(invalid)
    return result


def _decode_turn_start(
    payload: bytes,
    borrow: NativeSavedTurnStartRequest,
    *,
    publication_possible: bool,
) -> dict[str, Any]:
    invalid = "native saved Turn start owner result is invalid"
    result = _decode_owner_result(payload, invalid)
    if (
        type(result) is not dict
        or set(result) != {"claimed", "turn"}
        or result["claimed"] is not (borrow.action == "claim" and publication_possible)
        or type(result["turn"]) is not dict
    ):
        raise _ProtocolError(invalid)
    turn = result["turn"]
    published_status = "running" if borrow.action == "claim" else "queued"
    if (
        turn.get("id") != borrow.turn_id
        or turn.get("profile_id") != borrow.context.profile_id
        or turn.get("conversation_id") != borrow.conversation_id
        or type(turn.get("conversation_revision")) is not int
        or turn["conversation_revision"] != borrow.conversation_revision
        or turn.get("input_digest") != borrow.input_digest
        or (publication_possible and turn.get("status") != published_status)
    ):
        raise _ProtocolError(invalid)
    return result


def _decode_turn_settlement(
    payload: bytes, borrow: NativeSavedTurnSettlementRequest,
) -> dict[str, Any]:
    invalid = "native saved-turn settlement owner result is invalid"
    result = _decode_owner_result(payload, invalid)
    if (
        type(result) is not dict
        or result.get("id") != borrow.turn_id
        or result.get("profile_id") != borrow.context.profile_id
        or result.get("input_digest") != borrow.input_digest
        or result.get("status") != "completed"
        or type(result.get("request_id")) is not str
        or not result["request_id"].startswith("saved-turn.")
        or type(result.get("revision")) is not int
        or result["revision"] < 1
        or type(result.get("result_reference")) is not dict
        or type(result["result_reference"].get("conversation_revision")) is not int
        or result["result_reference"] != borrow.result_reference
    ):
        raise _ProtocolError(invalid)
    return result


def _decode_conversation_confirmation(
    payload: bytes,
    borrow: NativeConversationConfirmationRequest,
    *,
    publication_possible: bool,
) -> dict[str, Any]:
    invalid = "native Conversation confirmation owner result is invalid"
    result = _decode_owner_result(payload, invalid)
    if (
        type(result) is not dict
        or set(result) != {
            "confirmed", "conversation_id", "conversation_revision", "store_revision",
        }
        or result["conversation_id"] != borrow.terminal["conversation_id"]
        or type(result["confirmed"]) is not bool
        or (publication_possible and not result["confirmed"])
        or not _bounded_revision(result["conversation_revision"], 1)
        or not _bounded_revision(result["store_revision"], 0)
    ):
        raise _ProtocolError(invalid)
    return result


def _decode_model_resolve(payload: bytes, identifier: str) -> dict[str, Any]:
    invalid = "native Model Resolve owner result is invalid"
    result = _decode_owner_result(payload, invalid)
    if (
        type(result) is not dict
        or set(result) != {
            "requested_id", "resolved_profile_id", "aliased", "profile",
            "store_revision",
        }
        or result["requested_id"] != identifier
        or type(result["resolved_profile_id"]) is not str
        or result["aliased"] is not (result["resolved_profile_id"] != identifier)
        or type(result["profile"]) is not dict
    ):
        raise _ProtocolError(invalid)
    return result


def _decode_model_save(payload: bytes) -> dict[str, Any]:
    invalid = "native Model Settings owner result is invalid"
    result = _decode_owner_result(payload, invalid)
    if (
        type(result) is not dict
        or set(result) != {"action", "store_revision", "profile"}
        or result["action"] != "saved"
        or type(result["store_revision"]) is not int
        or type(result["profile"]) is not dict
    ):
        raise _ProtocolError(invalid)
    profile = result["profile"]
    if type(profile.get("enabled")) is not bool or any(
        not isinstance(profile.get(key), str) or not profile[key].strip()
        for key in ("model_profile_id", "model_id", "display_name")
    ):
        raise _ProtocolError(invalid)
    pending: list[Any] = [result]
    nodes = 0
    while pending:
        value = pending.pop()
        nodes += 1
        children: Collection[Any]
        if isinstance(value, dict):
            nodes += len(value)  # Include the object keys in the node budget.
            children = value.values()
        elif isinstance(value, list):
            children = value
        else:
            continue
        if nodes + len(pending) + len(children) > _MODEL_SAVE_MAX_NODES:
            raise _ProtocolError(invalid)
        pending.extend(children)
    return result


class _Exchange:
    def __init__(
        self,
        connection: socket.socket,
        plan_limit: int,
        *,
        permits_publication: bool,
        conversation_mutation: bool,
        requires_body_ready: bool,
        ai_routing: bool = False,
        application_presentation: bool = False,
        permits_dispatch: bool = False,
        accepts_projected_local_chat: bool = False,
        compiles_local_chat_request: bool = False,
        compiles_local_chat_policy: bool = False,
    ) -> None:
        self.connection = connection
        self.plan_limit = plan_limit
        self.permits_publication = permits_publication
        self.permits_dispatch = permits_dispatch
        self.accepts_projected_local_chat = accepts_projected_local_chat
        self.requires_body_ready = requires_body_ready
        self.ai_routing = ai_routing
        self.read_only_body = ai_routing or application_presentation
        self.legacy_failure = (
            _CONVERSATION_LEGACY_READ_FAILURE
            if conversation_mutation
            else _LEGACY_READ_FAILURE
        )
        self.failure_codes = _FAILURES
        if permits_dispatch:
            self.failure_codes |= {b"invalid-response"}
        if compiles_local_chat_request:
            self.failure_codes |= {_LEGACY_REQUEST_FAILURE}
        if compiles_local_chat_policy:
            self.failure_codes |= _LOCAL_CHAT_POLICY_FAILURES
        if not requires_body_ready or ai_routing:
            self.failure_codes |= {self.legacy_failure}
        if conversation_mutation:
            self.failure_codes |= {_PUBLICATION_UNCONFIRMED}
        self.failure_limit = max(map(len, self.failure_codes))
        self.frame_header = bytearray(_FRAME.size)
        self.frame_payload: bytearray | None = None
        self.frame_received = 0
        self.partial_write = False
        self.started = False
        self.terminal = False
        self.entered_live_phase = False
        self.publication_possible = False
        self.dispatch_possible = False
        self.dispatch_permitted = False

    def send(
        self, tag: int, payload: bytes, wait_seconds: Callable[[], float],
    ) -> None:
        frame = memoryview(_FRAME.pack(tag, len(payload)) + payload)
        while frame:
            self.connection.settimeout(wait_seconds())
            try:
                sent = self.connection.send(frame)
            except TimeoutError:
                continue
            if not sent:
                raise _ProtocolError("native Provider preparation connection closed")
            frame = frame[sent:]
            self.partial_write = bool(frame)

    def _receive_into(
        self, buffer: bytearray, wait_seconds: Callable[[], float],
    ) -> None:
        view = memoryview(buffer)
        while self.frame_received < len(view):
            self.connection.settimeout(wait_seconds())
            try:
                count = self.connection.recv_into(view[self.frame_received:])
            except TimeoutError:
                continue
            if not count:
                raise _ProtocolError("native Provider preparation connection closed")
            self.frame_received += count

    def receive(self, wait_seconds: Callable[[], float]) -> tuple[int, bytes]:
        if self.frame_payload is None:
            self._receive_into(self.frame_header, wait_seconds)
        tag, size = _FRAME.unpack(self.frame_header)
        limit = self.plan_limit if tag == _PLAN else _INBOUND_LIMITS.get(tag)
        if tag == _FAILURE:
            limit = self.failure_limit
        elif tag == _PROJECTED_LOCAL_CHAT and self.accepts_projected_local_chat:
            limit = self.plan_limit
        elif tag == _PUBLISH_CHECK and self.permits_publication:
            limit = 0
        elif tag == _DISPATCH_CHECK and self.permits_dispatch:
            limit = 0
        elif tag == _BODY_READY and self.requires_body_ready:
            limit = 0
        if limit is None or size > limit:
            raise _ProtocolError("native Provider preparation frame is invalid")
        if self.frame_payload is None:
            # Keep progress across a failed lease check so cleanup can finish
            # draining a partial frame before recognizing terminal proof.
            self.frame_payload = bytearray(size)
            self.frame_received = 0
        self._receive_into(self.frame_payload, wait_seconds)
        payload = bytes(self.frame_payload)
        self.frame_payload = None
        self.frame_received = 0
        return tag, payload

    def cancel_and_confirm(self) -> None:
        deadline = time.monotonic() + _CLEANUP_SECONDS

        def wait_seconds() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            return min(_POLL_SECONDS, remaining)

        try:
            if self.partial_write:
                # A CANCEL header inside an incomplete payload is just more data.
                # Stop writing so the Launcher sees EOF and cancels its worker.
                self.connection.shutdown(socket.SHUT_WR)
            else:
                self.send(_CANCEL, b"", wait_seconds)
        except (OSError, _ProtocolError):
            # The Launcher may have closed its receive side after sending proof.
            pass
        try:
            while True:
                tag, payload = self.receive(wait_seconds)
                if tag == _FAILURE and payload in self.failure_codes:
                    if self.dispatch_possible and payload in _LOCAL_CHAT_POLICY_FAILURES:
                        break
                    return
                if (tag == _PLAN and (
                    self.dispatch_possible or (self.read_only_body and self.entered_live_phase)
                )) or (
                    tag == _PROJECTED_LOCAL_CHAT and self.dispatch_permitted
                ):
                    # The worker already finished. The caller still discards the
                    # result because its live guard failed before acceptance.
                    return
                if not (
                    tag == _CHECK
                    or (tag == _PUBLISH_CHECK and self.permits_publication)
                    or (tag == _DISPATCH_CHECK and self.permits_dispatch)
                    or (tag == _BODY_READY and self.requires_body_ready)
                ):
                    break
        except (OSError, _ProtocolError):
            pass
        raise _ProtocolError("native Provider preparation cleanup was unconfirmed")

    def run_saved_local_chat(
        self,
        borrow: NativeSavedLocalChatRequest,
        start: bytes,
        wait_seconds: Callable[[], float],
    ) -> bytes | dict[str, Any] | None:
        self.send(_START, start, wait_seconds)
        self.started = True
        body_ready = False
        entered_live_phase = False
        while True:
            tag, payload = self.receive(wait_seconds)
            if tag == _FAILURE and payload in self.failure_codes:
                if payload in _LOCAL_CHAT_POLICY_FAILURES and (
                    not body_ready or self.dispatch_possible
                ):
                    raise _ProtocolError(
                        "native saved local chat policy result is out of order",
                    )
                self.terminal = True
                wait_seconds()
                if (
                    payload == _LEGACY_REQUEST_FAILURE
                    and borrow.compile_request
                    and body_ready
                    and not self.dispatch_possible
                ):
                    return None
                raise NativeSavedLocalChatFailure(payload.decode("ascii"))
            if tag == _INAPPLICABLE and not body_ready:
                self.terminal = True
                wait_seconds()
                return None
            if tag == _BODY_READY and not body_ready:
                body_ready = True
                self.send(_SAVED_LOCAL_CHAT_INPUT, borrow.input, wait_seconds)
            elif tag == _CHECK and body_ready:
                self.send(_LIVE, b"", wait_seconds)
                entered_live_phase = True
            elif (
                tag == _DISPATCH_CHECK
                and entered_live_phase
                and not self.dispatch_possible
            ):
                wait_seconds()
                borrow.assert_dispatch()
                # The authority callback may block while it rechecks the binding.
                wait_seconds()
                self.dispatch_possible = True
                borrow.mark_dispatch_possible()
                self.send(_DISPATCH_PERMIT, b"", wait_seconds)
                self.dispatch_permitted = True
            elif tag == _PROJECTED_LOCAL_CHAT and self.dispatch_permitted:
                self.terminal = True
                wait_seconds()
                result = _decode_projected_local_chat(payload, borrow.role)
                wait_seconds()
                return result
            elif tag == _PLAN and self.dispatch_possible:
                self.terminal = True
                wait_seconds()
                return payload
            else:
                raise _ProtocolError("native saved local chat frame is out of order")

    def run(
        self,
        borrow: _NativeBorrow,
        deadline_ns: int,
        wait_seconds: Callable[[], float],
        *,
        body_input: bytes | None = None,
    ) -> dict[str, Any] | None:
        conversation_append = isinstance(borrow, NativeConversationAppendRequest)
        if isinstance(borrow, NativeSavedOrchestrationRequest):
            body_input = _saved_orchestration_input(borrow)
        try:
            start = _start_payload(
                borrow, deadline_ns,
                body_input_bytes=len(body_input or b""),
            )
        except NativeProviderRegistryRequiresLegacyRead:
            wait_seconds()
            raise
        self.send(_START_APPEND if conversation_append else _START, start, wait_seconds)
        self.started = True
        conversation_get = isinstance(borrow, NativeConversationGetRequest)
        while True:
            tag, payload = self.receive(wait_seconds)
            if tag == _FAILURE and payload in self.failure_codes:
                self.terminal = True
                if (
                    payload == self.legacy_failure
                    and self.entered_live_phase
                    and not self.publication_possible
                ):
                    wait_seconds()
                    raise NativeProviderRegistryRequiresLegacyRead(
                        "owner requires a legacy read outside the native envelope",
                    )
                if payload != self.legacy_failure:
                    if isinstance(borrow, NativeSavedOrchestrationRequest):
                        wait_seconds()
                        raise NativeSavedOrchestrationFailure(payload.decode("ascii"))
                    if self.ai_routing:
                        wait_seconds()
                        raise NativeAiRoutingFailure(payload.decode("ascii"))
                    if self.entered_live_phase and isinstance(
                        borrow, NativeSavedTurnReadRequest,
                    ):
                        wait_seconds()
                        raise NativeSavedTurnReadFailure(payload.decode("ascii"))
                    if isinstance(borrow, NativeModelSaveRequest):
                        wait_seconds()
                        raise NativeModelSaveFailure(payload.decode("ascii"))
                    if isinstance(borrow, NativeSavedTurnStartRequest):
                        wait_seconds()
                        raise NativeSavedTurnStartFailure(payload.decode("ascii"))
                    if isinstance(borrow, NativeSavedTurnSettlementRequest):
                        wait_seconds()
                        raise NativeSavedTurnSettlementFailure(payload.decode("ascii"))
                    if conversation_append and payload != _PUBLICATION_UNCONFIRMED:
                        wait_seconds()
                        raise NativeConversationAppendFailure(payload.decode("ascii"))
                    if (
                        isinstance(borrow, NativeConversationConfirmationRequest)
                        and payload != _PUBLICATION_UNCONFIRMED
                    ):
                        wait_seconds()
                        raise NativeConversationConfirmationFailure(
                            payload.decode("ascii"),
                        )
                if (
                    self.entered_live_phase
                    and payload in (b"not-found", b"invalid", b"denied")
                ):
                    if conversation_get:
                        wait_seconds()
                        raise NativeConversationGetFailure(payload.decode("ascii"))
                    if isinstance(borrow, NativeSavedReceiptRequest):
                        wait_seconds()
                        raise NativeSavedReceiptFailure(payload.decode("ascii"))
                    if isinstance(borrow, NativeModelResolveRequest):
                        wait_seconds()
                        raise NativeModelResolveFailure(payload.decode("ascii"))
                raise _ProtocolError(
                    f"native Provider preparation failed ({payload.decode('ascii')})"
                )
            if self.read_only_body and tag == _PLAN and self.entered_live_phase:
                # The Launcher sends PLAN only after the pure worker drains.
                self.terminal = True
            wait_seconds()
            if tag == _BODY_READY and body_input is not None:
                self.send(_ORCHESTRATION_INPUT, body_input, wait_seconds)
                body_input = None
            elif tag == _CHECK and body_input is None:
                self.send(_LIVE, b"", wait_seconds)
                self.entered_live_phase = True
            elif (
                tag == _PUBLISH_CHECK
                and self.entered_live_phase
                and isinstance(borrow, (
                    NativeModelSaveRequest, NativeSavedTurnStartRequest,
                    NativeSavedTurnSettlementRequest,
                    NativeConversationConfirmationRequest, NativeConversationAppendRequest,
                ))
                and not self.publication_possible
            ):
                # The marker can record uncertainty even if its callback raises.
                self.publication_possible = True
                borrow.mark_publication_possible()
                # send() checks the current guard again before offering any byte.
                self.send(_PUBLISH_PERMIT, b"", wait_seconds)
            elif tag == _PLAN and self.entered_live_phase:
                plan: dict[str, Any] | None
                if conversation_get:
                    plan = _decode_conversation_get(payload)
                elif conversation_append:
                    if not self.publication_possible:
                        raise _ProtocolError("native Conversation append permit is missing")
                    plan = _decode_conversation_append(payload, borrow)
                elif isinstance(borrow, NativeSavedReceiptRequest):
                    plan = _decode_saved_receipt(payload)
                elif isinstance(borrow, NativeSavedTurnReadRequest):
                    plan = _decode_saved_turn_read(payload, borrow)
                elif isinstance(borrow, NativeSavedTurnStartRequest):
                    plan = _decode_turn_start(
                        payload, borrow, publication_possible=self.publication_possible,
                    )
                elif isinstance(borrow, NativeSavedTurnSettlementRequest):
                    plan = _decode_turn_settlement(payload, borrow)
                elif isinstance(borrow, NativeConversationConfirmationRequest):
                    plan = _decode_conversation_confirmation(
                        payload, borrow, publication_possible=self.publication_possible,
                    )
                elif isinstance(borrow, NativeModelResolveRequest):
                    plan = _decode_model_resolve(payload, borrow.identifier)
                elif isinstance(borrow, NativeModelSaveRequest):
                    plan = _decode_model_save(payload)
                elif isinstance(borrow, NativeAiRoutingRequest):
                    if borrow.stream_result:
                        plan = _decode_ai_stream_result(payload, borrow)
                    elif borrow.usage_cost:
                        plan = _decode_ai_usage_cost(payload)
                    elif borrow.tool_intents:
                        plan = _decode_ai_tool_intents(payload, borrow)
                    elif borrow.pipeline == "prepare":
                        plan = _decode_ai_pipeline_prepare(payload, borrow)
                    elif borrow.pipeline == "failover":
                        plan = _decode_ai_pipeline_failover(payload)
                    elif borrow.gateway_result:
                        plan = _decode_ai_gateway_result(payload, borrow)
                    elif borrow.gateway_policy:
                        plan = _decode_ai_gateway_policy(payload, borrow)
                    else:
                        plan = _decode_ai_routing(payload)
                else:
                    plan = _decode_plan(payload, self.plan_limit)
                wait_seconds()
                self.terminal = True
                return plan
            elif (
                tag == _INAPPLICABLE
                and not self.entered_live_phase
                and (not self.requires_body_ready or body_input is not None)
            ):
                self.terminal = True
                raise NativeProviderPrepareUnavailable("native Provider is unavailable")
            else:
                raise _ProtocolError("native Provider frame is out of order")


def _launcher_deadline(
    launcher: _Launcher, borrow: _NativeBorrow | NativeSavedLocalChatRequest,
) -> tuple[int, Callable[[], float]]:
    numerator, denominator = borrow.deadline_monotonic.as_integer_ratio()
    deadline_ns = numerator * _NANOS_PER_SECOND // denominator
    if not 0 < deadline_ns <= _U64_MAX:
        raise _ProtocolError("native Provider preparation deadline is invalid")

    def wait_seconds() -> float:
        borrow.assert_current()
        launcher.assert_parent()
        remaining = deadline_ns - time.monotonic_ns()
        if remaining <= 0:
            raise TimeoutError("native Provider preparation deadline elapsed")
        return min(_POLL_SECONDS, remaining / _NANOS_PER_SECOND)

    return deadline_ns, wait_seconds


@contextmanager
def _connect_launcher(
    launcher: _Launcher, wait_seconds: Callable[[], float],
) -> Iterator[socket.socket]:
    launcher.assert_start()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(wait_seconds())
        try:
            connection.connect(launcher.address)
        except OSError:
            raise _ProtocolError(_CONNECTION_ERROR) from None
        _authenticate(connection, launcher)
        yield connection


def _invoke_saved_local_chat(
    launcher: _Launcher,
    borrow: NativeSavedLocalChatRequest,
    *,
    project_response: bool,
) -> bytes | dict[str, Any]:
    try:
        result = _exchange_saved_local_chat(
            launcher, borrow, project_response=project_response,
        )
    except NativeProviderPrepareUnavailable:
        # Borrowed callbacks cannot authorize fallback for this exchange.
        raise NativeSavedLocalChatFailure("unavailable") from None
    if result is None:
        raise NativeProviderPrepareUnavailable("native saved local chat unavailable")
    return result


def _exchange_saved_local_chat(
    launcher: _Launcher,
    borrow: NativeSavedLocalChatRequest,
    *,
    project_response: bool,
) -> bytes | dict[str, Any] | None:
    deadline_ns, wait_seconds = _launcher_deadline(launcher, borrow)
    wait_seconds()
    start = _start_payload(
        borrow, deadline_ns, project_local_chat_response=project_response,
    )
    with _connect_launcher(launcher, wait_seconds) as connection:
        exchange = _Exchange(
            connection,
            SAVED_LOCAL_CHAT_MAX_RESULT_BYTES,
            permits_publication=False,
            conversation_mutation=False,
            requires_body_ready=True,
            permits_dispatch=True,
            accepts_projected_local_chat=project_response,
            compiles_local_chat_request=borrow.compile_request,
            compiles_local_chat_policy=borrow.compile_policy,
        )
        try:
            return exchange.run_saved_local_chat(borrow, start, wait_seconds)
        except Exception as error:
            if (exchange.started or exchange.partial_write) and not exchange.terminal:
                try:
                    exchange.cancel_and_confirm()
                except _ProtocolError:
                    if exchange.dispatch_possible:
                        raise NativeSavedLocalChatDispatchUnconfirmed(
                            "native saved local chat dispatch could not be confirmed",
                        ) from None
                    raise
            if isinstance(error, OSError) and not isinstance(
                error, (PermissionError, TimeoutError),
            ):
                raise _ProtocolError(_CONNECTION_ERROR) from None
            raise


def _invoke_ai_route(
    launcher: _Launcher,
    borrow: NativeAiRoutingRequest,
    *,
    stream_result: bool,
    usage_cost: bool = False,
    tool_intents: bool = False,
    pipeline: bool = False,
    gateway_result: bool = False,
    gateway_policy: bool = False,
) -> dict[str, Any]:
    if (
        borrow.stream_result is not stream_result
        or borrow.usage_cost is not usage_cost
        or borrow.tool_intents is not tool_intents
        or (borrow.pipeline is not None) is not pipeline
        or borrow.gateway_result is not gateway_result
        or borrow.gateway_policy is not gateway_policy
    ):
        raise NativeAiRoutingFailure("invalid")
    return _invoke(launcher, borrow, operation="ai_route")


@overload
def _invoke(
    launcher: _Launcher,
    borrow: _NativeBorrow,
    *,
    operation: _NativeOperation,
) -> dict[str, Any]: ...


@overload
def _invoke(
    launcher: _Launcher,
    borrow: NativeSavedTurnReadRequest,
    *,
    operation: Literal["turn_read"],
) -> dict[str, Any] | None: ...


def _invoke(
    launcher: _Launcher,
    borrow: _NativeBorrow,
    *,
    operation: _NativeOperation | Literal["turn_read"],
) -> dict[str, Any] | None:
    model_settings = isinstance(borrow, NativeModelSettingsRequest)
    model_save = isinstance(borrow, NativeModelSaveRequest)
    conversation_get = isinstance(borrow, NativeConversationGetRequest)
    conversation_append = isinstance(borrow, NativeConversationAppendRequest)
    saved_receipt = isinstance(borrow, NativeSavedReceiptRequest)
    turn_read = isinstance(borrow, NativeSavedTurnReadRequest)
    turn_start = isinstance(borrow, NativeSavedTurnStartRequest)
    saved_orchestration = isinstance(borrow, NativeSavedOrchestrationRequest)
    application_presentation = isinstance(borrow, NativeApplicationPresentationRequest)
    turn_settlement = isinstance(borrow, NativeSavedTurnSettlementRequest)
    conversation_confirmation = isinstance(borrow, NativeConversationConfirmationRequest)
    model_resolve = isinstance(borrow, NativeModelResolveRequest)
    ai_routing = isinstance(borrow, NativeAiRoutingRequest)
    if (
        model_settings != (operation == "model_list")
        or model_save != (operation == "model_save")
        or conversation_get != (operation == "conversation_get")
        or conversation_append != (operation == "conversation_append")
        or saved_receipt != (operation == "conversation_receipt")
        or turn_read != (operation == "turn_read")
        or turn_start != (operation == "turn_start")
        or saved_orchestration != (operation == "saved_orchestration")
        or application_presentation != (operation == "application_presentation")
        or turn_settlement != (operation == "turn_settlement")
        or conversation_confirmation != (operation == "conversation_confirmation")
        or model_resolve != (operation == "model_resolve")
        or ai_routing != (operation == "ai_route")
    ):
        raise _ProtocolError("native Registry request role is invalid")

    deadline_ns, wait_seconds = _launcher_deadline(launcher, borrow)
    body_input = None
    if ai_routing:
        wait_seconds()
        body_input = _ai_routing_input(borrow)
    elif isinstance(borrow, NativeApplicationPresentationRequest):
        wait_seconds()
        body_input = canonical_json(dict(borrow.input))
        if len(body_input) > _APPLICATION_PRESENTATION_INPUT_LIMIT:
            raise _ProtocolError("native Application presentation input exceeds IPC limit")
    with _connect_launcher(launcher, wait_seconds) as connection:
        if saved_orchestration:
            plan_limit = MAX_CANONICAL_JSON_BYTES
        elif turn_start:
            plan_limit = _TURN_START_RESULT_LIMIT
        elif ai_routing:
            plan_limit = _AI_ROUTING_LIMIT
        elif (
            model_settings or model_save or conversation_get
            or saved_receipt or model_resolve or turn_read or turn_settlement
            or conversation_append
            or application_presentation
        ):
            plan_limit = _OWNER_RESULT_LIMIT
        else:
            plan_limit = _JSON_LIMIT
        exchange = _Exchange(
            connection,
            plan_limit,
            permits_publication=(
                model_save or turn_start or turn_settlement or conversation_confirmation
                or conversation_append
            ),
            conversation_mutation=conversation_confirmation or conversation_append,
            requires_body_ready=saved_orchestration or ai_routing or application_presentation,
            ai_routing=ai_routing,
            application_presentation=application_presentation,
        )
        try:
            return exchange.run(borrow, deadline_ns, wait_seconds, body_input=body_input)
        except Exception as error:
            if exchange.started and not exchange.terminal:
                try:
                    exchange.cancel_and_confirm()
                except _ProtocolError as cleanup_error:
                    if not exchange.publication_possible:
                        raise cleanup_error from None
            if exchange.publication_possible:
                if conversation_append:
                    raise NativeConversationAppendPublicationUnconfirmed(
                        "native Conversation append publication could not be confirmed",
                    ) from None
                if conversation_confirmation:
                    raise NativeConversationConfirmationPublicationUnconfirmed(
                        "native Conversation confirmation publication could not be confirmed",
                    ) from None
                if turn_start:
                    raise HostPublicationUnconfirmed(
                        "native saved Turn start publication could not be confirmed",
                    ) from None
                if turn_settlement:
                    raise NativeSavedTurnSettlementPublicationUnconfirmed(
                        "native saved-turn settlement publication could not be confirmed",
                    ) from None
                raise NativeModelSavePublicationUnconfirmed(
                    "native Model Registry publication could not be confirmed",
                ) from None
            if isinstance(error, OSError) and not isinstance(
                error, (PermissionError, TimeoutError),
            ):
                raise _ProtocolError(_CONNECTION_ERROR) from None
            raise
