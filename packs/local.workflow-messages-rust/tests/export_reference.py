"""Export deterministic cases from the production Python message stage."""

from __future__ import annotations

# ruff: noqa: E402

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

RUNTIME = Path(__file__).resolve().parents[3] / "tobkiri_runtime"
sys.path.insert(0, str(RUNTIME))

from ecosystem.tobkiri_conversation_orchestration_pack.runtime.messages import (
    MAX_BYTES,
    tobkiri_packvm_invoke,
)
from tobkiri_protocol.canonical import canonical_json
from tobkiri_protocol.saved_tools import saved_tool_logs


def base() -> dict[str, Any]:
    """Create a valid one-message input."""
    return {
        "conversation": {
            "current_node_id": "one",
            "messages": [message("one", "user", "日本語\x1c\t🙂")],
        },
        "system_prompt": None,
    }


def message(
    identity: str, role: str, content: Any, **other: Any,
) -> dict[str, Any]:
    """Create one complete durable history item."""
    return {
        "id": identity, "role": role, "status": "complete", "content": content, **other,
    }


def prompt(identity: str, body: str) -> dict[str, str]:
    """Create resolved prompt DATA with a matching content hash."""
    return {
        "prompt_id": identity,
        "body": body,
        "body_hash": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
    }


def text(value: str) -> dict[str, str]:
    """Create a text content block."""
    return {"type": "text", "text": value}


def image(mime: str, data: str) -> dict[str, Any]:
    """Create one candidate inline image block."""
    return {
        "type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{data}"},
    }


def call(identity: str, name: str, args: str) -> dict[str, Any]:
    """Keep the tool argument bytes as a JSON string."""
    return {
        "id": identity, "type": "function",
        "function": {"name": name, "arguments": args},
    }


def tool(identity: str, result: str) -> dict[str, str]:
    """Create a tool result paired to its call identity."""
    return {"role": "tool", "tool_call_id": identity, "content": result}


def tool_payload() -> dict[str, Any]:
    """Create an assistant turn with grouped ordered calls and their logs."""
    trace = [
        {
            "role": "assistant", "content": [],
            "tool_calls": [
                call("call:1", "tool/first", '{ "é": -0, "漢": [true,"🙂"] }'),
                call("call:2", "tool-second", '{"limit":9007199254740991}'),
            ],
        },
        tool("call:1", '{"retain":"raw text"}'),
        tool("call:2", "second result"),
        {
            "role": "assistant", "content": [text("next")],
            "tool_calls": [call("c.3", "x", "{}")],
        },
        tool("c.3", "third result"),
    ]
    value = base()
    value["conversation"]["messages"][0] = message(
        "one", "assistant", [text("done"), text("!")],
        metadata={"saved_tool_messages": trace}, tool_logs=saved_tool_logs(trace),
    )
    return value


def timed() -> dict[str, Any]:
    """Create owner-bound timing beyond the binary timestamp precision boundary."""
    value = base()
    value["conversation"]["lifecycle"] = {
        "version": "tobkiri.conversation-lifecycle.v1",
        "state": "running",
        "completion_message_id": {"ignored": True},
        "active_user_message_id": "one",
        "resumed_completed_at_ms": 253402280000001,
        "active_user_received_at_ms": 253402283600001,
    }
    return value


def cases() -> list[dict[str, Any]]:
    """Run each case against the production entrypoint before mutating inputs."""
    results: list[dict[str, Any]] = []

    def add(name: str, value: dict[str, Any], *, code: str = "Invalid") -> None:
        value = copy.deepcopy(value)
        try:
            output = tobkiri_packvm_invoke("messages_build", value)
        except ValueError as exc:
            results.append(
                {
                    "name": name, "input": value, "output": None, "code": code,
                    # datetime's range diagnostic differs across CPython versions.
                    # The original managed PackVM exposes only a refusal here.
                    "error": None if name == "owner_timing_outside_calendar" else str(exc),
                }
            )
        else:
            results.append({"name": name, "input": value, "output": output})

    add("unicode_and_control_text", base())
    add("empty_history", {"conversation": {"messages": []}, "system_prompt": None})
    add("undeclared_outer_field", {**base(), "approved": True})
    for name, fields in [
        ("empty_history_with_current", {"messages": []}),
        ("missing_current", {"current_node_id": None}),
        ("bad_metadata", {"metadata": []}),
        ("bad_tags", {"tags": {"unexpected": True}}),
        ("inert_falsey_tags", {"tags": {}}),
        ("invalid_prompt_id", {"system_prompt_id": "bad:prompt"}),
        ("unresolved_prompt", {"system_prompt_id": "_valid.prompt"}),
        ("agent_context", {"agent_id": "agent"}),
        ("unknown_kind", {"conversation_kind": "operations"}),
        ("workspace_identity", {"metadata": {"workspace_id": "/absolute"}}),
        ("coding_missing_workspace", {
            "conversation_kind": "coding", "metadata": {"mode": "coding"},
        }),
        ("coding_mode_disagrees", {
            "conversation_kind": "coding", "metadata": {"workspace_id": "work"},
        }),
        ("forged_workspace_resolution", {
            "conversation_kind": "coding",
            "metadata": {"mode": "coding", "workspace_id": "work"},
            "workspace_resolution": {
                "workspace_id": "work", "conversation_revision": 1,
            },
        }),
        ("forged_group_resolution", {
            "group_id": "same",
            "metadata": {"groupId": "same", "group_id": "same"},
            "owned_context_resolution": {
                "profile_id": "default", "context_digest": "claimed",
            },
        }),
        ("group_conflict", {"group_id": "one", "metadata": {"groupId": "two"}}),
        ("empty_group_identity", {"group_id": ""}),
        ("benign_null_group_alias", {"metadata": {"groupId": None}}),
        ("company_tag", {"tags": ["mimo-coding-company"]}),
        ("company_profile", {
            "metadata": {"profile_id": "defaultspack.operations_company"},
        }),
        ("workspace_alias_false_still_requires_owner", {
            "metadata": {"workspaceId": False},
        }),
        ("workspace_path_empty_is_inert", {"metadata": {"workspace_path": ""}}),
        ("workspace_path_requires_owner", {"metadata": {"workspacePath": "path"}}),
        ("read_only_requires_owner", {"metadata": {"shared_read_only": True}}),
        ("number_safe_range", {"audit": [2**53 - 1, -(2**53 - 1)]}),
        ("number_outside_safe_range", {"audit": 2**53}),
        ("float_rejected", {"audit": 1.25}),
    ]:
        value = base()
        value["conversation"].update(fields)
        add(name, value)

    value = base()
    value["conversation"]["system_prompt_id"] = "_authored"
    value["system_prompt"] = prompt("_authored", "Preserve 🌱\r\n")
    add("exact_prompt", value)
    other = copy.deepcopy(value)
    other["system_prompt"] = prompt("different", "Preserve")
    add("prompt_identity_does_not_match", other)
    other = copy.deepcopy(value)
    other["system_prompt"]["body"] = "Substituted"
    add("prompt_body_does_not_match_hash", other)
    other = copy.deepcopy(value)
    other["system_prompt"] = prompt("_authored", "")
    add("empty_resolved_prompt", other)

    for size in (60 * 1024, 60 * 1024 + 1):
        value = base()
        value["conversation"]["system_prompt_id"] = "_authored"
        size_left = size - len(canonical_json(prompt("_authored", "\x01")))
        value["system_prompt"] = prompt("_authored", "x" * size_left + "\x01")
        assert len(canonical_json(value["system_prompt"])) == size
        add("prompt_exact_limit" if size == 60 * 1024 else "prompt_over_limit", value)

    for depth in (32, 33):
        value = base()
        item: Any = None
        for _ in range(depth - 2):
            item = [item]
        value["conversation"]["audit"] = item
        add("depth_exact_limit" if depth == 32 else "depth_over_limit", value)

    for size in (MAX_BYTES, MAX_BYTES + 1):
        value = base()
        value["conversation"]["audit"] = ""
        value["conversation"]["audit"] = "x" * (size - len(canonical_json(value)))
        assert len(canonical_json(value)) == size
        add("input_exact_limit" if size == MAX_BYTES else "input_over_limit", value,
            code="LimitExceeded")

    # Minimum-size JPEGs cost less to store than the omission marker. This
    # collection crosses the output cap while remaining under the input cap.
    history = [
        message(str(index), "user", [text("x"), image("jpeg", "/9j/")])
        for index in range(2900)
    ]
    value = {
        "conversation": {"messages": history, "current_node_id": "2899"},
        "system_prompt": None,
    }
    output = tobkiri_packvm_invoke("messages_build", value)
    spare = MAX_BYTES - len(canonical_json(output))
    assert spare > 0
    last_text = history[-1]["content"][0]
    last_text["text"] += "x" * spare
    assert len(canonical_json(value)) < MAX_BYTES
    add("output_exact_limit", value)
    assert len(canonical_json(results[-1]["output"])) == MAX_BYTES
    last_text["text"] += "x"
    add("output_over_limit", value, code="LimitExceeded")

    # Maximize the number of parsed in-budget scalar nodes using one-digit numbers.
    value = base()
    value["conversation"]["ignored_owner_data"] = []
    dense_count = (MAX_BYTES - len(canonical_json(value))) // 2
    value["conversation"]["ignored_owner_data"] = [0] * dense_count
    add("dense_input_near_limit", value)

    value = timed()
    add("owner_timing_microseconds", value)
    for name, field in [
        ("owner_timing_below_threshold", {
            "active_user_received_at_ms": 253402283600000,
        }),
        ("owner_timing_identity_missing", {"active_user_message_id": "missing"}),
        ("owner_timing_boolean_is_not_integer", {"resumed_completed_at_ms": True}),
        ("owner_timing_outside_calendar", {
            "active_user_received_at_ms": 9007199254740991,
        }),
    ]:
        value = timed()
        value["conversation"]["lifecycle"].update(field)
        add(name, value)
    value = timed()
    value["conversation"]["messages"].insert(0, message("branch", "user", "off branch"))
    value["conversation"]["lifecycle"]["active_user_message_id"] = "branch"
    value["conversation"]["messages"][1]["parent_id"] = None
    value["conversation"]["messages"].append(
        message("tail", "assistant", "other", parent_id="branch")
    )
    add("owner_timing_checks_full_history", value)

    value = base()
    history = value["conversation"]["messages"]
    history.append(message("offbranch", "opaque", {"ignored": 42}))
    add("flat_selection_uses_current_boundary", value)
    history[1]["id"] = "one"
    add("offbranch_duplicate_id_rejected", value)
    value = base()
    value["conversation"]["messages"][0]["parent_id"] = "one"
    add("cycle_rejected", value)
    value["conversation"]["messages"][0]["parent_id"] = "missing"
    add("missing_parent_rejected", value)
    value["conversation"]["messages"][0]["parent_id"] = 0
    add("invalid_parent_rejected", value)

    value = base()
    owner = value["conversation"]["messages"][0]
    owner.update(parts=[], widget=False)
    owner["content"] = "x" * (60 * 1024 + 1)
    add("plain_text_exceeds_image_text_limit", value)
    owner["content"] = ""
    add("empty_plain_text", value)
    owner["content"] = []
    add("empty_owner_text_blocks", value)
    owner["content"] = [text("")]
    add("empty_string_in_text_block", value)
    owner["parts"] = [{"type": "text"}]
    add("structured_parts_require_resolution", value)
    owner["metadata"] = []
    add("invalid_message_metadata_precedes_content", value)

    value = tool_payload()
    add("ordered_multipair_transcript", value)
    owner = value["conversation"]["messages"][0]
    owner["role"] = "user"
    add("transcript_rejected_on_user", value)
    owner["role"] = "assistant"
    owner["tool_logs"][0]["arguments"]["漢"][0] = 1
    add("tool_log_distinguishes_boolean_and_integer", value)
    trace = owner["metadata"]["saved_tool_messages"]
    trace[1], trace[2] = trace[2], trace[1]
    add("tool_result_order", value)
    trace[1], trace[2] = trace[2], trace[1]
    trace.pop()
    add("incomplete_transcript", value)
    trace[3]["tool_calls"][0]["id"] = "call:1"
    add("duplicate_tool_call_id", value)

    for name, args in [
        ("tool_arguments_must_be_object", "[]"),
        ("tool_arguments_reject_float", '{"i": 1.0}'),
        ("tool_arguments_reject_unsafe_integer", '{"i":9007199254740992}'),
        (
            "tool_arguments_no_more_than_64_levels",
            '{"i":' + "[" * 64 + "null" + "]" * 64 + "}",
        ),
    ]:
        value = tool_payload()
        owner = value["conversation"]["messages"][0]
        trace = owner["metadata"]["saved_tool_messages"]
        trace[0]["tool_calls"][0]["function"]["arguments"] = args
        add(name, value)

    for size in (16 * 1024, 16 * 1024 + 1):
        value = tool_payload()
        owner = value["conversation"]["messages"][0]
        args = '{"i":"' + "x" * (size - 8) + '"}'
        assert len(args) == size
        trace = owner["metadata"]["saved_tool_messages"]
        trace[0]["tool_calls"][0]["function"]["arguments"] = args
        if size == 16 * 1024:
            owner["tool_logs"] = saved_tool_logs(trace)
        name = "tool_arguments_exact_limit"
        if size > 16 * 1024:
            name = "tool_arguments_over_limit"
        add(name, value, code="LimitExceeded")

    for size in (40 * 1024, 40 * 1024 + 1):
        value = tool_payload()
        owner = value["conversation"]["messages"][0]
        trace = owner["metadata"]["saved_tool_messages"]
        trace[1]["content"] = ""
        trace[1]["content"] = "x" * (size - len(canonical_json(trace)))
        assert len(canonical_json(trace)) == size
        if size == 40 * 1024:
            owner["tool_logs"] = saved_tool_logs(trace)
        add("tool_trace_exact_limit" if size == 40 * 1024 else "tool_trace_over_limit",
            value, code="LimitExceeded")

    value = tool_payload()
    conversation = value["conversation"]
    conversation["system_prompt_id"] = "_context"
    value["system_prompt"] = prompt("_context", "Prompt")
    owner = conversation["messages"][0]
    owner["parent_id"] = "newest"
    conversation["messages"] = [
        owner,
        message("old", "user", [text("old"), image("png", "iVBORw0KGgo=")]),
        message("middle", "user", [
            text("middle"),
            image("jpeg", "/9j/"),
            image("gif", "R0lGODlh"),
        ], parent_id="old"),
        message(
            "bad-offbranch", "tool", {"no": "owner resolution"}, parent_id="missing",
        ),
        message("newest", "user", [
            text("newest"), image("webp", "UklGRgAAAABXRUJQ"),
        ], parent_id="middle"),
    ]
    add("rich_selected_history", value)
    newest = conversation["messages"][4]["content"]
    newest[1]["image_url"]["url"] = "https://example.invalid/image.png"
    add("remote_image_requires_resolution", value)
    newest[1] = image("png", "/9j/")
    add("wrong_raster_magic", value)
    newest[1] = image("png", "iVBORw0KGgp=")
    add("image_unused_base64_tail_bits", value)
    newest[0] = text("\u001f")
    add("image_caption_python_blank", value)
    return results


def main() -> None:
    """Write expected production results as compact JSON."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    encoded = json.dumps(
        cases(), ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    if args.output is None:
        print(encoded)
    else:
        args.output.write_text(encoded + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
