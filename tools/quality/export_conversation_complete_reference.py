"""Record bounded inputs and outputs of the original signed Conversation guest."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
import hashlib
import json
from pathlib import Path
import runpy


SOURCE = Path(__file__).resolve().parents[2] / (
    "tobkiri_runtime/ecosystem/defaultspack/runtime/conversation.py"
)
ORIGINAL_SHA256 = "28f40236e26209bc3360cd59618291f5b98be5354ea70d0f39d2dbbaef86d793"
MODEL = {"role": "user", "content": "こんにちは 👋"}


def prepared_inputs() -> Iterator[tuple[str, object]]:
    """Cover accepted requests and the original bridge limits."""
    yield "fixed surface overrides outer metadata", {
        "messages": [MODEL],
        "model": "local/モデル",
        "profile_id": "a different profile",
        "requirements": {"request_surface": "unknown"},
        "tools": ["none"],
    }
    yield "model omitted", {"messages": [MODEL, {"role": "system", "content": ""}]}
    yield "nested content", {"messages": [{
        "role": "user", "content": [{"type": "text", "text": "first"}, None, True],
    }]}
    yield "outer null", None
    yield "missing messages", {}
    yield "empty messages", {"messages": []}
    yield "message must be an object", {"messages": ["text"]}
    yield "too many messages", {"messages": [{}] * 65}
    yield "both phases", {"messages": [MODEL], "continuation": None}
    yield "outer bridge result", {"messages": [MODEL], "bridge_result": {}}
    for name, model in (
        ("empty", ""), ("null", None), ("numeric", 1),
        ("whitespace", "モデル\u3000"),
        ("control", "model\x1fref"), ("DEL", "a\x7fb"), ("exact", "猫" * 256),
        ("too long", "猫" * 257),
    ):
        yield f"model {name}", {"messages": [MODEL], "model": model}
    for name, value in (
        ("positive safe integer", 2**53 - 1), ("unsafe positive integer", 2**53),
        ("negative safe integer", -2**53 + 1), ("unsafe negative integer", -2**53),
        ("object width", {str(index): None for index in range(64)}),
        ("excess object width", {str(index): None for index in range(65)}),
        ("array width", [None] * 64), ("excess array width", [None] * 65),
        ("depth", [[[[[[None]]]]]]), ("excess depth", [[[[[[[None]]]]]]]),
        ("maximum key", {"x" * 128: True}), ("oversize key", {"x" * 129: True}),
        ("empty key", {"": True}), ("maximum scalar", "a" * (16 * 1024)),
        ("oversize scalar", "a" * (16 * 1024 + 1)),
        ("four-byte Unicode", "😀" * 16_384),
    ):
        yield name, {"messages": [{"value": value}]}
    for name, size in (
        ("exact request budget", 65_536), ("request exceeds budget", 65_537),
    ):
        messages = [{"content": "a" * 16_384} for _ in range(3)] + [{"content": ""}]
        request = {
            "messages": messages,
            "requirements": {"request_surface": "defaultspack.conversation"},
        }
        base_bytes = len(json.dumps(request, separators=(",", ":")).encode())
        messages[-1]["content"] = "a" * (size - base_bytes)
        yield name, {"messages": messages}


def finished_inputs() -> Iterator[tuple[str, dict[str, object]]]:
    """Exercise the original completion projector and typed bridge errors."""
    for name, value in (
        ("empty", {}), ("text", {"output": "こんにちは", "extra": 3}),
        ("boolean output", {"output": False}), ("integer output", {"output": 7}),
        ("object output", {"output": {"type": "text", "text": "ignored"}}),
        ("blocks", {"output": [
            {"type": "text", "text": "preserved"}, 1, "drop", None,
        ]}),
        ("tools", {"output": "call", "tool_intents": [
            {"intent_id": "tool-1", "operation": "look-up", "arguments": {"語": 1}},
            12,
            {"intent_id": 4, "operation": False, "arguments": [["x", 1], ["x", 2]]},
            {},
        ]}),
        ("string intents", {"tool_intents": "empty after filtering"}),
        ("object intents", {"tool_intents": {"key": 1}}),
        ("invalid intents", {"tool_intents": True}),
        ("invalid arguments", {"tool_intents": [{"arguments": "invalid"}]}),
        ("canonical key order in tool display", {"tool_intents": [{
            "intent_id": {"z": 1, "a": [True, None]},
            "arguments": [{"z": "ignored", "a": "ignored"}],
        }]}),
        ("empty output", {"output": "", "tool_intents": []}),
        ("large result", {"output": ["a" * 16_384] * 31}),
        ("oversize result", {"output": ["a" * 16_384] * 32}),
    ):
        yield name, {"status": "ok", "value": value}
    yield "missing outcome", {}
    yield "extra outcome fields", {"status": "ok", "value": {}, "other": False}
    yield "value is not an object", {"status": "ok", "value": []}
    yield "typed error", {
        "status": "error", "error": {"code": "PROVIDER_FAILED", "message": "失敗"},
    }
    for name, code, message in (
        ("lowercase", "unknown", "cause"), ("blank message", "FAILED", "\u3000"),
        ("maximum error code", "A" * 64, "x" * 512), ("large code", "A" * 65, "x"),
        ("large message", "FAILED", "失" * 513),
    ):
        yield name, {"status": "error", "error": {"code": code, "message": message}}


def export_reference(source: Path, output: Path) -> None:
    """Run the sealed two-call guest and retain its request and reply projection."""
    if hashlib.sha256(source.read_bytes()).hexdigest() != ORIGINAL_SHA256:
        raise ValueError("Conversation reference is not the exact signed source")
    module = runpy.run_path(str(source))
    invoke = module["tobkiri_packvm_invoke"]
    digest = module["_canonical_digest"]
    cases = []
    for name, payload in prepared_inputs():
        try:
            expected = invoke("complete", payload)["request"]
        except (TypeError, ValueError):
            expected = None
        cases.append({
            "operation": "prepare", "name": name, "input": payload, "output": expected,
        })
    for name, outcome in finished_inputs():
        # The authenticated Host sends a canonical JSON frame. Its decoded
        # mappings also determine the order of the original Python repr.
        outcome = json.loads(module["_canonical_json"](outcome))
        prepare = invoke("complete", {"messages": [MODEL]})
        continuation = prepare["continuation"]
        reply = {
            **continuation,
            "kind": module["PACKVM_BRIDGE_RESULT_KIND"],
            "result": outcome,
            "result_digest": digest(outcome),
        }
        try:
            expected = invoke(
                "complete", {"continuation": continuation, "bridge_result": reply},
            )
        except (TypeError, ValueError):
            expected = None
        cases.append({
            "operation": "finish", "name": name, "input": outcome, "output": expected,
        })
    output.write_text(
        json.dumps(cases, ensure_ascii=False, separators=(",", ":")), encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    export_reference(arguments.source, arguments.output)
