#!/usr/bin/env python3
"""Freeze test-only R11.4 reference vectors from the unchanged legacy source.

Run with the existing source checkout's .venv/bin/python -I -B. No store or
runtime constructor is called. Temporary Pack/artifact files and an in-memory
SQLite table are fixtures; no live user data, servers, network, or approvals
are involved. Expected codec results come from the original source methods.
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import importlib
import importlib.metadata
import inspect
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from typing import Any, Callable


DEFAULT_SOURCE = Path("/Users/haru/.codex/worktrees/e8bc/tobikiri-pr-1322")
EXPECTED_COMMIT_PREFIX = "cd7c20e9d"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "evidence/legacy-vectors.json"
TEST_ONLY_KEY = bytes(range(32))


def fingerprint(raw: bytes) -> str:
    """Fingerprint evidence, not a replacement expected runtime algorithm."""
    return hashlib.sha256(raw).hexdigest()


def byte_evidence(raw: bytes, *, recipe: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"length": len(raw), "sha256_hex": fingerprint(raw)}
    if len(raw) <= 8192:
        result["hex"] = raw.hex()
        try:
            result["utf8"] = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            pass
    else:
        if recipe is None:
            raise AssertionError("large exact byte evidence requires a reconstruction recipe")
        result["exact_bytes_recipe"] = recipe
        result["first_32_bytes_hex"] = raw[:32].hex()
        result["last_32_bytes_hex"] = raw[-32:].hex()
    return result


def outcome(call: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"status": "ok", "value": call()}
    except Exception as error:
        return {
            "status": "error",
            "exception_type": f"{type(error).__module__}.{type(error).__name__}",
            "message": str(error),
        }


def generate(source_root: Path) -> dict[str, Any]:
    sys.dont_write_bytecode = True
    source_root = source_root.resolve(strict=True)
    commit = subprocess.check_output(
        ["git", "-C", str(source_root), "rev-parse", "HEAD"], text=True
    ).strip()
    if not commit.startswith(EXPECTED_COMMIT_PREFIX):
        raise RuntimeError(f"expected legacy commit {EXPECTED_COMMIT_PREFIX}, found {commit}")
    sys.path.insert(0, str(source_root / "tobkiri_runtime"))
    canonical = importlib.import_module("tobkiri_protocol.canonical")
    workflow = importlib.import_module("core_runtime.workflow_v4.store")
    authority = importlib.import_module("core_runtime.authority.v4_models")
    authority_store = importlib.import_module("core_runtime.authority.v4_store")
    signatures = importlib.import_module("core_runtime.pack_signature")
    artifacts = importlib.import_module("tobkiri_protocol.platform_artifact")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sources: dict[str, Any] = {}
    calls: dict[str, Any] = {}

    def source_record(path: Path) -> str:
        relative = path.resolve().relative_to(source_root).as_posix()
        raw = path.read_bytes()
        committed = subprocess.check_output(
            ["git", "-C", str(source_root), "show", f"{commit}:{relative}"]
        )
        if raw != committed:
            raise RuntimeError(f"referenced source differs from clean commit: {relative}")
        sources[relative] = {"git_commit": commit, "sha256_hex": fingerprint(raw), "length": len(raw)}
        return relative

    def call_record(name: str, function: Callable[..., Any]) -> str:
        path = Path(inspect.getsourcefile(function) or "")
        relative = source_record(path)
        text, line = inspect.getsourcelines(function)
        calls[name] = {
            "source": relative,
            "qualified_name": function.__qualname__,
            "first_line": line,
            "callable_source_sha256_hex": fingerprint("".join(text).encode("utf-8")),
            "execution": "direct call to original imported source callable",
        }
        return name

    for name, function in (
        ("protocol.strict_loads", canonical.strict_loads),
        ("protocol.canonical_json", canonical.canonical_json),
        ("protocol.canonical_bytes", canonical.canonical_bytes),
        ("protocol.canonical_digest", canonical.canonical_digest),
        ("workflow._encode", workflow.WorkflowStoreV4._encode),
        ("workflow._decode", workflow.WorkflowStoreV4._decode),
        ("authority.canonical_json", authority.canonical_json),
        ("authority.authority_digest", authority.authority_digest),
        ("authority.interactive_confirmation_digest", authority.interactive_confirmation_digest),
        ("authority.InvocationLease.from_dict", authority.InvocationLease.from_dict),
        ("authority.InvocationLease.to_dict", authority.InvocationLease.to_dict),
        ("authority.InvocationLease.digest", authority.InvocationLease.digest.fget),
        ("authority_store._encode_lease_token", authority_store.AuthorityStore._encode_lease_token),
        ("authority_store._decode_lease_token", authority_store.AuthorityStore._decode_lease_token),
        ("authority_store._append_audit", authority_store.AuthorityStore._append_audit),
        ("authority_store._verify_audit_rows", authority_store.AuthorityStore._verify_audit_rows),
        ("pack.build_signed_manifest", signatures.build_signed_manifest),
        ("pack.sign_manifest", signatures.sign_manifest),
        ("pack.verify_signed_manifest", signatures.verify_signed_manifest),
        ("pack.verify_signed_pack", signatures.verify_signed_pack),
        ("pack._canonical_bytes", signatures._canonical_bytes),
        ("artifact.artifact_digest", artifacts.artifact_digest),
    ):
        call_record(name, function)
    for relative in (
        "tobkiri_runtime/tobkiri_protocol/errors.py",
        "tobkiri_runtime/core_runtime/workflow_v4/models.py",
        "tobkiri_runtime/core_runtime/authority/store_contracts.py",
    ):
        source_record(source_root / relative)

    protocol_cases: list[dict[str, Any]] = []

    def protocol_case(name: str, raw: bytes, *, kwargs: dict[str, Any] | None = None,
                      recipe: dict[str, Any] | None = None) -> None:
        options = kwargs or {}
        result = outcome(lambda: canonical.strict_loads(raw, **options))
        if result["status"] == "ok":
            parsed = result.pop("value")
            encoded = canonical.canonical_json(parsed)
            result.update({"canonical_bytes": byte_evidence(encoded),
                           "canonical_digest": canonical.canonical_digest(parsed)})
        protocol_cases.append({
            "id": name,
            "source_calls": ["protocol.strict_loads", "protocol.canonical_json", "protocol.canonical_digest"],
            "input_bytes": byte_evidence(raw, recipe=recipe),
            "strict_loads_kwargs": options,
            "expected": result,
        })

    for name, raw in (
        ("null", b"null"), ("booleans", b"[true,false]"),
        ("sorted_keys_and_unicode", b'{"z":0,"a":"Tobkiri\\u00a0\\u3042"}'),
        ("non_bmp_codepoint_key_sort", b'{"\\ud800\\udc00":1,"\\ue000":2,"a":3,"\\ud83d\\ude80":4}'),
        ("escaped_controls_and_separators", b'{"s":"\\u0000\\b\\f\\n\\r\\t/\\u2028\\u2029"}'),
        ("unicode_normalization_preserved", b'{"e\\u0301":"decomposed","\\u00e9":"composed"}'),
        ("safe_integer_bounds", b"[-9007199254740991,9007199254740991]"),
        ("duplicate_keys", b'{"a":1,"a":2}'),
        ("nested_duplicate_keys", b'{"outer":{"a":1,"a":2}}'),
        ("float_integral", b'{"n":1.0}'), ("float_negative_zero", b"-0.0"),
        ("float_exponent", b"1e-7"), ("nan", b"NaN"),
        ("positive_infinity", b"Infinity"), ("negative_infinity", b"-Infinity"),
        ("unsafe_positive_integer", b"9007199254740992"),
        ("unsafe_negative_integer", b"-9007199254740992"),
        ("invalid_utf8", b'{"a":"\xff"}'),
        ("unpaired_high_surrogate", b'{"a":"\\ud800"}'),
        ("unpaired_low_surrogate_key", b'{"\\udfff":0}'),
        ("valid_surrogate_pair", b'"\\ud83d\\ude80"'),
        ("depth_64", b"[" * 64 + b"0" + b"]" * 64),
        ("depth_65", b"[" * 65 + b"0" + b"]" * 65),
        ("malformed_trailing_comma", b'{"a":1,}'),
    ):
        protocol_case(name, raw)
    maximum = canonical.MAX_CANONICAL_JSON_BYTES
    for count, name in ((maximum - 1, "input_bytes_at_4mib"), (maximum, "input_bytes_over_4mib")):
        protocol_case(name, b"0" + b" " * count,
                      recipe={"kind": "concat", "parts": [{"hex": "30"},
                              {"repeat_byte_hex": "20", "count": count}]})
    protocol_case("custom_byte_bound_at_limit", b'{"a":1}', kwargs={"max_bytes": 7})
    protocol_case("custom_byte_bound_over_limit", b'{"a":1} ', kwargs={"max_bytes": 7})
    protocol_case("custom_depth_zero", b"[0]", kwargs={"max_depth": 0})

    direct_cases: list[dict[str, Any]] = []
    for name, expression, value in (
        ("direct_nonstring_key", '{1: "x"}', {1: "x"}),
        ("direct_tuple", "(1, 2)", (1, 2)),
        ("direct_unpaired_surrogate", '"\\ud800"', "\ud800"),
        ("direct_floating_point", "1.0", 1.0),
        ("direct_unsafe_integer", "2**53", 2**53),
    ):
        direct_cases.append({"id": name, "input_python_expression": expression,
                             "source_calls": ["protocol.canonical_json"],
                             "expected": outcome(lambda value=value: canonical.canonical_json(value))})
    for count, name in ((maximum - 2, "canonical_bytes_at_4mib"),
                        (maximum - 1, "canonical_bytes_over_4mib")):
        value = "a" * count
        result = outcome(lambda: canonical.canonical_json(value))
        if result["status"] == "ok":
            encoded = result.pop("value")
            result["canonical_bytes"] = byte_evidence(encoded, recipe={
                "kind": "concat", "parts": [{"hex": "22"},
                {"repeat_byte_hex": "61", "count": count}, {"hex": "22"}]})
            result["canonical_digest"] = canonical.canonical_digest(value)
        direct_cases.append({"id": name, "input_python_expression": f"'a' * {count}",
                             "source_calls": ["protocol.canonical_json", "protocol.canonical_digest"],
                             "expected": result})

    canonical_object_cases = []
    for name, value in (("object", {"z": 1, "a": "あ🚀"}),
                        ("array_rejected", []), ("scalar_rejected", 0)):
        result = outcome(lambda value=value: canonical.canonical_bytes(value))
        if result["status"] == "ok":
            result["canonical_bytes"] = byte_evidence(result.pop("value"))
        canonical_object_cases.append({"id": name, "input": value,
            "source_calls": ["protocol.canonical_bytes"], "expected": result})

    workflow_receiver = object.__new__(workflow.WorkflowStoreV4)
    workflow_receiver._seal_key = TEST_ONLY_KEY
    workflow_cases = []
    for name, value in (
        ("minimal", {"state": "queued", "version": 1}),
        ("unicode_checkpoint", {"checkpoint_id": "fixture:1", "time_ms": 1750000000125,
                                "payload": {"message": "あ🚀", "keys": [None, True, 0]}}),
    ):
        payload, seal = workflow.WorkflowStoreV4._encode(workflow_receiver, value)
        workflow_cases.append({"id": name, "input": value,
            "source_calls": ["workflow._encode", "workflow._decode"],
            "expected": {"payload_bytes": byte_evidence(payload.encode("utf-8")),
                         "seal_lowercase_hex": seal,
                         "decoded": workflow.WorkflowStoreV4._decode(workflow_receiver, payload, seal),
                         "tampered_payload": outcome(lambda: workflow.WorkflowStoreV4._decode(
                             workflow_receiver, payload + " ", seal)),
                         "tampered_seal": outcome(lambda: workflow.WorkflowStoreV4._decode(
                             workflow_receiver, payload, ("0" if seal[0] != "0" else "1") + seal[1:]))}})

    authority_cases = []
    for name, raw in (
        ("integer_and_float_distinct", '{"integer":1,"float":1.0}'),
        ("negative_zero", '{"timestamp":-0.0}'),
        ("float_exponent_boundaries", '{"a":1e-7,"b":1e-6,"c":1e20,"d":1e21}'),
        ("fractional_timestamp", '{"created_at":1750000000.125,"expires_at":1750000001.0}'),
        ("subnormal_and_max_float", '[5e-324,1.7976931348623157e308]'),
        ("integer_beyond_protocol_range", '[9007199254740992,-9007199254740992]'),
        ("unicode_keys", '{"\\ud800\\udc00":1,"\\ue000":2}'),
        ("nonfinite_nan_rejected", "NaN"),
        ("nonfinite_infinity_rejected", "Infinity"),
    ):
        value = json.loads(raw)
        result = outcome(lambda: authority.canonical_json(value))
        if result["status"] == "ok":
            encoded = result.pop("value")
            result.update({"canonical_bytes": byte_evidence(encoded),
                           "authority_digest": authority.authority_digest(value)})
        authority_cases.append({"id": name, "input_json": raw,
                                "source_calls": ["authority.canonical_json", "authority.authority_digest"],
                                "expected": result})

    # Execute the original single assignment, rather than reimplementing the
    # derivation formula. No other __init__ statement (paths, keys, DB) runs.
    store_path = Path(authority_store.__file__)
    store_text = store_path.read_text(encoding="utf-8")
    tree = ast.parse(store_text, filename=str(store_path))
    store_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AuthorityStore")
    initializer = next(node for node in store_class.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    assignment = next(node for node in initializer.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Attribute) and target.attr == "_mac_key" for target in node.targets)
        and isinstance(node.value, ast.Call))
    segment = ast.get_source_segment(store_text, assignment)
    calls["authority_store.__init__._mac_key_assignment"] = {
        "source": source_record(store_path), "first_line": assignment.lineno,
        "exact_source_segment": segment,
        "source_segment_sha256_hex": fingerprint((segment or "").encode("utf-8")),
        "execution": "compile and execute the original AST assignment only; no constructor"}
    receiver = SimpleNamespace(_fernet_key=base64.urlsafe_b64encode(TEST_ONLY_KEY))
    exec(compile(ast.Module(body=[assignment], type_ignores=[]), str(store_path), "exec"),
         {"self": receiver, "hashlib": hashlib})
    def assert_fixture_key() -> None:
        assert len(receiver._mac_key) == 32
    receiver._assert_crypto_material = assert_fixture_key
    caller_input = {"parent_artifact_digest": "sha256:" + "1" * 64,
        "function_implementation_digest": "sha256:" + "2" * 64,
        "function_id": "fixture.caller", "contract_revision_digest": "sha256:" + "3" * 64,
        "operation_id": "fixture.invoke"}
    target_input = {**caller_input, "function_id": "fixture.target",
        "parent_artifact_digest": "sha256:" + "4" * 64,
        "function_implementation_digest": "sha256:" + "5" * 64}
    scope_input = {"capability": "fixture.read", "semantics_digest": "sha256:" + "6" * 64,
        "dimensions": {"path": ["fixture/b", "fixture/a", "fixture/a"]},
        "quotas": {"bytes": 128}, "exact_request_digest": None, "opaque": False}
    lease_input = {"lease_id": "fixture.lease.1", "request_id": "fixture.request.1",
        "caller": caller_input, "target": target_input,
        "caller_domain_id": "fixture.domain.caller", "caller_boot_epoch": 1,
        "target_domain_id": "fixture.domain.target", "target_boot_epoch": 2,
        "request_digest": "sha256:" + "7" * 64, "effect_digest": "sha256:" + "8" * 64,
        "authorized_scope": scope_input, "resource_namespace": "fixture.namespace",
        "profile_id": "fixture.profile", "activation_id": "fixture.activation",
        "activation_digest": "sha256:" + "9" * 64, "plan_digest": "sha256:" + "a" * 64,
        "profile_authority_digest": "sha256:" + "b" * 64, "fencing_token": 3,
        "caller_publisher_lineage": "fixture.publisher.caller",
        "target_publisher_lineage": "fixture.publisher.target",
        "host_extension_id": "fixture.host", "provider_authority_id": "fixture.provider",
        "provider_authority_digest": "sha256:" + "c" * 64, "grant_id": "fixture.grant",
        "audit_reservation_id": "fixture.audit.reservation", "security_epoch": 4,
        "issued_at": 1750000000.125, "expires_at": 1750000001.0, "call_chain": []}
    lease = authority.InvocationLease.from_dict(lease_input)
    token = authority_store.AuthorityStore._encode_lease_token(receiver, lease)
    token_payload, token_signature = token.split(".", 1)
    lease_vector = {
        "fixture_precondition": "receiver key material is fixed test data; filesystem/process guard is not exercised",
        "source_calls": ["authority_store.__init__._mac_key_assignment",
                         "authority.InvocationLease.from_dict", "authority.InvocationLease.to_dict",
                         "authority.InvocationLease.digest",
                         "authority_store._encode_lease_token", "authority_store._decode_lease_token"],
        "input": {"fernet_key_ascii": receiver._fernet_key.decode("ascii"),
                  "lease_definition": lease_input},
        "expected": {"derived_mac_key_hex": receiver._mac_key.hex(), "token": token,
                     "normalized_lease_definition": lease.to_dict(), "lease_digest": lease.digest,
                     "lease_canonical_bytes": byte_evidence(authority.canonical_json(lease.to_dict())),
                     "token_payload_bytes": byte_evidence(base64.urlsafe_b64decode(token_payload)),
                     "token_signature_bytes": byte_evidence(base64.urlsafe_b64decode(token_signature)),
                     "decoded": list(authority_store.AuthorityStore._decode_lease_token(receiver, token)),
                     "tampered": outcome(lambda: authority_store.AuthorityStore._decode_lease_token(
                         receiver, token_payload + "." + ("A" if token_signature[0] != "A" else "B") + token_signature[1:]))}}

    audit_cases = []
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE authority_audit(sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_id TEXT,event_type TEXT,event_state TEXT,previous_digest TEXT,event_digest TEXT,"
        "encrypted_payload BLOB,created_at REAL)")
    audit_receiver = SimpleNamespace(_audit_fault=None,
        _encrypt=lambda payload: authority.canonical_json(payload),
        _decrypt=lambda payload: json.loads(payload.decode("utf-8")))
    for index, timestamp in enumerate((0.0, 1e-7, 1750000000.125), start=1):
        audit_receiver._clock = lambda timestamp=timestamp: timestamp
        payload = {"fixture": True, "index": index, "outcome": "あ🚀", "approved": False}
        result = authority_store.AuthorityStore._append_audit(audit_receiver, connection,
            event_id=f"fixture.audit.{index}", event_type="fixture", event_state="recorded", payload=payload)
        row = connection.execute("SELECT * FROM authority_audit WHERE event_id=?", (f"fixture.audit.{index}",)).fetchone()
        frame = {key: row[key] for key in ("event_id", "event_type", "event_state", "previous_digest", "created_at")}
        frame["payload"] = payload
        audit_cases.append({"id": f"audit_chain_event_{index}", "input": frame,
            "source_calls": ["authority_store._append_audit", "authority.canonical_json"],
            "expected": {"event_digest": result, "digest_frame_bytes": byte_evidence(authority.canonical_json(frame))}})
    rows = connection.execute("SELECT * FROM authority_audit ORDER BY sequence").fetchall()
    verified = authority_store.AuthorityStore._verify_audit_rows(audit_receiver, rows)
    assert [item["event_digest"] for item in verified] == [item["expected"]["event_digest"] for item in audit_cases]
    connection.execute("UPDATE authority_audit SET event_digest=? WHERE sequence=2", ("sha256:" + "0" * 64,))
    corrupted_rows = connection.execute("SELECT * FROM authority_audit ORDER BY sequence").fetchall()
    corrupt_audit = outcome(lambda: authority_store.AuthorityStore._verify_audit_rows(audit_receiver, corrupted_rows))
    connection.close()

    artifact_cases = []
    corpus_path = source_root / "tobkiri_launcher/scripts/tests/fixtures/artifact_integrity_vectors.json"
    corpus_source = source_record(corpus_path)
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    extra_corpus = {
        "empty-file": {"kind": "file", "root_name": "empty", "files": {"": ""}},
        "empty-directory": {"kind": "directory", "root_name": "empty-dir", "files": {}},
        "unicode-nested-sort": {"kind": "directory", "root_name": "unicode", "files": {
            "z.txt": "last\n", "a/nested.txt": "line\r\n", "\ue000.txt": "bmp", "\U00010000.txt": "non-bmp"}},
    }
    with tempfile.TemporaryDirectory(prefix="tobkiri-legacy-vector-fixture-") as temporary:
        temporary_root = Path(temporary)
        for name, fixture in {**corpus, **extra_corpus}.items():
            root = temporary_root / "artifacts" / fixture["root_name"]
            if fixture["kind"] == "directory":
                root.mkdir(parents=True)
            for relative, content in fixture["files"].items():
                path = root / relative if relative else root
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content.encode("utf-8"))
            digest = artifacts.artifact_digest(root)
            if name in corpus:
                assert digest == fixture["sha256"]
            artifact_cases.append({"id": name, "input": fixture,
                "source_calls": ["artifact.artifact_digest"],
                "corpus_source": corpus_source if name in corpus else "generator fixture inputs",
                "expected": {"artifact_digest": digest}})
        linked = temporary_root / "symlink-artifact"
        linked.mkdir()
        target = temporary_root / "outside-fixture.txt"
        target.write_text("fixture", encoding="utf-8")
        (linked / "link").symlink_to(target)
        artifact_cases.append({"id": "symlink_rejected", "input": {"kind": "symlink-member"},
                               "source_calls": ["artifact.artifact_digest"],
                               "expected": outcome(lambda: artifacts.artifact_digest(linked))})

        pack_root = temporary_root / "signed-pack"
        pack_root.mkdir(mode=0o755)
        pack_files = {"handler.py": {"utf8": "VALUE = 'fixture'\n", "mode": 0o644},
                      "あ🚀.txt": {"utf8": "test-only\n", "mode": 0o644},
                      "run.sh": {"utf8": "#!/bin/sh\nexit 0\n", "mode": 0o755}}
        for name, contents in pack_files.items():
            path = pack_root / name
            path.write_bytes(contents["utf8"].encode("utf-8"))
            path.chmod(contents["mode"])
        manifest_inputs = {"pack_id": "local.fixture", "version": "1.2.3",
            "publisher_id": "fixture.publisher", "core_compatibility": ">=0",
            "contract_versions": {"fixture.z": "2.0.0", "fixture.a": "1.0.0"},
            "requested_capabilities": ["network.read", "fs.read", "network.read"],
            "build_provenance": {"fixture": True, "notes": "あ🚀"},
            "created_at": "2000-01-01T00:00:00+00:00"}
        unsigned = signatures.build_signed_manifest(pack_root, **manifest_inputs)
        private = Ed25519PrivateKey.from_private_bytes(TEST_ONLY_KEY)
        signed = signatures.sign_manifest(unsigned, private)
        public = private.public_key()
        signature_vector = {"source_calls": ["pack.build_signed_manifest", "pack._canonical_bytes",
                "pack.sign_manifest", "pack.verify_signed_manifest", "pack.verify_signed_pack"],
            "input": {"test_only_ed25519_seed_hex": TEST_ONLY_KEY.hex(),
                      "files": pack_files, "manifest_arguments": manifest_inputs},
            "expected": {"public_key_raw_hex": public.public_bytes(serialization.Encoding.Raw,
                    serialization.PublicFormat.Raw).hex(),
                "unsigned_manifest": unsigned,
                "unsigned_canonical_bytes": byte_evidence(signatures._canonical_bytes(unsigned)),
                "signed_manifest": signed,
                "signature_raw_hex": signatures._unb64url(signed["signature"]["value"]).hex(),
                "verify_envelope": signatures.verify_signed_manifest(signed, public),
                "verify_file_inventory": signatures.verify_signed_pack(pack_root, signed, public)}}
        tampered = {**signed, "version": "1.2.4"}
        signature_vector["expected"]["tampered_envelope"] = outcome(
            lambda: signatures.verify_signed_manifest(tampered, public))

    confirmation = "fixture only: あ🚀"
    return {
        "format": "tobkiri.legacy-differential-vectors.v1",
        "purpose": "Preparatory reference evidence for issue #1520 R11.4; no production architecture decision",
        "fixtures_only": True,
        "secrets": "Every seed/key/token is deterministic public test data. Never use these in production.",
        "provenance": {"git_commit": commit, "source_tree": str(source_root),
            "referenced_files_match_commit": True, "sources": sources, "source_calls": calls,
            "generator_sha256_hex": fingerprint(Path(__file__).read_bytes()),
            "python": sys.version.split()[0],
            "dependency_versions": {name: importlib.metadata.version(name) for name in ("cryptography", "packaging")}},
        "exact_bytes_convention": "Small outputs contain full hex and UTF-8 when valid. Large bounds contain a lossless concat/repeated-byte recipe plus actual length and SHA-256.",
        "protocol": {"strict_loads": protocol_cases, "canonical_direct_inputs": direct_cases,
                     "canonical_object_inputs": canonical_object_cases},
        "workflow_hmac": {"test_only_seal_key_hex": TEST_ONLY_KEY.hex(),
            "fixture_receiver": "WorkflowStoreV4.__new__; only _seal_key assigned; __init__ never called",
            "cases": workflow_cases},
        "authority": {"canonical_float_and_integer_cases": authority_cases,
            "interactive_confirmation": {"input": confirmation,
                "source_calls": ["authority.interactive_confirmation_digest"],
                "expected_digest": authority.interactive_confirmation_digest(confirmation)},
            "lease_codec": lease_vector,
            "audit_chain": {"fixture_boundary": "Original _append_audit and _verify_audit_rows against isolated :memory: SQLite; injected identity encryption/decryption sink. Encryption bytes, filesystem/process guards and transaction crash behavior are not covered.",
                "cases": audit_cases, "verified_chain": verified,
                "verification_source_call": "authority_store._verify_audit_rows",
                "corrupt_chain_expected": corrupt_audit}},
        "ed25519_pack": signature_vector,
        "artifact_tree": artifact_cases,
        "not_covered": ["Fernet ciphertext and key-store encryption compatibility",
            "Filesystem/process guards, transaction/crash semantics, revocation and live authority",
            "PackVM sealed application/environment manifests",
            "Chat RunSeal random nonce/marker format and change-request seal semantics",
            "All domain-specific content hashes and every legacy digest call site"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="Regenerate and compare without rewriting evidence")
    args = parser.parse_args()
    value = generate(args.source_root)
    encoded = (json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if args.check:
        if not args.output.is_file() or args.output.read_bytes() != encoded:
            raise SystemExit("legacy vector evidence differs; regenerate and inspect")
        print(f"legacy vectors reproducible: {len(encoded)} bytes sha256:{fingerprint(encoded)}")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(encoded)
        print(f"wrote {args.output}: {len(encoded)} bytes sha256:{fingerprint(encoded)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
