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
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from functools import lru_cache, partial
from typing import Any, Literal

from core_runtime.process_identity import process_start_identity
from core_runtime.provider_prepare_native_v4 import (
    MINIMUM_PREPARE_MEMORY,
    NativeProviderPrepareBorrow,
    NativeProviderPreparePlanner,
    NativeProviderRegistryRequiresLegacyRead,
)
from tobkiri_host.models import RequestContext
from tobkiri_protocol.canonical import strict_loads

_SOCKET_ENV = "TOBKIRI_NATIVE_PROVIDER_PREPARE_SOCKET"
_OWNER_ENV = "TOBKIRI_NATIVE_PROVIDER_PREPARE_OWNER"
_OWNER = re.compile(r"([0-9]{1,10}):([0-9]{1,20}):([0-9]{1,6})")
_INSTANCE = re.compile(r"[0-9a-f]{32}")
_MODEL_RESOLVE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_SOL_LOCAL = 0
_LOCAL_PEERPID = 2
_SOCKET_PATH_MAX = 103  # Darwin sockaddr_un.sun_path includes the terminating NUL.
_FRAME = struct.Struct("!BI")
_START, _CHECK, _LIVE = 1, 2, 3
_PLAN, _FAILURE, _CANCEL = 7, 8, 9
_INAPPLICABLE = 10
_PUBLISH_CHECK, _PUBLISH_PERMIT = 11, 12
_JSON_LIMIT = 16 * 1024
_OWNER_RESULT_LIMIT = 1024 * 1024
# The Host borrows the same bridge allowance as the Registry owner routes.
# The managed Launcher's Wasm instance has its own physical memory cap.
CONVERSATION_GET_MINIMUM_MEMORY = MINIMUM_PREPARE_MEMORY
MODEL_RESOLVE_MINIMUM_MEMORY = MINIMUM_PREPARE_MEMORY
# Reserve START identity space even if each ID character needs a JSON escape.
CONVERSATION_GET_MAX_ID_CHARS = _JSON_LIMIT // 8
_MODEL_SAVE_MAX_NODES = 100_000
_OWNER_MAX_INTEGER_DIGITS = 4300
_FAILURES = frozenset({
    b"denied", b"approval-required", b"cancelled", b"not-found", b"invalid",
    b"limit-exceeded", b"conflict", b"unavailable", b"internal", b"legacy-read-required",
})
_INBOUND_LIMITS = {
    _CHECK: 0,
    _FAILURE: max(map(len, _FAILURES)),
    _INAPPLICABLE: 0,
}
_POLL_SECONDS = 0.05
_CLEANUP_SECONDS = 10.0
_NANOS_PER_SECOND = 1_000_000_000
_U64_MAX = (1 << 64) - 1


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


class NativeModelResolveFailure(RuntimeError):
    """The Launcher returned a typed failure from the Model Registry owner."""

    def __init__(self, code: str) -> None:
        super().__init__(f"native Model Resolve failed ({code})")
        self.code = code


class NativeModelSavePublicationUnconfirmed(RuntimeError):
    """A permitted Model Registry publication has no confirmed owner result."""


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


ModelResolveRole = Literal["quote-generate", "quote-stream", "generate", "stream"]


@dataclass(frozen=True)
class NativeModelResolveRequest:
    """Borrow the original Model Resolve capture and live guard."""

    context: RequestContext
    runtime_instance: str
    deadline_monotonic: float
    assert_current: Callable[[], None]
    role: ModelResolveRole
    identifier: str


_NativeBorrow = (
    NativeProviderPrepareBorrow
    | NativeModelSettingsRequest
    | NativeModelSaveRequest
    | NativeConversationGetRequest
    | NativeModelResolveRequest
)


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


def _start_payload(borrow: _NativeBorrow, deadline_ns: int) -> bytes:
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
        model_save: bool,
    ) -> None:
        self.connection = connection
        self.plan_limit = plan_limit
        self.model_save = model_save
        self.received = bytearray()
        self.partial_write = False
        self.started = False
        self.terminal = False
        self.publication_possible = False

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

    def _receive_until(self, size: int, wait_seconds: Callable[[], float]) -> None:
        while len(self.received) < size:
            self.connection.settimeout(wait_seconds())
            try:
                part = self.connection.recv(size - len(self.received))
            except TimeoutError:
                continue
            if not part:
                raise _ProtocolError("native Provider preparation connection closed")
            self.received.extend(part)

    def receive(self, wait_seconds: Callable[[], float]) -> tuple[int, bytes]:
        self._receive_until(_FRAME.size, wait_seconds)
        tag, size = _FRAME.unpack_from(self.received)
        limit = self.plan_limit if tag == _PLAN else _INBOUND_LIMITS.get(tag)
        if tag == _PUBLISH_CHECK and self.model_save:
            limit = 0
        if limit is None or size > limit:
            raise _ProtocolError("native Provider preparation frame is invalid")
        self._receive_until(_FRAME.size + size, wait_seconds)
        payload = bytes(self.received[_FRAME.size:])
        self.received.clear()
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
                if tag == _FAILURE and payload in _FAILURES:
                    return
                if tag != _CHECK and not (tag == _PUBLISH_CHECK and self.model_save):
                    break
        except (OSError, _ProtocolError):
            pass
        raise _ProtocolError("native Provider preparation cleanup was unconfirmed")

    def run(
        self,
        borrow: _NativeBorrow,
        deadline_ns: int,
        wait_seconds: Callable[[], float],
    ) -> dict[str, Any]:
        self.send(_START, _start_payload(borrow, deadline_ns), wait_seconds)
        self.started = True
        entered_live_phase = False
        conversation_get = isinstance(borrow, NativeConversationGetRequest)
        while True:
            tag, payload = self.receive(wait_seconds)
            if tag == _FAILURE and payload in _FAILURES:
                self.terminal = True
                if (
                    payload == b"legacy-read-required"
                    and entered_live_phase
                    and not self.publication_possible
                ):
                    wait_seconds()
                    raise NativeProviderRegistryRequiresLegacyRead(
                        "owner requires a legacy read outside the native envelope",
                    )
                if self.model_save and payload != b"legacy-read-required":
                    wait_seconds()
                    raise NativeModelSaveFailure(payload.decode("ascii"))
                if (
                    entered_live_phase
                    and payload in (b"not-found", b"invalid", b"denied")
                ):
                    if conversation_get:
                        wait_seconds()
                        raise NativeConversationGetFailure(payload.decode("ascii"))
                    if isinstance(borrow, NativeModelResolveRequest):
                        wait_seconds()
                        raise NativeModelResolveFailure(payload.decode("ascii"))
                raise _ProtocolError(
                    f"native Provider preparation failed ({payload.decode('ascii')})"
                )
            wait_seconds()
            if tag == _CHECK:
                self.send(_LIVE, b"", wait_seconds)
                entered_live_phase = True
            elif (
                tag == _PUBLISH_CHECK
                and entered_live_phase
                and isinstance(borrow, NativeModelSaveRequest)
                and not self.publication_possible
            ):
                # The marker can record uncertainty even if its callback raises.
                self.publication_possible = True
                borrow.mark_publication_possible()
                # send() checks the current guard again before offering any byte.
                self.send(_PUBLISH_PERMIT, b"", wait_seconds)
            elif tag == _PLAN and entered_live_phase:
                if conversation_get:
                    plan = _decode_conversation_get(payload)
                elif isinstance(borrow, NativeModelResolveRequest):
                    plan = _decode_model_resolve(payload, borrow.identifier)
                elif self.model_save:
                    plan = _decode_model_save(payload)
                else:
                    plan = _decode_plan(payload, self.plan_limit)
                wait_seconds()
                self.terminal = True
                return plan
            elif tag == _INAPPLICABLE and not entered_live_phase:
                self.terminal = True
                raise NativeProviderPrepareUnavailable("native Provider is unavailable")
            else:
                raise _ProtocolError("native Provider frame is out of order")


def _invoke(
    launcher: _Launcher,
    borrow: _NativeBorrow,
    *,
    operation: Literal[
        "provider_prepare", "model_list", "model_save", "conversation_get",
        "model_resolve",
    ],
) -> Mapping[str, Any]:
    model_settings = isinstance(borrow, NativeModelSettingsRequest)
    model_save = isinstance(borrow, NativeModelSaveRequest)
    conversation_get = isinstance(borrow, NativeConversationGetRequest)
    model_resolve = isinstance(borrow, NativeModelResolveRequest)
    if (
        model_settings != (operation == "model_list")
        or model_save != (operation == "model_save")
        or conversation_get != (operation == "conversation_get")
        or model_resolve != (operation == "model_resolve")
    ):
        raise _ProtocolError("native Registry request role is invalid")

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

    launcher.assert_start()
    connection_error = "native Provider preparation connection failed"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(wait_seconds())
        try:
            connection.connect(launcher.address)
        except OSError:
            raise _ProtocolError(connection_error) from None
        _authenticate(connection, launcher)
        plan_limit = (
            _OWNER_RESULT_LIMIT
            if model_settings or model_save or conversation_get or model_resolve
            else _JSON_LIMIT
        )
        exchange = _Exchange(connection, plan_limit, model_save=model_save)
        try:
            return exchange.run(borrow, deadline_ns, wait_seconds)
        except Exception as error:
            if exchange.started and not exchange.terminal:
                try:
                    exchange.cancel_and_confirm()
                except _ProtocolError as cleanup_error:
                    if not exchange.publication_possible:
                        raise cleanup_error from None
            if exchange.publication_possible:
                raise NativeModelSavePublicationUnconfirmed(
                    "native Model Registry publication could not be confirmed",
                ) from None
            if isinstance(error, OSError) and not isinstance(
                error, (PermissionError, TimeoutError),
            ):
                raise _ProtocolError(connection_error) from None
            raise
