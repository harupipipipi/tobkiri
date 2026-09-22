"""Truthfulness tests for production Wasm resource-controller admission."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

from tobkiri_host import resource_controller
from tobkiri_host.resource_controller import (
    ResourceControllerStatus,
    detect_production_resource_controller,
)
from tobkiri_host.wasm_backend import WasmComponentBackend
from tobkiri_protocol.canonical import canonical_digest


class _HardController:
    status = ResourceControllerStatus(
        controller_id="test-hard-controller",
        production_eligible=True,
        hard_physical_memory_limit=True,
        detail="test-only hard controller",
    )

    def prepare(self, memory_limit_bytes: int) -> object:
        raise AssertionError("status construction must not launch a worker")


def test_macos_process_monitoring_is_never_a_production_hard_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resource_controller.sys, "platform", "darwin")
    controller, status = detect_production_resource_controller()
    assert controller is None
    assert not status.production_eligible
    assert not status.hard_physical_memory_limit
    assert "VZ/PackVM" in status.detail
    assert "diagnostic only" in status.detail


def test_linux_without_delegated_cgroup_v2_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(resource_controller.sys, "platform", "linux")
    monkeypatch.setattr(
        resource_controller,
        "_current_unified_cgroup",
        lambda: Path("missing-delegation"),
    )
    original_resolve = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        if path == Path("/sys/fs/cgroup"):
            return tmp_path.resolve()
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    controller, status = detect_production_resource_controller()
    assert controller is None
    assert status.controller_id == "linux-cgroup-v2-unavailable"
    assert not status.production_eligible
    assert not status.hard_physical_memory_limit
    assert "unavailable" in status.detail


def test_backend_only_claims_resource_gate_for_hard_controller() -> None:
    command = (sys.executable, "-I", "-B", "-c", "pass")
    parameters = {
        "worker_command_digest": canonical_digest(list(command)),
        "worker_runtime_digest": canonical_digest({"runtime": "test"}),
    }
    development = WasmComponentBackend(command, **parameters)
    assert not development.status.ready_for_production
    assert development.status.conformance_only
    assert "resource_controller" not in development.status.satisfied_gates
    assert development.resource_controller_status.controller_id == (
        "development-rss-sampler"
    )

    production = WasmComponentBackend(
        command,
        resource_controller=_HardController(),
        **parameters,
    )
    assert production.status.ready_for_production
    assert "resource_controller" in production.status.satisfied_gates
    assert production.status.backend_digest != development.status.backend_digest


def test_backend_rejects_hard_status_without_matching_controller() -> None:
    command = (sys.executable, "-I", "-B", "-c", "pass")
    parameters = {
        "worker_command_digest": canonical_digest(list(command)),
        "worker_runtime_digest": canonical_digest({"runtime": "test"}),
    }
    hard = _HardController.status
    with pytest.raises(ValueError, match="controller is missing"):
        WasmComponentBackend(
            command,
            resource_controller_status=hard,
            **parameters,
        )
    with pytest.raises(ValueError, match="status mismatch"):
        WasmComponentBackend(
            command,
            resource_controller=_HardController(),
            resource_controller_status=ResourceControllerStatus(
                controller_id="forged",
                production_eligible=True,
                hard_physical_memory_limit=True,
                detail="forged",
            ),
            **parameters,
        )
