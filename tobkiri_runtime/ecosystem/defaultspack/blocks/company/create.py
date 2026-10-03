from blocks._common import ok, error
from domain.company.contract_facade import CompanyContractFacade, CompanyFacadeError

from ._helpers import invalid, require_dict


def run(input_data, context):
    if require_dict(input_data) is None:
        return invalid("input_data must be a dict")
    try:
        return ok(CompanyContractFacade(input_data, context).run("create"))
    except CompanyFacadeError as exc:
        return error(str(exc), exc.code)
    except ValueError as exc:
        return invalid(str(exc))
    except Exception as exc:
        return error("company create failed: " + str(exc), "COMPANY_CREATE_ERROR")
