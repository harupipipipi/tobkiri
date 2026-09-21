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
    if action == "delete" and not input_data.get("route_id"):
        if not input_data.get("id"):
            return invalid("route_id is required")
        input_data = {**input_data, "route_id": input_data["id"]}
    try:
        facade = CompanyContractFacade(input_data, context)
        if action == "list":
            routes = facade.run("list_routes")
            return ok({"routes": routes, "total": len(routes)}) if routes is not None else missing_company(company_id)
        if action in {"upsert", "create", "update"}:
            route = facade.run("upsert_route")
            return ok(route) if route is not None else missing_company(company_id)
        if action == "delete":
            deleted = facade.run("delete_route")
            return ok({"deleted": True, "route_id": input_data["route_id"]}) if deleted else error("route not found", "NOT_FOUND")
        if action == "ingest":
            if not input_data.get("content"):
                return invalid("content is required")
            result = facade.run("append_inbound")
            return ok(result) if result is not None else missing_company(company_id)
        return invalid("unsupported inbound_routes action: " + action)
    except CompanyFacadeError as exc:
        return error(str(exc), exc.code)
    except Exception as exc:
        return error("company inbound routes failed: " + str(exc), "COMPANY_INBOUND_ROUTES_ERROR")
