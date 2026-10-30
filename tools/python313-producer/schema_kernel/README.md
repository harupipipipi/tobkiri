# Private original-object Rust schema port

This is an experimental Rust/PyO3 port of jsonschema 4.26.0 traversal and all
36 Draft 2020-12 keyword handlers. It is not connected to product Setup or the
product validator. Source 23400a4a7 passed actual native compilation and its
64-case / 15-group functional comparison in CI 41092195794. Its actual Wasm
call still exhausted the unchanged fuel limit; native parity is not Wasm or
product acceptance.

## Current candidate boundary

The current unverified source translates the pinned `_keywords.py` definitions
at build time, before the first jsonschema import. All 36 public signatures,
parameter names, docstrings and generator status are preserved. Normal module
execution connects the real class mappings to those public definitions. Traversal
always executes the selected actual callable, so same-object code changes, saved
aliases, existing validators and suspended generators use ordinary Python rules.
No live module reload, function-code rewrite or code-identity audit guard is used.

The authenticated original source remains unchanged in the input package and is
also executed as an independent original-keyword oracle module for boundary
comparisons. Both native and guest captures record original/derived source hashes;
no schema, validation result or resolver state is prepared by this translation.
The guest capture still checks its original 64MiB total after adding the oracle.
This is a derived experimental source artifact, not an unchanged upstream wheel.
Full package-metadata/resource, frame/refcount and product parity remain unproved.

The default type bypass is removed; real checker and predicate callables execute
normally. Previous type-shortcut fuel results do not qualify this new structure.
API18 adds same-function keyword/checker/predicate code changes, preselected and
saved handlers, started generators, replacement mappings, exception identity and
runtime audit observations. FFI keyword names are now opaque Rust metadata;
forged Python tuples cannot reach the four-pointer vectorcall stack.

## Historical boundary and experiments

`adapter.py` retains the real Python Validator classes and their public state,
VALIDATORS dictionaries, type/format checkers, evolve, registry/resolver and error
constructors. It captures each original traversal closure's actual applicable
selector/specification rather than recomputing it from mutable class metadata.
Thin generator methods delegate to the Rust lazy traversal. Module create/extend
factory results are installed without replacing the genuine generated class;
installation metadata uses a WeakSet. Previously acquired factory references and
all remaining API routes still require coverage; this wrapper alone is not proof
that every subsequently created class executes the Rust path. Rust dispatches the
36 original builtin callables by actual identity; explicit extension and older
draft-specific callables retain their normal dispatch inside the same Wasm
budget. A failed native builtin is never retried through the old Python handler.
No standalone validator class, serde values, reconstructed references or parallel
resolver/cache is introduced. Regex, Python arithmetic/Fraction, original errors
and evaluated-member helpers remain genuine Python operations inside Wasm.

The Rust iterators start work on first next, preserve branch consumption/order,
close after errors, expose GC traversal/clear and release retained references on
completion/close. A genuine thin Python generator surrounds each Rust builtin
iterator so finalization delegates close to an externally retained child, as in
the original generator. This delegate performs no validation of its own and
adds no Rust finalizer. All public API/lifetime equivalence remains to be verified.

## Required actual qualification

The CI probe uses the original pinned wheel/CPython resource capture and actual
successful rpds extension. The original 54e7da008997a3eb45876cbe18459dba9902cc9d
source produces the unchanged 64-case reference: 42 original schema files and
22 boundary inputs. Only raw JSON bytes of case names/schemas enter the guest;
input objects are materialized inside the actual request, before checking them. Expected results and
expected meta-registry objects stay outside the guest; it uses its genuine
original classes and registry. Its 64 actual selfchecks run in one real Core
NativeData invocation under unchanged 64 MiB / 2-billion fuel / original absolute
30-second limits. Compare first errors, ordered context, actual references,
parent/cause, paths and complete rendering hashes, plus the real meta-registry
and class format-checker membership. Fifteen additional actual public API groups
cover lazy resume/close, interleaving, PEP479, false-schema early paths, evolve
and unregistered extension-class reclamation, unstarted cursors, final reference
release and cyclic GC with an externally retained delegated child. The same
checks also run against the actual original native classes before the guest test.
At source 638bf7489, CI 41070883433 passed the actual Rust extension build and
ABI checks and entered original producer preinitialization. It stopped because
the test App sealed its loader before sysconfig initialized its original config
module. The order is corrected to match the original probe: target module/config
initialization, then seal. No Core call or 64-case result was reached at that
source. The next CI 41071710332 at source 70c926adb generated the component and
reached the actual Core call, then failed the positive remaining-fuel lifetime
condition. Its numeric remaining fuel/trap was not printed before validation;
full bounded diagnostics are now retained before that validation can raise.
No returned 64-case result or numeric fuel measurement is claimed for that run.
CI 41074411036 at f4f95793b then confirmed the actual failure: all 2 billion
fuel consumed, remaining fuel 0, elapsed 1.311426821 seconds, original deadline
preserved, no deadline/unreachable trap. The original native 12 API checks passed;
the guest returned no 64-case result and did not reach its API suite.

The next source adds narrow default-type dispatch to the already qualified Rust
primitives: exact original Draft202012 class, actual unchanged default checker,
plain str among seven names, and ordinary default attribute/method metadata.
The guard reads class dictionaries rather than evaluating a TYPE_CHECKER
descriptor twice. Custom attribute accessors/checker methods also keep original
dispatch. Integer additionally requires an exact builtin
int/bool/float to preserve arbitrary float-subclass return objects. Other classes,
custom checkers/redefinitions, unknown/nonstandard names and subclasses requiring
original behavior dispatch to the actual original method before any evaluation.
Native failures are not retried. Real numbers.Number ABC registration/cache
behavior remains in Python inside Wasm. Actual execution counters retain no type
or validation results. Added comparisons include non-finite floats, bigint,
subclasses/overridden methods and exceptions, Number ABC registration/cache clear,
unknown/surrogate/nonstandard names, checker changes and method overrides.
The optimized source passed native compilation/parity at 2f853413f; its Wasm
call still exhausted the original fuel, as recorded below. The complete Wasm
64-case and cold catalog gates remain required.

This is not the full 45-cold-call / 305-entry / 453-read catalog or ordinary Flow,
real OS owner, original GUI, 1B conversation or product parity proof. Per-handler
execution coverage and remaining supported API contracts still require evidence.
No cap or acceptance condition is waived. No schema/result cache is warmed in
producer preinitialization; only genuine modules and callable metadata initialize.

## Earlier actual evidence and rejected dependency

The prior primitives-only source e436a6d5f passed CI 41065687808 / job
124096340005: actual extension build, limited-API inspection, original producer
import/component generation and real Core call with actual root/nested identity,
mutation aliases and 21 default type cases. This did not run schema validation.
`retain_objects` and `is_original_json_type` remain private building blocks.
Unknown type names raise private ValueError; they are not the full TypeChecker
exception API, which the port retains through the real Validator.is_type.

The earlier jsonschema 0.60.2 library candidate was rejected: CI 41058189370
entered Rust but trapped on unavailable WASI randomness; known serde reference/
ordering losses were separate structural gaps. Its 64 comparisons were not run.
No entropy forwarding, fixed seed or preinitialized registry rescued it.
Only exact PyO3 0.30.1 remains. The 15-package source lock was authenticated by
CI 41065264251, then consumed frozen/offline; no local dependency downloads or
compilation are part of this workflow. Upstream-derived code retains the license
in [COPYING.jsonschema](COPYING.jsonschema).

[Original scope](../../../docs/migration/1520/schema-port-scope.md).
[Cleanup notes](../../../docs/migration/1520/cleanup-notes.md).

## Native functional precheck and invalid type-name keys

CI 41080438535 at ea0e6160a stopped in the original native API fixture before
building the optimized candidate: original rpds lookup raised BaseException-based
PanicException for a non-str type-name key. The documented parameter is str.
The typed API suite retains its string/subclass comparisons; None/list/bytes
parameter-violation cases remain in a separate native comparison that observes
the original actual exception or panic. Non-plain-str public calls dispatch in
Python before entering the new Rust type helper, preserving that boundary.
An independent original/candidate Wasm negative-trap comparison is still unproved;
these native observations do not establish full API or denial parity.

The new CI-only native precheck compiles the same source/PyO3 ABI as a real native
extension, compares all 64 cases with strict bool/int/float distinctions, checks
all 15 API groups and the separate invalid-key outcomes before the expensive Core
consumer build. It records no Wasm execution/budget/GUI/product acceptance. The
subsequent unchanged Wasm gate remains mandatory. This precheck is not shipped.

## Actual 2f comparison and next evolve port

CI 41086108540 / job 124167563976 at 2f853413f passed the native Rust extension
comparison: all 64 original cases and all 13 API groups, plus actual None/list/
bytes argument-violation outcomes matching the original. Native type dispatch
counts were 38,308 guarded defaults / 32 explicit originals. The Wasm call still
exhausted 2 billion fuel (remaining 0, elapsed 2.028240556 seconds, no deadline or
unreachable trap). No Wasm comparison passed and no fuel improvement is claimed.

The next source ports the original evolve inheritance loop into Rust. It retains
the actual original closure list, globals and Function.__builtins__ dictionary;
it does not freeze an iteration snapshot. Only the matching original method code
is replaced; custom/subclass evolve methods keep their own behavior. Schema is
read even when supplied, the current selector/global-builtin lookup is respected,
missing aliases read actual attributes in original order, and the selected actual
constructor is called with original object references. The fourteenth API group
compares getter order, live field-list mutation, explicit None, namespace changes,
NameError.name, constructor failures, draft switching and overrides. Context GC
visits the added actual references. At source 34994f41b, CI 41090025133 / job
124181172734 passed the original reference/API step and the actual native Rust
comparison step, including all 64 cases and 14 API groups. The same job built
the actual native Core consumer and component, but its Core/Wasm call exhausted
all 2 billion fuel after 2.049135565 seconds, with the original deadline preserved
and no deadline/unreachable trap. It returned no 64-case result. Overall CI failed;
no fuel improvement is claimed. The stack includes genuine generated-constructor
initialization under the Rust keyword iterator, not proof of its whole cost.

The next narrow port replaces only the original `__attrs_post_init__` body with
Rust, guarded by actual original code identity. The genuine attrs constructor,
class, field initialization, registry/resource/resolver and custom method overrides
remain in place. Original closure callables and function namespaces are retained.
Resolver creation, boolean short-circuit attribute reads, live keyword selection,
commit-after-comprehension behavior and legacy scope callbacks keep their original
order. CI 41092195794 / job 124188678135 at 23400a4a7 passed actual native
compilation, all 64 comparisons and all 15 API groups (including repeated calls
and partial failure state). The actual extension was 813,072 bytes with SHA-256
fa836ad19b0453bb721d757340483c22bacf9afaa708003465b64dbe9691c455.
Core/component builds passed, but the actual Wasm invocation again consumed all
2 billion fuel after 2.059816844 seconds, retaining the original deadline without
deadline/unreachable traps. It returned no 64-case result; overall CI failed.
The stack was inside a Python method call from the Rust keyword iterator. This
does not identify its total cost or prove fuel improvement. Remaining hot work
needs measured cost before further speculative ports; original limits stay fixed.
Detailed temporary-object/finalization parity and full API coverage remain open.

## Same-Store progress diagnosis

The workflow's explicit `diagnostic_progress=true` dispatch builds a separately
identified diagnostic component. Default push/manual runs still use the unchanged
one-shot App and its complete 64-case/API gate. Diagnosis returns preparation,
individual actual case observations, APIs and completion through at most 67 calls
on the same retained native DATA entry/Store/account/component. The original
cumulative 2B fuel, 64MiB and first absolute 30-second deadline are never renewed.
Each post-return observation is recorded while its actual output remains funded;
the output is dropped before the next call. A trap or explicit failure stops and
retires immediately. Final cleanup still requires zero charged bytes.

The first actual call performs original preparation/input creation; no schema or
result is prepared during snapshotting. Original case outcomes are checked outside
the guest. Progress records distinguish the last completed case and the next case
interrupted by a trap. Split return boundaries change reference lifetimes, GC and
cache behavior: these measurements are neither an upper bound nor one-shot parity
or budget acceptance. Every diagnostic receipt has `qualification=false`; the
normal one-shot gate and product acceptance remain mandatory.

At source 2418b8edb, normal CI 41094612644 / job 124197028502 again passed
native 64-case / 15-group comparison and actual Core/component builds, then
exhausted the one-shot 2B fuel (remaining 0, elapsed 1.0004439 seconds, original
deadline preserved, no deadline/unreachable trap). No improvement is claimed.
Explicit diagnostic CI 41094625731 / job 124197075079 successfully recorded
actual same-Store progress and cleanup. Its separately identified component was
c8a48cb24131e7cea8298f4922b50af79faef0f24be5281a73ad0d702649a1d6.
Preparation consumed 53,122,419 fuel; the next seven actual cases returned matching
original observations. After contract_v4.schema.json, 400,836,291 fuel remained.
The next case, defaults_setup_v4.schema.json, exhausted the cumulative remainder
after 1.411181439 seconds overall; no deadline trap occurred. Its API phase was
not reached. This localizes remaining work to schema validation, rather than only
preparation or the final API suite. It does not rank functions within that work.
Native call profiling is the next supporting measurement; it must not be confused
with actual Wasm fuel. Split execution remains incomplete and qualification=false.

An explicit `native_profile_only=true` dispatch provides supporting call-path
observations without repeating the Core/Wasm build. It profiles only the same
64 actual `check_schema` calls, once each; metadata/observer work and later APIs
are excluded. All native comparisons and 15 API groups remain required. The
workflow visibly skips Core/Wasm in this mode and rejects simultaneous progress
mode. Both diagnostic inputs default to false; ordinary qualification is intact.
cProfile has different overhead for Python and C, so its times are not a speed
benchmark or Wasm-fuel measurement. Explicit CI 41096568179 / job 124203753335
at 486190619 passed all native comparisons and 15 API groups, then visibly skipped
Core/Wasm. Its profile observed 2,062,841 calls, including 18,123 reference lookups,
39,112 evolves and 38,182 type calls. The instrumented 2.117-second total and nested
cumulative times are not additive or an uninstrumented benchmark. Normal same-source
CI 41096555198 / job 124203703948 kept profiling disabled and again exhausted the
one-shot 2B fuel after native parity; the original deadline was preserved.

The next bounded change interns only fixed attribute/method names and keyword
argument keys in the Rust port. PyO3 documents this to avoid repeated temporary
Unicode allocation. Actual lookups, callables, classes, errors, getter order and
explicit custom dispatch remain in place; schema/URI/caller names are not interned.
The finite metadata references live inside each guest Store, not a Host cache of
guest objects, and initialization is charged to its original cold budget.
Existing API groups additionally check actual original LOAD_ATTR name identity,
legacy push_scope literal identity and evolve keyword-key identity. This source
change at 45b1c4147 passed actual native compilation and all 64 cases / 15 API
groups, including the name/key identity observations. Normal CI 41098227240 / job
124209443526 still exhausted the one-shot 2B fuel (0 remaining, 0.850197199 seconds,
original deadline preserved). Explicit progress CI 41098272613 / job 124209608899
recorded eight returned cases; defaults_setup_v4 completed and distribution_v1
was interrupted. Through the same contract_v4 checkpoint, measured diagnostic fuel
use declined from 1,599,163,709 to 1,276,375,494 (about 20.2%). This is an actual
reduction within the same diagnostic protocol, not one-shot or product acceptance.
The separate new diagnostic component was a80f9356681c824c56cad6c0ef058418f90e06936d1101a002071677398a7565.

A further narrow change reuses the seven fixed type-comparison names and five
metadata guard keys. The private builtin keyword helper also shares its four
original fixed kind literals, while actual schema-supplied type objects remain
untouched. Comparison order and original unknown/surrogate/custom dispatch remain
in place. Additional actual callbacks compare original fixed kind identity and
preserve a dynamically created schema type string in both callback and error.
At source 0e2dfed1a, native 64-case / 15-group comparison passed. Normal CI
41100329636 / job 124216396038 still exhausted the one-shot 2B fuel. Explicit
progress CI 41100353558 / job 124216453220 recorded nine returned cases, then
trapped inside executable_catalog_v4. Through contract_v4, diagnostic fuel use
was 1,243,994,756 versus the original 1,599,163,709 (about 22.2% less in this
protocol). Full one-shot, catalog and product acceptance remain incomplete.


## Rejected standalone urljoin body port

The experimental CPython 3.13.16 `urllib.parse.urljoin` body port at
97eb93207 / 72fa58ca0 retained genuine helpers, configuration, parse results,
cache and Registry objects. Native compilation and all 64 comparisons / 16 API
groups passed, including the added original slash-constant identity and slice
field observations at 72fa58ca0 (CI 41108293467 / job 124243776309).
The original schema comparisons entered native urljoin 15,840 times before API
checks. The normal one-shot Wasm call still exhausted the unchanged 2B fuel.

Explicit progress CI 41108401129 / job 124244170322 showed a regression:
through the same contract_v4 checkpoint it consumed 1,280,400,271 fuel versus
1,243,994,756 at 0e2dfed1a (about 2.93% more). Eight cases returned versus nine
at 0e2dfed1a; distribution_v1 was interrupted. These split-call diagnostics
are not one-shot qualification, an upper bound or product acceptance.

The active Rust engine and adapter are restored to 0e2dfed1a. The unused URI
module, wrapper, native counter requirements and its CPython notice are removed
from the active eight-file source closure. The complete prototype and legal
notice remain together in Git history. The sixteenth differential API group
stays as coverage of the original Python URI path; it no longer proves native
URI entry. Restoration must pass fresh native/API and Wasm CI before any further
optimization is measured. Original fuel, memory, output and deadline limits stay
fixed; product integration remains incomplete.

## Rejected positional binding candidate

A source-only candidate attempted to remove temporary descend kwargs dictionaries
by checking exact bound-method/function types and known thin-wrapper code identity,
then calling the same genuine generator positionally. It was rejected before
compilation or execution: reading even an exact Python function's `__code__`
introduces an `object.__getattr__` audit callback at each descent. Such a callback
can raise or mutate state, so exact types do not make the guard observationally
neutral. Function identity alone cannot guard against mutable code. The direct
PyFunction_GetCode API is outside this extension's required Limited API boundary.

The candidate, counters, metadata and incomplete dedicated test helper are removed.
No new audit event or non-Limited API dependency is admitted. The active engine
remains the restored 0e implementation. A larger contiguous reference-resolution
body port needs separate design and parity evidence; local micro-optimizations
have not established a route to the unchanged one-shot budget.

## Experimental keyword-preserving vectorcall

A separate candidate keeps all original positional/keyword arguments and their
insertion order, including omitted versus explicit None. It uses the Stable ABI
PyObject_Vectorcall entry (available since Python 3.12) to call the same resolved
method once without first constructing a temporary kwargs dictionary. Arbitrary
callables retain normal interpreter dispatch. No code/type guard, extra code
attribute read, native-error retry or replacement generator is added.

Eight immutable keyword-name tuples are finite per-context metadata charged to
the cold budget and visited by GC. A synchronous four-pointer borrowed stack is
bounded by the original signature; no vectorcall offset flag is used. The real
callable and all actual inputs remain owned across callbacks. API17 adds original
handler comparisons for arity, keyword order/object identity, descriptor access,
custom callables/code changes, lazy generators and absence of new code-read audit
events. This is source-only until fresh compilation/native/Wasm CI completes.

Restoration 811cb8bca passed native 64 comparisons / API16 in normal CI
41112798825 / job 124259214054; its original one-shot call still exhausted 2B
fuel with the deadline preserved. Progress CI 41112803950 / job 124259239999
returned nine cases, then exhausted fuel in executable_catalog_v4. Its measured
contract_v4 checkpoint used 1,244,463,543 fuel. This fresh restoration measurement,
including the retained URI API helper, is the comparison baseline; it is not
one-shot or product acceptance.

## Open callable-mutation parity gap

Source review identified an additional unverified contract: keyword dispatch
selects Rust by original function identity, and default type dispatch guards the
checker method by identity. Mutating the same function's code can keep these
identities while changing its real Python body. Current tests cover callable
replacement and descend code mutation, not these two cases. This is a static
finding, not a reproduced runtime failure; native 64/API17 evidence cannot prove
this missing coverage. The kernel remains disconnected from product Setup.

Before product use, actual selected callables must execute normally, with public
thin wrappers entering Rust bodies rather than identity-based bypass. Saved
aliases, preexisting validators, started generators, new calls, custom replacement
and audit events require comparisons. Added code getters or direct internal links
must not bypass mutable callables. More crossings may cost fuel; that would mean
compatibility and the unchanged budget are not yet jointly achieved.

Source fbe042701 passed native 64/API17 in CI 41114239126 / job 124264147903.
Its original one-shot Wasm call still exhausted 2B fuel with the deadline preserved.
Progress CI 41114246891 / job 124264186051 used 1,242,952,199 fuel through
contract_v4 versus restoration 811cb8bca's 1,244,463,543 (about 0.12% less).
Nine cases returned; executable_catalog_v4 was interrupted. This small diagnostic
change does not establish a route to the budget or product acceptance. The new
public-callable structure requires fresh native/API18 and Wasm measurements.

The 1cbd05f75 public-definition structure passed actual native 64/API18 in normal
CI 41117640666 / job 124275891668. The original one-shot call still exhausted 2B
fuel with the deadline preserved. Progress CI 41117649051 / job 124275924360 used
1,182,899,525 fuel through contract_v4 (about 4.8% less than fbe042701), returned
nine cases and stopped in executable_catalog_v4. This does not qualify one-shot
or product use. The next narrow source change reuses only four fixed global-name
metadata strings in live traversal lookups; actual namespace/builtin values,
lookup order, exceptions and callbacks remain uncached. Its compile/parity/fuel
measurements are pending.
