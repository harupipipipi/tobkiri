"""
blocks/chat/_prompt_helpers.py - Shared prompt building and response parsing utilities.

Consolidates the following previously duplicated helpers:
- extract_text(): response dict -> plain text
- build_text_from_content(): content field (str | list) -> plain text
- build_summarize_prompt(): build messages for conversation summarization
- build_analysis_prompt(): build messages for AI analysis of compactable segments
- resolve_conversation_system_prompt(): resolve system_prompt_id to text
- build_summarizer_system_prompt(): build the summarizer persona text
- build_content_classifier_prompt(): build classifier prompts (consent / disclaimer)
"""

import re
from pathlib import Path


_PROMPT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def extract_text(response):
    """Extract plain text from an AI response dict.

    Handles the common StandardResponse shapes:
    - {"data": {...}} wrapper
    - {"content": "string"}
    - {"content": [{"type": "text", "text": "..."}, ...]}
    Returns "" for empty / non-dict inputs.
    """
    if isinstance(response, dict) and "data" in response and isinstance(response["data"], dict):
        response = response["data"]
    if not isinstance(response, dict):
        return str(response or "")
    content = response.get("content", [])
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def build_text_from_content(content):
    """Extract plain text from a content field (str or list of blocks).

    Recognised block types: "text", "tool_call", "tool_result".
    Unknown dict blocks fall back to .get("text") or str().
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type", "text")
                if btype == "text":
                    parts.append(block.get("text", ""))
                elif btype == "tool_call":
                    parts.append("[tool_call: " + str(block.get("name", "unknown")) + "]")
                elif btype == "tool_result":
                    tc = block.get("content", "")
                    parts.append(tc if isinstance(tc, str) else str(tc))
                else:
                    parts.append(block.get("text", str(block)))
            elif isinstance(block, str):
                parts.append(block)
            else:
                parts.append(str(block))
        return "\n".join(t for t in parts if t)
    return str(content)


def _format_conversation_lines(standard_messages):
    """Format standard messages as [role]: content lines."""
    parts = []
    for msg in standard_messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if content:
            parts.append("[" + role + "]: " + content)
    return "\n".join(parts)


_SUMMARIZER_BASE = (
    "You are a conversation summarizer. "
    "You will be given a segment of conversation messages. "
    "Produce a concise summary that preserves all important decisions, results, "
    "conclusions, tool outcomes, file paths, IDs, and unresolved follow-ups. "
    "Discard intermediate work, debug output, and verbose logs. "
    "Output ONLY the summary text, no extra formatting or preamble."
)


def build_summarizer_system_prompt(persona="summarizer", extra_instruction=None):
    """Build a summarizer-style system prompt with a configurable persona.

    Personas:
    - "summarizer": neutral summarizer (default)
    - "editor": emphasises "discard intermediate work"
    - "compactor": emphasises "compact older chat range"
    - "compaction_analyst": identifies segments to compact
    - "trim_analyst": identifies segments to trim
    - "context": standalone context summariser
    - "subagent": utility subagent summariser
    """
    personas = {
        "summarizer": _SUMMARIZER_BASE,
        "editor": (
            "You are a conversation editor. "
            "You will be given a segment of conversation messages. "
            "Create a concise summary that preserves all important decisions, "
            "results, conclusions, and actionable information. "
            "Discard intermediate work, debug output, and verbose logs. "
            "Output ONLY the summary text, no extra formatting or preamble."
        ),
        "compactor": (
            "You are a conversation compactor. Summarize an older chat range into "
            "a compact assistant message. Preserve decisions, requirements, facts, "
            "tool outcomes, file paths, IDs, and unresolved follow-ups. Omit routine "
            "back-and-forth and verbose intermediate logs. Output only the summary."
        ),
        "compaction_analyst": (
            "You are a conversation compaction analyst. Analyze the conversation and "
            "identify segments of messages that can be replaced by a brief summary "
            "without losing important information.\n\n"
            "Good candidates for compaction:\n"
            "- Intermediate experiment/work logs where only the conclusion matters\n"
            "- Step-by-step debug output that led to a fix\n"
            "- Repetitive trial-and-error sequences\n"
            "- Verbose tool outputs superseded by later summaries\n\n"
            "Do NOT compact:\n"
            "- The initial user request or problem statement\n"
            "- Final results, conclusions, or decisions\n"
            "- Important turning points in the conversation\n"
            "- The most recent 2 messages (they are still active context)"
        ),
        "trim_analyst": (
            "You are a conversation analyst. Analyze the following conversation and identify "
            "segments of intermediate/verbose messages that can be summarized without losing "
            "important information. Focus on:\n"
            "- Intermediate work logs or step-by-step outputs that have a final summary\n"
            "- Repetitive trial-and-error messages where only the conclusion matters\n"
            "- Debug outputs or verbose logs\n"
            "- Messages that are superseded by later corrections\n\n"
            "Do NOT suggest trimming:\n"
            "- The initial user request\n"
            "- Final results or conclusions\n"
            "- Important decisions or turning points\n"
            "- Messages the user explicitly asked to keep"
        ),
        "context": (
            "You are a context summarizer. Produce a concise summary of the given "
            "conversation that preserves all important information for downstream agents. "
            "Output ONLY the summary text."
        ),
        "subagent": (
            "You are a utility subagent that produces concise summaries of the given "
            "context for a parent agent. Preserve key facts, IDs, and decisions. "
            "Output ONLY the summary text."
        ),
    }
    text = personas.get(persona, _SUMMARIZER_BASE)
    if extra_instruction:
        text += "\n\nAdditional instruction: " + str(extra_instruction)
    return text


def build_summarize_prompt(standard_messages, instruction=None,
                          persona="summarizer", user_prefix=None):
    """Build messages list for summarization tasks.

    Args:
        standard_messages: list of {role, content} dicts
        instruction: optional extra instruction appended to system prompt
        persona: one of build_summarizer_system_prompt personas
        user_prefix: optional override for the user message prefix
    """
    system_text = build_summarizer_system_prompt(persona=persona, extra_instruction=instruction)
    conversation_text = _format_conversation_lines(standard_messages)
    if user_prefix is None:
        user_prefix = "Please summarize the following conversation segment:"
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_prefix + "\n\n" + conversation_text},
    ]


def build_segment_summary_prompt(standard_messages, reason, summary_preview):
    """Build messages list for summarising a single identified segment."""
    system_text = build_summarizer_system_prompt(persona="editor")
    system_text += (
        "\nCompaction reason: " + str(reason) +
        "\nExpected summary direction: " + str(summary_preview) +
        "\n\nOutput ONLY the summary text."
    )
    conversation_text = _format_conversation_lines(standard_messages)
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": "Summarize:\n\n" + conversation_text},
    ]


def build_analysis_prompt(messages_with_ids, max_context_tokens=None,
                         persona="compaction_analyst", truncate_at=300):
    """Build messages list for AI-driven segment analysis.

    Args:
        messages_with_ids: list of {id, role, content} dicts
        max_context_tokens: optional budget hint injected into system prompt
        persona: "compaction_analyst" (default) or "trim_analyst"
        truncate_at: truncate each message body in the user payload
    """
    system_text = build_summarizer_system_prompt(persona=persona)
    if max_context_tokens is not None:
        verb = "compacted" if persona == "compaction_analyst" else "trimmed"
        system_text += (
            "\n\nThe conversation should ideally fit within "
            + str(max_context_tokens)
            + " tokens after " + verb + "."
        )
    system_text += (
        "\n\nRespond with a JSON array of segments. Each segment:\n"
        '{"start_id": "<message_id>", "end_id": "<message_id>", '
        '"reason": "<why>", "summary_preview": "<what the summary would say>"}\n\n'
        "If nothing should be processed, respond with: []\n"
        "Output ONLY the JSON array."
    )

    lines = []
    for entry in messages_with_ids:
        msg_id = entry["id"]
        role = entry["role"]
        text = entry["content"] or "(empty)"
        if isinstance(text, str) and len(text) > truncate_at:
            text = text[:truncate_at] + "..."
        lines.append("[ID: " + str(msg_id) + "] [" + role + "]: " + text)

    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": "Analyze this conversation:\n\n" + "\n".join(lines)},
    ]


def resolve_conversation_system_prompt(conv, manager, prompts_dir=None):
    """Resolve a conversation's system_prompt_id to the actual prompt text.

    Lookup order:
    1. Manager registry (get_prompt / get_prompt_by_name)
    2. <prompts_dir>/<prompt_id>.system.md file
    3. manager.get_system_prompt() default fallback

    Args:
        conv: conversation dict (may be None)
        manager: prompt manager instance
        prompts_dir: optional Path override for the markdown prompts directory
    """
    prompt_id = str((conv or {}).get("system_prompt_id") or "").strip()
    if not prompt_id:
        return manager.get_system_prompt()
    prompt = manager.get_prompt(prompt_id) or manager.get_prompt_by_name(prompt_id)
    if isinstance(prompt, dict):
        body = prompt.get("body") or prompt.get("content")
        if body:
            return str(body)
    if _PROMPT_ID_RE.match(prompt_id):
        base = prompts_dir or (Path(__file__).resolve().parents[2] / "prompts")
        prompt_path = base / (prompt_id + ".system.md")
        try:
            if prompt_path.is_file():
                return prompt_path.read_text(encoding="utf-8")
        except OSError:
            pass
    return manager.get_system_prompt()


def build_content_classifier_prompt(categories, field_name="categories", scope="consent"):
    """Build the AI content-classifier system prompt.

    Args:
        categories: list of category names to detect
        field_name: "categories" (consent) or "detected" (disclaimer)
        scope: "consent" or "disclaimer" (only affects framing)
    """
    scope_intro = (
        "Analyze the given text and determine if it contains sensitive advice in any of these categories: "
        if scope == "consent" else
        "Analyze the given text and determine if it contains advice or information that requires a legal disclaimer in any of these categories. "
    )
    cats_text = ", ".join(categories) if categories else "(no categories configured)"
    return (
        "You are a content classifier. " + scope_intro + cats_text + ".\n"
        "Respond with ONLY a JSON object: "
        '{"' + field_name + '": ["name1", ...], "confidence": 0.0-1.0}\n'
        'If no matching content is found, respond: '
        '{"' + field_name + '": [], "confidence": 1.0}'
    )
