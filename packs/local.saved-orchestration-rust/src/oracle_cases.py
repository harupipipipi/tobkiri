"""Build step-by-step observations from the unchanged saved conversation guest."""

from copy import deepcopy
import base64
import json

from ecosystem.tobkiri_conversation_orchestration_pack.runtime import saved_conversation as owner
from tobkiri_protocol.saved_task_context import VERSION as TASK_CONTEXT


cases = []
lost = {"status": "error", "error": {"code": "TIMEOUT", "message": "unknown"}}
request = {
    "turn_id": "turn-1",
    "conversation_id": "conversation-1",
    "conversation_revision": 7,
    "content": "先頭\t\n🙂\u001f",
}
conversation = {
    "id": request["conversation_id"],
    "conversation_revision": 7,
    "current_node_id": "branch-2",
    "messages": [{"id": "branch-1"}, {"id": "branch-2"}],
    "model_reference": "model-profile",
}


def capture(name, document):
    saved = deepcopy(document)
    try:
        output = owner.tobkiri_packvm_invoke("saved_complete", document)
    except (TypeError, ValueError, KeyError):
        cases.append({"name": name, "input": saved, "ok": False})
        return None
    cases.append({"name": name, "input": saved, "ok": True, "output": deepcopy(output)})
    return output


def step(name, intent, outcome):
    return capture(name, {"state": deepcopy(intent["state"]), "outcome": outcome})


def okay(value):
    return {"status": "ok", "value": value}


def read(name, intent, changes=None, **metadata):
    return step(name, intent, okay({
        "conversation": {**conversation, **(changes or {})},
        **metadata,
    }))


def acknowledge(intent, revision):
    return okay({
        "action": "message_appended",
        "conversation_revision": revision,
        "message": {
            **intent["payload"]["message"],
            "sequence": 2,
            "sequence_number": 3,
        },
    })


begin = capture("start with Unicode and controls", {"request": request})
send = read("read exact branch", begin)
ai = step("acknowledge user at a nonconsecutive revision", send, acknowledge(send, 11))
write = step("AI returns ordinary text", ai, okay({"status": "ok", "output": "回答"}))
step("assistant acknowledgement preserves owner fields", write, acknowledge(write, 13))
for phase, intent in [("read", begin), ("user", send), ("ai", ai), ("assistant", write)]:
    step(f"{phase}: lost reply", intent, lost)

read("prior user message requests reconciliation", begin, {
    "messages": [{"id": send["payload"]["message"]["id"]}],
})
read("stale conversation revision", begin, {"conversation_revision": 8})
read("agent context is unresolved", begin, {"agent_id": "agent"})
read("blank model", begin, {"model_reference": "\u001c\u3000"})
read("missing prompt digest", begin, {"system_prompt_id": "prompt"})
read("unexpected prompt digest", begin, system_prompt_digest="sha256:" + "a" * 64)
read("unrecognized delivery mode", begin, delivery_mode="other")
step("user content changed", send, okay({
    **acknowledge(send, 8)["value"],
    "message": {**send["payload"]["message"], "content": "changed"},
}))
step("malformed assistant acknowledgement", write, okay({
    **acknowledge(write, 13)["value"],
    "message": {**write["payload"]["message"], "role": "user"},
}))
step("non-advancing revision", send, acknowledge(send, 7))
step("AI error", ai, okay({"status": "denied"}))
step("invalid finish reason", ai, okay({
    "status": "ok", "output": "text", "finish_reason": "other",
}))
step("text block answer and waiting", ai, okay({
    "status": "ok",
    "output": [{"type": "text", "text": "block"}],
    "finish_reason": "waiting_user",
}))

image = base64.b64encode(b"GIF89a" + b"saved raster").decode("ascii")
rich = {
    **request,
    "content": [
        {"type": "text", "text": "画像"},
        {"type": "image_url", "image_url": {"url": "data:image/gif;base64," + image}},
    ],
    "action_approval_mode": "agent",
    "thinking_level": "xhigh",
    "task_context": {
        "version": TASK_CONTEXT,
        "source_id": "task-source",
        "conversation_id": request["conversation_id"],
        "recipient_id": "conversation:" + request["conversation_id"],
        "input_id": request["turn_id"],
        "source_revision": 0,
        "generation": 1,
        "items": [{"id": "goal-1", "kind": "goal", "body": "続ける"}],
    },
    "context_binding": {
        "version": "tobkiri.conversation-context-binding.v1",
        "profile_id": "defaults",
        "parent_conversation_id": "parent",
        "parent_revision": 4,
        "context_digest": "sha256:" + "d" * 64,
    },
}
rich_begin = capture("rich request", {"request": rich})
rich_write = read("streaming with saved prompt", rich_begin, {"system_prompt_id": "prompt"},
                  delivery_mode="incremental", system_prompt_digest="sha256:" + "a" * 64)
step("streaming target retains prompt and compact request", rich_write, acknowledge(rich_write, 8))
strategy = capture("strategy request", {"request": {**rich, "strategy_reference": "strategy"}})
strategy_write = read("strategy read", strategy, {"system_prompt_id": "prompt"},
                      system_prompt_digest="sha256:" + "a" * 64)
step("strategy target nests requirements and prompt", strategy_write,
     acknowledge(strategy_write, 8))
legacy_state = deepcopy(send["state"])
del legacy_state["ai_mode"]
capture("retained legacy state defaults to buffered", {
    "state": legacy_state, "outcome": acknowledge(send, 8),
})

with_tools = capture("explicit tool selection", {"request": {
    **request,
    "tool_selection": {
        "mode": "manual", "must_use": True, "include": [{"kind": "tool", "id": "fixture.echo"}],
    },
}})
tool_user = read("tool turn read", with_tools)
tool_ai = step("tool turn user acknowledged", tool_user, acknowledge(tool_user, 8))
step("required tool was omitted", tool_ai, okay({"status": "ok", "output": "text"}))
tools = [
    {"operation": "fixture.echo", "intent_id": "tool-a", "arguments": {"z": True, "a": "日本語"}},
    {"operation": "fixture.inspect", "intent_id": "tool-b", "arguments": {"number": 0}},
]
planned_tools = okay({
    "status": "ok", "output": "",
    "tool_intents": tools,
    "tool_definitions": {"fixture.echo": "b" * 64, "fixture.inspect": "c" * 64},
})
step("tools are denied without opt-in", ai, planned_tools)
first_tool = step("two tools queued in original order", tool_ai, planned_tools)
step("tool outcome lost", first_tool, lost)
step("tool outcome correlates its call id", first_tool, okay({
    "tool_id": "fixture.echo", "tool_call_id": "other", "content": "echo",
}))
second_tool = step("acknowledge first tool", first_tool, okay({
    "tool_id": "fixture.echo", "tool_call_id": "tool-a", "content": "echo",
}))
again = step("acknowledge second tool and resume AI", second_tool, okay({
    "tool_id": "fixture.inspect", "tool_call_id": "tool-b", "content": "info",
}))
tool_write = step("tool trace and logs for assistant", again, okay({
    "status": "ok", "output": "tools finished", "finish_reason": "stop",
}))
step("assistant confirms tool trace", tool_write, acknowledge(tool_write, 10))
step("AI cannot replay prior tool IDs", again, planned_tools)

for field, value in (
    ("profile_id", "other"), ("context_binding", {}), ("thinking_level", "unknown"),
    ("chat_references", [{"kind": "chat", "profile_id": "defaults", "id": "chat-1"}]),
    ("conversation_revision", True), ("content", " "),
):
    capture(f"reject request field {field}", {"request": {**request, field: value}})
capture("reject forged state and request combination", {"request": request, "state": send["state"]})
capture("reject late hop", {"state": {**ai["state"], "hop": 20}, "outcome": lost})
print(json.dumps(cases, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
