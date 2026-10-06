"""Host-owned, attested transport for native project directory selection."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.parse
import urllib.request
from typing import Any
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
    response: dict[str, Any], public_key: str, instance: str, nonce: str, status: int
) -> dict[str, Any]:
    att = response.get("_launcher_attestation")
    if not isinstance(att, dict) or (
        type(att.get("version")) is not int
        or att["version"] != 1
        or att.get("algorithm") != "Ed25519"
        or att.get("instance_nonce") != instance
        or att.get("request_nonce") != nonce
        or att.get("method") != "POST"
        or att.get("path") != _PATH
        or type(att.get("status")) is not int
        or att["status"] != status
    ):
        raise ValueError("invalid attestation")
    payload = _decode(att["payload"])
    digest = hashlib.sha256(payload).hexdigest()
    if not secrets.compare_digest(digest, att["payload_sha256"]):
        raise ValueError("invalid digest")
    signed = (
        "tobkiri-launcher-response-v1\n"
        f"{instance}\n{nonce}\nPOST\n{_PATH}\n{status}\n{digest}"
    ).encode("utf-8")
    Ed25519PublicKey.from_public_bytes(_decode(public_key)).verify(
        _decode(att["signature"]), signed
    )
    verified = json.loads(payload.decode("utf-8"))
    if not isinstance(verified, dict):
        raise ValueError("invalid payload")
    return verified


def pick_project_directory(
    binding: ExecutionProfileIdentity,
) -> Path | None:
    """Ask the identity-matched Launcher to pick an existing directory.

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
        nonce = secrets.token_urlsafe(32)
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
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )
        with opener.open(request, timeout=300) as response:
            status = response.status
            data = response.read(_LIMIT + 1)
        if status != 200 or len(data) > _LIMIT:
            raise ValueError("invalid response")
        decoded = json.loads(data.decode("utf-8"))
        verified = _verify(decoded, key, instance, nonce, status)
        if (
            verified.get("ok") is not True
            or verified.get("identity") != binding.as_mapping()
        ):
            raise ValueError("invalid selection identity")
        if verified.get("cancelled") is True and verified.get("path") is None:
            return None
        path = verified.get("path")
        if verified.get("cancelled") is not False or not isinstance(path, str):
            raise ValueError("invalid selection")
        root = Path(path)
        if (
            not root.is_absolute()
            or len(path) > 32768
            or any(ord(c) < 32 for c in path)
        ):
            raise ValueError("invalid directory")
        return root
    except Exception:
        raise RuntimeError(_ERROR) from None


class LauncherDirectoryPickerPort:
    """Fixed attested native dialog bound to one captured execution identity."""

    def __init__(self, identity: ExecutionProfileIdentity) -> None:
        self._identity = identity

    def pick_directory(self) -> Path | None:
        """Return only the result of the trusted Launcher OS picker."""
        return pick_project_directory(self._identity)
