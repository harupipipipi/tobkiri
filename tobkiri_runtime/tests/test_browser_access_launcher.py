"""Fail-closed tests for Host-owned native browser access transport."""

import base64
import hashlib
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from core_runtime import browser_access_launcher as launcher
from core_runtime.host_contract import bind_host_contract
from core_runtime.panel_auth import PanelAuthBinding

BINDING = PanelAuthBinding(
    "defaults", "sha256:" + "a" * 64, "activation:abcdefgh", "sha256:" + "b" * 64, 1
)


def encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


@pytest.fixture
def transport(monkeypatch):
    key = Ed25519PrivateKey.generate()
    values = {
        "viewer_broker_url": "http://127.0.0.1:8770",
        "viewer_broker_token": "private-token",
        "viewer_broker_attestation_public_key": encode(
            key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        ),
        "viewer_broker_instance_nonce": "private-instance",
    }
    contract = {
        "schema_version": "tobkiri.host-contract.v1",
        "values": values,
        **{
            field: getattr(BINDING, field)
            for field in (
                "profile_id",
                "profile_revision",
                "activation_id",
                "plan_digest",
            )
        },
    }
    state = SimpleNamespace(
        mutator=lambda att: None,
        calls=[],
        payload={"ok": True, "opened": True, "request_id": "request_1"},
    )

    def open_request(request, timeout):
        state.calls.append(request)
        assert timeout == 10
        assert request.get_header("Authorization") == "Bearer private-token"
        assert json.loads(request.data) == {
            "request_id": "request_1",
            "runtime_port": 8765,
        }
        nonce = request.get_header("X-rumi-launcher-response-nonce")
        payload = json.dumps(state.payload).encode()
        digest = hashlib.sha256(payload).hexdigest()
        signed = (
            f"tobkiri-launcher-response-v1\nprivate-instance\n{nonce}\n"
            f"POST\n{launcher._PATH}\n200\n{digest}"
        ).encode()
        att = {
            "version": 1,
            "algorithm": "Ed25519",
            "instance_nonce": "private-instance",
            "request_nonce": nonce,
            "method": "POST",
            "path": launcher._PATH,
            "status": 200,
            "payload": encode(payload),
            "payload_sha256": digest,
            "signature": encode(key.sign(signed)),
        }
        state.mutator(att)
        # Unsigned outer claims are intentionally contradictory.
        raw = json.dumps({"ok": False, "_launcher_attestation": att}).encode()

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, size):
                assert size == 65537
                return raw

        return Response()

    def build(*handlers):
        assert handlers[0].proxies == {}
        assert isinstance(handlers[1], launcher._NoRedirect)
        return SimpleNamespace(open=open_request)

    monkeypatch.setattr(launcher.urllib.request, "build_opener", build)
    return contract, state


def test_verified_signed_payload_is_authoritative(transport):
    contract, state = transport
    with bind_host_contract(contract):
        assert launcher.open_browser_access_window(BINDING, "request_1", 8765) is None
    assert len(state.calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_nonce", "foreign"),
        ("instance_nonce", "foreign"),
        ("signature", encode(b"x" * 64)),
        ("payload_sha256", "0" * 64),
        ("method", "GET"),
        ("path", "/other"),
        ("status", 201),
        ("version", True),
        ("algorithm", "other"),
    ],
)
def test_attestation_mismatch_fails(transport, field, value):
    contract, state = transport
    state.mutator = lambda att: att.update({field: value})
    with bind_host_contract(contract), pytest.raises(RuntimeError):
        launcher.open_browser_access_window(BINDING, "request_1", 8765)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8770",
        "https://127.0.0.1:8770",
        "http://127.0.0.1:8770/path",
        "http://user@127.0.0.1:8770",
        "http://127.0.0.1:8770?secret",
        "http://127.0.0.1:0",
        "http://127.0.0.1:8770#x",
    ],
)
def test_invalid_url_never_sends(transport, url):
    contract, state = transport
    contract["values"]["viewer_broker_url"] = url
    with bind_host_contract(contract), pytest.raises(RuntimeError):
        launcher.open_browser_access_window(BINDING, "request_1", 8765)
    assert not state.calls


def test_foreign_identity_never_sends(transport):
    contract, state = transport
    contract["activation_id"] = "activation:foreign1"
    with bind_host_contract(contract), pytest.raises(RuntimeError):
        launcher.open_browser_access_window(BINDING, "request_1", 8765)
    assert not state.calls


@pytest.mark.parametrize("port", [True, 0, 65536, "8765"])
def test_invalid_runtime_port_never_sends(transport, port):
    contract, state = transport
    with bind_host_contract(contract), pytest.raises(RuntimeError):
        launcher.open_browser_access_window(BINDING, "request_1", port)
    assert not state.calls


def test_missing_contract_does_not_use_environment(monkeypatch, transport):
    _, state = transport
    monkeypatch.setenv("RUMI_VIEWER_BROKER_TOKEN", "private-token")
    monkeypatch.setenv("RUMI_VIEWER_HOST_BROKER_CONNECTION", "/secret/fallback.json")
    monkeypatch.setattr(
        launcher,
        "capture_host_contract",
        lambda **kwargs: (_ for _ in ()).throw(
            ValueError("private-token socket raw body")
        ),
    )
    with pytest.raises(RuntimeError) as error:
        launcher.open_browser_access_window(BINDING, "request_1", 8765)
    assert str(error.value) == launcher._ERROR
    assert error.value.__cause__ is None
    assert not state.calls


@pytest.mark.parametrize(
    "field",
    [
        "viewer_broker_url",
        "viewer_broker_token",
        "viewer_broker_attestation_public_key",
        "viewer_broker_instance_nonce",
    ],
)
def test_missing_pin_or_credential_never_sends(transport, field):
    contract, state = transport
    del contract["values"][field]
    with bind_host_contract(contract), pytest.raises(RuntimeError):
        launcher.open_browser_access_window(BINDING, "request_1", 8765)
    assert not state.calls


def test_signed_success_must_match_request(transport):
    contract, state = transport
    state.payload["request_id"] = "foreign"
    with bind_host_contract(contract), pytest.raises(RuntimeError):
        launcher.open_browser_access_window(BINDING, "request_1", 8765)


def test_redirect_handler_never_forwards_credentials():
    """Private localhost credentials must not follow HTTP redirects."""
    assert (
        launcher._NoRedirect().redirect_request(
            None, None, 302, "redirect", {}, "https://foreign.example"
        )
        is None
    )


@pytest.mark.parametrize("status,size", [(200, 65537), (302, 10), (500, 10)])
def test_oversized_or_unsuccessful_response_fails(transport, monkeypatch, status, size):
    """Bound response size and refuse all unsuccessful status codes."""
    contract, _ = transport

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return b"x" * size

    response = Response()
    response.status = status
    monkeypatch.setattr(
        launcher.urllib.request,
        "build_opener",
        lambda *args: SimpleNamespace(open=lambda *a, **kw: response),
    )
    with bind_host_contract(contract), pytest.raises(RuntimeError) as error:
        launcher.open_browser_access_window(BINDING, "request_1", 8765)
    assert str(error.value) == launcher._ERROR
