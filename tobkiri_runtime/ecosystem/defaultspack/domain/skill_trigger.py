from __future__ import annotations

from pathlib import Path
from typing import Any

from domain.extensions.runtime import get_extension_registry
from domain.mention import extract_mention_values

_MAX_PROMPT_FILE_CHARS = 80_000
_COMPOSITION_ORDER = {"safety": 0, "required": 1, "explicit": 2, "optional": 3}


class RuntimeSkillTriggerService:
    """Matches enabled extension skills and renders their runtime instructions."""

    def __init__(self, skills: list[dict[str, Any]] | None = None) -> None:
        self._skills = skills

    def evaluate(
        self,
        *,
        user_text: str,
        tool_names: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = context if isinstance(context, dict) else {}
        if context.get("disable_runtime_skill_triggers") is True:
            return {"matched": [], "instructions": ""}
        text = str(user_text or "").casefold()
        tool_set = {str(name or "").strip() for name in (tool_names or []) if str(name or "").strip()}
        skills = self._list_skills()
        explicit = _resolve_skill_ids(
            [
                *_as_list(context.get("verified_explicit_skills")),
                *_mentioned_skill_ids(user_text, skills),
            ],
            skills,
        )
        required = _resolve_skill_ids(
            _as_list(context.get("required_skills")), skills
        )
        safety = _resolve_skill_ids(
            _as_list(context.get("safety_skills")), skills
        )
        matched: list[dict[str, Any]] = []
        for skill in skills:
            skill_id = str(skill.get("id") or "").strip()
            if not skill_id:
                continue
            triggers = _as_list(skill.get("triggers") or skill.get("keywords"))
            applies_to = _as_list(skill.get("applies_to_tools") or skill.get("tool_ids"))
            composition = (
                skill.get("composition")
                if isinstance(skill.get("composition"), dict)
                else {}
            )
            declared_class = str(
                composition.get("class") or "optional"
            ).strip().lower()
            forced_hit = skill_id in explicit or skill_id in required or skill_id in safety
            trigger_hit = forced_hit or not triggers or any(str(trigger).casefold() in text for trigger in triggers)
            tool_hit = forced_hit or not applies_to or bool(tool_set.intersection(applies_to))
            if not (trigger_hit and tool_hit):
                continue
            instruction = _instruction_text(skill)
            if not instruction:
                continue
            if skill_id in safety:
                composition_class = "safety"
            elif skill_id in required:
                composition_class = "required"
            elif skill_id in explicit:
                composition_class = "explicit"
            elif declared_class in _COMPOSITION_ORDER:
                composition_class = declared_class
            else:
                composition_class = "optional"
            matched.append(
                {
                    "id": skill_id,
                    "display_name": str(skill.get("display_name") or skill_id),
                    "triggers": triggers,
                    "applies_to_tools": applies_to,
                    "instruction": instruction,
                    "composition_class": composition_class,
                    "priority": int(composition.get("priority", 0)),
                }
            )
        matched.sort(
            key=lambda item: (
                _COMPOSITION_ORDER.get(
                    str(item.get("composition_class") or "optional"), 3
                ),
                -int(item.get("priority", 0)),
                str(item.get("id") or ""),
            )
        )
        return {"matched": matched, "instructions": render_skill_instructions(matched)}

    def _list_skills(self) -> list[dict[str, Any]]:
        if self._skills is not None:
            return [skill for skill in self._skills if isinstance(skill, dict)]
        try:
            from domain.capability.skill_lifecycle import SkillLifecycleStore

            skills = get_extension_registry().skills().list(enabled_only=True)
            return SkillLifecycleStore().apply(skills)
        except Exception:
            return []


def render_skill_instructions(matched: list[dict[str, Any]]) -> str:
    if not matched:
        return ""
    lines = [
        "Runtime skill instructions matched this turn. These are active system-level instructions for this turn; follow them unless they conflict with higher-priority safety or user instructions:"
    ]
    for item in matched:
        lines.append("- {}: {}".format(item.get("id"), str(item.get("instruction") or "").strip()))
    return "\n".join(lines).strip()


def _instruction_text(skill: dict[str, Any]) -> str:
    source_path = _skill_source_path(skill)
    if source_path:
        instructions = (
            skill.get("instructions")
            if isinstance(skill.get("instructions"), dict)
            else {}
        )
        instruction = _instruction_file_text(skill)
        if not instruction:
            return ""
        max_tokens = max(1, int(instructions.get("max_tokens", 1)))
        return instruction[: max_tokens * 4].strip()
    # Source-less definitions are trusted process-local records used by tests
    # and programmatic composition. Persisted extensions must use SKILL.md.
    for key in ("instructions", "instruction"):
        value = skill.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _instruction_file_text(skill: dict[str, Any]) -> str:
    source_path = _skill_source_path(skill)
    if not source_path:
        return ""
    try:
        manifest_path = Path(source_path).expanduser().resolve(strict=True)
        skill_root = manifest_path.parent.resolve(strict=True)
        skill_md_candidate = skill_root / "SKILL.md"
        if skill_md_candidate.is_symlink():
            return ""
        skill_md = skill_md_candidate.resolve(strict=True)
        if skill_md.parent != skill_root:
            return ""
        return skill_md.read_text(
            encoding="utf-8", errors="ignore"
        )[:_MAX_PROMPT_FILE_CHARS].strip()
    except OSError:
        return ""


def _skill_source_path(skill: dict[str, Any]) -> str:
    metadata = (
        skill.get("metadata")
        if isinstance(skill.get("metadata"), dict)
        else {}
    )
    return str(
        skill.get("source_path") or metadata.get("manifest_path") or ""
    ).strip()


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = value.replace(",", "\n").splitlines()
    elif isinstance(value, list):
        raw = value
    else:
        raw = []
    result = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _mentioned_skill_ids(text: str, skills: list[dict[str, Any]]) -> list[str]:
    aliases = _skill_alias_lookup(skills)
    ids: list[str] = []
    seen = set()
    for value in extract_mention_values(str(text or ""), aliases.keys()):
        token = _normalize_mention_token(value)
        if token.startswith("skill:"):
            token = token.split(":", 1)[1]
        skill_id = aliases.get(token)
        if not skill_id or skill_id in seen:
            continue
        seen.add(skill_id)
        ids.append(skill_id)
    return ids


def _resolve_skill_ids(values: list[str], skills: list[dict[str, Any]]) -> set[str]:
    aliases = _skill_alias_lookup(skills)
    result: set[str] = set()
    for value in values:
        normalized = _normalize_mention_token(value)
        skill_id = aliases.get(normalized)
        if skill_id:
            result.add(skill_id)
    return result


def _skill_alias_lookup(skills: list[dict[str, Any]]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for skill in skills:
        skill_id = str(skill.get("id") or "").strip()
        if not skill_id:
            continue
        values = [
            skill_id,
            skill_id.rsplit("/", 1)[-1],
            str(skill.get("display_name") or ""),
            str(skill.get("name") or ""),
        ]
        metadata = skill.get("metadata") if isinstance(skill.get("metadata"), dict) else {}
        aliases = skill.get("aliases") or metadata.get("aliases")
        if isinstance(aliases, list):
            values.extend(str(item) for item in aliases)
        for value in values:
            for alias in _alias_variants(value):
                lookup.setdefault(alias, skill_id)
    return lookup


def _alias_variants(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    variants = {text, text.replace(" ", "_"), text.replace("_", "-"), text.replace("/", "_"), text.replace("/", "-")}
    return [_normalize_mention_token(item) for item in variants if _normalize_mention_token(item)]


def _normalize_mention_token(value: str) -> str:
    return str(value or "").strip().strip(".,!?;:)]}）】」'\"").casefold()
