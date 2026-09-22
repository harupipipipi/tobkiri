"""
conftest.py - テスト共通 fixture

core_runtime/__init__.py は大量のサブモジュールを import するため、
テストでは __init__.py の実行を回避し、対象サブモジュールのみを
直接 import できるようにする。
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import types
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_PARENT = _PROJECT_ROOT.parent

if str(_PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_PARENT))
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ---------------------------------------------------------------------------
# core_runtime パッケージを __init__.py を実行せずに登録する
# ---------------------------------------------------------------------------
_CORE_RUNTIME_DIR = str(_PROJECT_ROOT / "core_runtime")
_CORE_RUNTIME_PACKAGE_DIR = _PROJECT_ROOT / "core_runtime"
_CORE_RUNTIME_ALIAS_PREFIX = "tobkiri_runtime.core_runtime"


def _module_path_exists(package_dir: Path, attr_name: str) -> bool:
    if attr_name.startswith("__"):
        return False
    for child in package_dir.iterdir():
        if child.is_file() and child.name == f"{attr_name}.py":
            return True
        if child.is_dir() and child.name == attr_name and (child / "__init__.py").is_file():
            return True
    return False


def _alias_for_module(module_name: str) -> str | None:
    if module_name == "core_runtime":
        return _CORE_RUNTIME_ALIAS_PREFIX
    if module_name.startswith("core_runtime."):
        return f"{_CORE_RUNTIME_ALIAS_PREFIX}{module_name.removeprefix('core_runtime')}"
    return None


def _canonical_for_module(module_name: str) -> str | None:
    if module_name == _CORE_RUNTIME_ALIAS_PREFIX:
        return "core_runtime"
    prefix = f"{_CORE_RUNTIME_ALIAS_PREFIX}."
    if module_name.startswith(prefix):
        return f"core_runtime.{module_name.removeprefix(prefix)}"
    return None


def _is_real_core_runtime_module(module) -> bool:
    module_file = getattr(module, "__file__", None)
    if not module_file:
        return False
    try:
        Path(module_file).resolve().relative_to(_CORE_RUNTIME_PACKAGE_DIR.resolve())
        return True
    except (OSError, ValueError):
        return False


def _bind_parent_module(module_name: str, module=None) -> None:
    module = sys.modules.get(module_name) if module is None else module
    if module is None or "." not in module_name:
        return
    parent_name, attr_name = module_name.rsplit(".", 1)
    parent = sys.modules.get(parent_name)
    if parent is not None:
        setattr(parent, attr_name, module)


def _sync_core_runtime_alias(module_name: str, module=None) -> None:
    module = sys.modules.get(module_name) if module is None else module
    if module is None:
        return

    alias_name = _alias_for_module(module_name)
    canonical_name = _canonical_for_module(module_name)

    if alias_name:
        existing_alias = sys.modules.get(alias_name)
        if existing_alias is not None and existing_alias is not module:
            if _is_real_core_runtime_module(existing_alias) and not _is_real_core_runtime_module(module):
                sys.modules[module_name] = existing_alias
                _bind_parent_module(module_name, existing_alias)
                module = existing_alias
            else:
                sys.modules[alias_name] = module
                _bind_parent_module(alias_name, module)
        else:
            sys.modules[alias_name] = module
            _bind_parent_module(alias_name, module)
    elif canonical_name:
        sys.modules[canonical_name] = module
        _bind_parent_module(canonical_name, module)


def _make_lazy_submodule_getattr(package_name: str, package_dir: Path):
    def _lazy_submodule_getattr(attr_name: str):
        if not _module_path_exists(package_dir, attr_name):
            raise AttributeError(f"module {package_name!r} has no attribute {attr_name!r}")

        module_name = f"{package_name}.{attr_name}"
        canonical_name = _canonical_for_module(module_name)
        alias_name = _alias_for_module(module_name)

        module = None
        if alias_name:
            existing_alias = sys.modules.get(alias_name)
            if existing_alias is not None and _is_real_core_runtime_module(existing_alias):
                module = existing_alias
        if module is None:
            module = sys.modules.get(module_name)
        if module is None and canonical_name:
            module = sys.modules.get(canonical_name)
        if module is None and alias_name:
            module = sys.modules.get(alias_name)
        if module is None:
            module = importlib.import_module(module_name)

        _bind_parent_module(module_name, module)
        if canonical_name:
            sys.modules[canonical_name] = module
            _bind_parent_module(canonical_name, module)
            _sync_core_runtime_alias(canonical_name, module)
        if alias_name:
            sys.modules[alias_name] = module
            _bind_parent_module(alias_name, module)
            _sync_core_runtime_alias(module_name, module)

        package = sys.modules.get(package_name)
        if package is not None:
            setattr(package, attr_name, module)
        return module

    return _lazy_submodule_getattr


def _install_core_runtime_package_hooks(module_name: str, package_dir: Path) -> None:
    pkg = sys.modules.get(module_name)
    if pkg is None:
        return
    pkg.__getattr__ = _make_lazy_submodule_getattr(module_name, package_dir)


def _ensure_package_module(module_name: str, package_dir: Path) -> None:
    pkg = sys.modules.get(module_name)
    if pkg is None:
        pkg = types.ModuleType(module_name)
        sys.modules[module_name] = pkg
    pkg.__path__ = [str(package_dir)]
    pkg.__package__ = module_name
    pkg.__file__ = str(package_dir / "__init__.py")
    _install_core_runtime_package_hooks(module_name, package_dir)
    if "." in module_name:
        parent_name, attr_name = module_name.rsplit(".", 1)
        parent = sys.modules.get(parent_name)
        if parent is not None:
            setattr(parent, attr_name, pkg)


def _reset_package_roots() -> None:
    _ensure_package_module("core_runtime", _PROJECT_ROOT / "core_runtime")
    _ensure_package_module("backend_core", _PROJECT_ROOT / "backend_core")
    _ensure_package_module("backend_core.ecosystem", _PROJECT_ROOT / "backend_core" / "ecosystem")
    _ensure_package_module("tobkiri_runtime", _PROJECT_ROOT)
    _ensure_package_module("tobkiri_runtime.core_runtime", _PROJECT_ROOT / "core_runtime")
    _ensure_package_module("tobkiri_runtime.backend_core", _PROJECT_ROOT / "backend_core")
    core_pkg = sys.modules.get("core_runtime")
    alias_pkg = sys.modules.get(_CORE_RUNTIME_ALIAS_PREFIX)
    if core_pkg is not None and alias_pkg is not None:
        for attr_name, value in vars(core_pkg).items():
            if not attr_name.startswith("__"):
                setattr(alias_pkg, attr_name, value)


_reset_package_roots()


def _compute_file_sha256(file_path: Path) -> str | None:
    try:
        path = Path(file_path)
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


@dataclass
class _ShimHandlerDef:
    handler_id: str
    permission_id: str
    entrypoint: str
    description: str = ""
    risk: str = "low"
    handler_dir: Path | None = None
    handler_py_path: Path | None = None
    handler_py_sha256: str | None = None
    is_builtin: bool = False


@dataclass
class _ShimLoadResult:
    success: bool = True
    handlers_loaded: int = 0
    errors: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)


class _ShimCapabilityHandlerRegistry:
    def __init__(self, handlers_dir: str | None = None):
        self.handlers_dir = Path(handlers_dir) if handlers_dir else None
        self._builtin_handlers_dir = _PROJECT_ROOT / "core_runtime" / "builtin_capability_handlers"
        self._core_pack_handler_dirs: list[Path] = []
        self._by_permission_id: dict[str, _ShimHandlerDef] = {}
        self._by_handler_id: dict[str, _ShimHandlerDef] = {}
        self._loaded = False

    def _iter_source_dirs(self):
        if self.handlers_dir:
            yield self.handlers_dir, False
        if self._builtin_handlers_dir:
            yield Path(self._builtin_handlers_dir), True
        for directory in self._core_pack_handler_dirs:
            yield Path(directory), True

    def load_all(self) -> _ShimLoadResult:
        self._by_permission_id.clear()
        self._by_handler_id.clear()
        result = _ShimLoadResult()
        pending: dict[str, list[_ShimHandlerDef]] = {}

        for base_dir, is_builtin in self._iter_source_dirs():
            if not base_dir.exists():
                continue
            for slug_dir in sorted((p for p in base_dir.iterdir() if p.is_dir()), key=lambda p: p.name):
                handler_json = slug_dir / "handler.json"
                if not handler_json.is_file():
                    if (slug_dir / "handler.py").exists():
                        result.errors.append({"handler_dir": str(slug_dir), "error": "Missing handler.json"})
                    continue
                try:
                    data = json.loads(handler_json.read_text(encoding="utf-8"))
                except Exception as exc:
                    result.errors.append({"handler_dir": str(slug_dir), "error": f"Invalid handler.json: {exc}"})
                    continue

                handler_id = data.get("handler_id")
                permission_id = data.get("permission_id")
                entrypoint = data.get("entrypoint")
                if not handler_id or not permission_id or not entrypoint:
                    result.errors.append({"handler_dir": str(slug_dir), "error": "Missing required handler fields"})
                    continue
                if ":" not in entrypoint:
                    result.errors.append({"handler_dir": str(slug_dir), "error": "Invalid entrypoint format"})
                    continue

                rel_path, _callable_name = entrypoint.split(":", 1)
                handler_py = slug_dir / rel_path
                if not handler_py.is_file():
                    result.errors.append({"handler_dir": str(slug_dir), "error": "Missing entrypoint file"})
                    continue

                if handler_id in self._by_handler_id:
                    result.errors.append({"handler_dir": str(slug_dir), "error": f"Duplicate handler_id: {handler_id}"})
                    continue

                entry = _ShimHandlerDef(
                    handler_id=handler_id,
                    permission_id=permission_id,
                    entrypoint=entrypoint,
                    description=data.get("description", ""),
                    risk=data.get("risk", "low"),
                    handler_dir=slug_dir,
                    handler_py_path=handler_py,
                    handler_py_sha256=_compute_file_sha256(handler_py),
                    is_builtin=is_builtin,
                )
                self._by_handler_id[handler_id] = entry
                pending.setdefault(permission_id, []).append(entry)

        for permission_id, entries in pending.items():
            if len(entries) > 1:
                result.success = False
                result.duplicates.append({"permission_id": permission_id, "handler_count": len(entries)})
                for entry in entries:
                    self._by_handler_id.pop(entry.handler_id, None)
                continue
            self._by_permission_id[permission_id] = entries[0]

        result.handlers_loaded = len(self._by_permission_id)
        self._loaded = result.success and result.handlers_loaded > 0
        return result

    def is_loaded(self) -> bool:
        return self._loaded

    def get_by_permission_id(self, permission_id: str):
        return self._by_permission_id.get(permission_id)

    def get_by_handler_id(self, handler_id: str):
        return self._by_handler_id.get(handler_id)

    def list_permission_ids(self) -> list[str]:
        return sorted(self._by_permission_id)


def _install_capability_handler_registry_shim() -> None:
    module_name = "core_runtime.capability_handler_registry"
    if module_name not in sys.modules:
        mod = types.ModuleType(module_name)
        mod.CapabilityHandlerRegistry = _ShimCapabilityHandlerRegistry
        mod.compute_file_sha256 = _compute_file_sha256
        mod.__file__ = str(_PROJECT_ROOT / "tests" / "_capability_handler_registry_shim.py")
        sys.modules[module_name] = mod

    alias_name = "tobkiri_runtime.core_runtime.capability_handler_registry"
    if alias_name not in sys.modules:
        sys.modules[alias_name] = sys.modules[module_name]


def _force_real_import(module_name: str) -> None:
    """collection 時の sys.modules 汚染を、必要なテストの前に実 module へ戻す。"""
    sys.modules.pop(module_name, None)
    alias_name = _alias_for_module(module_name)
    if alias_name:
        sys.modules.pop(alias_name, None)
    module = importlib.import_module(module_name)
    _bind_parent_module(module_name, module)
    if alias_name:
        sys.modules[alias_name] = module
        _bind_parent_module(alias_name, module)


def _sync_alias_module(alias_name: str, target_name: str) -> None:
    target_module = sys.modules.get(target_name)
    if target_module is None:
        target_module = importlib.import_module(target_name)
    sys.modules[alias_name] = target_module
    _bind_parent_module(alias_name, target_module)
    _sync_core_runtime_alias(target_name, target_module)


def _restore_real_di_container() -> None:
    _REAL_DI_CONTAINER_MODULE.get_container = _REAL_GET_CONTAINER
    sys.modules["core_runtime.di_container"] = _REAL_DI_CONTAINER_MODULE
    _bind_parent_module("core_runtime.di_container", _REAL_DI_CONTAINER_MODULE)


_RESTORE_REAL_MODULES = (
    "core_runtime.deprecation",
    "core_runtime.kernel_core",
    "core_runtime.kernel_flow_execution",
    "core_runtime.kernel_handlers_runtime",
    "core_runtime.audit_logger",
    "core_runtime.network_grant_manager",
    "core_runtime.capability_proxy",
    "core_runtime.paths",
    "backend_core.ecosystem.mounts",
    "backend_core.ecosystem.registry",
    "backend_core.ecosystem.compat",
    "backend_core.ecosystem.uuid_utils",
    "backend_core.ecosystem.json_patch",
    "backend_core.ecosystem.spec",
    "backend_core.ecosystem.spec.schema",
    "backend_core.ecosystem.spec.schema.validator",
)

_BIND_ONLY_MODULES = (
    "core_runtime.egress_proxy",
    "core_runtime.store_registry",
    "core_runtime.container_orchestrator",
    "core_runtime.kernel_core",
    "core_runtime.kernel_handlers_system",
    "core_runtime.python_file_executor",
    "core_runtime.unit_executor",
)

_ALIAS_MODULES = (
    ("tobkiri_runtime.core_runtime.authority", "core_runtime.authority"),
    ("tobkiri_runtime.core_runtime.authority.approval_attestation", "core_runtime.authority.approval_attestation"),
    ("tobkiri_runtime.core_runtime.authority.approval_challenge_store", "core_runtime.authority.approval_challenge_store"),
    ("tobkiri_runtime.core_runtime.authority.config_lattice", "core_runtime.authority.config_lattice"),
    ("tobkiri_runtime.core_runtime.authority.device_key_registry", "core_runtime.authority.device_key_registry"),
    ("tobkiri_runtime.core_runtime.authority.models", "core_runtime.authority.models"),
    ("tobkiri_runtime.core_runtime.authority.principal", "core_runtime.authority.principal"),
    ("tobkiri_runtime.core_runtime.authority.request_store", "core_runtime.authority.request_store"),
    ("tobkiri_runtime.core_runtime.authority.service", "core_runtime.authority.service"),
    ("tobkiri_runtime.core_runtime.authority.ui_operator", "core_runtime.authority.ui_operator"),
    ("tobkiri_runtime.core_runtime.audit_logger", "core_runtime.audit_logger"),
    ("tobkiri_runtime.core_runtime.network_grant_manager", "core_runtime.network_grant_manager"),
    ("tobkiri_runtime.core_runtime.capability_proxy", "core_runtime.capability_proxy"),
    ("tobkiri_runtime.core_runtime.capability_executor", "core_runtime.capability_executor"),
    ("tobkiri_runtime.core_runtime.crypto_utils", "core_runtime.crypto_utils"),
    ("tobkiri_runtime.core_runtime.docker_capability", "core_runtime.docker_capability"),
    ("tobkiri_runtime.core_runtime.docker_run_builder", "core_runtime.docker_run_builder"),
    ("tobkiri_runtime.core_runtime.function_registry", "core_runtime.function_registry"),
    ("tobkiri_runtime.core_runtime.python_file_executor", "core_runtime.python_file_executor"),
    ("tobkiri_runtime.core_runtime.pack_function_runtime", "core_runtime.pack_function_runtime"),
    ("tobkiri_runtime.core_runtime.pack_importer", "core_runtime.pack_importer"),
    ("tobkiri_runtime.core_runtime.pack_applier", "core_runtime.pack_applier"),
    ("tobkiri_runtime.core_runtime.kernel_handlers_runtime", "core_runtime.kernel_handlers_runtime"),
    ("tobkiri_runtime.core_runtime.flow_loader", "core_runtime.flow_loader"),
    ("tobkiri_runtime.core_runtime.flow_modifier", "core_runtime.flow_modifier"),
    ("tobkiri_runtime.core_runtime.component_lifecycle", "core_runtime.component_lifecycle"),
    ("tobkiri_runtime.core_runtime.unit_executor", "core_runtime.unit_executor"),
    ("tobkiri_runtime.core_runtime.approval_manager", "core_runtime.approval_manager"),
    ("tobkiri_runtime.core_runtime.store_registry", "core_runtime.store_registry"),
    ("tobkiri_runtime.core_runtime.unit_registry", "core_runtime.unit_registry"),
    ("tobkiri_runtime.core_runtime.capability_grant_manager", "core_runtime.capability_grant_manager"),
    ("tobkiri_runtime.core_runtime.unit_trust_store", "core_runtime.unit_trust_store"),
    ("tobkiri_runtime.core_runtime.health", "core_runtime.health"),
    ("tobkiri_runtime.core_runtime.metrics", "core_runtime.metrics"),
    ("tobkiri_runtime.core_runtime.paths", "core_runtime.paths"),
    ("tobkiri_runtime.core_runtime.profiling", "core_runtime.profiling"),
)

_RESTORE_SKIP_PREFIXES = (
    "tests/test_bug_20260305_01_flow_fallback.py",
    "tests/test_wave20a_active_ecosystem_hmac.py",
    "tests/test_wave20b_container_cleanup.py",
    "tests/test_wave21a_host_privilege_hardening.py",
    "tests/test_wave21b_hmac_key_encryption.py",
    "tests/test_wave22c_core_pack_structure.py",
    "tests/test_wave24b_registry_function_scan.py",
    "tests/test_wave25a_function_call_dispatch.py",
    "tests/test_wave27_flow_engine.py",
    "tests/test_function_unification/test_wave27_flow_engine.py",
)

def _should_skip_restore(nodeid: str | None) -> bool:
    return bool(nodeid) and any(prefix in nodeid for prefix in _RESTORE_SKIP_PREFIXES)


def _is_di_phase_test(nodeid: str | None) -> bool:
    return bool(nodeid) and "test_di_phase" in nodeid


def _restore_test_module_mocks(test_module) -> None:
    mock_mods = getattr(test_module, "_mock_mods", None)
    if isinstance(mock_mods, dict):
        for module_name, module in mock_mods.items():
            sys.modules[module_name] = module
            _bind_parent_module(module_name, module)
    for attr_name, module_name in (
        ("_hmac_module", "core_runtime.hmac_key_manager"),
        ("_dummy_hmac", "core_runtime.hmac_key_manager"),
        ("_dummy_audit", "core_runtime.audit_logger"),
        ("_dummy_paths", "core_runtime.paths"),
        ("_dummy_di", "core_runtime.di_container"),
    ):
        module = getattr(test_module, attr_name, None)
        if module is not None:
            sys.modules[module_name] = module
            _bind_parent_module(module_name, module)
    if getattr(test_module, "_mock_container", None) is not None:
        module = types.ModuleType("core_runtime.di_container")
        module.get_container = lambda: test_module._mock_container
        sys.modules["core_runtime.di_container"] = module
        _bind_parent_module("core_runtime.di_container", module)
    if getattr(test_module, "_mock_audit_logger", None) is not None:
        module = types.ModuleType("core_runtime.audit_logger")
        module.get_audit_logger = lambda: test_module._mock_audit_logger
        sys.modules["core_runtime.audit_logger"] = module
        _bind_parent_module("core_runtime.audit_logger", module)
    paths_prefix = getattr(test_module, "_CORE_PACK_ID_PREFIX", None)
    if paths_prefix is None and hasattr(test_module, "_mock_container"):
        paths_prefix = "core_"
    if paths_prefix is not None:
        module = types.ModuleType("core_runtime.paths")
        module.CORE_PACK_ID_PREFIX = paths_prefix
        sys.modules["core_runtime.paths"] = module
        _bind_parent_module("core_runtime.paths", module)
    registry_mod = getattr(test_module, "_registry_mod", None)
    if registry_mod is not None:
        sys.modules["backend_core.ecosystem.registry"] = registry_mod
        _bind_parent_module("backend_core.ecosystem.registry", registry_mod)


def _remove_module_binding(module_name: str) -> None:
    sys.modules.pop(module_name, None)
    if "." not in module_name:
        return
    parent_name, attr_name = module_name.rsplit(".", 1)
    parent = sys.modules.get(parent_name)
    if parent is not None and getattr(parent, attr_name, None) is not None:
        try:
            delattr(parent, attr_name)
        except AttributeError:
            pass


def _restore_real_modules() -> None:
    for _mod_name in _RESTORE_REAL_MODULES:
        try:
            _force_real_import(_mod_name)
        except Exception:
            pass


def pytest_runtest_setup(item):
    _reset_package_roots()
    _restore_real_di_container()
    if _is_di_phase_test(item.nodeid):
        setattr(item.module, "get_container", _REAL_DI_CONTAINER_MODULE.get_container)
    if _should_skip_restore(item.nodeid):
        _restore_test_module_mocks(getattr(item, "module", None))
        return
    for _mod_name in _BIND_ONLY_MODULES:
        try:
            _bind_parent_module(_mod_name)
        except Exception:
            pass
    for _alias_name, _target_name in _ALIAS_MODULES:
        try:
            _sync_alias_module(_alias_name, _target_name)
        except Exception:
            pass


def pytest_collectreport(report):
    _reset_package_roots()
    if _should_skip_restore(getattr(report, "nodeid", None)):
        _restore_real_di_container()
        _restore_real_modules()
        return
    _restore_real_modules()
    for _mod_name in _BIND_ONLY_MODULES:
        try:
            _bind_parent_module(_mod_name)
        except Exception:
            pass
    for _alias_name, _target_name in _ALIAS_MODULES:
        try:
            _sync_alias_module(_alias_name, _target_name)
        except Exception:
            pass
    _restore_real_di_container()


_install_capability_handler_registry_shim()
_REAL_DI_CONTAINER_MODULE = importlib.import_module("core_runtime.di_container")
_REAL_GET_CONTAINER = _REAL_DI_CONTAINER_MODULE.get_container

# ---------------------------------------------------------------------------
# 共通 fixture
# ---------------------------------------------------------------------------
import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _verified_packaged_profile_bundle(tmp_path_factory):
    """Route bootstrap tests through the official packaged-bundle generator."""

    from tests.conformance_support.packaged_profile import (
        build_packaged_profile_bundle,
        create_test_source_provenance,
        inject_packaged_profile_bundle,
    )

    source_bundle = _PROJECT_ROOT / "ecosystem" / "defaultspack" / "v4"
    git_value = os.environ.get("TOBKIRI_PACKAGING_GIT") or shutil.which("git")
    if not git_value:
        raise RuntimeError("an absolute Git executable is required for packaged fixtures")
    git = Path(git_value).resolve()
    git_environment = {
        "GIT_CONFIG_GLOBAL": "NUL" if os.name == "nt" else os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    if os.name == "nt" and os.environ.get("SystemRoot"):
        git_environment["SystemRoot"] = os.environ["SystemRoot"]
    revision = subprocess.run(
        [str(git), "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=_PROJECT_PARENT,
        check=True,
        capture_output=True,
        text=True,
        env=git_environment,
    ).stdout.strip()
    assert len(revision) == 40 and all(
        character in "0123456789abcdef" for character in revision
    )
    source_tree = subprocess.run(
        [str(git), "rev-parse", "--verify", "HEAD^{tree}"],
        cwd=_PROJECT_PARENT,
        check=True,
        capture_output=True,
        text=True,
        env=git_environment,
    ).stdout.strip()
    source_status = subprocess.run(
        [str(git), "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=_PROJECT_PARENT,
        check=True,
        capture_output=True,
        text=True,
        env=git_environment,
    ).stdout.strip()
    assert not source_status, "packaged fixture source must start from a clean checkout"
    injection = pytest.MonkeyPatch()
    fixture_root = tmp_path_factory.mktemp("verified-packaged-profile")
    provenance = create_test_source_provenance(
        _PROJECT_ROOT,
        fixture_root,
        provenance_record={
            "source_commit": revision,
            "source_tree": source_tree,
            "source_clean": True,
        },
    )
    bundle = build_packaged_profile_bundle(
        source_bundle,
        fixture_root,
        source_provenance_file=provenance,
    )
    from core_runtime.bootstrap import profile_capture, runtime

    def provider(_base_dir=None):
        return bundle

    injection.setattr(profile_capture, "_bundle_root", provider)
    injection.setattr(runtime, "_bundle_root", provider)
    inject_packaged_profile_bundle(bundle)
    yield
    inject_packaged_profile_bundle(None)
    injection.undo()


@pytest.fixture(autouse=True)
def _bind_legacy_chat_facade_to_test_owner(request, tmp_path, monkeypatch):
    """Give legacy compatibility tests an explicit canonical conversation owner.

    Production ``ChatStore`` must fail closed when the global owner is absent.
    These older adapter tests exercise UI/tool behavior through the facade, so
    bind them to an isolated owner rather than reviving the removed local
    storage fallback.
    """

    supported_files = {
        "test_defaultspack_coding_approval_followup_replay.py",
        "test_defaultspack_progress_tool.py",
        "test_defaultspack_tool_assist.py",
        "test_defaultspack_tool_eligibility.py",
        "test_defaultspack_browser_state_guardrails.py",
        "test_defaultspack_ui_registry.py",
    }
    if request.path.name not in supported_files:
        yield
        return

    from domain.chat import store as facade
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore

    user_data_root = tmp_path / "conversation_owner"
    snapshot = _defaultspack_v4_snapshot()
    owner = ConversationStore(snapshot.profile_id, user_data_root=user_data_root)
    _bind_v4_snapshot(
        snapshot,
        monkeypatch,
        request,
        user_data_root,
        facade,
    )

    def invoke(contract_id, operation, payload):
        if contract_id == facade.CONVERSATION:
            if operation == "list":
                return owner.snapshot()
            if operation == "get":
                return owner.get(str(payload.get("conversation_id") or ""))
        if contract_id == facade.MESSAGE and operation == "get":
            conversation = owner.get(str(payload.get("conversation_id") or ""))
            return next(
                (
                    message
                    for message in (conversation or {}).get("messages", [])
                    if message.get("id") == payload.get("message_id")
                ),
                None,
            )
        if contract_id == facade.CONVERSATION_MANAGE:
            if operation == "create":
                return owner.create(
                    payload["conversation"],
                    expected_revision=int(payload["expected_revision"]),
                )
            if operation == "update":
                return owner.update(
                    str(payload["conversation_id"]),
                    payload["patch"],
                    expected_conversation_revision=int(
                        payload["expected_conversation_revision"]
                    ),
                )
            if operation == "delete":
                return owner.delete(
                    str(payload["conversation_id"]),
                    expected_conversation_revision=int(
                        payload["expected_conversation_revision"]
                    ),
                )
        if contract_id == facade.MESSAGE_MANAGE:
            if operation == "append":
                return owner.append_message(
                    str(payload["conversation_id"]),
                    payload["message"],
                    expected_conversation_revision=int(
                        payload["expected_conversation_revision"]
                    ),
                )
            if operation in {"update", "delete"}:
                return owner.mutate_message(
                    str(payload["conversation_id"]),
                    str(payload["message_id"]),
                    expected_conversation_revision=int(
                        payload["expected_conversation_revision"]
                    ),
                    patch=payload.get("patch"),
                    delete=operation == "delete",
                )
        raise AssertionError(f"unexpected contract call: {contract_id}/{operation}")

    # Several legacy tests import the facade before pytest restores the
    # canonical package aliases.  Patch every loaded copy of the same source
    # module so a stale module object cannot bypass the isolated owner.
    facade_path = Path(facade.__file__).resolve()
    bound_modules = []
    for module in list(sys.modules.values()):
        module_path = getattr(module, "__file__", None)
        if not module_path:
            continue
        try:
            same_source = Path(module_path).resolve() == facade_path
        except OSError:
            same_source = False
        if same_source and hasattr(module, "_invoke"):
            monkeypatch.setattr(module, "_invoke", invoke)
            bound_modules.append(module)
    if not bound_modules:
        monkeypatch.setattr(facade, "_invoke", invoke)

    # A few compatibility tests imported ``ChatStore`` before another test
    # refreshed the ``domain`` package.  The class then retains its original
    # function globals even when that module is no longer in sys.modules.
    # Bind those detached method globals as well, without changing production
    # fail-closed behavior.
    for value in vars(request.module).values():
        candidates = [value]
        if isinstance(value, type):
            candidates.extend(vars(value).values())
        for candidate in candidates:
            candidate_globals = getattr(candidate, "__globals__", None)
            if not isinstance(candidate_globals, dict):
                continue
            if candidate_globals.get("CONVERSATION") != facade.CONVERSATION:
                continue
            monkeypatch.setitem(candidate_globals, "_invoke", invoke)
    yield


@pytest.fixture(autouse=True)
def _clean_env_vars(monkeypatch):
    """テスト間で環境変数が漏れないようにする"""
    for var in (
        "RUMI_HMAC_ROTATE",
        "RUMI_HMAC_SECRET",
        "RUMI_LOCAL_PACK_MODE",
        "RUMI_HASH_CACHE_TTL_SEC",
        "RUMI_ALLOW_WINDOWS_TCP_FALLBACK",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _reset_singletons(request):
    """各テスト後にグローバルシングルトンをリセットする"""
    skip_restore = _should_skip_restore(request.node.nodeid)
    if request.node.nodeid.endswith(
        "tests/test_function_unification/test_phase_d.py::TestFileDeletion::test_handler_registry_not_importable"
    ):
        sys.modules.pop("core_runtime.capability_handler_registry", None)
        sys.modules.pop("tobkiri_runtime.core_runtime.capability_handler_registry", None)
    yield
    if skip_restore:
        _restore_real_di_container()
        _restore_real_modules()
        for attr_name, module_name in (
            ("_dummy_hmac", "core_runtime.hmac_key_manager"),
            ("_dummy_audit", "core_runtime.audit_logger"),
            ("_dummy_paths", "core_runtime.paths"),
            ("_dummy_di", "core_runtime.di_container"),
        ):
            if getattr(request.module, attr_name, None) is not None:
                _remove_module_binding(module_name)
    _install_capability_handler_registry_shim()

    # ================================================================
    # DI Container (must be first — clears all DI-managed singletons)
    # ================================================================
    try:
        from core_runtime.di_container import reset_container
        reset_container()
    except Exception:
        pass

    try:
        from tobkiri_runtime.core_runtime.di_container import reset_container as _reset_pkg_container
        _reset_pkg_container()
    except Exception:
        pass

    # Startup capability compilation intentionally leaves the resolved
    # profile active for the process lifetime.  A test that exercises that
    # startup path must not lend its authority scope to the next test.
    for module_name in (
        "core_runtime.resolved_profile_scope",
        "tobkiri_runtime.core_runtime.resolved_profile_scope",
    ):
        profile_scope = sys.modules.get(module_name)
        if profile_scope is None:
            continue
        try:
            profile_scope._ACTIVE_PROFILE.set(None)
            profile_scope._PERSISTED_PROFILE_CACHE = None
        except (AttributeError, LookupError):
            pass

    # ================================================================
    # Legacy global variables (cleared for safety, not yet removed)
    # ================================================================

    # network_grant_manager
    try:
        from core_runtime import network_grant_manager as _ngm
        if hasattr(_ngm, '_global_network_grant_manager'):
            _ngm._global_network_grant_manager = None
    except Exception:
        pass
    # hmac_key_manager
    try:
        from core_runtime import hmac_key_manager as _hkm
        _hkm._global_hmac_key_manager = None
    except Exception:
        pass
    # capability_trust_store
    try:
        from core_runtime import capability_trust_store as _cts
        _cts._global_trust_store = None
    except Exception:
        pass
    # store_registry
    try:
        from core_runtime import store_registry as _sr
        if hasattr(_sr, '_global_store_registry'):
            _sr._global_store_registry = None
    except Exception:
        pass
    # vocab_registry
    try:
        from core_runtime import vocab_registry as _vr
        _vr._global_vocab_registry = None
    except Exception:
        pass
    # approval_manager
    try:
        from core_runtime import approval_manager as _am
        _am._global_approval_manager = None
    except Exception:
        pass
    # permission_manager
    try:
        from core_runtime import permission_manager as _pm
        _pm._global_permission_manager = None
    except Exception:
        pass
    # container_orchestrator
    try:
        from core_runtime import container_orchestrator as _co
        if hasattr(_co, '_global_orchestrator'):
            _co._global_orchestrator = None
    except Exception:
        pass
    # host_privilege_manager
    try:
        from core_runtime import host_privilege_manager as _hpm
        if hasattr(_hpm, '_global_privilege_manager'):
            _hpm._global_privilege_manager = None
    except Exception:
        pass
    # flow_composer
    try:
        from core_runtime import flow_composer as _fc
        if hasattr(_fc, '_global_flow_composer'):
            _fc._global_flow_composer = None
    except Exception:
        pass
    # function_alias_registry
    try:
        from core_runtime import function_alias as _fa
        if hasattr(_fa, '_global_function_alias_registry'):
            _fa._global_function_alias_registry = None
    except Exception:
        pass
    # secrets_store
    try:
        from core_runtime import secrets_store as _ss
        if hasattr(_ss, '_global_secrets_store'):
            _ss._global_secrets_store = None
    except Exception:
        pass
    # modifier_loader / modifier_applier
    try:
        from core_runtime import flow_modifier as _fm
        if hasattr(_fm, '_global_modifier_loader'):
            _fm._global_modifier_loader = None
        if hasattr(_fm, '_global_modifier_applier'):
            _fm._global_modifier_applier = None
    except Exception:
        pass

    # ================================================================
    # Wave 5: New DI-managed services (legacy globals cleared)
    # ================================================================

    # pack_api_server
    try:
        from core_runtime import pack_api_server as _pas
        if hasattr(_pas, '_api_server'):
            _pas._api_server = None
    except Exception:
        pass
    # egress_proxy (UDS proxy manager)
    try:
        from core_runtime import egress_proxy as _ep
        if hasattr(_ep, '_global_uds_proxy_manager'):
            _ep._global_uds_proxy_manager = None
        if hasattr(_ep, '_global_egress_proxy'):
            _ep._global_egress_proxy = None
    except Exception:
        pass
    # python_file_executor
    try:
        from core_runtime import python_file_executor as _pfe
        if hasattr(_pfe, '_global_executor'):
            _pfe._global_executor = None
    except Exception:
        pass
    # secure_executor
    try:
        from core_runtime import secure_executor as _se
        if hasattr(_se, '_global_secure_executor'):
            _se._global_secure_executor = None
    except Exception:
        pass
    # lib_executor
    try:
        from core_runtime import lib_executor as _le
        if hasattr(_le, '_global_lib_executor'):
            _le._global_lib_executor = None
    except Exception:
        pass
    # unit_executor
    try:
        from core_runtime import unit_executor as _ue
        if hasattr(_ue, '_global_unit_executor'):
            _ue._global_unit_executor = None
    except Exception:
        pass
    # capability_executor
    try:
        from core_runtime import capability_executor as _ce
        if hasattr(_ce, '_global_executor'):
            _ce._global_executor = None
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Canonical owner bindings for compatibility suites
# ---------------------------------------------------------------------------
# These suites predate the persisted Defaults Profile and now need a scoped
# owner contract in order to exercise their legacy facades.  The fixture below
# is autouse only for this exact allowlist; all other tests, including negative
# owner-absence tests, continue to exercise the production fail-closed path.
_OWNER_MIGRATION_TEST_FILES = frozenset(
    {
        "test_defaultspack_agent_service_plan.py",
        "test_defaultspack_agent_scheduler_approvals.py",
        "test_defaultspack_operations_company.py",
        "test_defaultspack_subagent_compat.py",
        "test_company_workspace.py",
        "test_defaultspack_ambient_trigger_pack.py",
        "test_defaultspack_agent_os_tools.py",
        "test_defaultspack_scheduler.py",
        "test_subagent_team_workspace.py",
    }
)
_WAVE7_OWNER_TEST_FILES = frozenset(
    {
        "test_defaultspack_memory2.py",
        "test_defaultspack_external_submit.py",
        "test_defaultspack_artifact_file.py",
        "test_defaultspack_provider_trace.py",
        "test_defaultspack_kanban_conversation_import.py",
        "test_defaultspack_skill_feedback.py",
    }
)
_CHAT_OWNER_TEST_FILES = frozenset()
_OWNER_BINDING_TEST_FILES = (
    _OWNER_MIGRATION_TEST_FILES | _WAVE7_OWNER_TEST_FILES | _CHAT_OWNER_TEST_FILES
)
_LEGACY_DEFAULTSPACK_EFFECTIVE_PACK_IDS = frozenset(
    {
        "defaultspack",
        "rumi_agent_services_pack",
        "rumi_browser_host_service_pack",
        "rumi_clipboard_host_service_pack",
        "rumi_default_tools_pack",
        "rumi_desktop_host_service_pack",
        "rumi_local_agent_pack",
        "rumi_operations_company_pack",
    }
)

_COMPANY_OWNER_MIGRATION_TEST_FILES = frozenset(
    {
        "test_defaultspack_operations_company.py",
        "test_company_workspace.py",
        "test_subagent_team_workspace.py",
    }
)


@dataclass(frozen=True)
class _V4TestResolvedSnapshot:
    """Expose one verified v4 snapshot to legacy test adapters.

    The runtime's v4 resolver returns protocol documents, while a few
    compatibility consumers still read the small attribute-shaped projection
    that was formerly supplied by the profile fixture.  Keeping that
    projection backed by the resolved v4 documents lets the tests exercise the
    current profile boundary without recreating the removed global owner API.
    """

    resolved: object
    profile_id: str
    effective_pack_set: tuple[str, ...]
    providers: tuple[object, ...]
    effective_permissions: frozenset[str]
    plan_hash: str

    @property
    def profile(self):
        """Return the protocol Profile document captured by the resolver."""
        return self.resolved.profile

    @property
    def lock(self):
        """Return the protocol ProfileLock document captured by the resolver."""
        return self.resolved.lock

    @property
    def plan(self):
        """Return the protocol ResolvedPlan document captured by the resolver."""
        return self.resolved.plan

    def __getattr__(self, name: str):
        """Expose only fields present in the captured v4 documents."""
        for document in (self.resolved.plan, self.resolved.profile, self.resolved.lock):
            if name in document:
                return document[name]
        raise AttributeError(name)


class _V4TestInterfaceRegistry:
    """Minimal test-owned registry for compatibility provider registration."""

    def __init__(self) -> None:
        self._store: dict[str, list[object]] = {}

    def register(self, key: str, value: object, meta=None) -> None:
        """Append one provider record under its interface key."""
        del meta
        self._store.setdefault(key, []).append(value)

    def get(self, key: str, strategy: str = "last"):
        """Return one or all records using the historical test contract."""
        values = list(self._store.get(key, ()))
        if strategy == "all":
            return values
        return values[-1] if values else None

    def list(self) -> dict[str, list[object]]:
        """Return the current test registry projection."""
        return {key: list(values) for key, values in self._store.items()}


@lru_cache(maxsize=1)
def _defaultspack_v4_snapshot() -> _V4TestResolvedSnapshot:
    """Resolve the checked-in Defaults Profile with the Tauri shell binding."""
    from ecosystem.defaultspack.domain.runtime_v4 import (
        BundledCatalog,
        resolve_default_profile,
    )

    from tests.conformance_support.packaged_profile import (
        packaged_profile_bundle_root,
    )

    bundle_root = packaged_profile_bundle_root()
    catalog = BundledCatalog.load(bundle_root)
    source = catalog.profiles["defaults"]
    authority_bindings = {
        "|".join(
            str(edge.get(field) or "")
            for field in (
                "caller_function_id",
                "target_provider_id",
                "contract_id",
                "operation_id",
            )
        ): f"authority-ref:test.default.{index}"
        for index, edge in enumerate(source["requested_edges"])
    }
    resolved = resolve_default_profile(
        catalog,
        "defaults",
        approved_artifact_digests={
            str(manifest["pack"]["artifact_digest"])
            for manifest in catalog.packs.values()
        },
        authority_snapshot_digest="sha256:" + "9" * 64,
        authority_bindings=authority_bindings,
        security_epoch=1,
    )
    assert resolved.profile["shell"]["provider_id"] == "shell.tauri.default"
    assert resolved.profile["requested_edges"][0]["caller_function_id"] == (
        "shell.tauri.default"
    )
    return _V4TestResolvedSnapshot(
        resolved=resolved,
        profile_id=str(resolved.profile["profile_id"]),
        effective_pack_set=tuple(
            item["identity"] for item in resolved.lock["effective_set"]
        ),
        providers=(),
        effective_permissions=frozenset(),
        plan_hash=str(resolved.plan["plan_digest"]),
    )


def _bind_v4_snapshot(
    snapshot: _V4TestResolvedSnapshot,
    monkeypatch,
    request,
    user_data_root: Path,
    chat_store_module=None,
) -> None:
    """Bind active v4 identity and one profile-scoped facade artifact root."""
    from core_runtime import resolved_profile_scope

    token = resolved_profile_scope.activate_resolved_profile(snapshot)
    request.addfinalizer(lambda: resolved_profile_scope.restore_resolved_profile(token))
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data_root))
    compatibility_pack_ids = frozenset(
        set(snapshot.effective_pack_set) | _LEGACY_DEFAULTSPACK_EFFECTIVE_PACK_IDS
    )
    monkeypatch.setattr(
        resolved_profile_scope,
        "effective_pack_ids",
        lambda: compatibility_pack_ids,
    )
    if chat_store_module is not None:
        artifact_root = (
            user_data_root
            / "compatibility"
            / "conversation_artifacts"
            / snapshot.profile_id
        )
        monkeypatch.setattr(chat_store_module, "USER_DATA_DIR", user_data_root)
        monkeypatch.setattr(
            chat_store_module.ChatStore,
            "_artifact_root",
            staticmethod(lambda root=artifact_root: root),
        )

    for module in tuple(sys.modules.values()):
        if module is None:
            continue
        try:
            if hasattr(module, "active_resolved_profile"):
                monkeypatch.setattr(
                    module,
                    "active_resolved_profile",
                    lambda: snapshot,
                )
            if hasattr(module, "effective_pack_ids"):
                monkeypatch.setattr(
                    module,
                    "effective_pack_ids",
                    lambda: compatibility_pack_ids,
                )
        except (AttributeError, TypeError):
            continue


def _owner_contract_invoker(owner, facade, expected_profile_id=None):
    """Return the exact conversation owner contract test adapter."""

    def invoke(contract_id, operation, payload):
        if expected_profile_id is not None:
            request_profile = str(payload.get("profile_id") or expected_profile_id)
            if request_profile != expected_profile_id:
                raise AssertionError("conversation request used an unexpected profile")

        if contract_id == facade.CONVERSATION:
            if operation == "list":
                return owner.snapshot()
            if operation == "get":
                return owner.get(str(payload.get("conversation_id") or ""))

        if contract_id == facade.CONVERSATION_MANAGE:
            conversation_id = str(payload.get("conversation_id") or "")
            expected = int(
                payload.get("expected_conversation_revision")
                or payload.get("expected_revision")
                or 0
            )
            if operation == "create":
                return owner.create(
                    payload["conversation"],
                    expected_revision=int(payload.get("expected_revision") or 0),
                )
            if operation == "update":
                return owner.update(
                    conversation_id,
                    payload.get("patch") or {},
                    expected_conversation_revision=expected,
                )
            if operation == "delete":
                return owner.delete(
                    conversation_id,
                    expected_conversation_revision=expected,
                )

        if contract_id == facade.MESSAGE:
            conversation = owner.get(str(payload.get("conversation_id") or ""))
            if operation == "list":
                if conversation is None:
                    return None
                return {
                    "conversation_id": conversation["id"],
                    "conversation_revision": conversation["conversation_revision"],
                    "messages": list(conversation.get("messages") or []),
                }
            if operation == "get":
                return next(
                    (
                        item
                        for item in (conversation or {}).get("messages", [])
                        if item.get("id") == payload.get("message_id")
                    ),
                    None,
                )

        if contract_id == facade.MESSAGE_MANAGE:
            conversation_id = str(payload.get("conversation_id") or "")
            expected = int(payload.get("expected_conversation_revision") or 0)
            if operation == "append":
                return owner.append_message(
                    conversation_id,
                    payload["message"],
                    expected_conversation_revision=expected,
                )
            if operation == "update":
                return owner.mutate_message(
                    conversation_id,
                    str(payload.get("message_id") or ""),
                    expected_conversation_revision=expected,
                    patch=payload.get("patch") or {},
                )
            if operation == "delete":
                return owner.mutate_message(
                    conversation_id,
                    str(payload.get("message_id") or ""),
                    expected_conversation_revision=expected,
                    delete=True,
                )
            if operation == "replace":
                return owner.replace_messages(
                    conversation_id,
                    payload.get("messages") or [],
                    expected_conversation_revision=expected,
                )

        raise AssertionError(f"unexpected owner contract call: {contract_id}/{operation}")

    return invoke


@pytest.fixture
def defaultspack_conversation_owner(request, monkeypatch, tmp_path):
    """Bind one test explicitly to an isolated canonical conversation owner."""
    from domain.chat import store as chat_store_module
    from domain.tool.registry import ToolRegistry
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore

    user_data_root = tmp_path / "user_data"
    snapshot = _defaultspack_v4_snapshot()
    owner = ConversationStore(snapshot.profile_id, user_data_root=user_data_root)

    monkeypatch.setenv("RUMI_TEST_CONVERSATION_OWNER_ROOT", str(user_data_root))
    _bind_v4_snapshot(
        snapshot,
        monkeypatch,
        request,
        user_data_root,
        chat_store_module,
    )
    ToolRegistry._instance = None

    chat_invoke = _owner_contract_invoker(
        owner,
        chat_store_module,
        expected_profile_id=snapshot.profile_id,
    )
    monkeypatch.setattr(chat_store_module, "_invoke", chat_invoke)
    try:
        yield owner
    finally:
        ToolRegistry._instance = None


@pytest.fixture
def defaultspack_v4_tool_dispatch(defaultspack_conversation_owner, monkeypatch):
    """Bind a test-only v4 tool-definition session to the active Defaults Profile.

    Production intentionally fails closed when no captured v4 dispatch session exists.
    These compatibility-heavy chat tests still exercise the checked-in legacy tool
    fixtures, so project those fixtures through the finite v4 definition contract
    instead of reviving a production registry fallback.
    """

    del defaultspack_conversation_owner

    from core_runtime.di_container import get_container
    from core_runtime.global_contract_dispatch import GlobalContractUnavailable
    from ecosystem.rumi_default_tool_projection_pack.runtime import projection
    from ecosystem.rumi_default_tool_projection_pack.runtime.projection import (
        create_source_operation,
    )
    from domain.tool.registry import ToolRegistry as RuntimeToolRegistry

    # The projection pack is imported through its installed package name in
    # production, while compatibility tests may import the same source as the
    # top-level ``domain`` package.  Dynamic MCP tools must enter the same
    # canonical registry that the MCP block mutates; otherwise the test
    # dispatch session would silently project a second, stale registry.
    monkeypatch.setattr(projection, "ToolRegistry", RuntimeToolRegistry)

    snapshot = _defaultspack_v4_snapshot()
    definition_contract = "rumi.resource.tool.definition.v1"

    class _ToolDefinitionDispatchSession:
        profile_id = snapshot.profile_id
        plan_digest = snapshot.plan_hash

        @staticmethod
        def _catalog():
            return create_source_operation(None)("list", {})

        def invoke(
            self,
            contract_id,
            operation_id,
            payload,
            *,
            version_range=">=1,<2",
        ):
            del version_range
            if contract_id != definition_contract:
                raise GlobalContractUnavailable(
                    f"test v4 tool dispatch does not provide {contract_id}"
                )
            request_profile_id = str(payload.get("profile_id") or self.profile_id)
            if request_profile_id != self.profile_id:
                raise GlobalContractUnavailable(
                    "test v4 tool dispatch profile identity mismatch"
                )

            source = self._catalog()
            definitions = [
                dict(item)
                for item in source.get("definitions", [])
                if isinstance(item, dict)
            ]
            aliases = {
                str(alias): str(target)
                for alias, target in dict(source.get("aliases") or {}).items()
            }
            if operation_id in {"list", "catalog"}:
                return {
                    "profile_id": self.profile_id,
                    "revision": 0,
                    "definitions": definitions,
                    "aliases": aliases,
                    "migration": None,
                }
            if operation_id in {"get", "resolve"}:
                requested = str(payload.get("tool_id") or "").strip()
                resolved = aliases.get(requested, requested)
                definition = next(
                    (
                        item
                        for item in definitions
                        if str(item.get("tool_id") or "") == resolved
                    ),
                    None,
                )
                return {
                    "requested_tool_id": requested,
                    "resolved_tool_id": resolved,
                    "aliased": requested != resolved,
                    "definition": definition,
                    "registry_revision": 0,
                }
            raise GlobalContractUnavailable(
                f"test v4 tool definition operation is unavailable: {operation_id}"
            )

        def provider_metadata(self, contract_id):
            if contract_id == definition_contract:
                return ()
            raise GlobalContractUnavailable(
                f"test v4 tool dispatch does not provide {contract_id}"
            )

    container = get_container()
    marker = object()
    previous = container._instances.get("v4_dispatch_session", marker)
    session = _ToolDefinitionDispatchSession()
    container.set_instance("v4_dispatch_session", session)
    try:
        yield session
    finally:
        if previous is marker:
            container._instances.pop("v4_dispatch_session", None)
        else:
            container._instances["v4_dispatch_session"] = previous


@pytest.fixture
def defaultspack_capability_plan_context():
    """Build a signed detached CapabilityPlan for selected tool-contract tests."""

    from core_runtime.capability_plan import canonical_capability_plan_digest
    from domain.tool.registry import ToolRegistry

    def build(*tool_ids, **context):
        registry = ToolRegistry()
        tool_overrides = context.pop("_tool_definitions", {})
        tool_overrides = tool_overrides if isinstance(tool_overrides, dict) else {}
        selected_ids = [str(tool_id).strip() for tool_id in tool_ids if str(tool_id).strip()]
        if not selected_ids:
            raise AssertionError("at least one selected tool definition is required")
        schema_hashes = {}
        for tool_id in selected_ids:
            tool = tool_overrides.get(tool_id) or registry.get(tool_id)
            if not isinstance(tool, dict):
                raise AssertionError(f"missing selected tool definition: {tool_id}")
            schema = tool.get("schema")
            if not isinstance(schema, dict):
                contract = tool.get("contract")
                schema = (
                    contract.get("input_schema")
                    if isinstance(contract, dict)
                    and isinstance(contract.get("input_schema"), dict)
                    else {}
                )
            schema_hashes[tool_id] = hashlib.sha256(
                json.dumps(
                    schema,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
        plan = {
            "schema_version": "tobkiri.capability-plan/v1",
            "plan_id": "plan_test_" + "_".join(selected_ids),
            "registry_revision": "registry_test",
            "effective_capabilities": [],
            "provider_selections": {},
            "tools": {
                "attached": selected_ids,
                "schema_hashes": schema_hashes,
            },
        }
        plan["digest"] = canonical_capability_plan_digest(plan)
        return {"principal_id": "defaultspack", "capability_plan": plan, **context}

    return build


@pytest.fixture
def defaultspack_active_profile(request, monkeypatch, tmp_path):
    """Bind v4 gateway compatibility tests to one active selected profile."""

    from types import SimpleNamespace

    from core_runtime.di_container import get_container
    from domain.chat import store as chat_store_module
    from domain.tool.registry import ToolRegistry
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
    from ecosystem.rumi_turn_runtime_pack.runtime.turns import (
        create_turn_action,
        create_turn_resource,
    )

    container = get_container()
    marker = object()
    previous_registry = container._instances.get("interface_registry", marker)
    registry = (
        previous_registry
        if previous_registry is not marker
        else _V4TestInterfaceRegistry()
    )
    if previous_registry is marker:
        container.set_instance("interface_registry", registry)
    previous_store = {
        key: list(values) for key, values in registry._store.items()
    }
    content_hash = "sha256:0eefb8b32c083309abf0d20688d7769b8533f726f516891de0fead09bfa792ed"
    turn_resource = create_turn_resource(None)
    turn_action = create_turn_action(None)
    registry.register(
        "global_contract.provider.rumi.resource.turn.v1",
        {
            "contract_id": "rumi.resource.turn.v1",
            "provider_instance_id": "turn-runtime.resource",
            "source_pack_id": "rumi_turn_runtime_pack",
            "content_hash": content_hash,
            "required_capabilities": ["turn.read"],
            "operation": turn_resource,
        },
    )
    registry.register(
        "global_contract.provider.rumi.action.turn.lifecycle.v1",
        {
            "contract_id": "rumi.action.turn.lifecycle.v1",
            "provider_instance_id": "turn-runtime.lifecycle",
            "source_pack_id": "rumi_turn_runtime_pack",
            "content_hash": content_hash,
            "required_capabilities": ["turn.manage"],
            "operation": turn_action,
        },
    )

    base = _defaultspack_v4_snapshot()
    plan = _V4TestResolvedSnapshot(
        resolved=base.resolved,
        profile_id=base.profile_id,
        effective_pack_set=tuple(
            sorted(set(base.effective_pack_set) | {"rumi_turn_runtime_pack"})
        ),
        providers=(
            SimpleNamespace(
                contract_id="rumi.resource.turn.v1",
                provider_instance_id="turn-runtime.resource",
                source_pack_id="rumi_turn_runtime_pack",
                content_hash=content_hash,
            ),
            SimpleNamespace(
                contract_id="rumi.action.turn.lifecycle.v1",
                provider_instance_id="turn-runtime.lifecycle",
                source_pack_id="rumi_turn_runtime_pack",
                content_hash=content_hash,
            ),
        ),
        effective_permissions=frozenset({"turn.read", "turn.manage"}),
        plan_hash=base.plan_hash,
    )
    owner = ConversationStore(plan.profile_id, user_data_root=tmp_path)
    monkeypatch.setenv("RUMI_TEST_CONVERSATION_OWNER_ROOT", str(tmp_path))
    _bind_v4_snapshot(plan, monkeypatch, request, tmp_path, chat_store_module)
    monkeypatch.setattr(
        chat_store_module,
        "_invoke",
        _owner_contract_invoker(
            owner,
            chat_store_module,
            expected_profile_id=plan.profile_id,
        ),
    )
    ToolRegistry._instance = None
    try:
        yield plan
    finally:
        registry._store.clear()
        registry._store.update(previous_store)
        if previous_registry is marker:
            container._instances.pop("interface_registry", None)
        ToolRegistry._instance = None


def _memory_contract_invoker(owner, facade):
    """Return the exact memory owner contract test adapter."""

    def invoke(contract_id, operation, payload):
        if contract_id == facade.RESOURCE:
            if operation == "snapshot":
                return owner.snapshot()
            if operation == "get":
                return owner.get(str(payload.get("memory_id") or ""))
            if operation == "search":
                return owner.search(
                    str(payload.get("query") or ""),
                    limit=int(payload.get("limit") or 8),
                )
        if contract_id == facade.MANAGE:
            expected = int(payload.get("expected_revision") or 0)
            if operation == "put":
                return owner.put(payload["item"], expected_revision=expected)
            if operation == "delete":
                return owner.delete(
                    str(payload.get("memory_id") or ""),
                    expected_revision=expected,
                )
        raise AssertionError(f"unexpected memory owner call: {contract_id}/{operation}")

    return invoke


def _wave7_conversation_owner():
    """Create the env-selected test owner without changing production behavior."""
    raw_path = os.environ.get("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", "").strip()
    if not raw_path:
        return None
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore

    path = Path(raw_path).expanduser()
    owner = ConversationStore(
        _defaultspack_v4_snapshot().profile_id,
        user_data_root=path.parent,
    )
    owner.root = path.parent
    owner.path = path
    owner.backup_root = path.parent / "migration_backups"
    owner.lock_root = path.parent / "locks"
    return owner


def _wave7_memory_owner():
    """Create the env-selected memory owner without changing production behavior."""
    raw_root = os.environ.get("RUMI_DEFAULTSPACK_MEMORY2_DIR", "").strip()
    if not raw_root:
        return None
    from ecosystem.rumi_memory_store_pack.runtime.store import MemoryStore

    root = Path(raw_root).expanduser()
    owner = MemoryStore("default", user_data_root=root)
    owner.root = root
    owner.path = root / "memories.json"
    owner.backup_root = root / "migration_backups"
    owner.lock_root = root / "locks"
    return owner


@pytest.fixture
def wave7_owner_bindings(request, monkeypatch, tmp_path):
    """Bind explicit Wave 7 compatibility tests to their selected owners."""
    from domain.chat import store as chat_facade
    from domain.memory import store as memory_facade

    _bind_v4_snapshot(
        _defaultspack_v4_snapshot(),
        monkeypatch,
        request,
        tmp_path / "wave7_user_data",
        chat_facade,
    )

    original_chat_invoke = chat_facade._invoke
    original_memory_invoke = memory_facade._invoke

    def chat_invoke(contract_id, operation, payload):
        owner = _wave7_conversation_owner()
        if owner is None:
            return original_chat_invoke(contract_id, operation, payload)
        return _owner_contract_invoker(owner, chat_facade)(
            contract_id,
            operation,
            payload,
        )

    def memory_invoke(contract_id, operation, payload):
        owner = _wave7_memory_owner()
        if owner is None:
            return original_memory_invoke(contract_id, operation, payload)
        return _memory_contract_invoker(owner, memory_facade)(
            contract_id,
            operation,
            payload,
        )

    monkeypatch.setattr(chat_facade, "_invoke", chat_invoke)
    monkeypatch.setattr(memory_facade, "_invoke", memory_invoke)


def _patch_imported_facade_globals(
    test_module,
    monkeypatch,
    *,
    chat_invoke=None,
    memory_invoke=None,
):
    """Patch stale facade module globals retained by collected test imports.

    Some compatibility tests import facade classes at collection time while
    another test module intentionally replaces the top-level ``domain``
    package.  Patching only the current ``sys.modules`` entry would leave the
    already-bound class methods on the old module fail-closed.  Patch those
    method globals too, but only for the two compatibility facade modules.
    """
    seen_globals: set[int] = set()
    for value in vars(test_module).values():
        candidates = [value]
        if isinstance(value, type):
            candidates.extend(vars(value).values())
        for candidate in candidates:
            global_namespace = getattr(candidate, "__globals__", None)
            if not isinstance(global_namespace, dict):
                continue
            namespace_id = id(global_namespace)
            if namespace_id in seen_globals:
                continue
            module_name = str(global_namespace.get("__name__") or "")
            replacement = None
            if chat_invoke is not None and module_name.endswith("domain.chat.store"):
                replacement = chat_invoke
            elif memory_invoke is not None and module_name.endswith("domain.memory.store"):
                replacement = memory_invoke
            if replacement is None:
                continue
            seen_globals.add(namespace_id)
            monkeypatch.setitem(global_namespace, "_invoke", replacement)


def _install_company_owner_contract_test_double(tmp_path, monkeypatch):
    """Bind legacy Company routes to the canonical profile-scoped owner."""
    from ecosystem.rumi_company_state_store_pack.runtime.store import (
        CompanyStateStore,
        _arguments,
    )
    from domain.company import contract_facade
    from domain.tool_policy.internal_context import mark_tool_server_approval_context

    owner = CompanyStateStore("default", root=tmp_path)
    receipts = set()

    def invoke(contract_id, operation, payload):
        if contract_id == contract_facade.AUTHORITY:
            if operation in {"authorize", "redeem"}:
                receipt = str(payload.get("receipt") or "")
                if operation == "redeem" and receipt not in receipts:
                    return {"authorized": False, "reason": "unknown test receipt"}
                issued = "test-company-receipt"
                receipts.add(issued)
                return {"authorized": True, "receipt": issued}
            raise AssertionError(f"unexpected company authority call: {operation}")
        if contract_id == contract_facade.RESOURCE:
            if operation == "list":
                return owner.snapshot()
            if operation == "get":
                return owner.get(str(payload.get("company_id") or ""))
            raise AssertionError(f"unexpected company resource call: {operation}")
        if contract_id == contract_facade.ACTION:
            receipt = str(payload.get("authority_receipt") or "")
            if receipt not in receipts:
                raise PermissionError("company owner receipt is unavailable")
            return owner.apply(operation, _arguments(operation, payload))
        raise AssertionError(f"unexpected company contract call: {contract_id}/{operation}")

    monkeypatch.setattr(contract_facade, "_invoke", invoke)
    monkeypatch.setattr(
        contract_facade,
        "_profile_id",
        lambda: "default",
    )
    original_init = contract_facade.CompanyContractFacade.__init__

    def init_with_server_context(self, input_data, context):
        trusted_context = mark_tool_server_approval_context(dict(context or {}))
        original_init(self, input_data, trusted_context)

    monkeypatch.setattr(
        contract_facade.CompanyContractFacade,
        "__init__",
        init_with_server_context,
    )


@pytest.fixture(autouse=True)
def defaultspack_owner_bindings(request, monkeypatch, tmp_path):
    """Bind only allowlisted compatibility tests to canonical owner doubles.

    The fixture is intentionally strict-limited: it is a no-op for every
    other test, so owner absence, missing Capability Plans, and permission
    denial remain fail-closed in both production and negative tests.
    """
    file_name = Path(request.node.fspath).name
    if file_name not in _OWNER_BINDING_TEST_FILES:
        yield None
        return

    from domain.chat import store as chat_facade

    if file_name in _CHAT_OWNER_TEST_FILES:
        from ecosystem.rumi_conversation_store_pack.runtime.store import (
            ConversationStore,
        )

        user_data_root = tmp_path / "user_data"
        snapshot = _defaultspack_v4_snapshot()
        owner = ConversationStore(snapshot.profile_id, user_data_root=user_data_root)
        monkeypatch.setenv("RUMI_TEST_CONVERSATION_OWNER_ROOT", str(user_data_root))
        _bind_v4_snapshot(
            snapshot,
            monkeypatch,
            request,
            user_data_root,
            chat_facade,
        )
        chat_invoke = _owner_contract_invoker(
            owner, chat_facade, expected_profile_id=snapshot.profile_id
        )
        monkeypatch.setattr(chat_facade, "_invoke", chat_invoke)
        _patch_imported_facade_globals(
            request.module,
            monkeypatch,
            chat_invoke=chat_invoke,
        )
        yield owner
        return

    if file_name in _OWNER_MIGRATION_TEST_FILES:
        from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore

        snapshot = _defaultspack_v4_snapshot()
        owner = ConversationStore(snapshot.profile_id, user_data_root=tmp_path)
        monkeypatch.setenv("RUMI_TEST_CONVERSATION_OWNER_ROOT", str(tmp_path))
        _bind_v4_snapshot(snapshot, monkeypatch, request, tmp_path, chat_facade)
        chat_invoke = _owner_contract_invoker(
            owner,
            chat_facade,
            expected_profile_id=snapshot.profile_id,
        )
        monkeypatch.setattr(chat_facade, "_invoke", chat_invoke)
        _patch_imported_facade_globals(
            request.module,
            monkeypatch,
            chat_invoke=chat_invoke,
        )

        def selected_pack_ids():
            return _LEGACY_DEFAULTSPACK_EFFECTIVE_PACK_IDS

        import core_runtime.resolved_profile_scope as profile_scope

        monkeypatch.setattr(profile_scope, "effective_pack_ids", selected_pack_ids)
        for module in tuple(sys.modules.values()):
            if module is None or not hasattr(module, "effective_pack_ids"):
                continue
            try:
                monkeypatch.setattr(module, "effective_pack_ids", selected_pack_ids)
            except (AttributeError, TypeError):
                continue
        if file_name in _COMPANY_OWNER_MIGRATION_TEST_FILES:
            _install_company_owner_contract_test_double(
                tmp_path,
                monkeypatch,
            )
        yield owner
        return

    original_chat_invoke = chat_facade._invoke
    from domain.memory import store as memory_facade

    original_memory_invoke = memory_facade._invoke
    snapshot = _defaultspack_v4_snapshot()
    _bind_v4_snapshot(snapshot, monkeypatch, request, tmp_path, chat_facade)

    def chat_invoke(contract_id, operation, payload):
        owner = _wave7_conversation_owner()
        if owner is None:
            return original_chat_invoke(contract_id, operation, payload)
        return _owner_contract_invoker(owner, chat_facade, snapshot.profile_id)(
            contract_id, operation, payload
        )

    def memory_invoke(contract_id, operation, payload):
        owner = _wave7_memory_owner()
        if owner is None:
            return original_memory_invoke(contract_id, operation, payload)
        return _memory_contract_invoker(owner, memory_facade)(
            contract_id, operation, payload
        )

    monkeypatch.setattr(chat_facade, "_invoke", chat_invoke)
    monkeypatch.setattr(memory_facade, "_invoke", memory_invoke)
    _patch_imported_facade_globals(
        request.module,
        monkeypatch,
        chat_invoke=chat_invoke,
        memory_invoke=memory_invoke,
    )
    yield None


@pytest.fixture
def provider_model_catalog_selected(monkeypatch):
    """Select the bundled model-catalog owner for provider compatibility tests."""

    from core_runtime import resolved_profile_scope
    from domain.components import registry as component_registry

    selected = frozenset({"rumi_model_catalog_pack"})
    monkeypatch.setattr(
        resolved_profile_scope,
        "effective_pack_ids",
        lambda: selected,
    )
    monkeypatch.setattr(component_registry, "effective_pack_ids", lambda: selected)
    component_registry.get_domain_component_registry(force_reload=True)
    yield
    component_registry.get_domain_component_registry(force_reload=True)


@pytest.fixture
def defaultspack_component_catalog_selected(monkeypatch):
    """Select the finite first-party catalog for legacy component unit tests."""

    from core_runtime import resolved_profile_scope
    from domain.components import registry as component_registry
    from domain.tool import registry as tool_registry

    selected = _LEGACY_DEFAULTSPACK_EFFECTIVE_PACK_IDS
    monkeypatch.setattr(
        resolved_profile_scope,
        "effective_pack_ids",
        lambda: selected,
    )
    monkeypatch.setattr(component_registry, "effective_pack_ids", lambda: selected)
    monkeypatch.setattr(tool_registry, "effective_pack_ids", lambda: selected)
    component_registry.get_domain_component_registry(force_reload=True)
    tool_registry.ToolRegistry._instance = None
    yield
    tool_registry.ToolRegistry._instance = None
    component_registry.get_domain_component_registry(force_reload=True)


@pytest.fixture
def configured_cloud_provider(monkeypatch, tmp_path):
    """Configure broker-backed cloud credentials under an explicit Host bind."""

    from core_runtime.host_contract import bind_host_contract
    from domain.ai_client.api_key_store import set_provider_api_key

    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_SECRETS_DIR",
        str(tmp_path / "provider-secrets"),
    )

    def configure(provider_id: str, value: str) -> None:
        result = set_provider_api_key(provider_id, value)
        assert result["success"] is True

    with bind_host_contract(
        {
            "profile_id": "default",
            "values": {"cloud_providers_enabled": "true"},
        }
    ):
        yield configure
