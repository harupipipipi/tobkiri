"""Fail-closed tests for Host-owned native project directory selection transport."""

import base64
import hashlib
import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from core_runtime import project_directory_launcher as launcher
from core_runtime.host_contract import bind_host_contract
from core_runtime.host_contract import ExecutionProfileIdentity

BINDING = ExecutionProfileIdentity(
    "defaults", "sha256:" + "a" * 64, "activation:abcdefgh", "sha256:" + "b" * 64
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
        payload={
            "ok": True,
            "identity": BINDING.as_mapping(),
            "cancelled": False,
            "path": "/native/project",
        },
    )

    def open_request(request, timeout):
        state.calls.append(request)
        assert timeout == 300
        assert request.get_header("Authorization") == "Bearer private-token"
        assert json.loads(request.data) == BINDING.as_mapping()
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
        assert str(launcher.pick_project_directory(BINDING)) == "/native/project"
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
        launcher.pick_project_directory(BINDING)


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
        launcher.pick_project_directory(BINDING)
    assert not state.calls


def test_foreign_identity_never_sends(transport):
    contract, state = transport
    contract["activation_id"] = "activation:foreign1"
    with bind_host_contract(contract), pytest.raises(RuntimeError):
        launcher.pick_project_directory(BINDING)
    assert not state.calls


@pytest.mark.parametrize(
    "change",
    [
        {"identity": {"profile_id": "foreign"}},
        {"path": "relative"},
        {"path": "/native/" + chr(0)},
        {"cancelled": True},
        {"path": None},
    ],
)
def test_signed_malformed_selection_is_rejected(transport, change):
    contract, state = transport
    state.payload.update(change)
    with bind_host_contract(contract), pytest.raises(RuntimeError):
        launcher.pick_project_directory(BINDING)


def test_verified_native_cancellation_is_returned_without_path(transport):
    contract, state = transport
    state.payload.update(cancelled=True, path=None)
    with bind_host_contract(contract):
        assert launcher.pick_project_directory(BINDING) is None


def test_attested_multiple_paths_and_legacy_adapter(transport):
    contract, state = transport
    state.payload.pop("path")
    state.payload["paths"] = ["/native/project", "/native/second"]
    with bind_host_contract(contract):
        picker = launcher.LauncherDirectoryPickerPort(BINDING)
        assert [str(path) for path in picker.pick_directories()] == state.payload["paths"]
        assert str(picker.pick_directory()) == "/native/project"


def test_legacy_path_is_wrapped_for_multiple_selection(transport):
    contract, state = transport
    with bind_host_contract(contract):
        assert [str(path) for path in launcher.pick_project_directories(BINDING)] == [
            "/native/project"
        ]


@pytest.mark.parametrize("paths", [[], None, "/native/project", ["/native/ok", "relative"],
                                  ["/native/ok", 123], ["/native/ok", "/bad\x7f"]])
def test_malformed_array_fails_without_partial_results(transport, paths):
    contract, state = transport
    state.payload["paths"] = paths
    with bind_host_contract(contract), pytest.raises(RuntimeError):
        launcher.pick_project_directories(BINDING)


def test_multiple_selection_cancellation(transport):
    contract, state = transport
    state.payload.pop("path")
    state.payload.update(cancelled=True, paths=None)
    with bind_host_contract(contract):
        assert launcher.pick_project_directories(BINDING) is None


def test_cancelled_selection_with_paths_is_rejected(transport):
    contract, state = transport
    state.payload.pop("path")
    state.payload.update(cancelled=True, paths=["/native/project"])
    with bind_host_contract(contract), pytest.raises(RuntimeError):
        launcher.pick_project_directories(BINDING)
