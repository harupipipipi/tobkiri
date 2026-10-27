# Offline Python Normal Pack authoring

`core_runtime.pack_authoring` is a public authoring API for capability-free
Python Functions in `normal_sandbox` Packs. It uses the existing Pack v4 schemas,
canonical digest rules and Host artifact compiler. It produces executable
metadata, not permission to execute. It does not install, select, activate or
sign a Pack, change publisher trust, or approve a VM.

## Existing PackVM Python ABI

The existing `tobkiri.python-pack-v4` backend uses `runtime_abi: python3.13`.
Each explicit implementation file must define a synchronous top-level function:

```python
def tobkiri_packvm_invoke(operation_id: str, payload: dict) -> dict:
    if operation_id == "echo":
        return {"message": payload["message"]}
    raise ValueError("unknown operation")
```

The guest loads the captured implementation file and calls this function with
the selected Operation and its input. The returned object is the Contract
outcome. The guest wraps it in the existing PackVM terminal result envelope;
the author does not manufacture that envelope. Avoid `tobkiri.packvm.*` control
frames. This producer supports pure outcomes only, with no Host bridge or
capability requests. It does not prove a module is pure by inspecting Python;
the runtime sandbox remains the execution boundary.

Keep the module self-contained or use the Python standard library. This API
does not install dependencies. Import-time work must not write stdout, spawn
processes, access the network or acquire Host authority. The producer parses
the AST to check the entry shape but never imports or executes author code.

## Public producer

```python
import json
from pathlib import Path

from core_runtime.pack_authoring import PythonPackFunction, build_python_pack
from tobkiri_protocol.canonical import canonical_digest

contract = json.loads(Path("my-contract.v4.json").read_text())
# Revision pins the canonical Contract fields, excluding provenance/revision.
contract["revision_digest"] = canonical_digest({
    key: value for key, value in contract.items()
    if key not in {"revision_digest", "provenance"}
})
root = build_python_pack(
    Path("dist/example.echo"),
    pack_id="example.echo",
    version="1.0.0",
    display_name="Example echo",
    contracts=[contract],
    functions=[PythonPackFunction(
        function_id="example.echo.function",
        contract_id=contract["contract_id"],
        operation_ids=("echo",),
        implementation_path="runtime/main.py",
        source=Path("main.py").read_bytes(),
    )],
    assets={"prompt.md": b"An optional immutable resource.\n"},
)
```

Contract inputs conform to `tobkiri_protocol/schemas/contract_v4.schema.json`:
schema catalog keys are `canonical_digest(schema)`; Operations reference those
keys; Contract revisions use the canonical rule shown above. The producer
preserves the author's status and provenance and checks the revision. It never
promotes `draft` to `accepted`.

For this minimal producer, provider isolation must be `sandbox`, failure must
be `fail_closed`, required capabilities must be empty, and each Operation must
have declarative scope with effects `[]` or `["pure"]`. Every declared Operation
must bind exactly once. Timeout defaults are 30000/300000 ms; explicit limits
must satisfy `1 <= default <= hard <= 300000`. Functions have role `pure`,
isolation `pack_vm`, domain `sandbox.default.v1`, and materialization `on_demand`.

The producer captures supplied bytes, rejects unsafe/reserved paths and duplicate
bindings, computes the complete artifact chain, and calls the actual
`tobkiri_host.artifact_compiler.compile_pack_root` before publishing the new
directory. Output is deterministic for identical inputs. Existing destinations,
including empty directories and symlink ancestors, are refused. Individual
input files are limited to 1 MiB, total inputs to 16 MiB and 256 files.

The public compiler can also verify an output later:

```python
from tobkiri_host.artifact_compiler import compile_pack_root

compiled = compile_pack_root(root)
assert compiled.routes[(contract["contract_id"], "echo")]["runtime_abi"] == "python3.13"
```

Compilation is offline validation. Actual dispatch requires publisher signature,
Host install policy and signed external admission, a selected Profile/plan,
activation and the normal runtime/VM approvals. The existing signing APIs
`core_runtime.pack_signature.build_signed_manifest` and `sign_manifest` do not
grant authority. `core_runtime.external_pack_catalog_v4.admit_signed_external_pack`
is a Host installation port, subject to exact Host-owned policy. An isolated
test policy must never be presented as live approval.

## Scope and remaining integration

This API makes pure Normal Pack components executable by the existing compiler.
It does not create a CLI Shell/Application or add conversation completion events,
durable state, hidden-context injection, or automatic flow/prompt binding. Assets
such as flow/prompt files are immutable resources until a formally selected
consumer uses them. Component compiler success is separate from product UI,
Broker dispatch, VM execution and provider API acceptance.
