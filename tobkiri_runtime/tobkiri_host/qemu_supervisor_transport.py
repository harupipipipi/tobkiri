"""Platform-neutral authenticated PackVM protocol over an owned QEMU channel.

Transport liveness is never attestation. The common supervisor verifies every
fresh guest Ed25519 signature before it exposes any result to the Host.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import secrets
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from tobkiri_protocol.canonical import canonical_digest, canonical_json

from .errors import BackendUnavailableError
from .macos_vz_supervisor import MacOSVZDomainAllocation
from .qemu_request_ledger import QemuRequestLedger

PROTOCOL = "io.tobkiri.macos-vz-supervisor.v1"
GUEST_REQUEST_PROTOCOL = "io.tobkiri.packvm-supervisor.v1"
_FLOW_DATA_ENCODING = "tobkiri.flow-data.ieee754.v1"
_MAX_INVOKE_PAYLOAD_BYTES = 1280 * 1024


@dataclass(frozen=True)
class QemuRuntime:
    """Actual fixed VM configuration; never claim the legacy VZ vsock device."""

    accelerator: str
    cpu_count: int = 1
    memory_bytes: int = 1024 * 1024 * 1024

    def to_dict(self) -> dict[str, str | int]:
        """Return the configuration used by the exact platform process adapter."""
        if (
            self.accelerator not in {"kvm", "whpx"}
            or self.cpu_count != 1
            or self.memory_bytes != 1024 * 1024 * 1024
        ):
            raise BackendUnavailableError("QEMU production runtime configuration is invalid")
        return {
            "cpu_count": self.cpu_count,
            "memory_bytes": self.memory_bytes,
            "accelerator": self.accelerator,
            "machine": "q35",
            "guest_transport": "virtio-serial",
            "guest_port": "io.tobkiri.packvm.agent",
        }


class QemuSupervisorTransport:
    """One Host-owned VM and an authenticated, challenge-multiplexed channel."""

    platform = "linux-amd64"

    def __init__(
        self, *, process: Any, allocation: MacOSVZDomainAllocation, assets: Any, channel_key: bytes
    ) -> None:
        if not isinstance(channel_key, bytes) or len(channel_key) != 32:
            raise ValueError("QEMU PackVM requires a private per-domain MAC key")
        self._process = process
        self._allocation = allocation
        self._assets = assets
        self._key = channel_key
        self._binding_digest: str | None = None
        self._launch_nonce: str | None = None
        self._binding: dict[str, Any] | None = None
        self._guest_identity: str | None = None
        self._nonces: set[str] = set()
        self._requests = QemuRequestLedger()
        self._lock = threading.RLock()
        self._started = False
        self._closed = False

    def enroll_launch_secret(
        self, *, domain_id: str, host_nonce: str, launch_binding_digest: str, secret: bytes
    ) -> None:
        """Bind the in-memory Host secret once; it never enters VM data."""
        with self._lock:
            if (
                self._binding_digest is not None
                or self._closed
                or domain_id != self._allocation.domain_id
                or not hmac.compare_digest(secret, self._key)
            ):
                raise BackendUnavailableError("QEMU PackVM launch enrollment mismatch")
            self._binding_digest = launch_binding_digest
            self._launch_nonce = host_nonce

    def exchange(self, envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        """Wrap real guest evidence; never synthesize a guest signature."""
        operation = envelope.get("operation")
        common = {
            "kind",
            "protocol",
            "version",
            "operation",
            "host_nonce",
            "domain_id",
            "launch_binding_digest",
        }
        extras = {
            "launch": {"launch_binding", "guest_challenge"},
            "invoke": {"request", "guest_challenge"},
            "bridge_result": {"host_bridge_result", "guest_challenge"},
            "cancel": {"request_id", "request_digest", "guest_challenge"},
            "terminate": {"lease_id", "reservation_id"},
        }
        with self._lock:
            nonce = envelope.get("host_nonce")
            if (
                operation not in extras
                or set(envelope) != common | extras[operation]
                or envelope.get("kind") != "tobkiri.macos-vz.supervisor.request.v1"
                or envelope.get("protocol") != PROTOCOL
                or envelope.get("version") != 1
                or envelope.get("domain_id") != self._allocation.domain_id
                or envelope.get("launch_binding_digest") != self._binding_digest
                or not isinstance(nonce, str)
                or len(nonce) != 64
                or any(c not in "0123456789abcdef" for c in nonce)
                or nonce in self._nonces
                or len(self._nonces) >= 8192
                or self._closed
            ):
                raise BackendUnavailableError(
                    "QEMU PackVM supervisor request is invalid or replayed"
                )
            self._nonces.add(nonce)
        if operation == "launch":
            payload = self._launch(envelope)
        elif operation == "terminate":
            if (
                envelope["lease_id"] != self._allocation.lease_id
                or envelope["reservation_id"] != self._allocation.reservation_id
            ):
                raise BackendUnavailableError("QEMU PackVM termination identity mismatch")
            self._process.stop()
            if self._process.alive():
                raise BackendUnavailableError("QEMU PackVM process did not terminate")
            payload = {
                "state": "terminated",
                "domain_id": self._allocation.domain_id,
                "lease_id": self._allocation.lease_id,
                "reservation_id": self._allocation.reservation_id,
                "cleanup": {"vm": "released", "cow_disk": "detached", "efi_store": "detached"},
            }
        else:
            payload = self._guest_exchange(envelope)
        core = {
            "kind": "tobkiri.macos-vz.supervisor.response.v1",
            "protocol": PROTOCOL,
            "version": 1,
            "operation": operation,
            "host_nonce": nonce,
            "domain_id": self._allocation.domain_id,
            "launch_binding_digest": self._binding_digest,
            "payload": payload,
        }
        return {
            **core,
            "agent_mac": hmac.new(self._key, canonical_json(core), hashlib.sha256).hexdigest(),
        }

    def _launch(self, envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        binding = envelope["launch_binding"]
        with self._lock:
            if (
                self._started
                or envelope["host_nonce"] != self._launch_nonce
                or not isinstance(binding, dict)
                or canonical_digest(binding) != self._binding_digest
                or binding.get("platform") != self.platform
                or binding.get("domain_allocation") != self._allocation.to_dict()
                or binding.get("domain_id") != self._allocation.domain_id
            ):
                raise BackendUnavailableError("QEMU PackVM launch binding mismatch")
            self._assets.verify()
            self._binding = dict(binding)
            self._started = True
        try:
            deadline = time.monotonic() + 180
            self._process.start()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("QEMU PackVM startup deadline expired")
            self._process.wait_for_guest_ready(timeout=remaining)
            request = self._base_guest_request(
                "attest", f"attest-{self._allocation.domain_id}", envelope["guest_challenge"]
            )
            request["attestation_nonce"] = envelope["host_nonce"]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("QEMU PackVM startup deadline expired")
            reply = self._process.exchange(request, timeout=remaining)
            self._verify_guest_reply(request, reply)
            # The outer driver repeats the full evidence checks independently.
            if isinstance(reply.get("data"), dict):
                self._guest_identity = reply["data"].get("guest_artifact_identity")
            return reply
        except Exception as error:
            try:
                capture = getattr(self._process, "_capture_failure_diagnostics", None)
                if callable(capture):
                    capture(error)
            except Exception:
                # Diagnostic collection cannot suppress owned-child cleanup.
                pass
            self._process.stop()
            raise

    def _base_guest_request(
        self, operation: str, request_id: str, challenge: str
    ) -> dict[str, Any]:
        if self._binding is None:
            raise BackendUnavailableError("QEMU PackVM has not launched")
        return {
            "protocol": GUEST_REQUEST_PROTOCOL,
            "operation": operation,
            "request_id": request_id,
            "domain_id": self._allocation.domain_id,
            "binding_digests": self._binding["binding_digests"],
            "guest_challenge": challenge,
        }

    def _guest_exchange(self, envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        operation = envelope["operation"]
        if self._binding is None or not self._started or self._guest_identity is None:
            raise BackendUnavailableError("QEMU PackVM guest is not attested")
        ticket = None
        if operation == "invoke":
            raw = envelope["request"]
            fields = {
                "request_id",
                "request_digest",
                "contract_id",
                "contract_version",
                "operation_id",
                "deadline_monotonic",
            }
            if not isinstance(raw, dict) or any(
                not isinstance(raw.get(field), str)
                for field in ("contract_id", "contract_version", "operation_id")
            ):
                raise BackendUnavailableError("QEMU PackVM invoke shape is invalid")
            if set(raw) == fields | {"payload"} and isinstance(raw["payload"], dict):
                payload_value = raw["payload"]
            elif (
                set(raw) == fields | {"payload_encoding", "payload_tokens"}
                and raw["payload_encoding"] == _FLOW_DATA_ENCODING
                and isinstance(raw["payload_tokens"], list)
                and raw["contract_id"] not in {
                    "conversation.saved-turn.v1",
                    "tobkiri.service.mcp.tool.call.v1",
                }
            ):
                # Route by public contract; operation IDs remain opaque. The
                # guest owns legacy ABI alias validation before artifact access.
                # Preserve the encoded fields exactly. Decoding belongs in the
                # guest adapter, after protected-channel and launch-binding
                # validation.
                payload_value = {
                    "payload_encoding": raw["payload_encoding"],
                    "payload_tokens": raw["payload_tokens"],
                }
            else:
                raise BackendUnavailableError("QEMU PackVM invoke shape is invalid")
            try:
                payload_bytes = canonical_json(payload_value)
            except ValueError as exc:
                raise BackendUnavailableError("QEMU PackVM invoke payload is invalid") from exc
            if len(payload_bytes) > _MAX_INVOKE_PAYLOAD_BYTES:
                raise BackendUnavailableError("QEMU PackVM invoke payload exceeds limit")
            request_id = raw["request_id"]
            now = time.monotonic()
            value = raw["deadline_monotonic"]
            if value is None:
                deadline = now + 60
            elif isinstance(value, str) and len(value) <= 64:
                try:
                    deadline = float(value)
                except ValueError as exc:
                    raise BackendUnavailableError("QEMU PackVM deadline is invalid") from exc
            else:
                raise BackendUnavailableError("QEMU PackVM deadline is invalid")
            if not math.isfinite(deadline) or deadline <= now:
                raise BackendUnavailableError("QEMU PackVM invocation deadline expired")
            deadline = min(deadline + 5, now + 605)
            # A default relative budget must not grow through floating-point
            # cancellation when reconstructed from an absolute monotonic time.
            budget_limit = 65 if value is None else 600
            saved = (raw["contract_id"], raw["contract_version"], raw["operation_id"]) == (
                "conversation.saved-turn.v1",
                "1.0.0",
                "saved_complete",
            )
            ticket = self._requests.begin(
                request_id, raw["request_digest"], deadline, maximum_bridges=4 if saved else 16
            )
            guest = self._base_guest_request("invoke", request_id, envelope["guest_challenge"])
            guest["payload"] = {
                **raw,
                "operation": "invoke",
                "target_domain": self._allocation.domain_id,
                "artifact_digest": self._binding["artifact"]["artifact_digest"],
                "materialization_digest": self._binding["artifact"]["materialization_digest"],
                "guest_artifact_identity": self._guest_identity,
                "cancel_token": secrets.token_hex(32),
                "budget_seconds": format(min(budget_limit, max(0, deadline - now)), ".17g"),
            }
        elif operation == "bridge_result":
            raw = envelope.get("host_bridge_result")
            if not isinstance(raw, dict) or not isinstance(raw.get("request_id"), str):
                raise BackendUnavailableError("QEMU bridge result identity is invalid")
            ticket = self._requests.resume(raw["request_id"])
            deadline = ticket.deadline
            guest = self._base_guest_request(
                operation, ticket.request_id, envelope["guest_challenge"]
            )
            guest["host_bridge_result"] = raw
        else:
            request_id = envelope["request_id"]
            self._requests.cancel(request_id, envelope["request_digest"])
            deadline = time.monotonic() + 10
            guest = self._base_guest_request(operation, request_id, envelope["guest_challenge"])
        try:
            reply = self._process.exchange(guest, timeout=max(0.001, deadline - time.monotonic()))
            self._verify_guest_reply(guest, reply)
            if ticket is not None:
                data = reply.get("data")
                self._requests.settle(
                    ticket, pending=isinstance(data, dict) and data.get("state") == "pending"
                )
            return reply
        except Exception:
            if ticket is not None:
                self._requests.abandon(ticket)
            raise

    def _verify_guest_reply(self, request: Mapping[str, Any], reply: object) -> None:
        """Verify real root-agent evidence before retaining any helper-local state."""
        if not isinstance(reply, dict):
            raise BackendUnavailableError("QEMU guest reply is invalid")
        expected = {
            "kind",
            "protocol",
            "version",
            "operation",
            "request_id",
            "domain_id",
            "binding_digests",
            "guest_challenge",
            "success",
            "agent_signature",
        }
        if request["operation"] == "attest":
            expected.add("attestation_nonce")
        expected.add("data" if reply.get("success") is True else "error")
        if (
            set(reply) != expected
            or type(reply.get("success")) is not bool
            or reply.get("kind") != "tobkiri.packvm.guest.response.v1"
            or reply.get("protocol") != PROTOCOL
            or reply.get("version") != 1
            or any(
                reply.get(key) != request.get(key)
                for key in (
                    "operation",
                    "request_id",
                    "domain_id",
                    "binding_digests",
                    "guest_challenge",
                )
            )
            or (
                request["operation"] == "attest"
                and reply.get("attestation_nonce") != request["attestation_nonce"]
            )
        ):
            raise BackendUnavailableError("QEMU signed guest reply binding mismatch")
        try:
            encoded = reply["agent_signature"]
            if not isinstance(encoded, str) or len(encoded) > 128:
                raise ValueError("invalid signature size")
            signature = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            )
            core = {key: value for key, value in reply.items() if key != "agent_signature"}
            Ed25519PublicKey.from_public_bytes(self._allocation.guest_public_key).verify(
                signature, canonical_json(core)
            )
        except (ValueError, TypeError, InvalidSignature) as exc:
            raise BackendUnavailableError("QEMU guest signature verification failed") from exc

    def alive(self) -> bool:
        """Return actual child liveness, never a cached readiness claim."""
        return not self._closed and self._process.alive()

    def close(self) -> None:
        """Reap this exact process before allowing its allocator to clean up."""
        self._process.stop()
        if self._process.alive():
            raise BackendUnavailableError("QEMU PackVM still owns live resources")
        with self._lock:
            self._closed = True
            self._key = b""
