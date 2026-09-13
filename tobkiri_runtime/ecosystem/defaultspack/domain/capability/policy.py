"""Effect-level approval policy with non-bypassable security minimums."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


_MODE_RANK = {"auto": 0, "confirm": 1, "deny": 2}
_WRITE_EFFECTS = {
    "create",
    "update",
    "send",
    "execute",
    "computer",
    "delete",
    "credential",
}


@dataclass(frozen=True, slots=True)
class EffectDecision:
    """Resolved policy for one concrete Tool effect."""

    effect_class: str
    mode: str
    source: str
    hard_minimum: str

    def to_dict(self) -> dict[str, str]:
        """Return the approval-plan representation."""

        return {
            "class": self.effect_class,
            "mode": self.mode,
            "source": self.source,
            "hard_minimum": self.hard_minimum,
        }


class EffectPolicyEngine:
    """Resolve approval after selection without granting execution authority."""

    def resolve(
        self,
        tool: dict[str, Any],
        settings: dict[str, Any],
        *,
        profile_policy: dict[str, Any] | None = None,
        full_access: bool = False,
    ) -> list[EffectDecision]:
        """Return decisions for every declared or legacy Tool effect."""

        effects = tool.get("effects")
        if not isinstance(effects, list) or not effects:
            effects = [{"class": _legacy_effect_class(tool)}]
        decisions: list[EffectDecision] = []
        for effect in effects:
            if not isinstance(effect, dict):
                continue
            effect_class = str(effect.get("class") or "execute").strip().lower()
            hard_minimum = _hard_minimum(tool, effect_class)
            requested, source = _requested_mode(
                tool,
                settings,
                profile_policy=profile_policy,
                effect_class=effect_class,
            )
            if full_access and _full_access_may_auto(tool, effect_class):
                hard_minimum = "auto"
                requested, source = "auto", "full_access"
            mode = _more_restrictive(requested, hard_minimum)
            decisions.append(
                EffectDecision(
                    effect_class=effect_class,
                    mode=mode,
                    source=source,
                    hard_minimum=hard_minimum,
                )
            )
        return decisions


def _hard_minimum(tool: dict[str, Any], effect_class: str) -> str:
    metadata = tool.get("metadata") if isinstance(tool.get("metadata"), dict) else {}
    trusted = bool(tool.get("trusted") or metadata.get("trusted"))
    risk_value = tool.get("risk")
    if isinstance(risk_value, dict):
        risk = str(risk_value.get("level") or "").lower()
    else:
        risk = str(risk_value or metadata.get("risk") or "").lower()
    if not trusted or risk in {"high", "critical"}:
        return "confirm"
    if effect_class in {"delete", "credential"}:
        return "confirm"
    return "confirm" if effect_class in _WRITE_EFFECTS else "auto"


def _full_access_may_auto(
    tool: dict[str, Any],
    effect_class: str,
) -> bool:
    """Allow trusted ordinary effects while preserving hard safety gates."""

    metadata = tool.get("metadata") if isinstance(tool.get("metadata"), dict) else {}
    if not bool(tool.get("trusted") or metadata.get("trusted")):
        return False
    risk_value = tool.get("risk")
    if isinstance(risk_value, dict):
        risk = str(risk_value.get("level") or "").lower()
    else:
        risk = str(risk_value or metadata.get("risk") or "").lower()
    if risk in {"high", "critical"}:
        return False
    return effect_class not in {"delete", "credential"}


def _requested_mode(
    tool: dict[str, Any],
    settings: dict[str, Any],
    *,
    profile_policy: dict[str, Any] | None,
    effect_class: str,
) -> tuple[str, str]:
    if isinstance(profile_policy, dict):
        deny = profile_policy.get("deny_effects")
        if isinstance(deny, list) and effect_class in deny:
            return "deny", "runtime_profile"
    capabilities = (
        settings.get("capabilities")
        if isinstance(settings.get("capabilities"), dict)
        else settings
    )
    approval = (
        capabilities.get("approval")
        if isinstance(capabilities.get("approval"), dict)
        else {}
    )
    actions = (
        approval.get("actions")
        if isinstance(approval.get("actions"), dict)
        else {}
    )
    mode = str(actions.get(effect_class) or "confirm").lower()
    if mode not in _MODE_RANK:
        mode = "confirm"
    overrides = (
        capabilities.get("tools")
        if isinstance(capabilities.get("tools"), dict)
        else {}
    )
    overrides = (
        overrides.get("overrides")
        if isinstance(overrides.get("overrides"), dict)
        else {}
    )
    tool_id = str(tool.get("tool_id") or tool.get("id") or "").strip()
    override = overrides.get(tool_id)
    if isinstance(override, dict):
        override_mode = str(override.get("approval") or "").lower()
        if override_mode in _MODE_RANK:
            return override_mode, "tool_override"
    return mode, "action_class"


def _more_restrictive(left: str, right: str) -> str:
    return max((left, right), key=lambda mode: _MODE_RANK.get(mode, 1))


def _legacy_effect_class(tool: dict[str, Any]) -> str:
    for source in (
        tool,
        tool.get("metadata") if isinstance(tool.get("metadata"), dict) else {},
    ):
        value = str(
            source.get("action_type") or source.get("action") or ""
        ).strip().lower()
        if value in _MODE_RANK or value in _WRITE_EFFECTS or value in {
            "read",
            "search",
        }:
            return value
    return "execute" if bool(tool.get("write_action")) else "read"
