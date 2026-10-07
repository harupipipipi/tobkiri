"""Bounded external Pack authoring: authored source to canonical Pack v4 files.

This module deliberately does not read the repository-global Pack catalogs
(``schemas/pack_v4_catalog.v1.json``, ``executable_sources.v1.json``).  Pack ID
collision and admission policy are install-time Host concerns; the builder
only turns one authored Pack source directory into the canonical quartet and
verifies the staged result with the same validators and compiler the Host
admission path runs.

Publication model: the authored source tree is read-only.  The builder copies
it (real file copies, no links, so edits after copying a file do not
change that copied file) into a staging directory beside the
explicit output directory, renders the four generated artifacts inside the
staging copy, validates and compiles the staging copy, then publishes with a
single ``os.rename`` of the staged directory onto an output path that must
not exist.  An occupied output (file, directory, or symlink) is refused; no
overwrite path exists, and no general overwrite atomicity is claimed.
``check=True`` performs the same staged build and additionally requires an
existing output tree whose bytes match the staged tree exactly; it never
writes.

Authoring subset (stated, not general): ``kind`` is always ``normal_sandbox``,
``execution_boundary`` is always ``sandbox``, every Function compiles to a
Python PackVM variant (backend ``tobkiri.python-pack-v4``, ``runtime_abi``
``python3.13``, ``materialization_mode`` ``on_demand``, domain profile
``sandbox.default.v1``) whose implementation exports the production ABI
callable ``tobkiri_packvm_invoke(operation_id, payload) -> dict``.
Requirements declarations (capabilities, Contract/Pack dependencies, network,
secrets, approval policy, workspace boundary) pass through to the manifest
verbatim; declaring a requirement grants nothing and never elevates the Pack.

Static implementation checks are honest bounds, not proof: ``ast.parse`` can
confirm that a module statically defines a synchronous two-positional-argument
``tobkiri_packvm_invoke`` and only imports standard-library modules; it cannot
prove properties of arbitrary dynamic Python (conditional definitions,
metaprogramming, or runtime behavior).
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator

from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_protocol.canonical import (
    canonical_digest,
    canonical_json,
    strict_loads,
)
from tobkiri_protocol.errors import ProtocolError
from tobkiri_protocol.ids import (
    validate_canonical_id,
    validate_contract_id,
    validate_semver,
)
from tobkiri_protocol.validation import validate_document

from .pack_boundary import PackBoundaryError, finite_children


class PackAuthoringError(ValueError):
    """Raised when authored Pack source or publication fails closed."""


SOURCE_FILENAME = "pack-source.v1.json"
SOURCE_API_VERSION = "io.tobkiri.pack-source.v1"
GENERATOR_ID = "tobkiri.core_runtime.pack_authoring"
GENERATOR_VERSION = "1.0.0"
PACKVM_ENTRYPOINT = "tobkiri_packvm_invoke"
PACKVM_BACKEND = "tobkiri.python-pack-v4"
PACKVM_RUNTIME_ABI = "python3.13"
PACKVM_MATERIALIZATION = "on_demand"
PACKVM_DOMAIN_PROFILE = "sandbox.default.v1"
GENERATED_FILENAMES = (
    "pack.v4.json",
    "contracts.v4.json",
    "artifact-index.v4.json",
    "executables.v4.json",
)

_MAX_SOURCE_BYTES = 1024 * 1024
_MAX_IMPLEMENTATION_BYTES = 2 * 1024 * 1024
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_FILES = 4096
_MAX_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_SOURCE_DEPTH = 32
_MAX_CONTRACTS = 16
_MAX_FUNCTIONS = 16
_MAX_OPERATIONS = 64
_MAX_TIMEOUT_MS = 600_000
_MAX_IDENTIFIER_TEXT = 4096

_SUPPORTED_CONTRACT_STATUSES = ("draft", "accepted", "deprecated", "sunset")
_SUPPORTED_SECURITY = ("public", "internal", "sensitive", "restricted")
_SUPPORTED_SCOPE_SEMANTICS = ("declarative", "host_broker", "opaque", "unknown")
_SUPPORTED_EFFECT_CLASSES = (
    "pure",
    "read",
    "write",
    "external_effect",
    "privileged",
)
_SUPPORTED_ROLES = ("pure", "vendor_semantic", "brokered", "host_capability_provider")
_SUPPORTED_ISOLATION = ("pack_vm", "dedicated_process")
_SUPPORTED_IDEMPOTENCY = ("none", "keyed", "replayable")
_SUPPORTED_CARDINALITY = ("one", "many", "keyed", "chain", "fanout", "optional")
_SUPPORTED_APPROVAL_POLICY = ("none", "capability_gated", "always")
_SUPPORTED_WORKSPACE_BOUNDARY = (
    "pack_local",
    "workspace_brokered",
    "host_brokered",
    "none",
)

_DEFAULT_ERROR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "code": {"type": "string"},
        "message": {"type": "string"},
    },
    "required": ["code", "message"],
}

# Modules the isolated ``python -I -S`` guest interpreter can resolve.  The
# staged child carries no site-packages, so any top-level import outside the
# host's stdlib inventory (plus the named post-3.10 additions present in the
# declared guest ABI) cannot resolve at run time and is rejected at build.
_EXTRA_STDLIB_ROOTS = frozenset({"tomllib"})


def build_authored_pack(
    source_root: Path, output_dir: Path, *, check: bool = False
) -> dict[str, Any]:
    """Compile one authored Pack source into the canonical quartet.

    ``source_root`` must contain ``pack-source.v1.json`` and is never
    modified.  ``output_dir`` is the explicit publication target: its name
    must equal ``pack_id`` and, for a build, it must not already exist.  The
    staged Pack tree is verified with ``validate_document`` and
    ``compile_pack_root`` — the Host admission gate — before a single
    ``os.rename`` places it at ``output_dir``.  ``check=True`` verifies the
    staged build and requires an existing output tree matching it
    byte-for-byte; it never writes.

    Author code is never imported or executed; implementations are checked
    with ``ast.parse`` only.
    """

    source = _prepare_source_root(source_root)
    pack_id = _source_pack_id(source)
    # Staging is a sibling of the output. If either is inside source, the
    # recursive source copy would copy its own growing staging tree.
    candidate = Path(output_dir).resolve()
    if candidate == source or source in candidate.parents:
        raise PackAuthoringError("output directory must be outside the source tree")
    output = _prepare_output(output_dir, pack_id, check=check)
    stage_container = Path(
        tempfile.mkdtemp(prefix=".pack-authoring-", dir=output.parent)
    )
    try:
        staged = stage_container / pack_id
        _copy_source_tree(source, staged)
        rendered = _render_staged(staged)
        if check:
            _check_output_fresh(output, staged)
            return _build_result(source, output, rendered, check=True)
        _refuse_occupied_output(output)
        os.rename(staged, output)
        return _build_result(source, output, rendered, check=False)
    finally:
        if stage_container.exists():
            shutil.rmtree(stage_container)


def _build_result(
    source: Path,
    output: Path,
    rendered: Mapping[str, str],
    *,
    check: bool,
) -> dict[str, Any]:
    # The staged tree already compiled; compile what is actually on disk at
    # the output path once more so the reported artifact identity is real.
    compiled = compile_pack_root(output)
    return {
        "source_root": str(source),
        "output": str(output),
        "pack_id": compiled.artifact.pack_id,
        "artifact_digest": compiled.artifact.digest,
        "operations": sorted(
            f"{contract_id}/{operation_id}"
            for (contract_id, operation_id) in compiled.routes
        ),
        "artifacts": {
            name: hashlib.sha256(text.encode("utf-8")).hexdigest()
            for name, text in sorted(rendered.items())
        },
        "check": check,
    }


def _prepare_source_root(source_root: Path) -> Path:
    """Return the resolved source root after the fixed safety checks."""

    root = Path(source_root)
    if root.is_symlink() or not root.is_dir():
        raise PackAuthoringError("Pack source root must be a real directory")
    source_path = root / SOURCE_FILENAME
    if source_path.is_symlink() or not source_path.is_file():
        raise PackAuthoringError(
            f"{SOURCE_FILENAME} is required in a safe Pack source root"
        )
    return root.resolve()


def _source_pack_id(source: Path) -> str:
    """Return the declared, canonically valid ``pack_id`` of the source."""

    document = _load_source(source)
    return _canonical_id(document.get("pack_id"), "pack_id")


def _prepare_output(output_dir: Path, pack_id: str, *, check: bool) -> Path:
    """Resolve the explicit output directory and apply its existence policy."""

    raw = Path(output_dir)
    if raw.name != pack_id:
        raise PackAuthoringError(
            f"output directory name must equal pack_id {pack_id!r}"
        )
    parent = raw.parent
    if check:
        # Check mode never writes — not even missing parent directories.
        if parent.is_symlink() or not parent.is_dir():
            raise PackAuthoringError(
                "--check requires an existing output directory"
            )
    else:
        parent.mkdir(parents=True, exist_ok=True)
        if parent.is_symlink() or not parent.is_dir():
            raise PackAuthoringError("output parent must be a real directory")
    output = parent.resolve() / raw.name
    if check:
        if output.is_symlink() or not output.is_dir():
            raise PackAuthoringError(
                "--check requires an existing output directory"
            )
    else:
        _refuse_occupied_output(output)
    return output


def _refuse_occupied_output(output: Path) -> None:
    if output.exists() or output.is_symlink():
        raise PackAuthoringError(
            f"refusing to replace an occupied output path: {output}"
        )


def _copy_source_tree(source: Path, target: Path) -> None:
    """Bound the inventory before copying any authored file bytes."""
    pending = [(source, Path(), 0)]
    directories: list[Path] = []
    files: list[tuple[Path, Path, int]] = []
    total = 0
    entries = 0
    while pending:
        directory, relative, depth = pending.pop()
        if depth > _MAX_SOURCE_DEPTH:
            raise PackAuthoringError("authored Pack exceeds the directory depth limit")
        try:
            children = finite_children(directory)
        except PackBoundaryError as exc:
            raise PackAuthoringError(str(exc)) from exc
        for child in children:
            entries += 1
            if entries > _MAX_FILES:
                raise PackAuthoringError("authored Pack exceeds the inventory limit")
            if depth == 0 and child.name in GENERATED_FILENAMES:
                raise PackAuthoringError(
                    f"{child.name} is a generated artifact; the canonical quartet "
                    "is written to the output directory, not authored in source"
                )
            child_relative = relative / child.name
            if child.is_dir():
                directories.append(child_relative)
                pending.append((child, child_relative, depth + 1))
            elif child.is_file():
                size = child.stat().st_size
                total += size
                if size > _MAX_FILE_BYTES or total > _MAX_TOTAL_BYTES:
                    raise PackAuthoringError("authored Pack exceeds the size limit")
                files.append((child, child_relative, size))
            else:
                raise PackAuthoringError("Pack source contains a non-regular file")
    target.mkdir(mode=0o755)
    for relative in sorted(directories, key=lambda item: len(item.parts)):
        (target / relative).mkdir(mode=0o755)
    for child, relative, size in files:
        # Bound reads even if an editor grows a file after inventory. Compiling
        # the copied tree pins the actual copied bytes, not the earlier stat.
        with child.open("rb") as stream:
            content = stream.read(size + 1)
        if len(content) != size:
            raise PackAuthoringError("Pack source changed while being copied")
        (target / relative).write_bytes(content)


def _render_staged(staged: Path) -> dict[str, str]:
    """Render and fully validate the quartet inside the staging copy."""

    source = _load_source(staged)
    files = _finite_pack_files(staged)
    documents = _render_documents(source, staged, files)
    rendered = {
        name: _json_text(document) for name, document in documents.items()
    }
    for name, document in documents.items():
        if name not in GENERATED_FILENAMES:
            raise PackAuthoringError(f"unexpected rendered artifact: {name}")
        destination = staged / name
        destination.unlink(missing_ok=True)
        destination.write_text(rendered[name], encoding="utf-8", newline="\n")
    validate_document(
        Path(staged / "pack.v4.json").read_bytes(), "pack"
    )
    validate_document(
        Path(staged / "contracts.v4.json").read_bytes(),
        "pack_contract_catalog",
    )
    validate_document(
        Path(staged / "artifact-index.v4.json").read_bytes(),
        "pack_artifact_index",
    )
    validate_document(
        Path(staged / "executables.v4.json").read_bytes(),
        "executable_catalog",
    )
    compile_pack_root(staged)
    return rendered


def _check_output_fresh(output: Path, staged: Path) -> None:
    """Require the published output to byte-match the staged build."""

    staged_files = _file_digest_map(staged)
    output_files = _file_digest_map(output)
    if staged_files == output_files:
        return
    missing = sorted(set(staged_files) - set(output_files))
    extra = sorted(set(output_files) - set(staged_files))
    changed = sorted(
        path
        for path in staged_files.keys() & output_files.keys()
        if staged_files[path] != output_files[path]
    )
    parts = []
    if missing:
        parts.append("missing: " + ", ".join(missing))
    if changed:
        parts.append("stale: " + ", ".join(changed))
    if extra:
        parts.append("extra: " + ", ".join(extra))
    raise PackAuthoringError(
        "published output diverges from the authored source (" +
        "; ".join(parts) + ")"
    )


def _file_digest_map(root: Path) -> dict[str, str]:
    """Map every regular file under ``root`` to its sha256 digest."""

    digests: dict[str, str] = {}
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            children = finite_children(current)
        except PackBoundaryError as exc:
            raise PackAuthoringError(str(exc)) from exc
        for path in children:
            if path.is_dir():
                pending.append(path)
            elif path.is_file():
                relative = path.relative_to(root).as_posix()
                digests[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def _finite_pack_files(root: Path) -> tuple[Path, ...]:
    """Enumerate every file under the staged copy, rejecting symlinks."""

    files: list[Path] = []
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            children = finite_children(current)
        except PackBoundaryError as exc:
            raise PackAuthoringError(str(exc)) from exc
        for path in children:
            if path.is_dir():
                pending.append(path)
            elif path.is_file():
                files.append(path)
    if len(files) > _MAX_FILES:
        raise PackAuthoringError("authored Pack exceeds the file inventory limit")
    return tuple(sorted(files))


def _load_source(root: Path) -> Mapping[str, Any]:
    """Parse and structurally validate the authored Pack source."""

    source_path = root / SOURCE_FILENAME
    if source_path.is_symlink() or not source_path.is_file():
        raise PackAuthoringError(f"{SOURCE_FILENAME} is required")
    try:
        source = strict_loads(
            source_path.read_bytes(), max_bytes=_MAX_SOURCE_BYTES
        )
    except ProtocolError as exc:
        raise PackAuthoringError(f"{SOURCE_FILENAME} is malformed: {exc}") from exc
    if not isinstance(source, dict):
        raise PackAuthoringError(f"{SOURCE_FILENAME} must be an object")
    allowed = {
        "source_api_version",
        "pack_id",
        "version",
        "display_name",
        "description",
        "publisher_id",
        "contracts",
        "functions",
        "capabilities",
        "contract_dependencies",
        "pack_dependencies",
        "network",
        "secrets",
        "approval_policy",
        "workspace_boundary",
    }
    unknown = sorted(set(source) - allowed)
    if unknown:
        raise PackAuthoringError(
            f"{SOURCE_FILENAME} carries unknown fields: {', '.join(unknown)}"
        )
    if source.get("source_api_version") != SOURCE_API_VERSION:
        raise PackAuthoringError(
            f"{SOURCE_FILENAME} must declare {SOURCE_API_VERSION}"
        )
    return source


def _render_documents(
    source: Mapping[str, Any],
    staged: Path,
    files: tuple[Path, ...],
) -> dict[str, dict[str, Any]]:
    """Build the four canonical v4 documents from validated source."""

    pack_id = _canonical_id(source.get("pack_id"), "pack_id")
    if staged.name != pack_id:
        raise PackAuthoringError(
            f"staged Pack directory name must equal pack_id {pack_id!r}"
        )
    version = _semver(source.get("version"), "version")
    display_name = _bounded_text(source.get("display_name"), "display_name")
    description = source.get("description")
    if description is not None:
        description = _bounded_text(description, "description")
    publisher_id = source.get("publisher_id")
    if publisher_id is not None:
        publisher_id = _canonical_id(publisher_id, "publisher_id")

    contracts_source = _sequence(source.get("contracts"), "contracts")
    functions_source = _sequence(source.get("functions"), "functions")
    if not 1 <= len(contracts_source) <= _MAX_CONTRACTS:
        raise PackAuthoringError("authored Pack requires 1-16 contracts")
    if not 1 <= len(functions_source) <= _MAX_FUNCTIONS:
        raise PackAuthoringError("authored Pack requires 1-16 functions")

    contracts = [_render_contract(item, pack_id) for item in contracts_source]
    contract_by_id = {item["contract_id"]: item for item in contracts}
    if len(contract_by_id) != len(contracts):
        raise PackAuthoringError("duplicate contract_id in authored Pack")
    operation_contracts: dict[str, Mapping[str, Any]] = {}
    for contract in contracts:
        for operation in contract["operations"]:
            operation_id = operation["operation_id"]
            if operation_id in operation_contracts:
                raise PackAuthoringError(
                    f"duplicate operation_id across contracts: {operation_id}"
                )
            operation_contracts[operation_id] = contract

    implementations = {
        function["implementation_path"]
        for function in functions_source
        if isinstance(function, Mapping)
        and isinstance(function.get("implementation_path"), str)
    }
    artifact_files = _artifact_entries(staged, files, implementations)
    functions = [
        _render_function(item, staged, operation_contracts)
        for item in functions_source
    ]
    if len({item["id"] for item in functions}) != len(functions):
        raise PackAuthoringError("duplicate function_id in authored Pack")
    claimed: dict[str, int] = {}
    for function in functions:
        for operation_id in function["operations"]:
            if operation_id in claimed:
                raise PackAuthoringError(
                    f"operation is implemented by two functions: {operation_id}"
                )
            claimed[operation_id] = 1
    unimplemented = sorted(set(operation_contracts) - set(claimed))
    if unimplemented:
        raise PackAuthoringError(
            "contract operations lack an implementing function: "
            + ", ".join(unimplemented)
        )

    requirements = _render_requirements(source)
    source_identity = canonical_digest(source)
    provenance = _provenance(source_identity, artifact_files, implementations)

    contracts_document = {
        "catalog_api_version": "io.tobkiri.pack-contract-catalog.v4",
        "pack_id": pack_id,
        "source_identity": source_identity,
        "contracts": [_contract_document(item, provenance) for item in contracts],
    }
    executables_unsigned = {
        "catalog_api_version": "io.tobkiri.executable-catalog.v4",
        "pack_id": pack_id,
        "source_identity": source_identity,
        "variants": [_variant_document(function) for function in functions],
    }
    executables = {
        **executables_unsigned,
        "catalog_digest": canonical_digest(executables_unsigned),
    }

    artifacts = sorted(
        (
            *artifact_files,
            {
                "path": "executables.v4.json",
                "digest": _text_digest(_json_text(executables)),
                "kind": "sidecar",
            },
        ),
        key=lambda item: item["path"],
    )
    artifact_set_digest = canonical_digest(artifacts)
    manifest: dict[str, Any] = {
        "pack_api_version": "io.tobkiri.pack.v4",
        "pack": {
            "id": pack_id,
            "version": version,
            "kind": "normal_sandbox",
            "artifact_digest": artifact_set_digest,
            "display_name": display_name,
            **({"description": description} if description else {}),
            **({"publisher_id": publisher_id} if publisher_id else {}),
        },
        "functions": [_manifest_function(item) for item in functions],
        "contracts": [
            {
                "contract_id": contract["contract_id"],
                "revision_digest": contract["revision_digest"],
                "operations": [
                    operation["operation_id"]
                    for operation in contract["operations"]
                ],
            }
            for contract in contracts
        ],
        "artifacts": artifacts,
        "requirements": requirements,
        "operation_catalog": [
            {
                "operation_id": operation["operation_id"],
                "owner": pack_id,
                "contract_reference": contract["contract_id"],
                "provider_id": contract["provider_semantics"]["provider_id"],
                "source_kind": "canonical_v4_contract",
                "effect_ceiling": operation["effect_ceiling"],
            }
            for contract in contracts
            for operation in contract["operations"]
        ],
        "provider_catalog": [
            {
                "provider_id": contract["provider_semantics"]["provider_id"],
                "owner": pack_id,
                "contract_reference": contract["contract_id"],
                "operations": [
                    operation["operation_id"]
                    for operation in contract["operations"]
                ],
            }
            for contract in contracts
        ],
        "integrity": {
            "source_identity": source_identity,
            "artifact_set_digest": artifact_set_digest,
            "contract_catalog_digest": _text_digest(_json_text(contracts_document)),
        },
        "provenance": provenance,
        "migration": {
            "compatibility": "none",
            "legacy_ids": [],
            "removal_wave": 0,
            "sunset_at": "2099-12-31",
        },
    }
    manifest_text = _json_text(manifest)
    contract_text = _json_text(contracts_document)
    index_entries = [
        {
            "path": "pack.v4.json",
            "digest": _text_digest(manifest_text),
            "role": "canonical_manifest",
        },
        {
            "path": "contracts.v4.json",
            "digest": _text_digest(contract_text),
            "role": "contract_catalog",
        },
    ]
    index_entries.extend(
        {
            "path": item["path"],
            "digest": item["digest"],
            "role": _index_role(item),
        }
        for item in artifacts
    )
    unsigned_index = {
        "index_api_version": "io.tobkiri.pack-artifact-index.v4",
        "pack_id": pack_id,
        "source_identity": source_identity,
        "artifacts": index_entries,
        "artifact_set_digest": artifact_set_digest,
    }
    index = {
        **unsigned_index,
        "integrity_seal": {
            "algorithm": "sha256-canonical-v1",
            "signed_digest": canonical_digest(unsigned_index),
        },
    }
    return {
        "pack.v4.json": manifest,
        "contracts.v4.json": contracts_document,
        "artifact-index.v4.json": index,
        "executables.v4.json": executables,
    }


def _render_contract(item: Any, pack_id: str) -> dict[str, Any]:
    """Normalize one authored Contract block."""

    label = "contracts[]"
    contract = _mapping(item, label)
    _known_keys(
        contract,
        {"contract_id", "version", "provider_id", "status", "security", "operations"},
        label,
    )
    contract_id = _contract_id(contract.get("contract_id"), f"{label}.contract_id")
    version = _semver(contract.get("version"), f"{label}.version")
    provider_id = _canonical_id(
        contract.get("provider_id"), f"{label}.provider_id"
    )
    status = _enum(
        contract.get("status", "accepted"), _SUPPORTED_CONTRACT_STATUSES,
        f"{label}.status",
    )
    security = _enum(
        contract.get("security", "restricted"), _SUPPORTED_SECURITY,
        f"{label}.security",
    )
    raw_operations = _sequence(contract.get("operations"), f"{label}.operations")
    if not 1 <= len(raw_operations) <= _MAX_OPERATIONS:
        raise PackAuthoringError(f"{label}.operations requires 1-64 entries")
    operations = [
        _render_operation(item, f"{label}.operations[]")
        for item in raw_operations
    ]
    if len({op["operation_id"] for op in operations}) != len(operations):
        raise PackAuthoringError(f"{label} contains a duplicate operation_id")
    revision_digest = canonical_digest(
        {
            "contract_id": contract_id,
            "version": version,
            "operations": [
                {
                    "operation_id": operation["operation_id"],
                    "input_schema_digest": canonical_digest(
                        operation["input_schema"]
                    ),
                    "output_schema_digest": canonical_digest(
                        operation["output_schema"]
                    ),
                    "error_schema_digest": canonical_digest(
                        operation["error_schema"]
                    ),
                    "effect_ceiling": operation["effect_ceiling"],
                    "scope_semantics": operation["scope_semantics"],
                    "idempotency": operation["idempotency"],
                    "timeout_default_ms": operation["timeout_default_ms"],
                    "timeout_hard_max_ms": operation["timeout_hard_max_ms"],
                }
                for operation in operations
            ],
        }
    )
    return {
        "contract_id": contract_id,
        "version": version,
        "status": status,
        "revision_digest": revision_digest,
        "operations": operations,
        "provider_semantics": {
            "provider_id": provider_id,
            "cardinality": "one",
            "security": security,
            "failure": "fail_closed",
            "isolation": "sandbox",
            "required_capabilities": [],
            "lifecycle": {
                "deprecated": status in {"deprecated", "sunset"},
                "introduced": version,
            },
        },
        "pack_id": pack_id,
    }


def _render_operation(item: Any, label: str) -> dict[str, Any]:
    """Normalize one authored Operation declaration."""

    operation = _mapping(item, label)
    _known_keys(
        operation,
        {
            "operation_id",
            "input_schema",
            "output_schema",
            "error_schema",
            "effect_class",
            "effect_ceiling",
            "scope_semantics",
            "idempotency",
            "timeout_default_ms",
            "timeout_hard_max_ms",
        },
        label,
    )
    operation_id = _canonical_id(operation.get("operation_id"), f"{label}.operation_id")
    input_schema = _json_schema(operation.get("input_schema"), f"{label}.input_schema")
    output_schema = _json_schema(
        operation.get("output_schema"), f"{label}.output_schema"
    )
    error_schema = operation.get("error_schema")
    error_schema = (
        _json_schema(error_schema, f"{label}.error_schema")
        if error_schema is not None
        else dict(_DEFAULT_ERROR_SCHEMA)
    )
    effect_class = _enum(
        operation.get("effect_class", "pure"), _SUPPORTED_EFFECT_CLASSES,
        f"{label}.effect_class",
    )
    scope_semantics = _enum(
        operation.get("scope_semantics", "declarative"),
        _SUPPORTED_SCOPE_SEMANTICS,
        f"{label}.scope_semantics",
    )
    effect_ceiling = operation.get("effect_ceiling", [])
    if (
        not isinstance(effect_ceiling, list)
        or len(effect_ceiling) != len(set(effect_ceiling))
        or any(
            not isinstance(item, str) or not item or len(item) > 256
            for item in effect_ceiling
        )
    ):
        raise PackAuthoringError(f"{label}.effect_ceiling must be unique strings")
    timeout_default = _timeout(
        operation.get("timeout_default_ms", 120_000), f"{label}.timeout_default_ms"
    )
    timeout_hard = _timeout(
        operation.get("timeout_hard_max_ms", 300_000),
        f"{label}.timeout_hard_max_ms",
    )
    if timeout_hard < timeout_default:
        raise PackAuthoringError(
            f"{label}.timeout_hard_max_ms must be >= timeout_default_ms"
        )
    idempotency = _idempotency(operation.get("idempotency", "none"), label)
    return {
        "operation_id": operation_id,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "error_schema": error_schema,
        "effect_class": effect_class,
        "effect_ceiling": sorted(str(item) for item in effect_ceiling),
        "scope_semantics": scope_semantics,
        "idempotency": idempotency,
        "timeout_default_ms": timeout_default,
        "timeout_hard_max_ms": timeout_hard,
    }


def _json_schema(value: Any, label: str) -> dict[str, Any]:
    """Require a canonical-JSON object that is a valid JSON Schema."""

    if not isinstance(value, dict) or not value:
        raise PackAuthoringError(
            f"{label} must be a non-empty JSON Schema object"
        )
    try:
        canonical_json(value)
    except ProtocolError as exc:
        raise PackAuthoringError(
            f"{label} is not canonical JSON: {exc}"
        ) from exc
    try:
        Draft202012Validator.check_schema(value)
    except Exception as exc:
        raise PackAuthoringError(
            f"{label} is not a valid Draft 2020-12 schema: {exc}"
        ) from exc
    return dict(value)


def _idempotency(value: Any, label: str) -> dict[str, Any]:
    """Normalize an idempotency declaration into the Contract object form."""

    if isinstance(value, str):
        mode = _enum(value, _SUPPORTED_IDEMPOTENCY, f"{label}.idempotency")
        return {"mode": mode}
    declaration = _mapping(value, f"{label}.idempotency")
    _known_keys(
        declaration,
        {"mode", "key_namespace", "reconcile_operation_id", "retention_seconds"},
        f"{label}.idempotency",
    )
    mode = _enum(
        declaration.get("mode"), _SUPPORTED_IDEMPOTENCY, f"{label}.idempotency.mode"
    )
    normalized: dict[str, Any] = {"mode": mode}
    key_namespace = declaration.get("key_namespace")
    if key_namespace is not None:
        if not isinstance(key_namespace, str) or not key_namespace:
            raise PackAuthoringError(f"{label}.idempotency.key_namespace is invalid")
        normalized["key_namespace"] = key_namespace
    reconcile = declaration.get("reconcile_operation_id")
    if reconcile is not None:
        normalized["reconcile_operation_id"] = _canonical_id(
            reconcile, f"{label}.idempotency.reconcile_operation_id"
        )
    retention = declaration.get("retention_seconds")
    if retention is not None:
        if (
            not isinstance(retention, int)
            or isinstance(retention, bool)
            or retention < 0
        ):
            raise PackAuthoringError(
                f"{label}.idempotency.retention_seconds is invalid"
            )
        normalized["retention_seconds"] = retention
    return normalized


def _render_function(
    item: Any,
    staged: Path,
    operation_contracts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Normalize one authored Function and digest-verify its implementation."""

    label = "functions[]"
    function = _mapping(item, label)
    _known_keys(
        function,
        {"function_id", "implementation_path", "operations", "role", "isolation"},
        label,
    )
    function_id = _canonical_id(
        function.get("function_id"), f"{label}.function_id"
    )
    implementation_path = _safe_relative_path(
        function.get("implementation_path"), f"{label}.implementation_path"
    )
    role = _enum(function.get("role", "pure"), _SUPPORTED_ROLES, f"{label}.role")
    isolation = _enum(
        function.get("isolation", "pack_vm"), _SUPPORTED_ISOLATION,
        f"{label}.isolation",
    )
    raw_operations = _sequence(function.get("operations"), f"{label}.operations")
    operations = [
        _canonical_id(item, f"{label}.operations[]") for item in raw_operations
    ]
    if not operations or len(set(operations)) != len(operations):
        raise PackAuthoringError(f"{label}.operations requires unique ids")
    contract_ids = {
        operation_contracts[operation]["contract_id"]
        if operation in operation_contracts
        else None
        for operation in operations
    }
    if None in contract_ids:
        missing = sorted(
            operation for operation in operations
            if operation not in operation_contracts
        )
        raise PackAuthoringError(
            f"{label} references unknown operations: {', '.join(missing)}"
        )
    if len(contract_ids) != 1:
        raise PackAuthoringError(
            f"{label} must implement operations from exactly one Contract"
        )
    contract = operation_contracts[operations[0]]
    contract_operations = {
        operation["operation_id"]: operation for operation in contract["operations"]
    }
    implementation = staged / implementation_path
    resolved = implementation.resolve()
    staged_resolved = staged.resolve()
    if staged_resolved != resolved and staged_resolved not in resolved.parents:
        raise PackAuthoringError(
            f"{label}.implementation_path escapes the Pack root"
        )
    if implementation.is_symlink() or not implementation.is_file():
        raise PackAuthoringError(
            f"{label}.implementation_path must be a regular source file"
        )
    if implementation.stat().st_size > _MAX_IMPLEMENTATION_BYTES:
        raise PackAuthoringError("PackVM implementation exceeds the size limit")
    content = implementation.read_bytes()
    implementation_digest = "sha256:" + hashlib.sha256(content).hexdigest()
    _check_python_implementation(implementation_path, content)

    variant_operations = [
        {
            "contract_id": contract["contract_id"],
            "contract_version": contract["version"],
            "revision_digest": contract["revision_digest"],
            "operation_id": operation_id,
            "input_schema": contract_operations[operation_id]["input_schema"],
            "output_schema": contract_operations[operation_id]["output_schema"],
            "error_schema": contract_operations[operation_id]["error_schema"],
            "effect_class": contract_operations[operation_id]["effect_class"],
            "timeout_default_ms": contract_operations[operation_id][
                "timeout_default_ms"
            ],
            "timeout_hard_max_ms": contract_operations[operation_id][
                "timeout_hard_max_ms"
            ],
            "idempotency": contract_operations[operation_id]["idempotency"]["mode"],
        }
        for operation_id in operations
    ]
    return {
        "id": function_id,
        "implementation_path": implementation_path,
        "implementation_digest": implementation_digest,
        "contract_revision_digest": contract["revision_digest"],
        "operations": operations,
        "role": role,
        "isolation": isolation,
        "variant_id": f"{function_id}.python",
        "variant_operations": variant_operations,
    }


def _check_python_implementation(path: str, content: bytes) -> None:
    """Statically check the PackVM ABI without executing author code.

    Accepted export forms are a synchronous ``def tobkiri_packvm_invoke``
    or a ``tobkiri_packvm_invoke = <lambda>`` assignment whose signature can
    accept the production call ``(operation_id, payload)`` — two positional
    arguments.  ``async def``, decorated definitions, and any other
    assignment are rejected because this static check cannot prove them
    callable with the production signature.
    """

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PackAuthoringError(
            f"PackVM implementation is not UTF-8: {path}"
        ) from exc
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        raise PackAuthoringError(
            f"PackVM implementation has a syntax error: {path}:{exc.lineno}"
        ) from exc

    binding: tuple[str, ast.stmt] | None = None
    for declaration in tree.body:
        if (
            isinstance(declaration, (ast.FunctionDef, ast.AsyncFunctionDef))
            and declaration.name == PACKVM_ENTRYPOINT
        ):
            binding = ("async" if isinstance(declaration, ast.AsyncFunctionDef)
                       else "def", declaration)
        elif isinstance(declaration, (ast.Assign, ast.AnnAssign)):
            targets = (
                declaration.targets if isinstance(declaration, ast.Assign) else (declaration.target,)
            )
            if any(
                isinstance(target, ast.Name) and target.id == PACKVM_ENTRYPOINT
                for target in targets
            ):
                binding = ("assign", declaration)
    if binding is None:
        raise PackAuthoringError(
            f"PackVM implementation does not export {PACKVM_ENTRYPOINT}: {path}"
        )
    kind, bound_node = binding
    if kind == "async":
        raise PackAuthoringError(
            f"PackVM {PACKVM_ENTRYPOINT} must be a synchronous def: "
            f"{path}:{bound_node.lineno}"
        )
    if kind == "assign":
        value = getattr(bound_node, "value", None)
        if not isinstance(value, ast.Lambda):
            raise PackAuthoringError(
                f"PackVM {PACKVM_ENTRYPOINT} assignment must bind a lambda "
                f"(the guest calls it with two positional arguments): "
                f"{path}:{bound_node.lineno}"
            )
        _check_two_positional_signature(value.args, path, bound_node.lineno)
    else:
        assert isinstance(bound_node, ast.FunctionDef)
        if bound_node.decorator_list:
            raise PackAuthoringError(
                f"decorated PackVM {PACKVM_ENTRYPOINT} cannot be statically "
                f"verified as callable: {path}:{bound_node.lineno}"
            )
        _check_two_positional_signature(bound_node.args, path, bound_node.lineno)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_import_root(alias.name, path, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise PackAuthoringError(
                    f"PackVM implementation uses a relative import "
                    f"({path}:{node.lineno}) which the staged interpreter "
                    "cannot resolve"
                )
            if node.module is not None:
                _check_import_root(node.module, path, node.lineno)


def _check_two_positional_signature(
    args: ast.arguments, path: str, line: int
) -> None:
    """Require a signature callable as ``(operation_id, payload)``."""

    total_positional = len(args.posonlyargs) + len(args.args)
    required_positional = total_positional - len(args.defaults)
    required_kwonly = sum(
        1 for default in args.kw_defaults if default is None
    )
    if required_positional > 2 or required_kwonly:
        raise PackAuthoringError(
            f"PackVM {PACKVM_ENTRYPOINT} requires arguments the guest "
            f"never passes ({path}:{line}); the ABI is "
            "(operation_id, payload)"
        )
    if args.vararg is None and total_positional < 2:
        raise PackAuthoringError(
            f"PackVM {PACKVM_ENTRYPOINT} must accept two positional "
            f"arguments ({path}:{line}); the ABI is (operation_id, payload)"
        )


def _check_import_root(module: str, path: str, line: int) -> None:
    root = module.split(".", 1)[0]
    if root not in sys_stdlib_names() and root not in _EXTRA_STDLIB_ROOTS:
        raise PackAuthoringError(
            f"PackVM implementation imports non-stdlib module "
            f"{module!r} ({path}:{line}); the isolated guest interpreter "
            "cannot resolve it"
        )


def sys_stdlib_names() -> frozenset[str]:
    import sys

    names = getattr(sys, "stdlib_module_names", None)
    if names is None:  # pragma: no cover - Python >= 3.10 guarantees it
        raise PackAuthoringError("stdlib inventory is unavailable")
    return frozenset(names)


def _artifact_entries(
    staged: Path,
    files: tuple[Path, ...],
    implementations: set[str],
) -> list[dict[str, Any]]:
    """List the complete deterministic artifact inventory for the manifest."""

    entries: list[dict[str, Any]] = []
    for path in files:
        relative = path.relative_to(staged).as_posix()
        if relative in GENERATED_FILENAMES:
            continue
        if path.stat().st_size > _MAX_FILE_BYTES:
            raise PackAuthoringError(
                f"Pack file exceeds the size limit: {relative}"
            )
        content = path.read_bytes()
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        if relative in implementations:
            entries.append(
                {
                    "path": relative,
                    "digest": digest,
                    "kind": "executable",
                    "platform": "any",
                    "entrypoint": PACKVM_ENTRYPOINT,
                }
            )
        elif relative == SOURCE_FILENAME:
            entries.append({"path": relative, "digest": digest, "kind": "sidecar"})
        elif relative.endswith(".schema.json"):
            entries.append({"path": relative, "digest": digest, "kind": "schema"})
        else:
            entries.append({"path": relative, "digest": digest, "kind": "asset"})
    return entries


def _render_requirements(source: Mapping[str, Any]) -> dict[str, Any]:
    """Pass through declared Normal Sandbox requirements verbatim."""

    capabilities = _string_list(
        source.get("capabilities", []), "capabilities"
    )
    secrets = source.get("secrets", [])
    if not isinstance(secrets, list):
        raise PackAuthoringError("secrets must be a list")
    try:
        if len({canonical_json(item) for item in secrets}) != len(secrets):
            raise PackAuthoringError("secrets must not contain duplicates")
    except ProtocolError as exc:
        raise PackAuthoringError(
            f"secrets entries must be canonical JSON: {exc}"
        ) from exc
    pack_dependencies = source.get("pack_dependencies", {})
    if not isinstance(pack_dependencies, Mapping):
        raise PackAuthoringError("pack_dependencies must be an object")
    normalized_dependencies: dict[str, str] = {}
    for name, dependency in pack_dependencies.items():
        dependency_id = _canonical_id(name, "pack_dependencies key")
        if not isinstance(dependency, str) or not dependency or len(dependency) > 256:
            raise PackAuthoringError(
                "pack_dependencies must map Pack ids to version ranges"
            )
        normalized_dependencies[dependency_id] = dependency
    contract_dependencies = []
    for item in _sequence(
        source.get("contract_dependencies", []), "contract_dependencies"
    ):
        dependency = _mapping(item, "contract_dependencies[]")
        _known_keys(
            dependency,
            {
                "contract_id",
                "version_range",
                "cardinality",
                "optional",
                "instance_key",
                "operations",
            },
            "contract_dependencies[]",
        )
        entry = {
            "contract_id": _contract_id(
                dependency.get("contract_id"),
                "contract_dependencies[].contract_id",
            ),
            "version_range": _bounded_text(
                dependency.get("version_range"),
                "contract_dependencies[].version_range",
            ),
            "cardinality": _enum(
                dependency.get("cardinality", "one"),
                _SUPPORTED_CARDINALITY,
                "contract_dependencies[].cardinality",
            ),
            "optional": _bool(
                dependency.get("optional", False),
                "contract_dependencies[].optional",
            ),
        }
        if "instance_key" in dependency:
            entry["instance_key"] = _bounded_text(
                dependency["instance_key"],
                "contract_dependencies[].instance_key",
            )
        if "operations" in dependency:
            entry["operations"] = [
                _canonical_id(item, "contract_dependencies[].operations[]")
                for item in _sequence(
                    dependency["operations"],
                    "contract_dependencies[].operations",
                )
            ]
            if not entry["operations"]:
                raise PackAuthoringError(
                    "contract_dependencies[].operations must not be empty"
                )
        contract_dependencies.append(entry)
    network = source.get("network", {})
    if not isinstance(network, Mapping):
        raise PackAuthoringError("network must be an object")
    try:
        canonical_json(dict(network))
    except ProtocolError as exc:
        raise PackAuthoringError(
            f"network must be canonical JSON: {exc}"
        ) from exc
    normalized_network = dict(network)
    normalized_network["allowed_domains"] = _string_list(
        network.get("allowed_domains", []), "network.allowed_domains"
    )
    allowed_ports = network.get("allowed_ports", [])
    if not isinstance(allowed_ports, list) or any(
        not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535
        for port in allowed_ports
    ):
        raise PackAuthoringError("network.allowed_ports must be TCP port numbers")
    normalized_network["allowed_ports"] = sorted(
        set(int(port) for port in allowed_ports)
    )
    approval_policy = _enum(
        source.get("approval_policy", "none"),
        _SUPPORTED_APPROVAL_POLICY,
        "approval_policy",
    )
    workspace_boundary = _enum(
        source.get("workspace_boundary", "pack_local"),
        _SUPPORTED_WORKSPACE_BOUNDARY,
        "workspace_boundary",
    )
    return {
        "pack_dependencies": dict(sorted(normalized_dependencies.items())),
        "contract_dependencies": contract_dependencies,
        "capabilities": capabilities,
        "network": normalized_network,
        "secrets": secrets,
        "execution_boundary": "sandbox",
        "approval_policy": approval_policy,
        "workspace_boundary": workspace_boundary,
    }


def _provenance(
    source_identity: str,
    artifact_files: Sequence[Mapping[str, Any]],
    implementations: set[str],
) -> dict[str, Any]:
    evidence = [
        {
            "path": SOURCE_FILENAME,
            "rule_id": "external-pack-source",
            "digest": source_identity,
        }
    ]
    for item in artifact_files:
        if item["path"] in implementations:
            evidence.append(
                {
                    "path": item["path"],
                    "rule_id": "external-pack-implementation",
                    "digest": item["digest"],
                }
            )
    return {
        "schema": "io.tobkiri.provenance.v1",
        "source_kind": "external",
        "source_path": SOURCE_FILENAME,
        "source_digest": source_identity,
        "repository_commit": "working-tree",
        "repository_tree": source_identity.removeprefix("sha256:"),
        "generator": GENERATOR_ID,
        "generator_version": GENERATOR_VERSION,
        "normative": False,
        "evidence": evidence,
    }


def _contract_document(
    contract: Mapping[str, Any], provenance: Mapping[str, Any]
) -> dict[str, Any]:
    schema_catalog: dict[str, Any] = {}
    operations = []
    for operation in contract["operations"]:
        input_digest = canonical_digest(operation["input_schema"])
        output_digest = canonical_digest(operation["output_schema"])
        error_digest = canonical_digest(operation["error_schema"])
        schema_catalog[input_digest] = operation["input_schema"]
        schema_catalog[output_digest] = operation["output_schema"]
        schema_catalog[error_digest] = operation["error_schema"]
        operations.append(
            {
                "operation_id": operation["operation_id"],
                "input_schema_digest": input_digest,
                "output_schema_digest": output_digest,
                "error_schema_digest": error_digest,
                "effect_ceiling": operation["effect_ceiling"],
                "scope_semantics": operation["scope_semantics"],
                "idempotency": operation["idempotency"],
                "timeout_default_ms": operation["timeout_default_ms"],
                "timeout_hard_max_ms": operation["timeout_hard_max_ms"],
            }
        )
    return {
        "contract_api_version": "io.tobkiri.contract.v4",
        "contract_id": contract["contract_id"],
        "version": contract["version"],
        "revision_digest": contract["revision_digest"],
        "owner": contract["pack_id"],
        "status": contract["status"],
        "operations": operations,
        "schema_catalog": schema_catalog,
        "provider_semantics": contract["provider_semantics"],
        "provenance": provenance,
    }


def _variant_document(function: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "variant_id": function["variant_id"],
        "function_id": function["id"],
        "implementation_path": function["implementation_path"],
        "implementation_digest": function["implementation_digest"],
        "execution_kind": "pack_vm",
        "platform": "any",
        "architecture": "any",
        "runtime_abi": PACKVM_RUNTIME_ABI,
        "backend": PACKVM_BACKEND,
        "materialization_mode": PACKVM_MATERIALIZATION,
        "execution_domain_profile": PACKVM_DOMAIN_PROFILE,
        "operations": function["variant_operations"],
    }


def _manifest_function(function: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": function["id"],
        "implementation_digest": function["implementation_digest"],
        "contract_revision_digest": function["contract_revision_digest"],
        "operations": function["operations"],
        "role": function["role"],
        "isolation": function["isolation"],
    }


def _index_role(artifact: Mapping[str, Any]) -> str:
    kind = artifact["kind"]
    return "runtime" if kind == "executable" else kind


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _text_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PackAuthoringError(f"{label} must be an object")
    return value


def _sequence(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise PackAuthoringError(f"{label} must be a list")
    return value


def _known_keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(str(key) for key in set(value) - allowed)
    if unknown:
        raise PackAuthoringError(
            f"{label} carries unknown fields: {', '.join(unknown)}"
        )


def _canonical_id(value: Any, label: str) -> str:
    try:
        return validate_canonical_id(str(value or ""), field=label)
    except ProtocolError as exc:
        raise PackAuthoringError(str(exc)) from exc


def _contract_id(value: Any, label: str) -> str:
    try:
        return validate_contract_id(str(value or ""), field=label)
    except ProtocolError as exc:
        raise PackAuthoringError(str(exc)) from exc


def _semver(value: Any, label: str) -> str:
    try:
        return validate_semver(str(value or ""), field=label)
    except ProtocolError as exc:
        raise PackAuthoringError(str(exc)) from exc


def _enum(value: Any, choices: Sequence[str], label: str) -> str:
    if value not in choices:
        raise PackAuthoringError(
            f"{label} must be one of {', '.join(choices)}"
        )
    return str(value)


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise PackAuthoringError(f"{label} must be a boolean")
    return value


def _timeout(value: Any, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= _MAX_TIMEOUT_MS
    ):
        raise PackAuthoringError(
            f"{label} must be an integer within 1..{_MAX_TIMEOUT_MS}"
        )
    return value


def _bounded_text(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_IDENTIFIER_TEXT
    ):
        raise PackAuthoringError(f"{label} must be a bounded non-empty string")
    return value


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item or len(item) > 256 for item in value
    ):
        raise PackAuthoringError(f"{label} must be a list of bounded strings")
    if len(set(value)) != len(value):
        raise PackAuthoringError(f"{label} must not contain duplicates")
    return sorted(str(item) for item in value)


def _safe_relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PackAuthoringError(f"{label} must be a relative path string")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or "." in relative.parts
        or "\\" in value
        or relative.as_posix() != value
        or not value.endswith(".py")
    ):
        raise PackAuthoringError(f"{label} is not a safe relative .py path")
    return value


__all__ = [
    "GENERATED_FILENAMES",
    "PACKVM_ENTRYPOINT",
    "PackAuthoringError",
    "SOURCE_API_VERSION",
    "SOURCE_FILENAME",
    "build_authored_pack",
]
