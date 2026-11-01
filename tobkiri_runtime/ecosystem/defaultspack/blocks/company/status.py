from blocks._common import ok, error
from domain.company.contract_facade import CompanyContractFacade, CompanyFacadeError

from ._helpers import company_id_from


def run(input_data, context):
    try:
        if not isinstance(input_data, dict):
            input_data = {}
        request = dict(input_data)
        if not request.get("company_id") and not request.get("conversation_id"):
            request["company_id"] = company_id_from(input_data) or "operations-company"
        return ok(CompanyContractFacade(request, context).run("status"))
    except CompanyFacadeError as exc:
        return error(str(exc), exc.code)
    except Exception as exc:
        return error("company status failed: " + str(exc), "COMPANY_STATUS_ERROR")
