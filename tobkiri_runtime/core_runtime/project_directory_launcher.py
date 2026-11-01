"""Host-owned, attested transport for native project directory selection."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.parse
import urllib.request
from typing import Any
from threading import Lock
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .host_contract import capture_host_contract
from .host_contract import ExecutionProfileIdentity

_PATH = "/api/host/project-directory/pick"
_LIMIT = 64 * 1024
_ERROR = "Tobkiri Launcher directory selection could not be verified."


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _decode(value: str) -> bytes:
    raw = value.encode("ascii")
    return base64.b64decode(raw + b"=" * (-len(raw) % 4), altchars=b"-_", validate=True)


def _verify(
    response: dict[str, Any],
    public_key: str,
    instance: str,
    nonce: str,
    status: int,
    *,
    path: str = _PATH,
) -> dict[str, Any]:
    att = response.get("_launcher_attestation")
    if not isinstance(att, dict) or (
        type(att.get("version")) is not int
        or att["version"] != 1
        or att.get("algorithm") != "Ed25519"
        or att.get("instance_nonce") != instance
        or att.get("request_nonce") != nonce
        or att.get("method") != "POST"
        or att.get("path") != path
        or type(att.get("status")) is not int
        or att["status"] != status
    ):
        raise ValueError("invalid attestation")
    payload = _decode(att["payload"])
    digest = hashlib.sha256(payload).hexdigest()
    if not secrets.compare_digest(digest, att["payload_sha256"]):
        raise ValueError("invalid digest")
    signed = (
        f"tobkiri-launcher-response-v1\n{instance}\n{nonce}\nPOST\n{path}\n{status}\n{digest}"
    ).encode("utf-8")
    Ed25519PublicKey.from_public_bytes(_decode(public_key)).verify(
        _decode(att["signature"]), signed
    )
    verified = json.loads(payload.decode("utf-8"))
    if not isinstance(verified, dict):
        raise ValueError("invalid payload")
    return verified


def pick_project_directories(
    binding: ExecutionProfileIdentity,
    *,
    _request_nonce: str | None = None,
) -> list[Path] | None:
    """Ask the identity-matched Launcher to pick existing directories.

    Credentials come exclusively from the captured Host contract. Responses
    must carry an Ed25519 attestation bound to this exact request and instance.
    All failures expose only a fixed message, including transport exceptions.
    """
    try:
        values = capture_host_contract(expected_identity=binding)["values"]
        url, token, key, instance = (
            values.get(name)
            for name in (
                "viewer_broker_url",
                "viewer_broker_token",
                "viewer_broker_attestation_public_key",
                "viewer_broker_instance_nonce",
            )
        )
        if any(
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 4096
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            for value in (url, token, key, instance)
        ):
            raise ValueError("invalid contract")
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or port is None
            or not 1 <= port <= 65535
            or url not in {f"http://127.0.0.1:{port}", f"http://127.0.0.1:{port}/"}
        ):
            raise ValueError("invalid broker URL")
        # Validate the pin before sending private material to the broker.
        Ed25519PublicKey.from_public_bytes(_decode(key))
        nonce = _request_nonce or secrets.token_urlsafe(32)
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{_PATH}",
            data=json.dumps(binding.as_mapping()).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "X-Rumi-Viewer-Broker-Token": token,
                "X-Rumi-Launcher-Response-Nonce": nonce,
            },
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=300) as response:
            status = response.status
            data = response.read(_LIMIT + 1)
        if status != 200 or len(data) > _LIMIT:
            raise ValueError("invalid response")
        decoded = json.loads(data.decode("utf-8"))
        verified = _verify(decoded, key, instance, nonce, status)
        if verified.get("ok") is not True or verified.get("identity") != binding.as_mapping():
            raise ValueError("invalid selection identity")
        if verified.get("cancelled") is True:
            if verified.get("paths") is not None or verified.get("path") is not None:
                raise ValueError("invalid cancellation")
            return None
        paths = verified.get("paths") if "paths" in verified else [verified.get("path")]
        if (
            verified.get("cancelled") is not False
            or not isinstance(paths, list)
            or not 1 <= len(paths) <= 32
        ):
            raise ValueError("invalid selection")
        roots = []
        for path in paths:
            if not isinstance(path, str):
                raise ValueError("invalid directory")
            root = Path(path)
            if (
                not root.is_absolute()
                or len(path.encode("utf-8")) > 32768
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in path)
            ):
                raise ValueError("invalid directory")
            roots.append(root)
        return roots
    except Exception:
        raise RuntimeError(_ERROR) from None


def pick_project_directory(binding: ExecutionProfileIdentity) -> Path | None:
    """Return the first native selection for legacy single-directory callers."""
    roots = pick_project_directories(binding)
    return roots[0] if roots else None


class LauncherDirectoryPickerPort:
    """Fixed attested native dialog bound to one captured execution identity."""

    def __init__(self, identity: ExecutionProfileIdentity) -> None:
        self._identity = identity
        self._lock = Lock()
        self._pending_nonce: str | None = None
        self._closed = False

    def pick_directories(self) -> list[Path] | None:
        """Return all selections from the trusted Launcher OS picker."""
        nonce = secrets.token_urlsafe(32)
        with self._lock:
            if self._closed:
                return None
            self._pending_nonce = nonce
        try:
            roots = pick_project_directories(self._identity, _request_nonce=nonce)
            with self._lock:
                return None if self._closed else roots
        finally:
            with self._lock:
                if self._pending_nonce == nonce:
                    self._pending_nonce = None

    def pick_directory(self) -> Path | None:
        """Return only the result of the trusted Launcher OS picker."""
        roots = self.pick_directories()
        return roots[0] if roots else None

    def cancel_pending(self) -> None:
        """Retire only this adapter's outstanding native dialog, never another UI."""
        with self._lock:
            self._closed = True
            nonce = self._pending_nonce
        if nonce is not None:
            _cancel_project_directory_pick(self._identity, nonce)


def _cancel_project_directory_pick(binding: ExecutionProfileIdentity, pick_nonce: str) -> None:
    """Send a bounded authenticated cancellation for a retired captured picker."""
    path = "/api/host/project-directory/cancel"
    try:
        # A retired execution's credentials may have rotated. The new Host
        # connection can only cancel a matching old dialog; it cannot select.
        values = capture_host_contract()["values"]
        url = values["viewer_broker_url"]
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or not 1 <= parsed.port <= 65535
            or url not in {f"http://127.0.0.1:{parsed.port}", f"http://127.0.0.1:{parsed.port}/"}
        ):
            return
        Ed25519PublicKey.from_public_bytes(_decode(values["viewer_broker_attestation_public_key"]))
        nonce = secrets.token_urlsafe(32)
        token = values["viewer_broker_token"]
        request = urllib.request.Request(
            f"http://127.0.0.1:{parsed.port}{path}",
            data=json.dumps({**binding.as_mapping(), "request_nonce": pick_nonce}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "X-Rumi-Viewer-Broker-Token": token,
                "X-Rumi-Launcher-Response-Nonce": nonce,
            },
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=2) as response:
            if response.status != 200:
                return
            raw = response.read(_LIMIT + 1)
        if len(raw) > _LIMIT:
            return
        _verify(
            json.loads(raw),
            values["viewer_broker_attestation_public_key"],
            values["viewer_broker_instance_nonce"],
            nonce,
            200,
            path=path,
        )
    except Exception:
        # Selection is already retired. Failure cannot authorize a late result;
        # the owned native deadline still closes that exact panel.
        return
