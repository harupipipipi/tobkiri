import json
from pathlib import Path


from blocks._common import error, ok
from domain.context_engine.compact_packet import build_compact_packet
from domain.hooks.dispatcher import dispatch_hook


def _pack_root():
    return Path(__file__).resolve().parents[2]


def _context_root():
    root = _pack_root() / "user_data" / "context"
    root.mkdir(parents=True, exist_ok=True)
    return root


def run(input_data, context=None):
    dispatch_hook("before_compaction", {"input": input_data, "context": context or {}})
    summary = input_data.get("summary")
    if summary is None:
        messages = input_data.get("messages", [])
        if isinstance(messages, list):
            lines = []
            for message in messages[-20:]:
                if isinstance(message, dict):
                    lines.append(str(message.get("role", "unknown")) + ": " + str(message.get("content", ""))[:500])
            summary = "\n".join(lines)
        else:
            summary = ""
    payload = build_compact_packet(
        run_id=str(input_data.get("run_id", "")),
        conversation_id=str(input_data.get("conversation_id", "")),
        goal=str(input_data.get("goal", "")),
        summary=str(summary),
        source_transcript_id=str(input_data.get("source_transcript_id", "")),
        replacement_transcript_id=str(input_data.get("replacement_transcript_id", "")),
        progress=input_data.get("progress"),
        decisions=input_data.get("decisions", []),
        constraints=input_data.get("constraints", []),
        user_preferences=input_data.get("user_preferences", []),
        changed_files=input_data.get("changed_files", []),
        tool_results=input_data.get("tool_results", []),
        terminal_results=input_data.get("terminal_results", []),
        pinned_context=input_data.get("pinned_context", []),
        dropped_context_log=input_data.get("dropped_context_log", input_data.get("dropped_context", [])),
        memory_flush_refs=input_data.get("memory_flush_refs", []),
        next_steps=input_data.get("next_steps", []),
        critical_context=input_data.get("critical_context", []),
    )
    compact_id = payload["compact_id"]
    path = _context_root() / (compact_id + ".json")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["content_ref"] = path.relative_to(_pack_root()).as_posix()
    dispatch_hook("after_compaction", payload)
    return ok(payload)
