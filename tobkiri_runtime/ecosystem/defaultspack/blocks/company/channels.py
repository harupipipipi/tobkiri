from blocks._common import error, ok
from domain.company.contract_facade import CompanyContractFacade, CompanyFacadeError

from ._helpers import company_id_from, invalid, missing_company, require_dict


def run(input_data, context):
    if require_dict(input_data) is None:
        return invalid("input_data must be a dict")
    company_id = company_id_from(input_data)
    if not company_id:
        return invalid("company_id is required")
    action = str(input_data.get("action") or "list").lower()
    if action in {"get", "delete", "remove"}:
        if not input_data.get("channel_id") and not input_data.get("id"):
            return invalid("channel_id is required")
        if not input_data.get("channel_id"):
            input_data = {**input_data, "channel_id": input_data["id"]}
    try:
        facade = CompanyContractFacade(input_data, context)
        if action == "list":
            channels = facade.run("list_channels")
            if channels is None:
                return missing_company(company_id)
            return ok({"channels": channels, "total": len(channels)})
        if action == "get":
            channel = facade.run("get_channel")
            if channel is None:
                return error("channel not found", "NOT_FOUND")
            return ok(channel)
        if action in {"upsert", "create", "update"}:
            updated = facade.run("upsert_channel")
            if updated is None:
                return missing_company(company_id)
            return ok(updated)
        if action in {"delete", "remove"}:
            deleted = facade.run("delete_channel")
            if not deleted:
                return error("channel not found", "NOT_FOUND")
            return ok({"deleted": True, "channel_id": input_data["channel_id"]})
        return invalid("unsupported channels action: " + action)
    except CompanyFacadeError as exc:
        return error(str(exc), exc.code)
    except Exception as exc:
        return error("company channels failed: " + str(exc), "COMPANY_CHANNELS_ERROR")
