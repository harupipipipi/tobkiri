"""Exercise numeric transport through the real isolated guest zipapp child.

These tests do not claim a VM or bubblewrap boundary. The private child runs
with its real non-root and Linux seccomp checks, without ambient imports.
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ecosystem.defaultspack.backend.sandbox.isolation.resources import (
    packvm_guest_runner as runner,
)
from scripts.build_packvm_guest_bundle import build_guest_bundle
from tobkiri_host import bounded_child_io
from tobkiri_host.macos_vz_supervisor import (
    MacOSVZSupervisorDriver,
    _validated_invoke_outcome,
)
from tobkiri_protocol.canonical import canonical_json, strict_loads
from tobkiri_protocol.data_codec import VERSION
from tobkiri_protocol.packvm_data_wire import (
    decode_invoke_outcome,
    encode_invoke_payload,
    public_terminal_from_child,
)


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_RESULT_KIND = "tobkiri.packvm.private.result.v1"
PUBLIC_RESULT_KIND = "tobkiri.packvm.invoke.result.v2"
LEGACY_RESULT_KIND = "tobkiri.packvm.invoke.result.v1"
TARGET = {"contract_id": "example.numeric.v1", "operation_id": "echo"}
FLOATS = [
    0.125,
    1.0,
    -0.0,
    float.fromhex("0x0.0000000000001p-1022"),
    sys.float_info.max,
]
FLOAT_IDS = ["fractional", "integral-float", "negative-zero", "subnormal", "max-finite"]
LITERAL_TAG_DATA = {
    "_tobkiri_flow_data_encoding": {"version": VERSION, "paths": [["value"]]},
    "payload_encoding": VERSION,
    "payload_tokens": ["f", "8000000000000000"],
    "nested": [
        {
            "kind": PRIVATE_RESULT_KIND,
            "outcome_encoding": VERSION,
            "outcome_tokens": ["f", "3ff0000000000000"],
        }
    ],
}


@pytest.fixture(scope="module")
def sealed_archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the source allowlist and retain the actual child security checks."""
    if not sys.platform.startswith("linux"):
        pytest.skip("the real private child boundary requires Linux")
    if os.geteuid() == 0:
        pytest.skip("the real private child must never execute as root")
    try:
        ctypes.CDLL("libseccomp.so.2")
    except OSError:
        pytest.skip("the real private child requires libseccomp.so.2")
    archive = tmp_path_factory.mktemp("numeric-archive") / "runner.pyz"
    archive.write_bytes(build_guest_bundle(ROOT))
    return archive


def _run_child(
    archive: Path,
    tmp_path: Path,
    raw_request: bytes,
    *,
    expression: str = "payload",
) -> tuple[subprocess.CompletedProcess[bytes], Path]:
    invoked = tmp_path / "invoked"
    implementation = tmp_path / "handler.py"
    implementation.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "def tobkiri_packvm_invoke(operation_id, payload):\n"
        f"    Path({str(invoked)!r}).write_text('called')\n"
        "    assert sys.flags.isolated == 1 and sys.flags.no_site == 1\n"
        "    from tobkiri_protocol import data_codec\n"
        "    assert data_codec.__file__.startswith(sys.argv[0] + '/')\n"
        "    assert 'jsonschema' not in sys.modules\n"
        f"    return {expression}\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(archive),
            "--execute",
            str(implementation),
        ],
        input=raw_request,
        capture_output=True,
        check=False,
        timeout=10,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
    )
    return result, invoked


def _assert_identical(actual: Any, expected: Any) -> None:
    assert type(actual) is type(expected)
    if type(expected) is float:
        assert struct.pack(">d", actual) == struct.pack(">d", expected)
    elif type(expected) is dict:
        assert actual.keys() == expected.keys()
        for key in expected:
            _assert_identical(actual[key], expected[key])
    elif type(expected) is list:
        assert len(actual) == len(expected)
        for observed, wanted in zip(actual, expected):
            _assert_identical(observed, wanted)
    else:
        assert actual == expected


@pytest.mark.parametrize("number", FLOATS, ids=FLOAT_IDS)
def test_archive_numeric_echo_preserves_float_bits_and_types(
    sealed_archive: Path,
    tmp_path: Path,
    number: float,
) -> None:
    """Both child input and output traverse the shipped finite token codec."""
    payload = {"number": number, "integer": 1, "boolean": True}
    request = {**TARGET, **encode_invoke_payload(payload)}
    assert set(request) == {*TARGET, "payload_encoding", "payload_tokens"}
    result, invoked = _run_child(sealed_archive, tmp_path, canonical_json(request))
    assert result.returncode == 0, result.stderr.decode()
    assert invoked.exists()
    private = strict_loads(result.stdout)
    assert set(private) == {"kind", "outcome_encoding", "outcome_tokens"}
    assert private["kind"] == PRIVATE_RESULT_KIND
    assert private["outcome_encoding"] == VERSION
    public = public_terminal_from_child(private)
    assert public["kind"] == PUBLIC_RESULT_KIND
    _assert_identical(decode_invoke_outcome(public), payload)


def test_archive_nested_numeric_data_does_not_interpret_reserved_tag_literals(
    sealed_archive: Path,
    tmp_path: Path,
) -> None:
    payload = {"values": [FLOATS, {"literal": LITERAL_TAG_DATA}], "none": None}
    result, _ = _run_child(
        sealed_archive,
        tmp_path,
        canonical_json({**TARGET, **encode_invoke_payload(payload)}),
    )
    assert result.returncode == 0, result.stderr.decode()
    _assert_identical(
        decode_invoke_outcome(public_terminal_from_child(strict_loads(result.stdout))),
        payload,
    )


@pytest.mark.parametrize(
    "payload",
    [{}, {"a": [None, True, 1, -1, "text"]}, LITERAL_TAG_DATA],
    ids=["empty", "canonical", "literal-tags"],
)
def test_archive_legacy_payload_and_terminal_keep_exact_canonical_bytes(
    sealed_archive: Path,
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    request = {**TARGET, **encode_invoke_payload(payload)}
    assert request == {**TARGET, "payload": payload}
    raw = canonical_json(request)
    assert raw == canonical_json({**TARGET, "payload": payload})
    result, _ = _run_child(sealed_archive, tmp_path, raw)
    assert result.returncode == 0, result.stderr.decode()
    expected = {"kind": LEGACY_RESULT_KIND, "outcome": payload}
    assert result.stdout == canonical_json(expected)
    assert public_terminal_from_child(strict_loads(result.stdout)) == expected


INVALID_VARIANTS = [
    {"payload_tokens": ["o", 1, "n", "f", "3ff0000000000000"]},
    {"payload_encoding": VERSION},
    {
        "payload_encoding": "tobkiri.flow-data.ieee754.v999",
        "payload_tokens": ["o", 1, "n", "f", "3ff0000000000000"],
    },
    {"payload_encoding": None, "payload_tokens": []},
    {"payload_encoding": {"version": VERSION}, "payload_tokens": []},
    {
        "payload": {},
        "payload_encoding": VERSION,
        "payload_tokens": ["o", 1, "n", "f", "3ff0000000000000"],
    },
    {
        "payload_encoding": VERSION,
        "payload_tokens": ["o", 1, "n", "f", "7ff0000000000000"],
    },
    {
        "payload_encoding": VERSION,
        "payload_tokens": ["o", 1, "n", "f", "7ff8000000000000"],
    },
    {
        "payload_encoding": VERSION,
        "payload_tokens": ["o", 1, "n", "f", "3FF0000000000000"],
    },
    {"payload_encoding": VERSION, "payload_tokens": ["o", 1, "n", "f", "0000"]},
    {
        "payload_encoding": VERSION,
        "payload_tokens": ["o", 1, "n", "f", "3ff0000000000000", "n"],
    },
    {"payload_encoding": VERSION, "payload_tokens": ["o", 1, "n", "i", 1]},
    {"payload_encoding": VERSION, "payload_tokens": ["o", 1]},
    {"payload_encoding": VERSION, "payload_tokens": ["a", 100001]},
    {
        "payload_encoding": VERSION,
        "payload_tokens": ["o", 2, "n", "f", "3ff0000000000000", "n", "n"],
    },
]
INVALID_IDS = [
    "marker-stripped",
    "tokens-stripped",
    "unknown-version",
    "null-marker",
    "object-marker",
    "dual-representation",
    "infinity",
    "nan",
    "uppercase-hex",
    "short-hex",
    "trailing-token",
    "no-float",
    "truncated",
    "node-overflow",
    "duplicate-token-key",
]


@pytest.mark.parametrize("variant", INVALID_VARIANTS, ids=INVALID_IDS)
def test_archive_rejects_malformed_numeric_transport_before_pack_invocation(
    sealed_archive: Path,
    tmp_path: Path,
    variant: dict[str, object],
) -> None:
    result, invoked = _run_child(
        sealed_archive,
        tmp_path,
        canonical_json({**TARGET, **variant}),
    )
    assert result.returncode == 1
    assert result.stdout == b""
    assert not invoked.exists()


@pytest.mark.parametrize(
    "value",
    [
        b"0.125",
        b"1.0",
        b"-0.0",
        b"NaN",
        b"Infinity",
        b"1e999",
        b"9007199254740992",
        b'"\\ud800"',
        b'"\xff"',
        b'{"duplicate":1,"duplicate":2}',
    ],
    ids=[
        "raw-fraction",
        "raw-integral-float",
        "raw-negative-zero",
        "raw-nan",
        "raw-infinity",
        "raw-overflow",
        "unsafe-integer",
        "surrogate",
        "invalid-utf8",
        "duplicate-key",
    ],
)
def test_archive_rejects_invalid_legacy_json_before_pack_invocation(
    sealed_archive: Path,
    tmp_path: Path,
    value: bytes,
) -> None:
    raw = (
        b'{"contract_id":"example.numeric.v1","operation_id":"echo",'
        b'"payload":{"value":' + value + b"}}"
    )
    result, invoked = _run_child(sealed_archive, tmp_path, raw)
    assert result.returncode == 1
    assert result.stdout == b""
    assert not invoked.exists()


@pytest.mark.parametrize(
    "expression",
    [
        "{'value': float('nan')}",
        "{'value': float('inf')}",
        "{'value': -float('inf')}",
        "{'value': 9007199254740992}",
        "{'value': '\\ud800'}",
        repr(
            {
                "kind": PRIVATE_RESULT_KIND,
                "outcome_encoding": VERSION,
                "outcome_tokens": ["o", 1, "value", "f", "3ff0000000000000"],
            }
        ),
    ],
    ids=[
        "nan",
        "infinity",
        "negative-infinity",
        "unsafe-integer",
        "surrogate",
        "forged-private-control",
    ],
)
def test_archive_rejects_invalid_pack_results(
    sealed_archive: Path,
    tmp_path: Path,
    expression: str,
) -> None:
    result, invoked = _run_child(
        sealed_archive,
        tmp_path,
        canonical_json({**TARGET, "payload": {}}),
        expression=expression,
    )
    assert invoked.exists()
    assert result.returncode == 1
    assert result.stdout == b""


def _inject_child_stdout(
    monkeypatch: pytest.MonkeyPatch,
    stdout: bytes,
) -> dict[str, object]:
    def communicate(process: object, request: bytes, **limits: object) -> bytes:
        assert strict_loads(request) == {**TARGET, "payload": {}}
        assert limits["stdout_limit"] == runner.MAX_RESULT_BYTES
        assert limits["stderr_limit"] == runner.MAX_CHILD_STDERR_BYTES
        return stdout

    monkeypatch.setattr(bounded_child_io, "communicate_bounded", communicate)
    return runner._communicate_staged_implementation(
        SimpleNamespace(returncode=0),
        {**TARGET, "payload": {}},
    )


def test_root_rebuilds_validated_private_numeric_stdout_as_public_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = {
        "kind": PRIVATE_RESULT_KIND,
        "outcome_encoding": VERSION,
        "outcome_tokens": ["o", 1, "value", "f", "8000000000000000"],
    }
    public = _inject_child_stdout(monkeypatch, canonical_json(private))
    assert public["kind"] == PUBLIC_RESULT_KIND
    assert public == public_terminal_from_child(private)
    _assert_identical(decode_invoke_outcome(public), {"value": -0.0})


@pytest.mark.parametrize(
    "stdout",
    [
        canonical_json(
            {
                "kind": PRIVATE_RESULT_KIND,
                "outcome_tokens": ["o", 1, "n", "f", "3ff0000000000000"],
            }
        ),
        canonical_json(
            {
                "kind": PRIVATE_RESULT_KIND,
                "outcome_encoding": VERSION,
                "outcome_tokens": ["o", 1, "n", "f", "7ff0000000000000"],
            }
        ),
        canonical_json(
            {
                "kind": PRIVATE_RESULT_KIND,
                "outcome_encoding": "unknown",
                "outcome_tokens": ["o", 1, "n", "f", "3ff0000000000000"],
            }
        ),
        canonical_json(
            {
                "kind": PRIVATE_RESULT_KIND,
                "outcome_encoding": VERSION,
                "outcome_tokens": ["o", 1, "n", "f", "3ff0000000000000"],
                "outcome": {},
            }
        ),
        canonical_json(
            {
                "kind": PRIVATE_RESULT_KIND,
                "outcome_encoding": VERSION,
                "outcome_tokens": ["o", 1, "n", "f", "3ff0000000000000", "n"],
            }
        ),
        canonical_json(
            {
                "kind": PRIVATE_RESULT_KIND,
                "outcome_encoding": VERSION,
                "outcome_tokens": ["o", 1, "n", "i", 1],
            }
        ),
        canonical_json(
            {
                "kind": PUBLIC_RESULT_KIND,
                "outcome_encoding": VERSION,
                "outcome_tokens": ["o", 1, "n", "f", "3ff0000000000000"],
            }
        ),
        b'{"kind":"tobkiri.packvm.invoke.result.v1","outcome":{"value":1.0}}',
        b'{"kind":"tobkiri.packvm.invoke.result.v1","outcome":{"value":NaN}}',
        b'{"kind":"tobkiri.packvm.invoke.result.v1","outcome":{"value":9007199254740992}}',
        b'{"kind":"tobkiri.packvm.invoke.result.v1","outcome":{"value":"\\ud800"}}',
        b'{"kind":"tobkiri.packvm.invoke.result.v1","outcome":{},"outcome":{}}',
        b"{}\n{}",
    ],
    ids=[
        "marker-stripped",
        "infinity",
        "unknown-version",
        "dual-representation",
        "trailing-token",
        "no-float",
        "forged-public-control",
        "raw-float",
        "raw-nan",
        "unsafe-integer",
        "surrogate",
        "duplicate-key",
        "multiple-records",
    ],
)
def test_root_rejects_malformed_or_forged_numeric_child_stdout(
    monkeypatch: pytest.MonkeyPatch,
    stdout: bytes,
) -> None:
    with pytest.raises(ValueError):
        _inject_child_stdout(monkeypatch, stdout)


@pytest.mark.parametrize(
    "kind",
    [
        PRIVATE_RESULT_KIND,
        PUBLIC_RESULT_KIND,
        LEGACY_RESULT_KIND,
        "tobkiri.packvm.bridge.request.v1",
        "tobkiri.packvm.continuation.intent.v2",
    ],
)
def test_root_rejects_control_frames_hidden_inside_private_numeric_outcome(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    encoded = encode_invoke_payload({"kind": kind, "value": 0.125})
    private = {
        "kind": PRIVATE_RESULT_KIND,
        "outcome_encoding": VERSION,
        "outcome_tokens": encoded["payload_tokens"],
    }
    with pytest.raises(ValueError, match="not a terminal outcome"):
        _inject_child_stdout(monkeypatch, canonical_json(private))


def test_archive_numeric_result_survives_actual_signed_and_mac_boundaries(
    sealed_archive: Path,
    tmp_path: Path,
) -> None:
    """Real archive, bounded pipes, Ed25519, HMAC, and Host verification compose.

    This models only the helper transport, not a VM, launch attestation, or
    artifact staging. No child security check or authentication is mocked.
    """
    payload = {"numbers": FLOATS, "literal": LITERAL_TAG_DATA, "integer": 1}
    implementation = tmp_path / "echo.py"
    implementation.write_text(
        "def tobkiri_packvm_invoke(operation_id, payload):\n"
        "    assert operation_id == 'echo'\n"
        "    return payload\n",
        encoding="utf-8",
    )
    key = Ed25519PrivateKey.generate()
    channel_key = b"numeric-wire-test-channel-key!!!"
    assert len(channel_key) == 32
    domain = "numeric-domain"
    request_id = "numeric-request"
    nonce = "a" * 64
    challenge = "b" * 64
    bindings = {"artifact": "sha256:" + "c" * 64}
    binding_digest = "sha256:" + "d" * 64
    envelope = {
        "operation": "invoke",
        "host_nonce": nonce,
        "domain_id": domain,
        "launch_binding_digest": binding_digest,
        "request": {**TARGET, **encode_invoke_payload(payload)},
    }
    child_public: list[dict[str, object]] = []

    class SerializedHelper:
        def exchange(self, request: dict[str, Any]) -> dict[str, Any]:
            transported = strict_loads(canonical_json(request))
            assert transported == envelope
            with subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-B",
                    str(sealed_archive),
                    "--execute",
                    str(implementation),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=tmp_path,
                env={"PATH": "/usr/bin:/bin"},
                start_new_session=True,
                close_fds=True,
            ) as process:
                public = runner._communicate_staged_implementation(
                    process,
                    transported["request"],
                )
                assert process.returncode == 0
            child_public.append(public)
            assert public["kind"] == PUBLIC_RESULT_KIND
            signed = runner._sign_agent_response(
                runner._agent_success(
                    {
                        "operation": "invoke",
                        "request_id": request_id,
                        "domain_id": domain,
                        "binding_digests": bindings,
                        "guest_challenge": challenge,
                    },
                    public,
                ),
                key,
            )
            core = {
                "kind": "tobkiri.macos-vz.supervisor.response.v1",
                "protocol": "io.tobkiri.macos-vz-supervisor.v1",
                "version": 1,
                "operation": "invoke",
                "host_nonce": nonce,
                "domain_id": domain,
                "launch_binding_digest": binding_digest,
                "payload": strict_loads(canonical_json(signed)),
            }
            response = {
                **core,
                "agent_mac": hmac.new(
                    channel_key,
                    canonical_json(core),
                    hashlib.sha256,
                ).hexdigest(),
            }
            return strict_loads(canonical_json(response))

    # Only the methods below are under test; no fake launch is admitted.
    driver = object.__new__(MacOSVZSupervisorDriver)
    driver._lock = threading.RLock()
    driver._compromised_reason = None
    response = driver._exchange(
        envelope,
        transport=SerializedHelper(),
        channel_key=channel_key,
        expected_operation="invoke",
        expected_host_nonce=nonce,
        expected_domain_id=domain,
        expected_binding_digest=binding_digest,
    )
    verified = driver._validated_guest_response(
        response["payload"],
        operation="invoke",
        request_id=request_id,
        domain_id=domain,
        binding_digests=bindings,
        guest_challenge=challenge,
        public_key=key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
    )
    assert verified == child_public[0]
    _assert_identical(_validated_invoke_outcome(verified), payload)
    assert driver._compromised_reason is None


@pytest.mark.parametrize("child_value", [
    {"answer": 42},
    {"kind": []},
    {"kind": {}},
    {"kind": "application-kind"},
    {"state": "pending", "host_bridge_request": {"approved": True}},
    {"kind": "tobkiri.packvm.private.result.v999", "outcome_tokens": []},
    {"kind": "tobkiri.packvm.continuation.intent.v2", "hop": 0},
])
def test_root_never_signs_unwrapped_child_data_or_authority_claims(
    monkeypatch: pytest.MonkeyPatch, child_value: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="unrecognized child result"):
        _inject_child_stdout(monkeypatch, canonical_json(child_value))


@pytest.mark.parametrize("extra_byte", [False, True])
def test_root_counts_entire_encoded_child_request_against_wire_limit(
    monkeypatch: pytest.MonkeyPatch, extra_byte: bool,
) -> None:
    fields = encode_invoke_payload({"value": 0.5, "text": ""})
    request = {**TARGET, **fields}
    overhead = len(canonical_json(request))
    text_bytes = runner.MAX_CHILD_REQUEST_BYTES - overhead + int(extra_byte)
    request.update(encode_invoke_payload({"value": 0.5, "text": "x" * text_bytes}))
    assert len(canonical_json(request)) == runner.MAX_CHILD_REQUEST_BYTES + int(extra_byte)
    observed = []
    terminal = {"kind": LEGACY_RESULT_KIND, "outcome": {}}

    def communicate(process: object, raw: bytes, **kwargs: object) -> bytes:
        observed.append(raw)
        return canonical_json(terminal)

    monkeypatch.setattr(bounded_child_io, "communicate_bounded", communicate)
    monkeypatch.setattr(runner, "_stop_staged_implementation", lambda process: None)
    process = SimpleNamespace(returncode=0)
    if extra_byte:
        with pytest.raises(ValueError, match="payload exceeds size limit"):
            runner._communicate_staged_implementation(process, request)
        assert observed == []
    else:
        assert runner._communicate_staged_implementation(process, request) == terminal
        assert len(observed[0]) == runner.MAX_CHILD_REQUEST_BYTES


@pytest.mark.parametrize("extra_byte", [False, True])
def test_root_counts_entire_private_terminal_against_stdout_wire_limit(
    monkeypatch: pytest.MonkeyPatch, extra_byte: bool,
) -> None:
    # Exercise the exact enclosing-record edge at a bounded test ceiling. The
    # live constant is already asserted by _inject_child_stdout's pipe adapter.
    private = {"kind": PRIVATE_RESULT_KIND, "outcome_encoding": VERSION,
               "outcome_tokens": ["o", 1, "value", "f", "3fe0000000000000"]}
    raw = canonical_json(private)
    monkeypatch.setattr(runner, "MAX_RESULT_BYTES", len(raw) - int(extra_byte))
    if extra_byte:
        with pytest.raises(ValueError, match="result exceeds size limit"):
            _inject_child_stdout(monkeypatch, raw)
    else:
        assert _inject_child_stdout(monkeypatch, raw)["kind"] == PUBLIC_RESULT_KIND
