"""Fresh-process observations for Profile source relocation compatibility.

Importing this module uses only the standard library. ``observe`` performs the
runtime imports inside the caller's isolated CI namespace and intentionally
does not launch a Host, backend, VM, worker, or approval flow.
"""

from __future__ import annotations

import dataclasses
import enum
import importlib
import inspect
import json
import math
import pickle
import types
import typing
from collections.abc import Mapping
from pathlib import Path

_RELOCATION_RECORD = Path(__file__).with_name(
    "profile_resolution_source_relocation.json"
)
_FALLBACK_MOVED_DEFINITIONS = (
    "ProfileResolutionDenied",
    "ResolvedDefaultProfile",
    "_application_launch_identity",
    "_authority_mode",
    "_bundle_digest",
    "_edge_key",
    "_exact_executable_variant",
    "_provider_candidates",
    "_require_digest",
    "_require_optional_pin",
    "_sha256_bytes",
    "dynamic_profile_edges",
    "resolve_default_profile",
)
_RUNTIME_V4_ALL = (
    "ActiveDefaultProfile",
    "ActivationLockTimeout",
    "ActivationStore",
    "ArtifactVerificationTimeout",
    "BundleIntegrityError",
    "BundledCatalog",
    "DefaultProfileV4Error",
    "ProfileReconfirmationRequired",
    "ProfileResolutionDenied",
    "ResolvedDefaultProfile",
    "dynamic_profile_edges",
    "project_runtime_launch_selector",
    "resolve_default_profile",
)
_HOST_ALL = (
    "ArtifactVariant",
    "AuthorityV4Adapter",
    "AuthorityCeilings",
    "CompiledPack",
    "ContractOperation",
    "EffectClass",
    "FunctionArtifact",
    "CapabilityProviderRegistration",
    "HostExtensionRegistration",
    "HostExtensionSDK",
    "MacOSVZAgentIdentity",
    "MacOSVZDomainAllocation",
    "MacOSVZDomainAllocator",
    "MacOSVZHelperIdentity",
    "MacOSVZLaunchAssets",
    "MacOSVZRuntime",
    "MacOSVZSupervisorDriver",
    "MacOSVZSupervisorTransport",
    "MacOSVZTransportFactory",
    "InvocationFrame",
    "HostV4Composition",
    "OpaqueAuthorityRef",
    "PackArtifact",
    "ProductionRuntimeV4",
    "RequestContext",
    "V4DispatchSession",
    "install_dispatch_session",
    "production_backend_registry",
    "compile_pack_root",
    "routes_for_plan",
)
_HOST_EXPORT_MODULES = {
    "ArtifactVariant": "models",
    "AuthorityV4Adapter": "authority_v4",
    "AuthorityCeilings": "composition",
    "CompiledPack": "artifact_compiler",
    "ContractOperation": "models",
    "EffectClass": "models",
    "FunctionArtifact": "models",
    "CapabilityProviderRegistration": "extension_sdk",
    "HostExtensionRegistration": "extension_sdk",
    "HostExtensionSDK": "extension_sdk",
    "MacOSVZAgentIdentity": "macos_vz_supervisor",
    "MacOSVZDomainAllocation": "macos_vz_supervisor",
    "MacOSVZDomainAllocator": "macos_vz_supervisor",
    "MacOSVZHelperIdentity": "macos_vz_supervisor",
    "MacOSVZLaunchAssets": "macos_vz_supervisor",
    "MacOSVZRuntime": "macos_vz_supervisor",
    "MacOSVZSupervisorDriver": "macos_vz_supervisor",
    "MacOSVZSupervisorTransport": "macos_vz_supervisor",
    "MacOSVZTransportFactory": "macos_vz_supervisor",
    "InvocationFrame": "models",
    "HostV4Composition": "composition",
    "OpaqueAuthorityRef": "models",
    "PackArtifact": "models",
    "ProductionRuntimeV4": "runtime",
    "RequestContext": "models",
    "V4DispatchSession": "runtime",
    "install_dispatch_session": "runtime",
    "production_backend_registry": "backends",
    "compile_pack_root": "artifact_compiler",
    "routes_for_plan": "artifact_compiler",
}
_HOST_SUBMODULES = (
    "authority_v4",
    "backends",
    "macos_vz_supervisor",
    "models",
    "extension_sdk",
    "artifact_compiler",
    "composition",
    "runtime",
)
_MODEL_EXPORTS = (
    "ArtifactVariant",
    "ContractOperation",
    "EffectClass",
    "FunctionArtifact",
    "InvocationFrame",
    "OpaqueAuthorityRef",
    "PackArtifact",
    "RequestContext",
)


def _qualified_name(value: object) -> dict[str, str]:
    module = getattr(value, "__module__", None)
    qualname = getattr(value, "__qualname__", None)
    if not isinstance(module, str) or not isinstance(qualname, str):
        raise TypeError(
            "value has no stable module/qualname: "
            f"{type(value).__module__}.{type(value).__qualname__}"
        )
    return {"module": module, "qualname": qualname}


def _type_reference(value: object) -> dict[str, str]:
    return _qualified_name(value)


def _runtime_type(value: object) -> dict[str, str]:
    return _qualified_name(type(value))


def _default_value(value: object) -> object:
    """Normalize known signature defaults without address-based repr cleanup."""

    if value is inspect.Parameter.empty:
        return {"kind": "empty", "type": _runtime_type(value)}
    if value is dataclasses.MISSING:
        return {"kind": "sentinel", "type": _runtime_type(value)}
    factory_sentinel = getattr(dataclasses, "_HAS_DEFAULT_FACTORY", None)
    if factory_sentinel is not None and value is factory_sentinel:
        return {
            "kind": "dataclass-default-factory",
            "type": _runtime_type(value),
        }
    if isinstance(value, enum.Enum):
        return {
            "kind": "enum-member",
            "type": _qualified_name(type(value)),
            "name": value.name,
            "value": _default_value(value.value),
        }
    if value is None:
        return {"kind": "none", "type": _runtime_type(value)}
    if type(value) in (bool, int, str):
        return {
            "kind": type(value).__name__,
            "type": _runtime_type(value),
            "value": value,
        }
    if type(value) is float:
        return {
            "kind": "float",
            "type": _runtime_type(value),
            "value": value if math.isfinite(value) else _float_token(value),
        }
    if isinstance(value, bytes):
        return {"kind": "bytes", "type": _runtime_type(value), "hex": value.hex()}
    if isinstance(value, type):
        return {
            "kind": "type-reference",
            "type": _runtime_type(value),
            "reference": _qualified_name(value),
        }
    if isinstance(value, (types.FunctionType, types.BuiltinFunctionType)):
        return {
            "kind": "function-reference",
            "type": _runtime_type(value),
            "reference": _qualified_name(value),
        }
    if isinstance(value, types.MethodType):
        return {
            "kind": "method-reference",
            "type": _runtime_type(value),
            "function": _qualified_name(value.__func__),
            "owner": _qualified_name(type(value.__self__)),
        }
    if isinstance(value, tuple):
        return {
            "kind": "tuple",
            "type": _runtime_type(value),
            "items": [_default_value(item) for item in value],
        }
    if isinstance(value, list):
        return {
            "kind": "list",
            "type": _runtime_type(value),
            "items": [_default_value(item) for item in value],
        }
    if isinstance(value, (set, frozenset)):
        items = [_default_value(item) for item in value]
        items.sort(key=_stable_json)
        return {
            "kind": "frozenset" if isinstance(value, frozenset) else "set",
            "type": _runtime_type(value),
            "items": items,
        }
    if isinstance(value, Mapping):
        return {
            "kind": "mapping",
            "type": _type_reference(type(value)),
            "items": [
                [_default_value(key), _default_value(item)]
                for key, item in value.items()
            ],
        }
    raise TypeError(
        "unknown signature default needs explicit normalization: "
        f"{type(value).__module__}.{type(value).__qualname__}"
    )


def _float_token(value: float) -> str:
    if math.isnan(value):
        return "nan"
    return "+infinity" if value > 0 else "-infinity"


def _annotation_value(value: object) -> object:
    if value is typing.Any:
        return {"kind": "typing-special", "module": "typing", "qualname": "Any"}
    if value is inspect.Parameter.empty:
        return {"kind": "empty"}
    if value is Ellipsis:
        return {"kind": "ellipsis"}
    if isinstance(value, str):
        return {"kind": "forward-reference", "value": value}
    if value is None:
        return {"kind": "type", "module": "builtins", "qualname": "NoneType"}
    if type(value) in (list, tuple):
        return {
            "kind": "callable-parameter-group",
            "container_type": _runtime_type(value),
            "items": [_annotation_value(item) for item in value],
        }
    origin = typing.get_origin(value)
    if origin is not None:
        return {
            "kind": "generic",
            "origin": _annotation_value(origin),
            "args": [_annotation_value(item) for item in typing.get_args(value)],
        }
    if type(value) in (bool, int, float, str, bytes):
        return {
            "kind": "literal",
            "type": type(value).__name__,
            "value": value
            if type(value) is not float or math.isfinite(value)
            else _float_token(value),
        }
    if isinstance(value, type):
        return {"kind": "type", **_qualified_name(value)}
    if isinstance(value, enum.Enum):
        return {
            "kind": "enum-member",
            "type": _qualified_name(type(value)),
            "name": value.name,
            "value": _annotation_value(value.value),
        }
    module = getattr(value, "__module__", None)
    qualname = getattr(value, "__qualname__", None)
    if isinstance(module, str) and isinstance(qualname, str):
        return {"kind": "symbol", "module": module, "qualname": qualname}
    raise TypeError(
        "unknown type-hint value needs explicit normalization: "
        f"{type(value).__module__}.{type(value).__qualname__}"
    )


def _signature_data(value: object) -> dict[str, object]:
    try:
        signature = inspect.signature(value)
    except (TypeError, ValueError) as error:
        return {"available": False, "error": _exception_data(error)}
    parameters = []
    for parameter in signature.parameters.values():
        parameters.append(
            {
                "name": parameter.name,
                "kind": parameter.kind.name,
                "default": _default_value(parameter.default),
                "annotation": _annotation_value(parameter.annotation),
            }
        )
    return {
        "available": True,
        "parameters": parameters,
        "return_annotation": _annotation_value(signature.return_annotation),
    }


def _exception_data(error: BaseException) -> dict[str, object]:
    return {
        "module": type(error).__module__,
        "qualname": type(error).__qualname__,
        "args": [_json_value(item) for item in error.args],
    }


def _json_value(value: object) -> object:
    """Convert behavior data to JSON values while retaining mapping/list order."""

    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else {"$float": _float_token(value)}
    if isinstance(value, bytes):
        return {"$bytes_hex": value.hex()}
    if isinstance(value, enum.Enum):
        return {
            "$enum": _qualified_name(type(value)),
            "name": value.name,
            "value": _json_value(value.value),
        }
    if isinstance(value, Path):
        return {"$path": value.as_posix()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            "$dataclass": _qualified_name(type(value)),
            "fields": {
                field.name: _json_value(getattr(value, field.name))
                for field in dataclasses.fields(value)
            },
        }
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    "JSON observation mapping key is not a string: "
                    f"{type(key).__module__}.{type(key).__qualname__}"
                )
            result[key] = _json_value(item)
        return result
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, tuple):
        return {"$tuple": [_json_value(item) for item in value]}
    if isinstance(value, (set, frozenset)):
        items = [_json_value(item) for item in value]
        items.sort(key=_stable_json)
        return {"$frozenset" if isinstance(value, frozenset) else "$set": items}
    raise TypeError(
        "unknown observation value needs explicit normalization: "
        f"{type(value).__module__}.{type(value).__qualname__}"
    )


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _definition_type_hints(value: object, name: str) -> dict[str, object]:
    try:
        hints = typing.get_type_hints(value)
    except Exception as error:
        failure = _exception_data(error)
        if name in _FALLBACK_MOVED_DEFINITIONS:
            raise RuntimeError(
                "moved definition type hints must resolve: "
                + _stable_json({"name": name, "error": failure})
            ) from error
        return {"ok": False, "error": failure}
    return {
        "ok": True,
        "items": {key: _annotation_value(item) for key, item in hints.items()},
    }


def _callable_observation(value: object, name: str) -> dict[str, object]:
    if not callable(value):
        return {"callable": False}
    return {
        "callable": True,
        "signature": _signature_data(value),
        "type_hints": _definition_type_hints(value, name),
    }


def _moved_definition_names() -> tuple[str, ...]:
    if not _RELOCATION_RECORD.is_file():
        return _FALLBACK_MOVED_DEFINITIONS
    record = json.loads(_RELOCATION_RECORD.read_text(encoding="utf-8"))
    names = record.get("owned_definition_names")
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise RuntimeError("profile relocation record has invalid definition names")
    if tuple(names) != _FALLBACK_MOVED_DEFINITIONS:
        raise RuntimeError(
            "profile relocation record differs from expected moved definitions"
        )
    return tuple(names)


def _authority_bindings(catalog: object, canonical_digest: object) -> dict[str, str]:
    references = {}
    for edge in catalog.profiles["defaults"]["requested_edges"]:
        key = "|".join(
            str(edge[field])
            for field in (
                "caller_function_id",
                "target_provider_id",
                "contract_id",
                "operation_id",
            )
        )
        references[key] = "authority-ref:" + canonical_digest(key).removeprefix(
            "sha256:"
        )
    return references


def _invoke_expected_failure(
    name: str, callback: object, expected_error: type[Exception]
) -> dict[str, object]:
    try:
        callback()
    except Exception as error:
        if not isinstance(error, expected_error):
            raise
        return {"name": name, "failure": _exception_data(error)}
    raise RuntimeError(f"negative Profile observation unexpectedly succeeded: {name}")


def _observe_negative_resolution(
    catalog: object,
    resolver: object,
    approved: set[str],
    bindings: dict[str, str],
    expected_error: type[Exception],
) -> list[dict[str, object]]:
    common = {
        "approved_artifact_digests": approved,
        "authority_snapshot_digest": "sha256:" + "9" * 64,
        "authority_bindings": bindings,
        "security_epoch": 7,
    }
    if not bindings:
        raise RuntimeError("packaged defaults Profile has no authority bindings")
    missing = {}
    cases = (
        (
            "unknown-profile",
            lambda: resolver(catalog, "__missing_profile__", **common),
        ),
        (
            "invalid-snapshot-digest",
            lambda: resolver(
                catalog,
                "defaults",
                **{**common, "authority_snapshot_digest": "invalid"},
            ),
        ),
        (
            "invalid-security-epoch",
            lambda: resolver(
                catalog,
                "defaults",
                **{**common, "security_epoch": -1},
            ),
        ),
        (
            "empty-artifact-approvals",
            lambda: resolver(
                catalog,
                "defaults",
                **{**common, "approved_artifact_digests": set()},
            ),
        ),
        (
            "missing-authority-binding",
            lambda: resolver(
                catalog,
                "defaults",
                **{**common, "authority_bindings": missing},
            ),
        ),
    )
    return [
        _invoke_expected_failure(name, callback, expected_error)
        for name, callback in cases
    ]


def _observe_definition_metadata(
    definitions: object,
    legacy_service: object,
    moved_names: tuple[str, ...],
) -> dict[str, object]:
    result = {}
    for name in moved_names:
        value = getattr(definitions, name)
        legacy_value = getattr(legacy_service, name)
        if legacy_value is not value:
            raise RuntimeError(f"legacy service does not re-export moved object {name}")
        if getattr(value, "__module__", None) != (
            "ecosystem.defaultspack.domain.runtime_v4.service"
        ):
            raise RuntimeError(
                f"moved object has an incompatible public module: {name}"
            )
        try:
            hints = _definition_type_hints(value, name)
        except RuntimeError:
            raise
        if not hints.get("ok", False):
            raise RuntimeError(f"moved definition type hints are unresolved: {name}")
        result[name] = {
            "legacy_service_identity": True,
            "module": value.__module__,
            "qualname": value.__qualname__,
            "signature": _signature_data(value),
            "type_hints": hints,
        }
    return result


def _observe_runtime_facade(runtime_v4: object, service: object) -> dict[str, object]:
    exports = tuple(runtime_v4.__all__)
    if exports != _RUNTIME_V4_ALL:
        raise RuntimeError(
            "runtime_v4.__all__ differs from the original 13-name facade"
        )
    rows = []
    for name in exports:
        value = getattr(runtime_v4, name)
        same = value is getattr(service, name)
        if not same:
            raise RuntimeError(f"runtime_v4 export identity changed: {name}")
        rows.append(
            {
                "name": name,
                "defining_module": service.__name__,
                "same_object": True,
                **_callable_observation(value, name),
            }
        )
    return {"all": list(exports), "count": len(exports), "exports": rows}


def _observe_host_exports(
    host: object, reverse_access: bool = False
) -> dict[str, object]:
    exports = tuple(host.__all__)
    if exports != _HOST_ALL:
        raise RuntimeError(
            "tobkiri_host.__all__ differs from the original 30-name facade"
        )
    if set(_HOST_EXPORT_MODULES) != set(exports):
        raise RuntimeError("Host export owner map is incomplete")

    rows = []
    modules = {}
    access_order = list(reversed(exports)) if reverse_access else list(exports)
    for name in access_order:
        module_name = _HOST_EXPORT_MODULES[name]
        # Exercise the public facade before importing its owner ourselves.
        value = getattr(host, name)
        module = importlib.import_module("." + module_name, host.__name__)
        modules[module_name] = module
        same = value is getattr(module, name)
        if not same:
            raise RuntimeError(f"Host export identity changed: {name}")
        rows.append(
            {
                "name": name,
                "defining_module": module.__name__,
                "same_object": True,
                **_callable_observation(value, name),
            }
        )

    model_module = modules["models"]
    model_rows = []
    for name in _MODEL_EXPORTS:
        same = getattr(host, name) is getattr(model_module, name)
        if not same:
            raise RuntimeError(f"Host data model identity changed: {name}")
        model_rows.append({"name": name, "same_object": True})
    return {
        "all": list(exports),
        "count": len(exports),
        "access_order": access_order,
        "exports": rows,
        "models": model_rows,
    }


def _observe_host_submodules(host: object, reverse_access: bool) -> dict[str, object]:
    forward = list(_HOST_SUBMODULES)
    initial_order = list(reversed(forward)) if reverse_access else forward
    second_order = list(reversed(initial_order))
    directory = set(dir(host))
    if not set(_HOST_SUBMODULES).issubset(directory):
        raise RuntimeError("Host dir() omits a legacy submodule attribute")

    module_objects = {}
    getattr_checks = []
    for name in initial_order:
        module = getattr(host, name)
        full_name = host.__name__ + "." + name
        actual = sys_modules_module(full_name)
        if module is not actual:
            raise RuntimeError(f"Host getattr/sys.modules identity changed: {name}")
        module_objects[name] = actual
        getattr_checks.append({"name": name, "same_sys_modules_object": True})

    from_checks = []
    for name in second_order:
        namespace = {}
        exec(f"from tobkiri_host import {name}", {}, namespace)
        full_name = host.__name__ + "." + name
        actual = sys_modules_module(full_name)
        if namespace[name] is not actual or actual is not module_objects[name]:
            raise RuntimeError(f"Host from-import differs from sys.modules: {name}")
        from_checks.append({"name": name, "same_sys_modules_object": True})

    iterated_names = [name for name in dir(host) if name in _HOST_SUBMODULES]
    iteration_checks = []
    for name in iterated_names:
        module = getattr(host, name)
        if module is not module_objects[name]:
            raise RuntimeError(f"Host dir iteration identity changed: {name}")
        iteration_checks.append({"name": name, "same_sys_modules_object": True})

    all_before = tuple(host.__all__)
    before_reload_modules = {
        name: sys_modules_module(host.__name__ + "." + name)
        for name in _HOST_SUBMODULES
    }
    reloaded = importlib.reload(host)
    if tuple(reloaded.__all__) != all_before:
        raise RuntimeError("Host __all__ changed after facade reload")
    for name, previous in before_reload_modules.items():
        if sys_modules_module(host.__name__ + "." + name) is not previous:
            raise RuntimeError(f"Host reload replaced sys.modules module: {name}")

    reload_checks = []
    for name in second_order:
        actual = sys_modules_module(reloaded.__name__ + "." + name)
        namespace = {}
        exec(f"from tobkiri_host import {name}", {}, namespace)
        if namespace[name] is not actual:
            raise RuntimeError(f"Host post-reload from-import identity changed: {name}")
        module = getattr(reloaded, name)
        if module is not actual or module is not module_objects[name]:
            raise RuntimeError(f"Host post-reload submodule identity changed: {name}")
        reload_checks.append({"name": name, "same_sys_modules_object": True})

    return {
        "submodules": list(_HOST_SUBMODULES),
        "initial_getattr_order": initial_order,
        "from_import_order": second_order,
        "iteration_order": iterated_names,
        "post_reload_order": second_order,
        "getattr": getattr_checks,
        "from_import": from_checks,
        "iteration": iteration_checks,
        "after_reload": reload_checks,
        "all_unchanged": list(all_before) == list(reloaded.__all__),
    }


def sys_modules_module(name: str) -> object:
    import sys

    module = sys.modules.get(name)
    if module is None:
        raise RuntimeError(f"expected module is absent from sys.modules: {name}")
    return module


def _observe_profile_runtime_registration(
    port: object,
    composition: object,
    before_import: dict[str, object],
    after_import: dict[str, object],
) -> dict[str, object]:
    first = composition.install_defaultspack_profile_runtime()
    second = composition.install_defaultspack_profile_runtime()
    required = port.require_profile_runtime()
    singleton = getattr(composition, "_DEFAULTSPACK_PROFILE_RUNTIME")
    same = first is second is required is singleton
    if not same:
        raise RuntimeError("Defaultspack Profile runtime singleton identity changed")
    return {
        "before_import": before_import,
        "after_import": after_import,
        "two_installs_same_object": first is second,
        "require_same_object": required is first,
        "singleton_same_object": singleton is first,
    }


def _pickle_observation(
    resolved: object,
    definitions: object,
    names: tuple[str, ...],
    legacy_pickle: bytes | None,
) -> tuple[dict[str, object], bytes]:
    exception_type = definitions.ProfileResolutionDenied
    payload = {
        "resolved": resolved,
        "definitions": {name: getattr(definitions, name) for name in names},
        "exception": exception_type("profile relocation pickle observation"),
    }
    generated = pickle.dumps(payload, protocol=5)
    source = generated if legacy_pickle is None else legacy_pickle
    if not isinstance(source, bytes):
        raise TypeError("trusted legacy pickle must be bytes")
    decoded = pickle.loads(source)
    current_resolved = _json_value(resolved)
    decoded_resolved = _json_value(decoded["resolved"])
    resolved_type_same = type(decoded["resolved"]) is type(resolved)
    if not resolved_type_same or decoded_resolved != current_resolved:
        raise RuntimeError(
            "baseline pickle resolved result differs from current resolver"
        )

    definition_identity = {}
    for name in names:
        same = decoded["definitions"][name] is getattr(definitions, name)
        if not same:
            raise RuntimeError(f"baseline pickle definition identity changed: {name}")
        definition_identity[name] = True
    exception_same = type(decoded["exception"]) is exception_type
    if not exception_same or decoded["exception"].args != payload["exception"].args:
        raise RuntimeError("baseline pickle exception identity/args changed")

    import hashlib

    return (
        {
            "protocol": 5,
            "pickle_role": "original-protocol5-observation",
            "input_sha256": hashlib.sha256(source).hexdigest(),
            "input_size": len(source),
            "resolved_type_identity": True,
            "resolved_data_equal": True,
            "definition_identities": definition_identity,
            "exception_identity": True,
            "exception_args": [_json_value(item) for item in decoded["exception"].args],
        },
        generated,
    )


def observe(
    bundle_root: Path,
    *,
    legacy_pickle: bytes | None = None,
    reverse_access: bool = False,
) -> tuple[dict, bytes]:
    """Observe original Setup behavior and facade identities in this process.

    ``legacy_pickle`` must be the trusted protocol-5 baseline artifact verified
    by the controlling CI job. This API is not an untrusted-pickle interface.
    ``bundle_root`` is the already-created packaged fixture; no fixture build is
    performed here.
    """

    if type(reverse_access) is not bool:
        raise TypeError("reverse_access must be a bool")
    # The port must be imported first. Importing application composition is
    # metadata-only and must not install its singleton as an import side effect.
    port = importlib.import_module("core_runtime.profile_runtime_port")
    try:
        port.require_profile_runtime()
    except port.ProfileRuntimeUnavailable as initial_unavailable:
        initial_registration = _exception_data(initial_unavailable)
    else:
        raise RuntimeError("Profile runtime was configured before observation")
    composition = importlib.import_module(
        "ecosystem.defaultspack.defaultspack.profile_runtime_composition"
    )
    try:
        port.require_profile_runtime()
    except port.ProfileRuntimeUnavailable as after_import_unavailable:
        after_import_registration = _exception_data(after_import_unavailable)
    else:
        raise RuntimeError("Defaultspack composition registered on import")

    # Observe cold Host facade access before the Setup helpers/service can load
    # native modules. Each direction runs in an independent original/candidate
    # process; no owner module is prewarmed by this observer.
    host = importlib.import_module("tobkiri_host")
    if reverse_access:
        host_facade = _observe_host_exports(host, reverse_access=True)
        host_submodule_access = _observe_host_submodules(host, reverse_access=True)
    else:
        host_submodule_access = _observe_host_submodules(host, reverse_access=False)
        host_facade = _observe_host_exports(host, reverse_access=False)

    test_module = importlib.import_module("tests.test_captured_setup_projection")

    service = importlib.import_module(
        "ecosystem.defaultspack.domain.runtime_v4.service"
    )
    runtime_v4 = importlib.import_module("ecosystem.defaultspack.domain.runtime_v4")
    data_module_name = "ecosystem.defaultspack.domain.profile_resolution"
    try:
        definitions = importlib.import_module(data_module_name)
    except ModuleNotFoundError as error:
        if error.name != data_module_name:
            raise
        # The baseline owns these objects directly in service.py.
        definitions = service

    moved_names = _moved_definition_names()
    data = {}
    data["profile_runtime_registration"] = {
        **_observe_profile_runtime_registration(
            port, composition, initial_registration, after_import_registration
        ),
    }

    data["moved_definitions"] = _observe_definition_metadata(
        definitions, service, moved_names
    )
    data["runtime_v4_facade"] = _observe_runtime_facade(runtime_v4, service)

    data["host_facade"] = host_facade
    data["host_submodule_access"] = host_submodule_access

    catalog_class = service.BundledCatalog
    catalog = catalog_class.load(Path(bundle_root))
    resolved = test_module._resolve(catalog)
    projections = test_module._projections(catalog, resolved)
    approved = {str(pack["pack"]["artifact_digest"]) for pack in catalog.packs.values()}
    bindings = _authority_bindings(catalog, test_module.canonical_digest)
    resolver = service.resolve_default_profile

    data["setup"] = {
        "resolved": _json_value(projections["resolved"]),
        "preview": _json_value(projections["preview"]),
        "confirmation": _json_value(projections["confirmation"]),
        "listings": _json_value(projections["listings"]),
    }
    data["negative_resolutions"] = _observe_negative_resolution(
        catalog, resolver, approved, bindings, service.ProfileResolutionDenied
    )
    pickle_data, pickle_bytes = _pickle_observation(
        resolved, definitions, moved_names, legacy_pickle
    )
    data["pickle"] = pickle_data
    data["reverse_access"] = reverse_access
    return data, pickle_bytes


__all__ = ["observe"]
