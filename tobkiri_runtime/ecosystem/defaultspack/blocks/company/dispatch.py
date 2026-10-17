from blocks._common import ok, error
from domain.company.contract_facade import CompanyContractFacade, CompanyFacadeError

from ._helpers import company_id_from, invalid, require_dict


def run(input_data, context):
    if require_dict(input_data) is None:
        return invalid("input_data must be a dict")
    company_id = company_id_from(input_data)
    task_id = input_data.get("task_id") or input_data.get("id")
    if not company_id:
        return invalid("company_id is required")
    if not task_id:
        return invalid("task_id is required")
    try:
        request = {**input_data, "task_id": str(task_id)}
        return ok(CompanyContractFacade(request, context).run("dispatch_task"))
    except CompanyFacadeError as exc:
        return error(str(exc), exc.code)
    except Exception as exc:
        return error("company dispatch failed: " + str(exc), "COMPANY_DISPATCH_ERROR")
