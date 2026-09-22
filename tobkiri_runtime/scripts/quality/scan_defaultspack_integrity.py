from __future__ import annotations

import argparse
import ast
import hashlib
import re
import sys
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
WEBAPP_ROOT = DEFAULTSPACK_ROOT / "webapp"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tobkiri_protocol.canonical import canonical_digest, strict_loads  # noqa: E402
from tobkiri_protocol.defaultspack_bundle_order import (  # noqa: E402
    canonical_defaultspack_bundle_entries,
)
from tobkiri_protocol.validation import validate_document  # noqa: E402


V4_DOCUMENT_SCHEMAS = {
    "pack.v4.json": "pack",
    "contracts.v4.json": "pack_contract_catalog",
    "artifact-index.v4.json": "pack_artifact_index",
    "executables.v4.json": "executable_catalog",
}
V4_BUNDLE_LOCK = "bundle.lock.json"
V4_BUNDLE_DEFAULTSPACK = "packs/defaultspack.pack.v4.json"
V4_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _failures() -> list[str]:
    return []


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_relative_path(value: object) -> str | None:
    """Return a canonical relative POSIX path or ``None`` for unsafe input."""
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts:
        return None
    if any(part in {"", ".", ".."} for part in path.parts):
        return None
    normalized = "/".join(path.parts)
    return normalized if normalized == value else None


def _safe_file(
    errors: list[str],
    root: Path,
    relative: object,
    label: str,
) -> Path | None:
    """Resolve one declared file without accepting traversal or symlinks."""
    relative_path = _safe_relative_path(relative)
    if relative_path is None:
        errors.append(f"{label} has an unsafe relative path: {relative!r}")
        return None
    candidate = root / relative_path
    if candidate.is_symlink():
        errors.append(f"{label} must not be a symlink: {relative_path}")
        return None
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve(strict=False)
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError:
        errors.append(f"{label} escapes its root: {relative_path}")
        return None
    if not candidate.is_file():
        errors.append(f"{label} is missing: {relative_path}")
        return None
    return candidate


def _load_v4_document(
    errors: list[str], root: Path, filename: str, schema: str
) -> dict[str, Any] | None:
    path = _safe_file(errors, root, filename, f"v4 document {filename}")
    if path is None:
        return None
    try:
        return validate_document(path.read_bytes(), schema)
    except Exception as exc:
        errors.append(f"invalid v4 document {filename}: {type(exc).__name__}: {exc}")
        return None


def _check_contract_integrity(
    errors: list[str], pack: dict[str, Any], contracts: dict[str, Any]
) -> None:
    """Check contract revisions and operation schema digests from v4 records."""
    manifest_contracts = {
        item["contract_id"]: item["revision_digest"]
        for item in pack.get("contracts", [])
        if isinstance(item, dict)
        and isinstance(item.get("contract_id"), str)
        and isinstance(item.get("revision_digest"), str)
    }
    catalog_contracts = {
        item["contract_id"]: item["revision_digest"]
        for item in contracts.get("contracts", [])
        if isinstance(item, dict)
        and isinstance(item.get("contract_id"), str)
        and isinstance(item.get("revision_digest"), str)
    }
    if manifest_contracts != catalog_contracts:
        errors.append("v4 Pack and contract catalog declarations disagree")

    for contract in contracts.get("contracts", []):
        if not isinstance(contract, dict):
            continue
        unsigned = {
            key: value
            for key, value in contract.items()
            if key not in {"revision_digest", "provenance"}
        }
        if contract.get("revision_digest") != canonical_digest(unsigned):
            errors.append(
                f"contract revision digest mismatch: {contract.get('contract_id', '<unknown>')}"
            )
        schema_catalog = contract.get("schema_catalog", {})
        if not isinstance(schema_catalog, dict):
            continue
        for operation in contract.get("operations", []):
            if not isinstance(operation, dict):
                continue
            for field in (
                "input_schema_digest",
                "output_schema_digest",
                "error_schema_digest",
            ):
                digest = operation.get(field)
                schema = schema_catalog.get(digest)
                if not isinstance(schema, dict) or canonical_digest(schema) != digest:
                    errors.append(
                        "operation schema digest mismatch: "
                        f"{contract.get('contract_id', '<unknown>')}"
                        f"/{operation.get('operation_id', '<unknown>')}:{field}"
                    )


def _check_artifact_index(
    errors: list[str],
    root: Path,
    pack: dict[str, Any],
    index: dict[str, Any],
    contracts_path: Path,
) -> set[str]:
    """Verify the finite artifact set and return declared runtime paths."""
    expected: dict[str, tuple[str, str]] = {
        "pack.v4.json": ("canonical_manifest", _sha256_file(root / "pack.v4.json")),
        "contracts.v4.json": ("contract_catalog", _sha256_file(contracts_path)),
    }
    runtime_paths: set[str] = set()
    for item in pack.get("artifacts", []):
        if not isinstance(item, dict):
            continue
        relative = item.get("path")
        relative_path = _safe_relative_path(relative)
        if relative_path is None:
            errors.append(f"v4 Pack artifact has an unsafe relative path: {relative!r}")
            continue
        if relative_path in expected:
            errors.append(f"v4 Pack artifact duplicates catalog file: {relative_path}")
            continue
        runtime_paths.add(relative_path)
        artifact_kind = str(item.get("kind") or "")
        expected_role = {
            "sidecar": "sidecar",
            "executable": "runtime",
        }.get(artifact_kind)
        if expected_role is None:
            errors.append(f"v4 Pack artifact has an unsupported kind: {relative_path}")
            continue
        expected[relative_path] = (expected_role, str(item.get("digest") or ""))

    actual_entries: dict[str, dict[str, Any]] = {}
    for item in index.get("artifacts", []):
        if not isinstance(item, dict):
            errors.append("artifact index contains a non-object entry")
            continue
        relative = item.get("path")
        relative_path = _safe_relative_path(relative)
        if relative_path is None:
            errors.append(f"artifact index has an unsafe relative path: {relative!r}")
            continue
        if relative_path in actual_entries:
            errors.append(f"artifact index contains a duplicate path: {relative_path}")
            continue
        actual_entries[relative_path] = item
        candidate = _safe_file(errors, root, relative_path, "artifact index entry")
        if candidate is None:
            continue
        expected_entry = expected.get(relative_path)
        if expected_entry is None:
            errors.append(f"artifact index contains an extra artifact: {relative_path}")
            continue
        expected_role, expected_digest = expected_entry
        if item.get("role") != expected_role:
            errors.append(f"artifact index role mismatch: {relative_path}")
        if item.get("digest") != expected_digest:
            errors.append(f"artifact index digest mismatch: {relative_path}")
        try:
            actual_digest = _sha256_file(candidate)
        except OSError as exc:
            errors.append(f"cannot hash artifact index entry {relative_path}: {exc}")
        else:
            if actual_digest != item.get("digest"):
                errors.append(f"artifact hash mismatch: {relative_path}")

    for relative_path in sorted(set(expected) - set(actual_entries)):
        errors.append(f"artifact index is missing an artifact: {relative_path}")

    if index.get("artifact_set_digest") != pack.get("integrity", {}).get(
        "artifact_set_digest"
    ):
        errors.append("artifact index artifact_set_digest disagrees with the v4 Pack")
    if index.get("artifact_set_digest") != canonical_digest(pack.get("artifacts", [])):
        errors.append("artifact index artifact_set_digest is not canonical")
    unsigned = {key: value for key, value in index.items() if key != "integrity_seal"}
    seal = index.get("integrity_seal")
    if not isinstance(seal, dict) or seal.get("signed_digest") != canonical_digest(unsigned):
        errors.append("artifact index integrity seal is invalid")
    return runtime_paths


def _check_unlisted_runtime_files(
    errors: list[str], root: Path, declared_runtime_paths: set[str]
) -> None:
    """Reject files added below the v4 runtime artifact namespace."""
    runtime_root = root / "runtime"
    if runtime_root.is_symlink():
        errors.append("v4 runtime artifact root must not be a symlink")
        return
    if not runtime_root.is_dir():
        return
    for path in runtime_root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            errors.append(f"unlisted runtime artifact is a symlink: {relative}")
        elif path.is_file() and relative not in declared_runtime_paths:
            errors.append(f"unlisted runtime artifact: {relative}")


def _check_executable_catalog(
    errors: list[str],
    root: Path,
    pack: dict[str, Any],
    executable: dict[str, Any],
) -> None:
    """Check executable variants against v4 functions and real implementation bytes."""
    unsigned = {key: value for key, value in executable.items() if key != "catalog_digest"}
    if executable.get("catalog_digest") != canonical_digest(unsigned):
        errors.append("executable catalog digest changed")

    functions = {
        item["id"]: item
        for item in pack.get("functions", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    seen_functions: set[str] = set()
    for variant in executable.get("variants", []):
        if not isinstance(variant, dict):
            continue
        function_id = variant.get("function_id")
        function = functions.get(function_id)
        if function is None:
            errors.append(f"executable catalog references unknown Function: {function_id}")
            continue
        if function_id in seen_functions:
            errors.append(f"executable catalog duplicates Function: {function_id}")
        seen_functions.add(str(function_id))
        relative = variant.get("implementation_path")
        candidate = _safe_file(errors, root, relative, "executable implementation")
        if candidate is None:
            continue
        actual_digest = _sha256_file(candidate)
        expected_digest = variant.get("implementation_digest")
        if actual_digest != expected_digest:
            errors.append(f"implementation hash mismatch: {relative}")
        if actual_digest != function.get("implementation_digest"):
            errors.append(f"Function implementation hash mismatch: {function_id}")

    if seen_functions != set(functions):
        missing = sorted(set(functions) - seen_functions)
        extra = sorted(seen_functions - set(functions))
        if missing:
            errors.append(f"executable catalog is missing Functions: {', '.join(missing)}")
        if extra:
            errors.append(f"executable catalog has extra Functions: {', '.join(extra)}")

    try:
        from tobkiri_host.artifact_compiler import compile_pack_root

        compile_pack_root(root)
    except Exception as exc:
        errors.append(
            "v4 executable catalog compilation failed: "
            f"{type(exc).__name__}: {exc}"
        )


def _check_bundle(
    errors: list[str], root: Path, pack: dict[str, Any]
) -> None:
    """Verify every byte named by the v4 bundle lock and reject extras."""
    bundle_root = root / "v4"
    if bundle_root.is_symlink() or not bundle_root.is_dir():
        errors.append("v4 bundle directory is missing or is a symlink")
        return
    lock_path = _safe_file(errors, bundle_root, V4_BUNDLE_LOCK, "v4 bundle lock")
    if lock_path is None:
        return
    try:
        lock = strict_loads(lock_path.read_bytes())
    except Exception as exc:
        errors.append(f"invalid v4 bundle lock: {type(exc).__name__}: {exc}")
        return
    if not isinstance(lock, dict) or set(lock) != {"schema", "entries"}:
        errors.append("v4 bundle lock has unknown or missing fields")
        return
    if lock.get("schema") != "io.tobkiri.defaultspack-bundle-lock.v1":
        errors.append("v4 bundle lock schema is not supported")
        return
    entries = lock.get("entries")
    if not isinstance(entries, list) or not entries:
        errors.append("v4 bundle lock entries must be a non-empty array")
        return
    try:
        canonical_entries = canonical_defaultspack_bundle_entries(entries)
    except (TypeError, ValueError) as exc:
        errors.append(f"v4 bundle lock entry contract failed: {exc}")
    else:
        if entries != canonical_entries:
            errors.append("v4 bundle lock order is not canonical")

    valid_kinds = {"pack", "base", "shell", "profile", "executable_catalog"}
    seen_paths: set[str] = set()
    bundle_documents: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "kind", "digest"}:
            errors.append("v4 bundle lock contains an invalid entry")
            continue
        relative = entry.get("path")
        relative_path = _safe_relative_path(relative)
        if relative_path is None or relative_path in seen_paths:
            errors.append(f"v4 bundle lock contains an invalid path: {relative!r}")
            continue
        seen_paths.add(relative_path)
        kind = entry.get("kind")
        if kind not in valid_kinds:
            errors.append(f"v4 bundle entry has an invalid kind: {relative_path}")
            continue
        expected_digest = entry.get("digest")
        if not isinstance(expected_digest, str) or V4_DIGEST_RE.fullmatch(expected_digest) is None:
            errors.append(f"v4 bundle entry has an invalid digest: {relative_path}")
            continue
        candidate = _safe_file(errors, bundle_root, relative_path, "v4 bundle entry")
        if candidate is None:
            continue
        actual_digest = _sha256_file(candidate)
        if actual_digest != expected_digest:
            errors.append(f"bundle artifact digest mismatch: {relative_path}")
        try:
            document = validate_document(candidate.read_bytes(), kind)
        except Exception as exc:
            errors.append(
                f"invalid v4 bundle document {relative_path}: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        identity_source = document.get("pack") if kind == "pack" else document
        identity_field = "id" if kind == "pack" else (
            "pack_id"
            if kind in {"base", "executable_catalog"}
            else "provider_id"
            if kind == "shell"
            else "profile_id"
        )
        identity = identity_source.get(identity_field) if isinstance(identity_source, dict) else None
        identity_key = (str(kind), str(identity))
        if identity_key in bundle_documents:
            errors.append(f"v4 bundle contains a duplicate identity: {kind}:{identity}")
        bundle_documents[identity_key] = document

    for (kind, identity), catalog in sorted(bundle_documents.items()):
        if kind != "executable_catalog":
            continue
        manifest = bundle_documents.get(("pack", identity))
        if manifest is None:
            errors.append(f"executable catalog has no bundled Pack manifest: {identity}")
            continue
        if catalog.get("source_identity") != manifest.get("integrity", {}).get(
            "source_identity"
        ):
            errors.append(f"executable catalog source identity is stale: {identity}")
        unsigned = {key: value for key, value in catalog.items() if key != "catalog_digest"}
        if catalog.get("catalog_digest") != canonical_digest(unsigned):
            errors.append(f"executable catalog digest is stale: {identity}")
        catalog_entries = [
            item
            for item in manifest.get("artifacts", [])
            if isinstance(item, dict) and item.get("path") == "executables.v4.json"
        ]
        if len(catalog_entries) != 1:
            errors.append(f"Pack manifest does not pin executable catalog: {identity}")
            continue
        catalog_path = next(
            (
                entry.get("path")
                for entry in entries
                if isinstance(entry, dict)
                and entry.get("kind") == "executable_catalog"
                and entry.get("path", "").endswith(
                    f"/{identity}.executables.v4.json"
                )
            ),
            None,
        )
        if not isinstance(catalog_path, str):
            errors.append(f"executable catalog lock entry is missing: {identity}")
            continue
        catalog_digest = _sha256_file(bundle_root / catalog_path)
        if catalog_entries[0].get("digest") != catalog_digest:
            errors.append(f"Pack executable catalog artifact pin is stale: {identity}")

    actual_paths: set[str] = set()
    for path in bundle_root.rglob("*"):
        relative = path.relative_to(bundle_root).as_posix()
        if path.is_symlink():
            errors.append(f"v4 bundle artifact must not be a symlink: {relative}")
        elif path.is_file() and relative != V4_BUNDLE_LOCK:
            actual_paths.add(relative)
    for relative in sorted(actual_paths - seen_paths):
        errors.append(f"v4 bundle contains an extra artifact: {relative}")
    for relative in sorted(seen_paths - actual_paths):
        errors.append(f"v4 bundle is missing an artifact: {relative}")

    defaultspack_entry = next(
        (entry for entry in entries if isinstance(entry, dict) and entry.get("path") == V4_BUNDLE_DEFAULTSPACK),
        None,
    )
    if defaultspack_entry is None:
        errors.append("v4 bundle lock is missing the defaultspack Pack")
    else:
        candidate = bundle_root / V4_BUNDLE_DEFAULTSPACK
        if candidate.is_file() and not candidate.is_symlink():
            try:
                if candidate.read_bytes() != (root / "pack.v4.json").read_bytes():
                    errors.append("bundled defaultspack Pack differs from pack.v4.json")
            except OSError as exc:
                errors.append(f"cannot compare bundled defaultspack Pack: {exc}")


def check_v4_integrity(
    errors: list[str], pack_root: Path = DEFAULTSPACK_ROOT, *, strict: bool = False
) -> None:
    """Verify Defaultspack solely from its canonical Pack v4 records.

    The check deliberately does not read a legacy ecosystem manifest, a
    function Registry, a compatibility allowlist, or an authority record.
    """
    root = pack_root.resolve()
    documents = {
        filename: _load_v4_document(errors, root, filename, schema)
        for filename, schema in V4_DOCUMENT_SCHEMAS.items()
    }
    pack = documents.get("pack.v4.json")
    contracts = documents.get("contracts.v4.json")
    index = documents.get("artifact-index.v4.json")
    executable = documents.get("executables.v4.json")
    if pack is None or contracts is None or index is None or executable is None:
        return

    pack_id = pack.get("pack", {}).get("id")
    if pack_id != "defaultspack":
        errors.append(f"v4 Pack id must be defaultspack: {pack_id!r}")
    document_pack_ids = {
        contracts.get("pack_id"),
        index.get("pack_id"),
        executable.get("pack_id"),
    }
    if document_pack_ids != {pack_id}:
        errors.append("v4 artifact documents disagree on Pack identity")

    source_identity = pack.get("integrity", {}).get("source_identity")
    document_source_identities = {
        contracts.get("source_identity"),
        index.get("source_identity"),
        executable.get("source_identity"),
    }
    if document_source_identities != {source_identity}:
        errors.append("v4 artifact documents disagree on source identity")
    if pack.get("pack", {}).get("artifact_digest") != pack.get("integrity", {}).get(
        "artifact_set_digest"
    ):
        errors.append("v4 Pack artifact_digest disagrees with artifact_set_digest")
    if pack.get("integrity", {}).get("artifact_set_digest") != canonical_digest(
        pack.get("artifacts", [])
    ):
        errors.append("v4 Pack artifact_set_digest is not canonical")
    if pack.get("integrity", {}).get("contract_catalog_digest") != _sha256_file(
        root / "contracts.v4.json"
    ):
        errors.append("v4 Pack contract_catalog_digest is stale")

    _check_contract_integrity(errors, pack, contracts)
    contracts_path = root / "contracts.v4.json"
    runtime_paths = _check_artifact_index(errors, root, pack, index, contracts_path)
    _check_executable_catalog(errors, root, pack, executable)
    if strict:
        _check_unlisted_runtime_files(errors, root, runtime_paths)
    _check_bundle(errors, root, pack)


def check_local_first_defaults(errors: list[str]) -> None:
    critical_files = [
        DEFAULTSPACK_ROOT / "domain" / "ai_client" / "model_runtime_settings.py",
        DEFAULTSPACK_ROOT / "domain" / "chat" / "store.py",
        DEFAULTSPACK_ROOT / "domain" / "frontend" / "registry.py",
        WEBAPP_ROOT / "src" / "App.tsx",
    ]
    forbidden_defaults = [
        'DEFAULT_CHAT_MODEL = "openrouter/',
        'preferred_model ?? "openrouter/',
        '"default": "openrouter/',
        'model_api_routes": "openrouter/',
    ]
    for path in critical_files:
        text = path.read_text(encoding="utf-8")
        for needle in forbidden_defaults:
            if needle in text:
                errors.append(
                    "cloud model remains in local default path: "
                    f"{path.relative_to(ROOT)} contains {needle}"
                )


def check_sensitive_guard(errors: list[str]) -> None:
    http_text = (DEFAULTSPACK_ROOT / "transport" / "http.py").read_text(encoding="utf-8")
    if "require_local_guard(" not in http_text:
        errors.append(
            "transport/http.py does not call require_local_guard for sensitive coding paths"
        )
    approval_text = (
        DEFAULTSPACK_ROOT / "blocks" / "coding" / "_approval.py"
    ).read_text(encoding="utf-8")
    for needle in (
        "hash_arguments",
        "verify_execution_token",
        "create_approval_request",
    ):
        if needle not in approval_text:
            errors.append(f"coding approval helper missing {needle}")


def check_python_syntax(errors: list[str], paths: Iterable[Path]) -> None:
    for path in paths:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"syntax error in {path.relative_to(ROOT)}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    errors = _failures()
    check_v4_integrity(errors, DEFAULTSPACK_ROOT, strict=args.strict)
    check_local_first_defaults(errors)
    check_sensitive_guard(errors)
    check_python_syntax(
        errors,
        [
            DEFAULTSPACK_ROOT / "domain" / "safety" / "approval.py",
            DEFAULTSPACK_ROOT / "domain" / "safety" / "audit.py",
            DEFAULTSPACK_ROOT / "domain" / "safety" / "local_guard.py",
            DEFAULTSPACK_ROOT / "blocks" / "coding" / "_approval.py",
        ],
    )
    if errors:
        print("defaultspack integrity scan failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("defaultspack integrity scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
