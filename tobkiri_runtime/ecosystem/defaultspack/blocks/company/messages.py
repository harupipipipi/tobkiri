from blocks._common import ok, error
from domain.company.contract_facade import CompanyContractFacade, CompanyFacadeError

from ._helpers import (
    company_id_from,
    invalid,
    missing_company,
    require_dict,
    subagent_team_write_denied_for_company,
)


def run(input_data, context):
    if require_dict(input_data) is None:
        return invalid("input_data must be a dict")
    company_id = company_id_from(input_data)
    if not company_id:
        return invalid("company_id is required")
    action = str(input_data.get("action") or "list").lower()
    try:
        facade = CompanyContractFacade(input_data, context)
        if action == "list":
            result = facade.run("list_messages")
            if result is None:
                return missing_company(company_id)
            return ok(result)
        if action == "get":
            message_id = input_data.get("message_id") or input_data.get("id")
            if not message_id:
                return invalid("message_id is required")
            request = {**input_data, "message_id": str(message_id)}
            message = CompanyContractFacade(request, context).run("get_message")
            if message is None:
                return error("message not found: " + str(message_id), "NOT_FOUND")
            return ok(message)
        if action in {"add", "create"}:
            company = facade.run("get")
            if company is None:
                return missing_company(company_id)
            blocked = subagent_team_write_denied_for_company(company)
            if blocked is not None:
                return blocked
            content = input_data.get("content")
            if not content:
                return invalid("content is required")
            result = facade.run("append_message")
            if result is None:
                return missing_company(company_id)
            return ok(result)
        return invalid("unsupported messages action: " + action)
    except CompanyFacadeError as exc:
        return error(str(exc), exc.code)
    except Exception as exc:
        return error("company messages failed: " + str(exc), "COMPANY_MESSAGES_ERROR")
