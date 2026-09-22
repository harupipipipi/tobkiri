"""Physical retirement checks for the pre-v4 execution authorities."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


RUNTIME = Path(__file__).resolve().parents[1]
RETIRED_MODULES = {
    "binding_handlers.py",
    "capability_binding_registration.py",
    "capability_executor.py",
    "capability_graph_compiler.py",
    "capability_graph_loader.py",
    "capability_graph_handlers.py",
    "component_lifecycle.py",
    "control_panel_handlers.py",
    "ecosystem_nodes.py",
    "function_registry.py",
    "interface_registry.py",
    "kernel.py",
    "kernel_context_builder.py",
    "kernel_core.py",
    "kernel_handlers_system.py",
    "permission_manager.py",
    "profile_graph_builder.py",
    "profile_graph_models.py",
    "profile_loader.py",
    "profile_models.py",
    "profile_node_registry.py",
    "profile_runtime_selection.py",
    "profile_workspace_migration.py",
    "node_state.py",
    "port_standards.py",
    "startup_capability_bridge.py",
    "startup_profiles.py",
}


def test_retired_execution_authority_modules_are_physically_absent() -> None:
    """Legacy executable authorities cannot be imported from production."""
    core = RUNTIME / "core_runtime"
    assert not {path.name for path in core.iterdir()} & RETIRED_MODULES


def test_manifest_authority_catalog_classifies_all_direct_pack_roots() -> None:
    """The finite catalog owns every root as the v4 runtime authority."""
    ecosystem = RUNTIME / "ecosystem"
    roots = {
        path.name
        for path in ecosystem.iterdir()
        if path.is_dir() and path.name != "setup_pack" and not path.name.startswith(".")
    }
    catalog = json.loads(
        (RUNTIME / "schemas" / "manifest_authority.v1.json").read_text(encoding="utf-8")
    )["packs"]
    assert set(catalog) == roots
    assert len(catalog) == 143
    assert set(catalog.values()) == {"v4-authoritative"}
    assert catalog["defaults"] == "v4-authoritative"
    assert catalog["defaultspack"] == "v4-authoritative"
    assert all((ecosystem / pack_id / "pack.v4.json").is_file() for pack_id in roots)
    assert not any(
        (ecosystem / pack_id / legacy_name).exists()
        for pack_id in ("defaults", "defaultspack")
        for legacy_name in ("ecosystem.json", "rumi.pack.v3.json")
    )
    from backend_core.ecosystem.registry import (
        LegacyRegistryUnavailable,
        Registry,
    )

    with pytest.raises(LegacyRegistryUnavailable):
        Registry().load_all_packs()


def test_top_level_runtime_does_not_import_legacy_composition() -> None:
    """A clean process reaches no retired authority through ``tobkiri``."""
    code = """
import json
import sys
import tobkiri.runtime
blocked = {
    'app',
    'backend_core.ecosystem.registry',
    'core_runtime.capability_executor',
    'core_runtime.function_registry',
    'core_runtime.interface_registry',
}
print(json.dumps(sorted(blocked.intersection(sys.modules))))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=RUNTIME,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == "[]"


def test_production_packages_do_not_export_legacy_profiles_or_executors() -> None:
    """A clean package import exposes no pre-Broker execution entrypoint."""

    code = """
import json
import sys
import core_runtime
import core_runtime.global_contracts
from core_runtime.di_container import get_container

retired_exports = {
    'ContainerOrchestrator',
    'DockerRunBuilder',
    'FlowComposer',
    'FlowLoader',
    'LibExecutor',
    'PythonFileExecutor',
    'SecureExecutor',
    'UnitExecutor',
}
retired_services = {
    'container_orchestrator',
    'docker_capability_handler',
    'flow_composer',
    'lib_executor',
    'python_file_executor',
    'secure_executor',
    'unit_executor',
}
print(json.dumps({
    'exports': sorted(name for name in retired_exports if hasattr(core_runtime, name)),
    'services': sorted(retired_services.intersection(get_container().registered_names())),
    'manifest_loaded': 'core_runtime.global_contracts.manifest' in sys.modules,
    'legacy_contract_exports': sorted(
        name
        for name in {'ContractRegistry', 'LegacyRegistryProjection', 'load_manifest'}
        if hasattr(core_runtime.global_contracts, name)
    ),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=RUNTIME,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert json.loads(result.stdout) == {
        "exports": [],
        "services": [],
        "manifest_loaded": False,
        "legacy_contract_exports": [],
    }


def test_runtime_projection_module_is_metadata_only() -> None:
    """Only the offline script retains one-way projection implementation."""
    from core_runtime import manifest_projection

    assert manifest_projection.PROJECTION_RUNTIME_EXECUTABLE is False
    assert manifest_projection.PROJECTION_OWNER == "scripts/offline_legacy_projection.py"
    assert manifest_projection.PROJECTION_SOURCE == "rumi.pack.v3.json"
    assert not hasattr(manifest_projection, "generate_legacy_ecosystem_projection")
    offline = (RUNTIME / "scripts" / "offline_legacy_projection.py").read_text(encoding="utf-8")
    assert 'PROJECTION_SOURCE = "rumi.pack.v3.json"' in offline
    assert "RUNTIME_EXECUTABLE = False" in offline
    assert "def generate_legacy_ecosystem_projection(" in offline


def test_canonical_startup_never_reads_or_writes_legacy_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real v4 startup ignores all pre-v4 Profile and setup authorities."""

    from core_runtime.bootstrap.profile_capture import (
        capture_default_profile,
        prepare_default_profile_confirmation,
    )
    from core_runtime.di_container import reset_container
    from core_runtime.pack_api_server import shutdown_pack_api_server

    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    legacy_paths = (
        user_data / "settings" / "profile.json",
        user_data / "settings" / "setup_pack_selection.json",
        user_data / "active_ecosystem" / "active_ecosystem.json",
        user_data / "legacy-pack" / "ecosystem.json",
        user_data / "legacy-pack" / "rumi.pack.v3.json",
    )
    for path in legacy_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"legacy":true}\n', encoding="utf-8")
    capture_default_profile(confirmation=prepare_default_profile_confirmation())

    forbidden_names = {
        "active_ecosystem.json",
        "ecosystem.json",
        "profile.json",
        "rumi.pack.v3.json",
        "setup_pack_selection.json",
    }

    def forbidden(path: Path) -> bool:
        return path.name in forbidden_names or "setup_pack" in path.parts

    def guard(method):
        def checked(path: Path, *args, **kwargs):
            candidate = Path(path)
            if forbidden(candidate):
                raise AssertionError(f"canonical startup touched legacy state: {candidate}")
            return method(path, *args, **kwargs)

        return checked

    for method_name in ("open", "read_bytes", "read_text", "write_bytes", "write_text"):
        monkeypatch.setattr(Path, method_name, guard(getattr(Path, method_name)))

    import core_runtime.bootstrap.runtime as runtime_bootstrap

    shutdown_pack_api_server()
    reset_container()
    monkeypatch.setattr(runtime_bootstrap, "resolve_runtime_port", lambda: 0)
    kernel = runtime_bootstrap.Kernel()
    try:
        result = kernel.run_startup()
        assert result["runtime_ready"] is True
    finally:
        kernel.shutdown()
        shutdown_pack_api_server()
        reset_container()
