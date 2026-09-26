from blocks._common import error, ok
from domain.company.service import CompanyService
from domain.company.store import CompanyStore

from ._helpers import invalid, missing_team, require_dict


def run(input_data, context, *, settings_owner=None):
    del context
    try:
        if require_dict(input_data) is None:
            return invalid("input_data must be a dict")
        metadata = input_data.get("metadata")
        if not isinstance(metadata, dict):
            return invalid("metadata must be a dict")

        company_id = str(input_data.get("company_id") or "").strip()
        conversation_id = str(input_data.get("conversation_id") or metadata.get("conversation_id") or "").strip()
        store = CompanyStore()
        if not company_id and conversation_id:
            company = store.find_company_by_conversation_id(conversation_id)
            company_id = str((company or {}).get("id") or "").strip()
        if not company_id:
            return invalid("company_id is required")

        company = CompanyService(
            store,
            settings_owner=settings_owner,
        ).update_company(company_id, {"metadata": metadata})
        if company is None:
            return missing_team(company_id)
        return ok(company)
    except Exception as exc:
        return error("subagent team metadata update failed: " + str(exc), "SUBAGENT_TEAM_METADATA_ERROR")
