"""Focused tests for the isolated Defaultspack Conversation PackVM ABI."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping

import pytest

from ecosystem.defaultspack.runtime import conversation


_MESSAGES = [{"role": "user", "content": "hello"}]


def _bridge_result(
    bridge_request: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    continuation = bridge_request["continuation"]
    return {
        "kind": conversation.PACKVM_BRIDGE_RESULT_KIND,
        "protocol": conversation.PACKVM_BRIDGE_PROTOCOL,
        "version": conversation.PACKVM_BRIDGE_VERSION,
        "operation_id": "complete",
        "nonce": continuation["nonce"],
        "target": dict(continuation["target"]),
        "request_digest": continuation["request_digest"],
        "result": dict(result),
        "result_digest": conversation._canonical_digest(result),
    }


def test_staged_conversation_exports_guest_abi_under_isolated_python() -> None:
    """The one-file artifact loads with neither repo imports nor site packages."""

    source = Path(conversation.__file__).resolve()
    script = """
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("staged_conversation", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
result = module.tobkiri_packvm_invoke(
    "complete", {"messages": [{"role": "user", "content": "isolated"}]}
)
print(json.dumps(result, sort_keys=True, separators=(",", ":")))
"""
    process = subprocess.run(
        (sys.executable, "-I", "-S", "-c", script, str(source)),
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result["kind"] == conversation.PACKVM_BRIDGE_REQUEST_KIND
    assert result["target"] == {
        "contract_id": conversation.AI_GENERATE_CONTRACT,
        "operation_id": conversation.AI_GENERATE_OPERATION,
    }
    assert result["continuation"]["nonce"]


def test_guest_complete_emits_a_fixed_target_and_digest_bound_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guest can request only the canonical Host AI capability."""

    monkeypatch.setattr(conversation.secrets, "token_hex", lambda _size: "a" * 48)

    result = conversation.tobkiri_packvm_invoke("complete", {"messages": _MESSAGES})

    assert result == {
        "kind": conversation.PACKVM_BRIDGE_REQUEST_KIND,
        "protocol": conversation.PACKVM_BRIDGE_PROTOCOL,
        "version": 1,
        "target": {
            "contract_id": conversation.AI_GENERATE_CONTRACT,
            "operation_id": conversation.AI_GENERATE_OPERATION,
        },
        "request": {
            "messages": _MESSAGES,
            "requirements": {"request_surface": "defaultspack.conversation"},
        },
        "request_digest": conversation._canonical_digest(
            {
                "messages": _MESSAGES,
                "requirements": {"request_surface": "defaultspack.conversation"},
            }
        ),
        "continuation": {
            "kind": conversation.PACKVM_CONTINUATION_KIND,
            "protocol": conversation.PACKVM_BRIDGE_PROTOCOL,
            "version": 1,
            "operation_id": "complete",
            "nonce": "a" * 48,
            "target": {
                "contract_id": conversation.AI_GENERATE_CONTRACT,
                "operation_id": conversation.AI_GENERATE_OPERATION,
            },
            "request_digest": conversation._canonical_digest(
                {
                    "messages": _MESSAGES,
                    "requirements": {"request_surface": "defaultspack.conversation"},
                }
            ),
        },
    }


def test_guest_strips_outer_host_metadata_before_requesting_capability() -> None:
    """Guest input cannot choose a profile, surface, or target for the Host."""

    result = conversation.tobkiri_packvm_invoke(
        "complete",
        {
            "messages": _MESSAGES,
            "profile_id": "attacker-selected-profile",
            "requirements": {"request_surface": "attacker.surface"},
            "caller_metadata": {"request_id": "panel-request"},
        },
    )

    assert result["request"] == {
        "messages": _MESSAGES,
        "requirements": {"request_surface": "defaultspack.conversation"},
    }


def test_guest_resumes_matching_host_result_with_existing_completion_projection() -> None:
    """A Host bridge success returns the normal Conversation completion shape."""

    request = conversation.tobkiri_packvm_invoke("complete", {"messages": _MESSAGES})
    bridge_result = _bridge_result(
        request,
        {
            "status": "ok",
            "value": {
                "output": "hello from the Host Broker",
                "tool_intents": [
                    {
                        "intent_id": "tool-1",
                        "operation": "tools.example",
                        "arguments": {"value": 7},
                    }
                ],
            },
        },
    )

    result = conversation.tobkiri_packvm_invoke(
        "complete",
        {"continuation": request["continuation"], "bridge_result": bridge_result},
    )

    assert result["content"] == [
        {"type": "text", "text": "hello from the Host Broker"},
        {
            "type": "tool_use",
            "id": "tool-1",
            "name": "tools.example",
            "input": {"value": 7},
        },
    ]
    assert result["tool_calls"] == [
        {
            "intent_id": "tool-1",
            "operation": "tools.example",
            "arguments": {"value": 7},
        }
    ]


@pytest.mark.parametrize("field", ("nonce", "target", "request_digest", "result"))
def test_guest_rejects_tampered_or_cross_request_bridge_result(field: str) -> None:
    """All Host output identities are bound to the emitted continuation."""

    request = conversation.tobkiri_packvm_invoke("complete", {"messages": _MESSAGES})
    bridge_result = _bridge_result(
        request,
        {"status": "ok", "value": {"output": "safe"}},
    )
    if field == "nonce":
        bridge_result[field] = "b" * 48
    elif field == "target":
        bridge_result[field] = {
            "contract_id": conversation.AI_GENERATE_CONTRACT,
            "operation_id": "attacker.selected.operation",
        }
    elif field == "request_digest":
        bridge_result[field] = "sha256:" + "0" * 64
    else:
        bridge_result[field] = {"status": "ok", "value": {"output": "tampered"}}

    with pytest.raises(ValueError):
        conversation.tobkiri_packvm_invoke(
            "complete",
            {"continuation": request["continuation"], "bridge_result": bridge_result},
        )


def test_guest_rejects_replayed_continuation_and_projects_typed_bridge_error() -> None:
    """A guest fences local replay and never leaks a raw Host error envelope."""

    request = conversation.tobkiri_packvm_invoke("complete", {"messages": _MESSAGES})
    bridge_result = _bridge_result(
        request,
        {
            "status": "error",
            "error": {"code": "CAPABILITY_UNAVAILABLE", "message": "Provider is offline"},
        },
    )
    payload = {"continuation": request["continuation"], "bridge_result": bridge_result}

    assert conversation.tobkiri_packvm_invoke("complete", payload) == {
        "content": [],
        "tool_calls": [],
        "error": {
            "code": "CAPABILITY_UNAVAILABLE",
            "message": "Provider is offline",
        },
    }
    with pytest.raises(ValueError, match="already consumed"):
        conversation.tobkiri_packvm_invoke("complete", payload)


def test_guest_rejects_extra_envelope_fields_and_unbounded_error() -> None:
    """The Host channel stays strict even when its result is authenticated."""

    request = conversation.tobkiri_packvm_invoke("complete", {"messages": _MESSAGES})
    bridge_result = _bridge_result(
        request,
        {
            "status": "error",
            "error": {"code": "CAPABILITY_UNAVAILABLE", "message": "x" * 513},
        },
    )
    bridge_result["untrusted_metadata"] = "must not cross the guest ABI"

    with pytest.raises(ValueError, match="fields are invalid"):
        conversation.tobkiri_packvm_invoke(
            "complete",
            {"continuation": request["continuation"], "bridge_result": bridge_result},
        )

    bounded_result = _bridge_result(
        request,
        {
            "status": "error",
            "error": {"code": "CAPABILITY_UNAVAILABLE", "message": "x" * 513},
        },
    )
    with pytest.raises(ValueError, match="message is invalid"):
        conversation.tobkiri_packvm_invoke(
            "complete",
            {"continuation": request["continuation"], "bridge_result": bounded_result},
        )


def test_guest_accepts_finite_json_numbers_and_bounds_local_replay_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finite provider numbers work without turning the local fence into a DoS."""

    conversation._CONSUMED_CONTINUATION_NONCES.clear()
    conversation._CONSUMED_CONTINUATION_ORDER.clear()
    monkeypatch.setattr(conversation, "_MAX_CONTINUATIONS_PER_GUEST", 2)

    for index in range(3):
        monkeypatch.setattr(
            conversation.secrets,
            "token_hex",
            lambda _size, index=index: f"{index + 1:048x}",
        )
        request = conversation.tobkiri_packvm_invoke(
            "complete", {"messages": _MESSAGES}
        )
        bridge_result = _bridge_result(
            request,
            {"status": "ok", "value": {"output": "safe", "score": 0.5}},
        )
        result = conversation.tobkiri_packvm_invoke(
            "complete",
            {"continuation": request["continuation"], "bridge_result": bridge_result},
        )
        assert result["score"] == 0.5

    assert len(conversation._CONSUMED_CONTINUATION_NONCES) == 2
    assert len(conversation._CONSUMED_CONTINUATION_ORDER) == 2
