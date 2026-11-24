"""Pure message-stage behavior remains identical to the saved bridge boundary."""

from copy import deepcopy

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.bootstrap.saved_bridge import _messages
from tobkiri_protocol.saved_messages import (
    SavedMessageContextError,
    build_saved_model_messages,
)
from tobkiri_protocol.saved_tools import saved_tool_logs


def message(identifier, content, *, parent=None, role="user"):
    return {
        "id": identifier, "parent_id": parent, "role": role,
        "content": content, "status": "complete",
    }


def test_selected_branch_and_prompt_are_preserved_without_mutating_owner():
    conversation = {
        "system_prompt_id": "system",
        "current_node_id": "chosen",
        "messages": [
            message("root", "Question"),
            message("other", "Other answer", parent="root", role="assistant"),
            message("chosen", [{"type": "text", "text": "Chosen answer"}],
                    parent="root", role="assistant"),
        ],
    }
    before = deepcopy(conversation)
    prompt = {"prompt_id": "system", "body": "Instructions"}
    expected = [
        {"role": "system", "content": "Instructions"},
        {"role": "user", "content": "Question"},
        {"role": "assistant", "content": [{"type": "text", "text": "Chosen answer"}]},
    ]
    assert build_saved_model_messages(conversation, system_prompt=prompt) == expected
    assert _messages(conversation, system_prompt=prompt) == expected
    assert build_saved_model_messages(
        conversation, system_prompt=prompt, flatten_text_blocks=True,
    )[-1] == {"role": "assistant", "content": "Chosen answer"}
    assert conversation == before


def test_flat_legacy_history_stops_at_selected_node():
    conversation = {"current_node_id": "first", "messages": [
        message("first", "One"), message("later", "Do not include"),
    ]}
    assert build_saved_model_messages(conversation) == [{"role": "user", "content": "One"}]


@pytest.mark.parametrize("patch", [
    {"current_node_id": "missing"},
    {"system_prompt_id": "unresolved"},
    {"agent_id": "unresolved-agent"},
    {"messages": [message("first", "One", parent="first")]},
    {"messages": [message("first", "One", parent="missing")]},
    {"messages": [message("first", "One"), message("first", "Duplicate")]},
    {"messages": [{**message("first", "One"), "status": "streaming"}]},
    {"messages": [{**message("first", "One"), "widget": {"id": "unresolved"}}]},
])
def test_unresolved_context_has_pure_error_and_legacy_authority_error(patch):
    conversation = {"current_node_id": "first", "messages": [message("first", "One")], **patch}
    with pytest.raises(SavedMessageContextError):
        build_saved_model_messages(conversation)
    with pytest.raises(AuthorityDenied):
        _messages(conversation)


def test_complete_tool_trace_is_preserved_and_unmatched_logs_are_rejected():
    trace = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call-1", "type": "function", "function": {
                "name": "search", "arguments": '{"query":"test"}',
            }},
        ]},
        {"role": "tool", "tool_call_id": "call-1", "content": "Found"},
    ]
    reply = {**message("reply", "Answer", role="assistant"),
             "metadata": {"saved_tool_messages": trace}, "tool_logs": saved_tool_logs(trace)}
    conversation = {"current_node_id": "reply", "messages": [reply]}
    assert build_saved_model_messages(conversation) == [
        *trace, {"role": "assistant", "content": "Answer"},
    ]
    reply["tool_logs"] = []
    with pytest.raises(SavedMessageContextError, match="tool transcript"):
        build_saved_model_messages(conversation)


def test_image_history_bounds_only_older_images_without_mutating_context():
    url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/axR4xUAAAAASUVORK5CYII="
    content = [{"type": "text", "text": "Describe"},
               {"type": "image_url", "image_url": {"url": url}}]
    conversation = {"current_node_id": "third", "messages": [
        message("first", deepcopy(content)),
        message("second", deepcopy(content), parent="first"),
        message("third", deepcopy(content), parent="second"),
    ]}
    before = deepcopy(conversation)
    assembled = build_saved_model_messages(conversation)
    assert all(block["type"] == "text" for block in assembled[0]["content"])
    assert "omitted" in assembled[0]["content"][-1]["text"]
    assert assembled[1]["content"] == content
    assert assembled[2]["content"] == content
    assert conversation == before
