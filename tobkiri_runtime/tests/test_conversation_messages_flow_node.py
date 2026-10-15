"""Pure registered Pack stage; these tests do not claim native VM boot."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator

from scripts.build_packvm_guest_bundle import build_guest_bundle
from tobkiri_protocol.saved_tools import saved_tool_logs

from ecosystem.tobkiri_conversation_orchestration_pack.runtime.messages import (
    MAX_BYTES, tobkiri_packvm_invoke,
)

ROOT = Path(__file__).resolve().parents[1]
PROVIDER = "tobkiri_conversation_orchestration_pack.messages"


def payload():
    return {"conversation": {"current_node_id": "one", "messages": [
        {"id": "one", "role": "user", "status": "complete", "content": "Hello"},
    ]}, "system_prompt": None}


def test_pure_stage_returns_detached_role_preserving_data():
    value = payload()
    value["conversation"]["messages"][0]["content"] = [{"type": "text", "text": "Hello"}]
    before = deepcopy(value)
    result = tobkiri_packvm_invoke("messages_build", value)
    assert result == {"messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}]}
    result["messages"][0]["content"][0]["text"] = "Changed"
    assert value == before


def test_prompt_digest_is_checked_but_is_not_an_authority_grant():
    value = payload()
    value["conversation"]["system_prompt_id"] = "system"
    value["system_prompt"] = {"prompt_id": "system", "body": "Instructions",
        "body_hash": "sha256:" + hashlib.sha256(b"Instructions").hexdigest()}
    assert tobkiri_packvm_invoke("messages_build", value)["messages"][0] == {
        "role": "system", "content": "Instructions",
    }
    value["system_prompt"]["body"] = "Substituted"
    with pytest.raises(ValueError, match="prompt"):
        tobkiri_packvm_invoke("messages_build", value)


@pytest.mark.parametrize("extra", ["approved", "credential_handle", "resume", "grant"])
def test_stage_rejects_undeclared_fields(extra):
    with pytest.raises(ValueError, match="fields"):
        tobkiri_packvm_invoke("messages_build", {**payload(), extra: True})


def test_stage_rejects_wrong_operation_and_invalid_metadata():
    with pytest.raises(ValueError, match="operation"):
        tobkiri_packvm_invoke("saved_complete", payload())
    value = payload()
    value["conversation"]["messages"][0]["metadata"] = ["invalid"]
    with pytest.raises(ValueError, match="messages"):
        tobkiri_packvm_invoke("messages_build", value)


@pytest.mark.parametrize("role", [[], {}, None, False])
def test_malformed_role_has_validation_error(role):
    value = payload()
    value["conversation"]["messages"][0]["role"] = role
    with pytest.raises(ValueError, match="messages"):
        tobkiri_packvm_invoke("messages_build", value)


def test_node_preserves_tool_pairs_and_rejects_incomplete_trace():
    value = payload()
    trace = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call.1", "type": "function", "function": {
                "name": "tool.test", "arguments": "{}",
            }},
        ]},
        {"role": "tool", "tool_call_id": "call.1", "content": "result"},
    ]
    message = value["conversation"]["messages"][0]
    message.update(role="assistant", metadata={"saved_tool_messages": trace}, tool_logs=saved_tool_logs(trace))
    assert tobkiri_packvm_invoke("messages_build", value)["messages"][:2] == trace
    trace.pop()
    with pytest.raises(ValueError, match="incomplete"):
        tobkiri_packvm_invoke("messages_build", value)


def test_node_bounds_inline_images_and_rejects_remote_images():
    value = payload()
    url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/axR4xUAAAAASUVORK5CYII="
    first = value["conversation"]["messages"][0]
    first["content"] = [{"type": "text", "text": "Describe"},
                        {"type": "image_url", "image_url": {"url": url}}]
    value["conversation"]["messages"] = [
        {**deepcopy(first), "id": str(i)} for i in range(3)
    ]
    value["conversation"]["current_node_id"] = "2"
    messages = tobkiri_packvm_invoke("messages_build", value)["messages"]
    assert "omitted" in messages[0]["content"][-1]["text"]
    assert messages[1]["content"] == first["content"]
    assert messages[2]["content"] == first["content"]
    value["conversation"]["messages"][-1]["content"][1]["image_url"]["url"] = "https://example.invalid/image.png"
    with pytest.raises(ValueError):
        tobkiri_packvm_invoke("messages_build", value)


def test_stage_cannot_exceed_ordinary_guest_request_budget():
    value = payload()
    value["conversation"]["messages"][0]["content"] = "x" * MAX_BYTES
    with pytest.raises(ValueError):
        tobkiri_packvm_invoke("messages_build", value)


def test_stage_is_pure_packvm_with_no_outbound_profile_edges():
    path = ROOT / "ecosystem/tobkiri_conversation_orchestration_pack/executables.v4.json"
    variants = json.loads(path.read_text())["variants"]
    variant = next(item for item in variants if item["function_id"] == PROVIDER)
    assert variant["execution_kind"] == "pack_vm"
    assert variant["backend"] == "tobkiri.python-pack-v4"
    assert variant["operations"][0]["effect_class"] == "pure"
    intent = json.loads((ROOT / "ecosystem/defaultspack/v4/defaults.profile.intent.v1.json").read_text())
    assert not any(edge["caller_function_id"] == PROVIDER for edge in intent["requested_edges"])
    incoming = [edge for edge in intent["requested_edges"] if edge["target_provider_id"] == PROVIDER]
    assert len(incoming) == 1
    assert incoming[0]["caller_function_id"] == "tobkiri.workflow.provider"
    assert incoming[0]["operation_id"] == "messages_build"


def test_runtime_result_matches_declared_data_ports():
    source = json.loads((ROOT / "schemas/pack_v4_catalog.v1.json").read_text())
    pack = next(pack for pack in source["packs"] if pack["pack_id"] == "tobkiri_conversation_orchestration_pack")
    contract = next(item for item in pack["provided_contracts"] if item["provider_id"] == PROVIDER)
    schemas = contract["schemas"]
    value = payload()
    Draft202012Validator(schemas["input"]).validate(value)
    Draft202012Validator(schemas["output"]).validate(tobkiri_packvm_invoke("messages_build", value))
    assert not Draft202012Validator(schemas["input"]).is_valid({**value, "approved": True})


def test_entrypoint_runs_with_only_guest_archive_and_stdlib(tmp_path):
    archive = tmp_path / "guest.pyz"
    archive.write_bytes(build_guest_bundle(ROOT))
    entrypoint = ROOT / "ecosystem/tobkiri_conversation_orchestration_pack/runtime/messages.py"
    probe = """
import importlib.util, json, sys
sys.path.insert(0, sys.argv[1])
spec = importlib.util.spec_from_file_location('message_stage', sys.argv[2])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
result = module.tobkiri_packvm_invoke('messages_build', json.loads(sys.stdin.read()))
assert not any(name.startswith(('core_runtime', 'tobkiri_host', 'jsonschema')) for name in sys.modules)
print(json.dumps(result))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", probe, str(archive), str(entrypoint)],
        input=json.dumps(payload()), text=True, capture_output=True, check=True, timeout=10,
    )
    assert json.loads(completed.stdout) == {"messages": [{"role": "user", "content": "Hello"}]}
