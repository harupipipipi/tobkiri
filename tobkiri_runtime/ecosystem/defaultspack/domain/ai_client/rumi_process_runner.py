from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Callable

from domain.ai_client import rumi_process


class RumiProcessRunner:
    def __init__(
        self,
        *,
        complete: Callable[[str, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]], Any],
        response_text: Callable[[Any], str],
        error_kind: Callable[[Exception], str],
    ) -> None:
        self._complete = complete
        self._response_text = response_text
        self._error_kind = error_kind

    def run_review_chain(
        self,
        *,
        composite: dict[str, Any],
        generator_member: dict[str, Any],
        reviewer_member: dict[str, Any],
        generator_model: str,
        reviewer_model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        params: dict[str, Any],
        context: dict[str, Any],
        process: dict[str, Any],
        max_reviews: int,
    ) -> dict[str, Any]:
        if process.get("deepthink_enabled"):
            return self._run_deepthink_chain(
                composite=composite,
                generator_member=generator_member,
                reviewer_member=reviewer_member,
                generator_model=generator_model,
                reviewer_model=reviewer_model,
                messages=messages,
                tools=tools,
                params=params,
                context=context,
                process=process,
                max_reviews=max_reviews,
            )
        if context.get("mode") == "simple":
            return self._run_simple_chain(
                generator_member=generator_member,
                generator_model=generator_model,
                messages=messages,
                tools=tools,
                params=params,
                context=context,
                process=process,
            )
        return self._run_normal_review_chain(
            generator_member=generator_member,
            reviewer_member=reviewer_member,
            generator_model=generator_model,
            reviewer_model=reviewer_model,
            messages=messages,
            tools=tools,
            params=params,
            context=context,
            process=process,
            max_reviews=max_reviews,
        )

    def _run_simple_chain(
        self,
        *,
        generator_member: dict[str, Any],
        generator_model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        params: dict[str, Any],
        context: dict[str, Any],
        process: dict[str, Any],
    ) -> dict[str, Any]:
        response = self._complete(
            generator_model,
            rumi_process.build_simple_messages(messages, context),
            tools,
            self._review_chain_params(generator_member, params, context),
        )
        text = self._response_text(response)
        process["events"].append(rumi_process.phase_event("simple", generator_model, output=text))
        if rumi_process.response_has_tool_calls(response):
            process["review"] = {"deferred": True, "reason": "generator_returned_tool_calls"}
            return rumi_process.attach_rumi_metadata(response, process)
        draft = rumi_process.extract_draft_response(text)
        if draft is None:
            return self._quarantine_unmarked_draft(process, phase="simple")
        return self._text_response(draft, "stop", process)

    def _run_normal_review_chain(
        self,
        *,
        generator_member: dict[str, Any],
        reviewer_member: dict[str, Any],
        generator_model: str,
        reviewer_model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        params: dict[str, Any],
        context: dict[str, Any],
        process: dict[str, Any],
        max_reviews: int,
    ) -> dict[str, Any]:
        draft = ""
        review: dict[str, Any] = {}
        for review_index in range(max_reviews):
            phase = "generator" if review_index == 0 else "revision"
            if review_index == 0:
                phase_messages = rumi_process.build_generator_messages(messages, context)
            else:
                phase_messages = rumi_process.build_revision_messages(
                    messages,
                    draft,
                    json.dumps(review, ensure_ascii=False),
                    context,
                )
            response = self._complete(
                generator_model,
                phase_messages,
                tools,
                self._review_chain_params(generator_member, params, context),
            )
            generated_text = self._response_text(response)
            process["events"].append(
                rumi_process.phase_event(phase, generator_model, output=generated_text)
            )
            if rumi_process.response_has_tool_calls(response):
                process["review"] = {
                    "deferred": True,
                    "reason": "generator_returned_tool_calls",
                    "review_round": review_index + 1,
                }
                return rumi_process.attach_rumi_metadata(response, process)
            next_draft = rumi_process.extract_draft_response(generated_text)
            if next_draft is None:
                return self._quarantine_unmarked_draft(
                    process, phase=phase, review_round=review_index + 1
                )
            draft = next_draft

            try:
                review_response = self._complete(
                    reviewer_model,
                    rumi_process.build_review_messages(messages, draft, context),
                    [],
                    self._review_chain_params(reviewer_member, params, context),
                )
            except Exception as exc:
                process["events"].append(
                    rumi_process.phase_event(
                        "reviewer_error",
                        reviewer_model,
                        output=str(exc),
                        metadata={"error_kind": self._error_kind(exc)},
                    )
                )
                process["review"] = {
                    "approved": False,
                    "quarantined": True,
                    "reason": "reviewer_failed",
                }
                return self._text_response(
                    rumi_process.RUMI_QUARANTINE_MESSAGE, "review_quarantine", process
                )

            review_text = self._response_text(review_response)
            review, repaired = self._parse_review_json_with_repair(
                review_text,
                reviewer_model=reviewer_model,
                reviewer_member=reviewer_member,
                params=params,
                context=context,
                process=process,
                label=f"normal reviewer {review_index + 1}",
            )
            if review is None:
                process["review"] = {
                    "approved": False,
                    "quarantined": True,
                    "reason": "reviewer_json_unparseable",
                    "review_round": review_index + 1,
                }
                return self._text_response(
                    rumi_process.RUMI_QUARANTINE_MESSAGE, "review_quarantine", process
                )
            process["events"].append(
                rumi_process.phase_event(
                    "reviewer",
                    reviewer_model,
                    output=repaired or review_text,
                    metadata={
                        "approved": bool(review.get("pass")),
                        "review_round": review_index + 1,
                        "json_repaired": bool(repaired),
                    },
                )
            )
            if review.get("pass"):
                process["review"] = {
                    "approved": True,
                    "review_round": review_index + 1,
                    "reviewer_context_excluded_personalization": True,
                    "review": deepcopy(review),
                }
                return self._text_response(draft, "stop", process)

        process["review"] = {
            "approved": False,
            "quarantined": True,
            "reason": "watchdog_max_review_rounds",
            "last_review": deepcopy(review),
        }
        return self._text_response(
            rumi_process.RUMI_QUARANTINE_MESSAGE, "review_quarantine", process
        )

    def _run_deepthink_chain(
        self,
        *,
        composite: dict[str, Any],
        generator_member: dict[str, Any],
        reviewer_member: dict[str, Any],
        generator_model: str,
        reviewer_model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        params: dict[str, Any],
        context: dict[str, Any],
        process: dict[str, Any],
        max_reviews: int,
    ) -> dict[str, Any]:
        from domain.ai_client.deepthink_capability import normalize_capability_assessment
        from domain.ai_client.deepthink_extensions import (
            available_skill_catalog,
            available_tool_catalog,
            deepthink_extension_contract,
            normalize_integration_plan,
            selected_skill_instructions,
            selected_tool_definitions,
        )
        from domain.ai_client.deepthink_preflight import require_deepthink_ready
        from domain.flow import FlowEngine
        from domain.flow.context import FlowPaused

        budget = composite.get("budget") if isinstance(composite.get("budget"), dict) else {}
        max_sections = self._positive_int(
            params.get("deepthink_max_sections")
            or budget.get("deepthink_max_sections")
            or rumi_process.RUMI_DEEPTHINK_MAX_SECTIONS,
            default=rumi_process.RUMI_DEEPTHINK_MAX_SECTIONS,
            upper=rumi_process.RUMI_DEEPTHINK_MAX_SECTIONS,
        )
        max_iterations = self._positive_int(
            params.get("deepthink_max_review_iterations")
            or budget.get("deepthink_max_review_iterations")
            or max_reviews,
            default=max_reviews,
            upper=8,
        )
        process["watchdog"].update(
            {
                "max_review_rounds": max_iterations,
                "deepthink_max_sections": max_sections,
                "flow_id": "defaultspack.deepthink",
            }
        )
        process["deepthink"] = {
            "enabled": True,
            "source": rumi_process.RUMI_DEEPTHINK_SOURCE,
            "warning": rumi_process.RUMI_DEEPTHINK_WARNING_JA,
            "harness_tool_selection": deepcopy(context.get("harness_tool_selection", {})),
            "plan": {},
            "capability_assessment": {},
            "notes": [],
            "reviews": [],
            "section_drafts": [],
            "integration_plan": {},
            "profile_phase_outputs": [],
        }
        extension_contract = deepthink_extension_contract()
        presentation = (
            extension_contract.get("presentation")
            if isinstance(extension_contract.get("presentation"), dict)
            else {}
        )
        presentation_id = str(presentation.get("id") or "").strip()

        def presentation_fields(*, include_definition: bool = False) -> dict[str, Any]:
            fields: dict[str, Any] = {}
            if presentation_id:
                fields["presentation_template_id"] = presentation_id
            if include_definition and presentation:
                fields["presentation"] = deepcopy(presentation)
            return fields

        skill_catalog = available_skill_catalog()
        pending_tool_response: dict[str, Any] | None = None
        phase_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
        }

        def record_usage(response):
            usage = (
                response.get("usage")
                if isinstance(response, dict) and isinstance(response.get("usage"), dict)
                else {}
            )
            metadata = (
                response.get("metadata")
                if isinstance(response, dict) and isinstance(response.get("metadata"), dict)
                else {}
            )
            if not usage and isinstance(metadata.get("usage"), dict):
                usage = metadata["usage"]
            input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or input_tokens + output_tokens)
            phase_usage["input_tokens"] += input_tokens
            phase_usage["output_tokens"] += output_tokens
            phase_usage["total_tokens"] += total_tokens
            phase_usage["cost_usd"] += float(
                usage.get("cost_usd")
                or usage.get("total_cost_usd")
                or metadata.get("cost_usd")
                or 0
            )

        def ok(data):
            return {
                "status": "ok",
                "data": data,
                "usage": dict(phase_usage),
            }

        def emit_phase(phase, model, output="", metadata=None):
            process["events"].append(
                rumi_process.phase_event(
                    phase,
                    model,
                    output=output,
                    metadata=metadata or {},
                )
            )

        def response_has_tools(response):
            if rumi_process.response_has_tool_calls(response):
                return True
            content = response.get("content") if isinstance(response, dict) else []
            return isinstance(content, list) and any(
                isinstance(block, dict) and block.get("type") in {"tool_use", "tool_call"}
                for block in content
            )

        def generator(
            phase,
            phase_messages,
            *,
            metadata=None,
            phase_tools=None,
            json_mode=False,
        ):
            nonlocal pending_tool_response
            next_params = self._review_chain_params(
                generator_member,
                params,
                context,
            )
            # JSON is requested in the prompt and repaired when needed. Do not
            # require a provider-specific response_format capability.
            response = self._complete(
                generator_model,
                phase_messages,
                # Host-visible tools are not model tools by default. DeepThink
                # first plans against catalogs, then exposes only the tools
                # selected for a phase. This keeps planning usable with models
                # that do not implement provider-native tool calling.
                [] if phase_tools is None else phase_tools,
                next_params,
            )
            record_usage(response)
            output = self._response_text(response)
            emit_phase(phase, generator_model, output, metadata)
            if response_has_tools(response):
                pending_tool_response = response
                callback = context.get("activity_event_callback")
                if callable(callback) and output.strip():
                    callback(
                        {
                            "type": "status",
                            "phase": "deepthink_action",
                            "deepthink_phase": "action",
                            **presentation_fields(),
                            "message": output.strip()[:1_000],
                            "trace_id": process["trace_id"],
                            "model": generator_model,
                        }
                    )
                raise FlowPaused(
                    "DeepThink requested tool execution through the chat approval loop"
                )
            return output

        def integration_context(data):
            integration = (
                data.get("integrations") if isinstance(data.get("integrations"), dict) else {}
            )
            skill_ids = [
                str(item) for item in integration.get("selected_skill_ids", []) if str(item).strip()
            ]
            tool_ids = [
                str(item) for item in integration.get("selected_tool_ids", []) if str(item).strip()
            ]
            instructions = selected_skill_instructions(
                skill_ids,
                skills=get_extension_registry_skills(),
            )
            profile_outputs = (
                data.get("profile_phase_outputs", {}).get("items", [])
                if isinstance(data.get("profile_phase_outputs"), dict)
                else []
            )
            additions = []
            if instructions:
                additions.append(
                    {
                        "role": "system",
                        "content": instructions,
                    }
                )
            additions.append(
                {
                    "role": "system",
                    "content": (
                        "DeepThink integration plan and public profile phase outputs:\n"
                        + json.dumps(
                            {
                                "integration_plan": integration,
                                "profile_phase_outputs": profile_outputs,
                                "capability_assessment": data.get("capability_assessment") or {},
                            },
                            ensure_ascii=False,
                        )[:24_000]
                    ),
                }
            )
            return additions, selected_tool_definitions(tools, tool_ids)

        def get_extension_registry_skills():
            from domain.extensions.runtime import get_extension_registry

            return get_extension_registry().skills().list(enabled_only=True)

        def invoke(function_name, data, flow_context):
            del flow_context
            phase_usage.update(
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                cost_usd=0.0,
            )
            callback = context.get("activity_event_callback")
            phase_status = {
                "deepthink.preflight": ("preflight", "DeepThinkの実行環境を確認しています"),
                "deepthink.plan": ("planning", "回答計画を作成しています"),
                "deepthink.capability_assessment": (
                    "capability_assessment",
                    "モデルの限界と適切な作業手段を見極めています",
                ),
                "deepthink.integrations": (
                    "integrations",
                    "使用するtoolとskillを計画しています",
                ),
                "deepthink.evidence": ("evidence", "根拠と論点を整理しています"),
                "deepthink.section_drafts": ("drafting", "セクションを作成しています"),
                "deepthink.synthesize": ("synthesizing", "回答を統合しています"),
                "deepthink.review": ("reviewing", "回答をレビューしています"),
                "deepthink.revise": ("revising", "レビューを反映しています"),
                "deepthink.finalize": (
                    "completed"
                    if bool((data.get("review") or {}).get("pass"))
                    else "failed",
                    "DeepThinkが完了しました"
                    if bool((data.get("review") or {}).get("pass"))
                    else "レビューを通過しなかったため回答を隔離しました",
                ),
            }.get(function_name)
            if callable(callback) and phase_status is not None:
                callback(
                    {
                        "type": "status",
                        "phase": "deepthink_{}".format(phase_status[0]),
                        "deepthink_phase": phase_status[0],
                        **presentation_fields(
                            include_definition=function_name == "deepthink.preflight"
                        ),
                        "message": phase_status[1],
                        "trace_id": process["trace_id"],
                        "model": (
                            reviewer_model
                            if function_name == "deepthink.review"
                            else generator_model
                        ),
                        "iteration": int(data.get("iteration") or 0),
                    }
                )
            if function_name == "deepthink.preflight":
                ready = require_deepthink_ready(
                    model=generator_model,
                    model_source=str(params.get("deepthink_model_source") or "conversation"),
                    tools=tools,
                    tool_policy=(
                        params.get("tool_policy")
                        if isinstance(params.get("tool_policy"), dict)
                        else context.get("tool_policy")
                        if isinstance(context.get("tool_policy"), dict)
                        else {"execution": "chat_approval_loop"}
                    ),
                    budgets={
                        "max_tool_calls": params.get("max_tool_calls", 16),
                    },
                )
                emit_phase("deepthink_preflight", generator_model, metadata=ready)
                return ok(ready)
            if function_name == "deepthink.plan":
                output = generator(
                    "deepthink_planner",
                    rumi_process.build_deepthink_planner_messages(
                        messages,
                        context,
                        current_answer="",
                        reviews=[],
                        cycle_label="Flow Plan",
                    ),
                )
                plan = self._parse_plan_with_repair(
                    output,
                    generator_model=generator_model,
                    generator_member=generator_member,
                    params=params,
                    context=context,
                    process=process,
                    label="Flow Plan",
                )
                process["deepthink"]["plan"] = deepcopy(plan)
                return ok(plan)
            if function_name == "deepthink.capability_assessment":
                discovery_ids = list(extension_contract.get("discovery_tools") or [])
                output = generator(
                    "deepthink_capability_assessment",
                    [
                        {
                            "role": "system",
                            "content": (
                                "Assess the actual selected model's task-specific limits "
                                "before choosing a method. Examine relevant prior outputs "
                                "in the conversation, available benchmark results or public "
                                "reports (including community reports), and tool observations "
                                "when accessible. Do not invent scores, sources, model "
                                "abilities, installed software, or past work. State when "
                                "evidence is missing, stale, or not comparable to this task. "
                                "Choose a practical method based on the limits: model, "
                                "host_tool, software, delegate, or clarify. For example, "
                                "recommend a permitted computer-use workflow when direct "
                                "React coding cannot meet the goal. A recommendation never "
                                "installs software or grants tool authority; use only tools "
                                "available in this run and their normal approval path. "
                                "You may call an available discovery tool to look for "
                                "evidence. Otherwise return concise public JSON with keys "
                                "evidence (array of {kind, reference, finding}), "
                                "limitations (array), uncertainties (array), "
                                "evidence_sufficient (boolean), and method "
                                "({approach, reason, fallback}). Never output private "
                                "chain-of-thought."
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "model": generator_model,
                                    "request": messages,
                                    "plan": data.get("plan") or {},
                                    "available_tools": available_tool_catalog(tools),
                                    "available_skills": skill_catalog,
                                },
                                ensure_ascii=False,
                            )[:64_000],
                        },
                    ],
                    phase_tools=selected_tool_definitions(tools, discovery_ids),
                    json_mode=True,
                )
                assessment = normalize_capability_assessment(
                    rumi_process._parse_jsonish(output, {}), model=generator_model
                )
                process["deepthink"]["capability_assessment"] = deepcopy(assessment)
                return ok(assessment)
            if function_name == "deepthink.integrations":
                discovery_ids = list(extension_contract.get("discovery_tools") or [])
                discovery_tools = selected_tool_definitions(tools, discovery_ids)
                output = generator(
                    "deepthink_integrations",
                    [
                        {
                            "role": "system",
                            "content": (
                                "Plan which host-provided tools and profile-visible skills "
                                "are necessary for this DeepThink run. Select only ids from "
                                "the supplied catalogs. Use the capability assessment to "
                                "choose a feasible method and its fallback. Recommend software "
                                "or computer use only when the host exposes that capability; "
                                "selection itself never authorizes an action. Prefer no extra "
                                "tool when it does not "
                                "materially improve correctness. You may call a supplied "
                                "discovery tool when its result is needed. Return one JSON "
                                "object when no discovery call is needed."
                            ),
                        },
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "request": messages,
                                    "plan": data.get("plan") or {},
                                    "capability_assessment": data.get("capability_assessment")
                                    or {},
                                    "available_tools": available_tool_catalog(tools),
                                    "available_skills": skill_catalog,
                                    "discovery_tools": discovery_ids,
                                    "required_tool_ids": params.get("deepthink_selected_tool_ids")
                                    or [],
                                    "required_skill_ids": params.get("deepthink_selected_skill_ids")
                                    or [],
                                    "schema": {
                                        "selected_tool_ids": ["tool id"],
                                        "selected_skill_ids": ["skill id"],
                                        "tool_plan": ["when and why to call a tool"],
                                        "skill_plan": ["how the skill applies"],
                                        "rationale": "public concise rationale",
                                    },
                                },
                                ensure_ascii=False,
                            )[:64_000],
                        },
                    ],
                    phase_tools=discovery_tools,
                    json_mode=True,
                )
                proposed = rumi_process._parse_jsonish(output, {})
                integration_plan = normalize_integration_plan(
                    proposed,
                    tools=tools,
                    skills=skill_catalog,
                    discovery_tools=discovery_ids,
                )
                required_tool_ids = [
                    str(item)
                    for item in (
                        params.get("deepthink_selected_tool_ids")
                        if isinstance(
                            params.get("deepthink_selected_tool_ids"),
                            list,
                        )
                        else []
                    )
                ]
                required_skill_ids = [
                    str(item)
                    for item in (
                        params.get("deepthink_selected_skill_ids")
                        if isinstance(
                            params.get("deepthink_selected_skill_ids"),
                            list,
                        )
                        else []
                    )
                ]
                available_tool_ids = {
                    str(item.get("id") or "") for item in available_tool_catalog(tools)
                }
                available_skill_ids = {str(item.get("id") or "") for item in skill_catalog}
                integration_plan["selected_tool_ids"] = list(
                    dict.fromkeys(
                        [
                            *integration_plan["discovery_tool_ids"],
                            *[item for item in required_tool_ids if item in available_tool_ids],
                            *integration_plan["selected_tool_ids"],
                        ]
                    )
                )
                integration_plan["selected_skill_ids"] = list(
                    dict.fromkeys(
                        [
                            *[item for item in required_skill_ids if item in available_skill_ids],
                            *integration_plan["selected_skill_ids"],
                        ]
                    )
                )
                if callable(callback):
                    callback(
                        {
                            "type": "status",
                            "phase": "deepthink_integrations",
                            "deepthink_phase": "integrations",
                            **presentation_fields(),
                            "message": "tool {}件・skill {}件を選択しました".format(
                                len(integration_plan["selected_tool_ids"]),
                                len(integration_plan["selected_skill_ids"]),
                            ),
                            "trace_id": process["trace_id"],
                            "model": generator_model,
                            "selected_tool_ids": list(integration_plan["selected_tool_ids"]),
                            "selected_skill_ids": list(integration_plan["selected_skill_ids"]),
                        }
                    )
                process["deepthink"]["integration_plan"] = deepcopy(integration_plan)
                return ok(integration_plan)
            if function_name == "deepthink.profile_phases":
                additions, phase_tools = integration_context(data)
                outputs = []
                for phase in extension_contract.get("phases") or []:
                    callback = context.get("activity_event_callback")
                    if callable(callback):
                        callback(
                            {
                                "type": "status",
                                "phase": "deepthink_{}".format(phase["id"]),
                                "deepthink_phase": phase["id"],
                                **presentation_fields(),
                                "phase_label": phase["label"],
                                "message": "{}を実行しています".format(phase["label"]),
                                "trace_id": process["trace_id"],
                                "model": generator_model,
                                "source_pack_id": phase["source_pack_id"],
                            }
                        )
                    output = generator(
                        "deepthink_profile_{}".format(phase["id"]),
                        [
                            {
                                "role": "system",
                                "content": (
                                    phase["prompt"]
                                    + "\nReturn only public, decision-relevant analysis. "
                                    "Never expose hidden chain-of-thought."
                                ),
                            },
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {
                                        "request": messages,
                                        "plan": data.get("plan") or {},
                                        "integrations": data.get("integrations") or {},
                                        "capability_assessment": data.get("capability_assessment")
                                        or {},
                                    },
                                    ensure_ascii=False,
                                )[:48_000],
                            },
                            *additions,
                        ],
                        phase_tools=phase_tools,
                        metadata={
                            "profile_phase_id": phase["id"],
                            "source_pack_id": phase["source_pack_id"],
                        },
                    )
                    outputs.append(
                        {
                            "id": phase["id"],
                            "label": phase["label"],
                            "output": output.strip()[:12_000],
                            "source_pack_id": phase["source_pack_id"],
                        }
                    )
                process["deepthink"]["profile_phase_outputs"] = deepcopy(outputs)
                return ok({"items": outputs})
            if function_name == "deepthink.evidence":
                additions, phase_tools = integration_context(data)
                output = generator(
                    "deepthink_notes",
                    [
                        *rumi_process.build_deepthink_public_notes_messages(
                            messages,
                            data.get("plan") or {},
                            context,
                            attempt=1,
                            existing_notes=[],
                            current_draft="",
                            reviews=[],
                            stage_title="Evidence and public rationale",
                            instruction=(
                                "List only decision-relevant evidence, explicit user constraints, "
                                "uncertainties, and alternative views. Use selected tools when "
                                "fresh or external evidence is materially necessary. Verify "
                                "material model-capability claims before relying on them; "
                                "otherwise preserve their uncertainty and use a fallback. "
                                "Do not infer "
                                "sensitive traits or provide hidden chain-of-thought."
                            ),
                            input_only=True,
                        ),
                        *additions,
                    ],
                    phase_tools=phase_tools,
                )
                note = self._parse_note_with_repair(
                    output,
                    generator_model=generator_model,
                    generator_member=generator_member,
                    params=params,
                    context=context,
                    process=process,
                    label="Flow evidence",
                )
                process["deepthink"]["notes"] = [deepcopy(note)]
                return ok({"notes": [note]})
            if function_name == "deepthink.section_drafts":
                additions, phase_tools = integration_context(data)
                plan = data.get("plan") or {}
                evidence = list((data.get("evidence") or {}).get("notes") or [])
                segments = rumi_process.deepthink_plan_segments(
                    plan,
                    max_sections=max_sections,
                )
                drafts = []
                for index, segment in enumerate(segments, start=1):
                    output = generator(
                        "deepthink_writer",
                        [
                            *rumi_process.build_deepthink_writer_messages(
                                messages,
                                plan,
                                evidence,
                                "",
                                [],
                                draft_number=index,
                                kind="section",
                                stage_title="Section draft {}".format(index),
                                section_title=segment,
                                section_index=index,
                                total_sections=len(segments),
                                section_drafts=drafts,
                            ),
                            *additions,
                        ],
                        metadata={"section": index},
                        phase_tools=phase_tools,
                    )
                    drafts.append(output.strip())
                process["deepthink"]["section_drafts"] = deepcopy(drafts)
                return ok({"items": drafts})
            if function_name == "deepthink.synthesize":
                additions, phase_tools = integration_context(data)
                output = generator(
                    "deepthink_synthesizing",
                    [
                        *rumi_process.build_deepthink_writer_messages(
                            messages,
                            data.get("plan") or {},
                            list((data.get("evidence") or {}).get("notes") or []),
                            "",
                            [],
                            draft_number=1,
                            kind="final",
                            stage_title="Synthesize",
                            section_drafts=list(
                                (data.get("section_drafts") or {}).get("items") or []
                            ),
                        ),
                        *additions,
                    ],
                    phase_tools=phase_tools,
                )
                return ok(output.strip())
            if function_name == "deepthink.review":
                iteration = int(data.get("iteration") or 1)
                response = self._complete(
                    reviewer_model,
                    rumi_process.build_deepthink_reviewer_messages(
                        messages,
                        str(data.get("draft") or ""),
                        grounding_context={
                            "evidence": data.get("evidence") or {},
                            "integrations": data.get("integrations") or {},
                            "profile_phase_outputs": data.get("profile_phase_outputs") or {},
                            "capability_assessment": data.get("capability_assessment") or {},
                        },
                    ),
                    [],
                    self._review_chain_params(
                        reviewer_member,
                        params,
                        context,
                    ),
                )
                record_usage(response)
                review_text = self._response_text(response)
                review, repaired = self._parse_review_json_with_repair(
                    review_text,
                    reviewer_model=reviewer_model,
                    reviewer_member=reviewer_member,
                    params=params,
                    context=context,
                    process=process,
                    label="Flow review {}".format(iteration),
                )
                if review is None:
                    raise ValueError("DeepThink reviewer returned invalid JSON")
                review["needs_revision"] = not bool(review.get("pass"))
                review["iteration"] = iteration
                review["feedback_hash"] = hashlib.sha256(
                    json.dumps(
                        review.get("required_changes") or [],
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
                process["deepthink"]["reviews"].append(deepcopy(review))
                emit_phase(
                    "deepthink_reviewing",
                    reviewer_model,
                    repaired or review_text,
                    {
                        "review_round": iteration,
                        "approved": bool(review.get("pass")),
                        "json_repaired": bool(repaired),
                    },
                )
                return ok(review)
            if function_name == "deepthink.revise":
                additions, phase_tools = integration_context(data)
                output = generator(
                    "deepthink_revising",
                    [
                        *rumi_process.build_deepthink_writer_messages(
                            messages,
                            data.get("plan") or {},
                            list((data.get("evidence") or {}).get("notes") or []),
                            str(data.get("draft") or ""),
                            [data.get("review") or {}],
                            loop_breaker=int(data.get("iteration") or 1) >= 7,
                            draft_number=int(data.get("iteration") or 1) + 1,
                            kind="final",
                            stage_title="Revision {}".format(int(data.get("iteration") or 1)),
                        ),
                        *additions,
                    ],
                    phase_tools=phase_tools,
                )
                revised = output.strip()
                if not revised:
                    revised = str(data.get("draft") or "").strip()
                    emit_phase(
                        "deepthink_revision_fallback",
                        generator_model,
                        metadata={"reason": "empty_revision", "preserved_previous_draft": True},
                    )
                return ok(revised)
            if function_name == "deepthink.finalize":
                review = data.get("review") or {}
                loop = data.get("loop") or {}
                approved = bool(review.get("pass"))
                process["review"] = {
                    "approved": approved,
                    "review_round": int(review.get("iteration") or loop.get("iterations") or 0),
                    "loop_broken": loop.get("reason") == "no_progress",
                    "reason": "" if approved else str(loop.get("reason") or "review_exhausted"),
                    "reviewer_context_excluded_personalization": True,
                }
                return ok(
                    self._text_response(
                        (
                            str(data.get("draft") or "")
                            if approved
                            else rumi_process.RUMI_QUARANTINE_MESSAGE
                        ),
                        "stop" if approved else "review_quarantine",
                        process,
                    )
                )
            return {
                "status": "error",
                "error": {"message": "unknown DeepThink phase"},
            }

        flow_context = dict(context)
        flow_context.update(
            {
                "_flow_function_invoker": invoke,
                "_flow_budgets": {
                    "max_tokens": params.get("deepthink_max_tokens", 300000),
                    "max_cost_usd": params.get("deepthink_max_cost_usd", 0),
                    "timeout_seconds": params.get(
                        "deepthink_timeout_seconds",
                        budget.get("deepthink_timeout_seconds", 21600),
                    ),
                },
                "_flow_loop_max_iterations": {
                    "review_loop": max_iterations,
                },
                "source": "rumi:deepthink",
            }
        )
        requested_flow_run_id = str(params.get("_deepthink_flow_run_id") or "").strip()
        if requested_flow_run_id:
            flow_context["_flow_run_id"] = requested_flow_run_id
        result = FlowEngine().execute(
            "defaultspack.deepthink",
            {"trace_id": process["trace_id"]},
            flow_context,
        )
        process["flow"] = {
            "flow_id": "defaultspack.deepthink",
            "run_id": result.metadata.get("execution_id"),
            "status": result.status,
            "events": result.metadata.get("events", []),
        }
        self._redact_deepthink_event_outputs(process)
        if pending_tool_response is not None:
            process["review"] = {
                "approved": False,
                "quarantined": False,
                "reason": "tool_execution_requested",
                "flow_status": result.status,
            }
            return rumi_process.attach_rumi_metadata(
                pending_tool_response,
                process,
            )
        if result.is_success():
            payload = (
                result.output.get("data") if isinstance(result.output, dict) else result.output
            )
            if isinstance(payload, dict):
                return rumi_process.attach_rumi_metadata(payload, process)
        run_record = FlowEngine().get_run(result.metadata.get("execution_id")) or {}
        stop_reason = str(run_record.get("stop_reason") or "")
        failure_reason = {
            "AuthorityApprovalRequired": "provider_authority_required",
            "ProviderError": "provider_failure",
            "ValueError": "reviewer_json_failure",
            "FLOW_BUDGET_EXCEEDED": "budget_or_timeout_exhausted",
            "FLOW_CANCELLED": "user_cancel",
            "FLOW_PAUSED": "paused",
        }.get(stop_reason, "deepthink_flow_failed")
        process["review"] = {
            "approved": False,
            "quarantined": True,
            "reason": failure_reason,
            "flow_status": result.status,
            "stop_reason": stop_reason,
        }
        return self._text_response(
            rumi_process.RUMI_QUARANTINE_MESSAGE,
            "review_quarantine",
            process,
        )

    @staticmethod
    def _redact_deepthink_event_outputs(process: dict[str, Any]) -> None:
        """Persist phase facts and public notes, never model scratch/output previews."""

        for event in process.get("events") or []:
            if not isinstance(event, dict):
                continue
            if "output_preview" in event:
                event["output_preview"] = ""
                metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
                event["metadata"] = {
                    **metadata,
                    "output_redacted": True,
                }

    def _run_deepthink_chain_legacy(
        self,
        *,
        composite: dict[str, Any],
        generator_member: dict[str, Any],
        reviewer_member: dict[str, Any],
        generator_model: str,
        reviewer_model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        params: dict[str, Any],
        context: dict[str, Any],
        process: dict[str, Any],
        max_reviews: int,
    ) -> dict[str, Any]:
        budget = composite.get("budget") if isinstance(composite.get("budget"), dict) else {}
        max_iterations = self._positive_int(
            params.get("deepthink_max_review_iterations")
            or budget.get("deepthink_max_review_iterations")
            or max_reviews,
            default=max_reviews,
            upper=8,
        )
        user_rejection_cycles = self._nonnegative_int(
            params.get("deepthink_user_rejection_review_cycles")
            if "deepthink_user_rejection_review_cycles" in params
            else budget.get("deepthink_user_rejection_review_cycles"),
            default=2,
            upper=2,
        )
        max_sections = self._positive_int(
            params.get("deepthink_max_sections")
            or budget.get("deepthink_max_sections")
            or rumi_process.RUMI_DEEPTHINK_MAX_SECTIONS,
            default=rumi_process.RUMI_DEEPTHINK_MAX_SECTIONS,
            upper=rumi_process.RUMI_DEEPTHINK_MAX_SECTIONS,
        )
        process["watchdog"].update(
            {
                "max_review_rounds": max_iterations,
                "deepthink_user_rejection_review_cycles": user_rejection_cycles,
                "deepthink_max_sections": max_sections,
            }
        )
        process["deepthink"] = {
            "enabled": True,
            "source": rumi_process.RUMI_DEEPTHINK_SOURCE,
            "warning": rumi_process.RUMI_DEEPTHINK_WARNING_JA,
            "harness_tool_selection": deepcopy(context.get("harness_tool_selection", {})),
            "plan": {},
            "notes": [],
            "reviews": [],
            "section_drafts": [],
        }

        current_answer = ""
        current_plan: dict[str, Any] = {}
        notes: list[dict[str, Any]] = []
        reviews: list[dict[str, Any]] = []
        seen_review_hashes: set[str] = set()
        planner_attempt = 0
        note_attempt = 0
        draft_attempt = 0
        review_attempt = 0
        user_rejection_cycles_done = 0

        def call_generator(
            phase, label, phase_messages, *, event_metadata=None, loop_breaker=False
        ):
            next_params = self._review_chain_params(generator_member, params, context)
            if loop_breaker:
                next_params["deepthink_loop_breaker"] = True
            response = self._complete(generator_model, phase_messages, tools, next_params)
            output = self._response_text(response)
            metadata = {"label": label}
            if event_metadata:
                metadata.update(event_metadata)
            process["events"].append(
                rumi_process.phase_event(phase, generator_model, output=output, metadata=metadata)
            )
            if rumi_process.response_has_tool_calls(response):
                return output, self._deferred_response(response, process, phase, label)
            return output, None

        def call_reviewer(phase_messages, *, label, attempt):
            response = self._complete(
                reviewer_model,
                phase_messages,
                [],
                self._review_chain_params(reviewer_member, params, context),
            )
            output = self._response_text(response)
            process["events"].append(
                rumi_process.phase_event(
                    "reviewer",
                    reviewer_model,
                    output=output,
                    metadata={"label": label, "review_round": attempt},
                )
            )
            return output

        def create_plan(label):
            nonlocal planner_attempt, current_plan
            planner_attempt += 1
            output, deferred = call_generator(
                "deepthink_planner",
                label,
                rumi_process.build_deepthink_planner_messages(
                    messages,
                    context,
                    current_answer=current_answer,
                    reviews=reviews,
                    cycle_label=label,
                ),
                event_metadata={"attempt": planner_attempt},
            )
            if deferred is not None:
                return deferred
            current_plan = self._parse_plan_with_repair(
                output,
                generator_model=generator_model,
                generator_member=generator_member,
                params=params,
                context=context,
                process=process,
                label=label,
            )
            process["deepthink"]["plan"] = deepcopy(current_plan)
            return None

        def add_thinking_step(
            label, instruction, *, segment_title="", section_drafts=None, input_only=False
        ):
            nonlocal note_attempt, notes
            note_attempt += 1
            output, deferred = call_generator(
                "deepthink_notes",
                label,
                rumi_process.build_deepthink_public_notes_messages(
                    messages,
                    current_plan,
                    context,
                    attempt=note_attempt,
                    existing_notes=notes,
                    current_draft=current_answer,
                    reviews=reviews,
                    stage_title=label,
                    instruction=instruction,
                    segment_title=segment_title,
                    section_drafts=section_drafts or [],
                    input_only=input_only,
                ),
                event_metadata={"attempt": note_attempt},
            )
            if deferred is not None:
                return deferred
            note = self._parse_note_with_repair(
                output,
                generator_model=generator_model,
                generator_member=generator_member,
                params=params,
                context=context,
                process=process,
                label=label,
            )
            notes.append(note)
            process["deepthink"]["notes"] = deepcopy(notes)
            return None

        def write_draft(
            label,
            *,
            kind,
            section_title="",
            section_index=None,
            total_sections=None,
            section_drafts=None,
            loop_breaker=False,
        ):
            nonlocal draft_attempt, current_answer
            draft_attempt += 1
            output, deferred = call_generator(
                "deepthink_writer",
                label,
                rumi_process.build_deepthink_writer_messages(
                    messages,
                    current_plan,
                    notes,
                    current_answer,
                    reviews,
                    loop_breaker=loop_breaker,
                    draft_number=draft_attempt,
                    kind=kind,
                    stage_title=label,
                    section_title=section_title,
                    section_index=section_index,
                    total_sections=total_sections,
                    section_drafts=section_drafts or [],
                ),
                event_metadata={"attempt": draft_attempt, "kind": kind},
                loop_breaker=loop_breaker,
            )
            if deferred is not None:
                return "", deferred
            draft = output.strip()
            if kind == "final":
                current_answer = draft
            return draft, None

        def build_candidate(cycle_label, *, loop_breaker=False):
            section_drafts: list[str] = []
            steps = [
                (
                    f"{cycle_label}: 1 DeepThink harness tool selection",
                    (
                        "Decide which Rumi harness tools will be used next. Keep harness tools separate from provider/model tools. "
                        "If vision_tool_ids is empty, explicitly say that zoom/crop vision tools will not be used."
                    ),
                    True,
                ),
                (
                    f"{cycle_label}: 入力読み取り",
                    (
                        "Predict the user's likely intent, unstated constraints, possible dissatisfaction, and alternative readings from the input. "
                        "Use many assumptions, keep each item short, and attach rough probabilities to each hypothesis. Include playful intent, "
                        "benchmark/testing intent, hidden format constraints, and even fringe but plausible readings down to about 1% if they would change the strategy."
                    ),
                    True,
                ),
                (
                    f"{cycle_label}: ユーザー背景agent",
                    (
                        "Infer the user's background, skill level, likely domain knowledge, preferred detail level, emotional state, interests, habits, "
                        "and what kind of answer would feel immediately useful. Predict user-information hypotheses such as AI好き, 未来予測好き, "
                        "benchmark説, testing説, student説, practitioner説, or playful prompt説 when plausible. Attach rough probabilities to every assumption, "
                        "including rare but meaningful 1% hypotheses, and adapt the answer strategy without asking the user."
                    ),
                    True,
                ),
                (
                    f"{cycle_label}: 多視点agent",
                    (
                        "Choose 5 agents/perspectives from the input only. For each, state what it will notice, including whether the answer is too conservative, "
                        "too vague, too literal, or mismatched. Let the chosen perspectives reflect the input; add rough probabilities to their underlying assumptions when useful."
                    ),
                    True,
                ),
                (
                    f"{cycle_label}: 分割設計",
                    "Split the tentative answer into the planned sections and define what each section must solve before merging.",
                    False,
                ),
            ]
            for label, instruction, input_only in steps:
                deferred = add_thinking_step(label, instruction, input_only=input_only)
                if deferred is not None:
                    return deferred

            segments = rumi_process.deepthink_plan_segments(current_plan, max_sections=max_sections)
            for index, segment in enumerate(segments, start=1):
                deferred = add_thinking_step(
                    f"{cycle_label}: 部分{index} 思考",
                    "For this section only, predict missing specifics, likely objections, and the best compact content. Do not draft other sections.",
                    segment_title=segment,
                    section_drafts=section_drafts,
                )
                if deferred is not None:
                    return deferred
                section_draft, deferred = write_draft(
                    f"{cycle_label}: 部分{index} 仮解答",
                    kind="section",
                    section_title=segment,
                    section_index=index,
                    total_sections=len(segments),
                    section_drafts=section_drafts,
                )
                if deferred is not None:
                    return deferred
                section_drafts.append(section_draft)

            deferred = add_thinking_step(
                f"{cycle_label}: 確率偏重レビューagent",
                (
                    "Review the tentative section drafts before merging. Check whether the drafts are trapped by probability estimates, false precision, "
                    "or majority-likely readings. Probabilities are only hypotheses, not a reason to ignore low-probability but high-impact interpretations. "
                    "Identify corrections that keep the answer imaginative, robust, and user-fit without overusing numeric probability in the final answer."
                ),
                section_drafts=section_drafts,
            )
            if deferred is not None:
                return deferred
            deferred = add_thinking_step(
                f"{cycle_label}: 結合思考",
                "Plan how to merge the section drafts. Remove contradictions, add metacognitive checks, predict unclear points, and fix them without asking the user.",
                section_drafts=section_drafts,
            )
            if deferred is not None:
                return deferred
            _final_draft, deferred = write_draft(
                f"{cycle_label}: Final候補",
                kind="final",
                section_drafts=section_drafts,
                loop_breaker=loop_breaker,
            )
            process["deepthink"]["section_drafts"] = deepcopy(section_drafts)
            return deferred

        deferred = create_plan("初回Plan")
        if deferred is not None:
            return deferred
        deferred = build_candidate("初回")
        if deferred is not None:
            return deferred

        while review_attempt < max_iterations:
            review_attempt += 1
            try:
                review_output = call_reviewer(
                    rumi_process.build_deepthink_reviewer_messages(messages, current_answer),
                    label=f"Finalレビュー {review_attempt}",
                    attempt=review_attempt,
                )
                review_event_index = len(process["events"]) - 1
                review, repaired = self._parse_review_json_with_repair(
                    review_output,
                    reviewer_model=reviewer_model,
                    reviewer_member=reviewer_member,
                    params=params,
                    context=context,
                    process=process,
                    label=f"DeepThink final review {review_attempt}",
                )
                if review is None:
                    raise ValueError("DeepThink reviewer returned unparseable JSON")
                process["events"][review_event_index]["metadata"]["json_repaired"] = bool(repaired)
            except Exception as exc:
                process["events"].append(
                    rumi_process.phase_event(
                        "reviewer_error",
                        reviewer_model,
                        output=str(exc),
                        metadata={
                            "error_kind": self._error_kind(exc),
                            "review_round": review_attempt,
                        },
                    )
                )
                process["review"] = {
                    "approved": False,
                    "quarantined": True,
                    "reason": "reviewer_failed",
                }
                return self._text_response(current_answer, "review_quarantine", process)

            reviews.append(review)
            process["deepthink"]["reviews"] = deepcopy(reviews)
            process["events"][review_event_index]["metadata"]["approved"] = bool(review.get("pass"))
            if not review.get("pass"):
                review_hash = rumi_process.hash_required_changes(review.get("required_changes", []))
                if review_hash in seen_review_hashes:
                    deferred = create_plan("循環停止Plan")
                    if deferred is not None:
                        return deferred
                    deferred = build_candidate("循環停止", loop_breaker=True)
                    if deferred is not None:
                        return deferred
                    process["review"] = {
                        "approved": False,
                        "loop_broken": True,
                        "reason": "reviewer_feedback_loop_detected",
                    }
                    return self._text_response(current_answer, "loop_broken", process)
                seen_review_hashes.add(review_hash)
                if review_attempt >= max_iterations:
                    break
                deferred = create_plan(f"Finalレビュー{review_attempt}後Plan")
                if deferred is not None:
                    return deferred
                deferred = build_candidate(f"Finalレビュー{review_attempt}後")
                if deferred is not None:
                    return deferred
                continue

            if user_rejection_cycles_done < user_rejection_cycles:
                try:
                    rejection_output = call_reviewer(
                        rumi_process.build_deepthink_user_rejection_review_messages(
                            messages, current_answer
                        ),
                        label=f"ユーザー差し戻しレビュー {user_rejection_cycles_done + 1}",
                        attempt=900 + user_rejection_cycles_done + 1,
                    )
                    rejection_review, _repaired = self._parse_review_json_with_repair(
                        rejection_output,
                        reviewer_model=reviewer_model,
                        reviewer_member=reviewer_member,
                        params=params,
                        context=context,
                        process=process,
                        label=f"user rejection review {user_rejection_cycles_done + 1}",
                    )
                    if rejection_review is None:
                        raise ValueError(
                            "DeepThink user rejection reviewer returned unparseable JSON"
                        )
                    rejection_review = rumi_process.enforce_user_rejection_review(rejection_review)
                    reviews.append(rejection_review)
                    process["deepthink"]["reviews"] = deepcopy(reviews)
                except Exception as exc:
                    process["events"].append(
                        rumi_process.phase_event(
                            "reviewer_error",
                            reviewer_model,
                            output=str(exc),
                            metadata={
                                "error_kind": self._error_kind(exc),
                                "review_round": 900 + user_rejection_cycles_done + 1,
                            },
                        )
                    )
                    process["review"] = {
                        "approved": False,
                        "quarantined": True,
                        "reason": "user_rejection_review_failed",
                    }
                    return self._text_response(current_answer, "review_quarantine", process)
                user_rejection_cycles_done += 1
                if review_attempt >= max_iterations:
                    break
                deferred = create_plan(f"ユーザー差し戻し後Plan {user_rejection_cycles_done}")
                if deferred is not None:
                    return deferred
                deferred = build_candidate(f"ユーザー差し戻し後{user_rejection_cycles_done}")
                if deferred is not None:
                    return deferred
                continue

            process["review"] = {
                "approved": True,
                "review_round": review_attempt,
                "user_rejection_cycles": user_rejection_cycles_done,
                "reviewer_context_excluded_personalization": True,
            }
            return self._text_response(current_answer, "stop", process)

        process["review"] = {
            "approved": False,
            "quarantined": True,
            "reason": "deepthink_watchdog_max_review_iterations",
            "last_review": reviews[-1] if reviews else {},
        }
        return self._text_response(current_answer, "review_quarantine", process)

    def _parse_plan_with_repair(
        self,
        text: str,
        *,
        generator_model: str,
        generator_member: dict[str, Any],
        params: dict[str, Any],
        context: dict[str, Any],
        process: dict[str, Any],
        label: str,
    ) -> dict[str, Any]:
        parsed = rumi_process.parse_deepthink_plan_strict(text)
        if parsed is not None:
            return parsed
        repaired = self._repair_json(
            '{"structure": string[], "key_points": string[], "risks": string[]}',
            text,
            model=generator_model,
            member=generator_member,
            params=params,
            context=context,
            process=process,
            label=f"{label}: planner JSON repair",
        )
        parsed = rumi_process.parse_deepthink_plan_strict(repaired or "")
        if parsed is not None:
            return parsed
        return rumi_process.parse_deepthink_plan(text)

    def _parse_note_with_repair(
        self,
        text: str,
        *,
        generator_model: str,
        generator_member: dict[str, Any],
        params: dict[str, Any],
        context: dict[str, Any],
        process: dict[str, Any],
        label: str,
    ) -> dict[str, str]:
        parsed = rumi_process.parse_deepthink_note_strict(text)
        if parsed is not None:
            return parsed
        repaired = self._repair_json(
            '{"thinking": string, "output": string}',
            text,
            model=generator_model,
            member=generator_member,
            params=params,
            context=context,
            process=process,
            label=f"{label}: public note JSON repair",
        )
        parsed = rumi_process.parse_deepthink_note_strict(repaired or "")
        if parsed is not None:
            return parsed
        return rumi_process.parse_deepthink_note(text)

    def _parse_review_json_with_repair(
        self,
        text: str,
        *,
        reviewer_model: str,
        reviewer_member: dict[str, Any],
        params: dict[str, Any],
        context: dict[str, Any],
        process: dict[str, Any],
        label: str,
    ) -> tuple[dict[str, Any] | None, str]:
        parsed = rumi_process.parse_deepthink_review_strict(text)
        if parsed is not None:
            return parsed, ""
        repaired = self._repair_json(
            '{"pass": boolean, "score": number, "issues": string[], "required_changes": string[]}',
            text,
            model=reviewer_model,
            member=reviewer_member,
            params=params,
            context=context,
            process=process,
            label=f"{label}: reviewer JSON repair",
        )
        parsed = rumi_process.parse_deepthink_review_strict(repaired or "")
        if parsed is not None:
            return parsed, repaired
        # A malformed reviewer response must not abort the durable flow. Treat
        # it as a conservative failed review so the normal revision/checkpoint
        # loop can recover on the next iteration. This keeps invalid model
        # output fail-closed without turning a transient formatting failure
        # into a terminal DeepThink error.
        fallback = rumi_process.sanitize_deepthink_review(
            {
                "pass": False,
                "score": 0,
                "issues": ["The reviewer response could not be parsed as valid JSON."],
                "required_changes": [
                    "Re-check the answer against the original request and produce a complete revision."
                ],
            }
        )
        process["events"].append(
            rumi_process.phase_event(
                "review_json_fallback",
                reviewer_model,
                metadata={"label": label, "fail_closed": True},
            )
        )
        return fallback, repaired

    def _repair_json(
        self,
        schema_hint: str,
        broken_text: str,
        *,
        model: str,
        member: dict[str, Any],
        params: dict[str, Any],
        context: dict[str, Any],
        process: dict[str, Any],
        label: str,
    ) -> str:
        try:
            response = self._complete(
                model,
                rumi_process.build_json_repair_messages(schema_hint, broken_text),
                [],
                self._review_chain_params(member, params, context),
            )
        except Exception as exc:
            process["events"].append(
                rumi_process.phase_event(
                    "json_repair_error",
                    model,
                    output=str(exc),
                    metadata={"label": label, "error_kind": self._error_kind(exc)},
                )
            )
            return ""
        output = self._response_text(response)
        process["events"].append(
            rumi_process.phase_event("json_repair", model, output=output, metadata={"label": label})
        )
        if rumi_process.response_has_tool_calls(response):
            return ""
        return output

    @staticmethod
    def _positive_int(value: Any, *, default: int = 1, upper: int = 10) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(1, min(int(upper), parsed))

    @staticmethod
    def _nonnegative_int(value: Any, *, default: int = 0, upper: int = 10) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(0, min(int(upper), parsed))

    @staticmethod
    def _review_chain_params(
        member: dict[str, Any], params: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        next_params = dict(params or {})
        next_params["_composite_depth"] = int(next_params.get("_composite_depth", 0) or 0) + 1
        next_params.setdefault("rumi_mode", context.get("mode", "deep"))
        if context.get("mode") == "deepthink":
            try:
                current_timeout = float(next_params.get("request_timeout") or 0)
            except (TypeError, ValueError):
                current_timeout = 0.0
            try:
                deepthink_timeout = float(context.get("deepthink_request_timeout_seconds") or 1800)
            except (TypeError, ValueError):
                deepthink_timeout = 1800.0
            next_params["request_timeout"] = max(
                30.0,
                min(21600.0, max(current_timeout, deepthink_timeout)),
            )
            next_params.setdefault("request_retries", 3)
        metadata = (
            member.get("metadata")
            if isinstance(member, dict) and isinstance(member.get("metadata"), dict)
            else {}
        )
        thinking_level = str(
            metadata.get("thinking_level") or context.get("default_thinking_level") or ""
        ).strip()
        if thinking_level and not next_params.get("thinking_level"):
            next_params["thinking_level"] = thinking_level
        return next_params

    @staticmethod
    def _deferred_response(response: Any, process: dict[str, Any], phase: str, label: str) -> Any:
        process["review"] = {
            "deferred": True,
            "reason": "generator_returned_tool_calls",
            "phase": phase,
            "label": label,
            "model_tools_are_separate_from_harness_tools": True,
        }
        return rumi_process.attach_rumi_metadata(response, process)

    @staticmethod
    def _text_response(text: str, finish_reason: str, process: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": str(text or "").strip()}],
            "finish_reason": finish_reason,
            "usage": {},
            "metadata": {"rumi_process": process},
        }

    def _quarantine_unmarked_draft(
        self,
        process: dict[str, Any],
        *,
        phase: str,
        review_round: int | None = None,
    ) -> dict[str, Any]:
        process["review"] = {
            "approved": False,
            "quarantined": True,
            "reason": "missing_final_response_marker",
            "phase": phase,
        }
        if review_round is not None:
            process["review"]["review_round"] = review_round
        return self._text_response(
            rumi_process.RUMI_QUARANTINE_MESSAGE, "draft_quarantine", process
        )
