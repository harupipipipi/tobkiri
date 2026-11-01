"""Assemble an explicit original service DATA seam, without importing it.

The original class and DATA reexports stay verbatim. Native persistence,
activation, locking and their package exports are not simulated in the guest.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
from typing import Any, Callable, Mapping

SERVICE = "tobkiri_runtime/ecosystem/defaultspack/domain/runtime_v4/service.py"
RECORDS = "tobkiri_runtime/ecosystem/defaultspack/domain/_profile_runtime_records.py"
INITIALIZER = "tobkiri_runtime/ecosystem/defaultspack/domain/runtime_v4/__init__.py"
SERVICE_PATH = f"source-package/{SERVICE}"
RECORDS_PATH = f"source-package/{RECORDS}"
INITIALIZER_PATH = f"source-package/{INITIALIZER}"
ORIGINAL_INITIALIZER_PATH = "source-package/facade-inputs/runtime_v4_initializer.py.txt"
RESOLUTION_EXPORTS = (
    "ProfileResolutionDenied",
    "ResolvedDefaultProfile",
    "_AUTHORITY_MODES",
    "_DIGEST_RE",
    "_FOUNDATIONAL_CONTRACT",
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
PACKAGE_EXPORTS = (
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
NATIVE_EXPORTS = ("ActivationStore",)
DATA_EXPORTS = tuple(n for n in PACKAGE_EXPORTS if n not in NATIVE_EXPORTS)
SERVICE_CLASSES = (
    "ProfileReconfirmationRequired",
    "ActivationLockTimeout",
    "ArtifactVerificationTimeout",
    "ActiveDefaultProfile",
)
SERVICE_FUNCTIONS = ("project_runtime_launch_selector",)


@dataclass(frozen=True)
class DataFacade:
    """Generated files plus exact source locations and explicit missing scope."""

    files: dict[str, bytes]
    locations: dict[tuple[str, str], tuple[str, dict[str, Any]]]
    modules: list[dict[str, Any]]
    custody: dict[str, Any]


def assemble(
    capsule: Mapping[str, bytes],
    metadata: dict[str, Any],
    extent: Callable[[bytes, ast.AST], dict[str, Any]],
) -> DataFacade:
    """Assemble validated source records; never execute original or generated code."""
    records = {}
    for path, symbols in (
        (RECORDS, SERVICE_CLASSES),
        (
            SERVICE,
            (
                *SERVICE_FUNCTIONS,
                "bundle_catalog_reexports",
                "profile_resolution_reexports",
                "canonical_reexports",
                "profile_runtime_record_reexports",
            ),
        ),
        (INITIALIZER, ("runtime_v4_package_initializer",)),
    ):
        for symbol in symbols:
            matches = [
                r
                for r in metadata["definitions"]
                if r["source_path"] == path and r["symbol"] == symbol
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"original service DATA definition is missing: {symbol}"
                )
            records[symbol] = matches[0]

    def raw(symbol: str) -> bytes:
        return capsule[records[symbol]["stored_as"]]

    record_source, = [r for r in metadata["sources"] if r["path"] == RECORDS]
    original_records = capsule[record_source["stored_as"]]
    records_tree = ast.parse(original_records)

    active_tree = ast.parse(raw("ActiveDefaultProfile"))
    if len(active_tree.body) != 1 or not isinstance(active_tree.body[0], ast.ClassDef):
        raise ValueError("original active DATA class changed")
    active = active_tree.body[0]
    if (
        active.name != "ActiveDefaultProfile"
        or active.bases
        or active.keywords
        or len(active.decorator_list) != 1
    ):
        raise ValueError("original active DATA class changed")
    expected_decorator = ast.parse("dataclass(frozen=True)", mode="eval").body
    if ast.dump(active.decorator_list[0]) != ast.dump(expected_decorator):
        raise ValueError("original active DATA dataclass decorator changed")
    expected_fields = ast.parse(
        "resolved: ResolvedDefaultProfile\nactivation: Mapping[str, Any]"
    ).body
    fields = [n for n in active.body if isinstance(n, ast.AnnAssign)]
    if (
        [ast.dump(n) for n in fields] != [ast.dump(n) for n in expected_fields]
        or len(active.body) != 3
        or not isinstance(active.body[0], ast.Expr)
        or not isinstance(active.body[0].value, ast.Constant)
        or type(active.body[0].value.value) is not str
    ):
        raise ValueError("original active DATA dependencies or body changed")

    expected_attributes = ast.parse(
        "verified_profile_definition_digest: str | None = None\n"
        "verified_profile: Mapping[str, Any] | None = None\n"
        "verified_activation_identity: tuple[str, str, str, str] | None = None\n"
    ).body
    for name, base in (
        ("ProfileReconfirmationRequired", "ProfileResolutionDenied"),
        ("ActivationLockTimeout", "ProfileResolutionDenied"),
        ("ArtifactVerificationTimeout", "ActivationLockTimeout"),
    ):
        tree = ast.parse(raw(name))
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.ClassDef):
            raise ValueError("original service DATA exception changed")
        cls = tree.body[0]
        attributes = (
            expected_attributes if name == "ProfileReconfirmationRequired" else []
        )
        if (
            cls.name != name
            or len(cls.bases) != 1
            or not isinstance(cls.bases[0], ast.Name)
            or cls.bases[0].id != base
            or cls.keywords
            or cls.decorator_list
            or not cls.body
            or not isinstance(cls.body[0], ast.Expr)
            or not isinstance(cls.body[0].value, ast.Constant)
            or type(cls.body[0].value.value) is not str
            or [ast.dump(n) for n in cls.body[1:]] != [ast.dump(n) for n in attributes]
        ):
            raise ValueError("original service DATA exception dependencies changed")
    selector_tree = ast.parse(raw("project_runtime_launch_selector"))
    if (
        len(selector_tree.body) != 1
        or not isinstance(selector_tree.body[0], ast.FunctionDef)
        or selector_tree.body[0].name != "project_runtime_launch_selector"
    ):
        raise ValueError("original launch selector DATA definition changed")

    imports = (
        (
            "bundle_catalog_reexports",
            "tobkiri_protocol.bundle_catalog",
            0,
            (
                ("BundleIntegrityError", "BundleIntegrityError"),
                ("BundledCatalog", None),
                ("DefaultProfileV4Error", "DefaultProfileV4Error"),
            ),
        ),
        (
            "profile_resolution_reexports",
            "profile_resolution",
            2,
            tuple((name, name) for name in RESOLUTION_EXPORTS),
        ),
        (
            "canonical_reexports",
            "tobkiri_protocol.canonical",
            0,
            (
                ("canonical_digest", None),
                ("canonical_json", None),
                ("strict_loads", None),
            ),
        ),
        (
            "profile_runtime_record_reexports",
            "_profile_runtime_records",
            2,
            (
                ("ActiveDefaultProfile", "ActiveDefaultProfile"),
                ("ActivationLockTimeout", "ActivationLockTimeout"),
                ("ArtifactVerificationTimeout", "ArtifactVerificationTimeout"),
                ("ProfileReconfirmationRequired", "ProfileReconfirmationRequired"),
            ),
        ),
    )
    for symbol, module, level, aliases in imports:
        tree = ast.parse(raw(symbol))
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.ImportFrom):
            raise ValueError("original service DATA reexport changed")
        node = tree.body[0]
        if (
            node.module != module
            or node.level != level
            or tuple((a.name, a.asname) for a in node.names) != aliases
        ):
            raise ValueError("original service DATA reexport changed")

    original_initializer = raw("runtime_v4_package_initializer")
    init_tree = ast.parse(original_initializer)
    if (
        len(init_tree.body) != 3
        or not isinstance(init_tree.body[0], ast.Expr)
        or not isinstance(init_tree.body[0].value, ast.Constant)
        or type(init_tree.body[0].value.value) is not str
        or not isinstance(init_tree.body[1], ast.ImportFrom)
        or not isinstance(init_tree.body[2], ast.Assign)
    ):
        raise ValueError(
            "original service package initializer responsibilities changed"
        )
    original_import, original_all = init_tree.body[1:]
    if (
        original_import.module != "service"
        or original_import.level != 1
        or tuple(a.name for a in original_import.names) != PACKAGE_EXPORTS
        or any(a.asname is not None for a in original_import.names)
        or len(original_all.targets) != 1
        or not isinstance(original_all.targets[0], ast.Name)
        or original_all.targets[0].id != "__all__"
        or ast.literal_eval(original_all.value) != list(PACKAGE_EXPORTS)
    ):
        raise ValueError("original service package export inventory changed")

    service = (
        b'"""Original service DATA only; native activation APIs are unconnected."""\n'
        b"from __future__ import annotations\n\n"
        b"from typing import Any, Mapping\n\n"
        + raw("bundle_catalog_reexports")
        + b"\n"
        + raw("profile_resolution_reexports")
        + b"\n\n"
        + raw("canonical_reexports")
        + b"\n\n"
        + raw("profile_runtime_record_reexports")
        + b"\n\n"
        + b"\n\n".join(raw(name) for name in SERVICE_FUNCTIONS)
    )
    initializer = (
        '"""Explicit original DATA exports; native runtime exports remain unconnected."""\n\n'
        + "from .service import (\n"
        + "".join(f"    {name},\n" for name in DATA_EXPORTS)
        + ")\n\n__all__ = "
        + repr(list(DATA_EXPORTS))
        + "\n"
    ).encode()
    generated_tree = ast.parse(service)
    locations = {}
    for name in SERVICE_CLASSES:
        (node,) = [
            n for n in records_tree.body
            if isinstance(n, ast.ClassDef) and n.name == name
        ]
        locations[(RECORDS, name)] = (
            RECORDS_PATH, extent(original_records, node),
        )
    for name in SERVICE_FUNCTIONS:
        (node,) = [
            n
            for n in generated_tree.body
            if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name == name
        ]
        locations[(SERVICE, name)] = (SERVICE_PATH, extent(service, node))
    for symbol, module, level, _ in imports:
        (node,) = [
            n
            for n in generated_tree.body
            if isinstance(n, ast.ImportFrom) and n.module == module and n.level == level
        ]
        locations[(SERVICE, symbol)] = (SERVICE_PATH, extent(service, node))
    locations[(INITIALIZER, "runtime_v4_package_initializer")] = (
        ORIGINAL_INITIALIZER_PATH,
        {
            "start_line": 1,
            "end_line": len(original_initializer.splitlines()),
            "start_byte": 0,
            "end_byte": len(original_initializer),
            "bytes": len(original_initializer),
            "sha256": hashlib.sha256(original_initializer).hexdigest(),
        },
    )
    for symbol in (*SERVICE_CLASSES, *SERVICE_FUNCTIONS, *(x[0] for x in imports)):
        source_path = RECORDS if symbol in SERVICE_CLASSES else SERVICE
        source = original_records if symbol in SERVICE_CLASSES else service
        _, location = locations[(source_path, symbol)]
        if source[location["start_byte"] : location["end_byte"]] != raw(symbol):
            raise ValueError("generated service DATA bytes changed")
    ast.parse(initializer)
    modules = []
    for path, content in (
        (SERVICE_PATH, service), (RECORDS_PATH, original_records),
        (INITIALIZER_PATH, initializer),
    ):
        record = {
            "path": path,
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
            "role": ("verbatim_original_data_module" if path == RECORDS_PATH
                     else "explicit_original_data_facade"),
        }
        if path == SERVICE_PATH:
            record["definition_extents"] = {
                name: locations[(SERVICE, name)][1]
                for name in SERVICE_FUNCTIONS
            }
        elif path == RECORDS_PATH:
            record["definition_extents"] = {
                name: locations[(RECORDS, name)][1] for name in SERVICE_CLASSES
            }
        modules.append(record)
    return DataFacade(
        {
            SERVICE_PATH: service,
            RECORDS_PATH: original_records,
            INITIALIZER_PATH: initializer,
            ORIGINAL_INITIALIZER_PATH: original_initializer,
        },
        locations,
        modules,
        {
            "original_service_source": SERVICE,
            "original_record_source": RECORDS,
            "original_initializer_source": INITIALIZER,
            "original_initializer_preserved_as": ORIGINAL_INITIALIZER_PATH,
            "class_generation_dependencies": [
                "dataclasses.dataclass(frozen=True)",
                "typing.Mapping",
                "typing.Any",
                "profile_resolution.ResolvedDefaultProfile",
                "profile_resolution.ProfileResolutionDenied",
            ],
            "class_definitions_synthesized": False,
            "launch_selector_dependencies": [
                "canonical.canonical_digest",
                "typing.Mapping",
                "ProfileResolutionDenied",
                "builtin DATA operations",
            ],
            "data_package_exports": list(DATA_EXPORTS),
            "unconnected_native_package_exports": list(NATIVE_EXPORTS),
            "class_identity_routes": [
                {
                    "names": [
                        "BundledCatalog",
                        "BundleIntegrityError",
                        "DefaultProfileV4Error",
                    ],
                    "defined_in": "tobkiri_protocol.bundle_catalog",
                    "service_binding": "same-object import/reexport",
                },
                {
                    "names": ["ResolvedDefaultProfile", "ProfileResolutionDenied"],
                    "defined_in": "ecosystem.defaultspack.domain.profile_resolution",
                    "reported_module": "ecosystem.defaultspack.domain.runtime_v4.service",
                    "service_binding": "same-object import/reexport; original __module__ assignments retained",
                },
                {
                    "names": list(SERVICE_CLASSES),
                    "defined_in": (
                        "ecosystem.defaultspack.domain._profile_runtime_records"
                    ),
                    "reported_module": (
                        "ecosystem.defaultspack.domain.runtime_v4.service"
                    ),
                    "service_binding": (
                        "same-object import/reexport; "
                        "canonical __module__ assignments retained"
                    ),
                    "package_binding": "same-object import/reexport",
                },
            ],
            "initializer_adapter_rule": "retain same-object DATA reexports; omit native exports without replacement",
            "native_state_transfer": {
                "service_import_initialization": "excluded service imports/state are not executed by this facade; pure DATA caches/helpers still require placement audit, native persistence/authority requires Rust owner integration",
                "activation_registration_recovery_handover": "outside this facade; existing user paths remain required",
                "connected": False,
            },
            "native_activation_state_in_guest": False,
            "original_class_aliases_executed": False,
            "launch_selector_wit_connected": False,
            "whole_service_compatible": False,
        },
    )
