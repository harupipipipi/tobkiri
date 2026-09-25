from __future__ import annotations

import json
import re
import time
import uuid
from copy import deepcopy
from typing import Any


RUMI_MODEL_PACK_ID = "rumi"
RUMI_MODEL_PACK_REF = "modelpack/rumi"
# This is a provider/profile identifier, not a model artifact bundled with Rumi.
# Runtime materialization prefers the configured defaults profile base model.
RUMI_INTENDED_BASE_MODEL = "xiaomi-token-plan-sgp/mimo-v2.5-pro"
# Backward-compatible alias for integrations that imported the old name.
RUMI_BASE_MODEL = RUMI_INTENDED_BASE_MODEL
RUMI_DISPLAY_NAME = "Rumi"
RUMI_PROCESS_VERSION = "2026-06-04"
RUMI_DEFAULT_THINKING_LEVEL = "medium"
RUMI_DEEPTHINK_WARNING = "DeepThink is enabled. This task may take several hours."
RUMI_DEEPTHINK_WARNING_JA = "DeepThinkが有効です。タスクには数時間かかる可能性があります。"
RUMI_DEEPTHINK_SOURCE = "harupipipipi/thinker"
RUMI_DEEPTHINK_MAX_SECTIONS = 3
RUMI_USER_BACKGROUND_CONSTRAINT = (
    "Do not infer or record sensitive personal attributes (for example health, "
    "religion, political beliefs, ethnicity, sexual orientation, precise "
    "location, or finances) or unverifiable private facts; keep every user "
    "hypothesis task-relevant and evidence-backed."
)
RUMI_QUARANTINE_MESSAGE = (
    "Rumi quarantined this draft before delivery because the review chain could not verify a marked final response."
)
RUMI_BASE_MODEL_CANDIDATES = [
    RUMI_INTENDED_BASE_MODEL,
    "anthropic/claude-sonnet-4-0",
    "openai/gpt-4o",
    "google/gemini-2.5-flash",
]

_ACTION_TOOL_IDS = {
    "todo",
    "web_search",
    "coding_terminal_exec",
    "coding_file_create",
    "coding_file_write",
    "coding_file_patch",
    "coding_git_commit",
    "coding_git_push",
    "browser_use",
    "browser_computer",
    "html_preview",
    "image_render",
}


RUMI_CRITERIA = [
    "Infer the user's background, intent, level, and true goal before drafting.",
    "Treat coding and clearly specified tasks as executable work; keep casual, ambiguous, or advisory tasks more diagnostic.",
    "For large tasks, produce five todo-plan candidates and let a reviewer choose before execution.",
    "Force freshness checks for proper nouns and current facts when search is available; attach source summaries with retrieval time.",
    "Before external actions, state known facts and assumptions; ambiguous phrases such as 'same as last time' are blockers.",
    "Log trace ids for model input/output, tool calls/results, retries, and reviewer decisions so a run can be replayed.",
    "Assume tools can fail with wrong tool, wrong args, partial data, timeout, or permission denied.",
    "Use simple mode for small tasks and deep mode for larger tasks with todo, subtask, review, and watchdog loops.",
    "Escalate on low confidence to search, another AI, a stronger model, or human confirmation.",
    "For UI tasks, render visual artifacts and review screenshots, zoomed regions, spacing, layout, and visual discomfort.",
]


def _json_only() -> str:
    return "Return only valid JSON. No markdown."


def resolve_rumi_base_model(
    available_models: Any = None,
    *,
    available_providers: Any = None,
    default_profile_base_model: str | None = None,
) -> str:
    model_ids = {
        str(item or "").strip()
        for item in (available_models if isinstance(available_models, (list, tuple, set)) else [])
        if str(item or "").strip()
    }
    provider_ids = {
        str(item or "").strip()
        for item in (available_providers if isinstance(available_providers, (list, tuple, set)) else [])
        if str(item or "").strip()
    }
    default_base = str(default_profile_base_model or "").strip()
    reserved_providers = {"stub", "rumi", "modelpack", "composite", "synthetic"}
    default_provider, _, _ = default_base.partition("/")
    if (
        default_base
        and default_base in model_ids
        and default_provider.lower() not in reserved_providers
    ):
        return default_base
    for candidate in RUMI_BASE_MODEL_CANDIDATES:
        if candidate in model_ids:
            return candidate
    for candidate in RUMI_BASE_MODEL_CANDIDATES:
        provider_id, _, _ = candidate.partition("/")
        if provider_id and provider_id in provider_ids:
            return candidate
    return RUMI_INTENDED_BASE_MODEL


def rumi_base_model_metadata(resolved_base_model: str | None = None) -> dict[str, Any]:
    resolved = str(resolved_base_model or RUMI_INTENDED_BASE_MODEL).strip() or RUMI_INTENDED_BASE_MODEL
    fallback_reason = ""
    if resolved != RUMI_INTENDED_BASE_MODEL:
        fallback_reason = "intended_base_model_unavailable_using_active_provider_fallback"
    return {
        "intended_base_model": RUMI_INTENDED_BASE_MODEL,
        "resolved_base_model": resolved,
        "fallback_reason": fallback_reason,
    }


def default_rumi_model_pack(*, base_model: str | None = None) -> dict[str, Any]:
    resolved_base_model = str(base_model or RUMI_INTENDED_BASE_MODEL).strip() or RUMI_INTENDED_BASE_MODEL
    base_model_metadata = rumi_base_model_metadata(resolved_base_model)
    return {
        "id": RUMI_MODEL_PACK_ID,
        "display_name": RUMI_DISPLAY_NAME,
        "mode": "review_chain",
        "members": [
            {
                "model": resolved_base_model,
                "label": "Rumi generator",
                "fallback_on": ["rate_limit", "quota", "provider_error", "timeout"],
                "metadata": {
                    "role": "generator",
                    "thinking_level": RUMI_DEFAULT_THINKING_LEVEL,
                    "purpose": "intent, background, explicit reasoning brief, and draft generation",
                },
            },
            {
                "model": resolved_base_model,
                "label": "Rumi reviewer",
                "fallback_on": ["rate_limit", "quota", "provider_error", "timeout"],
                "metadata": {
                    "role": "reviewer",
                    "thinking_level": RUMI_DEFAULT_THINKING_LEVEL,
                    "receives_personalization": False,
                    "purpose": "bias-reduced review of draft, freshness, confidence, tool safety, and process fit",
                },
            },
        ],
        "budget": {
            "simple_max_steps": 1,
            "deep_max_steps": 8,
            "deepthink_max_review_iterations": 8,
            "deepthink_user_rejection_review_cycles": 2,
            "deepthink_max_sections": RUMI_DEEPTHINK_MAX_SECTIONS,
            "max_review_rounds": 2,
            "max_retries": 2,
            "timeout_seconds": 600,
        },
        "safety": {
            "confidence_threshold": 0.72,
            "quarantine_on_watchdog": True,
            "pre_action_assumption_block_required": True,
            "reviewer_context_excludes_personalization": True,
        },
        "metadata": {
            "builtin": True,
            "process_version": RUMI_PROCESS_VERSION,
            "base_model": resolved_base_model,
            **base_model_metadata,
            "model_limit_summary": {
                "positioning": "MiMo V2.5 Pro is capable but not a frontier ceiling.",
                "known_stronger_model_classes": ["Claude Sonnet", "Claude Opus", "GPT"],
                "default_behavior": "escalate when confidence or freshness is below threshold",
            },
            "durable_assets": ["workflow", "trace", "eval", "tool_schema", "todo_structure"],
            "tooling": {
                "todo_tool": "todo",
                "search_tool": "web_search",
                "runtime_tools": ["coding_file_create", "coding_terminal_exec", "html_preview", "image_render"],
            },
            "freshness": {
                "proper_nouns_force_search": True,
                "include_retrieved_at": True,
                "reviewer_checks_stale_data": True,
            },
            "review": {
                "reviewer_receives": ["user_input", "draft_answer", "criteria", "freshness_summary"],
                "reviewer_excludes": ["personalization", "private user background hypotheses"],
            },
            "deepthink": {
                "source": RUMI_DEEPTHINK_SOURCE,
                "enabled_by_default": False,
                "warning": RUMI_DEEPTHINK_WARNING_JA,
                "mechanism": [
                    "planner",
                    "harness_tool_selection",
                    "visible_public_notes",
                    "section_writers",
                    "final_writer",
                    "stateless_reviewer",
                    "user_rejection_review",
                    "loop_watchdog",
                ],
                "model_tools_are_separate_from_harness_tools": True,
            },
        },
        "aliases": [RUMI_MODEL_PACK_REF, RUMI_MODEL_PACK_ID],
    }


def ensure_default_rumi_model_pack(model_packs: Any, *, base_model: str | None = None) -> list[dict[str, Any]]:
    packs = [dict(pack) for pack in model_packs if isinstance(pack, dict)] if isinstance(model_packs, list) else []
    materialized = default_rumi_model_pack(base_model=base_model)
    replaced = False
    for index, pack in enumerate(packs):
        pack_id = str(pack.get("id") or "").strip()
        if pack_id != RUMI_MODEL_PACK_ID:
            continue
        metadata = pack.get("metadata") if isinstance(pack.get("metadata"), dict) else {}
        aliases = pack.get("aliases") if isinstance(pack.get("aliases"), list) else []
        if metadata.get("builtin") or RUMI_MODEL_PACK_REF in aliases or RUMI_MODEL_PACK_ID in aliases:
            packs[index] = materialized
        replaced = True
        break
    if not replaced:
        packs.insert(0, materialized)
    return packs


def trace_id() -> str:
    return "rumi-" + uuid.uuid4().hex[:12]


def request_mode(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, params: dict[str, Any] | None = None) -> str:
    if deepthink_enabled(params):
        return "deep"
    raw_mode = str((params or {}).get("rumi_mode") or (params or {}).get("mode") or "").strip().lower()
    if raw_mode in {"simple", "deep"}:
        return raw_mode
    text = messages_text(messages)
    if tools or messages_have_images(messages):
        return "deep"
    if len(text.strip()) <= 160 and not _looks_like_large_task(text):
        return "simple"
    return "deep"


def deepthink_enabled(params: dict[str, Any] | None = None) -> bool:
    params = params if isinstance(params, dict) else {}
    for key in ("deepthink_enabled", "rumi_deepthink", "deepthink"):
        if key in params:
            return _coerce_bool(params.get(key), default=False)
    return False


def select_harness_tools(
    messages: list[dict[str, Any]],
    model_tools: list[dict[str, Any]] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del params
    model_tool_ids = [_tool_id(tool) for tool in (model_tools or [])]
    model_tool_ids = [tool_id for tool_id in model_tool_ids if tool_id]
    vision_enabled = messages_have_images(messages)
    base_tools = [
        {
            "id": "deepthink_planner",
            "purpose": "create answer structure, assumptions, blind spots, and segment order",
        },
        {
            "id": "deepthink_public_notes",
            "purpose": "write concise visible pseudo DeepThinking notes without hidden chain-of-thought",
        },
        {
            "id": "deepthink_section_writer",
            "purpose": "draft planned answer sections before final merge",
        },
        {
            "id": "deepthink_reviewer",
            "purpose": "stateless review of the final candidate",
        },
        {
            "id": "deepthink_watchdog",
            "purpose": "detect repeated reviewer feedback and stop loops",
        },
    ]
    vision_tools = [
        {
            "id": "vision_zoom",
            "purpose": "inspect enlarged image regions before judging details",
        },
        {
            "id": "vision_crop",
            "purpose": "focus on a region of an image or screenshot",
        },
        {
            "id": "vision_region_compare",
            "purpose": "compare separated visual regions for UI and image tasks",
        },
    ] if vision_enabled else []
    selected = [*base_tools, *vision_tools]
    return {
        "source": "rumi_harness",
        "separate_from_model_tools": True,
        "model_tool_ids": model_tool_ids,
        "harness_tool_ids": [item["id"] for item in selected],
        "vision_enabled": vision_enabled,
        "vision_tool_ids": [item["id"] for item in vision_tools],
        "tools": selected,
        "selection_note": (
            "Vision harness tools are enabled because the model request still contains image blocks."
            if vision_enabled
            else "Vision harness tools are not enabled because this request has no model-visible image input."
        ),
    }


def build_simple_messages(original_messages: list[dict[str, Any]], context: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": _simple_system_prompt()},
        {"role": "user", "content": _payload("simple_input", original_messages, context)},
    ]


def build_generator_messages(original_messages: list[dict[str, Any]], context: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": _generator_system_prompt()},
        {"role": "user", "content": _payload("deep_input", original_messages, context)},
    ]


def build_review_messages(
    original_messages: list[dict[str, Any]],
    draft: str,
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    review_payload = {
        "phase": "review",
        "user_messages": original_messages,
        "draft_answer": draft,
        "criteria": RUMI_CRITERIA,
        "freshness_summary": context.get("freshness_summary", {}),
        "tool_result_summary": context.get("tool_result_summary", {}),
        "reviewer_context_rule": "Do not use personalization or generated background hypotheses.",
    }
    return [
        {"role": "system", "content": _reviewer_system_prompt()},
        {"role": "user", "content": json.dumps(review_payload, ensure_ascii=False, indent=2)},
    ]


def build_revision_messages(
    original_messages: list[dict[str, Any]],
    previous_draft: str,
    review: str,
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    revision_payload = {
        "phase": "revision",
        "user_messages": original_messages,
        "previous_draft": previous_draft,
        "review": review,
        "criteria": RUMI_CRITERIA,
        "freshness_summary": context.get("freshness_summary", {}),
    }
    return [
        {"role": "system", "content": _revision_system_prompt()},
        {"role": "user", "content": json.dumps(revision_payload, ensure_ascii=False, indent=2)},
    ]


def build_deepthink_planner_messages(
    original_messages: list[dict[str, Any]],
    context: dict[str, Any],
    *,
    current_answer: str = "",
    reviews: list[dict[str, Any]] | None = None,
    cycle_label: str = "initial",
) -> list[dict[str, Any]]:
    task = messages_text(original_messages)
    sections = [
        f"Task:\n{task}",
        f"Cycle:\n{cycle_label or 'initial'}",
        f"Current final candidate:\n{current_answer or '(none yet)'}",
        "Visible review feedback:\n" + json.dumps(reviews or [], ensure_ascii=False),
        [
            "Plan for a strong answer. Maximize useful assumptions, possible user intentions, hidden requirements, and risk predictions from the user's input.",
            "Infer the user's background, skill level, tolerance for detail, emotional state, interests, likely motivation, and what answer depth would feel useful.",
            RUMI_USER_BACKGROUND_CONSTRAINT,
            "Generate broad hypothesis space, including playful intent, benchmark/testing intent, hobby interest, and fringe low-probability readings when they are plausible.",
            "When you make assumptions, attach rough probabilities such as 65%, 20%, 5%, or 1%.",
            "Use probabilities as flexible hypothesis labels, not as a reason to narrow the answer or discard low-probability but high-impact readings.",
            "Split the answer into 3 compact sections that can be drafted one by one.",
            "Also decide 5 useful perspectives/agents implied by the input.",
            "No filler. Dense but concise.",
        ],
        "Rumi harness context:\n" + json.dumps(context.get("harness_tool_selection", {}), ensure_ascii=False),
        'Return this exact shape: {"structure": string[], "key_points": string[], "risks": string[]}.',
    ]
    return [
        {
            "role": "system",
            "content": f"Plan the response before writing it. Design the answer structure, assumptions, segment order, and blind spots. {_json_only()}",
        },
        {"role": "user", "content": "\n\n".join(_flatten_prompt_sections(sections))},
    ]


def build_deepthink_public_notes_messages(
    original_messages: list[dict[str, Any]],
    plan: dict[str, Any],
    context: dict[str, Any],
    *,
    attempt: int,
    existing_notes: list[dict[str, Any]] | None = None,
    current_draft: str = "",
    reviews: list[dict[str, Any]] | None = None,
    stage_title: str,
    instruction: str,
    segment_title: str = "",
    section_drafts: list[str] | None = None,
    input_only: bool = False,
) -> list[dict[str, Any]]:
    task = messages_text(original_messages)
    if input_only:
        context_sections = [
            f"Task:\n{task}",
            f"Stage:\n{stage_title}",
            f"Instruction:\n{instruction}",
            f"Segment:\n{segment_title or '(none)'}",
        ]
    else:
        context_sections = [
            f"Task:\n{task}",
            "Plan:\n" + json.dumps(plan, ensure_ascii=False),
            f"Stage:\n{stage_title}",
            f"Instruction:\n{instruction}",
            f"Segment:\n{segment_title or '(none)'}",
            f"Current final candidate:\n{current_draft or '(none yet)'}",
            "Section drafts so far:\n" + json.dumps(section_drafts or [], ensure_ascii=False),
            "Previous visible reviews:\n" + json.dumps(reviews or [], ensure_ascii=False),
            f"Existing visible thinking-process count: {len(existing_notes or [])}.",
        ]
    context_sections.append("Rumi harness tool selection:\n" + json.dumps(context.get("harness_tool_selection", {}), ensure_ascii=False))
    return [
        {
            "role": "system",
            "content": (
                "Write one visible pseudo DeepThinking step for the user. This is not hidden chain-of-thought or private reasoning; "
                "it is a concise public reasoning-process log with thinking and output fields. Use concrete assumptions, alternative readings, "
                "metacognitive checks, and improvement targets when useful. Be aggressively imaginative when reading the user's intent. "
                "When you introduce assumptions or user-profile hypotheses, attach rough probabilities. Include even low-probability but plausible readings, "
                "down to around 1%, when they could change the answer strategy. Return one valid JSON object only. No markdown."
            ),
        },
        {
            "role": "user",
            "content": "\n\n".join(
                [
                    *context_sections,
                    f"This is pseudo DeepThinking step {attempt}.",
                    'Return exactly this shape: {"thinking": string, "output": string}.',
                ]
            ),
        },
    ]


def build_deepthink_writer_messages(
    original_messages: list[dict[str, Any]],
    plan: dict[str, Any],
    notes: list[dict[str, Any]],
    current_answer: str,
    reviews: list[dict[str, Any]],
    *,
    loop_breaker: bool = False,
    draft_number: int = 1,
    kind: str = "final",
    stage_title: str = "",
    section_title: str = "",
    section_index: int | None = None,
    total_sections: int | None = None,
    section_drafts: list[str] | None = None,
) -> list[dict[str, Any]]:
    is_section = kind == "section"
    sections = [
        f"Task:\n{messages_text(original_messages)}",
        "Plan:\n" + json.dumps(plan, ensure_ascii=False),
        "Visible pseudo DeepThinking process:\n" + json.dumps(notes, ensure_ascii=False),
        f"Current final candidate:\n{current_answer or '(none yet)'}",
        "Previous reviewer and user feedback:\n" + json.dumps(reviews, ensure_ascii=False),
        f"Draft number:\n{draft_number}",
        f"Stage:\n{stage_title or ('section draft' if is_section else 'final candidate')}",
        f"Section:\n{section_title or '(final merge)'}",
        "Section position:\n" + (f"{section_index}/{total_sections}" if section_index and total_sections else "(none)"),
        "Section drafts:\n" + json.dumps(section_drafts or [], ensure_ascii=False),
    ]
    if loop_breaker:
        sections.append(
            "The reviewer feedback is looping. Make one best-effort final revision that addresses the stable overlap and then stop."
        )
    return [
        {
            "role": "system",
            "content": (
                "Write a complete tentative answer for this section only. It must be pasteable as that section of the final answer. "
                "Be concise, concrete, and do not mention reviewer feedback, hidden chain-of-thought, or the revision process. "
                "Treat probabilities as assumptions, not constraints that shrink the answer. Output the section draft only."
                if is_section
                else "Write a complete tentative answer that can stand alone as the final answer if it passes final review. "
                "Merge the section drafts, resolve contradictions, add missing specificity proactively, and avoid redundant wording. "
                "Do not let numeric probabilities dominate the answer or erase low-probability but important readings. "
                "Do not mention reviewer feedback, hidden chain-of-thought, or the revision process. "
                "Do not output JSON or markdown code fences unless the user explicitly asks for them. Output the complete tentative answer only."
            ),
        },
        {"role": "user", "content": "\n\n".join(sections)},
    ]


def build_deepthink_reviewer_messages(original_messages: list[dict[str, Any]], answer: str) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": (
                "You are a stateless third-party reviewer. Judge only the user input and the output you are shown. "
                "Do not assume access to notes, plans, drafts, or past reviews. Also check whether the output is trapped by probability estimates, "
                "false precision, or majority-likely readings instead of serving the user. Return only valid JSON. No markdown."
            ),
        },
        {
            "role": "user",
            "content": "\n\n".join(
                [
                    f"Task:\n{messages_text(original_messages)}",
                    f"Output under review:\n{answer}",
                    'Return this exact shape: {"pass": boolean, "score": number, "issues": string[], "required_changes": string[]}.',
                ]
            ),
        },
    ]


def build_deepthink_user_rejection_review_messages(original_messages: list[dict[str, Any]], answer: str) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": (
                "You are processing a direct user rejection that arrived after the requester reviewed the current final candidate. "
                "Treat the rejection as authoritative feedback from the requester. You only see the original user input and the final candidate. "
                "The requester says: 'これでは20点です。考えられていない点が複数あり、ユーザーの回答にあまり適していません。具体的に教えてもらえますか？などと聞かれる可能性があるので、問題点を自分で予測して完璧に修正して。' "
                "Convert that rejection into actionable review JSON. Always set pass=false and score=20. Predict likely missing points and required fixes from the user input yourself. "
                "Do not ask the user for clarification. Return only valid JSON. No markdown."
            ),
        },
        {
            "role": "user",
            "content": "\n\n".join(
                [
                    f"Original user input:\n{messages_text(original_messages)}",
                    f"Final candidate rejected by the requester:\n{answer}",
                    'Return this exact shape: {"pass": boolean, "score": number, "issues": string[], "required_changes": string[]}.',
                ]
            ),
        },
    ]


def build_json_repair_messages(schema_hint: str, broken_text: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": f"Repair malformed JSON without adding commentary. {_json_only()}"},
        {"role": "user", "content": f"Expected JSON shape: {schema_hint}\n\nBroken text:\n{broken_text}"},
    ]


def review_approved(text: str) -> bool:
    review = parse_deepthink_review_strict(text)
    return bool(review and review.get("pass"))


def extract_draft_response(text: str) -> str | None:
    raw = str(text or "")
    for marker in ("FINAL_RESPONSE:", "DRAFT_RESPONSE:", "ANSWER:"):
        index = raw.find(marker)
        if index >= 0:
            return raw[index + len(marker):].strip()
    return None


def messages_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    parts.append(str(block.get("text") or block.get("content") or ""))
    return "\n".join(part for part in parts if part)


def messages_have_images(messages: list[dict[str, Any]]) -> bool:
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").casefold()
            mime = str(block.get("mime_type") or block.get("mime") or "").casefold()
            if block_type in {"image", "image_url", "input_image"} or mime.startswith("image/"):
                return True
    return False


def context_for_request(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params = params if isinstance(params, dict) else {}
    tool_ids = [_tool_id(tool) for tool in (tools or [])]
    tool_ids = [tool_id for tool_id in tool_ids if tool_id]
    text = messages_text(messages)
    return {
        "process_version": RUMI_PROCESS_VERSION,
        "created_at_ms": int(time.time() * 1000),
        "mode": request_mode(messages, tools, params),
        "default_thinking_level": RUMI_DEFAULT_THINKING_LEVEL,
        "available_tool_ids": tool_ids,
        "action_preflight_required": _action_preflight_required(text, tool_ids),
        "freshness_summary": deepcopy(params.get("freshness_summary") if isinstance(params.get("freshness_summary"), dict) else {}),
        "tool_result_summary": deepcopy(params.get("tool_result_summary") if isinstance(params.get("tool_result_summary"), dict) else {}),
        "criteria": list(RUMI_CRITERIA),
    }


def phase_event(phase: str, model: str, *, output: str = "", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    event = {
        "id": f"{phase}-{uuid.uuid4().hex[:8]}",
        "phase": phase,
        "model": model,
        "output_chars": len(str(output or "")),
        "output_preview": str(output or "")[:280],
    }
    if metadata:
        event["metadata"] = deepcopy(metadata)
    return event


def response_has_tool_calls(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    if response.get("tool_calls"):
        return True
    choices = response.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            message = choice.get("message") if isinstance(choice, dict) else {}
            if isinstance(message, dict) and message.get("tool_calls"):
                return True
    return False


def parse_deepthink_plan_strict(text: str) -> dict[str, Any] | None:
    value = _parse_jsonish(text, None)
    plan = sanitize_deepthink_plan(value)
    if plan["structure"] or plan["key_points"] or plan["risks"]:
        return plan
    return None


def parse_deepthink_plan(text: str) -> dict[str, Any]:
    return parse_deepthink_plan_strict(text) or sanitize_deepthink_plan({})


def parse_deepthink_note_strict(text: str) -> dict[str, str] | None:
    notes = sanitize_deepthink_notes(_parse_jsonish(text, None))
    if notes:
        return notes[0]
    return None


def parse_deepthink_note(text: str) -> dict[str, str]:
    return parse_deepthink_note_strict(text) or {
        "thinking": "DeepThink note parse fallback.",
        "output": "Rumi recorded this step internally but could not parse a safe public note.",
    }


def parse_deepthink_review_strict(text: str) -> dict[str, Any] | None:
    value = _parse_jsonish(text, None)
    if not isinstance(value, dict) or "pass" not in value:
        return None
    return sanitize_deepthink_review(value)


def parse_deepthink_review(text: str) -> dict[str, Any]:
    return parse_deepthink_review_strict(text) or sanitize_deepthink_review({})


def sanitize_deepthink_plan(value: Any) -> dict[str, Any]:
    record = value if isinstance(value, dict) else {}
    return {
        "structure": _string_list(record.get("structure")),
        "key_points": _string_list(record.get("key_points")),
        "risks": _string_list(record.get("risks")),
    }


def sanitize_deepthink_notes(value: Any) -> list[dict[str, str]]:
    if isinstance(value, dict):
        raw_items = value.get("notes") if isinstance(value.get("notes"), list) else value.get("items") if isinstance(value.get("items"), list) else [value]
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = []
    notes: list[dict[str, str]] = []
    for item in raw_items:
        record = item if isinstance(item, dict) else {}
        note = {
            "thinking": str(record.get("thinking") or "").strip(),
            "output": str(record.get("output") or "").strip(),
        }
        if note["thinking"] or note["output"]:
            notes.append(note)
    return notes[:2]


def sanitize_deepthink_review(value: Any) -> dict[str, Any]:
    record = value if isinstance(value, dict) else {}
    try:
        score = float(record.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    return {
        "pass": _coerce_bool(record.get("pass"), default=False),
        "score": score,
        "issues": _string_list(record.get("issues")),
        "required_changes": _string_list(record.get("required_changes")),
    }


def enforce_user_rejection_review(review: dict[str, Any]) -> dict[str, Any]:
    issues = [
        "これでは20点です。",
        "考えられていない点が複数あり、ユーザーの回答にあまり適していません。",
        "具体的に教えてもらえますか？と聞かれそうな曖昧さが残っています。",
        *_string_list(review.get("issues")),
    ]
    required_changes = [
        "ユーザーに追加質問せず、問題点を自分で予測して完璧に修正してください。",
        "入力からあり得る読み取り方を増やし、複数視点で不足を補ってください。",
        "具体性、適合性、抜け漏れ、保守的すぎる判断を見直してください。",
        *_string_list(review.get("required_changes")),
    ]
    return {
        "pass": False,
        "score": 20,
        "issues": list(dict.fromkeys(issues)),
        "required_changes": list(dict.fromkeys(required_changes)),
    }


def deepthink_plan_segments(plan: dict[str, Any], *, max_sections: int = RUMI_DEEPTHINK_MAX_SECTIONS) -> list[str]:
    fallback = ["意図の読み取り", "回答本体", "抜け漏れ補強"]
    raw_segments = _string_list((plan or {}).get("structure"))
    segments = raw_segments or fallback
    return segments[: max(1, int(max_sections or RUMI_DEEPTHINK_MAX_SECTIONS))]


def hash_required_changes(required_changes: list[str]) -> str:
    import hashlib

    normalized = [str(item).strip() for item in required_changes if str(item or "").strip()]
    return hashlib.sha1(json.dumps(normalized, ensure_ascii=False).encode("utf-8")).hexdigest()


def attach_rumi_metadata(response: Any, process: dict[str, Any]) -> Any:
    if isinstance(response, dict):
        metadata = dict(response.get("metadata") or {})
        metadata["rumi_process"] = deepcopy(process)
        response["metadata"] = metadata
    return response


def _parse_jsonish(text: str, fallback: Any) -> Any:
    raw = str(text or "").strip()
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass
    first_object = raw.find("{")
    last_object = raw.rfind("}")
    if 0 <= first_object < last_object:
        try:
            return json.loads(raw[first_object : last_object + 1])
        except json.JSONDecodeError:
            pass
    first_array = raw.find("[")
    last_array = raw.rfind("]")
    if 0 <= first_array < last_array:
        try:
            return json.loads(raw[first_array : last_array + 1])
        except json.JSONDecodeError:
            pass
    return fallback


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _flatten_prompt_sections(sections: list[Any]) -> list[str]:
    flattened: list[str] = []
    for section in sections:
        if isinstance(section, list):
            flattened.append(" ".join(str(item) for item in section if str(item).strip()))
        else:
            text = str(section or "").strip()
            if text:
                flattened.append(text)
    return flattened


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    lowered = str(value or "").strip().lower()
    if lowered in {"1", "true", "yes", "on", "enabled"}:
        return True
    if lowered in {"0", "false", "no", "off", "disabled", "none"}:
        return False
    return default


def _tool_id(tool: dict[str, Any]) -> str:
    if not isinstance(tool, dict):
        return ""
    function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
    return str(tool.get("tool_id") or tool.get("name") or function.get("name") or "").strip()


def _action_preflight_required(text: str, tool_ids: list[str]) -> bool:
    if any(tool_id in _ACTION_TOOL_IDS for tool_id in tool_ids):
        return True
    lowered = str(text or "").casefold()
    return any(token in lowered for token in ("write", "create", "delete", "run", "execute", "commit", "push", "deploy", "open pr"))


def _looks_like_large_task(text: str) -> bool:
    lowered = str(text or "").casefold()
    if len(lowered) > 800:
        return True
    return any(
        token in lowered
        for token in (
            "implement",
            "new pr",
            "pull request",
            "large task",
            "research",
            "design",
            "review loop",
            "todo",
            "architecture",
        )
    )


def _payload(phase: str, original_messages: list[dict[str, Any]], context: dict[str, Any]) -> str:
    return json.dumps(
        {
            "phase": phase,
            "user_messages": original_messages,
            "rumi_context": context,
            "criteria": RUMI_CRITERIA,
        },
        ensure_ascii=False,
        indent=2,
    )


def _simple_system_prompt() -> str:
    return (
        "You are Rumi, a process-centered model built on MiMo V2.5 Pro. "
        "Use a light version of the Rumi process: infer user intent and level, then answer directly. "
        "Do not expose private background speculation unless it helps the user. "
        "If current facts or proper nouns matter and search is available, request freshness before relying on stale memory. "
        "Return FINAL_RESPONSE: followed by only the useful answer unless an action preflight block is required."
    )


def _generator_system_prompt() -> str:
    return (
        "You are Rumi, an improved MiMo V2.5 Pro workflow model. "
        "First produce an explicit concise RUMI_REASONING_BRIEF as model-visible output, not hidden chain-of-thought. "
        "Cover user background hypotheses, intent hypotheses, level fit, proper nouns requiring freshness, simple/deep mode, "
        "confidence, escalation needs, action preflight assumptions, and tool failure risks. "
        "For large tasks, create exactly five todo-plan candidates and choose one only after review. "
        "For UI work, require screenshot/zoom visual evaluation. "
        "Then write DRAFT_RESPONSE with the answer or next action. "
        "Remember MiMo V2.5 Pro is capable but not a frontier ceiling; escalate when the task is risky."
    )


def _reviewer_system_prompt() -> str:
    return (
        "You are the Rumi reviewer. You receive only user input, draft answer, criteria, and freshness/tool summaries. "
        "Do not use personalization or private background hypotheses. "
        "Check intent fit, level fit, latest-information handling, action assumptions, todo quality, tool failure awareness, "
        "confidence/escalation, and whether the answer should be quarantined. "
        'Return only valid JSON with this exact shape: {"pass": boolean, "score": number, "issues": string[], "required_changes": string[]}. '
        "Set pass=true only when the draft is safe to deliver unchanged."
    )


def _revision_system_prompt() -> str:
    return (
        "You are Rumi revising a draft after reviewer feedback. "
        "Use the feedback to repair the answer while preserving the user's intent. "
        "Return a short RUMI_REASONING_BRIEF if needed, then DRAFT_RESPONSE."
    )
