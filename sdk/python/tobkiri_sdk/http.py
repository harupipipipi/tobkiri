"""Requests-like streaming shim over core network imports, with no sockets."""

import json as _json
from dataclasses import dataclass
from typing import Any, Iterator, Mapping
from ._binding import GuestError, invoke
from .secrets import SecretRef


@dataclass(frozen=True)
class SecretHeader:
    """Core injects prefix plus an opaque secret after the gate authorizes it."""

    secret: SecretRef
    prefix: str = ""


class Response:
    """Pull-based response owned by a generated WIT response resource."""

    def __init__(self, resource: Any) -> None:
        self._resource = resource
        self.status_code = resource.status()
        self.headers = dict(resource.headers())

    def iter_content(self, chunk_size: int = 8192) -> Iterator[bytes]:
        """Read bounded chunks; dropping or closing the resource cancels I/O."""
        if not 0 < chunk_size <= 1024 * 1024:
            raise ValueError("chunk_size must be between 1 and 1048576")
        while self._resource is not None:
            chunk = self._resource.read(chunk_size)
            if chunk is None:
                break
            yield bytes(chunk)

    def raise_for_status(self) -> None:
        """Raise a status-only error; never expose provider response secrets."""
        if not 200 <= self.status_code < 300:
            raise GuestError(f"HTTP status {self.status_code}")

    def close(self) -> None:
        """Drop the response resource, closing any outstanding network request."""
        self._resource = None

    def json(self) -> Any:
        """Decode a bounded JSON response (streaming users use iter_content)."""
        chunks = bytearray()
        for chunk in self.iter_content():
            chunks.extend(chunk)
            if len(chunks) > 4 * 1024 * 1024:
                raise GuestError("JSON response exceeds 4 MiB")
        return _json.loads(chunks)


def request(method: str, url: str, *, headers: Mapping[str, str | SecretHeader] | None = None,
            data: bytes | None = None, json: Any = None, timeout: float | None = None,
            stream: bool = False) -> Response:
    """Send one gated request to the caller's declared exact origin."""
    if data is not None and json is not None:
        raise ValueError("data and json are mutually exclusive")
    public_headers = []
    secret_headers = []
    resources = []
    for name, value in (headers or {}).items():
        if isinstance(value, SecretHeader):
            secret_headers.append({"name": name, "prefix": value.prefix, "index": len(resources)})
            resources.append(value.secret.resource)
        else:
            public_headers.append((name, value))
    body = data if json is None else _json.dumps(json, ensure_ascii=False, allow_nan=False).encode()
    if json is not None:
        public_headers.append(("content-type", "application/json"))
    req = {"method": method.upper(), "url": url, "headers": public_headers,
           "secret_headers": secret_headers, "body": body,
           "timeout_ms": None if timeout is None else int(timeout * 1000)}
    return Response(invoke("network", "fetch", req=req, secrets=resources))


def get(url: str, **kwargs: Any) -> Response:
    """Issue a gated GET request."""
    return request("GET", url, **kwargs)


def post(url: str, **kwargs: Any) -> Response:
    """Issue a gated POST request."""
    return request("POST", url, **kwargs)
