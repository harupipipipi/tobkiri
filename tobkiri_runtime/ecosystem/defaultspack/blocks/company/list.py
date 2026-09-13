from blocks._common import ok, error
from domain.company.contract_facade import CompanyContractFacade, CompanyFacadeError


def run(input_data, context):
    try:
        if not isinstance(input_data, dict):
            input_data = {}
        return ok(CompanyContractFacade(input_data, context).run("list"))
    except CompanyFacadeError as exc:
        return error(str(exc), exc.code)
    except Exception as exc:
        return error("company list failed: " + str(exc), "COMPANY_LIST_ERROR")
