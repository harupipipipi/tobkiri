from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from domain.external.event import ExternalEvent
from domain.frontend_settings import frontend_settings_path
from domain.input.envelope import RumiInputEnvelope


FIRE_ACTIONS = {"fire", "run", "process", "reply", "respond", "send", "fire_and_send"}
IGNORE_ACTIONS = {"ignore", "drop", "skip", "deny"}
STORE_ONLY_ACTIONS = {"store_only", "record", "remember"}


@dataclass
class TriggerDecision:
    action: str = "fire"
    fire: bool = True
    send_response: bool | None = None
    reason: str = ""
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "fire": self.fire,
            "send_response": self.send_response,
            "reason": self.reason,
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
        }


class TriggerDecisionService:
    """Decides whether an external event should fire chat and/or send externally."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = dict(config) if isinstance(config, dict) else {}
        self.enabled = bool(self.config.get("enabled"))
        self.mode = str(self.config.get("mode") or "vector").strip().lower() or "vector"
        self.default_action = str(self.config.get("default_action") or "fire").strip().lower() or "fire"

    @classmethod
    def from_profile(cls, profile: Any, context: dict[str, Any] | None = None) -> "TriggerDecisionService":
        context = context if isinstance(context, dict) else {}
        override = context.get("trigger_decision_config")
        if isinstance(override, dict):
            return cls(override)
        spec = getattr(profile, "spec", profile if isinstance(profile, dict) else {})
        frontend_config = _frontend_trigger_config()
        profile_config = spec.get("trigger_decision") if isinstance(spec, dict) and isinstance(spec.get("trigger_decision"), dict) else {}
        if not profile_config and isinstance(spec, dict) and isinstance(spec.get("trigger"), dict):
            profile_config = spec.get("trigger") or {}
        config = {**frontend_config, **profile_config} if isinstance(profile_config, dict) and profile_config else frontend_config
        return cls(config)

    def decide(
        self,
        event: ExternalEvent,
        *,
        envelope: RumiInputEnvelope,
        context: dict[str, Any] | None = None,
        requested_send_response: bool | None = None,
    ) -> TriggerDecision:
        context = context if isinstance(context, dict) else {}
        base_metadata = {
            "enabled": self.enabled,
            "mode": self.mode,
            "filter_unrelated": bool(self.config.get("filter_unrelated")),
            "requested_send_response": requested_send_response,
        }
        if isinstance(context.get("trigger_decision"), dict):
            return self._decision_from_payload(
                context["trigger_decision"],
                requested_send_response=requested_send_response,
                metadata=base_metadata,
            )
        if not self.enabled:
            return TriggerDecision(
                action="fire",
                fire=True,
                send_response=requested_send_response,
                reason="trigger decision disabled",
                metadata=base_metadata,
            )

        vector_result = self._vector_evidence(envelope, context)
        base_metadata["vector"] = vector_result
        if self.mode in {"llm", "hybrid"}:
            llm_decision = self._llm_decision(event, envelope, context, vector_result)
            if llm_decision is not None:
                return self._decision_from_payload(
                    llm_decision,
                    requested_send_response=requested_send_response,
                    metadata=base_metadata,
                )
        if self.mode == "vector":
            matched = bool(vector_result.get("matched"))
            action = self._vector_action(matched)
            return self._decision_from_payload(
                {"action": action, "reason": "vector threshold matched" if matched else "vector threshold not met"},
                requested_send_response=requested_send_response,
                metadata=base_metadata,
            )
        return self._decision_from_payload(
            {"action": self.default_action, "reason": "default trigger action"},
            requested_send_response=requested_send_response,
            metadata=base_metadata,
        )

    def _vector_evidence(self, envelope: RumiInputEnvelope, context: dict[str, Any]) -> dict[str, Any]:
        settings = self._settings("vector")
        enabled = bool(settings.get("enabled"))
        result: dict[str, Any] = {
            "enabled": enabled,
            "settings": _public_settings(settings),
            "matched": False,
            "matches": [],
        }
        if not enabled:
            return result
        query = str(settings.get("query") or envelope.input or "").strip()
        if not query:
            return result
        try:
            from domain.memory2.search import MemorySearch

            filters = settings.get("filters") if isinstance(settings.get("filters"), dict) else {}
            limit = max(1, min(int(settings.get("limit", 5) or 5), 20))
            matches = MemorySearch().search(query, limit=limit, filters=filters)
        except Exception as exc:
            result["error"] = str(exc)
            return result
        min_score = float(settings.get("min_score", settings.get("threshold", 0.1)) or 0.1)
        compact_matches = [
            {
                "id": match.get("id"),
                "scope": match.get("scope"),
                "score": float(match.get("score", 0) or 0),
                "metadata": match.get("metadata") if isinstance(match.get("metadata"), dict) else {},
            }
            for match in matches
            if isinstance(match, dict)
        ]
        result["matches"] = compact_matches
        result["matched"] = any(float(match.get("score", 0) or 0) >= min_score for match in compact_matches)
        result["min_score"] = min_score
        return result

    def _llm_decision(
        self,
        event: ExternalEvent,
        envelope: RumiInputEnvelope,
        context: dict[str, Any],
        vector_result: dict[str, Any],
    ) -> dict[str, Any] | None:
        client = context.get("trigger_decision_llm") or context.get("trigger_llm")
        if client is None:
            return None
        settings = self._settings("llm")
        payload = {
            "model": settings.get("model") or self.config.get("model") or "inherit",
            "mode": self.mode,
            "messages": [
                {
                    "role": "system",
                    "content": str(
                        settings.get("system_prompt")
                        or "Decide whether this external event should fire chat and whether a response may be sent. Return strict JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "provider": event.provider,
                            "event": event.as_dict(),
                            "input": envelope.input,
                            "vector": vector_result,
                            "allowed_actions": sorted(FIRE_ACTIONS | IGNORE_ACTIONS | STORE_ONLY_ACTIONS),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "settings": _public_settings(settings),
        }
        raw = client(payload) if callable(client) else client.decide(payload)
        parsed = _parse_json(raw)
        return parsed if isinstance(parsed, dict) else None

    def _decision_from_payload(
        self,
        payload: dict[str, Any],
        *,
        requested_send_response: bool | None,
        metadata: dict[str, Any],
    ) -> TriggerDecision:
        action = str(payload.get("action") or payload.get("decision") or self.default_action).strip().lower()
        fire = payload.get("fire")
        if not isinstance(fire, bool):
            fire = action in FIRE_ACTIONS or action in STORE_ONLY_ACTIONS
            if action in IGNORE_ACTIONS:
                fire = False
        send_value = payload.get("send_response", payload.get("send"))
        if isinstance(send_value, bool):
            send_response = send_value
        elif action in {"send", "reply", "respond", "fire_and_send"}:
            send_response = True
        elif action in STORE_ONLY_ACTIONS or action in IGNORE_ACTIONS:
            send_response = False
        else:
            send_response = requested_send_response
        confidence = payload.get("confidence", 1.0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 1.0
        decision_metadata = dict(metadata)
        if isinstance(payload.get("metadata"), dict):
            decision_metadata.update(payload["metadata"])
        decision_metadata["llm"] = _public_settings(self._settings("llm"))
        return TriggerDecision(
            action=action or "fire",
            fire=bool(fire),
            send_response=send_response,
            reason=str(payload.get("reason") or ""),
            confidence=confidence,
            metadata=decision_metadata,
        )

    def _settings(self, key: str) -> dict[str, Any]:
        value = self.config.get(key)
        if isinstance(value, dict):
            return dict(value)
        return {}

    def _vector_action(self, matched: bool) -> str:
        settings = self._settings("vector")
        if matched:
            return str(settings.get("on_match") or "fire").strip().lower() or "fire"
        return str(settings.get("on_miss") or self.config.get("fallback_action") or self.default_action).strip().lower() or "fire"


def _parse_json(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        if "content" not in raw:
            return raw
        content = raw.get("content")
        if isinstance(content, str):
            raw = content
        elif isinstance(content, list):
            raw = "\n".join(
                str(block.get("text") or "") if isinstance(block, dict) else str(block)
                for block in content
            )
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _public_settings(settings: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(settings or {}).items()
        if key not in {"api_key", "token", "secret", "password"}
    }


def _frontend_trigger_config() -> dict[str, Any]:
    path = frontend_settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    triggers = raw.get("triggers") if isinstance(raw, dict) and isinstance(raw.get("triggers"), dict) else {}
    mode = str(triggers.get("mode") or "vector").strip().lower()
    if mode not in {"vector", "llm"}:
        mode = "vector"
    try:
        threshold = float(triggers.get("vector_threshold", 0.1))
    except (TypeError, ValueError):
        threshold = 0.1
    model = str(triggers.get("model") or "").strip()
    filter_unrelated = bool(triggers.get("filter_unrelated", False))
    return {
        "enabled": True,
        "mode": mode,
        "default_action": "fire",
        "fallback_action": "fire",
        "filter_unrelated": filter_unrelated,
        "vector": {
            "enabled": mode == "vector",
            "threshold": threshold,
            "on_match": "fire",
            "on_miss": "fire",
        },
        "llm": {
            "model": model or "inherit",
            "filter_unrelated": filter_unrelated,
        },
    }
