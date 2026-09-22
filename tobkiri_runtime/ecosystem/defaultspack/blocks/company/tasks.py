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
    if action in {"get", "update", "delete", "remove"} and not input_data.get("task_id"):
        if not input_data.get("id"):
            return invalid("task_id is required")
        input_data = {**input_data, "task_id": input_data["id"]}
    try:
        facade = CompanyContractFacade(input_data, context)
        if action == "list":
            tasks = facade.run("list_tasks")
            if tasks is None:
                return missing_company(company_id)
            return ok({"tasks": tasks, "total": len(tasks)})
        if action == "get":
            task = facade.run("get_task")
            return ok(task) if task is not None else error("task not found", "NOT_FOUND")
        if action in {"create", "add", "update"}:
            task = facade.run("upsert_task")
            return ok(task) if task is not None else missing_company(company_id)
        if action in {"delete", "remove"}:
            deleted = facade.run("delete_task")
            return ok({"deleted": True, "task_id": input_data["task_id"]}) if deleted else error("task not found", "NOT_FOUND")
        return invalid("unsupported tasks action: " + action)
    except CompanyFacadeError as exc:
        return error(str(exc), exc.code)
    except Exception as exc:
        return error("company tasks failed: " + str(exc), "COMPANY_TASKS_ERROR")
