"""Validated Activity catalog and deterministic mention resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from domain.extensions.runtime import get_extension_registry
from domain.mention import extract_mention_values


@dataclass(frozen=True, slots=True)
class ActivityResolution:
    """A resolved Activity mention with its deterministic source."""

    activity_id: str
    source: str
    confidence: float


class ActivityRegistry:
    """Expose compact Activity records without loading Tool schemas or Skills."""

    def __init__(self, activities: Iterable[dict[str, Any]] | None = None) -> None:
        if activities is None:
            try:
                activities = get_extension_registry().activities().list(
                    enabled_only=True
                )
            except Exception:
                activities = []
        self._activities = {
            _activity_id(activity): dict(activity)
            for activity in activities
            if isinstance(activity, dict) and _activity_id(activity)
        }
        self._aliases, self._collisions = _build_alias_index(
            self._activities.values()
        )

    @property
    def collisions(self) -> dict[str, list[str]]:
        """Return aliases that cannot be resolved without a user choice."""

        return {
            alias: list(activity_ids)
            for alias, activity_ids in self._collisions.items()
        }

    @property
    def diagnostics(self) -> list[dict[str, str]]:
        """Return collision diagnostics suitable for catalog and Studio UIs."""

        return [
            {
                "code": "activity_alias_collision",
                "message": f"{alias}: {', '.join(activity_ids)}",
            }
            for alias, activity_ids in sorted(self._collisions.items())
        ]

    def list(self) -> list[dict[str, Any]]:
        """Return compact Activity records in stable ID order."""

        return [
            dict(self._activities[activity_id])
            for activity_id in sorted(self._activities)
        ]

    def get(self, activity_id: str) -> dict[str, Any] | None:
        """Return one Activity manifest."""

        activity = self._activities.get(str(activity_id or "").strip())
        return dict(activity) if activity else None

    def resolve_mentions(self, text: str) -> list[ActivityResolution]:
        """Resolve exact Activity mentions without asking a selector model."""

        known_values = set(self._aliases)
        known_values.update(
            f"activity:{value}" for value in tuple(known_values)
        )
        result: list[ActivityResolution] = []
        seen: set[str] = set()
        for raw_value in extract_mention_values(text, known_values):
            value = str(raw_value or "").casefold()
            if value.startswith("activity:"):
                value = value.split(":", 1)[1]
                source = "explicit_namespace"
            else:
                source = "explicit_mention"
            if value in self._collisions:
                continue
            activity_id = self._aliases.get(value)
            if not activity_id or activity_id in seen:
                continue
            seen.add(activity_id)
            result.append(
                ActivityResolution(
                    activity_id=activity_id,
                    source=source,
                    confidence=1.0,
                )
            )
        return result

    def infer(self, text: str, *, limit: int = 3) -> list[ActivityResolution]:
        """Use compact lexical metadata as the local-first intent fallback."""

        folded = str(text or "").casefold()
        scored: list[tuple[int, str]] = []
        for activity_id, activity in self._activities.items():
            selection = (
                activity.get("selection")
                if isinstance(activity.get("selection"), dict)
                else {}
            )
            if bool(selection.get("explicit_intent_required")):
                continue
            terms = {
                activity_id.casefold(),
                *(
                    str(alias).casefold()
                    for alias in activity.get("aliases", [])
                    if str(alias).strip()
                ),
            }
            description = _localized_text(activity.get("description")).casefold()
            score = sum(3 for term in terms if term and term in folded)
            score += sum(1 for token in folded.split() if token in description)
            if score:
                scored.append((score, activity_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            ActivityResolution(
                activity_id=activity_id,
                source="intent_lexical",
                confidence=min(0.9, 0.45 + score * 0.05),
            )
            for score, activity_id in scored[: max(0, limit)]
        ]

    def expand(
        self,
        activity_ids: Iterable[str],
        tools: Iterable[dict[str, Any]],
    ) -> dict[str, list[str]]:
        """Expand Activities into compact Tool and Skill candidate IDs."""

        tool_list = [tool for tool in tools if isinstance(tool, dict)]
        result = {
            "tool_ids": [],
            "required_skills": [],
            "optional_skills": [],
            "safety_skills": [],
            "service_ids": [],
        }
        for activity_id in activity_ids:
            activity = self._activities.get(str(activity_id or "").strip())
            if not activity:
                continue
            members = (
                activity.get("members")
                if isinstance(activity.get("members"), dict)
                else {}
            )
            _append_unique(result["tool_ids"], members.get("tool_ids"))
            _append_unique(result["service_ids"], members.get("service_ids"))
            requested_tags = {
                str(tag)
                for tag in members.get("tool_tags", [])
                if str(tag).strip()
            }
            for tool in tool_list:
                if requested_tags.intersection(_tool_tags(tool)):
                    _append_unique(result["tool_ids"], [_tool_id(tool)])
            skills = (
                members.get("skills")
                if isinstance(members.get("skills"), dict)
                else {}
            )
            _append_unique(result["required_skills"], skills.get("required"))
            _append_unique(result["optional_skills"], skills.get("optional"))
            _append_unique(result["safety_skills"], skills.get("safety"))
        return result


def _build_alias_index(
    activities: Iterable[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    candidates: dict[str, set[str]] = {}
    for activity in activities:
        activity_id = _activity_id(activity)
        aliases = [
            activity_id,
            *(
                str(value)
                for value in activity.get("aliases", [])
                if str(value).strip()
            ),
        ]
        for alias in aliases:
            candidates.setdefault(alias.casefold(), set()).add(activity_id)
    collisions = {
        alias: sorted(activity_ids)
        for alias, activity_ids in candidates.items()
        if len(activity_ids) > 1
    }
    index = {
        alias: next(iter(activity_ids))
        for alias, activity_ids in candidates.items()
        if len(activity_ids) == 1
    }
    return index, collisions


def _append_unique(target: list[str], values: Any) -> None:
    if not isinstance(values, list):
        return
    for value in values:
        text = str(value or "").strip()
        if text and text not in target:
            target.append(text)


def _activity_id(activity: dict[str, Any]) -> str:
    return str(activity.get("id") or "").strip()


def _tool_id(tool: dict[str, Any]) -> str:
    return str(tool.get("tool_id") or tool.get("name") or tool.get("id") or "").strip()


def _tool_tags(tool: dict[str, Any]) -> set[str]:
    metadata = tool.get("metadata") if isinstance(tool.get("metadata"), dict) else {}
    values = tool.get("tags") or metadata.get("tags") or []
    return {str(value) for value in values if str(value).strip()}


def _localized_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    return " ".join(str(text) for text in value.values() if str(text).strip())
