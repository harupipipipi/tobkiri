from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from blocks._common import gen_id, timestamp
from domain.extensions.manifest import validate_manifest
from domain.extensions.runtime import get_extension_registry
from domain.memory2.dreaming import record_dream


_SLUG_RE = re.compile(r"[^a-z0-9_]+")
_ALLOWED_PAYLOAD_KEYS = {
    "feedback",
    "correction",
    "applies_to_tools",
    "tool_ids",
    "triggers",
    "keywords",
    "skill_id",
    "name",
    "display_name",
    "description",
    "conversation_id",
    "message_id",
}


def create_skill_from_feedback(payload: dict[str, Any]) -> dict[str, Any]:
    payload = _sanitize_payload(payload)
    feedback = str(payload.get("feedback") or payload.get("correction") or "").strip()
    if not feedback:
        raise ValueError("feedback is required")
    tool_ids = _as_list(payload.get("applies_to_tools") or payload.get("tool_ids"))
    triggers = _as_list(payload.get("triggers") or payload.get("keywords"))
    if not triggers:
        triggers = _trigger_words(feedback)
    skill_id = _skill_id(payload.get("skill_id") or payload.get("name") or feedback)
    manifest = {
        "id": f"feedback/{skill_id}",
        "category": "skill",
        "version": "1",
        "enabled": False,
        "display_name": str(payload.get("display_name") or f"Feedback: {skill_id}"),
        "description": str(payload.get("description") or feedback[:240]),
        "triggers": triggers,
        "applies_to_tools": tool_ids,
        "metadata": {
            "source": "skill_create_from_feedback",
            "feedback": feedback,
            "conversation_id": str(payload.get("conversation_id") or ""),
            "message_id": str(payload.get("message_id") or ""),
            "created_at": timestamp(),
            "draft_agent": "rumi-feedback-skill-writer",
        },
    }
    validated = validate_manifest(manifest, expected_category="skill")
    root = _skills_root(payload)
    skill_dir = root / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = skill_dir / "manifest.json"
    manifest_path.write_text(json.dumps(validated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    dream_path = record_dream(
        "[feedback-skill] {} -> {} triggers={} tools={}".format(
            feedback,
            validated["id"],
            ",".join(triggers),
            ",".join(tool_ids),
        )
    )
    get_extension_registry(force_reload=True)
    return {
        "skill_id": validated["id"],
        "manifest": validated,
        "manifest_path": str(manifest_path),
        "dream_path": dream_path,
        "enabled": bool(validated.get("enabled", False)),
        "activation_required": True,
    }


def _sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    unknown = sorted(str(key) for key in payload if str(key) not in _ALLOWED_PAYLOAD_KEYS)
    if unknown:
        raise ValueError("unsupported fields: " + ", ".join(unknown))
    return {str(key): value for key, value in payload.items() if str(key) in _ALLOWED_PAYLOAD_KEYS}


def _skills_root(payload: dict[str, Any]) -> Path:
    return Path(__file__).resolve().parents[1] / "user_data" / "shared" / "extensions" / "skills"


def _skill_id(value: Any) -> str:
    text = str(value or "").strip().casefold()
    ascii_ish = _SLUG_RE.sub("_", text.encode("ascii", "ignore").decode("ascii"))
    ascii_ish = re.sub(r"_+", "_", ascii_ish).strip("_")
    if not ascii_ish:
        ascii_ish = gen_id("feedback_skill_")
    if not ascii_ish[0].isalpha():
        ascii_ish = "feedback_" + ascii_ish
    return ascii_ish[:64]


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = value.replace(",", "\n").splitlines()
    elif isinstance(value, list):
        raw = value
    else:
        raw = []
    result: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _trigger_words(feedback: str) -> list[str]:
    words = [word for word in re.split(r"\s+", feedback.strip()) if len(word) >= 3]
    triggers = []
    for word in words[:6]:
        cleaned = re.sub(r"^[^\w\u3040-\u30ff\u3400-\u9fff]+|[^\w\u3040-\u30ff\u3400-\u9fff]+$", "", word)
        if cleaned and cleaned not in triggers:
            triggers.append(cleaned)
    return triggers or ["feedback", "correction"]
