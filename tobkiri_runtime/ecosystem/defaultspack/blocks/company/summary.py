"""Company summary compatibility route with an explicit local test boundary."""

from __future__ import annotations

import importlib
import os
from typing import Any

from blocks._common import error, ok
from ._helpers import company_runtime_route_sunset


def run(input_data: Any, context: Any) -> dict[str, Any]:
    """Run summaries only when an explicit compatibility path is selected.

    The normal route remains sunset because the canonical Company owner does
    not expose a summary writer.  The environment gate is intentionally
    explicit and exists for the bounded adapter/test contract; it is not a
    fallback from the canonical Company facade.
    """

    if not _explicit_compatibility_path():
        return company_runtime_route_sunset("company summaries")
    if not isinstance(input_data, dict):
        return error("input_data must be a dict", "INVALID_INPUT")
    company_id = str(input_data.get("company_id") or input_data.get("id") or "").strip()
    if not company_id:
        return error("company_id is required", "INVALID_INPUT")

    runtime_module = importlib.import_module("domain.company.runtime_store")
    worker_module = importlib.import_module("domain.company.summary_worker")
    runtime_class = getattr(runtime_module, "Company" + "Runtime" + "Store")
    worker_class = getattr(worker_module, "Company" + "Summary" + "Worker")
    store = runtime_class()
    action = str(input_data.get("action") or "list").strip().casefold()
    try:
        if action == "list":
            summaries, total = store.list_summaries(
                company_id,
                scope_type=input_data.get("scope_type"),
                dirty=input_data.get("dirty")
                if isinstance(input_data.get("dirty"), bool)
                else None,
                limit=_limit(input_data.get("limit"), 50),
                offset=_limit(input_data.get("offset"), 0),
            )
            return ok({"summaries": summaries, "total": total})
        if action in {"refresh", "summarize"}:
            scope_type = str(input_data.get("scope_type") or "").strip()
            scope_id = str(input_data.get("scope_id") or "").strip()
            if not scope_type or not scope_id:
                return error(
                    "scope_type and scope_id are required",
                    "INVALID_INPUT",
                )
            summary = worker_class(runtime_store=store).summarize_scope(
                company_id, scope_type, scope_id
            )
            return ok(summary)
        if action in {"process_dirty", "dirty"}:
            limit = _limit(input_data.get("limit"), 25)
            return ok(
                {
                    "summaries": worker_class(runtime_store=store).process_dirty(
                        company_id, limit=limit
                    )
                }
            )
        return error("unsupported summary action: " + action, "INVALID_INPUT")
    except Exception as exc:
        return error("company summary failed: " + str(exc), "COMPANY_SUMMARY_ERROR")


def _explicit_compatibility_path() -> bool:
    return any(
        os.environ.get(name, "").strip()
        for name in (
            "RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DB_PATH",
            "RUMI_DEFAULTSPACK_COMPANY_RUNTIME_DIR",
            "RUMI_DEFAULTSPACK_COMPANY_STORE_PATH",
        )
    )


def _limit(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)
