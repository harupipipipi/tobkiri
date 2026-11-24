#!/usr/bin/env python3
"""Freeze original Python byte codecs; run with the legacy checkout's .venv.

The oracle is unchanged cd7c20e9d source, not this generator. Inputs and public
test-only keys are deterministic. Store constructors and live runtime effects
are never invoked. At final Python deletion keep vectors, remove these tools.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib
import importlib.util
import inspect
import json
from pathlib import Path
import random
import sqlite3
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path("/Users/haru/.codex/worktrees/e8bc/tobikiri-pr-1322")
SEED = bytes(range(32))  # Public fixture data, never a production key.


def b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=SOURCE)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    source = args.source_root.resolve()
    sys.path.insert(0, str(source / "tobkiri_runtime"))
    strict = importlib.import_module("tobkiri_protocol.canonical")
    authority = importlib.import_module("core_runtime.authority.v4_models")
    store = importlib.import_module("core_runtime.authority.v4_store")
    workflow = importlib.import_module("core_runtime.workflow_v4.store")
    pack = importlib.import_module("core_runtime.pack_signature")
    capsule = importlib.import_module("tobkiri_protocol.workspace_capsule_v1")
    global_canonical = importlib.import_module("core_runtime.global_contracts.canonical")
    artifacts = importlib.import_module("tobkiri_protocol.platform_artifact")
    continuation = importlib.import_module("tobkiri_host.continuation_envelope")
    chain = importlib.import_module("tobkiri_host.continuation_chain")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    reference_spec = importlib.util.spec_from_file_location("legacy_reference", Path(__file__).with_name("freeze_reference.py"))
    reference_module = importlib.util.module_from_spec(reference_spec)
    reference_spec.loader.exec_module(reference_module)
    reference = reference_module.generate(source)
    commit = reference["provenance"]["git_commit"]
    sources = dict(reference["provenance"]["sources"])
    source_calls = dict(reference["provenance"]["source_calls"])
    for name, function in (
        ("global.canonical_json", global_canonical.canonical_json),
        ("capsule.canonical", capsule.canonical),
        ("capsule.digest", capsule.digest),
        ("continuation.seal_continuation_intent", continuation.seal_continuation_intent),
    ):
        path = Path(inspect.getsourcefile(function))
        relative = path.relative_to(source).as_posix()
        raw = path.read_bytes()
        committed = subprocess.check_output(["git", "-C", str(source), "show", f"{commit}:{relative}"])
        assert raw == committed
        lines, first = inspect.getsourcelines(function)
        sources[relative] = {"git_commit": commit, "sha256_hex": hashlib.sha256(raw).hexdigest(), "length": len(raw)}
        source_calls[name] = {"source": relative, "first_line": first,
            "qualified_name": function.__qualname__,
            "callable_source_sha256_hex": hashlib.sha256("".join(lines).encode()).hexdigest()}

    outputs: dict[Path, bytes] = {}
    def json_output(path: str, value: object) -> None:
        outputs[ROOT / path] = (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode()
    def lines_output(path: str, values: list[dict]) -> None:
        outputs[ROOT / path] = b"".join((json.dumps(v, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode() for v in values)

    inputs: list[bytes] = []
    def add(value: object) -> None:
        inputs.append(json.dumps(value, ensure_ascii=True, allow_nan=True, separators=(",", ":")).encode())
    for index in range(96):
        add({"z": [index, None, bool(index % 2)], "あ🚀": {"name": f"fixture-{index}", "control": chr(index % 32)}})
    integers = [0, -1, 1, 2**53-1, -(2**53-1), 2**53, -(2**53), 2**63-1, 2**63, -(2**63)-1]
    for index in range(96):
        integers += [2**(index + 1) + index, -(2**(index + 1) + index)]
    for value in integers:
        add(value)
    for value in [0.0, -0.0, 1.0, 0.1, 1e-5, 1e-4, 1e15, 1e16, 1e23, 5e-324, 1.7976931348623157e308, float("nan"), float("inf"), float("-inf")]:
        add(value)
    rng = random.Random(1520)
    for _ in range(256):
        add(struct.unpack("!d", rng.getrandbits(64).to_bytes(8, "big"))[0])
    for index in range(128):
        add({chr(index): f"x{index}é\u0301\u2028🚀", "\U00010000": index, "\ue000": -index})
    for index in range(80):
        inputs.append(f'{{"key{index}":0,"key{index}":{index + 1}}}'.encode())
    for depth in range(100):
        inputs.append(b"[" * depth + b"0" + b"]" * depth)
    inputs += [b'{"x":"\\ud800"}', b'{"\\udfff":1}', b'{"x":"\xff"}', b"[1,]", b"+1", b"01", b"1.", b"1e", b'{"a":1,"\\u0061":2}']
    variants = {
        "StrictV1": (strict.strict_loads, strict.canonical_json, "protocol.canonical_json"),
        "LegacyFloat": (lambda b: json.loads(b.decode("utf-8")), authority.canonical_json, "authority.canonical_json"),
        "LegacyPackSig": (lambda b: json.loads(b.decode("utf-8")), pack._canonical_bytes, "pack._canonical_bytes"),
    }
    for variant, (load, encode, call) in variants.items():
        rows = []
        for index, raw in enumerate(inputs):
            row = {"id": index, "variant": variant, "input_b64": b64(raw), "source_call": call}
            try:
                value = load(raw)
                result = encode(value)
                if variant == "StrictV1":
                    identity = strict.canonical_digest(value)
                elif variant == "LegacyFloat":
                    assert result == global_canonical.canonical_json(value) == capsule.canonical(value)
                    identity = authority.authority_digest(value)
                    assert identity == capsule.digest(result)
                else:
                    identity = "sha256:" + hashlib.sha256(result).hexdigest()  # Transport fingerprint of original codec bytes.
                row.update(expect="ok", output_b64=b64(result), digest=identity)
            except Exception as error:
                row.update(expect="error", error_class=f"{type(error).__module__}.{type(error).__name__}")
            rows.append(row)
        assert len(rows) >= 300
        lines_output(f"vectors/canon/{variant}.jsonl", rows)

    workflow_receiver = object.__new__(workflow.WorkflowStoreV4)
    workflow_receiver._seal_key = SEED
    workflow_rows, lease_rows, audit_rows, signature_rows, artifact_rows, continuation_rows = [], [], [], [], [], []
    lease_receiver = SimpleNamespace(_mac_key=bytes.fromhex(reference["authority"]["lease_codec"]["expected"]["derived_mac_key_hex"]), _assert_crypto_material=lambda: None)
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE authority_audit(sequence INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT,event_type TEXT,event_state TEXT,previous_digest TEXT,event_digest TEXT,encrypted_payload BLOB,created_at REAL)")
    audit_receiver = SimpleNamespace(_audit_fault=None, _encrypt=authority.canonical_json, _decrypt=lambda b: json.loads(b.decode()))
    private = Ed25519PrivateKey.from_private_bytes(SEED)
    with tempfile.TemporaryDirectory(prefix="legacy-vector-oracle-") as temporary:
        root = Path(temporary)
        for index in range(300):
            value = {"fixture": index, "message": f"あ🚀-{index}", "state": [None, False, index], "time_ms": 1750000000125 + index}
            payload, seal = workflow.WorkflowStoreV4._encode(workflow_receiver, value)
            workflow_rows.append({"input_b64": b64(payload.encode()), "seal": seal, "key_hex": SEED.hex(), "source_call": "workflow._encode"})
            lease_input = {**reference["authority"]["lease_codec"]["input"]["lease_definition"], "lease_id": f"fixture.lease.{index}", "issued_at": 1750000000.125 + index, "expires_at": 1750000001.0 + index}
            lease = authority.InvocationLease.from_dict(lease_input)
            lease_rows.append({"input_b64": b64(authority.canonical_json(lease.to_dict())), "lease_digest": lease.digest, "lease_id": lease.lease_id,
                "fernet_key_ascii": reference["authority"]["lease_codec"]["input"]["fernet_key_ascii"], "derived_key_hex": lease_receiver._mac_key.hex(),
                "token": store.AuthorityStore._encode_lease_token(lease_receiver, lease), "source_call": "authority_store._encode_lease_token"})
            audit_receiver._clock = lambda index=index: 1750000000.125 + index
            event_digest = store.AuthorityStore._append_audit(audit_receiver, connection, event_id=f"fixture.audit.{index}", event_type="fixture", event_state="recorded", payload=value)
            row = connection.execute("SELECT * FROM authority_audit ORDER BY sequence DESC LIMIT 1").fetchone()
            frame = {key: row[key] for key in ("event_id", "event_type", "event_state", "previous_digest", "created_at")}; frame["payload"] = value
            audit_rows.append({"input_b64": b64(authority.canonical_json(frame)), "digest": event_digest, "source_call": "authority_store._append_audit"})
            unsigned = {**reference["ed25519_pack"]["expected"]["unsigned_manifest"], "version": f"1.2.{index}", "build_provenance": {"fixture": index, "unicode": "あ🚀"}}
            signed = pack.sign_manifest(unsigned, private)
            assert pack.verify_signed_manifest(signed, private.public_key())["verified"]
            signature_rows.append({"input_b64": b64(pack._canonical_bytes(unsigned)), "signature_b64url": signed["signature"]["value"],
                "key_id": signed["signature"]["key_id"], "public_key_hex": reference["ed25519_pack"]["expected"]["public_key_raw_hex"], "test_only_seed_hex": SEED.hex(), "source_call": "pack.sign_manifest"})
            artifact = root / f"artifact-{index}"; artifact.mkdir()
            files = {"a/nested.txt": f"fixture-{index}\r\n", "z.txt": f"あ🚀-{index}"}
            for relative, contents in files.items():
                path = artifact / relative; path.parent.mkdir(exist_ok=True); path.write_text(contents, encoding="utf-8")
            artifact_rows.append({"files": files, "kind": "directory", "digest": artifacts.artifact_digest(artifact), "source_call": "artifact.artifact_digest"})
            identity = chain.ChainIdentity("fixture.domain", f"fixture.request.{index}", "sha256:" + "a" * 64, 1750000900.0)
            intent = {"kind": "tobkiri.packvm.continuation.intent.v2", "hop": 0, "target": {"contract_id": "fixture.contract.v1", "operation_id": "fixture.op"}, "payload": value, "state": {"index": index}}
            sealed = continuation.seal_continuation_intent(strict.canonical_json(intent), identity=identity, hop=0, previous_digest=None, target=("fixture.contract.v1", "fixture.op"), nonce=f"{index:048x}")
            continuation_rows.append({"input_b64": b64(sealed.frame), "digest": sealed.digest, "source_call": "continuation.seal_continuation_intent"})
    connection.close()
    for name, rows in (("workflow-hmac", workflow_rows), ("authority-lease", lease_rows), ("authority-audit", audit_rows), ("pack-ed25519", signature_rows), ("artifact-tree", artifact_rows), ("continuation-frame", continuation_rows)):
        lines_output(f"vectors/seals/{name}.jsonl", rows)
    json_output("vectors/legacy-vectors.json", reference)
    json_output("vectors/canon/provenance.json", {"git_commit": commit, "sources": sources, "source_calls": source_calls,
        "test_only_keys": True, "canonical_cases_per_variant": len(inputs), "seal_cases_per_format": 300,
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "audit_encryption_not_covered": "original audit hash routine runs against identity encryption fixture sink and :memory: SQLite"})
    for path, content in outputs.items():
        if args.check:
            assert path.read_bytes() == content, f"changed vectors: {path}"
        else:
            path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
    print(f"{'verified' if args.check else 'wrote'} {len(outputs)} files; {len(inputs)} cases per canonical variant; 300 per seal format")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
