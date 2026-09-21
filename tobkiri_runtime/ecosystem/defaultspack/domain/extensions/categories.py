from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class CategorySpec:
    category_id: str
    manifest_glob: str


DEFAULT_CATEGORY_SPECS: Dict[str, CategorySpec] = {
    "llm_provider": CategorySpec(
        category_id="llm_provider",
        manifest_glob="llm/providers/*/manifest.json",
    ),
    "llm_model": CategorySpec(
        category_id="llm_model",
        manifest_glob="llm/providers/*/models/*.json",
    ),
    "prompt": CategorySpec(
        category_id="prompt",
        manifest_glob="prompts/*/manifest.json",
    ),
    "tool": CategorySpec(
        category_id="tool",
        manifest_glob="tools/*/manifest.json",
    ),
    "skill": CategorySpec(
        category_id="skill",
        manifest_glob="skills/*/manifest.json",
    ),
    "activity": CategorySpec(
        category_id="activity",
        manifest_glob="activities/*/manifest.json",
    ),
    "chat_mode": CategorySpec(
        category_id="chat_mode",
        manifest_glob="chat_modes/*/manifest.json",
    ),
    "agent_mode": CategorySpec(
        category_id="agent_mode",
        manifest_glob="agent_modes/*/manifest.json",
    ),
    "knowledge_backend": CategorySpec(
        category_id="knowledge_backend",
        manifest_glob="knowledge_backends/*/manifest.json",
    ),
    "transport": CategorySpec(
        category_id="transport",
        manifest_glob="transports/*/manifest.json",
    ),
    "ui_surface": CategorySpec(
        category_id="ui_surface",
        manifest_glob="ui/*/manifest.json",
    ),
    "policy": CategorySpec(
        category_id="policy",
        manifest_glob="policies/*/manifest.json",
    ),
}
