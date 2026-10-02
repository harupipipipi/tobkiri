from blocks._common import ok, error
from domain.company.contract_facade import CompanyContractFacade, CompanyFacadeError

from ._helpers import company_id_from, invalid, missing_company, require_dict


def run(input_data, context):
    if require_dict(input_data) is None:
        return invalid("input_data must be a dict")
    company_id = company_id_from(input_data)
    if not company_id:
        return invalid("company_id is required")
    try:
        deleted = CompanyContractFacade(input_data, context).run("delete")
        if not deleted:
            return missing_company(company_id)
        return ok({"deleted": True, "company_id": company_id})
    except CompanyFacadeError as exc:
        return error(str(exc), exc.code)
    except Exception as exc:
        return error("company delete failed: " + str(exc), "COMPANY_DELETE_ERROR")
