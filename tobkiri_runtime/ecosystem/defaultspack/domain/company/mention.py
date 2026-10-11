from __future__ import annotations

from typing import Any

from tobkiri_protocol.settings_state import SettingsOwnerPort

from ..mention import extract_mention_values
from .models import DEFAULT_CHANNEL_ID
from .store import CompanyStore


MENTION_ALIASES = {
    "pm": "project_manager",
    "project_manager": "project_manager",
    "coding_engineer": "coding_engineer",
    "reviewer": "reviewer",
    "ops_manager": "operations_manager",
    "operations_manager": "operations_manager",
    "scribe": "scribe",
}


def extract_mentions(text: str, known_values: list[str] | None = None) -> list[str]:
    mentions: list[str] = []
    seen: set[str] = set()
    for value in extract_mention_values(text, known_values):
        name = value.strip().lower()
        if name and name not in seen:
            seen.add(name)
            mentions.append(name)
    return mentions


class CompanyMentionService:
    def __init__(
        self,
        store: CompanyStore | None = None,
        *,
        settings_owner: SettingsOwnerPort | None = None,
    ) -> None:
        self.store = store or CompanyStore()
        self.settings_owner = settings_owner

    def resolve(self, company_id: str, text_or_mentions: str | list[str]) -> dict[str, Any] | None:
        company = self.store.get_company(company_id)
        if company is None:
            return None
        agents = company.get("agents", {})
        mentions = (
            extract_mentions(text_or_mentions, self._known_mentions(agents))
            if isinstance(text_or_mentions, str)
            else [
                str(item).strip().lstrip("@").lower()
                for item in text_or_mentions
                if str(item).strip()
            ]
        )
        resolved: list[dict[str, Any]] = []
        unresolved: list[str] = []
        seen_agents: set[str] = set()
        for mention in mentions:
            if mention in {"team", "channel"}:
                continue
            if mention == "all":
                for agent in agents.values():
                    agent_id = agent.get("agent_id")
                    if agent_id and agent_id not in seen_agents:
                        seen_agents.add(agent_id)
                        resolved.append(agent)
                continue
            target_key = MENTION_ALIASES.get(mention, mention)
            agent = self._find_agent(agents, target_key)
            if agent is None:
                unresolved.append(mention)
                continue
            agent_id = agent.get("agent_id")
            if agent_id not in seen_agents:
                seen_agents.add(agent_id)
                resolved.append(agent)
        return {
            "mentions": mentions,
            "resolved_agents": resolved,
            "resolved_agent_ids": [agent["agent_id"] for agent in resolved],
            "unresolved": unresolved,
        }

    @staticmethod
    def _known_mentions(agents: dict[str, dict[str, Any]]) -> list[str]:
        values = {*MENTION_ALIASES, "all", "channel", "team"}
        for agent in agents.values():
            metadata = (
                agent.get("metadata")
                if isinstance(agent.get("metadata"), dict)
                else {}
            )
            team = (
                metadata.get("subagent_team")
                if isinstance(metadata.get("subagent_team"), dict)
                else {}
            )
            values.update(
                str(value or "").strip()
                for value in (
                    agent.get("agent_id"),
                    agent.get("id"),
                    agent.get("role_key"),
                    metadata.get("short_id"),
                    team.get("short_id"),
                    team.get("legacy_short_id"),
                    *(agent.get("aliases") or []),
                )
                if str(value or "").strip()
            )
        return sorted(values)

    def create_message_task(
        self,
        company_id: str,
        *,
        content: str,
        sender_id: str = "user",
        channel_id: str = DEFAULT_CHANNEL_ID,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        from .message_router import CompanySlackRuntime

        return CompanySlackRuntime(
            company_store=self.store,
            settings_owner=self.settings_owner,
        ).post_message(
            company_id,
            content=content,
            sender_id=sender_id,
            channel_id=channel_id,
            metadata=metadata or {},
        )

    def _find_agent(self, agents: dict[str, dict[str, Any]], key: str) -> dict[str, Any] | None:
        for agent in agents.values():
            metadata = agent.get("metadata") if isinstance(agent.get("metadata"), dict) else {}
            team = metadata.get("subagent_team") if isinstance(metadata.get("subagent_team"), dict) else {}
            aliases = [str(alias).lower() for alias in agent.get("aliases", [])]
            if key in {
                str(metadata.get("short_id") or "").lower(),
                str(team.get("short_id") or "").lower(),
                str(team.get("legacy_short_id") or "").lower(),
                *aliases,
            }:
                return agent
        for agent in agents.values():
            aliases = [str(alias).lower() for alias in agent.get("aliases", [])]
            if key in {
                str(agent.get("agent_id", "")).lower(),
                str(agent.get("id", "")).lower(),
                str(agent.get("role_key", "")).lower(),
                *aliases,
            }:
                return agent
        return None
