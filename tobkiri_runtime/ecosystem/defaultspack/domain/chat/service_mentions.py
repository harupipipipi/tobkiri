"""Verify exact requested members of a service explicitly named in user text."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from domain.mention import is_mention_start
from domain.tool.service_catalog import ToolServiceCatalog


def explicit_service_member_ids(
    user_text: str,
    tools: list[dict[str, Any]],
    requested_tool_ids: set[str],
) -> set[str]:
    """Ground requested IDs in positive, unambiguous current service mentions.

    This verifies selection intent only. It neither selects additional members
    nor changes profile, connection, capability, or execution approval policy.
    """
    if "@" not in user_text or not requested_tool_ids:
        return set()
    records = ToolServiceCatalog(tools).compact_records()
    tool_spellings = {
        str(value).casefold()
        for tool in tools
        for value in (
            tool.get("tool_id"), tool.get("display_name"), tool.get("name"),
        )
        if value
    }
    tool_spellings.update(record["tool_id"].casefold() for record in records)
    aliases: dict[str, set[str]] = {}
    for record in records:
        service_id = record["service_id"]
        for value in (service_id, record["service_label"]):
            aliases.setdefault(value.casefold(), set()).add(service_id)
    mentioned_services: set[str] = set()
    known_values = [*aliases, *tool_spellings]
    for alias, service_ids in aliases.items():
        if len(service_ids) != 1 or alias in tool_spellings:
            continue
        pattern = re.compile(rf"@{re.escape(alias)}", re.IGNORECASE)
        if any(
            is_mention_start(user_text, match.start(), known_values)
            and (
                match.end() == len(user_text)
                or (
                    user_text[match.end()] not in "_./:-"
                    and unicodedata.category(user_text[match.end()])[0]
                    not in {"L", "M", "N"}
                )
            )
            for match in pattern.finditer(user_text)
        ):
            mentioned_services.update(service_ids)
    return {
        record["tool_id"]
        for record in records
        if record["tool_id"] in requested_tool_ids
        and record["service_id"] in mentioned_services
    }
