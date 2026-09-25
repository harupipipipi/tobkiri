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

        def call_generator(phase, label, phase_messages, *, event_metadata=None, loop_breaker=False):
            next_params = self._review_chain_params(generator_member, params, context)
            if loop_breaker:
                next_params["deepthink_loop_breaker"] = True
            response = self._complete(generator_model, phase_messages, tools, next_params)
            output = self._response_text(response)
            metadata = {"label": label}
            if event_metadata:
                metadata.update(event_metadata)
            process["events"].append(rumi_process.phase_event(phase, generator_model, output=output, metadata=metadata))
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

        def add_thinking_step(label, instruction, *, segment_title="", section_drafts=None, input_only=False):
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

        def write_draft(label, *, kind, section_title="", section_index=None, total_sections=None, section_drafts=None, loop_breaker=False):
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
                        "including rare but meaningful 1% hypotheses, and adapt the answer strategy without asking the user. "
                        + rumi_process.RUMI_USER_BACKGROUND_CONSTRAINT
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
                        metadata={"error_kind": self._error_kind(exc), "review_round": review_attempt},
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
                        rumi_process.build_deepthink_user_rejection_review_messages(messages, current_answer),
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
                        raise ValueError("DeepThink user rejection reviewer returned unparseable JSON")
                    rejection_review = rumi_process.enforce_user_rejection_review(rejection_review)
                    reviews.append(rejection_review)
                    process["deepthink"]["reviews"] = deepcopy(reviews)
                except Exception as exc:
                    process["events"].append(
                        rumi_process.phase_event(
                            "reviewer_error",
                            reviewer_model,
                            output=str(exc),
                            metadata={"error_kind": self._error_kind(exc), "review_round": 900 + user_rejection_cycles_done + 1},
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
        return None, repaired

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
    def _nonnegative_int(value: Any, *, default: int = 0, upper: int = 10) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(0, min(int(upper), parsed))

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
