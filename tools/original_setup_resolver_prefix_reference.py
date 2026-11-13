"""Observe pinned original Setup through the Pack resolver's first lock read.

The CI-only fixture changes disk or environment at the real caller boundary.
Original Setup, Pack, catalog, and Authority code runs through the second ID.
Additional cases observe the live catalog, static bindings, and Port inputs.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import json
import os
import shutil
import sys
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from types import FrameType
from typing import Any
from unittest.mock import patch

from original_authority_reference import ORIGINAL_HEAD, error_graph, write_json
from original_catalog_prefix_reference import IGNORED, bind_resources, code_hashes
from original_publication_reference import filesystem
from original_setup_candidate_selection_reference import DENIED_CLASS, _lock_free
from original_setup_review_reference import check_original
from original_setup_shell_reference import observe_shell

FORMAT = "io.tobkiri.original-setup-resolver-prefix-reference.v1"
BUNDLE = Path("ecosystem/defaultspack/v4")
DEV_BUNDLE = Path("bundled/dev-defaults/v4")
MANIFEST = Path("runtime-resource-manifest.v1.json")
LOCK = "bundle.lock.json"
AUTHORITY = Path("authority/v4.sqlite3")
GUARD = AUTHORITY.with_name(".v4.sqlite3.lifecycle.lock")
EPOCH_REASON = "original setup resolver prefix fixture"
MISSING_PROFILE_ID = "missing-fixture-profile"
REQUESTED_BINDINGS_CASE = "source-profile-requested-bindings"
DYNAMIC_EDGES_CASE = "fresh-bundled-dynamic-edges"
VERIFIED_ARTIFACTS_CASE = "fresh-bundled-verified-artifacts"
PACK_LOCK_CASES = (
    "pack-lock-reread", "pack-lock-symlink", "pack-lock-missing",
)
RESOLVE_CASES = (VERIFIED_ARTIFACTS_CASE, *PACK_LOCK_CASES)
DYNAMIC_CASES = (DYNAMIC_EDGES_CASE, *RESOLVE_CASES)
BINDING_CASES = (REQUESTED_BINDINGS_CASE, *DYNAMIC_CASES)
LOOKUP_CASES = ("source-profile-present", "source-profile-missing", *BINDING_CASES)
REQUESTED_EDGE_FIELDS = (
    "caller_function_id", "target_provider_id", "contract_id", "operation_id",
)
CASES = (
    "packaged-genesis",
    "second-user-data-existing-authority",
    "authority-directory",
    "authority-corrupt-file",
    "second-manifest-invalid-json",
    "second-lock-unbound",
    "second-lock-rebound",
    "second-development-root-missing-lock",
    "second-development-root",
    *LOOKUP_CASES,
)
EXTRA_SOURCES = (
    "core_runtime/authority/v4_store.py",
    "core_runtime/authority/v4_models.py",
    "core_runtime/bootstrap/profile_registry.py",
    "core_runtime/env_compat.py",
    "tobkiri_protocol/canonical.py",
    "tobkiri_protocol/ids.py",
    "tobkiri_protocol/platform_artifact.py",
    "scripts/generate_packaged_defaultspack_v4_bundle.py",
)


class _SourceProfileBoundary(BaseException):
    """Stop at the exact original source Profile boundary for this case."""


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _source_hashes(runtime: Path) -> dict[str, str]:
    return {
        **code_hashes(runtime),
        **{
            name: hashlib.sha256((runtime / name).read_bytes()).hexdigest()
            for name in EXTRA_SOURCES
        },
    }


def _resolver_lines(path: Path) -> tuple[int, int, int, range]:
    tree = ast.parse(path.read_bytes(), filename=str(path))
    resolver = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_resolve_bootstrap_candidate"
    )
    [index] = [
        index
        for index, node in enumerate(resolver.body)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(name, ast.Name) and name.id == "source_profile"
            for name in node.targets
        )
        and ast.unparse(node.value) == "catalog.profiles.get(profile_id)"
    ]
    guard = resolver.body[index + 1]
    if not isinstance(guard, ast.If) or ast.unparse(guard.test) != "source_profile is None":
        raise AssertionError("original Profile lookup guard changed")
    [dynamic_index] = [
        i
        for i, node in enumerate(resolver.body)
        if i > index + 1
        and isinstance(node, ast.For)
        and ast.unparse(node.iter)
        == "runtime.dynamic_profile_edges(catalog, profile_id, additional_pack_ids)"
    ]
    artifacts = resolver.body[dynamic_index + 1]
    if not (
        isinstance(artifacts, ast.Assign)
        and [ast.unparse(target) for target in artifacts.targets] == ["verified_artifacts"]
        and isinstance(artifacts.value, ast.SetComp)
        and artifacts.end_lineno is not None
    ):
        raise AssertionError("original verified-artifact boundary changed")
    return (
        resolver.body[index].lineno,
        guard.lineno,
        resolver.body[dynamic_index].lineno,
        range(artifacts.lineno, artifacts.end_lineno + 1),
    )


def _state(roots: dict[str, Path]) -> dict[str, dict]:
    result = {label: filesystem(path) for label, path in roots.items()}
    for snapshot in result.values():
        for row in snapshot["files"]:
            if row["path"] == AUTHORITY.with_suffix(".key").as_posix():
                row.pop("sha256", None)
    return result


def _seed(case: str, roots: dict[str, Path], store_type: type) -> None:
    authority = roots["first"] / AUTHORITY
    if case == "second-user-data-existing-authority":
        authority = roots["second"] / AUTHORITY
        with store_type(authority, clock=lambda: 1000.0) as store:
            if store.advance_security_epoch(EPOCH_REASON) != 2:
                raise AssertionError("original Authority fixture did not reach epoch 2")
    elif case == "authority-directory":
        authority.mkdir(mode=0o700, parents=True)
    elif case == "authority-corrupt-file":
        # Preserve the original key and guard so construction reaches SQLite.
        with store_type(authority, clock=lambda: 1000.0):
            pass
        authority.write_bytes(b"fixed non-SQLite fixture\n" * 8)


def _change_at_entry(case: str, runtime: Path, second: Path, lock: bytes) -> None:
    """Change only fixture inputs after the first real bundled candidate capture."""
    if case == "second-user-data-existing-authority":
        os.environ["TOBKIRI_USER_DATA"] = str(second)
    elif case == "second-manifest-invalid-json":
        (runtime / MANIFEST).write_bytes(b"{")
    elif case in {"second-lock-unbound", "second-lock-rebound"}:
        changed = lock + b"\n"
        (runtime / BUNDLE / LOCK).write_bytes(changed)
        if case == "second-lock-rebound":
            manifest = json.loads((runtime / MANIFEST).read_bytes())
            [entry] = [
                row
                for row in manifest["entries"]
                if row["path"] == (BUNDLE / LOCK).as_posix()
            ]
            entry.update(
                size=len(changed), sha256=_digest(changed).removeprefix("sha256:")
            )
            write_json(runtime / MANIFEST, manifest)
    elif case in {"second-development-root", "second-development-root-missing-lock"}:
        os.environ.update(RUMI_ENVIRONMENT="development", RUMI_APP_DIR=str(runtime))
        if case == "second-development-root":
            (runtime / DEV_BUNDLE / LOCK).write_bytes(lock + b"\n")


def _observe(case: str, runtime: Path, roots: dict[str, Path]) -> dict:
    from core_runtime.authority.v4 import AuthorityStore, authority_digest
    from core_runtime.bootstrap import profile_capture as capture
    from core_runtime.bootstrap.profile_publication import ProfilePublicationJournal
    from core_runtime.bootstrap.profile_registry import bootstrap_review_catalog
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        install_defaultspack_profile_runtime,
    )
    from ecosystem.defaultspack.domain.profile_resolution import (
        _bundle_digest,
        resolve_default_profile,
    )
    from tobkiri_protocol.bundle_catalog import BundledCatalog
    from tobkiri_protocol.canonical import canonical_digest

    port = install_defaultspack_profile_runtime()
    packaged = runtime / BUNDLE
    original_lock = (packaged / LOCK).read_bytes()
    original_manifest = (runtime / MANIFEST).read_bytes()
    resolver = capture._resolve_bootstrap_candidate.__code__
    caller = capture.prepare_bootstrap_profile_review.__code__
    acquire = capture._bootstrap_review_candidate.__code__
    capture_path = runtime / "core_runtime/bootstrap/profile_capture.py"
    if Path(resolver.co_filename).resolve() != capture_path:
        raise AssertionError("resolver was not imported from the staged original")
    lookup, lookup_guard, dynamic_edges, verified_artifacts = _resolver_lines(capture_path)
    root_get = capture._user_data_root.__code__
    recovery = capture._recover_bootstrap_publication.__code__
    journal_exists = ProfilePublicationJournal.exists.__code__
    bundle_get = capture._bundle_root.__code__
    bundle_verify = capture._verify_installed_bundle_binding.__code__
    catalog_load = BundledCatalog.load.__func__.__code__
    candidate_select = bootstrap_review_catalog.__code__
    dynamic_port = type(port).dynamic_profile_edges.__code__
    resolve_port = type(port).resolve_profile.__code__
    resolve_pack = resolve_default_profile.__code__
    pack_lock_digest = _bundle_digest.__code__
    dynamic_source = runtime / "ecosystem/defaultspack/domain/profile_resolution.py"
    id_get = capture._bootstrap_profile_id.__code__
    read_bytes = Path.read_bytes.__code__
    is_symlink = Path.is_symlink.__code__
    is_file = Path.is_file.__code__
    authority_init = AuthorityStore.__init__.__code__
    context_close = AuthorityStore.close.__code__
    record_read = inspect.unwrap(AuthorityStore.security_epoch_record.fget).__code__
    call_names = {
        bundle_verify: "verify-packaged-binding",
        catalog_load: "load-catalog",
        candidate_select: "select-candidate",
        type(port).host_resource_root.__code__: "get-host-resource-root",
        type(port).bundled_profile_root.__code__: "get-packaged-bundle-root",
        type(port).bootstrap_profile_id.__code__: "get-raw-profile-id",
        authority_init: "construct-authority",
        AuthorityStore.__enter__.__code__: "enter-authority",
        inspect.unwrap(AuthorityStore.security_epoch.fget).__code__: "read-epoch",
        capture._authority_snapshot_digest.__code__: "existing-snapshot",
        record_read: "read-epoch-record",
        AuthorityStore.__exit__.__code__: "exit-authority",
        context_close: "close-authority",
        capture._genesis_authority_snapshot_digest.__code__: "genesis-snapshot",
    }
    events: dict[str, list[str]] = {"capture": [], "resolver": []}
    phase = "capture"
    captured: Any = None
    dynamic_result: Any = None
    resolve_inputs: dict[str, Any] | None = None
    before_dynamic_bindings: Any = None
    before_dynamic_rows: list[tuple[str, str]] = []
    facts: dict[str, Any] = {
        "user_data_roots": [],
        "bundle_roots": [],
        "lock_reads": [],
        "profile_ids": [],
        "recovery_discard_superseded": [],
        "publication_journal_exists": [],
    }
    root_labels = {path: label for label, path in roots.items()}
    caller_labels = {
        recovery: "publication-recovery",
        acquire: "candidate-capture",
        resolver: "candidate-resolver",
        bundle_verify: "packaged-verification",
        catalog_load: "catalog-load",
        pack_lock_digest: "pack-resolver",
    }
    expected_root = (
        roots["second"]
        if case == "second-user-data-existing-authority"
        else roots["first"]
    )
    original_id_getter = type(port).bootstrap_profile_id

    def missing_second_id(instance: Any) -> str:
        value = original_id_getter(instance)
        return MISSING_PROFILE_ID if phase == "resolver" else value

    missing_id_fixture = (
        patch.object(type(port), "bootstrap_profile_id", missing_second_id)
        if case == "source-profile-missing"
        else nullcontext()
    )

    def trace(frame: FrameType, event: str, value: Any) -> Any:
        nonlocal phase, captured, dynamic_result, resolve_inputs, before_dynamic_bindings
        code = frame.f_code
        parent = frame.f_back.f_code if frame.f_back else None
        dynamic_pack = (
            code.co_name == "dynamic_profile_edges"
            and code.co_filename == str(dynamic_source)
        )
        log = events[phase]
        if event == "call":
            frame.f_trace_lines = code is resolver
            if code is resolver:
                if (
                    phase != "capture"
                    or parent is not caller
                    or captured is None
                    or frame.f_locals["review_candidate"] is not captured
                    or frame.f_locals["base_dir"] is not None
                ):
                    raise AssertionError("Setup did not forward its actual candidate")
                phase = "resolver"
                _change_at_entry(case, runtime, roots["second"], original_lock)
                return trace
            if code is dynamic_port or dynamic_pack:
                through_port = code is dynamic_port
                ids_name = "pack_ids" if through_port else "additional_pack_ids"
                local = frame.f_locals
                if (
                    case not in DYNAMIC_CASES
                    or phase != "resolver"
                    or before_dynamic_bindings is None
                    or parent is not (resolver if through_port else dynamic_port)
                    or local["catalog"] is not captured[0]
                    or local["profile_id"] is not facts["profile_ids"][-1]
                    or local[ids_name] is not captured[1]
                    or (through_port and local["self"] is not port)
                ):
                    raise AssertionError("original dynamic call replaced its inputs")
                log.append("call-dynamic-port" if through_port else "call-dynamic-pack")
                return trace
            if code is resolve_port:
                local = frame.f_locals
                outer = frame.f_back.f_locals
                kwargs = local["kwargs"]
                digests = kwargs["approved_artifact_digests"]
                bindings = kwargs["authority_bindings"]
                if (
                    case not in RESOLVE_CASES
                    or parent is not resolver
                    or local["self"] is not port
                    or before_dynamic_bindings is None
                    or "binding_count" not in facts.get("dynamic_edges", {})
                    or local["catalog"] is not captured[0]
                    or local["profile_id"] is not facts["profile_ids"][-1]
                    or digests is not outer["verified_artifacts"]
                    or type(digests) is not set
                    or any(type(digest) is not str for digest in digests)
                    or bindings is not before_dynamic_bindings
                    or list(bindings.items()) != before_dynamic_rows
                    or kwargs["additional_pack_ids"] is not captured[1]
                    or kwargs["authority_snapshot_digest"] != facts["authority_snapshot_digest"]
                    or kwargs["security_epoch"] != facts["security_epoch"]
                ):
                    raise AssertionError("original resolve Port did not receive its exact inputs")
                facts["verified_artifacts"] = {
                    "pack_count": len(captured[0].packs),
                    "digests": sorted(digests),
                }
                log.append("before-resolve-port-body")
                if case == VERIFIED_ARTIFACTS_CASE:
                    raise _SourceProfileBoundary
                resolve_inputs = kwargs
                path = captured[0].root / LOCK
                if path != packaged / LOCK:
                    raise AssertionError("fixture must change only the exact loaded root")
                if case == "pack-lock-reread":
                    path.write_bytes(original_lock + b"\n")
                else:
                    path.unlink()
                    if case == "pack-lock-symlink":
                        path.symlink_to(MANIFEST)
                return trace
            if code is resolve_pack:
                local = frame.f_locals
                if (
                    case not in PACK_LOCK_CASES
                    or parent is not resolve_port
                    or resolve_inputs is None
                    or local["catalog"] is not captured[0]
                    or local["profile_id"] is not facts["profile_ids"][-1]
                    or any(local[key] is not original for key, original in resolve_inputs.items())
                ):
                    raise AssertionError("actual Pack resolver did not receive its original inputs")
                log.append("call-resolve-pack")
                return trace
            if code is pack_lock_digest:
                if (
                    case not in PACK_LOCK_CASES
                    or parent is not resolve_pack
                    or frame.f_locals["catalog"] is not captured[0]
                ):
                    raise AssertionError("unexpected actual Pack bundle digest call")
                log.append("call-pack-lock-digest")
                return trace
            if parent is pack_lock_digest and code in (is_symlink, is_file):
                if frame.f_locals["self"] != captured[0].root / LOCK:
                    raise AssertionError("Pack resolver tested another path")
                log.append("pack-lock-is-symlink" if code is is_symlink else "pack-lock-is-file")
                return trace
            if code is read_bytes:
                path = frame.f_locals["self"]
                if path.name != LOCK or parent not in (
                    bundle_verify,
                    catalog_load,
                    resolver,
                    pack_lock_digest,
                ):
                    return None
                log.append(f"read-lock:{caller_labels[parent]}")
                return trace
            if code is recovery:
                facts["recovery_discard_superseded"].append(
                    frame.f_locals["discard_superseded"]
                )
            if code in call_names:
                log.append(call_names[code])
                if (
                    code is authority_init
                    and frame.f_locals["path"] != expected_root / AUTHORITY
                ):
                    raise AssertionError("resolver opened Authority under another root")
            if code in (
                acquire,
                root_get,
                bundle_get,
                id_get,
                journal_exists,
                record_read,
                context_close,
            ):
                return trace
            return None
        if event == "line" and code is resolver and frame.f_lineno == lookup:
            local = frame.f_locals
            if (
                local["catalog"] is not captured[0]
                or local["additional_pack_ids"] is not captured[1]
            ):
                raise AssertionError("resolver replaced its captured candidate data")
            if local["profile_id"] is not facts["profile_ids"][-1]:
                raise AssertionError("resolver replaced its actual second Profile ID")
            facts.update(
                bundle_lock_digest=local["bundle_lock_digest"],
                security_epoch=local["security_epoch"],
                authority_snapshot_digest=local["snapshot_digest"],
            )
            log.append("before-source-profile-lookup")
            if case in LOOKUP_CASES:
                return trace
            raise _SourceProfileBoundary
        if event == "line" and code is resolver and frame.f_lineno == lookup_guard:
            local = frame.f_locals
            profile = local["source_profile"]
            if case == "source-profile-missing" and profile is None:
                facts["source_profile"] = {"type": "NoneType"}
                log.append("source-profile-missing")
                return trace  # Allow the original guard and denial factory to run.
            if case not in ("source-profile-present", *BINDING_CASES) or (
                type(profile) is not dict
            ):
                raise AssertionError("unexpected original source Profile result")
            if profile is not captured[0].profiles[local["profile_id"]]:
                raise AssertionError("original lookup did not retain its source alias")
            facts["source_profile"] = {
                "type": "dict",
                "catalog_alias": True,
                "definition_digest": canonical_digest(profile),
            }
            log.append("source-profile-found")
            if case in BINDING_CASES:
                return trace
            raise _SourceProfileBoundary
        if event == "line" and code is resolver and frame.f_lineno == dynamic_edges:
            if case not in BINDING_CASES:
                raise AssertionError("unexpected entry into the static binding observation")
            local = frame.f_locals
            bindings = local["authority_bindings"]
            edges = local["source_profile"]["requested_edges"]
            if type(bindings) is not dict or type(edges) is not list:
                raise AssertionError("original requested-edge values changed their type")
            log.append("static-edges-bound")
            if case in DYNAMIC_CASES:
                before_dynamic_bindings = bindings
                before_dynamic_rows.extend(bindings.items())
                return trace
            facts["requested_bindings"] = {
                "source_edge_count": len(edges),
                "source_edge_fields": [
                    [str(edge[field]) for field in REQUESTED_EDGE_FIELDS]
                    for edge in edges
                ],
                "source_edge_keys": [
                    "|".join(str(edge[field]) for field in REQUESTED_EDGE_FIELDS)
                    for edge in edges
                ],
                "ordered_bindings": [[key, value] for key, value in bindings.items()],
            }
            raise _SourceProfileBoundary
        if (
            event == "line"
            and code is resolver
            and frame.f_lineno in verified_artifacts
            and "binding_count" not in facts.get("dynamic_edges", {})
        ):
            bindings = frame.f_locals["authority_bindings"]
            if (
                case not in DYNAMIC_CASES
                or "dynamic_edges" not in facts
                or bindings is not before_dynamic_bindings
                or list(bindings.items()) != before_dynamic_rows
            ):
                raise AssertionError("unexpected bindings after the original dynamic loop")
            facts["dynamic_edges"].update(
                binding_count=len(bindings),
                ordered_bindings_digest=canonical_digest(
                    [list(row) for row in before_dynamic_rows]
                ),
            )
            log.append("before-verified-artifacts")
            if case in RESOLVE_CASES:
                return trace
            raise _SourceProfileBoundary
        if event == "return" and (code is dynamic_port or dynamic_pack):
            if type(value) is not tuple or value:
                raise AssertionError("fresh bundled Pack did not return an empty tuple")
            if dynamic_pack:
                dynamic_result = value
                log.append("return-dynamic-pack")
            elif value is dynamic_result:
                facts["dynamic_edges"] = {
                    "additional_pack_count": len(captured[1]),
                    "returned_type": type(value).__name__,
                    "returned_edge_count": len(value),
                }
                log.append("return-dynamic-port")
            else:
                raise AssertionError("original Port replaced the actual Pack result")
            return trace
        if event == "return" and code is pack_lock_digest and value is not None:
            if case != "pack-lock-reread" or value != _digest(original_lock + b"\n"):
                raise AssertionError("Pack resolver did not hash the new lock bytes")
            facts["pack_bundle_digest"] = value
            log.append("return-pack-lock-digest")
            raise _SourceProfileBoundary
        if event != "return" or value is None and code is not context_close:
            return trace
        if code is acquire:
            if (
                phase != "capture"
                or type(value) is not tuple
                or len(value) != 2
                or type(value[0]) is not BundledCatalog
                or value[0].root != packaged
                or type(value[1]) is not tuple
                or value[1]
            ):
                raise AssertionError(
                    "original fresh Setup did not produce its bundled tuple"
                )
            captured = value
        elif code is root_get:
            log.append(f"user-data-root:{caller_labels[parent]}")
            facts["user_data_roots"].append(root_labels[value])
        elif code is bundle_get:
            log.append("select-bundle-root")
            facts["bundle_roots"].append(value.relative_to(runtime).as_posix())
        elif code is read_bytes:
            facts["lock_reads"].append(
                {
                    "caller": caller_labels[parent],
                    "path": frame.f_locals["self"].relative_to(runtime).as_posix(),
                    "bytes": len(value),
                    "digest": _digest(value),
                }
            )
        elif code is id_get:
            log.append("validate-profile-id")
            facts["profile_ids"].append(value)
        elif code is journal_exists:
            if (
                parent is not recovery
                or frame.f_locals["self"].root != roots["first"] / "profiles"
            ):
                raise AssertionError("unexpected publication journal root")
            facts["publication_journal_exists"].append(value)
        elif code is record_read:
            facts["authority_epoch_record"] = {
                "value": value.value,
                "advanced_at": value.advanced_at,
                "reason_digest": value.reason_digest,
            }
        elif code is context_close:
            facts["authority_closed"] = frame.f_locals["self"]._closed
        return trace

    _seed(case, roots, AuthorityStore)
    before = _state(roots)
    previous_trace = sys.gettrace()
    with (
        missing_id_fixture,
        patch.dict(
            os.environ,
            {
                "TOBKIRI_USER_DATA": str(roots["first"]),
                "RUMI_USER_DATA": str(roots["first"]),
                "RUMI_ENVIRONMENT": "production",
                "RUMI_APP_DIR": "",
            },
        ),
    ):
        try:
            sys.settrace(trace)
            capture.prepare_bootstrap_profile_review()
        except _SourceProfileBoundary:
            outcome = {"outcome": {
                "source-profile-present": "source-profile-found",
                REQUESTED_BINDINGS_CASE: "static-edges-bound",
                DYNAMIC_EDGES_CASE: "before-verified-artifacts",
                VERIFIED_ARTIFACTS_CASE: "before-resolve-port-body",
                "pack-lock-reread": "pack-lock-digest-returned",
            }.get(case, "before-source-profile-lookup")}
        except Exception as error:
            outcome = {"outcome": "error", "error": error_graph(error, runtime)}
            if case == "source-profile-missing":
                outcome["error"]["args"] = list(error.args)
            # This is a runtime resource path, not the Authority/user-data root.
            outcome["error"] = _rename_runtime_path(outcome["error"])
        else:
            raise AssertionError("original resolver ran past its finite boundary")
        finally:
            sys.settrace(previous_trace)
            if case in {"second-lock-unbound", "second-lock-rebound"}:
                (packaged / LOCK).write_bytes(original_lock)
            if case in PACK_LOCK_CASES:
                path = packaged / LOCK
                if path.is_symlink():
                    path.unlink()
                path.write_bytes(original_lock)
            if case in {"second-manifest-invalid-json", "second-lock-rebound"}:
                (runtime / MANIFEST).write_bytes(original_manifest)
    after = _state(roots)
    locks = {label: _lock_free(path / GUARD) for label, path in roots.items()}
    _check(
        case,
        events,
        facts,
        outcome,
        before,
        after,
        locks,
        original_lock,
        canonical_digest,
        authority_digest,
    )
    return {
        "case": case,
        "expected": outcome,
        "calls": events,
        "facts": facts,
        "user_data_before": before,
        "user_data_after": after,
        "authority_guard_free_after": locks,
    }


def _rename_runtime_path(node: dict) -> dict:
    for name in ("message", "filename"):
        if type(node.get(name)) is str:
            node[name] = node[name].replace("$USER_DATA", "$RUNTIME")
    for name in ("cause", "context"):
        if node.get(name) is not None:
            _rename_runtime_path(node[name])
    return node


def _check(
    case: str,
    events: dict,
    facts: dict,
    outcome: dict,
    before: dict,
    after: dict,
    locks: dict,
    original_lock: bytes,
    canonical_digest: Callable[[object], str],
    authority_digest: Callable[[object], str],
) -> None:
    packaged = [
        "get-host-resource-root",
        "get-packaged-bundle-root",
        "verify-packaged-binding",
    ]
    profile_id = ["get-raw-profile-id", "validate-profile-id"]
    capture = [
        "user-data-root:publication-recovery",
        "user-data-root:publication-recovery",
        "user-data-root:candidate-capture",
        *packaged,
        "read-lock:packaged-verification",
        "select-bundle-root",
        "load-catalog",
        "read-lock:catalog-load",
        *profile_id,
        "select-candidate",
    ]
    manifest_error = case == "second-manifest-invalid-json"
    unbound = case == "second-lock-unbound"
    development = case.startswith("second-development-root")
    missing_lock = case == "second-development-root-missing-lock"
    existing = case == "second-user-data-existing-authority"
    corrupt = case == "authority-corrupt-file"
    directory = case == "authority-directory"
    resolver = ["user-data-root:candidate-resolver"]
    if development:
        resolver += packaged[:1]
    else:
        resolver += packaged
        if not manifest_error:
            resolver += ["read-lock:packaged-verification"]
    if not (manifest_error or unbound):
        resolver += ["select-bundle-root", "read-lock:candidate-resolver"]
    if existing:
        resolver += [
            "construct-authority",
            "enter-authority",
            "read-epoch",
            "existing-snapshot",
            "read-epoch-record",
            "exit-authority",
            "close-authority",
        ]
    elif corrupt:
        resolver += ["construct-authority"]
    elif not (directory or manifest_error or unbound or missing_lock):
        resolver += ["genesis-snapshot"]
    success = not (directory or corrupt or manifest_error or unbound or missing_lock)
    lookup_missing = case == "source-profile-missing"
    lookup_present = case in ("source-profile-present", *BINDING_CASES)
    if success:
        resolver += [*profile_id, "before-source-profile-lookup"]
    if lookup_present:
        resolver.append("source-profile-found")
    if case in BINDING_CASES:
        resolver.append("static-edges-bound")
    if case in DYNAMIC_CASES:
        resolver.extend((
            "call-dynamic-port",
            "call-dynamic-pack",
            "return-dynamic-pack",
            "return-dynamic-port",
            "before-verified-artifacts",
        ))
    if case in RESOLVE_CASES:
        resolver.append("before-resolve-port-body")
    if case in PACK_LOCK_CASES:
        resolver.extend(("call-resolve-pack", "call-pack-lock-digest", "pack-lock-is-symlink"))
        if case != "pack-lock-symlink":
            resolver.append("pack-lock-is-file")
        if case == "pack-lock-reread":
            resolver.extend(("read-lock:pack-resolver", "return-pack-lock-digest"))
    if lookup_missing:
        resolver.append("source-profile-missing")
    expected_ids = ["defaults"]
    if success:
        expected_ids.append(MISSING_PROFILE_ID if lookup_missing else "defaults")
    selected_bundle = DEV_BUNDLE if development else BUNDLE
    selected_roots = [BUNDLE.as_posix()]
    if not (manifest_error or unbound):
        selected_roots.append(selected_bundle.as_posix())
    if (
        events != {"capture": capture, "resolver": resolver}
        or facts["recovery_discard_superseded"] != [True, False]
        or facts["publication_journal_exists"] != [False, False]
        or facts["profile_ids"] != expected_ids
        or facts["user_data_roots"]
        != ["first"] * 3 + ["second" if existing else "first"]
        or facts["bundle_roots"] != selected_roots
        or locks
        != {"first": True if corrupt else None, "second": True if existing else None}
        or (not (existing or corrupt) and before != after)
        or before["first" if existing else "second"]
        != after["first" if existing else "second"]
    ):
        raise AssertionError((case, events, facts, locks, before, after))
    if existing or corrupt:
        owner = "second" if existing else "first"
        previous_files = {row["path"]: row for row in before[owner]["files"]}
        current_files = {row["path"]: row for row in after[owner]["files"]}
        key = AUTHORITY.with_suffix(".key").as_posix()
        database = AUTHORITY.as_posix()
        if (
            set(current_files) != set(previous_files)
            or current_files[key]["mode"] != 0o600
            or current_files[GUARD.as_posix()]["mode"] != 0o600
            or any(
                row != current_files[path]
                for path, row in previous_files.items()
                if path != database or corrupt
            )
        ):
            raise AssertionError(
                (case, "unexpected Authority filesystem effects", before, after)
            )
    original_digest = _digest(original_lock)
    changed = case in {
        "second-lock-unbound",
        "second-lock-rebound",
        "second-development-root",
    }
    final_lock = original_lock + b"\n" if changed else original_lock
    final_digest = _digest(final_lock)
    first_reads = [
        {
            "caller": caller,
            "path": (BUNDLE / LOCK).as_posix(),
            "bytes": len(original_lock),
            "digest": original_digest,
        }
        for caller in ("packaged-verification", "catalog-load")
    ]
    second_reads = []
    if not (development or manifest_error):
        second_reads.append("packaged-verification")
    if not (manifest_error or unbound or missing_lock):
        second_reads.append("candidate-resolver")
    expected_reads = first_reads + [
        {
            "caller": caller,
            "path": (selected_bundle / LOCK).as_posix(),
            "bytes": len(final_lock),
            "digest": final_digest,
        }
        for caller in second_reads
    ]
    if case == "pack-lock-reread":
        expected_reads.append({
            "caller": "pack-resolver",
            "path": (BUNDLE / LOCK).as_posix(),
            "bytes": len(original_lock) + 1,
            "digest": _digest(original_lock + b"\n"),
        })
    if facts["lock_reads"] != expected_reads:
        raise AssertionError((case, facts["lock_reads"]))
    if success:
        epoch = 2 if existing else 1
        reason = authority_digest({"reason": EPOCH_REASON if existing else "genesis"})
        snapshot = canonical_digest(
            {
                "schema": "io.tobkiri.profile-authority-snapshot.v1",
                "security_epoch": epoch,
                "security_epoch_reason_digest": reason,
                "bundle_lock_digest": final_digest,
                "grant_import": "none",
            }
        )
        if (
            facts["bundle_lock_digest"] != final_digest
            or facts["security_epoch"] != epoch
            or facts["authority_snapshot_digest"] != snapshot
            or existing
            and (
                facts["authority_closed"] is not True
                or facts["authority_epoch_record"]
                != {
                    "value": epoch,
                    "advanced_at": 1000.0,
                    "reason_digest": reason,
                }
            )
        ):
            raise AssertionError((case, outcome, facts))
        if case in {"pack-lock-symlink", "pack-lock-missing"}:
            if outcome != {
                "outcome": "error",
                "error": {
                    "id": 0,
                    "type": DENIED_CLASS,
                    "message": "bundle lock is unavailable",
                    "suppressed": False,
                    "cause": None,
                    "context": None,
                },
            }:
                raise AssertionError((case, outcome, facts))
        elif lookup_missing:
            message = "bundled setup Profile is missing"
            if facts["source_profile"] != {"type": "NoneType"} or outcome != {
                "outcome": "error",
                "error": {
                    "id": 0,
                    "type": DENIED_CLASS,
                    "message": message,
                    "suppressed": False,
                    "cause": None,
                    "context": None,
                    "args": [message],
                },
            }:
                raise AssertionError((case, outcome, facts))
        elif lookup_present:
            wanted = {
                REQUESTED_BINDINGS_CASE: "static-edges-bound",
                DYNAMIC_EDGES_CASE: "before-verified-artifacts",
                VERIFIED_ARTIFACTS_CASE: "before-resolve-port-body",
                "pack-lock-reread": "pack-lock-digest-returned",
            }.get(case, "source-profile-found")
            if outcome != {"outcome": wanted} or not facts["source_profile"]["catalog_alias"]:
                raise AssertionError((case, outcome, facts))
            if case == REQUESTED_BINDINGS_CASE:
                _check_static_bindings(facts["requested_bindings"], snapshot)
            if case in DYNAMIC_CASES:
                dynamic = facts["dynamic_edges"]
                if (
                    dynamic["additional_pack_count"] != 0
                    or dynamic["returned_type"] != "tuple"
                    or dynamic["returned_edge_count"] != 0
                    or dynamic["binding_count"] != 310
                ):
                    raise AssertionError((case, outcome, facts))
            if case in RESOLVE_CASES:
                artifacts = facts["verified_artifacts"]
                if (
                    artifacts["pack_count"] != 153
                    or len(artifacts["digests"]) != 153
                    or any(
                        len(digest) != 71
                        or not digest.startswith("sha256:")
                        or any(c not in "0123456789abcdef" for c in digest[7:])
                        for digest in artifacts["digests"]
                    )
                ):
                    raise AssertionError((case, outcome, facts))
        elif outcome != {"outcome": "before-source-profile-lookup"}:
            raise AssertionError((case, outcome, facts))
        return
    expected_type, message, cause = {
        "authority-directory": (
            DENIED_CLASS,
            "Authority store path is not a regular file",
            None,
        ),
        "authority-corrupt-file": (
            "core_runtime.authority.store_contracts.AuthorityStoreError",
            "authority database is unavailable",
            "sqlite3.DatabaseError",
        ),
        "second-manifest-invalid-json": (
            DENIED_CLASS,
            "packaged runtime resource manifest is invalid",
            "json.decoder.JSONDecodeError",
        ),
        "second-lock-unbound": (
            DENIED_CLASS,
            f"packaged Profile resource is not launcher-bound: {BUNDLE / LOCK}",
            None,
        ),
        "second-development-root-missing-lock": (
            "builtins.FileNotFoundError",
            None,
            None,
        ),
    }[case]
    error = outcome["error"]
    if (
        error["type"] != expected_type
        or message is not None
        and error["message"] != message
        or error["suppressed"] is not (cause is not None)
        or cause is None
        and (error["cause"] is not None or error["context"] is not None)
        or cause is not None
        and (
            error["cause"]["type"] != cause
            or error["context"] != {"ref": error["cause"]["id"]}
        )
        or missing_lock
        and (
            error["errno"] != 2 or error["filename"] != f"$RUNTIME/{DEV_BUNDLE / LOCK}"
        )
    ):
        raise AssertionError((case, outcome))


def _check_static_bindings(observed: dict, snapshot: str) -> None:
    keys = observed["source_edge_keys"]
    fields = observed["source_edge_fields"]
    if (
        len(keys) != 310
        or observed["source_edge_count"] != len(keys)
        or len(fields) != len(keys)
        or any(len(row) != 4 for row in fields)
        or keys != ["|".join(row) for row in fields]
    ):
        raise AssertionError("pinned original Profile requested-edge count changed")
    expected = []
    for key in dict.fromkeys(keys):
        document = {
            "edge": key,
            "profile_authority_snapshot_digest": snapshot,
            "schema": "io.tobkiri.profile-authority-edge.v1",
        }
        preimage = json.dumps(
            document, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        expected.append([key, "authority-ref:" + hashlib.sha256(preimage).hexdigest()])
    if observed["ordered_bindings"] != expected:
        raise AssertionError("actual static authority bindings changed their order or bytes")


def prepare(original: Path, output: Path) -> None:
    """Stage the pinned original once and retain only bounded observation metadata."""
    check_original(original)
    if output.is_relative_to(original) or not sys.dont_write_bytecode:
        raise ValueError("independent fixture directory and disabled bytecode required")
    source = original / "tobkiri_runtime"
    expected = _source_hashes(source)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    runtime = output / "runtime"
    shutil.copytree(source, runtime, ignore=IGNORED)
    bind_resources(runtime)
    (runtime / DEV_BUNDLE).mkdir(parents=True, exist_ok=False)
    (runtime / DEV_BUNDLE.parent / "platform-artifacts").mkdir(exist_ok=False)
    if _source_hashes(runtime) != expected:
        raise ValueError("staged original sources differ")
    sys.path.insert(0, str(runtime))
    results = []
    for case in CASES:
        roots = {
            "first": output / case / "user-data",
            "second": output / case / "next-user-data",
        }
        results.append(_observe(case, runtime, roots))
    # The prior cases use the exact pinned source bundle. Only after those
    # observations, generate the packaged Shell fixture in the same CI tree.
    (runtime / BUNDLE.parent / "platform-artifacts").rmdir()
    observe_shell(original, runtime, output)
    if _source_hashes(runtime) != expected or _source_hashes(source) != expected:
        raise AssertionError("original sources changed during resolver observation")
    check_original(original)
    write_json(
        output / "summary.json",
        {
            "format": FORMAT,
            "original_head": ORIGINAL_HEAD,
            "python": "3.13.16",
            "source_hashes": expected,
            "case_count": len(results),
            "cases": results,
            "native_comparison": False,
            "qualification": False,
            "production_enabled": False,
            "scope": "fresh bundled candidate and original resolver prefix through "
            "second canonical Profile ID; additional cases observe the source alias, "
            "its denial, ordered bindings, the fresh dynamic Pack call, and the "
            "verified artifacts passed to the original resolve Port, and the Pack lock",
        },
    )


def main() -> None:
    """Run the original reference in its pinned CI lane."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.original_root.resolve(strict=True), args.output.resolve())


if __name__ == "__main__":
    main()
