"""Expose the Plan-pinned, signed AI strategy choice catalog."""

from __future__ import annotations

from typing import Any, Mapping

from blocks._common import error, ok
from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import invoke_global_contract
from ecosystem.defaultspack.defaultspack.ai_strategy_presentation import (
    present_ai_strategy_catalog,
)


_CATALOG_CONTRACT = "tobkiri.resource.ai.strategy.catalog.v1"
_CATALOG_OPERATION = "rumi_ai_strategy_runtime_pack.ai-strategy.catalog"


def run(
    input_data: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
) -> Mapping[str, object]:
    """Return only strategy contributions verified against the active Plan."""

    del context
    if input_data and any(key != "_method" for key in input_data):
        return error("strategy catalog accepts no parameters", "INVALID_INPUT")
    session = get_container().get_or_none("v4_dispatch_session")
    if session is None:
        return error("strategy catalog is unavailable", "STRATEGY_CATALOG_UNAVAILABLE")
    try:
        session.assert_current()
        catalog = invoke_global_contract(
            session,
            _CATALOG_CONTRACT,
            _CATALOG_OPERATION,
            {},
        )
        if not isinstance(catalog, Mapping):
            raise ValueError("strategy catalog response is invalid")
        return ok(present_ai_strategy_catalog(catalog, session=session))
    except Exception:
        # An empty, diagnostic-bearing catalog keeps Settings usable while
        # ensuring the browser cannot select a stale or unverified strategy.
        return ok(present_ai_strategy_catalog({}, session=None))
