"""Actual scheduler routing preserves finite due payloads at the schema edge.

These are unit boundary tests, not simulated Clock/grant evidence. The separate
captured Calendar test exercises the genuine Host wake and authority path.
"""

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError
import pytest


@pytest.fixture
def scheduler() -> Any:
    """Use the normal canonical implementation with no output overlays."""
    from ecosystem.rumi_scheduler_runtime_pack.runtime import scheduler

    return scheduler


@pytest.fixture
def client() -> Any:
    """Validate the exact emitted request with the unchanged official schema."""
    from core_runtime import host_provider_backend_v4

    runtime = Path(host_provider_backend_v4.__file__).resolve().parents[1]
    catalog = json.loads((runtime / "schemas/pack_v4_catalog.v1.json").read_text())
    pack = next(p for p in catalog["packs"] if p["pack_id"] == "rumi_schedule_store_pack")
    contract = next(
        c for c in pack["provided_contracts"] if c["contract_id"] == "tobkiri.resource.schedule.v1"
    )
    validator = Draft202012Validator(contract["schemas"]["input"])

    class FiniteClient:
        def __init__(self) -> None:
            self.requests = []

        def invoke(self, contract: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
            assert contract == "tobkiri.resource.schedule.v1"
            assert operation == "rumi_schedule_store_pack.schedule-resource"
            validator.validate(payload)
            self.requests.append(payload)
            return {"revision": 0, "schedules": []}

    return FiniteClient()


@pytest.mark.parametrize("arguments", [(), (None,)])
def test_untargeted_tick_omits_optional_identity(
    scheduler: Any, client: Any, arguments: tuple[Any, ...]
) -> None:
    """Missing or None target means a generic due read, not an empty ID."""
    runtime = scheduler.SchedulerRuntime(client, "defaults", canonical=True)
    assert runtime._tick(1000, 20, *arguments)["count"] == 0
    assert client.requests == [
        {"profile_id": "defaults", "operation": "due", "now_ms": 1000, "limit": 20}
    ]


def test_clock_control_uses_untargeted_due_shape(scheduler: Any, client: Any) -> None:
    """The actual canonical control entry reaches the same finite route."""
    runtime = scheduler.SchedulerRuntime(client, "defaults", canonical=True)
    assert runtime.control("tick", {"now_ms": 1000, "limit": 20})["count"] == 0
    assert "schedule_id" not in client.requests[-1]


def test_targeted_tick_keeps_exact_identity(scheduler: Any, client: Any) -> None:
    """A manual target is preserved and never broadened to all schedules."""
    runtime = scheduler.SchedulerRuntime(client, "defaults", canonical=True)
    runtime._tick(1000, 20, "selected-job")
    assert client.requests[0]["schedule_id"] == "selected-job"


@pytest.mark.parametrize("target", ["", "../wrong", 7, "x" * 257])
def test_explicit_invalid_target_rejected_before_due_effect(
    scheduler: Any, client: Any, target: Any
) -> None:
    """Explicit bad values remain present for strict owner-schema rejection."""
    runtime = scheduler.SchedulerRuntime(client, "defaults", canonical=True)
    with pytest.raises(ValidationError):
        runtime._tick(1000, 20, target)
    assert client.requests == []
