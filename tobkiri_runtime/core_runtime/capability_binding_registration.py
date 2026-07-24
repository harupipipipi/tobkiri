"""Manifest-driven Capability Graph binding registration."""

from __future__ import annotations

import importlib
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .interface_registry import InterfaceRegistry
from .global_contracts.manifest import load_manifest
from .global_contract_dispatch import (
    GlobalContractClient,
    GlobalContractInvocationError,
)
from .pack_artifact_integrity import verify_declared_artifacts
from .paths import (
    CORE_PACK_DIR,
    CORE_PACK_ID_PREFIX,
    ECOSYSTEM_DIR,
    PackLocation,
    discover_pack_locations,
    resolve_pack_locations,
)


TRUSTED_BUILTIN_PACK_IDS = {
    "defaultspack",
    "rumi_default_tools_pack",
    "rumi_host_capabilities_pack",
}


@dataclass
class CapabilityBindingRegistrationResult:
    ok: bool = True
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    registered: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "registered": list(self.registered),
            "skipped": list(self.skipped),
            "diagnostics": list(self.diagnostics),
        }


def register_pack_binding_handlers(
    *,
    interface_registry: InterfaceRegistry,
    approval_manager: Any = None,
    ecosystem_dir: Optional[str] = None,
    registry: Any = None,
    effective_pack_ids: Optional[Iterable[str]] = None,
) -> CapabilityBindingRegistrationResult:
    """Register explicit pack-owned binding handlers from approved packs."""
    result = CapabilityBindingRegistrationResult()
    effective = (
        frozenset(str(item) for item in effective_pack_ids)
        if effective_pack_ids is not None
        else None
    )
    for pack_id, pack_location in _iter_pack_locations(
        registry,
        ecosystem_dir,
        effective,
    ):
        if effective is not None and pack_id not in effective:
            continue
        ok, reason = _is_pack_approved(approval_manager, pack_id)
        if not ok:
            result.skipped.append(pack_id)
            result.diagnostics.append(
                _diagnostic(
                    "warning",
                    "binding_registration_pack_skipped_unapproved",
                    f"Pack '{pack_id}' binding registration skipped: {reason}",
                    pack_id=pack_id,
                    reason=reason,
                )
            )
            continue

        process_handled, process_registered = _register_v3_contract_bindings(
            pack_id,
            pack_location,
            interface_registry,
            result,
        )
        if process_handled:
            if process_registered:
                result.registered.append(pack_id)
            else:
                result.skipped.append(pack_id)
            continue

        manifest = _read_manifest(pack_location.ecosystem_json_path, result, pack_id)
        register_path = _register_path_from_manifest(manifest)
        if not register_path:
            result.skipped.append(pack_id)
            continue
        if not _register_path_allowed(register_path, pack_id):
            result.ok = False
            result.skipped.append(pack_id)
            result.diagnostics.append(
                _diagnostic(
                    "error",
                    "binding_registration_path_not_allowed",
                    f"Pack '{pack_id}' cannot register binding path '{register_path}'",
                    pack_id=pack_id,
                    register=register_path,
                )
            )
            continue
        ok, reason = _host_registration_allowed(pack_id, pack_location, manifest)
        if not ok:
            result.skipped.append(pack_id)
            result.diagnostics.append(
                _diagnostic(
                    "warning",
                    "binding_registration_host_execution_required",
                    f"Pack '{pack_id}' binding registration skipped: {reason}",
                    pack_id=pack_id,
                    register=register_path,
                    reason=reason,
                )
            )
            continue

        try:
            registered = _call_register_function(
                register_path,
                pack_location=pack_location,
                interface_registry=interface_registry,
            )
        except Exception as exc:
            result.ok = False
            result.skipped.append(pack_id)
            result.diagnostics.append(
                _diagnostic(
                    "error",
                    "binding_registration_failed",
                    f"Pack '{pack_id}' binding registration failed: {exc}",
                    pack_id=pack_id,
                    register=register_path,
                )
            )
            continue

        result.registered.append(pack_id)
        result.diagnostics.append(
            _diagnostic(
                "info",
                "binding_registration_registered",
                f"Pack '{pack_id}' binding handlers are registered",
                pack_id=pack_id,
                register=register_path,
                registered=registered,
            )
        )
    return result


def _register_v3_contract_bindings(
    pack_id: str,
    pack_location: PackLocation,
    interface_registry: InterfaceRegistry,
    result: CapabilityBindingRegistrationResult,
) -> tuple[bool, bool]:
    """Activate verified v3 providers only after pack approval."""
    manifest_path = pack_location.pack_subdir / "rumi.pack.v3.json"
    if not manifest_path.is_file():
        return False, False
    loaded = load_manifest(manifest_path)
    if not loaded.ok or not isinstance(loaded.value, dict):
        result.ok = False
        result.diagnostics.append(
            _diagnostic(
                "error",
                "v3_process_manifest_invalid",
                "; ".join(loaded.diagnostics),
                pack_id=pack_id,
            )
        )
        return True, False
    manifest = loaded.value
    ecosystem_manifest = _read_manifest(
        pack_location.ecosystem_json_path,
        result,
        pack_id,
    )
    host_allowed, host_reason = _host_registration_allowed(
        pack_id,
        pack_location,
        ecosystem_manifest,
    )
    if not host_allowed:
        result.diagnostics.append(
            _diagnostic(
                "warning",
                "v3_process_host_execution_required",
                f"Pack process activation skipped: {host_reason}",
                pack_id=pack_id,
            )
        )
        return True, False
    integrity_ok, integrity_diagnostics = verify_declared_artifacts(
        pack_location.pack_subdir,
        ecosystem_manifest,
    )
    if not integrity_ok:
        result.ok = False
        result.diagnostics.append(
            _diagnostic(
                "error",
                "v3_pack_artifact_integrity_failed",
                "; ".join(integrity_diagnostics),
                pack_id=pack_id,
            )
        )
        return True, False
    entrypoints = {
        str(item.get("contract_id") or ""): item
        for item in manifest.get("entrypoints", [])
        if isinstance(item, dict) and item.get("loader") in {"process", "python"}
    }
    required_contract_ids = frozenset(
        str(item.get("id") or "")
        for item in manifest.get("contracts", {}).get("requires", [])
        if isinstance(item, dict) and item.get("id")
    )
    providers = manifest.get("contracts", {}).get("provides", [])
    registered = 0
    expected = 0
    for provider in providers if isinstance(providers, list) else []:
        if not isinstance(provider, dict):
            continue
        contract_id = str(provider.get("id") or "")
        entrypoint = entrypoints.get(contract_id)
        if entrypoint is None:
            continue
        expected += 1
        module = str(entrypoint.get("module") or "").strip()
        if not module or not _module_owned_by_pack(module, pack_id):
            result.ok = False
            result.diagnostics.append(
                _diagnostic(
                    "error",
                    "v3_process_module_not_owned",
                    "Process entrypoint module is outside its owner namespace",
                    pack_id=pack_id,
                    contract_id=contract_id,
                )
            )
            continue
        module_path = _process_module_path(module, pack_location)
        expected_artifact_hash = str(entrypoint.get("artifact_hash") or "")
        if (
            module_path is None
            or not module_path.is_file()
            or _sha256(module_path) != expected_artifact_hash
        ):
            result.ok = False
            result.diagnostics.append(
                _diagnostic(
                    "error",
                    "v3_process_artifact_hash_mismatch",
                    "Process entrypoint artifact hash does not match",
                    pack_id=pack_id,
                    contract_id=contract_id,
                )
            )
            continue
        loader = str(entrypoint.get("loader") or "")
        if loader == "process":
            operation = _ProcessContractOperation(
                module=module,
                pack_location=pack_location,
            )
        else:
            try:
                operation = _load_python_contract_operation(
                    module=module,
                    symbol=str(entrypoint.get("symbol") or ""),
                    pack_location=pack_location,
                    client=GlobalContractClient(
                        interface_registry=interface_registry,
                        allowed_contract_ids=required_contract_ids,
                        consumer_pack_id=pack_id,
                    ),
                )
            except Exception as exc:
                result.ok = False
                result.diagnostics.append(
                    _diagnostic(
                        "error",
                        "v3_python_activation_failed",
                        f"Python contract activation failed: {exc}",
                        pack_id=pack_id,
                        contract_id=contract_id,
                    )
                )
                continue
        descriptor = {
            "contract_id": contract_id,
            "version": str(provider.get("version") or ""),
            "provider_instance_id": str(
                provider.get("provider_instance_id") or ""
            ),
            "source_pack_id": pack_id,
            "source_pack_version": str(manifest.get("pack", {}).get("version") or ""),
            "content_hash": str(manifest.get("provenance", {}).get("content_hash") or ""),
            "build_identity": str(manifest.get("provenance", {}).get("build_identity") or ""),
            "trust_class": str(manifest.get("provenance", {}).get("trust_class") or "untrusted"),
            "isolation": str(provider.get("isolation") or loader),
            "required_capabilities": list(provider.get("required_capabilities") or []),
            "routing_keys": sorted(
                {
                    str(value)
                    for value in provider.get("routing_keys", [])
                    if str(value).strip()
                }
            ),
            "operation": operation,
        }
        interface_registry.register(
            f"global_contract.provider.{contract_id}",
            descriptor,
            meta={
                "_source_pack_id": pack_id,
                "_source_pack_version": descriptor["source_pack_version"],
                "authority_grant": False,
                "isolation": descriptor["isolation"],
            },
        )
        registered += 1
    result.diagnostics.append(
        _diagnostic(
            "info" if registered else "warning",
            "v3_contract_bindings_registered",
            f"Registered {registered} contract providers",
            pack_id=pack_id,
            registered=registered,
        )
    )
    complete = registered > 0 and registered == expected
    if not complete and registered:
        for contract_id in entrypoints:
            interface_registry.unregister(
                f"global_contract.provider.{contract_id}",
                predicate=lambda entry, owner=pack_id: (
                    entry.get("meta", {}).get("_source_pack_id") == owner
                ),
            )
    return True, complete


class _ProcessContractOperation:
    """Invoke a declared pack process with a minimal non-secret environment."""

    def __init__(self, *, module: str, pack_location: PackLocation) -> None:
        self.module = module
        self.pack_location = pack_location

    def __call__(self, operation: str, payload: Dict[str, Any]) -> Any:
        runtime_root = self.pack_location.pack_dir.parent.parent
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        user_data_root = str(os.environ.get("RUMI_USER_DATA") or "").strip()
        if user_data_root:
            environment["RUMI_USER_DATA"] = user_data_root
        completed = subprocess.run(
            # ``-E`` intentionally ignores PYTHON* environment variables for
            # process isolation, including PYTHONDONTWRITEBYTECODE.  ``-B`` is
            # therefore required as an interpreter flag so a process-backed
            # pack can never add bytecode files to a signed application bundle.
            [sys.executable, "-B", "-s", "-E", "-m", self.module],
            input=json.dumps(
                {"operation": operation, "payload": dict(payload)},
                ensure_ascii=False,
            ),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            cwd=str(runtime_root),
            env=environment,
        )
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("pack process returned invalid JSON") from exc
        if not isinstance(response, dict) or response.get("status") != "ok":
            diagnostics = response.get("diagnostics") if isinstance(response, dict) else []
            code = str(response.get("error_code") or "provider_unavailable")
            raise GlobalContractInvocationError(
                code,
                "; ".join(str(item) for item in diagnostics),
            )
        return response.get("value")


def _load_python_contract_operation(
    *,
    module: str,
    symbol: str,
    pack_location: PackLocation,
    client: GlobalContractClient,
) -> Any:
    """Import one verified activation factory and return its operation only."""
    if not symbol.isidentifier():
        raise ValueError("python entrypoint symbol is invalid")
    runtime_root = pack_location.pack_dir.parent.parent
    added_path = str(runtime_root)
    inserted = added_path not in sys.path
    if inserted:
        sys.path.insert(0, added_path)
    try:
        activated_module = importlib.import_module(module)
        factory = getattr(activated_module, symbol)
        if not callable(factory):
            raise TypeError("python entrypoint factory is not callable")
        operation = factory(client)
    finally:
        if inserted:
            try:
                sys.path.remove(added_path)
            except ValueError:
                pass
    if not callable(operation):
        raise TypeError("python entrypoint did not return an operation")
    return operation


def _module_owned_by_pack(module: str, pack_id: str) -> bool:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", module) is None:
        return False
    prefixes = (f"{pack_id}.", f"ecosystem.{pack_id}.")
    return module.startswith(prefixes)


def _process_module_path(
    module: str,
    pack_location: PackLocation,
) -> Path | None:
    runtime_root = pack_location.pack_dir.parent.parent.resolve()
    candidate = runtime_root.joinpath(*module.split(".")).with_suffix(".py").resolve()
    try:
        candidate.relative_to(pack_location.pack_subdir.resolve())
    except ValueError:
        return None
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _iter_pack_locations(
    registry: Any,
    ecosystem_dir: Optional[str],
    effective_pack_ids: frozenset[str] | None = None,
) -> List[Tuple[str, PackLocation]]:
    if registry is not None and isinstance(getattr(registry, "packs", None), dict):
        pairs = []
        for pack_id, pack_info in registry.packs.items():
            eco_path = getattr(pack_info, "ecosystem_json_path", None)
            pack_subdir = getattr(pack_info, "pack_subdir", None) or getattr(pack_info, "subdir", None) or getattr(pack_info, "path", None)
            pack_dir = getattr(pack_info, "pack_dir", None) or pack_subdir
            if eco_path and pack_subdir and pack_dir:
                pairs.append(
                    (
                        str(pack_id),
                        PackLocation(
                            pack_dir=Path(pack_dir),
                            pack_id=str(pack_id),
                            ecosystem_json_path=Path(eco_path),
                            pack_subdir=Path(pack_subdir),
                        ),
                    )
                )
        if pairs:
            return pairs
    locations = (
        resolve_pack_locations(effective_pack_ids, ecosystem_dir)
        if effective_pack_ids is not None
        else discover_pack_locations(ecosystem_dir)
    )
    return [(loc.pack_id, loc) for loc in locations]


def _is_pack_approved(approval_manager: Any, pack_id: str) -> Tuple[bool, Optional[str]]:
    if approval_manager is None:
        try:
            from .approval_manager import get_approval_manager

            approval_manager = get_approval_manager()
        except Exception:
            return False, "approval_manager_unavailable"
    checker = getattr(approval_manager, "is_pack_approved_and_verified", None)
    if not callable(checker):
        return False, "approval_checker_unavailable"
    try:
        checked = checker(pack_id)
    except Exception as exc:
        return False, f"approval_check_error:{exc}"
    if isinstance(checked, tuple):
        return bool(checked[0]), checked[1] if len(checked) > 1 else None
    return bool(checked), None


def _read_manifest(path: Path, result: CapabilityBindingRegistrationResult, pack_id: str) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        result.ok = False
        result.diagnostics.append(
            _diagnostic(
                "error",
                "binding_registration_manifest_unreadable",
                f"Pack '{pack_id}' ecosystem.json could not be read: {exc}",
                pack_id=pack_id,
                path=str(path),
            )
        )
        return {}
    return data if isinstance(data, dict) else {}


def _register_path_from_manifest(manifest: Dict[str, Any]) -> Optional[str]:
    capability_bindings = manifest.get("capability_bindings")
    if isinstance(capability_bindings, dict) and isinstance(capability_bindings.get("register"), str):
        return capability_bindings["register"].strip() or None
    return None


def _register_path_allowed(register_path: str, pack_id: str) -> bool:
    module_path, _, fn_name = register_path.rpartition(".")
    if not module_path or not fn_name.isidentifier():
        return False
    allowed_prefixes = (
        f"{pack_id}.",
        f"ecosystem.{pack_id}.",
    )
    return register_path.startswith(allowed_prefixes)


def _host_registration_allowed(
    pack_id: str,
    pack_location: PackLocation,
    manifest: Dict[str, Any],
) -> Tuple[bool, str]:
    if _is_trusted_host_registration_pack(pack_id, pack_location):
        return True, "trusted_builtin_pack"
    if manifest.get("host_execution") is not True:
        return False, "host_execution_not_requested"
    if os.environ.get("RUMI_ALLOW_HOST_EXECUTION") != "true":
        return False, "host_execution_not_allowed"
    return True, "host_execution_allowed"


def _is_trusted_host_registration_pack(pack_id: str, pack_location: PackLocation) -> bool:
    normalized_pack_id = str(pack_id or "").strip()
    try:
        pack_dir = pack_location.pack_dir.resolve()
    except OSError:
        pack_dir = pack_location.pack_dir
    if normalized_pack_id.startswith(CORE_PACK_ID_PREFIX):
        try:
            core_pack_root = Path(CORE_PACK_DIR).resolve()
        except OSError:
            core_pack_root = Path(CORE_PACK_DIR)
        try:
            core_relative = pack_dir.relative_to(core_pack_root)
        except ValueError:
            return False
        return bool(core_relative.parts) and core_relative.parts[0] == normalized_pack_id
    if normalized_pack_id not in TRUSTED_BUILTIN_PACK_IDS:
        return False
    try:
        ecosystem_root = Path(ECOSYSTEM_DIR).resolve()
    except OSError:
        ecosystem_root = Path(ECOSYSTEM_DIR)
    try:
        relative = pack_dir.relative_to(ecosystem_root)
    except ValueError:
        return False
    return bool(relative.parts) and relative.parts[0] == normalized_pack_id


def _call_register_function(
    register_path: str,
    *,
    pack_location: PackLocation,
    interface_registry: InterfaceRegistry,
) -> List[str]:
    module_path, _, fn_name = register_path.rpartition(".")
    candidate_paths = [pack_location.pack_dir.parent]
    if register_path.startswith("ecosystem."):
        candidate_paths.append(pack_location.pack_dir.parent.parent)
    added_paths: List[str] = []
    for candidate_path in candidate_paths:
        added_path = str(candidate_path)
        if added_path not in sys.path:
            sys.path.insert(0, added_path)
            added_paths.append(added_path)
    try:
        module = importlib.import_module(module_path)
        fn = getattr(module, fn_name)
        if not callable(fn):
            raise TypeError(f"{register_path} is not callable")
        value = fn(interface_registry)
    finally:
        for added_path in added_paths:
            try:
                sys.path.remove(added_path)
            except ValueError:
                pass
    if isinstance(value, dict):
        registered = value.get("registered")
        if isinstance(registered, list):
            return [str(item) for item in registered]
    return []


def _diagnostic(level: str, code: str, message: str, **meta: Any) -> Dict[str, Any]:
    return {
        "level": level,
        "code": code,
        "message": message,
        **meta,
    }
