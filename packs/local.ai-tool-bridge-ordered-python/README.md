# Original Python tool bridge: ordered transport candidate

This is a separately versioned migration candidate, not an activated provider or
proof of final native Core acceptance. The Pack WIT string domain is
`ordered-finite-json-v1`; the copied canonical Core WIT is unchanged.

The original normalizer and `GlobalContractInvocationError` are captured verbatim
from the repository, with byte-slice provenance. Host factories, capture plumbing,
contract clients and authority imports are not present in the guest. All three
registered operation IDs follow the original Host factory's `normalize` mapping.
All four original caller edges are inventoried in `python/inventory.json`.

## Domain

Both request and successful response are bounded by 4 MiB UTF-8, depth 64 (root
zero), and 100,000 nodes including keys. Allowed exact native types are null,
bool, safe integer (absolute value at most 2^53-1), finite binary64, string, list,
and insertion-ordered string-keyed dict. Floating 1.0 stays distinct from integer
1 and negative zero keeps its sign. Outer duplicate keys, surrogates, cycles and
subclasses are rejected. Inner argument JSON is opaque to the transport and is
parsed only by the original normalizer, including its original duplicate-key
behavior. Original outputs containing big integers, non-finite numbers or excess
depth are explicit profile exclusions, never counted as successful semantic parity.

These exclusions are not all equivalent to the old Host boundary. Source-path
inspection of the original Broker output validation and `_digest` shows that
the six bigint/depth probes can survive ordinary nested Host calls: the output
schema requires only an object, and audit JSON serialization imposes neither
the safe-integer restriction nor depth 64. The three NaN probes instead fail
Broker audit serialization (`allow_nan=False`) with `ProviderExecutionError`,
not the candidate's typed INVALID. A later PackVM-specific Gateway response
boundary rejects bigint/depth, but ordinary Host calls do not use that boundary.
These are six feature gaps and three error-boundary differences, based on source
inspection, not a completed full-stack Host replay. The existing direct oracle
does not prove that complete Host path. All nine remain excluded from parity.

The successful result is the original result, not an error envelope. Original
normalization failures and transport exclusions become canonical typed INVALID.
Original exception class/code/message are retained in oracle evidence; the native
Core public error is independently sanitized to `provider reported invalid input`.
CoreStrictV1 is not relaxed or redefined.

## Reproduction and evidence

1. `prepare_ordered_tool_bridge.py --output <directory>` extracts and pins source.
2. `freeze_ordered_tool_bridge.py --output <oracle.json>` requires exact native
   CPython 3.13.5 / Unicode 15.1.0 and records real original outputs.
3. `stage_ordered_tool_bridge.py --output <new-directory>` creates a deterministic
   SDK build/3 plan without building or changing an active profile. Its ninth
   input, reviewed-inputs.json, pins the exact original oracle bytes, freeze
   script, protocol, source provenance and complete input case set using hashes
   only. Oracle outputs and freeze code are not staged inside the guest.
   Replay requires the full reviewed source manifest, manifest hash and plan hash,
   including the original source-to-provenance link and every adapter/codec/WIT
   input, rather than trusting a provenance hash alone.
4. Build only using separately verified producer tooling. Official componentize-py
   0.25.0's guest is CPython 3.14 / Unicode 16, not the original interpreter.
5. Replay the resulting typed component against the frozen original oracle. Guest
   runtime-info reports actual guest facts; it is not inferred from host Python.
   The reviewed custom producer target is CPython 3.13.16 / Unicode 15.1.0;
   the original reference remains CPython 3.13.5. Replay requires that exact
   reviewed guest pair AND exact original behavioral parity, not a minor-version
   match or an assumption that two patch versions behave identically.

The U+1C89 repr probe deliberately preserves original Unicode 15.1 expectations.
A newer guest's divergent result must remain a failing comparison. No historical
lost artifact, count or test run is evidence for this reconstructed candidate.

## Bounded native Pack materialization

`materialize_ordered_tool_bridge.py` consumes an existing component and its
adjacent `.build.json` receipt. Pass `--artifact`, `--artifact-sha256`,
`--receipt-sha256`, and a fresh `--output` directory. It performs no build,
producer execution, guest instantiation, or profile activation. Missing or
changed artifacts, symlink paths, duplicate evidence keys, and incomplete source
bindings fail closed. Component capture is capped at the native Pack member
limit of 128 MiB before reading, with a bounded limit-plus-one stream read.
A hash-pinned receipt is provenance, not proof of runtime
semantics or an independently trusted signature.

The generated Pack owns `local.ai-tool-bridge-ordered-python.bridge@1.0`, mapped
to the WIT `bridge` interface, and includes `wit/bridge.shape.json`, canonical
Core WIT, component, receipt and config. The frozen oracle stays external; only
its digest is reported in the materialization receipt, so expected answers are
not shipped in the Pack. Every included file is
size/hash-bound by `pack.json`. There are no authority ports, filesystem,
network, process, secret, host-I/O, or guest audit permissions. Native Core's
own dispatch audit is not dependent on guest audit permission. Output is an
inactive structural candidate only; its component header and hash bindings do
not establish ABI validity. Native manifest verification, actual component ABI
validation, and Core acceptance remain separate gates. No generated production
Pack is checked in here.

The ordered transport's 4 MiB string bound is not a promise of 4 MiB native
payload capacity. Native CoreStrictV1 also caps the *outer canonical JSON* at
4 MiB. Quoting an ordered JSON string adds escapes and can exceed that cap.
Materialization preflights all frozen request records and successful string
responses and reports maxima. Native admission additionally requires both
outer envelopes to fit; CoreStrictV1 and its limit are not relaxed. Input and
success strings remain opaque: never parse/re-serialize them as Core objects.

### Required real Core acceptance recipe

The following is a pending integration recipe, not an executed test result:

1. Materialize the real component with explicit artifact/receipt pins. Verify
   the directory using `tobkiri_manifest::verify_directory`, retaining its
   verified hash and a `PackPin`. Reject missing/changed bytes before startup.
   Use an isolated test root, not the active application profile.
2. Follow the real fixture in `crates/tobkiri-core/tests/provider_proxy.rs`:
   construct `Core` with its actual Wasm execution factory and Gate, load the
   verified candidate Pack, and save/start a temporary Profile bound to the
   exact provided shape. Do not substitute a `ProviderEntryBridge`, unknown-
   imports-as-traps Linker, synthetic component, or direct guest export call.
3. Call `Session::call_user_port` for `runtime-info` with empty arguments.
   Require the observed guest Python 3.13.16 / Unicode 15.1.0 pair. A receipt's
   toolchain label, host interpreter, or passing startup is not this evidence.
4. Replay all 33 current frozen cases through `call_user_port`, operation
   `normalize`, and an outer argument record containing `operation` and
   `payload` strings. Copy `input_json` unchanged into `payload`. Successful
   returned `CanonValue::String` bytes must equal `transport.output_json`
   exactly, including insertion order, numeric spelling and nested JSON text.
5. For original invalid cases, require canonical typed `invalid` and the public
   fixed message `provider reported invalid input`; do not expose guest-private
   exception text. Excluded-domain cases must be rejected and counted separately,
   never as successful semantic parity. Record every case, not only a pass count.
6. Read the actual session audit database and require dispatch/outcome events
   identifying this physical candidate Pack. Add a separate consumer fixture
   with its required shape declared but effective port permission removed:
   require Gate denial, a denied audit event, and no provider dispatch. A User
   utility call alone is not a substitute for this consumer-permission test.
7. Close the session through the public facade. Save exact Pack/component/
   receipt/oracle hashes and actual runtime observations with the case results.
   Keep native owner acceptance separate from component replay and packaging.

No native acceptance is claimed until this recipe executes with the real
component and the complete required evidence passes.
