"""Host-owned admission requests for an unauthenticated local browser."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable
from urllib.parse import quote, urlsplit

if TYPE_CHECKING:
    from .panel_auth import PanelAuthBinding, PanelAuthManager

logger = logging.getLogger(__name__)


class BrowserAccessError(ValueError):
    """A bounded browser admission operation was denied."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class _Request:
    request_id: str
    proof_hash: str
    binding: PanelAuthBinding
    origin: str
    target: str
    expires_at: float
    state: str = "pending"


class BrowserAccessManager:
    """Keep admission separate from tool approval and existing presenter grants."""

    def __init__(
        self,
        panel_auth: PanelAuthManager,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: int = 120,
        max_requests: int = 4,
        rate_limit: int = 3,
        rate_window_seconds: int = 60,
    ) -> None:
        self._panel_auth = panel_auth
        self._clock = clock
        self._ttl = max(1, ttl_seconds)
        self._max_requests = max(1, max_requests)
        self._rate_limit = max(1, rate_limit)
        self._rate_window = max(1, rate_window_seconds)
        self._lock = threading.Lock()
        self._requests: dict[str, _Request] = {}
        self._created: list[float] = []

    @staticmethod
    def _hash(proof: str) -> str:
        return hashlib.sha256(proof.encode("utf-8")).hexdigest()

    def _cleanup(self, now: float) -> None:
        self._requests = {
            key: value
            for key, value in self._requests.items()
            if value.expires_at > now
        }
        self._created = [
            stamp for stamp in self._created if stamp > now - self._rate_window
        ]

    @staticmethod
    def _validate_target(binding: PanelAuthBinding, origin: str, target: str) -> None:
        if not isinstance(origin, str) or not isinstance(target, str):
            raise BrowserAccessError("invalid_request")
        try:
            parsed_origin = urlsplit(origin)
            parsed_target = urlsplit(target)
            port = parsed_origin.port
        except ValueError as error:
            raise BrowserAccessError("invalid_request") from error
        root = "/p/" + quote(binding.profile_id, safe="-._~")
        if (
            parsed_origin.scheme != "http"
            or parsed_origin.hostname not in {"localhost", "127.0.0.1"}
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path
            or parsed_origin.query
            or parsed_origin.fragment
            or port is None
            or not 0 < port < 65536
            or parsed_target.scheme
            or parsed_target.netloc
            or parsed_target.query
            or parsed_target.fragment
            or not (target == root or target.startswith(root + "/"))
            or "\\" in target
            or "%" in parsed_target.path[len(root) :]
            or any(part in {".", ".."} for part in target.split("/"))
            or len(target) > 2048
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in origin + target
            )
        ):
            raise BrowserAccessError("invalid_request")

    def _view(self, request: _Request, now: float) -> dict[str, object]:
        return {
            "request_id": request.request_id,
            "status": request.state,
            "expires_in": max(0, int(request.expires_at - now)),
            "target": request.target,
        }

    def _find(
        self,
        request_id: str,
        binding: PanelAuthBinding,
        *,
        proof: str | None = None,
        origin: str | None = None,
    ) -> _Request:
        if not isinstance(request_id, str) or len(request_id) > 160:
            raise BrowserAccessError("invalid_request")
        request = self._requests.get(request_id)
        if (
            request is None
            or request.binding != binding
            or (origin is not None and request.origin != origin)
            or (
                proof is not None
                and (
                    not isinstance(proof, str)
                    or len(proof) > 512
                    or not hmac.compare_digest(request.proof_hash, self._hash(proof))
                )
            )
        ):
            raise BrowserAccessError("invalid_request")
        return request

    def create(
        self,
        binding: PanelAuthBinding,
        origin: str,
        target: str,
        existing_proof: str = "",
    ) -> dict[str, object]:
        """Create one request or recover the caller's identical live request."""
        self._validate_target(binding, origin, target)
        now = self._clock()
        with self._lock:
            self._cleanup(now)
            if isinstance(existing_proof, str) and 0 < len(existing_proof) <= 512:
                proof_hash = self._hash(existing_proof)
                for request in list(self._requests.values()):
                    if (
                        request.binding == binding
                        and request.origin == origin
                        and request.target == target
                        and hmac.compare_digest(request.proof_hash, proof_hash)
                    ):
                        if request.state == "denied":
                            del self._requests[request.request_id]
                            break
                        return {
                            **self._view(request, now),
                            "proof": existing_proof,
                            "created": False,
                        }
            if len(self._requests) >= self._max_requests:
                raise BrowserAccessError("queue_full")
            if len(self._created) >= self._rate_limit:
                raise BrowserAccessError("rate_limited")
            proof = secrets.token_urlsafe(48)
            request_id = str(uuid.uuid4())
            request = _Request(
                request_id, self._hash(proof), binding, origin, target, now + self._ttl
            )
            self._requests[request_id] = request
            self._created.append(now)
            logger.info("Browser admission requested request_id=%s", request_id)
            return {**self._view(request, now), "proof": proof, "created": True}

    def status(
        self,
        request_id: str,
        proof: str,
        binding: PanelAuthBinding,
        origin: str,
    ) -> dict[str, object]:
        """Read only the request held by this browser claimant."""
        if not isinstance(proof, str) or not proof or not isinstance(origin, str):
            raise BrowserAccessError("invalid_request")
        now = self._clock()
        with self._lock:
            self._cleanup(now)
            return self._view(
                self._find(request_id, binding, proof=proof, origin=origin), now
            )

    def context(self, request_id: str, binding: PanelAuthBinding) -> dict[str, object]:
        """Return display context for a separately authenticated native caller."""
        now = self._clock()
        with self._lock:
            self._cleanup(now)
            request = self._find(request_id, binding)
            return {
                **self._view(request, now),
                "origin": request.origin,
                "profile_id": request.binding.profile_id,
            }

    def pending(self, binding: PanelAuthBinding) -> list[dict[str, object]]:
        """List sanitized pending contexts for an authenticated Launcher."""
        now = self._clock()
        with self._lock:
            self._cleanup(now)
            return [
                {
                    **self._view(request, now),
                    "origin": request.origin,
                    "profile_id": request.binding.profile_id,
                }
                for request in self._requests.values()
                if request.binding == binding and request.state == "pending"
            ]

    def decide(
        self,
        request_id: str,
        binding: PanelAuthBinding,
        decision: str,
    ) -> dict[str, object]:
        """Record a native decision; caller authentication belongs to transport."""
        if decision not in {"approved", "denied"}:
            raise BrowserAccessError("invalid_decision")
        now = self._clock()
        with self._lock:
            self._cleanup(now)
            request = self._find(request_id, binding)
            if request.state != "pending":
                raise BrowserAccessError("already_decided")
            request.state = decision
            logger.info("Browser admission %s request_id=%s", decision, request_id)
            return self._view(request, now)

    def claim(
        self,
        request_id: str,
        proof: str,
        binding: PanelAuthBinding,
        origin: str,
    ) -> dict[str, object]:
        """Atomically consume approval and mint one fresh ordinary session."""
        if not isinstance(proof, str) or not proof or not isinstance(origin, str):
            raise BrowserAccessError("invalid_request")
        now = self._clock()
        with self._lock:
            self._cleanup(now)
            request = self._find(request_id, binding, proof=proof, origin=origin)
            if request.state != "approved":
                raise BrowserAccessError("not_approved")
            # Holding the admission lock across exchange guarantees a single mint.
            code = self._panel_auth.issue_login_code(binding)
            exchange = self._panel_auth.exchange_code(str(code["code"]), binding)
            if exchange is None:
                raise BrowserAccessError("invalid_request")
            del self._requests[request_id]
            logger.info("Browser admission claimed request_id=%s", request_id)
            return {**exchange, "target": request.target}
