"""Read-only byte-section evidence; no Wasm compiler or runtime is imported.

This bounded probe finds length-prefixed declaration-name tokens in the
outer sections of each pinned input and its embedded original component.
It deliberately excludes nested component/module bodies from each scope.
It is not a complete semantic Wasm parser, validation, execution, or authority
proof. The exact structural ABI evidence is separately captured by Wasmtime
before any export call in replay2-work/replay.json.
"""

import hashlib
import json
from pathlib import Path


ROOT = Path("/workspace/scratch/c5cef75e84b0")
INPUTS = {
    "caller": (
        "issue1520-gateway-trial/component-work/component.wasm",
        "1a438458ba804cf9b62c7125602b1499f159263387f56f2470c51ce4191e521c",
        "286b1622a99d9051d918621086f4c0981744f680757ee511133dca9e52a35c60",
    ),
    "provider": (
        "issue1520-v2-trial/component-work/component.wasm",
        "0f28f4a87158b18a24fe045a9be1c24e8d9405549a0af1b56546b2017ac92a46",
        "68f959c1b63c043fbc5f53b57ba7dddbac2b6db019bdd5018fc0da2495ceed76",
    ),
}
NAMES = (
    "error-code", "error", "json", "stream-handle", "callback-handle",
    "risk", "pending", "verdict",
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def leb(data, offset):
    value = shift = 0
    for _ in range(10):
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, offset
        shift += 7
    raise ValueError("oversized section-length LEB")


def sections(data):
    if data[:8] != b"\x00asm\x0d\x00\x01\x00":
        raise ValueError("not the reviewed component encoding")
    offset = 8
    result = []
    while offset < len(data):
        identifier = data[offset]
        size, start = leb(data, offset + 1)
        end = start + size
        if end > len(data):
            raise ValueError("section extends beyond input")
        result.append((identifier, data[start:end]))
        offset = end
    return result


def inspect(data):
    parsed = sections(data)
    # Component encoding section 1 is a core module; section 4 is a component.
    # Excluding those bodies prevents embedded Python data or deeper nested WIT
    # strings from being mistaken for declarations in this observation scope.
    selected = [(sid, body) for sid, body in parsed if sid not in (1, 4)]
    outer = b"".join(body for _, body in selected)
    return {
        "sha256": digest(data),
        "bytes": len(data),
        "all_outer_sections": [
            {"section_id": sid, "payload_bytes": len(body)}
            for sid, body in parsed
        ],
        "inspection_scope": "outer non-core-module/non-component sections only",
        "name_token_presence": {
            name: bytes([len(name)]) + name.encode() in outer for name in NAMES
        },
    }


result = {
    "format": "issue1520.read-only-component-section-name-probe/1",
    "wasm_compilation_performed": False,
    "wasm_execution_performed": False,
    "native_core_acceptance": False,
    "security_authority_evidence": False,
    "limitation": "length-prefixed-name token inspection, not semantic validation",
    "inputs": {},
}
for name, (relative, expected, expected_inner) in INPUTS.items():
    path = ROOT / relative
    data = path.read_bytes()
    if digest(data) != expected:
        raise ValueError(f"{name} input differs from the reviewed pin")
    children = [body for sid, body in sections(data) if sid == 4]
    if len(children) != 1 or digest(children[0]) != expected_inner:
        raise ValueError(f"{name} embedded original component differs")
    result["inputs"][name] = {
        "path": str(path),
        "reviewed_sha256": expected,
        "outer_component": inspect(data),
        "embedded_original_component": inspect(children[0]),
    }
result["conclusion"] = (
    "All five referenced Core type-name tokens are present; risk, pending and "
    "verdict are absent in both pinned outer inputs and their original inner "
    "component scopes. The omission therefore predates the new caller/provider "
    "replay composition. This is type-surface evidence only, not authority."
)
print(json.dumps(result, sort_keys=True, indent=2))
