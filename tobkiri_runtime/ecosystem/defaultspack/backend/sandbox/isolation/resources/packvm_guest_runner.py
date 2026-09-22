#!/usr/bin/env python3
"""Authenticated artifact staging endpoint for the managed PackVM guest."""

from __future__ import annotations

import base64
import binascii
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import hmac
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import signal
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from typing import Protocol


PROTOCOL = "io.tobkiri.packvm-supervisor.v1"
BUILD_ID = "tobkiri-packvm-runner-3"
ARTIFACT_ROOT = Path("/var/lib/tobkiri-packvm/artifacts")
REQUEST_ROOT = Path("/run/tobkiri-packvm/requests")
MAX_REQUEST_BYTES = 700 * 1024 * 1024
MAX_FILES = 10_000
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ARTIFACT_STORAGE_BYTES = 768 * 1024 * 1024
MIN_GUEST_FREE_RESERVE_BYTES = 512 * 1024 * 1024
MAX_ARTIFACT_METADATA_BYTES = 16 * 1024 * 1024
MAX_RESULT_BYTES = 16 * 1024 * 1024
CANCEL_GRACE_SECONDS = 0.25
PACK_UID = 65534
PACK_GID = 65534
PACKVM_BRIDGE_PROTOCOL = "io.tobkiri.packvm.bridge.v1"
PACKVM_BRIDGE_VERSION = 1
PACKVM_BRIDGE_REQUEST_KIND = "tobkiri.packvm.bridge.request.v1"
PACKVM_BRIDGE_RESULT_KIND = "tobkiri.packvm.bridge.result.v1"
PACKVM_CONTINUATION_KIND = "tobkiri.packvm.continuation.v1"
PACKVM_BRIDGE_HOST_RESULT_KIND = "tobkiri.packvm.bridge.host-result.v1"
PACKVM_BRIDGE_TARGET = {
    "contract_id": "tobkiri.service.ai.generate.v1",
    "operation_id": "rumi_ai_gateway_pack.ai-gateway.generate",
}
MAX_BRIDGE_REQUEST_BYTES = 64 * 1024
MAX_BRIDGE_RESULT_BYTES = 512 * 1024
PACKVM_GUEST_AGENT_PORT = 19001
PACKVM_GUEST_AGENT_CONFIG = Path("/run/tobkiri-packvm/agent-config.json")
PACKVM_GUEST_AGENT_KEY = Path("/run/tobkiri-packvm/agent-ed25519.pem")
PACKVM_GUEST_AGENT_REQUEST_PROTOCOL = PROTOCOL
PACKVM_GUEST_AGENT_RESPONSE_PROTOCOL = "io.tobkiri.macos-vz-supervisor.v1"
PACKVM_GUEST_AGENT_RESPONSE_KIND = "tobkiri.packvm.guest.response.v1"
PACKVM_GUEST_AGENT_RESPONSE_VERSION = 1
PACKVM_GUEST_AGENT_VERSION = 1
MAX_AGENT_REQUEST_BYTES = 1024 * 1024
MAX_AGENT_RESPONSE_BYTES = MAX_RESULT_BYTES
AGENT_IO_TIMEOUT_SECONDS = 30.0
MAX_PENDING_BRIDGES = 64
MAX_SEEN_AGENT_CHALLENGES = 256
PENDING_BRIDGE_TTL_SECONDS = 60.0
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_IDENTIFIER = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_BRIDGE_NONCE = re.compile(r"^[a-f0-9]{48}$")
_AGENT_CHALLENGE = re.compile(r"^[a-f0-9]{64}$")


def main() -> int:
    """Serve one bounded request from stdin and emit one JSON response."""

    try:
        if sys.argv[1:]:
            if len(sys.argv) == 3 and sys.argv[1] == "--execute":
                return _execute_staged_module(Path(sys.argv[2]))
            if len(sys.argv) == 2 and sys.argv[1] == "--serve-vsock":
                return _serve_vsock_agent()
            raise ValueError("PackVM runner arguments are invalid")
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("request exceeds size limit")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        operation = request.get("operation")
        if operation == "doctor":
            response = {
                "ok": True,
                "protocol": PROTOCOL,
                "build_id": BUILD_ID,
            }
        elif operation == "materialize":
            response = _materialize(request)
        elif (
            operation == "invoke"
            and request.get("contract_id") == "io.tobkiri.packvm.attestation.v1"
            and request.get("operation_id") == "challenge"
            and isinstance(request.get("payload"), dict)
            and isinstance(request["payload"].get("challenge"), str)
            and len(request["payload"]["challenge"]) == 64
        ):
            challenge = request["payload"]["challenge"]
            response = {
                "ok": True,
                "protocol": PROTOCOL,
                "payload": {
                    "challenge_digest": _sha256(challenge.encode()),
                },
            }
        elif operation == "invoke":
            response = _invoke(request)
        elif operation == "cancel":
            response = _cancel(request)
        else:
            raise ValueError("unsupported operation")
    except (binascii.Error, OSError, ValueError, json.JSONDecodeError) as exc:
        response = {
            "ok": False,
            "error": "invalid_request",
            "message": str(exc),
            "protocol": PROTOCOL,
        }
    sys.stdout.write(json.dumps(response, sort_keys=True, separators=(",", ":")))
    sys.stdout.write("\n")
    return 0


def _invoke(request: dict[str, object]) -> dict[str, object]:
    """Execute one digest-pinned implementation through the finite PackVM ABI."""

    required = {
        "operation",
        "request_id",
        "target_domain",
        "artifact_digest",
        "materialization_digest",
        "guest_artifact_identity",
        "contract_id",
        "contract_version",
        "operation_id",
        "payload",
        "request_digest",
        "deadline_monotonic",
        "cancel_token",
    }
    if set(request) != required:
        raise ValueError("PackVM invocation fields are invalid")
    for field in ("request_id", "target_domain", "contract_version"):
        if not isinstance(request[field], str) or not request[field]:
            raise ValueError(f"PackVM invocation {field} is invalid")
    if not isinstance(request["payload"], dict):
        raise ValueError("PackVM invocation payload must be an object")
    _digest(request["request_digest"], "request_digest")
    if not isinstance(request["deadline_monotonic"], (int, float)):
        raise ValueError("PackVM invocation deadline is invalid")
    cancel_token = str(request["cancel_token"] or "")
    if len(cancel_token) != 64 or any(value not in "0123456789abcdef" for value in cancel_token):
        raise ValueError("PackVM invocation cancel token is invalid")
    if os.geteuid() != 0:
        raise ValueError("PackVM invocation requires the root-owned supervisor")
    identity = _verify_invocation_artifact(request)
    artifact_digest = _digest(request["artifact_digest"], "artifact_digest")
    materialization_digest = _digest(request["materialization_digest"], "materialization_digest")
    target = (
        ARTIFACT_ROOT
        / artifact_digest.removeprefix("sha256:")
        / materialization_digest.removeprefix("sha256:")
    )
    manifest = _load_manifest(target)
    implementation_path = _relative_path(manifest.get("implementation_path"))
    implementation = target.joinpath(*PurePosixPath(implementation_path).parts)
    child_request = {
        "contract_id": _identifier(request["contract_id"], "contract_id"),
        "operation_id": _identifier(request["operation_id"], "operation_id"),
        "payload": request["payload"],
    }
    process = _spawn_staged_implementation(target, implementation)
    try:
        _register_request(request, process.pid, cancel_token)
    except Exception:
        _terminate_process_group(process.pid)
        process.communicate()
        raise
    try:
        result = _communicate_staged_implementation(process, child_request)
        if _looks_like_bridge_request(result):
            result = _validate_bridge_request(result)
    finally:
        _unregister_request(str(request["request_id"]), process.pid)
    return {
        "ok": True,
        "protocol": PROTOCOL,
        "guest_artifact_identity": identity,
        "payload": result,
    }


def _spawn_staged_implementation(target: Path, implementation: Path) -> subprocess.Popen[bytes]:
    """Start one untrusted artifact child without the Host bridge descriptor."""

    return subprocess.Popen(
        _sandbox_argv(target, implementation),
        cwd=target,
        # Never pass a supervisor descriptor, socket, signing key, or session
        # into the Pack. The root-owned guest agent resumes a fresh sandbox.
        env={"PATH": "/usr/bin:/bin"},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
    )


def _communicate_staged_implementation(
    process: subprocess.Popen[bytes],
    child_request: dict[str, object],
) -> dict[str, object]:
    """Run one sandboxed ABI step and return its one bounded object result."""

    encoded = _bridge_canonical_json(child_request)
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("PackVM invocation payload exceeds size limit")
    try:
        stdout, stderr = process.communicate(encoded, timeout=60.0)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process.pid)
        process.communicate()
        raise ValueError("PackVM invocation timed out") from exc
    if process.returncode != 0:
        # Child stderr is artifact-controlled.  Do not include it in errors
        # that cross the authenticated supervisor boundary.
        del stderr
        raise ValueError("PackVM implementation failed")
    if len(stdout) > MAX_RESULT_BYTES:
        raise ValueError("PackVM invocation result exceeds size limit")
    result = json.loads(stdout)
    if not isinstance(result, dict):
        raise ValueError("PackVM implementation result must be an object")
    return result


def _looks_like_bridge_request(value: dict[str, object]) -> bool:
    """Return whether an artifact selected the explicit bridge ABI path."""

    return value.get("kind") == PACKVM_BRIDGE_REQUEST_KIND


def _validate_bridge_request(value: object) -> dict[str, object]:
    """Accept only the fixed Conversation-to-AI bridge request ABI."""

    bridge_request = _exact_bridge_object(
        value,
        {
            "kind",
            "protocol",
            "version",
            "target",
            "request",
            "request_digest",
            "continuation",
        },
        "PackVM bridge request",
    )
    if (
        bridge_request["kind"] != PACKVM_BRIDGE_REQUEST_KIND
        or bridge_request["protocol"] != PACKVM_BRIDGE_PROTOCOL
        or bridge_request["version"] != PACKVM_BRIDGE_VERSION
    ):
        raise ValueError("PackVM bridge request identity is invalid")
    target = _validate_bridge_target(bridge_request["target"])
    requested = _exact_bridge_object(
        bridge_request["request"],
        {"messages", "requirements"},
        "PackVM bridge request payload",
    )
    messages = requested["messages"]
    if not isinstance(messages, list) or not messages:
        raise ValueError("PackVM bridge messages are invalid")
    request_payload = {
        "messages": _bounded_bridge_json(messages),
        "requirements": {"request_surface": "defaultspack.conversation"},
    }
    if requested["requirements"] != request_payload["requirements"]:
        raise ValueError("PackVM bridge request surface is invalid")
    if len(_bridge_canonical_json(request_payload)) > MAX_BRIDGE_REQUEST_BYTES:
        raise ValueError("PackVM bridge request exceeds the size limit")
    request_digest = _digest(
        bridge_request["request_digest"],
        "PackVM bridge request digest",
    )
    if not hmac.compare_digest(
        request_digest,
        _bridge_canonical_digest(request_payload),
    ):
        raise ValueError("PackVM bridge request digest is invalid")
    continuation = _validate_bridge_continuation(
        bridge_request["continuation"],
        target=target,
        request_digest=request_digest,
    )
    return {
        "kind": PACKVM_BRIDGE_REQUEST_KIND,
        "protocol": PACKVM_BRIDGE_PROTOCOL,
        "version": PACKVM_BRIDGE_VERSION,
        "target": target,
        "request": request_payload,
        "request_digest": request_digest,
        "continuation": continuation,
    }


def _validate_host_bridge_result(
    value: object,
    *,
    request_id: str,
    target_domain: str,
    guest_artifact_identity: str,
    request_digest: str,
    bridge_request_digest: str,
    continuation: object,
) -> dict[str, object]:
    """Validate the helper-bound Host result and return its Pack ABI result."""

    response = _exact_bridge_object(
        value,
        {
            "kind",
            "protocol",
            "version",
            "request_id",
            "target_domain",
            "guest_artifact_identity",
            "request_digest",
            "bridge_request_digest",
            "continuation_nonce",
            "bridge_result",
            "bridge_result_digest",
        },
        "PackVM Host bridge result",
    )
    expected = {
        "kind": PACKVM_BRIDGE_HOST_RESULT_KIND,
        "protocol": PACKVM_BRIDGE_PROTOCOL,
        "version": PACKVM_BRIDGE_VERSION,
        "request_id": request_id,
        "target_domain": target_domain,
        "guest_artifact_identity": guest_artifact_identity,
        "request_digest": request_digest,
        "bridge_request_digest": bridge_request_digest,
    }
    if any(response[field] != expected_value for field, expected_value in expected.items()):
        raise ValueError("PackVM Host bridge result binding is invalid")
    checked_continuation = _checked_continuation(continuation)
    if not hmac.compare_digest(
        str(response["continuation_nonce"]),
        str(checked_continuation["nonce"]),
    ):
        raise ValueError("PackVM Host bridge continuation binding is invalid")
    bridge_result = _validate_bridge_result(
        response["bridge_result"],
        checked_continuation,
    )
    if len(_bridge_canonical_json(bridge_result)) > MAX_BRIDGE_RESULT_BYTES:
        raise ValueError("PackVM Host bridge result exceeds the size limit")
    result_digest = _digest(
        response["bridge_result_digest"],
        "PackVM Host bridge result digest",
    )
    if not hmac.compare_digest(result_digest, _bridge_canonical_digest(bridge_result)):
        raise ValueError("PackVM Host bridge result digest is invalid")
    return bridge_result


def _checked_continuation(value: object) -> dict[str, object]:
    """Validate an already-emitted continuation without reconstructing it."""

    raw = _exact_bridge_object(
        value,
        {
            "kind",
            "protocol",
            "version",
            "operation_id",
            "nonce",
            "target",
            "request_digest",
        },
        "PackVM bridge continuation",
    )
    return _validate_bridge_continuation(
        raw,
        target=_validate_bridge_target(raw["target"]),
        request_digest=_digest(raw["request_digest"], "PackVM bridge continuation digest"),
    )


def _validate_bridge_continuation(
    value: object,
    *,
    target: dict[str, str],
    request_digest: str,
) -> dict[str, object]:
    """Return an exact continuation that may be resumed once by the Pack ABI."""

    continuation = _exact_bridge_object(
        value,
        {
            "kind",
            "protocol",
            "version",
            "operation_id",
            "nonce",
            "target",
            "request_digest",
        },
        "PackVM bridge continuation",
    )
    if (
        continuation["kind"] != PACKVM_CONTINUATION_KIND
        or continuation["protocol"] != PACKVM_BRIDGE_PROTOCOL
        or continuation["version"] != PACKVM_BRIDGE_VERSION
        or continuation["operation_id"] != "complete"
        or _validate_bridge_target(continuation["target"]) != target
        or not hmac.compare_digest(str(continuation["request_digest"]), request_digest)
    ):
        raise ValueError("PackVM bridge continuation is invalid")
    nonce = continuation["nonce"]
    if not isinstance(nonce, str) or _BRIDGE_NONCE.fullmatch(nonce) is None:
        raise ValueError("PackVM bridge continuation nonce is invalid")
    return {
        "kind": PACKVM_CONTINUATION_KIND,
        "protocol": PACKVM_BRIDGE_PROTOCOL,
        "version": PACKVM_BRIDGE_VERSION,
        "operation_id": "complete",
        "nonce": nonce,
        "target": dict(target),
        "request_digest": request_digest,
    }


def _validate_bridge_result(
    value: object,
    continuation: dict[str, object],
) -> dict[str, object]:
    """Validate the response that is safe to pass into the second child ABI."""

    bridge_result = _exact_bridge_object(
        value,
        {
            "kind",
            "protocol",
            "version",
            "operation_id",
            "nonce",
            "target",
            "request_digest",
            "result",
            "result_digest",
        },
        "PackVM bridge result",
    )
    if (
        bridge_result["kind"] != PACKVM_BRIDGE_RESULT_KIND
        or bridge_result["protocol"] != PACKVM_BRIDGE_PROTOCOL
        or bridge_result["version"] != PACKVM_BRIDGE_VERSION
        or bridge_result["operation_id"] != "complete"
        or not hmac.compare_digest(
            str(bridge_result["nonce"]), str(continuation["nonce"])
        )
        or _validate_bridge_target(bridge_result["target"])
        != continuation["target"]
        or not hmac.compare_digest(
            str(bridge_result["request_digest"]),
            str(continuation["request_digest"]),
        )
    ):
        raise ValueError("PackVM bridge result does not match its continuation")
    outcome = _validate_bridge_outcome(bridge_result["result"])
    result_digest = _digest(bridge_result["result_digest"], "PackVM bridge result digest")
    if not hmac.compare_digest(result_digest, _bridge_canonical_digest(outcome)):
        raise ValueError("PackVM bridge result digest is invalid")
    return {
        "kind": PACKVM_BRIDGE_RESULT_KIND,
        "protocol": PACKVM_BRIDGE_PROTOCOL,
        "version": PACKVM_BRIDGE_VERSION,
        "operation_id": "complete",
        "nonce": continuation["nonce"],
        "target": dict(PACKVM_BRIDGE_TARGET),
        "request_digest": continuation["request_digest"],
        "result": outcome,
        "result_digest": result_digest,
    }


def _validate_bridge_outcome(value: object) -> dict[str, object]:
    """Keep the resumed artifact input bounded and typed before sandbox entry."""

    if not isinstance(value, dict):
        raise ValueError("PackVM bridge outcome is invalid")
    if value.get("status") == "ok":
        outcome = _exact_bridge_object(value, {"status", "value"}, "PackVM bridge success")
        if not isinstance(outcome["value"], dict):
            raise ValueError("PackVM bridge success value is invalid")
        return {"status": "ok", "value": _bounded_bridge_json(outcome["value"])}
    if value.get("status") == "error":
        outcome = _exact_bridge_object(value, {"status", "error"}, "PackVM bridge error")
        error = _exact_bridge_object(outcome["error"], {"code", "message"}, "PackVM bridge error")
        code = error["code"]
        message = error["message"]
        if (
            not isinstance(code, str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) is None
            or not isinstance(message, str)
            or not message.strip()
            or len(message) > 512
        ):
            raise ValueError("PackVM bridge error is invalid")
        return {"status": "error", "error": {"code": code, "message": message}}
    raise ValueError("PackVM bridge outcome status is invalid")


def _validate_bridge_target(value: object) -> dict[str, str]:
    """Ensure the Pack cannot select a different Host capability target."""

    target = _exact_bridge_object(value, set(PACKVM_BRIDGE_TARGET), "PackVM bridge target")
    if target != PACKVM_BRIDGE_TARGET:
        raise ValueError("PackVM bridge target is not permitted")
    return dict(PACKVM_BRIDGE_TARGET)


def _exact_bridge_object(
    value: object,
    fields: set[str],
    label: str,
) -> dict[str, object]:
    """Copy an object only when its protocol field set is exact."""

    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label} fields are invalid")
    return dict(value)


def _bounded_bridge_json(value: object, *, depth: int = 0) -> object:
    """Copy an I-JSON-compatible value with finite nesting and scalar bounds."""

    if depth > 8:
        raise ValueError("PackVM bridge JSON nesting exceeds the limit")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -(2**53) < value < 2**53:
            raise ValueError("PackVM bridge integer is outside the safe range")
        return value
    if isinstance(value, str):
        if len(value) > 16 * 1024:
            raise ValueError("PackVM bridge string exceeds the limit")
        return value
    if isinstance(value, list):
        if len(value) > 64:
            raise ValueError("PackVM bridge array exceeds the limit")
        return [_bounded_bridge_json(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > 64:
            raise ValueError("PackVM bridge object exceeds the limit")
        copied: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise ValueError("PackVM bridge object key is invalid")
            copied[key] = _bounded_bridge_json(item, depth=depth + 1)
        return copied
    raise ValueError("PackVM bridge value must be JSON-compatible")


def _normalise_bridge_deadline(value: object) -> str:
    """Encode the Host deadline deterministically without non-finite JSON."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("PackVM invocation deadline is invalid")
    deadline = float(value)
    if not math.isfinite(deadline):
        raise ValueError("PackVM invocation deadline is invalid")
    return format(deadline, ".17g")


def _bridge_canonical_json(value: object) -> bytes:
    """Return deterministic I-JSON bytes for the private bridge frame."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _bridge_canonical_digest(value: object) -> str:
    """Return the SHA-256 binding used only by the private bridge protocol."""

    return _sha256(_bridge_canonical_json(value))


class _AgentSigner(Protocol):
    """Root-only signer for authenticated guest-agent responses."""

    def sign(self, payload: bytes) -> bytes:
        """Return an Ed25519 signature over canonical response bytes."""


@dataclass(frozen=True)
class _VsockAgentConfig:
    """Launch-bound identities that the guest agent must echo exactly."""

    domain_id: str
    binding_digests: dict[str, str]
    private_key_path: Path


@dataclass(frozen=True)
class _PendingBridge:
    """One bounded, one-shot continuation held only by the root guest agent."""

    request: dict[str, object]
    guest_artifact_identity: str
    bridge_request: dict[str, object]
    expires_at: float


class _PendingBridgeLedger:
    """Fence replayed Host bridge results across fresh Pack child processes."""

    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], _PendingBridge] = {}
        self._seen_challenges: OrderedDict[str, None] = OrderedDict()
        self._cancelled: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._lock = threading.RLock()

    def accept_challenge(self, challenge: object) -> str:
        """Consume one helper freshness challenge before dispatching its request."""

        with self._lock:
            if (
                not isinstance(challenge, str)
                or _AGENT_CHALLENGE.fullmatch(challenge) is None
            ):
                raise ValueError("PackVM guest agent challenge is invalid")
            if challenge in self._seen_challenges:
                raise ValueError("PackVM guest agent challenge was replayed")
            if len(self._seen_challenges) >= MAX_SEEN_AGENT_CHALLENGES:
                # Helper challenges are 256-bit and the Host's authenticated
                # ledger remains authoritative.  Keeping a fixed local window
                # prevents an otherwise permanent availability failure.
                self._seen_challenges.popitem(last=False)
            self._seen_challenges[challenge] = None
            return challenge

    def add(
        self,
        *,
        domain_id: str,
        request: dict[str, object],
        guest_artifact_identity: str,
        bridge_request: dict[str, object],
    ) -> None:
        """Persist one exact initial turn until its Host result is received."""

        with self._lock:
            self._purge_expired()
            key = (domain_id, str(request["request_id"]))
            if key in self._cancelled:
                raise ValueError("PackVM bridge request was cancelled")
            if key in self._pending:
                raise ValueError("PackVM bridge request is already pending")
            if len(self._pending) >= MAX_PENDING_BRIDGES:
                raise ValueError("PackVM bridge ledger is full")
            self._pending[key] = _PendingBridge(
                request=dict(request),
                guest_artifact_identity=guest_artifact_identity,
                bridge_request=dict(bridge_request),
                expires_at=time.monotonic() + PENDING_BRIDGE_TTL_SECONDS,
            )

    def consume(self, *, domain_id: str, request_id: str) -> _PendingBridge:
        """Remove and return a one-shot pending continuation before resuming it."""

        with self._lock:
            self._purge_expired()
            pending = self._pending.pop((domain_id, request_id), None)
            if pending is None:
                raise ValueError("PackVM bridge continuation is unavailable")
            return pending

    def cancel(self, *, domain_id: str, request_id: str) -> bool:
        """Erase an unconsumed bridge continuation for an authenticated cancel."""

        with self._lock:
            self._purge_expired()
            key = (domain_id, request_id)
            cancelled = self._pending.pop(key, None) is not None
            if len(self._cancelled) >= MAX_PENDING_BRIDGES:
                self._cancelled.popitem(last=False)
            self._cancelled[key] = time.monotonic() + PENDING_BRIDGE_TTL_SECONDS
            return cancelled

    def _purge_expired(self) -> None:
        now = time.monotonic()
        for key, pending in tuple(self._pending.items()):
            if pending.expires_at <= now:
                self._pending.pop(key, None)
        for key, expires_at in tuple(self._cancelled.items()):
            if expires_at <= now:
                self._cancelled.pop(key, None)


class _OpenSSLAgentSigner:
    """Use the root-only Ed25519 key without importing it into Python or Pack."""

    def __init__(self, key_path: Path) -> None:
        self._key_path = key_path

    def sign(self, payload: bytes) -> bytes:
        """Sign canonical bytes via OpenSSL and return only the detached signature."""

        _assert_root_only_regular_file(self._key_path, "PackVM guest agent key")
        try:
            completed = subprocess.run(
                (
                    "/usr/bin/openssl",
                    "pkeyutl",
                    "-sign",
                    "-rawin",
                    "-inkey",
                    str(self._key_path),
                ),
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5.0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("PackVM guest agent signer is unavailable") from exc
        if completed.returncode != 0 or len(completed.stdout) != 64:
            raise ValueError("PackVM guest agent signature is invalid")
        return completed.stdout


def _serve_vsock_agent() -> int:
    """Run the root-owned, no-network VZ virtio-socket guest agent."""

    if os.geteuid() != 0:
        raise ValueError("PackVM guest agent requires the root-owned supervisor")
    config = _load_vsock_agent_config(PACKVM_GUEST_AGENT_CONFIG)
    vsock_family = getattr(socket, "AF_VSOCK", None)
    vmaddr_any = getattr(socket, "VMADDR_CID_ANY", None)
    if not isinstance(vsock_family, int) or not isinstance(vmaddr_any, int):
        raise ValueError("PackVM guest agent requires AF_VSOCK")
    listener = socket.socket(vsock_family, socket.SOCK_STREAM)
    try:
        listener.bind((vmaddr_any, PACKVM_GUEST_AGENT_PORT))
        listener.listen(8)
        return _serve_authenticated_guest_agent(
            listener,
            config,
            _OpenSSLAgentSigner(config.private_key_path),
        )
    finally:
        listener.close()


def _load_vsock_agent_config(path: Path) -> _VsockAgentConfig:
    """Load only a root-owned launch binding and root-only signing-key path."""

    _assert_root_only_regular_file(path, "PackVM guest agent configuration")
    raw = path.read_bytes()
    if len(raw) > 16 * 1024:
        raise ValueError("PackVM guest agent configuration exceeds the size limit")
    value = json.loads(raw)
    config = _exact_bridge_object(
        value,
        {"version", "domain_id", "binding_digests", "private_key_path"},
        "PackVM guest agent configuration",
    )
    if config["version"] != PACKVM_GUEST_AGENT_VERSION:
        raise ValueError("PackVM guest agent configuration version is invalid")
    domain_id = config["domain_id"]
    if not isinstance(domain_id, str) or not domain_id:
        raise ValueError("PackVM guest agent configuration domain is invalid")
    bindings = config["binding_digests"]
    required_bindings = {
        "domain",
        "lease",
        "reservation",
        "image",
        "agent",
        "config",
        "disk",
        "guest_public_key",
        "efi_variable_store",
        "artifact",
        "executable",
        "materialization",
    }
    optional_linux_bindings = {"kernel", "initrd"}
    if not isinstance(bindings, dict) or set(bindings) not in (
        required_bindings,
        {*required_bindings, *optional_linux_bindings},
    ):
        raise ValueError("PackVM guest agent configuration bindings are invalid")
    copied_bindings = {
        key: _digest(value, f"PackVM guest agent {key} binding")
        for key, value in bindings.items()
    }
    key_value = config["private_key_path"]
    if not isinstance(key_value, str) or not key_value:
        raise ValueError("PackVM guest agent configuration key is invalid")
    key_path = Path(key_value)
    if not key_path.is_absolute():
        raise ValueError("PackVM guest agent key path is invalid")
    return _VsockAgentConfig(
        domain_id=domain_id,
        binding_digests=copied_bindings,
        private_key_path=key_path,
    )


def _assert_root_only_regular_file(path: Path, label: str) -> None:
    """Reject symlinks, hardlinks, or non-root-readable key/config material."""

    metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError(f"{label} is unsafe")


def _serve_authenticated_guest_agent(
    listener: socket.socket,
    config: _VsockAgentConfig,
    signer: _AgentSigner,
    *,
    max_requests: int | None = None,
) -> int:
    """Serve bounded canonical helper requests with signed bound responses.

    ``listener`` and ``signer`` are injected so this protocol is fully tested
    with a local Unix socket and fake signer; production binds AF_VSOCK only.
    """

    ledger = _PendingBridgeLedger()
    workers: list[threading.Thread] = []

    def serve_connection(connection: socket.socket) -> None:
        try:
            _serve_agent_connection(connection, config, signer, ledger)
        finally:
            connection.close()

    served = 0
    while max_requests is None or served < max_requests:
        connection, _address = listener.accept()
        worker = threading.Thread(
            target=serve_connection,
            args=(connection,),
            daemon=True,
        )
        worker.start()
        workers.append(worker)
        served += 1
    for worker in workers:
        worker.join()
    return 0


def _serve_agent_connection(
    connection: socket.socket,
    config: _VsockAgentConfig,
    signer: _AgentSigner,
    ledger: _PendingBridgeLedger,
) -> None:
    """Handle one helper request without exposing raw local diagnostics."""

    request: dict[str, object] | None = None
    try:
        request = _read_agent_request(connection)
        response = _dispatch_agent_request(request, config, ledger)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        response = _safe_agent_error_response(request, exc)
    signed = _sign_agent_response(response, signer)
    encoded = _bridge_canonical_json(signed)
    if len(encoded) > MAX_AGENT_RESPONSE_BYTES:
        raise ValueError("PackVM guest agent response exceeds the size limit")
    connection.sendall(encoded + b"\n")


def _read_agent_request(connection: socket.socket) -> dict[str, object]:
    """Read one canonical newline-delimited helper envelope under a hard cap."""

    connection.settimeout(AGENT_IO_TIMEOUT_SECONDS)
    content = bytearray()
    while True:
        chunk = connection.recv(min(64 * 1024, MAX_AGENT_REQUEST_BYTES + 1 - len(content)))
        if not chunk:
            raise ValueError("PackVM guest agent request ended before newline")
        content.extend(chunk)
        if len(content) > MAX_AGENT_REQUEST_BYTES:
            raise ValueError("PackVM guest agent request exceeds the size limit")
        if b"\n" in chunk:
            break
    if content.count(b"\n") != 1 or not content.endswith(b"\n"):
        raise ValueError("PackVM guest agent request framing is invalid")
    encoded = bytes(content[:-1])
    value = json.loads(encoded)
    if not isinstance(value, dict) or not hmac.compare_digest(
        encoded, _bridge_canonical_json(value)
    ):
        raise ValueError("PackVM guest agent request is invalid")
    return value


def _dispatch_agent_request(
    request: dict[str, object],
    config: _VsockAgentConfig,
    ledger: _PendingBridgeLedger,
) -> dict[str, object]:
    """Verify launch bindings and execute only invoke/cancel/bridge-result."""

    base = _validate_agent_envelope(request, config, ledger)
    operation = base["operation"]
    request_id = base["request_id"]
    if operation == "invoke":
        payload = base["payload"]
        if not isinstance(payload, dict):
            raise ValueError("PackVM guest agent invocation payload is invalid")
        if (
            payload.get("operation") != "invoke"
            or payload.get("request_id") != request_id
            or payload.get("target_domain") != config.domain_id
        ):
            raise ValueError("PackVM guest agent invocation binding is invalid")
        result = _invoke(dict(payload))
        bridge = result.get("payload")
        if isinstance(bridge, dict) and _looks_like_bridge_request(bridge):
            checked_bridge = _validate_bridge_request(bridge)
            ledger.add(
                domain_id=config.domain_id,
                request=dict(payload),
                guest_artifact_identity=str(result["guest_artifact_identity"]),
                bridge_request=checked_bridge,
            )
            return _agent_success(
                base,
                {
                    "state": "pending",
                    "host_bridge_request": {
                        "kind": "tobkiri.packvm.bridge.host-request.v1",
                        "protocol": PACKVM_BRIDGE_PROTOCOL,
                        "version": PACKVM_BRIDGE_VERSION,
                        "request_id": request_id,
                        "target_domain": config.domain_id,
                        "guest_artifact_identity": result[
                            "guest_artifact_identity"
                        ],
                        "request_digest": payload["request_digest"],
                        "bridge_request_digest": _bridge_canonical_digest(
                            checked_bridge
                        ),
                        "bridge_request": checked_bridge,
                        "deadline_monotonic": _normalise_bridge_deadline(
                            payload["deadline_monotonic"]
                        ),
                    },
                },
            )
        return _agent_success(base, result)
    if operation == "bridge_result":
        host_bridge_result = base["host_bridge_result"]
        pending = ledger.consume(domain_id=config.domain_id, request_id=request_id)
        bridge_result = _validate_host_bridge_result(
            host_bridge_result,
            request_id=request_id,
            target_domain=config.domain_id,
            guest_artifact_identity=pending.guest_artifact_identity,
            request_digest=_digest(pending.request["request_digest"], "request_digest"),
            bridge_request_digest=_bridge_canonical_digest(pending.bridge_request),
            continuation=pending.bridge_request["continuation"],
        )
        result = _resume_bridge_invocation(
            pending.request,
            pending.bridge_request,
            bridge_result,
        )
        return _agent_success(base, result)
    if operation == "attest":
        if request_id != f"attest-{config.domain_id}":
            raise ValueError("PackVM guest attestation request id is invalid")
        return _agent_success(
            base,
            {
                "guest_artifact_identity": _bridge_canonical_digest(
                    config.binding_digests
                ),
            },
        )
    if operation == "cancel":
        cancelled = ledger.cancel(domain_id=config.domain_id, request_id=request_id)
        signals = _cancel_agent_execution(config.domain_id, request_id)
        return _agent_success(
            base,
            {
                "ok": True,
                "protocol": PROTOCOL,
                "operation": "cancel",
                "request_id": request_id,
                "target_domain": config.domain_id,
                "state": "cancelled",
                "signals": signals,
                "pending_bridge_cancelled": cancelled,
            },
        )
    raise ValueError("PackVM guest agent operation is invalid")


def _cancel_agent_execution(domain_id: str, request_id: str) -> list[str]:
    """Cancel a currently running child using only the root ownership record."""

    try:
        record = _read_request(_request_path(request_id))
    except (OSError, ValueError):
        return []
    if not hmac.compare_digest(str(record.get("target_domain") or ""), domain_id):
        return []
    response = _cancel(
        {
            "operation": "cancel",
            "request_id": request_id,
            "target_domain": domain_id,
            "guest_artifact_identity": record["guest_artifact_identity"],
            "cancel_token": record["cancel_token"],
        }
    )
    signals = response.get("signals")
    if not isinstance(signals, list) or any(
        value not in {"TERM", "KILL"} for value in signals
    ):
        raise ValueError("PackVM guest cancellation acknowledgement is invalid")
    return list(signals)


def _validate_agent_envelope(
    request: dict[str, object],
    config: _VsockAgentConfig,
    ledger: _PendingBridgeLedger,
) -> dict[str, object]:
    """Validate one helper envelope against the authenticated launch binding."""

    operation = request.get("operation")
    fields_by_operation = {
        "invoke": {
            "protocol",
            "operation",
            "request_id",
            "domain_id",
            "binding_digests",
            "payload",
            "guest_challenge",
        },
        "bridge_result": {
            "protocol",
            "operation",
            "request_id",
            "domain_id",
            "binding_digests",
            "host_bridge_result",
            "guest_challenge",
        },
        "attest": {
            "protocol",
            "operation",
            "request_id",
            "domain_id",
            "binding_digests",
            "attestation_nonce",
            "guest_challenge",
        },
        "cancel": {
            "protocol",
            "operation",
            "request_id",
            "domain_id",
            "binding_digests",
            "guest_challenge",
        },
    }
    if not isinstance(operation, str) or operation not in fields_by_operation:
        raise ValueError("PackVM guest agent operation is invalid")
    envelope = _exact_bridge_object(
        request,
        fields_by_operation[operation],
        "PackVM guest agent request",
    )
    if envelope["protocol"] != PACKVM_GUEST_AGENT_REQUEST_PROTOCOL:
        raise ValueError("PackVM guest agent protocol is invalid")
    request_id = envelope["request_id"]
    if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
        raise ValueError("PackVM guest agent request id is invalid")
    if envelope["domain_id"] != config.domain_id:
        raise ValueError("PackVM guest agent domain binding is invalid")
    if envelope["binding_digests"] != config.binding_digests:
        raise ValueError("PackVM guest agent launch binding is invalid")
    if operation == "attest":
        nonce = envelope["attestation_nonce"]
        if not isinstance(nonce, str) or _AGENT_CHALLENGE.fullmatch(nonce) is None:
            raise ValueError("PackVM guest attestation nonce is invalid")
    ledger.accept_challenge(envelope["guest_challenge"])
    return envelope


def _agent_success(
    request: dict[str, object],
    data: dict[str, object],
) -> dict[str, object]:
    """Build the exact unsigned success envelope that the agent signs."""

    response = {
        "kind": PACKVM_GUEST_AGENT_RESPONSE_KIND,
        "protocol": PACKVM_GUEST_AGENT_RESPONSE_PROTOCOL,
        "version": PACKVM_GUEST_AGENT_RESPONSE_VERSION,
        "operation": request["operation"],
        "request_id": request["request_id"],
        "domain_id": request["domain_id"],
        "binding_digests": request["binding_digests"],
        "guest_challenge": request["guest_challenge"],
        "success": True,
        "data": data,
    }
    if request["operation"] == "attest":
        response["attestation_nonce"] = request["attestation_nonce"]
    return response


def _safe_agent_error_response(
    request: dict[str, object] | None,
    error: Exception,
) -> dict[str, object]:
    """Return a bounded error without leaking socket, path, key, or Pack data."""

    del error
    if isinstance(request, dict):
        operation = request.get("operation")
        request_id = request.get("request_id")
        domain_id = request.get("domain_id")
        bindings = request.get("binding_digests")
        challenge = request.get("guest_challenge")
        if (
            isinstance(operation, str)
            and isinstance(request_id, str)
            and isinstance(domain_id, str)
            and isinstance(bindings, dict)
            and isinstance(challenge, str)
        ):
            response = {
                "kind": PACKVM_GUEST_AGENT_RESPONSE_KIND,
                "protocol": PACKVM_GUEST_AGENT_RESPONSE_PROTOCOL,
                "version": PACKVM_GUEST_AGENT_RESPONSE_VERSION,
                "operation": operation,
                "request_id": request_id,
                "domain_id": domain_id,
                "binding_digests": bindings,
                "guest_challenge": challenge,
                "success": False,
                "error": {
                    "code": "CAPABILITY_UNAVAILABLE",
                    "message": "The authenticated PackVM operation was rejected.",
                },
            }
            if operation == "attest" and isinstance(
                request.get("attestation_nonce"), str
            ):
                response["attestation_nonce"] = request["attestation_nonce"]
            return response
    return {
        "kind": PACKVM_GUEST_AGENT_RESPONSE_KIND,
        "protocol": PACKVM_GUEST_AGENT_RESPONSE_PROTOCOL,
        "version": PACKVM_GUEST_AGENT_RESPONSE_VERSION,
        "operation": "invalid",
        "request_id": "invalid",
        "domain_id": "invalid",
        "binding_digests": {},
        "guest_challenge": "0" * 64,
        "success": False,
        "error": {
            "code": "CAPABILITY_UNAVAILABLE",
            "message": "The authenticated PackVM operation was rejected.",
        },
    }


def _sign_agent_response(
    response: dict[str, object],
    signer: _AgentSigner,
) -> dict[str, object]:
    """Attach exactly one Ed25519 signature over the canonical unsigned map."""

    if response.get("success") is True:
        expected_success_fields = {
            "kind",
            "protocol",
            "version",
            "operation",
            "request_id",
            "domain_id",
            "binding_digests",
            "guest_challenge",
            "success",
            "data",
        }
        if response.get("operation") == "attest":
            expected_success_fields.add("attestation_nonce")
        _exact_bridge_object(
            response,
            expected_success_fields,
            "PackVM guest agent success response",
        )
    else:
        expected_error_fields = {
            "kind",
            "protocol",
            "version",
            "operation",
            "request_id",
            "domain_id",
            "binding_digests",
            "guest_challenge",
            "success",
            "error",
        }
        if response.get("operation") == "attest":
            expected_error_fields.add("attestation_nonce")
        _exact_bridge_object(
            response,
            expected_error_fields,
            "PackVM guest agent error response",
        )
    signature = signer.sign(_bridge_canonical_json(response))
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise ValueError("PackVM guest agent signer returned an invalid signature")
    return {
        **response,
        "agent_signature": base64.b64encode(signature).decode("ascii"),
    }


def _resume_bridge_invocation(
    request: dict[str, object],
    bridge_request: dict[str, object],
    bridge_result: dict[str, object],
) -> dict[str, object]:
    """Resume exactly once in a fresh Pack sandbox after Host authorization."""

    identity = _verify_invocation_artifact(request)
    artifact_digest = _digest(request["artifact_digest"], "artifact_digest")
    materialization_digest = _digest(
        request["materialization_digest"], "materialization_digest"
    )
    target = (
        ARTIFACT_ROOT
        / artifact_digest.removeprefix("sha256:")
        / materialization_digest.removeprefix("sha256:")
    )
    manifest = _load_manifest(target)
    implementation_path = _relative_path(manifest.get("implementation_path"))
    implementation = target.joinpath(*PurePosixPath(implementation_path).parts)
    child_request = {
        "contract_id": _identifier(request["contract_id"], "contract_id"),
        "operation_id": _identifier(request["operation_id"], "operation_id"),
        "payload": {
            "continuation": bridge_request["continuation"],
            "bridge_result": bridge_result,
        },
    }
    cancel_token = str(request["cancel_token"])
    process = _spawn_staged_implementation(target, implementation)
    try:
        _register_request(request, process.pid, cancel_token)
    except Exception:
        _terminate_process_group(process.pid)
        process.communicate()
        raise
    try:
        result = _communicate_staged_implementation(process, child_request)
    finally:
        _unregister_request(str(request["request_id"]), process.pid)
    if _looks_like_bridge_request(result):
        raise ValueError("PackVM bridge requested more than one Host exchange")
    return {
        "ok": True,
        "protocol": PROTOCOL,
        "guest_artifact_identity": identity,
        "payload": result,
    }


def _execute_staged_module(path: Path) -> int:
    """Private child mode for the explicit staged Python Pack ABI."""

    try:
        if os.geteuid() == 0:
            raise ValueError("PackVM implementation may not execute as root")
        request = json.loads(sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1))
        if not isinstance(request, dict) or set(request) != {
            "contract_id",
            "operation_id",
            "payload",
        }:
            raise ValueError("PackVM child request is invalid")
        specification = importlib.util.spec_from_file_location("_tobkiri_packvm_entry", path)
        if specification is None or specification.loader is None:
            raise ValueError("PackVM implementation cannot be loaded")
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        operation = getattr(module, "tobkiri_packvm_invoke", None)
        if not callable(operation):
            raise ValueError("PackVM implementation does not export tobkiri_packvm_invoke")
        result = operation(request["operation_id"], request["payload"])
        if not isinstance(result, dict):
            raise ValueError("PackVM implementation result must be an object")
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > MAX_RESULT_BYTES:
            raise ValueError("PackVM implementation result exceeds size limit")
        sys.stdout.buffer.write(encoded)
        return 0
    except Exception as error:
        sys.stderr.write(f"{type(error).__name__}: {error}\n")
        return 1


def _sandbox_argv(target: Path, implementation: Path) -> tuple[str, ...]:
    """Build the mandatory default-deny guest sandbox command."""

    bwrap = shutil.which("bwrap")
    prlimit = shutil.which("prlimit")
    if bwrap is None or prlimit is None:
        raise ValueError("PackVM guest requires bubblewrap and prlimit")
    runner = Path(__file__).resolve()
    relative = implementation.relative_to(target).as_posix()
    command = [
        prlimit,
        "--nproc=64",
        "--as=536870912",
        "--cpu=60",
        "--fsize=16777216",
        "--nofile=64",
        "--",
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-net",
        "--uid",
        str(PACK_UID),
        "--gid",
        str(PACK_GID),
        "--cap-drop",
        "ALL",
    ]
    for system_root in ("/usr", "/bin", "/lib", "/lib64", "/etc"):
        if Path(system_root).exists():
            command.extend(("--ro-bind", system_root, system_root))
    command.extend(
        (
            "--ro-bind",
            str(target),
            "/pack",
            "--ro-bind",
            str(runner),
            "/runner.py",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/home",
            "--tmpfs",
            "/run",
            "--dir",
            "/var",
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--setenv",
            "HOME",
            "/home/pack",
            "--chdir",
            "/pack",
            "--",
            sys.executable,
            "-I",
            "-S",
            "/runner.py",
            "--execute",
            f"/pack/{relative}",
        )
    )
    return tuple(command)


def _request_path(request_id: str) -> Path:
    return REQUEST_ROOT / hashlib.sha256(request_id.encode()).hexdigest()


def _register_request(
    request: dict[str, object],
    process_group: int,
    cancel_token: str,
) -> None:
    REQUEST_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(REQUEST_ROOT, 0o700)
    record = {
        "request_id": request["request_id"],
        "target_domain": request["target_domain"],
        "guest_artifact_identity": request["guest_artifact_identity"],
        "cancel_token": cancel_token,
        "process_group": process_group,
    }
    path = _request_path(str(request["request_id"]))
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, json.dumps(record, sort_keys=True).encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unregister_request(request_id: str, process_group: int) -> None:
    path = _request_path(request_id)
    try:
        record = _read_request(path)
        if record.get("process_group") == process_group:
            path.unlink(missing_ok=True)
    except (OSError, ValueError):
        return


def _read_request(path: Path) -> dict[str, object]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0:
            raise ValueError("PackVM request ownership record is unsafe")
        raw = os.read(descriptor, 16 * 1024)
    finally:
        os.close(descriptor)
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {
        "request_id",
        "target_domain",
        "guest_artifact_identity",
        "cancel_token",
        "process_group",
    }:
        raise ValueError("PackVM request ownership record is invalid")
    return value


def _cancel(request: dict[str, object]) -> dict[str, object]:
    required = {
        "operation",
        "request_id",
        "target_domain",
        "guest_artifact_identity",
        "cancel_token",
    }
    if set(request) != required or os.geteuid() != 0:
        raise ValueError("PackVM cancellation fields are invalid")
    request_id = str(request["request_id"] or "")
    if not request_id:
        raise ValueError("PackVM cancellation request_id is invalid")
    record = _read_request(_request_path(request_id))
    for field in ("request_id", "target_domain", "guest_artifact_identity"):
        if not hmac.compare_digest(str(record.get(field) or ""), str(request[field] or "")):
            raise ValueError(f"PackVM cancellation {field} mismatch")
    if not hmac.compare_digest(
        str(record.get("cancel_token") or ""), str(request["cancel_token"] or "")
    ):
        raise ValueError("PackVM cancellation authentication failed")
    process_group = record.get("process_group")
    if not isinstance(process_group, int) or process_group <= 1:
        raise ValueError("PackVM cancellation process group is invalid")
    signals = _terminate_process_group(process_group)
    return {
        "ok": True,
        "protocol": PROTOCOL,
        "operation": "cancel",
        "request_id": request_id,
        "target_domain": request["target_domain"],
        "state": "cancelled",
        "signals": signals,
    }


def _terminate_process_group(process_group: int) -> list[str]:
    signals: list[str] = []
    try:
        os.killpg(process_group, signal.SIGTERM)
        signals.append("TERM")
    except ProcessLookupError:
        return signals
    deadline = time.monotonic() + CANCEL_GRACE_SECONDS
    while time.monotonic() < deadline:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return signals
        time.sleep(0.01)
    try:
        os.killpg(process_group, signal.SIGKILL)
        signals.append("KILL")
    except ProcessLookupError:
        pass
    return signals


def _materialize(request: dict[str, object]) -> dict[str, object]:
    if os.geteuid() != 0:
        raise ValueError("artifact materialization requires the root-owned supervisor")
    with _artifact_storage_lock():
        return _materialize_locked(request)


def _materialize_locked(request: dict[str, object]) -> dict[str, object]:
    """Materialize one artifact while holding the cumulative quota lock."""

    required = {
        "operation",
        "pack_id",
        "artifact_digest",
        "function_id",
        "implementation_digest",
        "implementation_path",
        "materialization_digest",
        "materialization_nonce",
        "files",
    }
    if set(request) != required:
        raise ValueError("artifact materialization fields are invalid")
    pack_id = _identifier(request["pack_id"], "pack_id")
    function_id = _identifier(request["function_id"], "function_id")
    artifact_digest = _digest(request["artifact_digest"], "artifact_digest")
    implementation_digest = _digest(request["implementation_digest"], "implementation_digest")
    materialization_digest = _digest(request["materialization_digest"], "materialization_digest")
    implementation_path = _relative_path(request["implementation_path"])
    nonce = str(request["materialization_nonce"])
    if len(nonce) != 64 or any(character not in "0123456789abcdef" for character in nonce):
        raise ValueError("materialization nonce is invalid")
    raw_files = request["files"]
    if not isinstance(raw_files, list) or not raw_files or len(raw_files) > MAX_FILES:
        raise ValueError("artifact file inventory is invalid")

    files: list[tuple[str, str, bool, bytes]] = []
    seen: set[str] = set()
    total = 0
    for raw_file in raw_files:
        if not isinstance(raw_file, dict) or set(raw_file) != {
            "path",
            "digest",
            "executable",
            "content",
        }:
            raise ValueError("artifact file entry is invalid")
        path = _relative_path(raw_file["path"])
        if path in seen:
            raise ValueError("artifact file path is duplicated")
        seen.add(path)
        digest = _digest(raw_file["digest"], "file digest")
        executable = raw_file["executable"]
        if not isinstance(executable, bool):
            raise ValueError("artifact executable flag is invalid")
        encoded = raw_file["content"]
        if not isinstance(encoded, str):
            raise ValueError("artifact file content is invalid")
        content = base64.b64decode(encoded, validate=True)
        if len(content) > MAX_FILE_BYTES or _sha256(content) != digest:
            raise ValueError("artifact file content digest mismatch")
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("artifact exceeds total size limit")
        files.append((path, digest, executable, content))
    implementations = [item for item in files if item[0] == implementation_path]
    if len(implementations) != 1 or implementations[0][1] != implementation_digest:
        raise ValueError("artifact implementation identity is unavailable")
    expected_materialization = _canonical_digest(
        {
            "pack_id": pack_id,
            "artifact_digest": artifact_digest,
            "function_id": function_id,
            "implementation_digest": implementation_digest,
            "implementation_path": implementation_path,
            "files": [
                {
                    "path": path,
                    "digest": digest,
                    "executable": executable,
                    "size": len(content),
                }
                for path, digest, executable, content in files
            ],
        }
    )
    if materialization_digest != expected_materialization:
        raise ValueError("artifact materialization digest mismatch")

    artifact_hex = artifact_digest.removeprefix("sha256:")
    materialization_hex = materialization_digest.removeprefix("sha256:")
    parent = ARTIFACT_ROOT / artifact_hex
    parent.mkdir(mode=0o700, exist_ok=True)
    if parent.is_symlink():
        raise ValueError("artifact staging parent is symlinked")
    target = parent / materialization_hex
    if target.exists():
        manifest = _load_manifest(target)
        if manifest.get("materialization_nonce") == nonce:
            raise ValueError("artifact materialization replay")
        identity = _verify_staged_artifact(
            target,
            artifact_digest,
            materialization_digest,
        )
        return {
            "ok": True,
            "protocol": PROTOCOL,
            "artifact_digest": artifact_digest,
            "materialization_digest": materialization_digest,
            "guest_artifact_identity": identity,
        }

    stored_bytes = _artifact_storage_bytes()
    projected_bytes = stored_bytes + total + MAX_ARTIFACT_METADATA_BYTES
    if projected_bytes > MAX_ARTIFACT_STORAGE_BYTES:
        raise ValueError(
            "PackVM artifact storage quota exceeded: "
            f"{projected_bytes} bytes projected, "
            f"{MAX_ARTIFACT_STORAGE_BYTES} bytes allowed"
        )
    free_bytes = int(shutil.disk_usage(ARTIFACT_ROOT).free)
    required_free = total + MAX_ARTIFACT_METADATA_BYTES + MIN_GUEST_FREE_RESERVE_BYTES
    if free_bytes < required_free:
        raise ValueError(
            "PackVM guest free space is insufficient: "
            f"{required_free} bytes required, {free_bytes} bytes available"
        )

    temporary = parent / f".{materialization_hex}.{nonce}.tmp"
    if temporary.exists() or temporary.is_symlink():
        raise ValueError("artifact staging temporary path already exists")
    temporary.mkdir(mode=0o700)
    try:
        for path, digest, executable, content in files:
            destination = temporary.joinpath(*PurePosixPath(path).parts)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o555 if executable else 0o444,
            )
            try:
                offset = 0
                while offset < len(content):
                    written = os.write(descriptor, content[offset:])
                    if written <= 0:
                        raise OSError("artifact staging write made no progress")
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.chmod(destination, 0o555 if executable else 0o444)
            if _sha256(destination.read_bytes()) != digest:
                raise ValueError("artifact changed while staging")
        manifest = {
            "version": "io.tobkiri.packvm-materialization.v1",
            "pack_id": pack_id,
            "artifact_digest": artifact_digest,
            "function_id": function_id,
            "implementation_digest": implementation_digest,
            "implementation_path": implementation_path,
            "materialization_digest": materialization_digest,
            "materialization_nonce": nonce,
            "files": [
                {
                    "path": path,
                    "digest": digest,
                    "executable": executable,
                    "size": len(content),
                }
                for path, digest, executable, content in files
            ],
        }
        manifest_path = temporary / ".tobkiri-materialization.json"
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(manifest_path, 0o444)
        for current, directories, _names in os.walk(temporary, topdown=False):
            for directory in directories:
                os.chmod(Path(current) / directory, 0o555)
            if Path(current) != temporary:
                os.chmod(current, 0o555)
        os.replace(temporary, target)
        os.chmod(target, 0o555)
    finally:
        if temporary.exists():
            _make_tree_writable(temporary)
            shutil.rmtree(temporary)
    identity = _verify_staged_artifact(target, artifact_digest, materialization_digest)
    return {
        "ok": True,
        "protocol": PROTOCOL,
        "artifact_digest": artifact_digest,
        "materialization_digest": materialization_digest,
        "guest_artifact_identity": identity,
    }


@contextmanager
def _artifact_storage_lock() -> Iterator[None]:
    """Serialize staging and quota accounting without following links."""

    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    root_metadata = ARTIFACT_ROOT.lstat()
    if ARTIFACT_ROOT.is_symlink() or not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("PackVM artifact storage root is unsafe")
    os.chmod(ARTIFACT_ROOT, 0o700)
    lock_path = ARTIFACT_ROOT / ".quota.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("PackVM artifact quota lock is unsafe")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _verify_invocation_artifact(request: dict[str, object]) -> str:
    artifact_digest = _digest(request.get("artifact_digest"), "artifact_digest")
    materialization_digest = _digest(
        request.get("materialization_digest"), "materialization_digest"
    )
    expected_identity = _digest(request.get("guest_artifact_identity"), "guest_artifact_identity")
    target = (
        ARTIFACT_ROOT
        / artifact_digest.removeprefix("sha256:")
        / materialization_digest.removeprefix("sha256:")
    )
    identity = _verify_staged_artifact(target, artifact_digest, materialization_digest)
    if identity != expected_identity:
        raise ValueError("guest artifact filesystem identity changed")
    return identity


def _make_tree_writable(root: Path) -> None:
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        os.chmod(current_path, 0o700)
        for directory in directories:
            path = current_path / directory
            if not path.is_symlink():
                os.chmod(path, 0o700)
        for filename in files:
            path = current_path / filename
            if not path.is_symlink():
                os.chmod(path, 0o600)


def _artifact_storage_bytes() -> int:
    """Measure retained root-owned artifacts without following filesystem links."""

    if not ARTIFACT_ROOT.exists():
        return 0
    root_metadata = ARTIFACT_ROOT.lstat()
    if ARTIFACT_ROOT.is_symlink() or not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("PackVM artifact storage root is unsafe")
    total = 0
    for current, directories, files in os.walk(ARTIFACT_ROOT, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            path = current_path / directory
            metadata = path.lstat()
            if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("PackVM artifact storage contains an unsafe directory")
        for filename in files:
            path = current_path / filename
            metadata = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise ValueError("PackVM artifact storage contains an unsafe file")
            total += metadata.st_size
            if total > MAX_ARTIFACT_STORAGE_BYTES:
                raise ValueError("PackVM artifact storage quota is already exceeded")
    return total


def _verify_staged_artifact(
    target: Path,
    artifact_digest: str,
    materialization_digest: str,
) -> str:
    metadata = target.lstat()
    if (
        target.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o555
    ):
        raise ValueError("staged artifact root is unsafe")
    manifest = _load_manifest(target)
    if (
        manifest.get("artifact_digest") != artifact_digest
        or manifest.get("materialization_digest") != materialization_digest
    ):
        raise ValueError("staged artifact identity mismatch")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("staged artifact manifest is invalid")
    expected_files = {_relative_path(item.get("path")) for item in files if isinstance(item, dict)}
    if len(expected_files) != len(files):
        raise ValueError("staged artifact manifest is invalid")
    expected_directories = {
        parent.as_posix()
        for path in expected_files
        for parent in PurePosixPath(path).parents
        if parent.as_posix() != "."
    }
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for current_path, directories, filenames in os.walk(target, followlinks=False):
        current = Path(current_path)
        for directory in directories:
            path = current / directory
            if path.is_symlink() or stat.S_IMODE(path.lstat().st_mode) != 0o555:
                raise ValueError("staged artifact directory is unsafe")
            actual_directories.add(path.relative_to(target).as_posix())
        for filename in filenames:
            path = current / filename
            relative = path.relative_to(target).as_posix()
            if relative != ".tobkiri-materialization.json":
                actual_files.add(relative)
    if actual_files != expected_files or actual_directories != expected_directories:
        raise ValueError("staged artifact file inventory changed")
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("staged artifact manifest is invalid")
        relative = _relative_path(item.get("path"))
        candidate = target.joinpath(*PurePosixPath(relative).parts)
        candidate_metadata = candidate.lstat()
        if candidate.is_symlink() or not stat.S_ISREG(candidate_metadata.st_mode):
            raise ValueError("staged artifact contains an unsafe file")
        expected_mode = 0o555 if item.get("executable") is True else 0o444
        if stat.S_IMODE(candidate_metadata.st_mode) != expected_mode:
            raise ValueError("staged artifact file is not read-only")
        content = candidate.read_bytes()
        if len(content) != item.get("size") or _sha256(content) != item.get("digest"):
            raise ValueError("staged artifact file digest changed")
    return _canonical_digest(
        {
            "artifact_digest": artifact_digest,
            "materialization_digest": materialization_digest,
            "device": int(metadata.st_dev),
            "inode": int(metadata.st_ino),
            "implementation_digest": manifest.get("implementation_digest"),
        }
    )


def _load_manifest(target: Path) -> dict[str, object]:
    path = target / ".tobkiri-materialization.json"
    metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o444
        or metadata.st_size > MAX_REQUEST_BYTES
    ):
        raise ValueError("staged artifact manifest is unsafe")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        content = os.read(descriptor, MAX_REQUEST_BYTES + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(content) > MAX_REQUEST_BYTES or (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("staged artifact manifest changed while reading")
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("staged artifact manifest is invalid")
    return value


def _relative_path(value: object) -> str:
    path = str(value or "")
    relative = PurePosixPath(path)
    if (
        not path
        or relative.is_absolute()
        or ".." in relative.parts
        or "." in relative.parts
        or "\\" in path
    ):
        raise ValueError("artifact path is unsafe")
    return path


def _identifier(value: object, label: str) -> str:
    normalized = str(value or "")
    if _IDENTIFIER.fullmatch(normalized) is None:
        raise ValueError(f"{label} is invalid")
    return normalized


def _digest(value: object, label: str) -> str:
    normalized = str(value or "")
    if _DIGEST.fullmatch(normalized) is None:
        raise ValueError(f"{label} is invalid")
    return normalized


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return _sha256(encoded)


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
