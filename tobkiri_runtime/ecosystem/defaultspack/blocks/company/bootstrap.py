from blocks._common import ok, error
from domain.company.contract_facade import CompanyContractFacade, CompanyFacadeError


def run(input_data, context):
    try:
        if not isinstance(input_data, dict):
            input_data = {}
        request = dict(input_data)
        metadata = request.get("metadata")
        if not request.get("conversation_id") and isinstance(metadata, dict):
            request["conversation_id"] = metadata.get("conversation_id")
        return ok(CompanyContractFacade(request, context).run("bootstrap"))
    except CompanyFacadeError as exc:
        return error(str(exc), exc.code)
    except Exception as exc:
        return error("company bootstrap failed: " + str(exc), "COMPANY_BOOTSTRAP_ERROR")
