"""Guardrails for the distinct Host-local text inference capability."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from threading import Event

import pytest

from core_runtime.local_model_transport import (
    LocalModelBinding,
    LocalModelTransport,
    _chat_body,
)


def binding():
    return LocalModelBinding(
        "defaults", "provider.local", "http://127.0.0.1:8080/v1", ("lfm-local",), 1
    )


def body():
    return {"model": "lfm-local", "messages": [{"role": "user", "content": "17+25"}]}


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:8080/v1",
        "http://127.0.0.2:8080/v1",
        "http://127.0.0.1:8080/v1/",
        "http://127.0.0.1:8080/v1?x=y",
        "http://127.0.0.1:8080/v1#fragment",
        "http://127.0.0.1:0/v1",
        "http://127.0.0.1:65536/v1",
        "http://127.0.0.1:08080/v1",
        "http://user@127.0.0.1:8080/v1",
        "http://[::1]:8080/v1",
        "https://127.0.0.1:8080/v1",
        "http://10.0.0.1:8080/v1",
        "http://169.254.169.254:8080/v1",
        "http://example.com:8080/v1",
    ],
)
def test_noncanonical_or_nonloopback_endpoint_denied(endpoint):
    with pytest.raises(ValueError):
        replace(binding(), endpoint=endpoint)


@pytest.mark.parametrize(
    "change",
    [
        {"model": "unapproved"},
        {"stream": True},
        {"stream": 1},
        {
            "messages": [
                {"role": "user", "content": [{"type": "image_url", "image_url": "http://host"}]}
            ]
        },
        {"tools": []},
        {"max_tokens": 0},
        {"max_tokens": True},
        {"max_tokens": 4097},
        {"temperature": float("nan")},
        {"messages": []},
        {"messages": [{"role": "tool", "content": "x"}]},
    ],
)
def test_request_is_text_only_bounded_and_model_allowlisted(change):
    with pytest.raises((ValueError, PermissionError)):
        _chat_body({**body(), **change}, binding())


def test_safe_defaults_and_request_size():
    result = json.loads(_chat_body(body(), binding()))
    assert result["stream"] is False
    assert result["max_tokens"] == 512
    with pytest.raises(ValueError, match="exceeds"):
        _chat_body(
            {**body(), "messages": [{"role": "user", "content": "x" * (1024 * 1024)}]}, binding()
        )


def transport(resolve=lambda _: binding(), guard=lambda: None):
    return LocalModelTransport(
        profile_id="defaults",
        resolve_binding=resolve,
        assert_current=guard,
        deadline_monotonic=time.monotonic() + 10,
        cancellation=Event(),
    )


def test_profile_mismatch_and_replay_fail_before_connect():
    instance = transport(resolve=lambda _: replace(binding(), profile_id="other"))
    with pytest.raises(PermissionError, match="does not match"):
        instance.post_chat(
            provider_instance_id="provider.local", body=body(), deadline=time.time() + 10
        )
    with pytest.raises(PermissionError, match="consumed"):
        instance.post_chat(
            provider_instance_id="provider.local", body=body(), deadline=time.time() + 10
        )


def test_expired_deadline_fails_before_connect():
    with pytest.raises(TimeoutError):
        transport().post_chat(
            provider_instance_id="provider.local", body=body(), deadline=time.time() - 1
        )


def test_revoked_invocation_fails_before_resolving_or_connecting():
    def guard():
        raise PermissionError("revoked")

    with pytest.raises(PermissionError, match="revoked"):
        transport(guard=guard).post_chat(
            provider_instance_id="provider.local", body=body(), deadline=time.time() + 10
        )
