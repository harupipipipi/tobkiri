from blocks._common import error, ok
from domain.company.service import CompanyService
from domain.subagent_team.availability import (
    settings_owner_from_context,
    subagent_delegation_enabled,
    subagents_disabled_result,
)


def run(input_data, context, *, settings_owner=None):
    settings_owner = settings_owner_from_context(settings_owner, context)
    try:
        if not subagent_delegation_enabled(settings_owner=settings_owner):
            disabled = subagents_disabled_result()
            return error(disabled["message"], disabled["code"])
        if not isinstance(input_data, dict):
            input_data = {}
        raw_metadata = input_data.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        metadata = {
            "subagent_team": True,
            "surface": "subagent_team_workspace",
            **metadata,
        }
        conversation_id = str(input_data.get("conversation_id") or metadata.get("conversation_id") or "").strip()
        scope = str(input_data.get("scope") or metadata.get("scope") or "").strip()
        service = CompanyService(settings_owner=settings_owner)
        if conversation_id and scope in {"conversation", "chat", "main_chat"}:
            company = service.bootstrap_conversation_company(conversation_id, metadata=metadata)
        else:
            company = service.bootstrap_default_company(metadata=metadata)
        return ok({"bootstrapped": True, "company": company})
    except Exception as exc:
        return error("subagent team bootstrap failed: " + str(exc), "SUBAGENT_TEAM_BOOTSTRAP_ERROR")
