"""
model_router.py — モデルルーティングロジック

AIClient をラップし、入力を分析して最適モデルに自動ルーティングする。
AIClient 自体は変更しない。
"""

import copy
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from domain.ai_client.model_pack_router import select_model_pack  # noqa: E402
from domain.ai_client.model_pack_store import ModelPackStore  # noqa: E402
from domain.ai_client.model_profiles import ModelProfileManager  # noqa: E402
from domain.ai_client.model_roles import (  # noqa: E402
    normalize_utility_model_policy,
    normalize_utility_models,
)
from domain.ai_client.model_search import (  # noqa: E402
    get_model_capabilities,
    models_for_group,
)
from domain.ai_client.task_analyzer import analyze_fast, analyze_heavy  # noqa: E402


@dataclass
class ModelRoutingRequest:
    conversation_id: str = ""
    user_text: str = ""
    has_images: bool = False
    has_audio: bool = False
    has_files: bool = False
    requested_tools: list[str] = field(default_factory=list)
    requires_tool_calling: bool = False
    requires_fast: bool = False
    requested_thinking_level: str | None = None
    preferred_model: str = "stub/default"
    preferred_group: str = "default"
    auto_route_within_group: bool = True
    task_hints: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelRoutingDecision:
    selected_model: str
    original_model: str
    selected_group: str
    reason_codes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    bridge_required: bool = False
    bridge_plan: dict[str, Any] = field(default_factory=dict)
    utility_models: dict[str, str] = field(default_factory=dict)
    explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def route_model_request(
    request: ModelRoutingRequest | dict[str, Any],
    *,
    profiles: list[dict[str, Any]] | None = None,
) -> ModelRoutingDecision:
    routing_request = _coerce_routing_request(request)
    settings = routing_request.settings if isinstance(routing_request.settings, dict) else {}
    original = routing_request.preferred_model or "stub/default"
    selected_group = routing_request.preferred_group or settings.get("preferred_model_group") or "default"
    if ModelPackStore.is_model_pack_ref(original):
        pack_selection = select_model_pack(
            original,
            {
                "user_text": routing_request.user_text,
                "has_images": routing_request.has_images,
                "has_audio": routing_request.has_audio,
                "requires_tool_calling": bool(routing_request.requires_tool_calling or routing_request.requested_tools),
                "requires_fast": bool(routing_request.requires_fast),
                "requested_thinking_level": routing_request.requested_thinking_level or "",
                "task_hints": routing_request.task_hints,
            },
            settings=settings,
            profiles=profiles,
        )
        if pack_selection is not None and not pack_selection.selected_model:
            utility_models = _resolve_utility_models(settings, models_for_group(selected_group, settings, profiles=profiles))
            warnings = list(pack_selection.warnings)
            return ModelRoutingDecision(
                selected_model=original,
                original_model=original,
                selected_group=str(selected_group),
                reason_codes=_dedupe(list(pack_selection.reason_codes) + ["model_pack_no_compatible_member"]),
                warnings=warnings,
                bridge_required=False,
                bridge_plan={},
                utility_models=utility_models,
                explanation=explain_model_choice(original, list(pack_selection.reason_codes), warnings),
            )
        if pack_selection is not None and pack_selection.selected_model:
            utility_models = _resolve_utility_models(settings, models_for_group(selected_group, settings, profiles=profiles))
            return ModelRoutingDecision(
                selected_model=pack_selection.selected_model,
                original_model=original,
                selected_group=str(selected_group),
                reason_codes=_dedupe(list(pack_selection.reason_codes) + ["model_pack_selected"]),
                warnings=list(pack_selection.warnings),
                bridge_required=False,
                bridge_plan={},
                utility_models=utility_models,
                explanation=explain_model_choice(pack_selection.selected_model, list(pack_selection.reason_codes), list(pack_selection.warnings)),
            )
    candidates = models_for_group(selected_group, settings, profiles=profiles)
    if not candidates:
        candidates = models_for_group("default", settings, profiles=profiles)
    original_caps = get_model_capabilities(
        original,
        profiles=profiles,
        settings=settings,
    )
    selected = original_caps or (candidates[0] if candidates else {"profile_id": original})
    reason_codes: list[str] = ["preferred_model"]
    warnings: list[str] = []

    needs_vision = bool(routing_request.has_images)
    needs_audio = bool(routing_request.has_audio)
    needs_tools = bool(routing_request.requires_tool_calling or routing_request.requested_tools)
    needs_fast = bool(routing_request.requires_fast)
    needs_thinking = str(routing_request.requested_thinking_level or "").strip() not in {"", "none"}
    original_in_group = any(_same_model(item, original) for item in candidates)
    explicit_model_outside_group = bool(original_caps and not original_in_group)
    route_from_preferred_for_tools = _is_auto_route_anchor(original, original_caps)
    keep_original = bool(
        original_caps
        and (
            explicit_model_outside_group
            or _compatible(
                original_caps,
                needs_vision=needs_vision,
                needs_audio=needs_audio,
                needs_tools=needs_tools and route_from_preferred_for_tools,
                needs_fast=needs_fast,
                needs_thinking=False,
            )
        )
    )

    if routing_request.auto_route_within_group and not keep_original:
        compatible = [
            item for item in candidates
            if _compatible(item, needs_vision=needs_vision, needs_audio=needs_audio, needs_tools=needs_tools, needs_fast=needs_fast, needs_thinking=needs_thinking)
        ]
        if compatible:
            selected = _best_candidate(compatible, routing_request)
            reason_codes = _selection_reasons(selected, routing_request, original)
        elif candidates:
            selected = _best_candidate(candidates, routing_request)
            warnings.append("no_model_satisfied_all_capabilities")
            reason_codes = _selection_reasons(selected, routing_request, original)
    elif keep_original:
        selected = original_caps
        reason_codes = _selection_reasons(selected, routing_request, original)

    if needs_vision and not selected.get("supports_vision"):
        policy = str(settings.get("on_switch_to_non_vision_with_images") or "auto_bridge")
        if policy == "block":
            warnings.append("selected_model_does_not_support_vision")
        elif policy != "ignore":
            reason_codes.append("vision_bridge_required")
    if needs_tools and not selected.get("supports_tool_calling"):
        warnings.append("selected_model_does_not_support_tool_calling")
        reason_codes.append("tool_calling_unavailable")
    if needs_thinking and not selected.get("supports_thinking"):
        warnings.append("selected_model_does_not_support_thinking")
        reason_codes.append("thinking_level_normalized")
    if needs_fast and not selected.get("supports_fast"):
        warnings.append("selected_model_does_not_support_fast")

    bridge_required = bool(needs_vision and not selected.get("supports_vision") and str(settings.get("on_switch_to_non_vision_with_images") or "auto_bridge") != "ignore")
    utility_models = _resolve_utility_models(settings, candidates)
    selected_model = str(selected.get("profile_id") or selected.get("qualified_model_id") or original)
    bridge_plan = {}
    if bridge_required:
        bridge_plan = {
            "type": "vision_bridge",
            "policy": str(settings.get("on_switch_to_non_vision_with_images") or "auto_bridge"),
            "model_role": "vision_ocr",
            "model": utility_models.get("vision_ocr", ""),
        }
    return ModelRoutingDecision(
        selected_model=selected_model,
        original_model=original,
        selected_group=str(selected_group),
        reason_codes=_dedupe(reason_codes),
        warnings=_dedupe(warnings),
        bridge_required=bridge_required,
        bridge_plan=bridge_plan,
        utility_models=utility_models,
        explanation=explain_model_choice(selected_model, reason_codes, warnings),
    )


def explain_model_choice(selected_model: str, reason_codes: list[str] | None = None, warnings: list[str] | None = None) -> str:
    labels = {
        "preferred_model": "preferred model is compatible",
        "same_model": "kept the current model",
        "requires_vision": "image input is present",
        "requires_tool_calling": "tools are requested",
        "requires_fast": "a fast model is requested",
        "requires_thinking": "thinking is requested",
        "fast_candidate": "fast reply is preferred",
        "deep_reasoning": "higher reasoning depth is useful",
        "model_pack": "model pack routing is active",
        "model_pack_selected": "selected a model from the requested pack",
        "vision_bridge_required": "vision bridge is needed",
        "tool_calling_unavailable": "tool calling is unavailable",
        "thinking_level_normalized": "thinking level will be normalized",
    }
    parts = [labels.get(code, code) for code in (reason_codes or [])]
    if warnings:
        parts.extend("warning: " + str(item) for item in warnings)
    return "{} selected because {}.".format(selected_model, ", ".join(parts) if parts else "it is the best available match")


def _coerce_routing_request(value: ModelRoutingRequest | dict[str, Any]) -> ModelRoutingRequest:
    if isinstance(value, ModelRoutingRequest):
        return value
    raw = value if isinstance(value, dict) else {}
    return ModelRoutingRequest(
        conversation_id=str(raw.get("conversation_id") or ""),
        user_text=str(raw.get("user_text") or ""),
        has_images=bool(raw.get("has_images")),
        has_audio=bool(raw.get("has_audio")),
        has_files=bool(raw.get("has_files")),
        requested_tools=[str(item) for item in raw.get("requested_tools", [])] if isinstance(raw.get("requested_tools"), list) else [],
        requires_tool_calling=bool(raw.get("requires_tool_calling")),
        requires_fast=bool(raw.get("requires_fast")),
        requested_thinking_level=raw.get("requested_thinking_level"),
        preferred_model=str(raw.get("preferred_model") or "stub/default"),
        preferred_group=str(raw.get("preferred_group") or "default"),
        auto_route_within_group=bool(raw.get("auto_route_within_group", True)),
        task_hints=raw.get("task_hints") if isinstance(raw.get("task_hints"), dict) else {},
        settings=raw.get("settings") if isinstance(raw.get("settings"), dict) else {},
    )


def _compatible(
    model: dict[str, Any],
    *,
    needs_vision: bool,
    needs_audio: bool = False,
    needs_tools: bool,
    needs_fast: bool = False,
    needs_thinking: bool,
) -> bool:
    if needs_vision and not model.get("supports_vision"):
        return False
    if needs_audio and not model.get("supports_audio"):
        return False
    if needs_tools and not model.get("supports_tool_calling"):
        return False
    if needs_fast and not model.get("supports_fast"):
        return False
    if needs_thinking and not model.get("supports_thinking"):
        return False
    return True


def _best_candidate(candidates: list[dict[str, Any]], request: ModelRoutingRequest) -> dict[str, Any]:
    text = request.user_text or ""
    text_key = text.casefold()
    notes = request.settings.get("model_notes") if isinstance(request.settings, dict) and isinstance(request.settings.get("model_notes"), dict) else {}
    is_short = len(text.strip()) <= 80 and not request.has_images and not request.has_audio and not request.has_files
    def score(model: dict[str, Any]) -> tuple[int, str]:
        value = int(model.get("knowledge_level") or 0)
        value += 30 if model.get("configured") else 0
        value += 20 if request.has_images and model.get("supports_vision") else 0
        value += 20 if request.has_audio and model.get("supports_audio") else 0
        value += 18 if request.requires_tool_calling and model.get("supports_tool_calling") else 0
        value += 16 if request.requires_fast and model.get("supports_fast") else 0
        value += 12 if request.requested_thinking_level and model.get("supports_thinking") else 0
        value += 16 if is_short and model.get("speed_tier") == "fast" else 0
        value += 8 if model.get("local") and request.preferred_group == "local" else 0
        if request.preferred_group == "cheap" and model.get("cost_tier") in {"free", "low"}:
            value += 16
        if request.preferred_group == "fast" and model.get("speed_tier") == "fast":
            value += 20
        profile_id = str(model.get("profile_id") or model.get("qualified_model_id") or "")
        note = str(model.get("notes") or notes.get(profile_id) or notes.get(str(model.get("qualified_model_id") or "")) or "").casefold()
        if note:
            for token in ("code", "coding", "tool", "vision", "image", "fast", "cheap", "deep", "long", "japanese", "日本語", "画像"):
                if token in text_key and token in note:
                    value += 10
        return value, str(model.get("label") or model.get("profile_id") or "")
    return sorted(candidates, key=lambda item: (-score(item)[0], score(item)[1].casefold()))[0]


def _selection_reasons(selected: dict[str, Any], request: ModelRoutingRequest, original: str) -> list[str]:
    reasons = ["same_model" if str(selected.get("profile_id") or "") == original else "routed_within_group"]
    if request.has_images and selected.get("supports_vision"):
        reasons.append("requires_vision")
    if request.has_audio and selected.get("supports_audio"):
        reasons.append("requires_audio")
    if request.requires_tool_calling and selected.get("supports_tool_calling"):
        reasons.append("requires_tool_calling")
    if request.requires_fast and selected.get("supports_fast"):
        reasons.append("requires_fast")
    if request.requested_thinking_level and selected.get("supports_thinking"):
        reasons.append("requires_thinking")
    if selected.get("speed_tier") == "fast":
        reasons.append("fast_candidate")
    if int(selected.get("knowledge_level") or 0) >= 85:
        reasons.append("deep_reasoning")
    return reasons


def _same_model(model: dict[str, Any], model_id: str) -> bool:
    needle = str(model_id or "")
    return needle in {
        str(model.get("profile_id") or ""),
        str(model.get("qualified_model_id") or ""),
        "{}/{}".format(model.get("provider_id") or model.get("provider") or "", model.get("model_id") or ""),
    }


def _is_auto_route_anchor(model_id: str, model: dict[str, Any] | None = None) -> bool:
    profile_id = str(model_id or "").strip()
    aliases = {
        "",
        "default",
        "stub/default",
    }
    if isinstance(model, dict):
        aliases.update(
            {
                str(model.get("profile_id") or ""),
                str(model.get("qualified_model_id") or ""),
            }
            if str(model.get("provider_id") or "") == "stub"
            else set()
        )
    return profile_id in aliases


def _resolve_utility_models(settings: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, str]:
    configured = normalize_utility_models(settings.get("utility_models") if isinstance(settings, dict) else None)
    policy = normalize_utility_model_policy(settings.get("utility_model_policy") if isinstance(settings, dict) else None)
    if not policy.get("allow_auto_select", True):
        return configured
    for role, current in list(configured.items()):
        if current:
            continue
        configured[role] = _auto_select_role(role, candidates, policy)
    return configured


def _auto_select_role(role: str, candidates: list[dict[str, Any]], policy: dict[str, Any]) -> str:
    min_levels = policy.get("min_knowledge_level") if isinstance(policy.get("min_knowledge_level"), dict) else {}
    floor = int(min_levels.get(role, 0) or 0)
    filtered = [item for item in candidates if item.get("configured") and int(item.get("knowledge_level") or 0) >= floor]
    if role == "vision_ocr":
        filtered = [item for item in filtered if item.get("supports_vision")]
    if role == "tool_selector":
        filtered = [item for item in filtered if item.get("supports_tool_calling") or item.get("supports_fast")]
    if not filtered:
        filtered = [item for item in candidates if item.get("configured")] or candidates
    if not filtered:
        return ""
    if policy.get("prefer_fast_for_utility", True):
        filtered = sorted(filtered, key=lambda item: (item.get("speed_tier") != "fast", -int(item.get("knowledge_level") or 0)))
    else:
        filtered = sorted(filtered, key=lambda item: -int(item.get("knowledge_level") or 0))
    return str(filtered[0].get("profile_id") or filtered[0].get("qualified_model_id") or "")


def _dedupe(values: list[str]) -> list[str]:
    result = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


class RoutingLog:
    """ルーティングログを管理するシンプルなインメモリストア。"""

    def __init__(self, max_entries=1000):
        self._entries = []
        self._max_entries = max_entries

    def add(self, entry):
        """ログエントリを追加する。

        Parameters
        ----------
        entry : dict
        """
        self._entries.append(entry)
        if len(self._entries) > self._max_entries:
            self._entries = self._entries[-self._max_entries:]

    def get_all(self, limit=100, offset=0):
        """ログを取得する（新しい順）。

        Parameters
        ----------
        limit : int
        offset : int

        Returns
        -------
        list[dict]
        """
        reversed_entries = list(reversed(self._entries))
        return reversed_entries[offset:offset + limit]

    def count(self):
        """ログ件数を返す。"""
        return len(self._entries)

    def clear(self):
        """ログをクリアする。"""
        self._entries.clear()


class RoutingRule:
    """カスタムルーティングルール。

    ルールは JSON シリアライズ可能な条件で定義される。
    condition は以下のフィールドを持つ dict:
    - "task_type": str (マッチするタスク種類)
    - "complexity": str (マッチする複雑さ)
    - "min_tokens": int (最小トークン数)
    - "max_tokens": int (最大トークン数)
    - "has_code": bool
    - "has_images": bool
    - "language_hint": str
    各フィールドは省略可能。指定されたフィールド全てがマッチした場合にルールが適用される。
    """

    def __init__(self, rule_id, name, condition, target_model, priority=0):
        self.rule_id = rule_id
        self.name = name
        self.condition = condition
        self.target_model = target_model
        self.priority = priority

    def matches(self, analysis_result):
        """分析結果がこのルールの条件にマッチするか判定する。

        Parameters
        ----------
        analysis_result : dict

        Returns
        -------
        bool
        """
        cond = self.condition
        if "task_type" in cond:
            if analysis_result.get("task_type") != cond["task_type"]:
                return False
        if "complexity" in cond:
            if analysis_result.get("complexity") != cond["complexity"]:
                return False
        if "min_tokens" in cond:
            if analysis_result.get("context_tokens_estimate", 0) < cond["min_tokens"]:
                return False
        if "max_tokens" in cond:
            if analysis_result.get("context_tokens_estimate", 0) > cond["max_tokens"]:
                return False
        if "has_code" in cond:
            if analysis_result.get("has_code") != cond["has_code"]:
                return False
        if "has_images" in cond:
            if analysis_result.get("has_images") != cond["has_images"]:
                return False
        if "language_hint" in cond:
            if analysis_result.get("language_hint") != cond["language_hint"]:
                return False
        return True

    def to_dict(self):
        """JSON シリアライズ可能な dict に変換する。"""
        return {
            "id": self.rule_id,
            "name": self.name,
            "condition": copy.deepcopy(self.condition),
            "target_model": self.target_model,
            "priority": self.priority,
        }


class ModelRouter:
    """モデルルーティングエンジン。

    AIClient をラップして使用する。AIClient 自体は変更しない。

    Parameters
    ----------
    client : AIClient
        AIClient インスタンス。
    """

    _instance = None

    def __new__(cls, client=None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, client=None):
        if self._initialized:
            return
        self._initialized = True
        self._client = client
        self._profile_manager = ModelProfileManager()
        self._routing_log = RoutingLog()
        self._custom_rules = []
        self._next_rule_id = 1

    @property
    def profile_manager(self):
        """ModelProfileManager へのアクセス。"""
        return self._profile_manager

    @property
    def routing_log(self):
        """RoutingLog へのアクセス。"""
        return self._routing_log

    def set_client(self, client):
        """AIClient を設定する。遅延初期化用。"""
        self._client = client

    def analyze(self, messages, mode="fast"):
        """入力を分析する。

        Parameters
        ----------
        messages : list[dict]
            StandardMessage 形式。
        mode : str
            "fast" or "heavy"

        Returns
        -------
        dict
            分析結果。
        """
        if mode == "heavy":
            return analyze_heavy(messages)
        return analyze_fast(messages)

    def add_rule(self, name, condition, target_model, priority=0):
        """カスタムルーティングルールを追加する。

        Parameters
        ----------
        name : str
        condition : dict
        target_model : str
        priority : int

        Returns
        -------
        dict
            作成されたルール。
        """
        rule_id = "rule_{}".format(self._next_rule_id)
        self._next_rule_id += 1
        rule = RoutingRule(rule_id, name, condition, target_model, priority)
        self._custom_rules.append(rule)
        # 優先度の高い順にソート
        self._custom_rules.sort(key=lambda r: r.priority, reverse=True)
        return rule.to_dict()

    def remove_rule(self, rule_id):
        """ルールを削除する。

        Parameters
        ----------
        rule_id : str

        Returns
        -------
        bool
            削除成功なら True。
        """
        before = len(self._custom_rules)
        self._custom_rules = [r for r in self._custom_rules if r.rule_id != rule_id]
        return len(self._custom_rules) < before

    def list_rules(self):
        """全ルールをリストで返す。

        Returns
        -------
        list[dict]
        """
        return [r.to_dict() for r in self._custom_rules]

    def route(self, messages, mode="fast", speed_preference="balanced"):
        """入力を分析し、最適モデルを選択する。

        Parameters
        ----------
        messages : list[dict]
            StandardMessage 形式。
        mode : str
            "fast" or "heavy"
        speed_preference : str
            "fast", "balanced", "heavy"

        Returns
        -------
        dict
            {
                "selected_model": str,
                "analysis": dict,
                "scores": dict,
                "matched_rule": str | None,
                "reason": str,
            }
        """
        start_time = time.time()

        # 1. 入力分析
        analysis = self.analyze(messages, mode=mode)

        # 2. カスタムルールチェック
        matched_rule = None
        for rule in self._custom_rules:
            if rule.matches(analysis):
                # ターゲットモデルのプロバイダーが利用可能か確認
                if self._is_model_available(rule.target_model):
                    matched_rule = rule
                    break

        if matched_rule is not None:
            selected_model = matched_rule.target_model
            reason = "Matched custom rule: {}".format(matched_rule.name)
            scores = {}
        else:
            # 3. プロファイルベースのスコアリング
            available_models = self._profile_manager.get_available_models(self._client)
            if not available_models:
                selected_model = "stub/default"
                reason = "No available models found"
                scores = {}
            else:
                scores = {}
                for model_key in available_models:
                    scores[model_key] = self._profile_manager.score_model(
                        model_key, analysis, speed_preference=speed_preference
                    )
                # 最高スコアのモデルを選択
                selected_model = max(scores, key=scores.get)
                reason = "Best score: {:.1f}".format(scores[selected_model])

        elapsed = time.time() - start_time

        # 4. ログ記録
        log_entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "selected_model": selected_model,
            "analysis": analysis,
            "scores": scores,
            "matched_rule": matched_rule.to_dict() if matched_rule else None,
            "reason": reason,
            "mode": mode,
            "speed_preference": speed_preference,
            "elapsed_ms": round(elapsed * 1000, 2),
        }
        self._routing_log.add(log_entry)

        return {
            "selected_model": selected_model,
            "analysis": analysis,
            "scores": scores,
            "matched_rule": matched_rule.name if matched_rule else None,
            "reason": reason,
        }

    def route_and_complete(self, messages, mode="fast", speed_preference="balanced",
                           tools=None, params=None):
        """ルーティング + AI 呼び出しを一括で行う。

        Parameters
        ----------
        messages : list[dict]
        mode : str
        speed_preference : str
        tools : list | None
        params : dict | None

        Returns
        -------
        dict
            {
                "routing": dict (route() の結果),
                "response": dict (AIClient.complete() の結果),
            }
        """
        routing_result = self.route(messages, mode=mode, speed_preference=speed_preference)
        selected_model = routing_result["selected_model"]

        response = self._client.complete(
            selected_model, messages, tools=tools or [], params=params or {}
        )

        return {
            "routing": routing_result,
            "response": response,
        }

    def route_and_stream(self, messages, mode="fast", speed_preference="balanced",
                         tools=None, params=None):
        """ルーティング + ストリーミング AI 呼び出しを一括で行う。

        Parameters
        ----------
        messages : list[dict]
        mode : str
        speed_preference : str
        tools : list | None
        params : dict | None

        Returns
        -------
        tuple(dict, generator)
            (routing_result, stream_generator)
        """
        routing_result = self.route(messages, mode=mode, speed_preference=speed_preference)
        selected_model = routing_result["selected_model"]

        stream_gen = self._client.stream(
            selected_model, messages, tools=tools or [], params=params or {}
        )

        return routing_result, stream_gen

    def _is_model_available(self, model_key):
        """モデルが利用可能か判定する。

        Parameters
        ----------
        model_key : str

        Returns
        -------
        bool
        """
        if self._client is None:
            return False
        if "/" in model_key:
            provider_name = model_key.split("/", 1)[0]
            return provider_name in self._client._providers
        return False
