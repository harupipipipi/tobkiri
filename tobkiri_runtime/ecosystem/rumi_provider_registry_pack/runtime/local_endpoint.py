"""Canonical endpoint validation for credentialless local AI providers."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


def local_openai_endpoint(value: Any) -> str:
    """Return a canonical loopback OpenAI base URL or reject the endpoint."""
    if not isinstance(value, str) or not value or len(value) > 2_048:
        raise ValueError("local provider endpoint is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("local provider endpoint is invalid") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or not 1_024 <= port <= 65_535
        or parsed.path not in {"/v1", "/v1/"}
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in value)
    ):
        raise ValueError("local provider endpoint is invalid")
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    canonical = f"http://{host}:{port}/v1"
    if value not in {canonical, canonical + "/"}:
        raise ValueError("local provider endpoint is invalid")
    return canonical
