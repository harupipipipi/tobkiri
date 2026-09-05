from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from ecosystem.defaultspack.backend.sandbox.control_lease import ControlLeaseManager
from ecosystem.defaultspack.backend.sandbox.errors import (
    DESKTOP_CONTROL_CONFLICT,
    DESKTOP_LEASE_EXPIRED,
    DESKTOP_LEASE_REQUIRED,
    INVALID_EXEC_REQUEST,
    RAW_COMMAND_REJECTED,
    SandboxContractError,
)
from ecosystem.defaultspack.backend.sandbox.frame_cache import FrameCache
from ecosystem.defaultspack.backend.sandbox.guest.protocol import DesktopInputRequest, GuestExecRequest
from ecosystem.defaultspack.backend.sandbox.models import (
    DesktopSpec,
    FilesystemPolicy,
    LifecyclePolicy,
    NetworkPolicy,
    OperationResult,
    ProgressEvent,
    ResolvedSandboxTemplate,
    ResourceLimits,
    RuntimeRequirements,
    SecretsPolicy,
)
from ecosystem.defaultspack.backend.sandbox.operation_store import RuntimeOperationStore
from ecosystem.defaultspack.backend.sandbox.provider_registry import ProviderRegistry
from ecosystem.defaultspack.backend.sandbox.sandbox_manager import SandboxManager
from ecosystem.defaultspack.backend.sandbox.testing.fake_guest_agent import FakeGuestAgent
from ecosystem.defaultspack.backend.sandbox.testing.fake_provider import FakeRuntimeProvider
from ecosystem.defaultspack.domain.tool_policy.internal_context import mark_tool_server_approval_context, seal_tool_context
from ecosystem.defaultspack.domain.coding.workspace_store import WorkspaceStore

DEFAULTSPACK_ROOT = Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack"


pytestmark = pytest.mark.contract


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _trusted_workspace(tmp_path, monkeypatch, *, workspace_id: str = "workspace-1"):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CODING_WORKSPACE_STORE_PATH", str(tmp_path / "workspaces.json"))
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    WorkspaceStore().create(root, workspace_id=workspace_id, trusted=True)
    return root


def _wait_for_runtime_operation(api, operation_id: str, *, timeout_seconds: float = 3.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, object] | None = None
    while time.monotonic() < deadline:
        latest = api.run({"_handler": "runtime_operation_get", "operation_id": operation_id}, {})
        if latest.get("status") == "ok":
            data = latest.get("data")
            if isinstance(data, dict) and data.get("status") in {"completed", "failed", "cancelled"}:
                return data
        time.sleep(0.02)
    raise AssertionError(f"Runtime operation did not finish: {operation_id}; latest={latest}")


def test_exec_protocol_rejects_raw_command_strings_and_accepts_argv() -> None:
    with pytest.raises(SandboxContractError) as raw:
        GuestExecRequest.from_payload({"command": "python -m pytest -q", "client_request_id": "req-1"})
    assert raw.value.code == RAW_COMMAND_REJECTED

    with pytest.raises(SandboxContractError) as argv_string:
        GuestExecRequest.from_payload({"argv": "python -m pytest -q", "client_request_id": "req-2"})
    assert argv_string.value.code == INVALID_EXEC_REQUEST

    with pytest.raises(SandboxContractError):
        GuestExecRequest.from_payload({"argv": ["python"], "cwd": "../outside", "client_request_id": "req-3"})

    request = GuestExecRequest.from_payload(
        {
            "argv": ["python", "-m", "pytest", "-q"],
            "cwd": "tests",
            "env": {"PYTHONUNBUFFERED": "1"},
            "timeout_ms": 120_000,
            "stdin": None,
            "client_request_id": "req-4",
        }
    )

    assert request.argv == ("python", "-m", "pytest", "-q")
    assert request.to_agent_payload()["argv"] == ["python", "-m", "pytest", "-q"]
    assert "command" not in request.to_agent_payload()


def test_fake_guest_agent_keeps_exec_as_argv_only() -> None:
    agent = FakeGuestAgent()

    result = agent.exec(
        "sandbox-1",
        {"argv": ["echo", "hello"], "cwd": ".", "env": {}, "timeout_ms": 1000, "client_request_id": "exec-1"},
    )

    assert result["ok"] is True
    assert result["argv"] == ["echo", "hello"]
    assert agent.exec_requests[0].argv == ("echo", "hello")

    with pytest.raises(SandboxContractError) as raw:
        agent.exec("sandbox-1", {"command": "echo hello", "client_request_id": "exec-2"})
    assert raw.value.code == RAW_COMMAND_REJECTED


def test_provider_registry_uses_status_and_required_capabilities() -> None:
    exec_provider = FakeRuntimeProvider(provider_id="fake-exec", capabilities={"sandbox.exec"})
    desktop_provider = FakeRuntimeProvider(
        provider_id="fake-desktop",
        capabilities={"sandbox.exec", "sandbox.desktop", "sandbox.desktop_input"},
    )
    registry = ProviderRegistry()
    registry.register(exec_provider)
    registry.register(desktop_provider)

    resolved = registry.resolve(
        "auto",
        RuntimeRequirements(required_capabilities=frozenset({"sandbox.desktop_input"})),
    )

    assert resolved.provider_id == "fake-desktop"
    status = registry.doctor(
        "fake-exec",
        RuntimeRequirements(required_capabilities=frozenset({"sandbox.desktop_input"})),
    )
    assert status.ready is False
    assert status.missing_requirements == ("sandbox.desktop_input",)


def test_control_lease_conflict_expiry_and_token_hash_storage() -> None:
    clock = Clock()
    manager = ControlLeaseManager(ttl_seconds=30, time_fn=clock, token_factory=lambda: "secret-token")

    grant = manager.acquire("seat-1", "human-1")
    assert grant.token == "secret-token"

    snapshot = manager.debug_snapshot()
    assert "token_hash" in snapshot["seat-1"]
    assert "token" not in snapshot["seat-1"]
    assert "secret-token" not in str(snapshot)

    with pytest.raises(SandboxContractError) as conflict:
        manager.acquire("seat-1", "human-2")
    assert conflict.value.code == DESKTOP_CONTROL_CONFLICT

    with pytest.raises(SandboxContractError) as ai_conflict:
        manager.validate_ai_input("seat-1")
    assert ai_conflict.value.code == DESKTOP_CONTROL_CONFLICT

    with pytest.raises(SandboxContractError) as missing:
        manager.validate_human_input("seat-1", None)
    assert missing.value.code == DESKTOP_LEASE_REQUIRED

    assert manager.validate_human_input("seat-1", grant.token).owner_id == "human-1"
    clock.advance(10)
    renewed = manager.renew("seat-1", "human-1", grant.token)
    assert renewed.expires_at == clock.now + 30

    clock.advance(31)
    with pytest.raises(SandboxContractError) as expired:
        manager.validate_human_input("seat-1", grant.token)
    assert expired.value.code == DESKTOP_LEASE_EXPIRED

    next_grant = manager.acquire("seat-1", "human-2")
    assert next_grant.owner_id == "human-2"


def test_control_lease_acquire_is_atomic_for_parallel_requests() -> None:
    manager = ControlLeaseManager(ttl_seconds=30)
    barrier = threading.Barrier(8)

    def acquire(index: int):
        barrier.wait(timeout=5)
        try:
            return ("ok", manager.acquire("seat-1", f"human-{index}").owner_id)
        except SandboxContractError as exc:
            return ("error", exc.code)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(acquire, range(8)))

    assert sum(1 for status, _ in results if status == "ok") == 1
    assert sum(1 for status, code in results if status == "error" and code == DESKTOP_CONTROL_CONFLICT) == 7


def test_desktop_input_requires_valid_lease_and_redacts_typed_text_from_audit() -> None:
    clock = Clock()
    lease_manager = ControlLeaseManager(ttl_seconds=30, time_fn=clock, token_factory=lambda: "lease-token")
    agent = FakeGuestAgent(lease_manager=lease_manager, width=800, height=600)
    grant = lease_manager.acquire("seat-1", "human-1")

    with pytest.raises(SandboxContractError) as missing:
        DesktopInputRequest.from_payload({"action": "click", "client_action_id": "act-1", "x": 1, "y": 2})
    assert missing.value.code == DESKTOP_LEASE_REQUIRED

    with pytest.raises(SandboxContractError) as ai_conflict:
        agent.desktop_input("sandbox-1", "seat-1", {"action": "key", "client_action_id": "ai-1", "key": "Enter"}, actor="ai")
    assert ai_conflict.value.code == DESKTOP_CONTROL_CONFLICT

    result = agent.desktop_input(
        "sandbox-1",
        "seat-1",
        {
            "action": "type_text",
            "client_action_id": "act-2",
            "lease_token": grant.token,
            "text": "do not audit this",
        },
    )

    assert result["ok"] is True
    assert agent.desktop_inputs[0].text == "do not audit this"
    assert "text" not in agent.audit_events[0]
    assert "lease_token" not in agent.audit_events[0]


def test_frame_cache_after_seq_returns_not_modified_without_advancing_frame() -> None:
    clock = Clock()
    cache = FrameCache(time_fn=clock)

    first = cache.put_frame("seat-1", b"frame-one", content_type="image/png", width=2, height=2)
    not_modified = cache.get_frame("seat-1", after_seq=first.frame_seq)

    assert not_modified.status_code == 204
    assert not_modified.not_modified is True
    assert not_modified.frame is None
    assert cache.last_metadata("seat-1")["frame_seq"] == first.frame_seq

    again = cache.get_frame("seat-1", after_seq=first.frame_seq)
    assert again.status_code == 204
    assert cache.last_metadata("seat-1")["frame_seq"] == first.frame_seq

    clock.advance(1)
    second = cache.put_frame("seat-1", b"frame-two", content_type="image/png", width=2, height=2)
    fetched = cache.get_frame("seat-1", after_seq=first.frame_seq)

    assert second.frame_seq == first.frame_seq + 1
    assert fetched.status_code == 200
    assert fetched.frame == second


def test_frame_cache_sequence_stays_monotonic_after_discard() -> None:
    cache = FrameCache(min_capture_interval_seconds=0)

    first = cache.put_frame("seat-1", b"frame-one", content_type="image/png", width=2, height=2)
    cache.discard("seat-1")
    second = cache.put_frame("seat-1", b"frame-two", content_type="image/png", width=2, height=2)
    fetched = cache.get_frame("seat-1", after_seq=first.frame_seq)

    assert second.frame_seq == first.frame_seq + 1
    assert fetched.status_code == 200
    assert fetched.frame == second


def test_frame_cache_reserve_capture_is_single_flight_per_seat() -> None:
    cache = FrameCache(min_capture_interval_seconds=0)
    barrier = threading.Barrier(8)

    def reserve(_index: int) -> bool:
        barrier.wait(timeout=5)
        return cache.reserve_capture("seat-1") is not None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(reserve, range(8)))

    assert results.count(True) == 1
    assert results.count(False) == 7

    cache.put_frame("seat-1", b"frame-one", content_type="image/png", width=2, height=2)
    assert cache.reserve_capture("seat-1") is not None


def test_fake_provider_create_lifecycle_is_local_only_contract_state() -> None:
    provider = FakeRuntimeProvider(provider_id="fake-runtime", capabilities={"sandbox.exec", "sandbox.desktop"})
    template = _template()

    instance = provider.create(
        _create_spec(template),
    )
    started = provider.start(instance)
    provider.stop(started)
    reconciled = provider.reconcile(started)
    provider.destroy(started)

    assert instance.provider_id == "fake-runtime"
    assert started.state == "ready"
    assert reconciled.instance.state == "stopped"
    assert started.provider_instance_id not in provider.instances


def test_expired_lifecycle_is_enforced_before_sandbox_operations(tmp_path) -> None:
    agent = FakeGuestAgent()
    provider = FakeRuntimeProvider(
        provider_id="fake-runtime",
        capabilities={
            "sandbox.exec",
            "sandbox.files",
            "sandbox.resource_limits",
            "sandbox.network_policy",
            "sandbox.desktop",
            "sandbox.desktop_input",
            "sandbox.snapshot",
        },
        guest_agent=agent,
        sandbox_id_factory=lambda: "ttl-seat",
    )
    registry = ProviderRegistry()
    registry.register(provider)
    manager = SandboxManager(state_dir=tmp_path, provider_registry=registry)

    created = manager.create(
        display=True,
        provider_id="fake-runtime",
        template_id="desktop.ubuntu",
        access_owner_id="local-user",
    )
    assert created["ok"] is True
    with manager._lock:
        inst = manager._instances["ttl-seat"]
        inst.lifecycle_policy = LifecyclePolicy(ttl_seconds=1, destroy_on_exit=True)
        inst.last_activity_at = time.time() - 5
        manager._save_registry()

    result = manager.exec(
        "ttl-seat",
        {
            "argv": ["echo", "hello"],
            "cwd": ".",
            "env": {},
            "timeout_ms": 1000,
            "client_request_id": "ttl-exec",
        },
    )
    status = manager.status("ttl-seat")

    assert result["ok"] is False
    assert result["code"] == "SANDBOX_NOT_RUNNING"
    assert result["state"] == "destroyed"
    assert status["state"] == "destroyed"
    assert agent.exec_requests == []
    assert provider.instances == {}


def test_expired_lifecycle_blocks_desktop_control_lease(tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    provider = FakeRuntimeProvider(
        provider_id="fake-runtime",
        capabilities={
            "sandbox.exec",
            "sandbox.files",
            "sandbox.resource_limits",
            "sandbox.network_policy",
            "sandbox.desktop",
            "sandbox.desktop_input",
            "sandbox.snapshot",
        },
        sandbox_id_factory=lambda: "ttl-control",
    )
    registry = ProviderRegistry()
    registry.register(provider)
    lease_manager = ControlLeaseManager(token_factory=lambda: "lease-token")
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=lease_manager,
    )
    api._reset_service_for_tests(service)
    try:
        created = api.run(
            {
                "_handler": "desktops_create",
                "template_id": "desktop.ubuntu",
                "provider_id": "fake-runtime",
                "owner_id": "local-user",
            },
            {"user_id": "local-user"},
        )
        assert created["status"] == "ok"
        with service.manager._lock:
            inst = service.manager._instances["ttl-control"]
            inst.lifecycle_policy = LifecyclePolicy(ttl_seconds=1, destroy_on_exit=True)
            inst.last_activity_at = time.time() - 5
            service.manager._save_registry()

        acquire = api.run(
            {"_handler": "desktop_control_acquire", "seat_id": "ttl-control", "owner_id": "local-user"},
            {"user_id": "local-user"},
        )
        status = service.manager.status("ttl-control")
    finally:
        api._reset_service_for_tests(None)

    assert acquire["status"] == "error"
    assert acquire["error"]["code"] == "DESKTOP_NOT_RUNNING"
    assert status["state"] == "destroyed"
    assert lease_manager.active_lease("ttl-control") is None


def test_lifecycle_sweeper_enforces_expired_desktop_without_api_polling(tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api
    from ecosystem.defaultspack.backend.sandbox.lifecycle_sweeper import LifecycleSweeper

    provider = FakeRuntimeProvider(
        provider_id="fake-runtime",
        capabilities={
            "sandbox.exec",
            "sandbox.files",
            "sandbox.resource_limits",
            "sandbox.network_policy",
            "sandbox.desktop",
            "sandbox.desktop_input",
            "sandbox.snapshot",
        },
        sandbox_id_factory=lambda: "ttl-sweep",
    )
    registry = ProviderRegistry()
    registry.register(provider)
    lease_manager = ControlLeaseManager(token_factory=lambda: "lease-token")
    frame_cache = FrameCache(min_capture_interval_seconds=0)
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        frame_cache=frame_cache,
        lease_manager=lease_manager,
    )
    created = service.manager.create(
        display=True,
        provider_id="fake-runtime",
        template_id="desktop.ubuntu",
        access_owner_id="local-user",
    )
    assert created["ok"] is True
    frame_cache.put_frame("ttl-sweep", b"frame", content_type="image/png", width=2, height=2)
    lease_manager.acquire("ttl-sweep", "local-user")
    with service.manager._lock:
        inst = service.manager._instances["ttl-sweep"]
        inst.lifecycle_policy = LifecyclePolicy(ttl_seconds=1, destroy_on_exit=True)
        inst.last_activity_at = time.time() - 5
        service.manager._save_registry()

    sweeper = LifecycleSweeper(lambda: api._sweep_lifecycle(service), interval_seconds=0.05)
    sweeper.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with service.manager._lock:
                state = service.manager._instances["ttl-sweep"].state
            if state == "destroyed":
                break
            time.sleep(0.02)
        else:
            raise AssertionError("Lifecycle sweeper did not destroy the expired desktop")
    finally:
        sweeper.stop()

    with service.manager._lock:
        final_state = service.manager._instances["ttl-sweep"].state
    assert final_state == "destroyed"
    assert provider.instances == {}
    assert frame_cache.last_metadata("ttl-sweep") is None
    assert lease_manager.active_lease("ttl-sweep") is None


def test_exec_timeout_cannot_exceed_template_resource_limit(tmp_path) -> None:
    agent = FakeGuestAgent()
    registry = ProviderRegistry()
    registry.register(
        FakeRuntimeProvider(
            provider_id="fake-runtime",
            capabilities={
                "sandbox.exec",
                "sandbox.files",
                "sandbox.overlay_workspace",
                "sandbox.resource_limits",
                "sandbox.network_policy",
            },
            guest_agent=agent,
            sandbox_id_factory=lambda: "resource-seat",
        )
    )
    manager = SandboxManager(state_dir=tmp_path, provider_registry=registry)

    created = manager.create(
        display=False,
        provider_id="fake-runtime",
        template_id="tool.ephemeral",
    )
    assert created["ok"] is True

    over_limit = manager.exec(
        "resource-seat",
        {
            "argv": ["python", "-V"],
            "cwd": ".",
            "env": {},
            "timeout_ms": 300_001,
            "client_request_id": "timeout-over",
        },
    )
    within_limit = manager.exec(
        "resource-seat",
        {
            "argv": ["python", "-V"],
            "cwd": ".",
            "env": {},
            "timeout_ms": 300_000,
            "client_request_id": "timeout-ok",
        },
    )

    assert over_limit["ok"] is False
    assert over_limit["code"] == "SANDBOX_RESOURCE_LIMIT_EXCEEDED"
    assert over_limit["max_timeout_ms"] == 300_000
    assert within_limit["ok"] is True
    assert [request.client_request_id for request in agent.exec_requests] == ["timeout-ok"]


def test_exec_output_is_bounded_by_template_resource_limit(tmp_path) -> None:
    class LoudGuestAgent(FakeGuestAgent):
        def exec(self, sandbox_id: str, payload):
            request = GuestExecRequest.from_payload(payload)
            self.exec_requests.append(request)
            return {
                "ok": True,
                "sandbox_id": sandbox_id,
                "argv": list(request.argv),
                "cwd": request.cwd,
                "exit_code": 0,
                "stdout": "abcdefghij",
                "stderr": "uvwxyz",
            }

    agent = LoudGuestAgent()
    registry = ProviderRegistry()
    registry.register(
        FakeRuntimeProvider(
            provider_id="fake-runtime",
            capabilities={
                "sandbox.exec",
                "sandbox.files",
                "sandbox.overlay_workspace",
                "sandbox.resource_limits",
                "sandbox.network_policy",
            },
            guest_agent=agent,
            sandbox_id_factory=lambda: "output-seat",
        )
    )
    manager = SandboxManager(state_dir=tmp_path, provider_registry=registry)
    created = manager.create(
        display=False,
        provider_id="fake-runtime",
        template_id="tool.ephemeral",
    )
    assert created["ok"] is True
    with manager._lock:
        inst = manager._instances["output-seat"]
        inst.resource_limits = replace(inst.resource_limits, output_bytes=4)
        manager._save_registry()

    result = manager.exec(
        "output-seat",
        {
            "argv": ["python", "-V"],
            "cwd": ".",
            "env": {},
            "timeout_ms": 1000,
            "client_request_id": "output-limit",
        },
    )

    assert result["ok"] is True
    assert result["stdout"] == "abcd"
    assert result["stderr"] == "uvwx"
    assert result["stdout_truncated"] is True
    assert result["stderr_truncated"] is True
    assert [request.client_request_id for request in agent.exec_requests] == ["output-limit"]


def test_manager_validates_and_sanitizes_desktop_input_before_provider(tmp_path) -> None:
    class PermissiveDesktopAgent(FakeGuestAgent):
        def __init__(self) -> None:
            super().__init__(width=800, height=600)
            self.provider_inputs: list[dict[str, object]] = []

        def desktop_input(self, sandbox_id: str, seat_id: str, payload, *, actor: str = "human"):
            self.provider_inputs.append(dict(payload))
            return {"ok": True, "sandbox_id": sandbox_id, "seat_id": seat_id, "action": payload.get("action")}

    agent = PermissiveDesktopAgent()
    registry = ProviderRegistry()
    registry.register(
        FakeRuntimeProvider(
            provider_id="fake-runtime",
            capabilities={
                "sandbox.exec",
                "sandbox.files",
                "sandbox.resource_limits",
                "sandbox.network_policy",
                "sandbox.desktop",
                "sandbox.desktop_input",
                "sandbox.snapshot",
            },
            guest_agent=agent,
            sandbox_id_factory=lambda: "input-seat",
        )
    )
    manager = SandboxManager(state_dir=tmp_path, provider_registry=registry)
    created = manager.create(
        display=True,
        provider_id="fake-runtime",
        template_id="desktop.ubuntu",
        width=800,
        height=600,
        access_owner_id="local-user",
    )
    assert created["ok"] is True

    invalid = manager.desktop_input(
        "input-seat",
        {
            "action": "click",
            "client_action_id": "bad-click",
            "lease_token": "lease-token",
            "x": 801,
            "y": 20,
            "button": "left",
        },
    )
    missing_action = manager.desktop_input(
        "input-seat",
        {
            "client_action_id": "missing-action",
            "lease_token": "lease-token",
            "text": "http://127.0.0.1:8766/chat",
        },
    )
    unsupported_action = manager.desktop_input(
        "input-seat",
        {
            "action": "navigate",
            "client_action_id": "unsupported-action",
            "lease_token": "lease-token",
            "text": "http://127.0.0.1:8766/chat",
        },
    )
    valid = manager.desktop_input(
        "input-seat",
        {
            "action": "click",
            "client_action_id": "good-click",
            "lease_token": "lease-token",
            "x": 10,
            "y": 20,
            "button": "left",
            "ignored_by_policy": "do-not-forward",
        },
    )

    assert invalid["ok"] is False
    assert invalid["code"] == "INVALID_DESKTOP_INPUT"
    assert missing_action["ok"] is False
    assert missing_action["code"] == "INVALID_DESKTOP_INPUT"
    assert "action=type_text" in missing_action["error"]
    assert "key=Enter" in missing_action["error"]
    assert unsupported_action["ok"] is False
    assert unsupported_action["code"] == "INVALID_DESKTOP_INPUT"
    assert "supported actions" in unsupported_action["error"]
    assert "action=type_text" in unsupported_action["error"]
    assert "key=Enter" in unsupported_action["error"]
    assert valid["ok"] is True
    assert agent.provider_inputs == [
        {
            "action": "click",
            "client_action_id": "good-click",
            "lease_token": "lease-token",
            "x": 10,
            "y": 20,
            "button": "left",
        }
    ]


def test_linux_native_provider_desktop_session_capture_and_input(monkeypatch) -> None:
    from ecosystem.defaultspack.backend.sandbox.providers.linux_native import LinuxNativeProvider

    monkeypatch.setattr("ecosystem.defaultspack.backend.sandbox.providers.linux_native.sys.platform", "linux")
    session = FakeX11Session(width=800, height=600)
    provider = LinuxNativeProvider(session_factory=lambda **kwargs: session)
    template = _desktop_only_template()

    assert provider.doctor(RuntimeRequirements(required_capabilities=template.provider_requirements)).ready is True

    instance = provider.create(_create_spec(template))
    started = provider.start(instance)
    agent = provider.connect_agent(started)

    frame = agent.capture_frame(started.sandbox_id, started.sandbox_id)
    action = agent.desktop_input(
        started.sandbox_id,
        started.sandbox_id,
        {"action": "click", "client_action_id": "act-1", "x": 10, "y": 20, "button": "left"},
    )

    assert started.state == "ready"
    assert started.opaque_state["x11_session"]["display"] == ":99"
    assert frame["ok"] is True
    assert frame["data"] == b"png"
    assert frame["width"] == 800
    assert frame["height"] == 600
    assert frame["metadata"]["path_deleted"] is True
    assert session.last_screenshot_path is not None
    assert not session.last_screenshot_path.exists()
    assert action["ok"] is True
    assert session.calls == [("start",), ("screenshot",), ("click", 10, 20, "left")]

    provider.destroy(started)
    assert session.calls[-1] == ("stop",)


def test_linux_native_provider_applies_browser_url_starter_when_network_allows(monkeypatch, tmp_path) -> None:
    import ecosystem.defaultspack.backend.sandbox.providers.linux_native as linux_native
    from ecosystem.defaultspack.backend.sandbox.providers.linux_native import LinuxNativeProvider

    monkeypatch.setattr(linux_native.sys, "platform", "linux")
    monkeypatch.setattr(
        linux_native.shutil,
        "which",
        lambda name: "/usr/bin/google-chrome-stable" if name == "google-chrome-stable" else None,
    )
    session = FakeX11Session(width=800, height=600, session_dir=tmp_path)
    provider = LinuxNativeProvider(session_factory=lambda **kwargs: session)
    template = replace(_desktop_only_template(), network=NetworkPolicy(mode="host_shared"))

    instance = provider.create(
        _create_spec(
            template,
            metadata={"startup": {"starter": "browser_url", "browser_url": "https://example.com/task"}},
        )
    )
    started = provider.start(instance)

    launch_call = next(call for call in session.calls if call[0] == "launch")
    argv = list(launch_call[2])
    assert launch_call[1] == "browser_url"
    assert launch_call[3] == "starter-browser.log"
    assert argv[0] == "/usr/bin/google-chrome-stable"
    assert "--new-window" in argv
    assert any(str(part).startswith("--user-data-dir=") for part in argv)
    assert argv[-1] == "https://example.com/task"
    assert started.opaque_state["startup_status"]["starter"] == "browser_url"
    assert started.opaque_state["startup_status"]["executed"] is True


def test_linux_native_provider_applies_browser_starter_without_url(monkeypatch, tmp_path) -> None:
    import ecosystem.defaultspack.backend.sandbox.providers.linux_native as linux_native
    from ecosystem.defaultspack.backend.sandbox.providers.linux_native import LinuxNativeProvider

    monkeypatch.setattr(linux_native.sys, "platform", "linux")
    monkeypatch.setattr(
        linux_native.shutil,
        "which",
        lambda name: "/usr/bin/google-chrome-stable" if name == "google-chrome-stable" else None,
    )
    session = FakeX11Session(width=800, height=600, session_dir=tmp_path)
    provider = LinuxNativeProvider(session_factory=lambda **kwargs: session)

    instance = provider.create(
        _create_spec(
            _desktop_only_template(),
            metadata={"startup": {"starter": "browser"}},
        )
    )
    started = provider.start(instance)

    launch_call = next(call for call in session.calls if call[0] == "launch")
    argv = list(launch_call[2])
    assert launch_call[1] == "browser"
    assert argv[0] == "/usr/bin/google-chrome-stable"
    assert "--new-window" in argv
    assert all(not str(part).startswith("http") for part in argv)
    assert started.opaque_state["startup_status"]["starter"] == "browser"
    assert started.opaque_state["startup_status"]["executed"] is True


def test_linux_native_provider_skips_browser_url_starter_when_network_is_off(monkeypatch) -> None:
    import ecosystem.defaultspack.backend.sandbox.providers.linux_native as linux_native
    from ecosystem.defaultspack.backend.sandbox.providers.linux_native import LinuxNativeProvider

    monkeypatch.setattr(linux_native.sys, "platform", "linux")
    monkeypatch.setattr(
        linux_native.shutil,
        "which",
        lambda name: "/usr/bin/google-chrome-stable" if name == "google-chrome-stable" else None,
    )
    session = FakeX11Session(width=800, height=600)
    provider = LinuxNativeProvider(session_factory=lambda **kwargs: session)

    instance = provider.create(
        _create_spec(
            _desktop_only_template(),
            metadata={"startup": {"starter": "browser_url", "browser_url": "https://example.com/task"}},
        )
    )
    started = provider.start(instance)

    assert session.calls == [("start",)]
    assert started.opaque_state["startup_status"]["skipped"] is True
    assert "Network policy" in started.opaque_state["startup_status"]["reason"]


def test_linux_native_api_default_desktop_template_is_compatible(monkeypatch, tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api
    from ecosystem.defaultspack.backend.sandbox.providers.linux_native import LinuxNativeProvider

    monkeypatch.setattr("ecosystem.defaultspack.blocks.sandbox.api.platform.system", lambda: "Linux")
    monkeypatch.setattr("ecosystem.defaultspack.backend.sandbox.sandbox_manager.platform.system", lambda: "Linux")
    monkeypatch.setattr("ecosystem.defaultspack.backend.sandbox.providers.linux_native.sys.platform", "linux")
    session = FakeX11Session(width=800, height=600)
    registry = ProviderRegistry()
    registry.register(LinuxNativeProvider(session_factory=lambda **kwargs: session))
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=ControlLeaseManager(),
    )
    api._reset_service_for_tests(service)
    try:
        providers = api.run({"_handler": "runtime_providers"}, {})
        created = api.run(
            {
                "_handler": "desktops_create",
                "provider_id": "linux_native",
                "owner_id": "local-user",
                "resolution": {"width": 800, "height": 600},
            },
            {"user_id": "local-user"},
        )
    finally:
        api._reset_service_for_tests(None)

    assert providers["data"]["providers"][0]["ready"] is True
    assert created["status"] == "ok"
    assert created["data"]["template_id"] == "desktop.linux_native"
    assert created["data"]["status"] == "running"


def test_api_rejects_template_kind_mismatches(tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    registry = ProviderRegistry()
    registry.register(
        FakeRuntimeProvider(
            provider_id="fake-runtime",
            capabilities={
                "sandbox.exec",
                "sandbox.files",
                "sandbox.resource_limits",
                "sandbox.network_policy",
                "sandbox.desktop",
                "sandbox.desktop_input",
                "sandbox.snapshot",
            },
        )
    )
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=ControlLeaseManager(),
    )
    api._reset_service_for_tests(service)
    try:
        desktop_via_sandbox = api.run(
            {
                "_handler": "sandboxes_create",
                "template_id": "desktop.ubuntu",
                "provider_id": "fake-runtime",
            },
            {},
        )
        sandbox_via_desktop = api.run(
            {
                "_handler": "desktops_create",
                "template_id": "tool.ephemeral",
                "provider_id": "fake-runtime",
                "owner_id": "local-user",
            },
            {},
        )
        sandbox_created = api.run(
            {
                "_handler": "sandboxes_create",
                "template_id": "tool.ephemeral",
                "provider_id": "fake-runtime",
            },
            {},
        )
        desktops = api.run({"_handler": "desktops_list"}, {})
    finally:
        api._reset_service_for_tests(None)

    assert desktop_via_sandbox["status"] == "error"
    assert desktop_via_sandbox["error"]["code"] == "SANDBOX_TEMPLATE_KIND_MISMATCH"
    assert sandbox_via_desktop["status"] == "error"
    assert sandbox_via_desktop["error"]["code"] == "SANDBOX_TEMPLATE_NOT_DESKTOP"
    assert sandbox_created["status"] == "ok"
    assert sandbox_created["data"]["template_id"] == "tool.ephemeral"
    assert desktops["data"]["desktops"] == []


def test_desktop_list_survives_invalid_desktop_payload(monkeypatch) -> None:
    from types import SimpleNamespace

    from ecosystem.defaultspack.blocks.sandbox import api

    service = SimpleNamespace(
        manager=SimpleNamespace(
            list_instances=lambda: [
                None,
                {
                    "display": True,
                    "id": None,
                    "sandbox_id": "seat-broken",
                    "name": "Broken desktop",
                    "provider_id": "windows_wsl",
                    "template_id": "desktop.browser",
                },
            ]
        ),
        frame_cache=SimpleNamespace(last_metadata=lambda _seat_id: None),
        lease_manager=SimpleNamespace(active_lease=lambda _seat_id: None),
    )

    def broken_payload(_service, _item):
        raise AttributeError("'NoneType' object has no attribute '__dict__'")

    monkeypatch.setattr(api, "_desktop_payload", broken_payload)

    desktops = api._desktop_list(service)

    assert len(desktops) == 1
    assert desktops[0]["id"] == "seat-broken"
    assert desktops[0]["seat_id"] == "seat-broken"
    assert desktops[0]["sandbox_id"] == "seat-broken"
    assert desktops[0]["status"] == "failed"
    assert desktops[0]["last_error"] == "Desktop state could not be serialized."


def test_desktop_list_prioritizes_running_desktops() -> None:
    from types import SimpleNamespace

    from ecosystem.defaultspack.blocks.sandbox import api

    service = SimpleNamespace(
        manager=SimpleNamespace(
            list_instances=lambda: [
                {
                    "display": True,
                    "sandbox_id": "old-destroyed-seat",
                    "name": "Old destroyed desktop",
                    "state": "destroyed",
                    "provider_id": "windows_wsl",
                    "template_id": "desktop.browser",
                    "updated_at": 20,
                },
                {
                    "display": True,
                    "id": None,
                    "sandbox_id": "current-running-seat",
                    "name": "Current QA worker",
                    "state": "ready",
                    "provider_id": "windows_wsl",
                    "template_id": "desktop.browser",
                    "updated_at": 10,
                },
                {
                    "display": True,
                    "id": None,
                    "seat_id": "newer-stopped-seat",
                    "name": "Newer stopped desktop",
                    "state": "stopped",
                    "provider_id": "windows_wsl",
                    "template_id": "desktop.browser",
                    "updated_at": 30,
                },
            ]
        ),
        frame_cache=SimpleNamespace(last_metadata=lambda _seat_id: None),
        lease_manager=SimpleNamespace(active_lease=lambda _seat_id: None),
    )

    desktops = api._desktop_list(service)

    assert [desktop["seat_id"] for desktop in desktops] == [
        "current-running-seat",
        "newer-stopped-seat",
        "old-destroyed-seat",
    ]
    assert desktops[0]["status"] == "running"
    for desktop in desktops:
        assert desktop["id"] == desktop["seat_id"]
        assert desktop["id"] == desktop["sandbox_id"]


def test_desktops_list_skips_malformed_manager_instances() -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    api._reset_service_for_tests(None)
    service = api._SandboxApiService(start_lifecycle_sweeper=False)
    service.manager._instances.clear()
    service.manager._instances["bad"] = None
    api._reset_service_for_tests(service)
    try:
        result = api.run({"_handler": "desktops_list"}, {})
    finally:
        api._reset_service_for_tests(None)

    assert result["status"] == "ok"
    assert result["data"]["desktops"] == []


def test_desktop_create_rejects_guest_provisioning_for_desktop_only_provider(tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    registry = ProviderRegistry()
    registry.register(
        FakeRuntimeProvider(
            provider_id="linux_native",
            capabilities={"sandbox.desktop", "sandbox.desktop_input", "sandbox.snapshot"},
            sandbox_id_factory=lambda: "native-seat",
        )
    )
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=ControlLeaseManager(),
    )
    api._reset_service_for_tests(service)
    try:
        created = api.run(
            {
                "_handler": "desktops_create",
                "template_id": "desktop.linux_native",
                "provider_id": "linux_native",
                "owner_id": "local-user",
                "provisioning": {
                    "apps": ["google-chrome-stable"],
                    "mcp_servers": ["playwright"],
                },
            },
            {"user_id": "local-user"},
        )
    finally:
        api._reset_service_for_tests(None)

    assert created["status"] == "error"
    assert created["error"]["code"] == "DESKTOP_PROVISIONING_UNSUPPORTED"
    assert service.manager.list_instances() == []


def test_desktop_create_browser_url_defaults_to_browser_template_and_context_network_approval(tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    provider = FakeRuntimeProvider(
        provider_id="fake-runtime",
        capabilities={
            "sandbox.exec",
            "sandbox.files",
            "sandbox.resource_limits",
            "sandbox.desktop",
            "sandbox.desktop_input",
            "sandbox.snapshot",
            "sandbox.network_policy",
        },
        sandbox_id_factory=lambda: "browser-seat",
    )
    registry = ProviderRegistry()
    registry.register(provider)
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=ControlLeaseManager(),
    )
    api._reset_service_for_tests(service)
    try:
        created = api.run(
            {
                "_handler": "desktops_create",
                "provider_id": "fake-runtime",
                "owner_id": "local-user",
                "starter": "browser_url",
                "browser_url": "http://127.0.0.1:8766/chat",
            },
            {"user_id": "local-user", "_tool_server_approved": True},
        )
    finally:
        api._reset_service_for_tests(None)

    assert created["status"] == "ok"
    assert created["data"]["template_id"] == "desktop.browser"
    assert provider.create_specs[0].template.template_id == "desktop.browser"
    assert provider.create_specs[0].metadata["startup"] == {
        "starter": "browser_url",
        "browser_url": "http://127.0.0.1:8766/chat",
    }
    assert provider.create_specs[0].metadata["network_approved"] is True
    assert created["data"]["startup"] == {
        "starter": "browser_url",
        "browser_url": "http://127.0.0.1:8766/chat",
    }
    assert created["data"]["metadata"]["startup"] == created["data"]["startup"]
    assert created["data"]["desktop_spec"]["enabled"] is True


def test_sandbox_port_api_uses_context_approval_not_payload_flags(tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    class PortGuest(FakeGuestAgent):
        def __init__(self) -> None:
            super().__init__()
            self.port_requests = []

        def expose_port(self, sandbox_id, payload):
            self.port_requests.append((sandbox_id, dict(payload)))
            return {"ok": True, "sandbox_id": sandbox_id, "port": int(payload["port"]), "url": "http://127.0.0.1:3000"}

    guest = PortGuest()
    registry = ProviderRegistry()
    registry.register(
        FakeRuntimeProvider(
            provider_id="fake-runtime",
            capabilities={
                "sandbox.exec",
                "sandbox.files",
                "sandbox.overlay_workspace",
                "sandbox.port_forward",
                "sandbox.resource_limits",
                "sandbox.network_policy",
            },
            guest_agent=guest,
        )
    )
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=ControlLeaseManager(),
    )
    api._reset_service_for_tests(service)
    try:
        created = api.run(
            {
                "_handler": "sandboxes_create",
                "template_id": "coding.python",
                "provider_id": "fake-runtime",
            },
            {},
        )
        forged = api.run(
            {
                "_handler": "sandbox_port_expose",
                "sandbox_id": created["data"]["sandbox_id"],
                "port": 3000,
                "_network_policy_approved": True,
            },
            {},
        )
        approved = api.run(
            {
                "_handler": "sandbox_port_expose",
                "sandbox_id": created["data"]["sandbox_id"],
                "port": 3000,
            },
            mark_tool_server_approval_context({}),
        )
    finally:
        api._reset_service_for_tests(None)

    assert forged["status"] == "error"
    assert forged["error"]["code"] == "SANDBOX_NETWORK_REQUIRES_APPROVAL"
    assert approved["status"] == "ok"
    assert guest.port_requests == [
        (
            created["data"]["sandbox_id"],
            {
                "_handler": "sandbox_port_expose",
                "sandbox_id": created["data"]["sandbox_id"],
                "port": 3000,
                "_network_policy_approved": True,
            },
        )
    ]


def test_sandbox_exec_secret_grants_are_server_context_only(tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    guest = FakeGuestAgent()
    registry = ProviderRegistry()
    registry.register(
        FakeRuntimeProvider(
            provider_id="fake-runtime",
            capabilities={
                "sandbox.exec",
                "sandbox.files",
                "sandbox.overlay_workspace",
                "sandbox.port_forward",
                "sandbox.resource_limits",
                "sandbox.network_policy",
            },
            guest_agent=guest,
        )
    )
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=ControlLeaseManager(),
    )
    api._reset_service_for_tests(service)
    try:
        created = api.run(
            {
                "_handler": "sandboxes_create",
                "template_id": "coding.python",
                "provider_id": "fake-runtime",
            },
            {},
        )
        forged_payload = api.run(
            {
                "_handler": "sandbox_exec",
                "sandbox_id": created["data"]["sandbox_id"],
                "argv": ["python", "--version"],
                "cwd": ".",
                "env": {"GITHUB_TOKEN": "ghp-test"},
                "timeout_ms": 60000,
                "client_request_id": "secret-forged",
                "approved": True,
                "approved_secret_ids": ["GITHUB_TOKEN"],
            },
            {"_sandbox_secret_grants": ["GITHUB_TOKEN"]},
        )
        sealed_grant = api.run(
            {
                "_handler": "sandbox_exec",
                "sandbox_id": created["data"]["sandbox_id"],
                "argv": ["python", "--version"],
                "cwd": ".",
                "env": {"GITHUB_TOKEN": "ghp-test"},
                "timeout_ms": 60000,
                "client_request_id": "secret-sealed",
            },
            seal_tool_context(
                {},
                {
                    "action": "allow",
                    "allowed": True,
                    "resource": {"secret_ids": ["GITHUB_TOKEN"]},
                },
            ),
        )
        internal_marker_grant = api.run(
            {
                "_handler": "sandbox_exec",
                "sandbox_id": created["data"]["sandbox_id"],
                "argv": ["python", "--version"],
                "cwd": ".",
                "env": {"GITHUB_TOKEN": "ghp-test"},
                "timeout_ms": 60000,
                "client_request_id": "secret-marker",
            },
            mark_tool_server_approval_context({"_sandbox_secret_grants": [{"env_key": "GITHUB_TOKEN"}]}),
        )
    finally:
        api._reset_service_for_tests(None)

    assert forged_payload["status"] == "error"
    assert forged_payload["error"]["code"] == "SANDBOX_SECRET_ACCESS_REQUIRES_APPROVAL"
    assert sealed_grant["status"] == "ok"
    assert internal_marker_grant["status"] == "ok"
    assert [request.env for request in guest.exec_requests] == [
        {"GITHUB_TOKEN": "ghp-test"},
        {"GITHUB_TOKEN": "ghp-test"},
    ]


def test_desktop_api_create_frame_lease_and_input_happy_path(monkeypatch, tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    lease_manager = ControlLeaseManager(ttl_seconds=30, token_factory=lambda: "lease-token")
    agent = CaptureGuestAgent(lease_manager=lease_manager, width=800, height=600)
    registry = ProviderRegistry()
    registry.register(
        FakeRuntimeProvider(
            provider_id="fake-runtime",
            capabilities={
                "sandbox.exec",
                "sandbox.files",
                "sandbox.resource_limits",
                "sandbox.network_policy",
                "sandbox.desktop",
                "sandbox.desktop_input",
                "sandbox.snapshot",
            },
            guest_agent=agent,
            sandbox_id_factory=lambda: "seat-1",
        )
    )
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=lease_manager,
    )
    api._reset_service_for_tests(service)
    try:
        created = api.run(
            {
                "_handler": "desktops_create",
                "name": "CI Ubuntu",
                "template_id": "desktop.ubuntu",
                "provider_id": "fake-runtime",
                "resolution": {"width": 800, "height": 600},
            },
            {"user_id": "owner-1"},
        )
        spoofed_get = api.run(
            {"_handler": "desktop_get", "seat_id": "seat-1", "owner_id": "owner-1"},
            {"user_id": "attacker-1"},
        )
        owner_get = api.run({"_handler": "desktop_get", "seat_id": "seat-1"}, {"user_id": "owner-1"})
        frame = api.run({"_handler": "desktop_frame", "seat_id": "seat-1"}, {"user_id": "owner-1"})
        lease = api.run({"_handler": "desktop_control_acquire", "seat_id": "seat-1"}, {"user_id": "owner-1"})
        click = api.run(
            {
                "_handler": "desktop_input",
                "seat_id": "seat-1",
                "action": "click",
                "client_action_id": "act-1",
                "lease_token": "lease-token",
                "x": 10,
                "y": 20,
                "button": "left",
            },
            {"user_id": "owner-1"},
        )
        stop_without_confirmation = api.run({"_handler": "desktop_stop", "seat_id": "seat-1"}, {"user_id": "owner-1"})
        stop = api.run(
            {
                "_handler": "desktop_stop",
                "seat_id": "seat-1",
                "confirm_destructive": True,
            },
            {"user_id": "owner-1"},
        )
        start = api.run({"_handler": "desktop_start", "seat_id": "seat-1"}, {"user_id": "owner-1"})
        restart = api.run({"_handler": "desktop_restart", "seat_id": "seat-1"}, {"user_id": "owner-1"})
        delete_without_confirmation = api.run({"_handler": "desktop_delete", "seat_id": "seat-1"}, {"user_id": "owner-1"})
        delete = api.run(
            {
                "_handler": "desktop_delete",
                "seat_id": "seat-1",
                "confirm_destructive": True,
            },
            {"user_id": "owner-1"},
        )
    finally:
        api._reset_service_for_tests(None)

    assert created["status"] == "ok"
    assert created["data"]["seat_id"] == "seat-1"
    assert created["data"]["status"] == "running"
    assert created["data"]["network_policy"]["default"] == "limited_or_approval_gated"
    assert spoofed_get["status"] == "error"
    assert spoofed_get["error"]["code"] == "DESKTOP_OWNER_REQUIRED"
    assert owner_get["status"] == "ok"
    assert frame["_binary"] is True
    assert frame["body"] == b"fake-png"
    assert frame["headers"]["X-Rumi-Frame-Width"] == "800"
    assert lease["data"]["lease_token"] == "lease-token"
    assert click["status"] == "ok"
    assert click["data"]["accepted"] is True
    assert stop_without_confirmation["status"] == "error"
    assert stop_without_confirmation["error"]["code"] == "DESTRUCTIVE_ACTION_CONFIRMATION_REQUIRED"
    assert stop["data"]["status"] == "stopped"
    assert start["data"]["status"] == "running"
    assert restart["data"]["status"] == "running"
    assert delete_without_confirmation["status"] == "error"
    assert delete_without_confirmation["error"]["code"] == "DESTRUCTIVE_ACTION_CONFIRMATION_REQUIRED"
    assert delete["status"] == "ok"
    assert delete["data"] == {"deleted": True, "seat_id": "seat-1"}
    assert service.frame_cache.last_metadata("seat-1") is None
    assert service.lease_manager.active_lease("seat-1") is None
    assert agent.desktop_inputs[0].action == "click"


def test_desktop_api_sees_desktop_created_by_external_tool_manager(tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    capabilities = {
        "sandbox.exec",
        "sandbox.files",
        "sandbox.resource_limits",
        "sandbox.network_policy",
        "sandbox.desktop",
        "sandbox.desktop_input",
        "sandbox.snapshot",
    }
    http_registry = ProviderRegistry()
    http_agent = FakeGuestAgent()
    http_registry.register(
        FakeRuntimeProvider(
            provider_id="fake-runtime",
            capabilities=capabilities,
            guest_agent=http_agent,
            sandbox_id_factory=lambda: "http-seat",
        )
    )
    service = SimpleNamespace(
        provider_registry=http_registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=http_registry),
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=ControlLeaseManager(),
    )

    tool_registry = ProviderRegistry()
    tool_registry.register(
        FakeRuntimeProvider(
            provider_id="fake-runtime",
            capabilities=capabilities,
            sandbox_id_factory=lambda: "tool-seat",
        )
    )
    tool_manager = SandboxManager(state_dir=tmp_path, provider_registry=tool_registry)
    created = tool_manager.create(
        display=True,
        provider_id="fake-runtime",
        template_id="desktop.ubuntu",
        access_owner_id="local-user",
    )

    api._reset_service_for_tests(service)
    try:
        listed = api.run({"_handler": "desktops_list"}, {})
        fetched = api.run({"_handler": "desktop_get", "seat_id": "tool-seat"}, {"user_id": "local-user"})
        ai_input = api.run(
            {
                "_handler": "desktop_ai_input",
                "seat_id": "tool-seat",
                "action": "click",
                "client_action_id": "ai-click-1",
                "x": 12,
                "y": 34,
                "button": "left",
            },
            {"user_id": "local-user", "agent_id": "agent-1"},
        )
    finally:
        api._reset_service_for_tests(None)

    assert created["ok"] is True
    assert created["sandbox_id"] == "tool-seat"
    assert listed["status"] == "ok"
    assert [desktop["seat_id"] for desktop in listed["data"]["desktops"]] == ["tool-seat"]
    assert fetched["status"] == "ok"
    assert fetched["data"]["seat_id"] == "tool-seat"
    assert fetched["data"]["status"] == "running"
    assert ai_input["status"] == "ok"
    assert ai_input["data"]["accepted"] is True
    assert http_agent.desktop_inputs[0].action == "click"


def test_desktop_api_sees_desktop_created_by_desktop_create_tool(tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    if str(DEFAULTSPACK_ROOT) not in sys.path:
        sys.path.insert(0, str(DEFAULTSPACK_ROOT))
    from domain.tool import desktop_tools
    from domain.tool_policy.internal_context import (
        seal_tool_context as domain_seal_tool_context,
    )

    capabilities = {
        "sandbox.exec",
        "sandbox.files",
        "sandbox.resource_limits",
        "sandbox.network_policy",
        "sandbox.desktop",
        "sandbox.desktop_input",
        "sandbox.snapshot",
    }
    tool_registry = ProviderRegistry()
    tool_registry.register(
        FakeRuntimeProvider(
            provider_id="fake-runtime",
            capabilities=capabilities,
            sandbox_id_factory=lambda: "tool-created-seat",
        )
    )
    tool_service = SimpleNamespace(
        provider_registry=tool_registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=tool_registry),
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=ControlLeaseManager(),
    )

    http_registry = ProviderRegistry()
    http_registry.register(
        FakeRuntimeProvider(provider_id="fake-runtime", capabilities=capabilities)
    )
    http_service = SimpleNamespace(
        provider_registry=http_registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=http_registry),
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=ControlLeaseManager(),
    )

    api._reset_service_for_tests(tool_service)
    try:
        created = desktop_tools.desktop_create(
            {
                "provider_id": "fake-runtime",
                "template_id": "desktop.ubuntu",
                "name": "Issue 416 QA desktop",
            },
            domain_seal_tool_context(
                {"user_id": "local-user"},
                {"action": "allow", "allowed": True},
            ),
        )

        api._reset_service_for_tests(http_service)
        listed = api.run({"_handler": "desktops_list"}, {})
        fetched = api.run(
            {"_handler": "desktop_get", "seat_id": "tool-created-seat"},
            {"user_id": "local-user"},
        )
    finally:
        api._reset_service_for_tests(None)

    assert created["status"] == "ok"
    assert created["data"]["seat_id"] == "tool-created-seat"
    assert listed["status"] == "ok"
    assert [desktop["seat_id"] for desktop in listed["data"]["desktops"]] == ["tool-created-seat"]
    assert fetched["status"] == "ok"
    assert fetched["data"]["seat_id"] == "tool-created-seat"


def test_sandbox_stop_and_delete_require_destructive_confirmation(tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    registry = ProviderRegistry()
    registry.register(
        FakeRuntimeProvider(
            provider_id="fake-runtime",
            capabilities={"sandbox.exec", "sandbox.files", "sandbox.resource_limits", "sandbox.network_policy"},
            sandbox_id_factory=lambda: "sandbox-1",
        )
    )
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=ControlLeaseManager(),
    )
    api._reset_service_for_tests(service)
    try:
        created = api.run(
            {
                "_handler": "sandboxes_create",
                "template_id": "tool.ephemeral",
                "provider_id": "fake-runtime",
            },
            {},
        )
        stop_without_confirmation = api.run({"_handler": "sandbox_stop", "sandbox_id": "sandbox-1"}, {})
        stop = api.run(
            {"_handler": "sandbox_stop", "sandbox_id": "sandbox-1", "confirm_destructive": True},
            {},
        )
        start = api.run({"_handler": "sandbox_start", "sandbox_id": "sandbox-1"}, {})
        delete_without_confirmation = api.run({"_handler": "sandbox_delete", "sandbox_id": "sandbox-1"}, {})
        delete = api.run(
            {"_handler": "sandbox_delete", "sandbox_id": "sandbox-1", "confirm_destructive": True},
            {},
        )
    finally:
        api._reset_service_for_tests(None)

    assert created["status"] == "ok"
    assert created["data"]["sandbox_id"] == "sandbox-1"
    assert stop_without_confirmation["status"] == "error"
    assert stop_without_confirmation["error"]["code"] == "DESTRUCTIVE_ACTION_CONFIRMATION_REQUIRED"
    assert stop_without_confirmation["error"]["details"]["resource"] == "sandbox"
    assert stop["status"] == "ok"
    assert stop["data"]["status"] == "stopped"
    assert start["status"] == "ok"
    assert start["data"]["status"] == "ready"
    assert delete_without_confirmation["status"] == "error"
    assert delete_without_confirmation["error"]["code"] == "DESTRUCTIVE_ACTION_CONFIRMATION_REQUIRED"
    assert delete["status"] == "ok"
    assert delete["data"] == {"deleted": True, "sandbox_id": "sandbox-1"}


def test_desktop_access_key_rules_and_ai_input_contract(tmp_path, monkeypatch) -> None:
    _trusted_workspace(tmp_path, monkeypatch)
    from ecosystem.defaultspack.blocks.sandbox import api

    lease_manager = ControlLeaseManager(ttl_seconds=30, token_factory=lambda: "lease-token")
    agent = CaptureGuestAgent(lease_manager=lease_manager, width=800, height=600)
    registry = ProviderRegistry()
    registry.register(
        FakeRuntimeProvider(
            provider_id="fake-runtime",
            capabilities={
                "sandbox.exec",
                "sandbox.files",
                "sandbox.resource_limits",
                "sandbox.network_policy",
                "sandbox.desktop",
                "sandbox.desktop_input",
                "sandbox.snapshot",
            },
            guest_agent=agent,
            sandbox_id_factory=lambda: "seat-locked",
        )
    )
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=lease_manager,
    )
    api._reset_service_for_tests(service)
    trusted_agent_context = {
        "source": "defaultspack_local_ui",
        "trusted_audience": "https://fake-audience.invalid/desktop",
        "trusted_origin": "https://fake-origin.invalid",
        "authenticated_principal_id": "agent-1",
        "authenticated_device_id": "fake-device-managed-contract",
        "authenticated_session_id": "fake-session-managed-contract",
    }
    scoped_agent_context = {
        key: value for key, value in trusted_agent_context.items() if key != "source"
    }
    scoped_agent_context["principal_id"] = "agent-1"
    try:
        missing_key = api.run(
            {
                "_handler": "desktops_create",
                "template_id": "desktop.ubuntu",
                "provider_id": "fake-runtime",
                "access": {"mode": "key_required"},
            },
            {},
        )
        created = api.run(
            {
                "_handler": "desktops_create",
                "template_id": "desktop.coding",
                "provider_id": "fake-runtime",
                "role": "browser operator",
                "rules": {"rule_ids": ["browser-only"]},
                "access": {"mode": "key_required", "access_key": "correct-key"},
                "workspace_id": "workspace-1",
                "workspace_access": "read_write",
            },
            {},
        )
        safe_workspace = api.run(
            {
                "_handler": "desktops_create",
                "template_id": "desktop.coding",
                "provider_id": "fake-runtime",
                "role": "browser operator",
                "rules": {"rule_ids": ["browser-only"]},
                "owner_id": "local-user",
                "access": {"mode": "owner_only", "owner_id": "local-user"},
                "assigned_agent": "agent-1",
                "workspace_id": "workspace-1",
                "workspace_access": "read_only",
            },
            {"user_id": "local-user"},
        )
        issued = api.run(
            {
                "_handler": "desktop_exchange_issue",
                "seat_id": "seat-locked",
                "operations": [
                    "desktop.read",
                    "desktop.rules.update",
                    "desktop.input",
                    "desktop.control.acquire",
                ],
            },
            trusted_agent_context,
        )
        redeemed = api.run(
            {
                "_handler": "desktop_exchange_redeem",
                "exchange_code": issued["data"]["exchange_code"],
            },
            scoped_agent_context,
        )
        scoped_credential = redeemed["data"]["session_credential"]
        request_required_create = api.run(
            {
                "_handler": "desktops_create",
                "template_id": "desktop.ubuntu",
                "provider_id": "fake-runtime",
                "access": {"mode": "request_required"},
            },
            {},
        )
        denied = api.run({"_handler": "desktop_get", "seat_id": "seat-locked"}, {})
        allowed = api.run(
            {
                "_handler": "desktop_get",
                "seat_id": "seat-locked",
                "desktop_session_credential": scoped_credential,
            },
            scoped_agent_context,
        )
        updated = api.run(
            {
                "_handler": "desktop_rules_update",
                "seat_id": "seat-locked",
                "desktop_session_credential": scoped_credential,
                "role": "coding desktop",
                "rules": ["playwright-ok"],
            },
            scoped_agent_context,
        )
        reissued = api.run(
            {
                "_handler": "desktop_exchange_issue",
                "seat_id": "seat-locked",
                "operations": [
                    "desktop.read",
                    "desktop.rules.update",
                    "desktop.ai_input",
                    "desktop.control.acquire",
                ],
            },
            trusted_agent_context,
        )
        re_redeemed = api.run(
            {
                "_handler": "desktop_exchange_redeem",
                "exchange_code": reissued["data"]["exchange_code"],
            },
            scoped_agent_context,
        )
        scoped_credential = re_redeemed["data"]["session_credential"]
        request_required_update = api.run(
            {
                "_handler": "desktop_rules_update",
                "seat_id": "seat-locked",
                "desktop_session_credential": scoped_credential,
                "access": {"mode": "request_required"},
            },
            scoped_agent_context,
        )
        access_request = api.run(
            {"_handler": "desktop_access_request", "seat_id": "seat-locked"},
            {"user_id": "local-user"},
        )
        wrong_agent_click = api.run(
            {
                "_handler": "desktop_ai_input",
                "seat_id": "seat-locked",
                "desktop_session_credential": scoped_credential,
                "action": "click",
                "client_action_id": "ai-wrong",
                "x": 10,
                "y": 10,
            },
            {**scoped_agent_context, "agent_id": "agent-2"},
        )
        spoofed_body_wrong_context = api.run(
            {
                "_handler": "desktop_ai_input",
                "seat_id": "seat-locked",
                "desktop_session_credential": scoped_credential,
                "action": "click",
                "client_action_id": "ai-spoof-wrong",
                "x": 10,
                "y": 10,
                "agent_id": "agent-1",
            },
            {**scoped_agent_context, "agent_id": "agent-2"},
        )
        spoofed_body_no_context = api.run(
            {
                "_handler": "desktop_ai_input",
                "seat_id": "seat-locked",
                "desktop_session_credential": scoped_credential,
                "action": "click",
                "client_action_id": "ai-spoof-missing",
                "x": 10,
                "y": 10,
                "agent_id": "agent-1",
            },
            scoped_agent_context,
        )
        ai_click = api.run(
            {
                "_handler": "desktop_ai_input",
                "seat_id": "seat-locked",
                "desktop_session_credential": scoped_credential,
                "action": "click",
                "client_action_id": "ai-1",
                "x": 10,
                "y": 10,
            },
            {**scoped_agent_context, "agent_id": "agent-1"},
        )
        lease = api.run(
            {
                "_handler": "desktop_control_acquire",
                "seat_id": "seat-locked",
                "desktop_session_credential": scoped_credential,
            },
            scoped_agent_context,
        )
        ai_conflict = api.run(
            {
                "_handler": "desktop_ai_input",
                "seat_id": "seat-locked",
                "desktop_session_credential": scoped_credential,
                "action": "click",
                "client_action_id": "ai-2",
                "x": 10,
                "y": 10,
            },
            {**scoped_agent_context, "agent_id": "agent-1"},
        )
    finally:
        api._reset_service_for_tests(None)

    assert missing_key["status"] == "error"
    assert missing_key["error"]["code"] == "DESKTOP_ACCESS_KEY_MISSING"
    assert created["status"] == "error"
    assert created["error"]["code"] == "DESKTOP_ACCESS_KEY_MIGRATION_REQUIRED"
    assert safe_workspace["status"] == "ok"
    assert safe_workspace["data"]["access_policy"]["key_required"] is False
    assert safe_workspace["data"]["network_policy"]["default"] == "project_policy_or_first_use_approval"
    assert safe_workspace["data"]["workspace"]["access"] == "read_only"
    assert "correct-key" not in str(safe_workspace)
    assert issued["status"] == "ok"
    assert redeemed["status"] == "ok"
    assert request_required_create["status"] == "error"
    assert request_required_create["error"]["code"] == "DESKTOP_OWNER_REQUIRED"
    assert denied["status"] == "error"
    assert denied["error"]["code"] == "DESKTOP_OWNER_REQUIRED"
    assert allowed["status"] == "ok"
    assert allowed["data"]["rules"]["role"] == "browser operator"
    assert updated["status"] == "ok"
    assert updated["data"]["rules"]["role"] == "coding desktop"
    assert updated["data"]["rules"]["rule_ids"] == ["playwright-ok"]
    assert request_required_update["status"] == "error"
    assert request_required_update["error"]["code"] == "DESKTOP_OWNER_REQUIRED"
    assert access_request["status"] == "ok"
    assert access_request["data"]["status"] == "owner"
    assert wrong_agent_click["status"] == "error"
    assert wrong_agent_click["error"]["code"] == "DESKTOP_AGENT_NOT_ASSIGNED"
    assert spoofed_body_wrong_context["status"] == "error"
    assert spoofed_body_wrong_context["error"]["code"] == "DESKTOP_AGENT_NOT_ASSIGNED"
    assert spoofed_body_no_context["status"] == "error"
    assert spoofed_body_no_context["error"]["code"] == "DESKTOP_AGENT_PRINCIPAL_REQUIRED"
    assert ai_click["status"] == "ok"
    assert ai_click["data"]["agent_id"] == "agent-1"
    assert ai_click["data"]["assigned_agent"] == "agent-1"
    assert ai_click["data"]["role"] == "coding desktop"
    assert ai_click["data"]["rules"]["role"] == "coding desktop"
    assert ai_click["data"]["rules"]["rule_ids"] == ["playwright-ok"]
    assert lease["data"]["lease_token"] == "lease-token"
    assert ai_conflict["status"] == "error"
    assert ai_conflict["error"]["code"] == DESKTOP_CONTROL_CONFLICT
    audit_events = service.manager.read_desktop_audit_events()
    assert {event["code"] for event in audit_events if event["code"]} >= {"DESKTOP_AGENT_NOT_ASSIGNED"}
    assert all(
        "correct-key" not in str(event)
        and scoped_credential not in str(event)
        and "lease-token" not in str(event)
        for event in audit_events
    )


def test_desktop_owner_only_and_shared_link_access_are_distinct(tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    ids = iter(["owner-seat", "link-seat"])
    registry = ProviderRegistry()
    registry.register(
        FakeRuntimeProvider(
            provider_id="fake-runtime",
            capabilities={
                "sandbox.exec",
                "sandbox.files",
                "sandbox.resource_limits",
                "sandbox.network_policy",
                "sandbox.desktop",
                "sandbox.desktop_input",
                "sandbox.snapshot",
            },
            sandbox_id_factory=lambda: next(ids),
        )
    )
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=ControlLeaseManager(ttl_seconds=30),
    )
    api._reset_service_for_tests(service)
    trusted_link_context = {
        "source": "defaultspack_local_ui",
        "trusted_audience": "https://fake-audience.invalid/desktop",
        "trusted_origin": "https://fake-origin.invalid",
        "authenticated_principal_id": "fake-link-principal",
        "authenticated_device_id": "fake-link-device",
        "authenticated_session_id": "fake-link-session",
    }
    try:
        owner_only = api.run(
            {
                "_handler": "desktops_create",
                "template_id": "desktop.ubuntu",
                "provider_id": "fake-runtime",
                "owner_id": "owner-1",
                "access": {"mode": "owner_only", "owner_id": "owner-1"},
            },
            {"user_id": "owner-1"},
        )
        shared_link = api.run(
            {
                "_handler": "desktops_create",
                "template_id": "desktop.ubuntu",
                "provider_id": "fake-runtime",
                "access": {"mode": "shared_link"},
            },
            {"user_id": "owner-1"},
        )
        owner_spoof_denied = api.run(
            {"_handler": "desktop_get", "seat_id": "owner-seat", "owner_id": "owner-1"},
            {"user_id": "attacker-1"},
        )
        owner_allowed = api.run(
            {"_handler": "desktop_get", "seat_id": "owner-seat"},
            {"user_id": "owner-1"},
        )
        link_denied_without_token = api.run(
            {"_handler": "desktop_get", "seat_id": "link-seat"},
            {},
        )
        link_allowed_with_token = api.run(
            {
                "_handler": "desktop_get",
                "seat_id": "link-seat",
                "access_key": shared_link["data"]["access_key"],
            },
            {},
        )
        issued = api.run(
            {
                "_handler": "desktop_exchange_issue",
                "seat_id": "link-seat",
                "operations": ["desktop.read"],
            },
            trusted_link_context,
        )
        redeemed = api.run(
            {
                "_handler": "desktop_exchange_redeem",
                "exchange_code": issued["data"]["exchange_code"],
            },
            trusted_link_context,
        )
        link_allowed_with_scope = api.run(
            {
                "_handler": "desktop_get",
                "seat_id": "link-seat",
                "desktop_session_credential": redeemed["data"]["session_credential"],
            },
            trusted_link_context,
        )
    finally:
        api._reset_service_for_tests(None)

    assert owner_only["status"] == "ok"
    assert shared_link["status"] == "ok"
    assert shared_link["data"]["access_key"]
    assert shared_link["data"]["access_key_hint"] == shared_link["data"]["access_policy"]["key_hint"]
    assert shared_link["data"]["access_key"] not in str(shared_link["data"]["access_policy"])
    assert owner_spoof_denied["status"] == "error"
    assert owner_spoof_denied["error"]["code"] == "DESKTOP_OWNER_REQUIRED"
    assert owner_allowed["status"] == "ok"
    assert link_denied_without_token["status"] == "error"
    assert link_denied_without_token["error"]["code"] == "DESKTOP_SHARED_LINK_TOKEN_REQUIRED"
    assert link_allowed_with_token["status"] == "error"
    assert link_allowed_with_token["error"]["code"] == "DESKTOP_ACCESS_KEY_MIGRATION_REQUIRED"
    assert issued["status"] == "ok"
    assert redeemed["status"] == "ok"
    assert link_allowed_with_scope["status"] == "ok"
    assert link_allowed_with_scope["data"]["access_policy"]["mode"] == "shared_link"
    assert link_allowed_with_scope["data"]["access_policy"]["link_enabled"] is True


def test_desktop_request_required_access_can_be_requested_and_granted(tmp_path, monkeypatch) -> None:
    _trusted_workspace(tmp_path, monkeypatch)
    from ecosystem.defaultspack.blocks.sandbox import api

    registry = ProviderRegistry()
    registry.register(
        FakeRuntimeProvider(
            provider_id="fake-runtime",
            capabilities={
                "sandbox.exec",
                "sandbox.files",
                "sandbox.resource_limits",
                "sandbox.network_policy",
                "sandbox.desktop",
                "sandbox.desktop_input",
                "sandbox.snapshot",
            },
            sandbox_id_factory=lambda: "seat-request",
        )
    )
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=ControlLeaseManager(ttl_seconds=30),
    )
    api._reset_service_for_tests(service)
    trusted_requester_context = {
        "source": "defaultspack_local_ui",
        "trusted_audience": "https://fake-audience.invalid/desktop",
        "trusted_origin": "https://fake-origin.invalid",
        "authenticated_principal_id": "requester-1",
        "authenticated_device_id": "fake-requester-device",
        "authenticated_session_id": "fake-requester-session",
    }
    try:
        created = api.run(
            {
                "_handler": "desktops_create",
                "template_id": "desktop.ubuntu",
                "provider_id": "fake-runtime",
                "owner_id": "owner-1",
                "access": {"mode": "request_required"},
            },
            {"user_id": "owner-1"},
        )
        requester_denied = api.run(
            {"_handler": "desktop_get", "seat_id": "seat-request", "owner_id": "owner-1"},
            {"user_id": "requester-1"},
        )
        owner_allowed = api.run(
            {"_handler": "desktop_get", "seat_id": "seat-request"},
            {"user_id": "owner-1"},
        )
        access_request = api.run(
            {
                "_handler": "desktop_access_request",
                "seat_id": "seat-request",
                "reason": "Need to inspect the browser session.",
            },
            {"user_id": "requester-1"},
        )
        wrong_owner_grant = api.run(
            {
                "_handler": "desktop_access_grant",
                "seat_id": "seat-request",
                "request_id": access_request["data"]["request_id"],
                "owner_id": "owner-1",
            },
            {"user_id": "requester-1"},
        )
        granted = api.run(
            {
                "_handler": "desktop_access_grant",
                "seat_id": "seat-request",
                "request_id": access_request["data"]["request_id"],
            },
            {"user_id": "owner-1"},
        )
        granted_key = granted["data"]["access_key"]
        requester_allowed = api.run(
            {"_handler": "desktop_get", "seat_id": "seat-request", "access_key": granted_key},
            {},
        )
        issued = api.run(
            {
                "_handler": "desktop_exchange_issue",
                "seat_id": "seat-request",
                "operations": ["desktop.read"],
            },
            trusted_requester_context,
        )
        redeemed = api.run(
            {
                "_handler": "desktop_exchange_redeem",
                "exchange_code": issued["data"]["exchange_code"],
            },
            trusted_requester_context,
        )
        requester_allowed_with_scope = api.run(
            {
                "_handler": "desktop_get",
                "seat_id": "seat-request",
                "desktop_session_credential": redeemed["data"]["session_credential"],
            },
            trusted_requester_context,
        )
        owner_reverted = api.run(
            {
                "_handler": "desktop_rules_update",
                "seat_id": "seat-request",
                "access": {"mode": "owner_only"},
            },
            {"user_id": "owner-1"},
        )
        requester_after_revert = api.run(
            {
                "_handler": "desktop_get",
                "seat_id": "seat-request",
                "desktop_session_credential": redeemed["data"]["session_credential"],
            },
            trusted_requester_context,
        )
        request_after_revert = api.run(
            {
                "_handler": "desktop_access_request",
                "seat_id": "seat-request",
            },
            {"user_id": "requester-2"},
        )
        registry_after_revert = service.manager.registry_path.read_text(encoding="utf-8")
        registry_text = service.manager.registry_path.read_text(encoding="utf-8")
        destroyed = service.manager.destroy("seat-request")
        registry_after_destroy = service.manager.registry_path.read_text(encoding="utf-8")
    finally:
        api._reset_service_for_tests(None)

    assert created["status"] == "ok"
    assert created["data"]["access_policy"]["mode"] == "request_required"
    assert created["data"]["access_policy"]["request_required"] is True
    assert requester_denied["status"] == "error"
    assert requester_denied["error"]["code"] == "DESKTOP_ACCESS_REQUEST_REQUIRED"
    assert owner_allowed["status"] == "ok"
    assert access_request["status"] == "ok"
    assert access_request["data"]["status"] == "pending"
    assert "access_key" not in access_request["data"]
    assert wrong_owner_grant["status"] == "error"
    assert wrong_owner_grant["error"]["code"] == "DESKTOP_OWNER_REQUIRED"
    assert granted["status"] == "ok"
    assert granted["data"]["status"] == "approved"
    assert granted["data"]["access_key_hint"].startswith("ends:")
    assert requester_allowed["status"] == "error"
    assert requester_allowed["error"]["code"] == "DESKTOP_ACCESS_KEY_MIGRATION_REQUIRED"
    assert issued["status"] == "ok"
    assert redeemed["status"] == "ok"
    assert requester_allowed_with_scope["status"] == "ok"
    assert owner_reverted["status"] == "ok"
    assert owner_reverted["data"]["access_policy"]["mode"] == "owner_only"
    assert owner_reverted["data"]["access_policy"]["request_required"] is False
    assert requester_after_revert["status"] == "error"
    assert requester_after_revert["error"]["code"] == "DESKTOP_SESSION_CREDENTIAL_REVOKED"
    assert request_after_revert["status"] == "error"
    assert request_after_revert["error"]["code"] == "DESKTOP_ACCESS_REQUEST_NOT_REQUIRED"
    assert access_request["data"]["request_id"] not in registry_after_revert
    assert granted_key not in registry_text
    assert destroyed["ok"] is True
    assert access_request["data"]["request_id"] not in registry_after_destroy


def test_defaultspack_runtime_requires_captured_operation(monkeypatch) -> None:
    del monkeypatch
    from tests.v4_batch_support import assert_route_cutover

    assert_route_cutover(
        "GET",
        "/api/runtime/providers",
        "tobkiri.managed-runtime.v1",
        "defaultspack.managed-runtime.providers",
    )


def test_runtime_doctor_selects_ready_desktop_provider(monkeypatch, tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    monkeypatch.setattr("ecosystem.defaultspack.blocks.sandbox.api.platform.system", lambda: "Linux")
    registry = ProviderRegistry()
    registry.register(FakeRuntimeProvider(provider_id="linux_native", ready=False, capabilities={"sandbox.desktop"}))
    registry.register(FakeRuntimeProvider(provider_id="docker", ready=True, capabilities={"sandbox.exec", "sandbox.files"}))
    registry.register(
        FakeRuntimeProvider(
            provider_id="mac_lima",
            ready=True,
            capabilities={"sandbox.desktop", "sandbox.desktop_input", "sandbox.snapshot"},
        )
    )
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        frame_cache=FrameCache(),
        lease_manager=ControlLeaseManager(),
    )
    api._reset_service_for_tests(service)
    try:
        providers = api.run({"_handler": "runtime_providers"}, {})
        doctor = api.run({"_handler": "runtime_doctor"}, {})
    finally:
        api._reset_service_for_tests(None)

    assert providers["data"]["default_provider_id"] == "linux_native"
    assert providers["data"]["selected_provider_id"] == "mac_lima"
    selected = {provider["provider_id"]: provider["selected"] for provider in providers["data"]["providers"]}
    assert selected["linux_native"] is False
    assert selected["docker"] is False
    assert selected["mac_lima"] is True
    assert doctor["data"]["status"] == "ready"
    assert doctor["data"]["selected_provider_id"] == "mac_lima"


def test_runtime_doctor_does_not_treat_exec_only_provider_as_desktop_ready(monkeypatch, tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    monkeypatch.setattr("ecosystem.defaultspack.blocks.sandbox.api.platform.system", lambda: "Linux")
    registry = ProviderRegistry()
    registry.register(FakeRuntimeProvider(provider_id="linux_native", ready=False, capabilities={"sandbox.desktop"}))
    registry.register(FakeRuntimeProvider(provider_id="docker", ready=True, capabilities={"sandbox.exec", "sandbox.files"}))
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        frame_cache=FrameCache(),
        lease_manager=ControlLeaseManager(),
    )
    api._reset_service_for_tests(service)
    try:
        providers = api.run({"_handler": "runtime_providers"}, {})
        doctor = api.run({"_handler": "runtime_doctor"}, {})
    finally:
        api._reset_service_for_tests(None)

    assert providers["data"]["default_provider_id"] == "linux_native"
    assert providers["data"]["selected_provider_id"] == "linux_native"
    selected = {provider["provider_id"]: provider["selected"] for provider in providers["data"]["providers"]}
    assert selected["linux_native"] is True
    assert selected["docker"] is False
    assert doctor["data"]["status"] == "needs_setup"
    assert doctor["data"]["selected_provider_id"] == "linux_native"


def test_defaultspack_runtime_service_registers_cross_platform_providers(tmp_path, monkeypatch) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    monkeypatch.setenv("RUMI_DEFAULTSPACK_SANDBOX_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("RUMI_CLOUDFLARE_SANDBOX_BRIDGE_URL", raising=False)
    monkeypatch.delenv("RUMI_CLOUDFLARE_SANDBOX_API_KEY", raising=False)
    service = api._SandboxApiService()
    provider_ids = set(service.provider_registry.provider_ids())
    cloudflare_status = service.provider_registry.doctor("cloudflare_sandbox_bridge")
    mac_isolation = api._provider_isolation("mac_lima", True)
    windows_isolation = api._provider_isolation("windows_wsl", True)
    linux_isolation = api._provider_isolation("linux_native", True)
    docker_isolation = api._provider_isolation("docker", True)
    cloudflare_isolation = api._provider_isolation("cloudflare_sandbox_bridge", True)

    assert provider_ids == {
        "cloudflare_sandbox_bridge",
        "docker",
        "linux_native",
        "mac_lima",
        "windows_wsl",
    }
    assert mac_isolation["mode"] == "lima_vm"
    assert mac_isolation["vm"] is True
    assert mac_isolation["security_boundary"] is True
    assert mac_isolation["separate_workdirs"] is True
    assert mac_isolation["shared_guest_identity"] is True
    assert mac_isolation["sandbox_workspace_shared"] is False
    assert mac_isolation["sandbox_process_namespace_shared"] is False
    assert mac_isolation["sandbox_operation_binding"] == "provider_instance_id"
    assert mac_isolation["sandbox_cgroup_scope"] == "guest_prlimit"
    assert mac_isolation["process_cleanup"] == "pid_namespace"
    assert mac_isolation["untrusted_pack_boundary"] is True
    assert mac_isolation["desktop_security_boundary"] is False
    assert "Desktop GUI processes share" in mac_isolation["warnings"][0]
    assert "prlimit" in mac_isolation["warnings"][1]
    assert windows_isolation["mode"] == "wsl2_vm"
    assert windows_isolation["vm"] is True
    assert windows_isolation["security_boundary"] is False
    assert windows_isolation["separate_workdirs"] is True
    assert windows_isolation["shared_guest_identity"] is True
    assert windows_isolation["sandbox_workspace_shared"] is True
    assert windows_isolation["sandbox_process_namespace_shared"] is True
    assert windows_isolation["sandbox_operation_binding"] == "provider_instance_id"
    assert windows_isolation["process_cleanup"] == "best_effort"
    assert windows_isolation["untrusted_pack_boundary"] is False
    assert linux_isolation["mode"] == "native_x11"
    assert linux_isolation["security_boundary"] is False
    assert docker_isolation["container"] is True
    assert docker_isolation["sandbox_process_namespace_shared"] is False
    assert docker_isolation["sandbox_cgroup_scope"] == "docker_container"
    assert cloudflare_isolation["container"] is True
    assert cloudflare_isolation["sandbox_workspace_shared"] is False
    assert cloudflare_isolation["sandbox_process_namespace_shared"] is False
    assert cloudflare_isolation["sandbox_network_namespace_shared"] is False
    assert cloudflare_isolation["sandbox_cgroup_scope"] == "cloudflare_container"
    assert cloudflare_isolation["sandbox_operation_binding"] == "bridge_sandbox_id"
    assert "PC-local" in cloudflare_isolation["summary"]


def test_windows_wsl_provider_detects_utf16_like_distribution_names(monkeypatch) -> None:
    from ecosystem.defaultspack.backend.sandbox.providers import managed_ubuntu
    from ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu import (
        GuestCommandResult,
        WindowsWslProvider,
    )

    commands: list[tuple[str, ...]] = []

    def runner(command, input_text, timeout):
        del input_text, timeout
        argv = tuple(command)
        commands.append(argv)
        if argv == ("wsl.exe", "--version"):
            return GuestCommandResult(returncode=0, stdout="WSL version: 2.5.0\n")
        if argv == ("wsl.exe", "-l", "-q"):
            return GuestCommandResult(
                returncode=0,
                stdout="d\x00o\x00c\x00k\x00e\x00r\x00-\x00d\x00e\x00s\x00k\x00t\x00o\x00p\x00\n\x00R\x00u\x00m\x00i\x00U\x00b\x00u\x00n\x00t\x00u\x00\n\x00",
            )
        if argv[:5] == ("wsl.exe", "-d", "RumiUbuntu", "--", "bash"):
            return GuestCommandResult(returncode=0, stdout="")
        return GuestCommandResult(returncode=1, stderr=f"unexpected command: {argv!r}")

    monkeypatch.setattr(managed_ubuntu.platform, "system", lambda: "Windows")
    provider = WindowsWslProvider(command_path="wsl.exe", runner=runner)

    status = provider.doctor(
        RuntimeRequirements(provider_id="windows_wsl", required_capabilities=frozenset())
    )

    dependency_checks = [
        command for command in commands if command[:5] == ("wsl.exe", "-d", "RumiUbuntu", "--", "bash")
    ]
    assert status.ready is True
    assert "managed_guest" not in status.missing_requirements
    assert dependency_checks
    assert "command -v Xvfb" in dependency_checks[0][-1]


def test_windows_wsl_guest_shell_preserves_guest_dollar_expansion_and_stdin() -> None:
    from ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu import (
        GuestCommandResult,
        WindowsWslProvider,
    )

    captured = {}

    def runner(command, input_text, timeout):
        captured["command"] = tuple(command)
        captured["input_text"] = input_text
        captured["timeout"] = timeout
        return GuestCommandResult(returncode=0, stdout="ok")

    provider = WindowsWslProvider(command_path="wsl.exe", runner=runner)

    result = provider._guest_shell(
        "wsl.exe",
        'DISPLAY_ID=":98"\nprintf "%s" "$DISPLAY_ID"',
        input_text="payload",
        timeout=5,
        check=False,
    )

    assert result.stdout == "ok"
    assert captured["command"] == (
        "wsl.exe",
        "-d",
        "RumiUbuntu",
        "--",
        "bash",
        "-lc",
        'DISPLAY_ID=":98"\nprintf "%s" "$DISPLAY_ID"',
    )
    assert captured["input_text"] == "payload"
    assert captured["timeout"] == 5
    assert "\\$DISPLAY_ID" not in captured["command"][-1]


def test_managed_ubuntu_desktop_start_script_prepares_x11_socket_dir_and_checks_processes() -> None:
    from ecosystem.defaultspack.backend.sandbox.providers.managed_ubuntu import _desktop_running_script, _desktop_start_script

    def assert_in_order(text: str, *needles: str) -> None:
        position = -1
        for needle in needles:
            next_position = text.index(needle, position + 1)
            assert next_position > position
            position = next_position

    script = _desktop_start_script(
        "windows_wsl-seat-1",
        "/workspace/windows_wsl-seat-1",
        1440,
        900,
        ":98",
        False,
        {"starter": "empty"},
    )

    assert "chmod 1777 /tmp/.X11-unix" in script
    assert "CLIENT_DISPLAY=\"127.0.0.1:${DISPLAY_NUM}.0\"" in script
    assert "$XVFB_TRANSPORT_ARGS" in script
    assert "-nolisten local -listen tcp" in script
    assert "display.env" in script
    assert "run_display_service setsid" in script
    assert "run_ui() {" in script
    assert "rumi_process_matches_instance" in script
    assert "rumi_find_instance_pid Xvfb" in script
    assert "rumi_find_instance_pid openbox" in script
    assert "rumi_pidfile_alive" in script
    assert "/proc/$pid/environ" in script
    assert "launched_xvfb" in script
    assert "launched_openbox" in script
    assert script.index("launched_xvfb") < script.index("Desktop Xvfb failed to stay running.")
    assert script.index("launched_openbox") < script.index("Desktop openbox failed to stay running.")
    assert 'rm -f "/tmp/.X${DISPLAY_NUM}-lock"' in script
    assert "Desktop Xvfb failed to stay running." in script
    assert "Desktop openbox failed to stay running." in script
    assert_in_order(
        script,
        "Xvfb :98 -screen 0 1440x900x24",
        "sleep 0.5",
        'launched_xvfb="$(rumi_find_instance_pid Xvfb || true)"',
        'echo "$launched_xvfb" > /tmp/rumi-managed-runtime/windows_wsl-seat-1/xvfb.pid',
        "Desktop Xvfb failed to stay running.",
    )
    assert_in_order(
        script,
        'DISPLAY="$CLIENT_DISPLAY" openbox',
        "sleep 0.2",
        'launched_openbox="$(rumi_find_instance_pid openbox || true)"',
        'echo "$launched_openbox" > /tmp/rumi-managed-runtime/windows_wsl-seat-1/openbox.pid',
        "Desktop openbox failed to stay running.",
    )

    running_script = _desktop_running_script("windows_wsl-seat-1")
    assert "rumi_process_matches_instance" in running_script
    assert "rumi_find_instance_pid Xvfb" in running_script
    assert "rumi_find_instance_pid openbox" in running_script
    assert "rumi_pidfile_alive" in running_script
    assert "/proc/$pid/environ" in running_script
    assert "xvfb.pid" in running_script
    assert "openbox.pid" in running_script


def test_runtime_update_and_uninstall_use_provider_operation_results(tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    registry = ProviderRegistry()
    provider = FakeRuntimeProvider(provider_id="fake-runtime")
    registry.register(provider)
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=ControlLeaseManager(),
    )
    api._reset_service_for_tests(service)
    try:
        update = api.run(
            {"_handler": "runtime_update", "provider_id": "fake-runtime", "request_id": "fake-update"},
            {},
        )
        update_done = _wait_for_runtime_operation(api, "fake-update")
        operations = api.run({"_handler": "runtime_operations"}, {})
        operation_get = api.run(
            {"_handler": "runtime_operation_get", "operation_id": "fake-update"},
            {},
        )
        operation_cancel = api.run(
            {"_handler": "runtime_operation_cancel", "operation_id": "fake-update"},
            {},
        )
        uninstall_without_confirmation = api.run(
            {"_handler": "runtime_uninstall", "provider_id": "fake-runtime", "request_id": "fake-uninstall-denied"},
            {},
        )
        uninstall = api.run(
            {
                "_handler": "runtime_uninstall",
                "provider_id": "fake-runtime",
                "request_id": "fake-uninstall",
                "confirm_destructive": True,
            },
            {},
        )
        uninstall_done = _wait_for_runtime_operation(api, "fake-uninstall")
    finally:
        api._reset_service_for_tests(None)

    assert update["data"]["operation_id"] == "fake-update"
    assert update["data"]["status"] == "running"
    assert update["data"]["step"] == "queued"
    assert update_done["status"] == "completed"
    assert update_done["step"] == "done"
    assert update_done["message"] == "Fake provider updated"
    assert update_done["progress_events"][0]["stage"] == "done"
    assert [operation["operation_id"] for operation in operations["data"]["operations"]] == ["fake-update"]
    assert operations["data"]["operations"][0]["progress_events"][0]["message"] == "Fake provider updated"
    assert operation_get["data"]["operation_id"] == "fake-update"
    assert operation_get["data"]["progress_events"][0]["operation_id"] == "fake-update"
    assert operation_cancel["data"]["operation_id"] == "fake-update"
    assert operation_cancel["data"]["cancelled"] is False
    assert operation_cancel["data"]["status"] == "completed"
    assert uninstall_without_confirmation["status"] == "error"
    assert uninstall_without_confirmation["_http_status"] == 409
    assert uninstall_without_confirmation["error"]["code"] == "DESTRUCTIVE_ACTION_CONFIRMATION_REQUIRED"
    assert uninstall["data"]["operation_id"] == "fake-uninstall"
    assert uninstall["data"]["status"] == "running"
    assert uninstall_done["status"] == "completed"
    assert uninstall_done["progress_events"][0]["stage"] == "done"


def test_runtime_ensure_persists_running_progress_events(tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    operation_store = RuntimeOperationStore(tmp_path / "runtime_operations.json")

    class ProgressRuntimeProvider(FakeRuntimeProvider):
        def __init__(self) -> None:
            super().__init__(provider_id="fake-runtime")
            self.running_snapshot: dict[str, object] | None = None

        def ensure(self, request, progress):
            progress.emit(
                ProgressEvent(
                    operation_id="fake-ensure",
                    stage="doctor",
                    message="Checking fake runtime",
                    percent=10,
                )
            )
            self.running_snapshot = operation_store.get("fake-ensure")
            progress.emit(
                ProgressEvent(
                    operation_id="fake-ensure",
                    stage="ready",
                    message="Fake runtime ready",
                    percent=100,
                )
            )
            return OperationResult(ok=True, provider_id=self.provider_id, operation_id="fake-ensure", status="ready")

    registry = ProviderRegistry()
    provider = ProgressRuntimeProvider()
    registry.register(provider)
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        operation_store=operation_store,
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=ControlLeaseManager(),
    )
    api._reset_service_for_tests(service)
    try:
        ensure = api.run(
            {"_handler": "runtime_ensure", "provider_id": "fake-runtime", "request_id": "fake-ensure"},
            {},
        )
        operation_get = _wait_for_runtime_operation(api, "fake-ensure")
    finally:
        api._reset_service_for_tests(None)
    reloaded = RuntimeOperationStore(tmp_path / "runtime_operations.json")

    assert provider.running_snapshot is not None
    assert provider.running_snapshot["status"] == "running"
    assert provider.running_snapshot["step"] == "doctor"
    assert provider.running_snapshot["progress"] == 10
    assert ensure["data"]["status"] == "running"
    assert ensure["data"]["progress"] == 0
    assert operation_get["status"] == "completed"
    assert operation_get["progress"] == 100
    assert [event["stage"] for event in operation_get["progress_events"]] == ["doctor", "ready"]
    assert reloaded.get("fake-ensure")["progress_events"][1]["message"] == "Fake runtime ready"


def test_runtime_operation_cancel_preserves_cancelled_status_after_worker_finishes(tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class BlockingRuntimeProvider(FakeRuntimeProvider):
        def __init__(self) -> None:
            super().__init__(provider_id="fake-runtime")

        def ensure(self, request, progress):
            progress.emit(
                ProgressEvent(
                    operation_id="provider-op",
                    stage="packages",
                    message="Installing fake runtime packages",
                    percent=40,
                )
            )
            started.set()
            release.wait(timeout=3)
            progress.emit(
                ProgressEvent(
                    operation_id="provider-op",
                    stage="ready",
                    message="Fake runtime ready",
                    percent=100,
                )
            )
            finished.set()
            return OperationResult(ok=True, provider_id=self.provider_id, operation_id="provider-op", status="ready")

    registry = ProviderRegistry()
    registry.register(BlockingRuntimeProvider())
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        operation_store=RuntimeOperationStore(tmp_path / "runtime_operations.json"),
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=ControlLeaseManager(),
    )
    api._reset_service_for_tests(service)
    try:
        ensure = api.run(
            {"_handler": "runtime_ensure", "provider_id": "fake-runtime", "request_id": "cancel-op"},
            {},
        )
        assert started.wait(timeout=3)
        cancelled = api.run({"_handler": "runtime_operation_cancel", "operation_id": "cancel-op"}, {})
        release.set()
        assert not finished.wait(timeout=0.2)
        final = api.run({"_handler": "runtime_operation_get", "operation_id": "cancel-op"}, {})
    finally:
        release.set()
        api._reset_service_for_tests(None)

    assert ensure["data"]["status"] == "running"
    assert cancelled["data"]["status"] == "cancelled"
    assert cancelled["data"]["cancelled"] is True
    assert final["data"]["status"] == "cancelled"
    assert [event["stage"] for event in final["data"]["progress_events"]] == ["packages"]


def test_runtime_operation_cancel_terminates_active_subprocess(tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api
    from ecosystem.defaultspack.backend.sandbox.cancellation import (
        RuntimeOperationCancelled,
        run_cancellable_subprocess,
    )

    started_file = tmp_path / "subprocess-started"
    finished_file = tmp_path / "subprocess-finished"
    worker_cancelled = threading.Event()
    script = (
        "from pathlib import Path\n"
        "import sys\n"
        "import time\n"
        "Path(sys.argv[1]).write_text('started', encoding='utf-8')\n"
        "time.sleep(30)\n"
        "Path(sys.argv[2]).write_text('finished', encoding='utf-8')\n"
    )

    class CancellableSubprocessProvider(FakeRuntimeProvider):
        def __init__(self) -> None:
            super().__init__(provider_id="fake-runtime")

        def ensure(self, request, progress):
            progress.emit(
                ProgressEvent(
                    operation_id="provider-op",
                    stage="packages",
                    message="Spawning cancellable fake runtime subprocess",
                    percent=40,
                )
            )
            try:
                run_cancellable_subprocess(
                    (sys.executable, "-c", script, str(started_file), str(finished_file)),
                    timeout=30,
                )
            except RuntimeOperationCancelled:
                worker_cancelled.set()
                raise
            return OperationResult(ok=True, provider_id=self.provider_id, operation_id="provider-op", status="ready")

    registry = ProviderRegistry()
    registry.register(CancellableSubprocessProvider())
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        operation_store=RuntimeOperationStore(tmp_path / "runtime_operations.json"),
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=ControlLeaseManager(),
    )
    api._reset_service_for_tests(service)
    try:
        ensure = api.run(
            {"_handler": "runtime_ensure", "provider_id": "fake-runtime", "request_id": "subprocess-cancel"},
            {},
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not started_file.exists():
            time.sleep(0.02)
        assert started_file.exists()
        cancelled = api.run({"_handler": "runtime_operation_cancel", "operation_id": "subprocess-cancel"}, {})
        assert worker_cancelled.wait(timeout=3)
        final = api.run({"_handler": "runtime_operation_get", "operation_id": "subprocess-cancel"}, {})
    finally:
        api._reset_service_for_tests(None)

    assert ensure["data"]["status"] == "running"
    assert cancelled["data"]["status"] == "cancelled"
    assert cancelled["data"]["cancelled"] is True
    assert final["data"]["status"] == "cancelled"
    assert not finished_file.exists()
    assert [event["stage"] for event in final["data"]["progress_events"]] == ["packages"]


def test_cancellable_subprocess_replaces_non_utf8_and_nul_output() -> None:
    from ecosystem.defaultspack.backend.sandbox.cancellation import run_cancellable_subprocess

    script = (
        "import sys; "
        "sys.stdout.buffer.write(b'frame-\\x89png\\x00tail'); "
        "sys.stderr.buffer.write(b'bad-\\xfc-byte\\x00err')"
    )

    completed = run_cancellable_subprocess((sys.executable, "-c", script), timeout=5)

    assert completed.returncode == 0
    assert completed.stdout == "frame-\ufffdpng\x00tail"
    assert completed.stderr == "bad-\ufffd-byte\x00err"


def test_runtime_operations_are_single_flight_per_provider(tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    started = threading.Event()
    release = threading.Event()

    class SingleFlightRuntimeProvider(FakeRuntimeProvider):
        def __init__(self) -> None:
            super().__init__(provider_id="fake-runtime")
            self.update_calls = 0

        def ensure(self, request, progress):
            progress.emit(
                ProgressEvent(
                    operation_id="provider-ensure",
                    stage="packages",
                    message="Installing fake runtime packages",
                    percent=20,
                )
            )
            started.set()
            release.wait(timeout=3)
            progress.emit(
                ProgressEvent(
                    operation_id="provider-ensure",
                    stage="ready",
                    message="Fake runtime ready",
                    percent=100,
                )
            )
            return OperationResult(ok=True, provider_id=self.provider_id, operation_id="provider-ensure", status="ready")

        def update(self, request, progress):
            self.update_calls += 1
            progress.emit(
                ProgressEvent(
                    operation_id="provider-update",
                    stage="done",
                    message="Fake runtime updated",
                    percent=100,
                )
            )
            return OperationResult(ok=True, provider_id=self.provider_id, operation_id="provider-update", status="updated")

    provider = SingleFlightRuntimeProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        operation_store=RuntimeOperationStore(tmp_path / "runtime_operations.json"),
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=ControlLeaseManager(),
    )
    api._reset_service_for_tests(service)
    try:
        ensure = api.run(
            {"_handler": "runtime_ensure", "provider_id": "fake-runtime", "request_id": "ensure-op"},
            {},
        )
        assert started.wait(timeout=3)
        concurrent_update = api.run(
            {"_handler": "runtime_update", "provider_id": "fake-runtime", "request_id": "update-op"},
            {},
        )
        release.set()
        ensure_done = _wait_for_runtime_operation(api, "ensure-op")
        update = api.run(
            {"_handler": "runtime_update", "provider_id": "fake-runtime", "request_id": "update-op"},
            {},
        )
        update_done = _wait_for_runtime_operation(api, "update-op")
    finally:
        release.set()
        api._reset_service_for_tests(None)

    assert ensure["data"]["operation_id"] == "ensure-op"
    assert concurrent_update["data"]["operation_id"] == "ensure-op"
    assert provider.update_calls == 1
    assert ensure_done["status"] == "completed"
    assert update["data"]["operation_id"] == "update-op"
    assert update_done["status"] == "completed"
    assert update_done["progress_events"][0]["stage"] == "done"


def test_runtime_operation_store_preserves_cancelled_running_operation(tmp_path) -> None:
    store = RuntimeOperationStore(tmp_path / "runtime_operations.json")
    store.append_progress(
        {
            "operation_id": "op-1",
            "stage": "packages",
            "message": "Installing runtime packages",
            "percent": 40,
            "details": {},
            "recorded_at": "2026-06-22T00:00:00Z",
        },
        provider_id="fake-runtime",
        updated_at="2026-06-22T00:00:00Z",
    )
    cancelled = store.cancel("op-1", updated_at="2026-06-22T00:00:01Z")
    final = store.put(
        {
            "operation_id": "op-1",
            "status": "completed",
            "step": "ready",
            "message": "Runtime ready",
            "progress": 100,
            "progress_events": [
                {
                    "operation_id": "op-1",
                    "stage": "ready",
                    "message": "Runtime ready",
                    "percent": 100,
                    "details": {},
                    "recorded_at": "2026-06-22T00:00:02Z",
                }
            ],
            "provider_id": "fake-runtime",
            "updated_at": "2026-06-22T00:00:02Z",
            "error": None,
        }
    )

    assert cancelled["cancelled"] is True
    assert final["status"] == "cancelled"
    assert final["cancelled"] is True
    assert [event["stage"] for event in final["progress_events"]] == ["packages", "ready"]


def test_runtime_operation_store_marks_nonterminal_operations_interrupted_on_restart(tmp_path) -> None:
    store = RuntimeOperationStore(tmp_path / "runtime_operations.json")
    store.put(
        {
            "operation_id": "running-op",
            "status": "running",
            "step": "packages",
            "message": "Installing runtime packages",
            "progress": 40,
            "provider_id": "fake-runtime",
            "updated_at": "2026-06-22T00:00:00Z",
            "error": None,
        }
    )
    store.put(
        {
            "operation_id": "completed-op",
            "status": "completed",
            "step": "ready",
            "message": "Runtime ready",
            "progress": 100,
            "provider_id": "fake-runtime",
            "updated_at": "2026-06-22T00:00:01Z",
            "error": None,
        }
    )

    interrupted = store.interrupt_nonterminal(
        updated_at="2026-06-22T00:00:02Z",
        message="Runtime operation was interrupted by a restart.",
    )
    reloaded = RuntimeOperationStore(tmp_path / "runtime_operations.json")

    assert [operation["operation_id"] for operation in interrupted] == ["running-op"]
    assert reloaded.get("running-op")["status"] == "failed"
    assert reloaded.get("running-op")["error"]["code"] == "RUNTIME_OPERATION_INTERRUPTED"
    assert reloaded.get("running-op")["progress"] == 40
    assert reloaded.get("completed-op")["status"] == "completed"


def test_runtime_uninstall_reconciles_manager_desktops_and_local_state(tmp_path) -> None:
    from ecosystem.defaultspack.blocks.sandbox import api

    registry = ProviderRegistry()
    provider = FakeRuntimeProvider(
        provider_id="fake-runtime",
        capabilities={
            "sandbox.exec",
            "sandbox.files",
            "sandbox.resource_limits",
            "sandbox.network_policy",
            "sandbox.desktop",
            "sandbox.desktop_input",
            "sandbox.snapshot",
        },
        sandbox_id_factory=lambda: "seat-uninstall",
    )
    registry.register(provider)
    lease_manager = ControlLeaseManager(token_factory=lambda: "lease-token")
    service = SimpleNamespace(
        provider_registry=registry,
        manager=SandboxManager(state_dir=tmp_path, provider_registry=registry),
        frame_cache=FrameCache(min_capture_interval_seconds=0),
        lease_manager=lease_manager,
    )
    created = service.manager.create(
        display=True,
        provider_id="fake-runtime",
        template_id="desktop.ubuntu",
        access_owner_id="local-user",
    )
    seat_id = str(created["sandbox_id"])
    service.frame_cache.put_frame(seat_id, b"frame", content_type="image/png", width=2, height=2)
    lease_manager.acquire(seat_id, "human")
    api._reset_service_for_tests(service)
    try:
        uninstall = api.run(
            {
                "_handler": "runtime_uninstall",
                "provider_id": "fake-runtime",
                "request_id": "fake-uninstall-reconcile",
                "confirm_destructive": True,
            },
            {},
        )
        uninstall_done = _wait_for_runtime_operation(api, "fake-uninstall-reconcile")
    finally:
        api._reset_service_for_tests(None)

    status = service.manager.status(seat_id)
    assert uninstall["data"]["status"] == "running"
    assert uninstall_done["status"] == "completed"
    assert status["state"] == "stopped"
    assert "uninstalled" in status["last_error"]
    assert service.frame_cache.last_metadata(seat_id) is None
    assert service.lease_manager.active_lease(seat_id) is None

    provider._sandbox_id_factory = lambda: "seat-remove-state"
    recreated = service.manager.create(
        display=True,
        provider_id="fake-runtime",
        template_id="desktop.ubuntu",
        access_owner_id="local-user",
    )
    assert recreated["ok"] is True
    api._reset_service_for_tests(service)
    try:
        remove_state = api.run(
            {
                "_handler": "runtime_uninstall",
                "provider_id": "fake-runtime",
                "remove_state": True,
                "request_id": "fake-uninstall-remove-state",
                "confirm_destructive": True,
            },
            {},
        )
        remove_state_done = _wait_for_runtime_operation(api, "fake-uninstall-remove-state")
    finally:
        api._reset_service_for_tests(None)

    assert remove_state["data"]["status"] == "running"
    assert remove_state_done["status"] == "completed"
    assert service.manager.list_instances() == []


def test_runtime_mutation_routes_are_local_guard_sensitive() -> None:
    from ecosystem.defaultspack.domain.safety.local_guard import is_sensitive_coding_path

    assert is_sensitive_coding_path("/api/runtime/ensure", "POST") is True
    assert is_sensitive_coding_path("/api/runtime/operations/op-1/cancel", "POST") is True
    assert is_sensitive_coding_path("/api/desktops", "POST") is True
    assert is_sensitive_coding_path("/api/desktops/seat-1/input", "POST") is True


def _template() -> ResolvedSandboxTemplate:
    return ResolvedSandboxTemplate(
        template_id="desktop.ubuntu",
        template_version="1",
        runtime_os="linux",
        provider_requirements=frozenset({"sandbox.exec", "sandbox.desktop"}),
        packages=(),
        desktop=DesktopSpec(enabled=True, width=800, height=600),
        filesystem=FilesystemPolicy(),
        network=NetworkPolicy(mode="off"),
        secrets=SecretsPolicy(mode="denied"),
        resources=ResourceLimits(cpu_count=1, memory_mb=1024),
        lifecycle=LifecyclePolicy(ttl_seconds=900),
        allowed_operations=frozenset({"exec", "desktop.input"}),
        source_template_ids=("desktop.ubuntu",),
    )


def _desktop_only_template() -> ResolvedSandboxTemplate:
    return ResolvedSandboxTemplate(
        template_id="desktop.ubuntu",
        template_version="1",
        runtime_os="linux",
        provider_requirements=frozenset({"sandbox.desktop", "sandbox.desktop_input", "sandbox.snapshot"}),
        packages=(),
        desktop=DesktopSpec(enabled=True, width=800, height=600),
        filesystem=FilesystemPolicy(),
        network=NetworkPolicy(mode="off"),
        secrets=SecretsPolicy(mode="denied"),
        resources=ResourceLimits(cpu_count=1, memory_mb=1024),
        lifecycle=LifecyclePolicy(ttl_seconds=900),
        allowed_operations=frozenset({"desktop.input", "desktop.snapshot"}),
        source_template_ids=("desktop.ubuntu",),
    )


def _create_spec(template: ResolvedSandboxTemplate, *, metadata: dict[str, object] | None = None):
    from ecosystem.defaultspack.backend.sandbox.models import SandboxCreateSpec

    return SandboxCreateSpec(name="fake desktop", template=template, provider_id="fake-runtime", metadata=metadata or {})


class FakeX11Session:
    display = ":99"

    def __init__(self, *, width: int, height: int, session_dir: Path | None = None) -> None:
        self.config = SimpleNamespace(width=width, height=height)
        self.session_dir = session_dir
        self.calls: list[tuple[object, ...]] = []
        self.last_screenshot_path = None

    def missing_commands(self) -> list[str]:
        return []

    def start(self) -> dict[str, object]:
        self.calls.append(("start",))
        return {"available": True, "running": True}

    def stop(self) -> dict[str, object]:
        self.calls.append(("stop",))
        return {"available": True, "running": False}

    def screenshot(self) -> dict[str, object]:
        import base64
        import tempfile
        from pathlib import Path

        self.calls.append(("screenshot",))
        fd, path = tempfile.mkstemp(prefix="rumi-fake-x11-", suffix=".png")
        try:
            import os

            os.close(fd)
        except OSError:
            pass
        self.last_screenshot_path = Path(path)
        self.last_screenshot_path.write_bytes(b"png")
        return {
            "path": path,
            "data_url": "data:image/png;base64," + base64.b64encode(b"png").decode("ascii"),
        }

    def click(self, x: int, y: int, *, button: str = "left") -> dict[str, object]:
        self.calls.append(("click", x, y, button))
        return {"executed": True}

    def launch(self, name: str, args: list[str], *, stdout_name: str | None = None) -> dict[str, object]:
        self.calls.append(("launch", name, tuple(args), stdout_name))
        return {
            "executed": True,
            "command": list(args),
            "pid": 1234,
            "process": f"launch-{name}",
            "log_path": str(self.session_dir / stdout_name) if self.session_dir is not None and stdout_name else None,
        }

    def owned_session_metadata(self) -> dict[str, object]:
        return {"display": self.display, "processes": {"xvfb": {"pid": 4321}}}


class CaptureGuestAgent(FakeGuestAgent):
    def capture_frame(self, sandbox_id: str, seat_id: str) -> dict[str, object]:
        return {
            "ok": True,
            "sandbox_id": sandbox_id,
            "seat_id": seat_id,
            "content_type": "image/png",
            "data": b"fake-png",
            "width": self.width,
            "height": self.height,
            "source": "capture_guest_agent",
        }
