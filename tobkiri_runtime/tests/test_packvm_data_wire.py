"""Bounded application-only codec admission at ordinary PackVM wire boundaries."""
from __future__ import annotations

import json
import struct

import pytest

from tobkiri_protocol.canonical import canonical_json, strict_loads
from tobkiri_protocol.data_codec import (
    VERSION, CodecError, Limits, decode_data_tokens, decode_payload, encode_payload,
)
from tobkiri_protocol import packvm_data_wire as wire


@pytest.mark.parametrize("number", [0.375, 1.0, -0.0, 5e-324, float.fromhex("0x1.fffffffffffffp1023")])
def test_request_private_public_chain_preserves_exact_binary64(number: float) -> None:
    value = {"numbers": [number, 1, True, None], "tag": ["f", "8000000000000000"]}
    request = wire.encode_invoke_payload(value)
    assert set(request) == {"payload_encoding", "payload_tokens"}
    restored = wire.decode_invoke_payload(strict_loads(canonical_json(request)))
    private = wire.private_terminal(restored)
    public = wire.public_terminal_from_child(strict_loads(canonical_json(private)))
    actual = wire.decode_invoke_outcome(strict_loads(canonical_json(public)))
    assert struct.pack(">d", actual["numbers"][0]) == struct.pack(">d", number)
    assert actual["tag"] == value["tag"]
    assert actual["numbers"][1:] == [1, True, None]


def test_legacy_wire_bytes_and_reserved_tag_values_are_unchanged() -> None:
    value = {"outcome_encoding": VERSION, "payload_tokens": ["f", "8000000000000000"],
             "kind": "ordinary", "unicode": "音声"}
    assert canonical_json(wire.encode_invoke_payload(value)) == canonical_json({"payload": value})
    old = {"kind": wire.INVOKE_RESULT_KIND, "outcome": value}
    assert canonical_json(wire.private_terminal(value)) == canonical_json(old)
    assert canonical_json(wire.public_terminal_from_child(old)) == canonical_json(old)
    assert wire.decode_invoke_outcome(old) == value


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 2**53, "\ud800"])
def test_invalid_application_data_never_encodes(value: object) -> None:
    for encode in (wire.encode_invoke_payload, wire.private_terminal):
        with pytest.raises(ValueError):
            encode({"value": value})


@pytest.mark.parametrize("change", [
    lambda value: value.pop("payload_encoding"),
    lambda value: value.pop("payload_tokens"),
    lambda value: value.update(payload={}),
    lambda value: value.update(payload_encoding="unknown"),
    lambda value: value.update(payload_encoding=None),
    lambda value: value.update(payload_tokens={}),
])
def test_request_marker_is_exact_and_cannot_be_stripped(change: object) -> None:
    record = wire.encode_invoke_payload({"value": 0.5})
    change(record)
    with pytest.raises(ValueError):
        wire.decode_invoke_payload(record)


@pytest.mark.parametrize("kind", [wire.PRIVATE_RESULT_KIND, wire.ENCODED_INVOKE_RESULT_KIND,
                                  wire.INVOKE_RESULT_KIND, "tobkiri.packvm.bridge.request.v1"])
def test_untrusted_private_tokens_cannot_smuggle_control_frames(kind: str) -> None:
    encoded = encode_payload({"kind": kind, "value": 0.5})
    record = {"kind": wire.PRIVATE_RESULT_KIND, "outcome_encoding": VERSION,
              "outcome_tokens": strict_loads(encoded.payload)}
    with pytest.raises(ValueError, match="not a terminal outcome"):
        wire.public_terminal_from_child(record)


@pytest.mark.parametrize("encoding", [None, True, 1, {}, [], "unknown"])
def test_public_v2_requires_exact_marker_even_after_authentication(encoding: object) -> None:
    with pytest.raises(ValueError):
        wire.decode_invoke_outcome({"kind": wire.ENCODED_INVOKE_RESULT_KIND,
                                   "outcome_encoding": encoding, "outcome_tokens": {"value": 1}})


@pytest.mark.parametrize("tokens", [
    ["o", 1, "value", "f", "7ff0000000000000"],
    ["o", 1, "value", "f", "7ff8000000000000"],
    ["o", 1, "value", "f", "FFFFFFFFFFFFFFFF"],
    ["o", 1, "value", "f", "0000"],
    ["o", 1, "value", "i", 2**53],
    ["o", 1, "value", "s", "\ud800"],
    ["o", 1, "value", "f"],
    ["o", 1, "value", "f", "3fe0000000000000", "n"],
    ["o", 1, "value", "i", 1],
    ["o", True],
    ["o", 2, "z", "f", "3fe0000000000000", "a", "n"],
    ["o", 2, "a", "f", "3fe0000000000000", "a", "n"],
    ["o", 1, "value", "a", 100001],
])
def test_untrusted_private_tokens_are_fully_validated(tokens: list) -> None:
    with pytest.raises(ValueError):
        wire.public_terminal_from_child({"kind": wire.PRIVATE_RESULT_KIND,
                                        "outcome_encoding": VERSION, "outcome_tokens": tokens})


def test_private_parser_implies_no_authenticated_record_authority() -> None:
    encoded = encode_payload({"value": 0.5})
    assert decode_data_tokens(encoded.payload, encoding=VERSION) == {"value": 0.5}
    with pytest.raises(CodecError, match="unauthenticated"):
        decode_payload(encoded.payload, encoding=VERSION)
    with pytest.raises(CodecError, match="non-canonical"):
        decode_data_tokens(b" " + encoded.payload, encoding=VERSION)
    with pytest.raises(CodecError, match="duplicate"):
        decode_data_tokens(b'{"a":1,"a":2}', encoding=VERSION)


def test_flat_wire_does_not_bypass_decoded_depth_or_node_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wire, "RESULT_LIMITS", Limits(max_bytes=4096, max_depth=3, max_nodes=8))
    for value in ({"value": [[[[0.5]]]]}, {"value": [0.5] * 8}):
        encoded = encode_payload(value)
        with pytest.raises(CodecError, match="depth|node|budget"):
            wire.public_terminal_from_child({"kind": wire.PRIVATE_RESULT_KIND,
                                            "outcome_encoding": VERSION,
                                            "outcome_tokens": strict_loads(encoded.payload)})


def test_expanded_wire_is_subject_to_existing_byte_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wire, "REQUEST_LIMITS", Limits(max_bytes=90))
    value = {"values": [0.5] * 4}
    assert len(json.dumps(value).encode()) < 90
    with pytest.raises(CodecError, match="byte limit"):
        wire.encode_invoke_payload(value)
    encoded = encode_payload(value)
    with pytest.raises(CodecError, match="byte limit"):
        wire.decode_invoke_payload({"payload_encoding": VERSION,
                                    "payload_tokens": strict_loads(encoded.payload)})


def test_child_cannot_claim_public_encoding_or_extra_authority() -> None:
    private = wire.private_terminal({"value": 0.5})
    public = wire.public_terminal_from_child(private)
    for invalid in (public, {**private, "approved": True}, {**private, "outcome": {}}):
        with pytest.raises(ValueError):
            wire.public_terminal_from_child(invalid)
    assert private["kind"] == wire.PRIVATE_RESULT_KIND
    assert public["kind"] == wire.ENCODED_INVOKE_RESULT_KIND


def _guest_request() -> dict:
    return {
        "operation": "invoke", "request_id": "request", "target_domain": "domain",
        "artifact_digest": "sha256:" + "a" * 64,
        "materialization_digest": "sha256:" + "b" * 64,
        "guest_artifact_identity": "sha256:" + "c" * 64,
        "contract_id": "example.numeric.v1", "contract_version": "1.0.0",
        "operation_id": "echo", "request_digest": "sha256:" + "d" * 64,
        "deadline_monotonic": "60", "cancel_token": "e" * 64,
        **wire.encode_invoke_payload({"value": 0.5}),
    }


def test_guest_root_passes_validated_carrier_without_lifting_user_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    from ecosystem.defaultspack.backend.sandbox.isolation.resources import packvm_guest_runner as guest

    request = _guest_request()
    observed = []

    def execute(captured: dict, payload: dict, deadline: float, **kwargs: object) -> dict:
        observed.append((captured, payload, kwargs["payload_fields"]))
        return {"ok": True}

    monkeypatch.setattr(guest.os, "geteuid", lambda: 0)
    monkeypatch.setattr(guest, "_execute_invocation_step", execute)
    assert guest._invoke(request) == {"ok": True}
    assert observed == [(request, {}, {key: request[key] for key in ("payload_encoding", "payload_tokens")})]
    assert request["request_digest"] == "sha256:" + "d" * 64


@pytest.mark.parametrize("change", [
    lambda request: request.pop("payload_encoding"),
    lambda request: request.update(payload_encoding="unknown"),
    lambda request: request.update(payload={}),
    lambda request: request.update(contract_id="conversation.saved-turn.v1", operation_id="saved_complete"),
    lambda request: request.update(contract_id="tobkiri.service.mcp.tool.call.v1", operation_id="rumi_mcp_gateway_pack.mcp-tool-call"),
    lambda request: request.update(operation_id="rumi_mcp_gateway_pack.mcp-tool-call"),
])
def test_guest_root_rejects_unadapted_or_malformed_variants_before_artifact_access(
    monkeypatch: pytest.MonkeyPatch, change: object,
) -> None:
    from ecosystem.defaultspack.backend.sandbox.isolation.resources import packvm_guest_runner as guest

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid wire request reached artifact execution")

    monkeypatch.setattr(guest, "_execute_invocation_step", forbidden)
    request = _guest_request()
    change(request)
    with pytest.raises(ValueError):
        guest._invoke(request)


def _nested_value(depth: int, leaf: object = 0.5) -> dict:
    value = leaf
    for _ in range(depth - 1):
        value = [value]
    return {"value": value}


@pytest.mark.parametrize(("data_depth", "encode", "enclose"), [
    (62, wire.encode_invoke_payload, lambda value: {"request": {"payload": value}}),
    (61, wire.private_terminal, lambda value: {"payload": {"data": {"outcome": value}}}),
])
def test_logical_depth_reserves_all_surrounding_transport_fields(
    data_depth: int, encode: object, enclose: object,
) -> None:
    for leaf in (0.5, 1):
        value = _nested_value(data_depth, leaf)
        encode(value)
        # For legacy values the historical canonical envelope accepts exactly
        # this depth, and rejects the next level. Float tokens must match it.
        legacy = _nested_value(data_depth, 1)
        canonical_json(enclose(legacy))
        with pytest.raises(ValueError, match="depth"):
            canonical_json(enclose(_nested_value(data_depth + 1, 1)))
        with pytest.raises(CodecError, match="depth"):
            encode(_nested_value(data_depth + 1, leaf))
    # A forged flat token stream cannot sidestep the same decoded bound.
    tokens = strict_loads(encode_payload(_nested_value(data_depth + 1)).payload)
    with pytest.raises(CodecError, match="depth"):
        if data_depth == 62:
            wire.decode_invoke_payload({"payload_encoding": VERSION, "payload_tokens": tokens})
        else:
            wire.public_terminal_from_child({"kind": wire.PRIVATE_RESULT_KIND,
                                            "outcome_encoding": VERSION, "outcome_tokens": tokens})
