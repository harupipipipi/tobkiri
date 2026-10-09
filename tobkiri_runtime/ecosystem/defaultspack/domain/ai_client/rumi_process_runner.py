from __future__ import annotations

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
            process["events"].append(rumi_process.phase_event(phase, generator_model, output=generated_text))
            if rumi_process.response_has_tool_calls(response):
                process["review"] = {
                    "deferred": True,
                    "reason": "generator_returned_tool_calls",
                    "review_round": review_index + 1,
                }
                return rumi_process.attach_rumi_metadata(response, process)
            next_draft = rumi_process.extract_draft_response(generated_text)
            if next_draft is None:
                return self._quarantine_unmarked_draft(process, phase=phase, review_round=review_index + 1)
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
                return self._text_response(rumi_process.RUMI_QUARANTINE_MESSAGE, "review_quarantine", process)

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
                return self._text_response(rumi_process.RUMI_QUARANTINE_MESSAGE, "review_quarantine", process)
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
        return self._text_response(rumi_process.RUMI_QUARANTINE_MESSAGE, "review_quarantine", process)

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
        process["events"].append(rumi_process.phase_event("json_repair", model, output=output, metadata={"label": label}))
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
    def _review_chain_params(member: dict[str, Any], params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        next_params = dict(params or {})
        next_params["_composite_depth"] = int(next_params.get("_composite_depth", 0) or 0) + 1
        next_params.setdefault("rumi_mode", context.get("mode", "deep"))
        metadata = member.get("metadata") if isinstance(member, dict) and isinstance(member.get("metadata"), dict) else {}
        thinking_level = str(metadata.get("thinking_level") or context.get("default_thinking_level") or "").strip()
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
        return self._text_response(rumi_process.RUMI_QUARANTINE_MESSAGE, "draft_quarantine", process)
