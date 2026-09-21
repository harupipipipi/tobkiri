"""Contract tests for model-agnostic computer hosts."""

from __future__ import annotations

import pytest

from core_runtime.host_broker.computer_contract import (
    ComputerHost,
    ComputerHostActionResult,
    ComputerHostCapabilities,
    ComputerHostExecutionOptions,
    ComputerHostObservation,
    ComputerHostTarget,
)
from ecosystem.rumi_default_tools_pack.domain.computer.host_adapter import (
    ComputerSeatHostAdapter,
)
from ecosystem.rumi_default_tools_pack.domain.computer.models import (
    ActionResult,
    ComputerCapabilities,
    ObserveResult,
)
from ecosystem.rumi_default_tools_pack.domain.computer.registry import DriverRegistry
from ecosystem.rumi_default_tools_pack.domain.computer.service import ComputerSeatService
from ecosystem.rumi_default_tools_pack.domain.computer.tool_service import ComputerToolService

pytestmark = pytest.mark.contract


class FakeComputerHost:
    """Small fake proving packs can test without an OS-native driver."""

    def probe(self) -> ComputerHostCapabilities:
        return ComputerHostCapabilities(
            host_id="fake",
            platform="test",
            primitives=("click",),
            can_verify=True,
        )

    def list_surfaces(self) -> list[dict[str, object]]:
        return [{"surface_id": "window:7", "title": "Fake Window"}]

    def observe(self, target: ComputerHostTarget) -> ComputerHostObservation:
        return ComputerHostObservation(
            surface_id=target.surface_id,
            observation_revision="rev-2",
            coordinate_space=target.coordinate_space,
            data={"title": "Saved"},
        )

    def execute_primitive(
        self,
        target: ComputerHostTarget,
        primitive: str,
        args: dict[str, object],
        options: ComputerHostExecutionOptions | None = None,
    ) -> ComputerHostActionResult:
        return ComputerHostActionResult(
            transport="fake",
            delivered=primitive == "click",
            effect_observed=True,
            surface_id=target.surface_id,
            observation_revision=target.observation_revision,
            coordinate_space=target.coordinate_space,
            data=args,
        )

    def verify(
        self,
        target: ComputerHostTarget,
        expected_postcondition: dict[str, object],
    ) -> ComputerHostActionResult:
        verified = expected_postcondition == {"title": "Saved"}
        return ComputerHostActionResult(
            transport="fake",
            delivered=True,
            effect_observed=verified,
            postcondition_verified=verified,
            surface_id=target.surface_id,
            observation_revision="rev-2",
            coordinate_space=target.coordinate_space,
        )


class FakeDriver:
    name = "fake_driver"
    platform = "test"

    def capabilities(self) -> ComputerCapabilities:
        return ComputerCapabilities(can_semantic_action=True)

    def observe(self, target) -> ObserveResult:
        return ObserveResult(
            platform="test",
            target_window={
                "surface_id": "window:7",
                "observation_revision": "rev-2",
            },
            ax_tree={"title": "Saved"},
        )

    def click(self, target, x=0, y=0, button="left") -> ActionResult:
        return ActionResult(
            action="click",
            driver=self.name,
            executed=True,
            confidence="verified",
            can_parallel_user_work=True,
            data={"x": x, "y": y, "button": button},
        )

    def is_available(self) -> bool:
        return True


def _adapter() -> ComputerSeatHostAdapter:
    registry = DriverRegistry()
    registry.register(FakeDriver())
    service = ComputerSeatService(registry)
    service._platform = "test"
    return ComputerSeatHostAdapter(service)


def test_fake_host_satisfies_contract_and_distinguishes_result_states() -> None:
    host = FakeComputerHost()
    target = ComputerHostTarget(
        surface_id="window:7",
        observation_revision="rev-1",
        coordinate_space="client",
    )

    assert isinstance(host, ComputerHost)
    delivered = host.execute_primitive(target, "click", {"x": 12, "y": 8})
    verified = host.verify(target, {"title": "Saved"})

    assert delivered.delivered is True
    assert delivered.effect_observed is True
    assert delivered.postcondition_verified is False
    assert verified.postcondition_verified is True
    assert delivered.surface_id == target.surface_id
    assert delivered.observation_revision == "rev-1"


def test_computer_seat_adapter_preserves_surface_binding() -> None:
    host = _adapter()
    target = ComputerHostTarget(
        surface_id="window:7",
        observation_revision="rev-2",
        coordinate_space="client",
        selectors={"app": "Fake", "window_id": 7},
    )

    observation = host.observe(target)
    action = host.execute_primitive(target, "click", {"x": 12, "y": 8})

    assert observation.surface_id == "window:7"
    assert observation.observation_revision == "rev-2"
    assert action.transport == "fake_driver"
    assert action.delivered is True
    assert action.surface_id == "window:7"
    assert action.observation_revision == "rev-2"


def test_computer_seat_adapter_rejects_tool_level_action_names() -> None:
    host = _adapter()
    target = ComputerHostTarget(surface_id="window:7")

    result = host.execute_primitive(target, "computer.click", {"x": 1, "y": 2})

    assert result.delivered is False
    assert result.transport == "none"
    assert "Unsupported computer primitive" in result.data["error"]


def test_computer_seat_adapter_rejects_stale_observation_revision() -> None:
    host = _adapter()
    target = ComputerHostTarget(
        surface_id="window:7",
        observation_revision="rev-1",
        selectors={"window_id": 7},
    )

    result = host.execute_primitive(target, "click", {"x": 12, "y": 8})

    assert result.delivered is False
    assert result.transport == "none"
    assert result.data["error_code"] == "STALE_COMPUTER_OBSERVATION"


def test_pack_tool_service_routes_actions_through_host_contract() -> None:
    host = FakeComputerHost()
    service = ComputerToolService(host)

    result = service.click(
        {
            "surface_id": "window:7",
            "observation_revision": "rev-1",
            "coordinate_space": "client",
        },
        x=12,
        y=8,
    )

    assert result["executed"] is True
    assert result["delivered"] is True
    assert result["effect_observed"] is True
    assert result["postcondition_verified"] is False
    assert result["driver"] == "fake"
    assert result["surface_id"] == "window:7"
    assert result["observation_revision"] == "rev-1"
