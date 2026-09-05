from __future__ import annotations

import time
from typing import Any

from blocks._common import gen_id
from domain.ai_client.model_search import search_models
from domain.capability.activity_registry import ActivityRegistry
from domain.capability.settings import normalize_capability_settings
from domain.chat.tool_embedding_index import ToolEmbeddingIndex
from domain.chat.tool_recommender import recommend_tool_ids
from domain.chat.tool_selection_orchestrator import ToolSelectionOrchestrator
from domain.chat.tool_selection_schema import (
    COMPUTER_TOOL_IDS,
    TOOL_SELECTION_STRATEGIES,
    ToolRecommendation,
    ToolSelectionDecision,
    ToolTarget,
    normalize_tool_targets,
)
from domain.tool.permission_resolver import ToolPermissionResolver, read_frontend_settings
from domain.tool.service_catalog import (
    ToolServiceCatalog,
    infer_connection_status,
    requires_explicit_intent,
)
from domain.tool.schema_adapter import tool_name_from_definition


DEFAULT_SEMANTIC_CANDIDATE_LIMIT = 32
DEFAULT_FINAL_TOOL_LIMIT = 8
DEFAULT_CATALOG_AI_DIRECT_LIMIT = 80


class ToolSelectionService:
    def __init__(self, *, call_handler: Any = None, settings: dict[str, Any] | None = None) -> None:
        self._call_handler = call_handler
        self._settings = settings if isinstance(settings, dict) else read_frontend_settings()
        self._tool_settings = self._settings.get("tools") if isinstance(self._settings.get("tools"), dict) else {}

    def select(
        self,
        user_text: str,
        tools: list[dict[str, Any]],
        *,
        selection: Any,
        context: dict[str, Any] | None = None,
    ) -> ToolSelectionDecision:
        started = time.perf_counter()
        context = context if isinstance(context, dict) else {}
        conversation_preferences = context.get("conversation_tool_preferences")
        conversation_preferences = conversation_preferences if isinstance(conversation_preferences, dict) else {}
        mode = str(getattr(selection, "mode", "auto") or "auto").strip().lower()
        conversation_mode = str(conversation_preferences.get("mode") or "").strip().lower()
        selection_include = normalize_tool_targets(getattr(selection, "include", []))
        selection_exclude = normalize_tool_targets(getattr(selection, "exclude", []))
        selection_has_turn_targets = bool(selection_include or selection_exclude)
        selection_scope = str(getattr(selection, "scope", "turn") or "turn").strip().lower()
        selection_source = str(getattr(selection, "source", "default") or "default").strip()
        if conversation_mode in {"auto", "review", "manual", "none"} and (
            selection_source == "default"
            or (selection_scope == "turn" and not selection_has_turn_targets and mode in {"auto", "review"})
        ):
            mode = conversation_mode
        if mode not in {"auto", "review", "manual", "none"}:
            mode = "auto"
        capability_settings = normalize_capability_settings(self._settings)
        if not capability_settings["capabilities"]["enabled"]:
            return self._decision(
                mode="none",
                strategy="master_gate",
                stage="capabilities_disabled",
                selected=[],
                eligible=[],
                candidates=[],
                started=started,
                metrics={"capability_master_enabled": False},
            )
        strategy = str(getattr(selection, "strategy", "") or self._tool_settings.get("selection_strategy") or "hybrid").strip().lower()
        if strategy not in TOOL_SELECTION_STRATEGIES:
            strategy = "hybrid"
        if strategy in {"all_schemas", "all_with_hints", "catalog_ai"} and not _developer_capability(context):
            raise PermissionError(
                "{} requires the developer capability".format(strategy)
            )
        conversation_include = normalize_tool_targets(conversation_preferences.get("include"))
        conversation_exclude = normalize_tool_targets(conversation_preferences.get("exclude"))
        include = _merge_targets(conversation_include, selection_include)
        exclude = _merge_targets(conversation_exclude, selection_exclude)
        verified_explicit_tool_ids = {
            str(item).strip()
            for item in context.get("verified_explicit_tool_ids", [])
            if str(item or "").strip()
        }
        unverified_low_level_targets = [
            target
            for target in [*include, *exclude]
            if target.kind in {"tool", "skill"}
            and not (
                target in include
                and target.kind == "tool"
                and target.id in verified_explicit_tool_ids
            )
        ]
        if (
            not _developer_capability(context)
            and unverified_low_level_targets
        ):
            raise PermissionError(
                "raw Tool and Skill targets require the developer capability"
            )
        if mode == "none":
            return self._decision(
                mode=mode,
                strategy=strategy,
                stage="none",
                selected=[],
                eligible=[],
                candidates=[],
                started=started,
            )

        activity_registry = ActivityRegistry()
        mentioned_activities = activity_registry.resolve_mentions(user_text)
        structured_activity_ids = [
            target.id for target in include if target.kind == "activity"
        ]
        explicit_activity_ids = _unique_ids(
            [
                *structured_activity_ids,
                *(item.activity_id for item in mentioned_activities),
            ]
        )
        activity_ids = list(explicit_activity_ids)
        if not activity_ids and mode in {"auto", "review"}:
            activity_ids = [
                item.activity_id
                for item in activity_registry.infer(user_text, limit=3)
            ]
        activity_expansion = activity_registry.expand(activity_ids, tools)
        activity_candidate_ids = (
            set(activity_expansion["tool_ids"])
            if explicit_activity_ids
            else set()
        )
        if activity_ids:
            context["capability_activity_ids"] = list(activity_ids)
            context["required_skills"] = list(
                activity_expansion["required_skills"]
            )
            context["safety_skills"] = list(activity_expansion["safety_skills"])
            if "computer" in activity_ids:
                context["user_requested_computer_use"] = True

        catalog = ToolServiceCatalog(tools)
        resolver = ToolPermissionResolver(self._settings)
        permission_allowed, permission_entries = resolver.filter_blocked(tools, context=context)
        explicit_ids = {target.id for target in include if target.kind == "tool"}
        explicit_service_ids = {target.id for target in include if target.kind == "service"}
        eligible = [
            tool
            for tool in permission_allowed
            if self._static_eligible(tool, user_text=user_text, context=context, explicit_tool_ids=explicit_ids, explicit_service_ids=explicit_service_ids, catalog=catalog)
        ]
        if activity_candidate_ids:
            eligible = [
                tool
                for tool in eligible
                if _tool_id(tool) in activity_candidate_ids
                or _tool_id(tool) in explicit_ids
            ]
        eligible = self._apply_excludes(eligible, exclude, catalog)
        low_level_include = [
            target for target in include if target.kind in {"tool", "service"}
        ]
        low_level_exclude = [
            target for target in exclude if target.kind in {"tool", "service"}
        ]
        included, unknown_targets = self._expand_targets(
            low_level_include, eligible, catalog
        )
        included = self._apply_excludes(
            included, low_level_exclude, catalog
        )

        if mode == "manual":
            selected = included
            if not selected and not include:
                selected = []
            return self._decision(
                mode=mode,
                strategy="manual",
                stage="manual",
                selected=selected,
                eligible=eligible,
                candidates=selected,
                started=started,
                unknown_targets=unknown_targets,
                permission_entries=permission_entries,
            )

        if strategy == "all_schemas":
            selected = self._stable_merge(included, eligible)
            return self._decision(
                mode=mode,
                strategy=strategy,
                stage="all_schemas",
                selected=selected,
                eligible=eligible,
                candidates=eligible,
                started=started,
                unknown_targets=unknown_targets,
                permission_entries=permission_entries,
                recommendations=[],
            )

        if strategy == "all_with_hints":
            hints = self._semantic_candidates(user_text, eligible, context=context)
            hint_selected_ids, hint_stage, hint_fallbacks, hint_recommendations, hint_selector_model = self._select_with_utility_model(
                user_text,
                eligible,
                strategy=strategy,
                fallback_ids=list(hints.get("tool_ids") or []),
                context=context,
                prefilter=False,
            )
            if not hint_recommendations:
                hint_recommendations = [
                    ToolRecommendation(tool_id=tool_id, confidence=0.5, reason="semantic hint")
                    for tool_id in list(hints.get("tool_ids") or [])[: self._final_limit()]
                ]
            selected = self._stable_merge(included, eligible)
            return self._decision(
                mode=mode,
                strategy=strategy,
                stage="all_with_hints",
                selected=selected,
                eligible=eligible,
                candidates=eligible,
                started=started,
                unknown_targets=unknown_targets,
                permission_entries=permission_entries,
                fallbacks=[*list(hints.get("fallbacks", [])), *hint_fallbacks],
                recommendations=hint_recommendations,
                metrics={
                    "recommendation_order": hint_selected_ids or list(hints.get("tool_ids", [])),
                    "recommended_tools": [item.to_dict() for item in hint_recommendations],
                    "hint_stage": hint_stage,
                    **({"selector_model": hint_selector_model} if hint_selector_model else {}),
                },
                cache_hit=bool(hints.get("cache_hit")),
            )

        if strategy == "lexical":
            lexical_ids = recommend_tool_ids(user_text, eligible, limit=self._final_limit(), threshold=0.0)
            candidates = self._tools_by_ids(eligible, lexical_ids)
            selected = self._stable_merge(included, candidates)[: self._final_limit()]
            return self._decision(
                mode=mode,
                strategy=strategy,
                stage="lexical",
                selected=selected,
                eligible=eligible,
                candidates=candidates,
                started=started,
                unknown_targets=unknown_targets,
                permission_entries=permission_entries,
            )
        semantic = self._semantic_candidates(user_text, eligible, context=context)
        semantic_ids = list(semantic.get("tool_ids") or [])
        semantic_candidates = self._tools_by_ids(eligible, semantic_ids)
        if strategy == "semantic":
            selected = self._stable_merge(included, semantic_candidates)[: self._final_limit()]
            return self._decision(
                mode=mode,
                strategy=strategy,
                stage=str(semantic.get("stage") or "semantic"),
                selected=selected,
                eligible=eligible,
                candidates=semantic_candidates,
                started=started,
                unknown_targets=unknown_targets,
                permission_entries=permission_entries,
                fallbacks=list(semantic.get("fallbacks", [])),
                cache_hit=bool(semantic.get("cache_hit")),
                metrics={"semantic_search_ms": semantic.get("duration_ms", 0), "catalog_hash": semantic.get("catalog_hash", "")},
            )

        if strategy == "catalog_ai":
            candidates = eligible
            selector_prefilter = False
            selector_fallback_ids = [_tool_id(tool) for tool in eligible]
        else:
            candidates = self._stable_merge(included, semantic_candidates)
            selector_prefilter = True
            selector_fallback_ids = semantic_ids
        selected_ids, stage, fallbacks, recommendations, selector_model = self._select_with_utility_model(
            user_text,
            candidates,
            strategy=strategy,
            fallback_ids=selector_fallback_ids,
            context=context,
            prefilter=selector_prefilter,
        )
        selected = self._stable_merge(included, self._tools_by_ids(eligible, selected_ids))[: self._final_limit()]
        if not selected and semantic_candidates:
            fallbacks.append({"stage": "semantic", "reason": "selector_returned_no_valid_tools"})
            selected = self._stable_merge(included, semantic_candidates)[: self._final_limit()]
            recommendations = [
                ToolRecommendation(tool_id=_tool_id(tool), confidence=0.5, reason="semantic fallback")
                for tool in selected
            ]
            stage = "semantic_fallback"
        return self._decision(
            mode=mode,
            strategy=strategy,
            stage=stage,
            selected=selected,
            eligible=eligible,
            candidates=candidates,
            started=started,
            unknown_targets=unknown_targets,
            permission_entries=permission_entries,
            fallbacks=[*list(semantic.get("fallbacks", [])), *fallbacks],
            recommendations=recommendations,
            cache_hit=bool(semantic.get("cache_hit")),
            metrics={
                "semantic_search_ms": semantic.get("duration_ms", 0),
                "catalog_hash": semantic.get("catalog_hash", ""),
                **({"selector_model": selector_model} if selector_model else {}),
            },
        )

    def _static_eligible(
        self,
        tool: dict[str, Any],
        *,
        user_text: str,
        context: dict[str, Any],
        explicit_tool_ids: set[str],
        explicit_service_ids: set[str],
        catalog: ToolServiceCatalog,
    ) -> bool:
        if infer_connection_status(tool) in {"setup_required", "unavailable", "error"}:
            return False
        tool_id = _tool_id(tool)
        service_id = catalog.compact_record(tool)["service_id"]
        if requires_explicit_intent(tool):
            if tool_id in explicit_tool_ids or service_id in explicit_service_ids:
                return True
            if bool(context.get("user_requested_computer_use")):
                return True
            return _explicit_intent_text(user_text, tool_id)
        return True

    def _expand_targets(
        self,
        targets: list[ToolTarget],
        tools: list[dict[str, Any]],
        catalog: ToolServiceCatalog,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        selected: list[dict[str, Any]] = []
        unknown: list[str] = []
        eligible_catalog = ToolServiceCatalog(tools)
        for target in targets:
            matches = eligible_catalog.tools_for_target(target.kind, target.id)
            if not matches:
                unknown.append(f"{target.kind}:{target.id}")
                continue
            selected = self._stable_merge(selected, matches)
        return selected, unknown

    def _apply_excludes(
        self,
        tools: list[dict[str, Any]],
        targets: list[ToolTarget],
        catalog: ToolServiceCatalog,
    ) -> list[dict[str, Any]]:
        if not targets:
            return list(tools)
        excluded_tools = {target.id for target in targets if target.kind == "tool"}
        excluded_services = {target.id for target in targets if target.kind == "service"}
        output: list[dict[str, Any]] = []
        for tool in tools:
            tool_id = _tool_id(tool)
            service_id = catalog.compact_record(tool)["service_id"]
            if tool_id in excluded_tools or service_id in excluded_services:
                continue
            output.append(tool)
        return output

    def _semantic_candidates(self, user_text: str, tools: list[dict[str, Any]], *, context: dict[str, Any]) -> dict[str, Any]:
        backend = str(self._tool_settings.get("semantic_backend") or "auto").strip().lower() or "auto"
        embedding_model = self._embedding_model()
        result = ToolEmbeddingIndex().search(
            user_text,
            tools,
            limit=self._semantic_limit(),
            backend=backend,
            model=embedding_model,
        )
        fallbacks = []
        if result.get("fallback_reason"):
            fallbacks.append({"stage": result.get("stage"), "reason": result.get("fallback_reason")})
        result["fallbacks"] = fallbacks
        return result

    def _embedding_model(self) -> str:
        configured = str(self._tool_settings.get("embedding_model") or "").strip()
        if configured:
            return configured
        # Provider discovery walks every model and OAuth connection manifest.
        # Doing that synchronously on each chat turn delays the first SSE event
        # and can deadlock against managed-runtime workspace synchronization.
        # Keep automatic discovery available as an explicit opt-in; the normal
        # hot path uses the deterministic lexical prefilter and still lets the
        # utility model make the final AI selection.
        if not bool(self._tool_settings.get("auto_discover_embedding_model", False)):
            return ""
        try:
            result = search_models({"type": "embedding", "configured_only": True, "max_results": 1})
        except Exception:
            return ""
        models = result.get("models") if isinstance(result, dict) else []
        if not isinstance(models, list) or not models:
            return ""
        model = models[0] if isinstance(models[0], dict) else {}
        return str(model.get("profile_id") or model.get("qualified_model_id") or model.get("model_id") or "").strip()

    def _select_with_utility_model(
        self,
        user_text: str,
        candidates: list[dict[str, Any]],
        *,
        strategy: str,
        fallback_ids: list[str],
        context: dict[str, Any],
        prefilter: bool = True,
    ) -> tuple[list[str], str, list[dict[str, Any]], list[ToolRecommendation], str]:
        if not candidates:
            return [], "empty_candidates", [], [], ""
        limit = self._final_limit()
        try:
            result = ToolSelectionOrchestrator(call_handler=self._call_handler).select(
                user_text,
                candidates,
                limit=limit,
                selected_model_capabilities=context.get("selected_model_capabilities") if isinstance(context.get("selected_model_capabilities"), dict) else None,
                settings=self._settings,
                prefilter=prefilter,
            )
        except Exception as exc:
            return fallback_ids[:limit], "semantic_fallback", [{"stage": "utility_model", "reason": str(exc)}], [
                ToolRecommendation(tool_id=tool_id, confidence=0.5, reason="fallback after selector error")
                for tool_id in fallback_ids[:limit]
            ], ""
        selector_model = _selector_model_from_result(result)
        allowed_ids = {_tool_id(tool) for tool in candidates}
        selected_ids: list[str] = []
        recommendations: list[ToolRecommendation] = []
        for item in result.get("recommended_tools", []) if isinstance(result, dict) else []:
            if not isinstance(item, dict):
                continue
            tool_id = str(item.get("tool_id") or item.get("id") or "").strip()
            if not tool_id or tool_id not in allowed_ids or tool_id in selected_ids:
                continue
            selected_ids.append(tool_id)
            try:
                confidence = float(item.get("confidence", 0.6))
            except (TypeError, ValueError):
                confidence = 0.6
            recommendations.append(
                ToolRecommendation(tool_id=tool_id, confidence=confidence, reason=str(item.get("reason") or "selected by tool selector"))
            )
        stage = str(result.get("stage") or "utility_model") if isinstance(result, dict) else "utility_model"
        if not selected_ids:
            return fallback_ids[:limit], "semantic_fallback", [{"stage": stage, "reason": "selector_returned_no_valid_tools"}], [
                ToolRecommendation(tool_id=tool_id, confidence=0.5, reason="fallback after invalid selector output")
                for tool_id in fallback_ids[:limit]
            ], selector_model
        return selected_ids[:limit], stage, [], recommendations[:limit], selector_model

    def _decision(
        self,
        *,
        mode: str,
        strategy: str,
        stage: str,
        selected: list[dict[str, Any]],
        eligible: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        started: float,
        unknown_targets: list[str] | None = None,
        permission_entries: list[dict[str, Any]] | None = None,
        fallbacks: list[dict[str, Any]] | None = None,
        recommendations: list[ToolRecommendation] | None = None,
        cache_hit: bool = False,
        metrics: dict[str, Any] | None = None,
    ) -> ToolSelectionDecision:
        catalog = ToolServiceCatalog(selected)
        permission_summary = {"auto": 0, "confirm": 0, "block": 0}
        selected_ids = {_tool_id(tool) for tool in selected}
        for entry in permission_entries or []:
            if str(entry.get("tool_id") or "") not in selected_ids:
                continue
            permission = str(entry.get("permission") or "auto")
            if permission in permission_summary:
                permission_summary[permission] += 1
        if recommendations is None:
            recommendations = [
                ToolRecommendation(tool_id=_tool_id(tool), confidence=0.6, reason=_default_reason(tool))
                for tool in selected
            ]
        return ToolSelectionDecision(
            selection_id=gen_id(),
            mode=mode,
            strategy=strategy,
            stage=stage,
            selected_tools=list(selected),
            recommendations=list(recommendations),
            selected_services=catalog.services(),
            permission_summary=permission_summary,
            eligible_count=len(eligible),
            candidate_count=len(candidates),
            selected_count=len(selected),
            provider_schema_count=len(selected),
            fallbacks=list(fallbacks or []),
            unknown_targets=list(unknown_targets or []),
            duration_ms=int((time.perf_counter() - started) * 1000),
            cache_hit=cache_hit,
            metrics=dict(metrics or {}),
        )

    def _semantic_limit(self) -> int:
        try:
            return max(8, min(64, int(self._tool_settings.get("semantic_candidate_limit", DEFAULT_SEMANTIC_CANDIDATE_LIMIT))))
        except (TypeError, ValueError):
            return DEFAULT_SEMANTIC_CANDIDATE_LIMIT

    def _final_limit(self) -> int:
        try:
            return max(1, min(24, int(self._tool_settings.get("final_tool_limit", DEFAULT_FINAL_TOOL_LIMIT))))
        except (TypeError, ValueError):
            return DEFAULT_FINAL_TOOL_LIMIT

    def _catalog_ai_direct_limit(self) -> int:
        try:
            return max(20, min(200, int(self._tool_settings.get("catalog_ai_direct_limit", DEFAULT_CATALOG_AI_DIRECT_LIMIT))))
        except (TypeError, ValueError):
            return DEFAULT_CATALOG_AI_DIRECT_LIMIT

    @staticmethod
    def _tools_by_ids(tools: list[dict[str, Any]], tool_ids: list[str]) -> list[dict[str, Any]]:
        by_id = {_tool_id(tool): tool for tool in tools}
        return [by_id[tool_id] for tool_id in tool_ids if tool_id in by_id]

    @staticmethod
    def _stable_merge(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for group in groups:
            for tool in group:
                tool_id = _tool_id(tool)
                if not tool_id or tool_id in seen:
                    continue
                seen.add(tool_id)
                merged.append(tool)
        return merged


def _tool_id(tool: dict[str, Any]) -> str:
    return str(tool.get("tool_id") or tool_name_from_definition(tool) or tool.get("name") or "").strip()


def _explicit_intent_text(user_text: str, tool_id: str) -> bool:
    text = str(user_text or "").lower()
    if tool_id in COMPUTER_TOOL_IDS:
        return any(token in text for token in ("computer", "pc操作", "ブラウザ操作", "画面操作", "クリック", "chrome", "vivaldi"))
    return False


def _default_reason(tool: dict[str, Any]) -> str:
    record = ToolServiceCatalog.compact_record(tool)
    action = record.get("action_class")
    if action == "search":
        return "依頼内容の検索に必要なため"
    if action == "read":
        return "依頼内容の確認に必要なため"
    if action in {"create", "update"}:
        return "依頼された変更に必要なため"
    if action in {"send", "execute", "computer", "delete"}:
        return "依頼された操作に必要なため"
    return "依頼内容に関連するため"


def _selector_model_from_result(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    direct = str(result.get("selector_model") or result.get("model") or "").strip()
    if direct:
        return direct
    routing = result.get("routing") if isinstance(result.get("routing"), dict) else {}
    return str(routing.get("selected_model") or "").strip()


def _merge_targets(*groups: list[ToolTarget]) -> list[ToolTarget]:
    merged: list[ToolTarget] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for target in group:
            key = (target.kind, target.id)
            if key in seen:
                continue
            seen.add(key)
            merged.append(target)
    return merged


def _unique_ids(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in result:
            result.append(item)
    return result


def _developer_capability(context: dict[str, Any]) -> bool:
    if context.get("developer_mode") is True:
        return True
    capabilities = context.get("principal_capabilities")
    if isinstance(capabilities, (list, tuple, set)):
        return "developer" in {str(item) for item in capabilities}
    return False
