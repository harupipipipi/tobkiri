"""Managed reads are real snapshots and cannot become lifecycle commands."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Mapping
import pytest


def load_candidate(name: str, relative: str) -> ModuleType:
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(name, root / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


owner = load_candidate(
    "managed_desktop_owner",
    "ecosystem/rumi_sandbox_runtime_pack/runtime/managed_desktops.py",
)
presentation = load_candidate(
    "managed_desktop_presentation",
    "ecosystem/defaultspack/defaultspack/managed_desktop_presentation.py",
)
OPERATION = next(iter(owner.OPERATIONS))


@pytest.fixture(autouse=True)
def isolated_legacy_defaults_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        owner,
        "legacy_defaults_registry_path",
        lambda: tmp_path / "absent-legacy" / "sandboxes.json",
    )


def context(root: Path, profile: str = "defaults") -> SimpleNamespace:
    binding = SimpleNamespace(
        function=SimpleNamespace(
            function_id=owner.FUNCTION_ID, implementation_digest="sha256:impl"
        ),
        operation=SimpleNamespace(
            contract_id=owner.CONTRACT_ID,
            operation_id=OPERATION,
            contract_version="1.0.0",
        ),
        principal_ref=SimpleNamespace(value="managed-owner"),
        artifact=SimpleNamespace(digest="sha256:artifact"),
    )
    return SimpleNamespace(
        profile_id=profile,
        user_data_root=root,
        provider_bindings=(binding,),
        domain_ids={(owner.CONTRACT_ID, OPERATION, "managed-owner"): "domain"},
    )


class Invocation:
    def __init__(self) -> None:
        self.checked = False

    def assert_current(self) -> None:
        self.checked = True


def test_read_real_registry_without_mutating_or_disclosing_credentials(
    tmp_path: Path,
) -> None:
    path = owner.registry_path_for_profile(tmp_path, "defaults")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 5,
                "instances": {
                    "desktop": {
                        "sandbox_id": "seat-1",
                        "display": True,
                        "state": "ready",
                        "name": "My actual desktop",
                        "provider_id": "mac_lima",
                        "provider_opaque_state": {"password": "sensitive-fixture"},
                    },
                    "coding": {"sandbox_id": "coding-1", "display": False},
                },
            }
        )
    )
    before = path.read_bytes()
    captured = owner.ManagedDesktopReadHostFactoryV4().capture(context(tmp_path))
    invocation = Invocation()
    result = captured.contributions[0].invoke(
        OPERATION, {"profile_id": "defaults"}, invocation
    )
    assert invocation.checked
    assert result["desktops"][0]["name"] == "My actual desktop"
    assert result["desktops"][0]["status"] == "running"
    assert len(result["desktops"]) == 1
    assert "sensitive-fixture" not in json.dumps(result)
    assert path.read_bytes() == before


def test_missing_registry_does_not_create_state(tmp_path: Path) -> None:
    captured = owner.ManagedDesktopReadHostFactoryV4().capture(context(tmp_path))
    assert captured.contributions[0].invoke(
        OPERATION, {"profile_id": "defaults"}, Invocation()
    ) == {"desktops": []}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"profile_id": "foreign"},
        {"profile_id": "defaults", "approved": True},
        {"profile_id": "defaults", "state_dir": "/tmp"},
        {"profile_id": "defaults", "operation": "start"},
    ],
)
def test_authority_and_path_payloads_are_rejected(
    tmp_path: Path, payload: Mapping[str, Any]
) -> None:
    captured = owner.ManagedDesktopReadHostFactoryV4().capture(context(tmp_path))
    with pytest.raises(PermissionError):
        captured.contributions[0].invoke(OPERATION, payload, Invocation())


def test_profile_paths_are_distinct_and_cannot_traverse(tmp_path: Path) -> None:
    assert owner.registry_path_for_profile(
        tmp_path, "a"
    ) != owner.registry_path_for_profile(tmp_path, "b")
    with pytest.raises(ValueError):
        owner.registry_path_for_profile(tmp_path, "../other")


def test_corrupt_registry_is_not_renamed_or_repaired(tmp_path: Path) -> None:
    path = tmp_path / "sandboxes.json"
    path.write_text("{broken")
    with pytest.raises(json.JSONDecodeError):
        owner.read_desktops(path)
    assert path.read_text() == "{broken"
    assert list(tmp_path.iterdir()) == [path]


def test_symlink_registry_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_text('{"schema_version":5,"instances":{}}')
    path = tmp_path / "sandboxes.json"
    path.symlink_to(source)
    with pytest.raises(PermissionError):
        owner.read_desktops(path)


def test_presentation_captures_profile_and_returns_frontend_envelope() -> None:
    target = next(item for item in presentation.READ_TARGETS if item[2] == OPERATION)
    assert presentation.normalize_managed_desktop_read(
        target, {}, profile_id="defaults"
    ) == {"profile_id": "defaults"}
    with pytest.raises(ValueError):
        presentation.normalize_managed_desktop_read(
            target, {"approved": True}, profile_id="defaults"
        )
    assert presentation.present_managed_desktop_read({"desktops": []}) == {
        "desktops": []
    }


def test_stale_invocation_never_reads_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = owner.ManagedDesktopReadHostFactoryV4().capture(context(tmp_path))

    def unexpected(path: Path) -> None:
        pytest.fail("stale invocation reached state read")

    monkeypatch.setattr(owner, "read_desktops", unexpected)
    invocation = Invocation()

    def stale() -> None:
        raise PermissionError("stale invocation")

    invocation.assert_current = stale
    with pytest.raises(PermissionError):
        captured.contributions[0].invoke(
            OPERATION, {"profile_id": "defaults"}, invocation
        )


def test_capture_rejects_missing_domain_and_wrong_function(tmp_path: Path) -> None:
    captured_context = context(tmp_path)
    captured_context.domain_ids = {}
    with pytest.raises(PermissionError):
        owner.ManagedDesktopReadHostFactoryV4().capture(captured_context)
    captured_context = context(tmp_path)
    captured_context.provider_bindings[0].function.function_id = "physical-host-screen"
    with pytest.raises(PermissionError):
        owner.ManagedDesktopReadHostFactoryV4().capture(captured_context)


def test_foreign_profile_registry_is_not_read(tmp_path: Path) -> None:
    foreign_path = owner.registry_path_for_profile(tmp_path, "foreign")
    foreign_path.parent.mkdir(parents=True)
    foreign_path.write_text("{corrupt foreign state}")
    captured = owner.ManagedDesktopReadHostFactoryV4().capture(context(tmp_path))
    assert captured.contributions[0].invoke(
        OPERATION, {"profile_id": "defaults"}, Invocation()
    ) == {"desktops": []}


def test_diagnostic_normalizer_drops_only_request_identity() -> None:
    target = next(
        item
        for item in presentation.READ_TARGETS
        if item[2].endswith("runtime-doctor-read")
    )
    assert presentation.normalize_managed_desktop_read(
        target, {"request_id": "doctor-123"}, profile_id="defaults"
    ) == {"profile_id": "defaults"}
    with pytest.raises(ValueError):
        presentation.normalize_managed_desktop_read(
            target, {"approved": True}, profile_id="defaults"
        )


def test_storage_matches_canonical_profile_workspace(tmp_path: Path) -> None:
    from core_runtime.profile_workspace import ProfileWorkspaceManager

    expected = ProfileWorkspaceManager(tmp_path).paths_for_profile("defaults").state_dir
    assert (
        owner.registry_path_for_profile(tmp_path, "defaults")
        == expected / "sandbox" / "sandboxes.json"
    )


def test_explicit_host_owned_legacy_defaults_registry_is_read_without_copy(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy" / "sandboxes.json"
    legacy.parent.mkdir()
    legacy.write_text(
        json.dumps(
            {
                "schema_version": 5,
                "instances": {
                    "old": {
                        "sandbox_id": "old-real-seat",
                        "display": True,
                        "state": "stopped",
                        "provider_id": "mac_lima",
                    }
                },
            }
        )
    )
    snapshot = legacy.read_bytes()
    factory = owner.ManagedDesktopReadHostFactoryV4(
        captured_legacy_defaults_registry=legacy
    )
    captured = factory.capture(context(tmp_path))
    result = captured.contributions[0].invoke(
        OPERATION, {"profile_id": "defaults"}, Invocation()
    )
    assert result["desktops"][0]["seat_id"] == "old-real-seat"
    assert legacy.read_bytes() == snapshot
    assert not owner.registry_path_for_profile(tmp_path, "defaults").exists()
    foreign = factory.capture(context(tmp_path, "foreign"))
    assert foreign.contributions[0].invoke(
        OPERATION, {"profile_id": "foreign"}, Invocation()
    ) == {"desktops": []}


def test_profile_registry_takes_precedence_over_legacy_registry(tmp_path: Path) -> None:
    scoped = owner.registry_path_for_profile(tmp_path, "defaults")
    scoped.parent.mkdir(parents=True)
    scoped.write_text('{"schema_version":5,"instances":{}}')
    legacy = tmp_path / "legacy.json"
    legacy.write_text("{corrupt legacy}")
    factory = owner.ManagedDesktopReadHostFactoryV4(
        captured_legacy_defaults_registry=legacy
    )
    captured = factory.capture(context(tmp_path))
    assert captured.contributions[0].invoke(
        OPERATION, {"profile_id": "defaults"}, Invocation()
    ) == {"desktops": []}


def test_exported_factory_reads_historical_defaults_without_manager_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ecosystem.defaultspack.backend.sandbox.sandbox_manager import SandboxManager

    legacy_dir = tmp_path / "old-defaultspack-sandbox"
    legacy_dir.mkdir()
    registry = legacy_dir / "sandboxes.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": 5,
                "instances": {
                    "actual": {
                        "sandbox_id": "historical-seat",
                        "display": True,
                        "state": "stopped",
                        "provider_id": "mac_lima",
                    }
                },
            }
        )
    )
    before = registry.read_bytes()
    monkeypatch.setattr(
        SandboxManager, "_default_state_dir", staticmethod(lambda: legacy_dir)
    )

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("read reached manager construction or lifecycle")

    monkeypatch.setattr(SandboxManager, "__init__", forbidden)
    monkeypatch.setattr(SandboxManager, "list_instances", forbidden)
    monkeypatch.setattr(SandboxManager, "enforce_lifecycle", forbidden)
    # Restore the real static compatibility resolver after isolation fixture.
    monkeypatch.setattr(
        owner,
        "legacy_defaults_registry_path",
        lambda: SandboxManager._default_state_dir() / "sandboxes.json",
    )
    factory = owner.HOST_PROVIDER_FACTORY[owner.FUNCTION_ID]
    captured = factory.capture(context(tmp_path))
    result = captured.contributions[0].invoke(
        OPERATION, {"profile_id": "defaults"}, Invocation()
    )
    assert result["desktops"][0]["seat_id"] == "historical-seat"
    assert registry.read_bytes() == before
    assert not owner.registry_path_for_profile(tmp_path, "defaults").exists()
    foreign = factory.capture(context(tmp_path, "other"))
    assert foreign.contributions[0].invoke(
        OPERATION, {"profile_id": "other"}, Invocation()
    ) == {"desktops": []}


def test_exported_factory_rejects_redirected_historical_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "actual.json"
    target.write_text('{"schema_version":5,"instances":{}}')
    redirected = tmp_path / "legacy.json"
    redirected.symlink_to(target)
    monkeypatch.setattr(owner, "legacy_defaults_registry_path", lambda: redirected)
    captured = owner.HOST_PROVIDER_FACTORY[owner.FUNCTION_ID].capture(context(tmp_path))
    with pytest.raises(PermissionError):
        captured.contributions[0].invoke(
            OPERATION, {"profile_id": "defaults"}, Invocation()
        )


def test_metadata_never_runs_provider_health_or_reads_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ecosystem.defaultspack.backend.sandbox.providers import (
        CloudflareSandboxBridgeProvider,
        DockerProvider,
        LinuxNativeProvider,
        MacLimaProvider,
        WindowsWslProvider,
    )

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("metadata read reached provider health/credentials/process")

    for provider in (
        CloudflareSandboxBridgeProvider,
        DockerProvider,
        LinuxNativeProvider,
        MacLimaProvider,
        WindowsWslProvider,
    ):
        monkeypatch.setattr(provider, "doctor", forbidden)
        monkeypatch.setattr(provider, "__init__", forbidden)
    monkeypatch.setattr(CloudflareSandboxBridgeProvider, "_api_key", forbidden)
    monkeypatch.setattr(CloudflareSandboxBridgeProvider, "_base_url", forbidden)
    result = owner.read_runtime_metadata("providers")
    assert len(result["providers"]) == 5
    assert all(
        item["diagnostics"]["probe_status"] == "not_run" for item in result["providers"]
    )
    assert all(
        "ready" not in item and "installed" not in item for item in result["providers"]
    )
    remote = next(
        item
        for item in result["providers"]
        if item["provider_id"] == "cloudflare_sandbox_bridge"
    )
    assert "sandbox.desktop" not in remote["capabilities"]
    assert "credential and network capability" in remote["message"]
    doctor = owner.read_runtime_metadata("doctor")
    assert doctor["status"] == "unavailable"
    assert doctor["diagnostics"]["probe_status"] == "not_run"
