from blocks._common import ok, error
from domain.company.contract_facade import CompanyContractFacade, CompanyFacadeError

from ._helpers import company_id_from, invalid, missing_company, require_dict


def run(input_data, context):
    if require_dict(input_data) is None:
        return invalid("input_data must be a dict")
    company_id = company_id_from(input_data)
    if not company_id:
        return invalid("company_id is required")
    action = str(input_data.get("action") or "message").lower()
    content = str(input_data.get("content") or input_data.get("message") or "")
    if not content:
        return invalid("content is required")
    try:
        facade = CompanyContractFacade(input_data, context)
        if action == "resolve":
            result = facade.run("resolve_mentions")
            if result is None:
                return missing_company(company_id)
            return ok(result)
        result = facade.run("mention")
        if result is None:
            return missing_company(company_id)
        return ok(result)
    except CompanyFacadeError as exc:
        return error(str(exc), exc.code)
    except Exception as exc:
        return error("company mention failed: " + str(exc), "COMPANY_MENTION_ERROR")
