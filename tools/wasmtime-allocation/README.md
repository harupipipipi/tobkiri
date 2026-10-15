# Pinned canonical ABI allocation work

This is an **incomplete source checkpoint** against Wasmtime 47.0.4. A scoped
library metadata check passed after two reborrow repairs and API documentation;
it emitted nine dead-code warnings for intentionally unavailable retained paths.
**That successful check does not include the later async/fiber/shared/resource
changes described below. The latest source is unbuilt.**
It does not establish runtime availability or complete memory bounds. Source
provenance and per-file hashes are in `source-inventory.json`; the shared global
Cargo cache was read only. Root alone controls Cargo patch/lock and build leases.

## Native graph boundary already measured

`crates/tobkiri-canon/src/ownership.rs` keeps immutable opaque CoreValue/CoreBuffer
allocations and their inline leases together. The custom shared control block
is freed before reservation release; handles share without physical copying.
New shared containers fund keys/reference metadata and retain each child's
original owner. Explicit physical copies reserve the destination before any
source can be freed. Objects use binary search and funded in-capacity insertion,
with no stable-sort temporary, BTreeMap allocator, ordinary owned escape or
production format!/collect/Box::new/Arc::new path. Exact allocation layout sizes
are charged; allocator-internal bookkeeping/rounding is not yet independently
measured. The external adapter's Arc is supported only through the approved
fixed-slot custody/reaper lifetime contract; a field inside that Arc cannot
release its own control-block charge safely.

The historical scoped native checkpoint had 60 tests
(Canon23/Manifest14/Profile23), strict Clippy and owned rustfmt passing. A later
pure Canon/Gate/Store lease actually ran all 10 then-current Canon ownership cases,
including pre-read write_chunk, producer array/parallel-slot builders and
LeasedShared final-drop order. Those 10 passed. That later lease did not rerun
Manifest/Profile or strict Clippy; its initial before-source hashes were not
captured (post-source hashes only), as recorded in the private receipt. It is
not a workspace or Wasmtime result. The later additive digest/eq_guest source
changes are unbuilt: three new ownership tests cover all 885 frozen StrictV1
oracle cases, complete-work denials, and non-Strict rejection; one new codecs
test checks Digest wire/Debug/Hash/order compatibility. None of those four new
tests has run. The previous ownership10 receipt precedes these source changes.

Digest now holds exactly 71 ASCII bytes inline. Successful parse/hash/clone no
longer creates a String, and the original tagged text, serde, Debug, ordering
and str Hash input are retained. Guest hashing streams the existing StrictV1
emitter into SHA256 after complete validation/work funding; it does not create
a serialized buffer or silently tag Float/BigInt. Guest equality prevalidates
both complete graphs, funds all byte/node comparison work, then borrows them.
The verified generating frame supplies the work budget, and original owners
remain held; fixed Digest metadata must live in an already funded envelope.

## Current source coverage (latest changes unbuilt)

| Surface | Source status | Remaining proof |
|---|---|---|
| Dynamic Val strings, UTF16/Latin1 transcodes, lists, maps, record names, tuples, variants, flags | BudgetedString/Vec/Box precharge, immutable shared ownership; each physical alias copy separately charged; scoped metadata check passed | Failure-at-each-point and hostile WAT tests |
| Dynamic Lift/load and lower/store | Depth<=64 and element/work accounting before recursive conversion | Counter accumulation and declared-width integration tests |
| Dynamic synchronous host params/results | Reserve combined scratch capacity before callback; only private unique scratch mutation | Pre-callback allocator instrumentation and retained result test |
| Dynamic export result storage | call_owned/call_async_owned return immutable funded vectors | Fiber path below is still incomplete |
| Dynamic result iterator | Stack-resident iterator replaces Box<dyn Iterator> | Type check and flat/indirect result tests |
| VM resource slot table, borrow lenders, synchronous call scopes, host generation metadata | Private mutable FundedVec reserves a complete destination while OLD storage remains charged, moves existing elements without copying payloads, then frees OLD before releasing its lease | Latest source is unbuilt; cancellation/identity/revision tests and transactional failure review |
| Canonical guest realloc | Checked OLD+NEW work/size before realloc; actual linear memory funding is the Store ResourceLimiter's responsibility | Root/B integration must prove initial/growth backing ownership; size counter alone is not byte funding |
| Static/generated component adapters and String/Vec native lifts | Explicit Unavailable before static entry/native owned lift; generated WASI has no fallback | Unsupported until complete allocation coverage |
| Dynamic async host callback future allocation | BudgetedFuture erased pinned state and func_new_async_owned source; closure Box plus custom shared HostFunc control storage funded before allocation | Type check, cancellation/final-drop and all17 integration proof pending; ordinary unowned API still Unavailable |
| Component concurrency/native future/stream/error-context/fixed-length list | Explicit Unavailable at dynamic lift/load/lower/store and async concurrent entry | Full task/resource/future storage proof pending |
| Native ResourceTable boxed data and child relations | new_owned factory funds outer dataBox/entries/sorted child metadata; unowned default table pushes deny before allocation; dynamic resource values still denied | Owned core destructor callback factory and nominal identity proof pending; inner payload ownership remains the producer's responsibility |
| Fiber stack, executor, VM state, trampoline closure/control allocation | OnDemand Unix Native stack rounds pages plus guard before allocation, retains lease through cache, and precharges trampoline Box exact size; custom/pooling/Pulley/nonUnix paths deny | Source is unbuilt; cancellation/retention and constructor failure tests pending |
| JIT/module/instance/compiled metadata and allocator internals | Outside current payload proof and not claimed bounded | Explicit source inventory and admission/funding decision required |
| Error construction/backtrace/diagnostic formatting | Unresolved heap paths in upstream bail!/format_err!/context | Bounded/prepaid error path required; counter-only lifting does not cover this |

All current Tobkiri async Gate/HTTP/Flow imports use func_new_async. Consequently
the ordinary API cannot link them. B must migrate to the owned API and retain
unavailable paths until their entire allocation coverage is proven. Core resource
destructor registration remains explicitly unavailable before its original
unbudgeted Arc/Box allocation. Earlier M2 receipts cannot be reused for this
source. Switching to synchronous callbacks to bypass Gate approval is forbidden.

The same pinned `wasmtime-internal-fiber@47.0.4` source is now vendored, with
license/archive hashes in the inventory. Its checked mmap constructor owns the
mapping before mprotect, returns any unreleased mapping after a failed munmap,
and exposes a failure counter. Checked release confirms munmap before quota
refund; uncertain mapping and owner lease are deliberately retained without a
timeout, including the OS owner lock. Three source-only safe KiB tests cover
protection failure cleanup, simulated cleanup uncertainty followed by explicit
successful release, and size overflow. These tests have not run. Ordinary legacy
unbudgeted constructors do not provide a runtime funding guarantee.

`focused-review-set.json` lists full Canon Digest/ownership source and ownership
tests, full allocation
containers, the upstream delta for all existing modified vendor sources, and
this inventory note. It is an immutable source review set under 200KiB; added
allocation source is included in full rather than duplicated in the delta.

## Next serialized type check

Root may grant this check after source freeze; workers must not run it while
Cargo is held:

```
CARGO_TARGET_DIR=../.work/cargo-target CARGO_BUILD_JOBS=2 CARGO_INCREMENTAL=0 \
  cargo check --offline --locked -p wasmtime --lib
```

Wasmtime is a patched dependency outside the workspace, so Cargo rejects feature
flags for this package selector before compilation. Root confirmed the actual
locked resolution: anyhow, async, cache, component-model, cranelift, once_cell,
parallel-compilation, runtime, std, wasmtime-jit-icache-coherence. No upstream
workspace member or test suite was added. Observed metadata cache growth was
about 207MiB, with a 5.5GiB stop floor. This was not a full Wasmtime test/build
or a workspace verification command.

Required adversarial tests use safe KiB limits: repeated aliases into nested
containers; overflow/depth/work rejection; allocation failure at every point;
pre-callback denial; retained result after Store/Session drop; source lowering
and destination reserve-before-release; async cancel/retain; nominal resource
identity; competing revisions with the same stable owner; custody last-reference
factory/reaper races. No huge OOM test or global allocator/TLS interception.
