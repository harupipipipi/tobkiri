# App validation backend boundary

Design checkpoint: 2026-10-31. This is an implementation boundary, not a new
product requirement or acceptance receipt. The experimental default-off backend prototype is not qualified or adopted. Issue1520, the original GUI flow, all20 acceptance items and all
839 original migration units remain in scope.

## Why a separate backend is needed

The generic Rust/Python-object kernel is still an unadopted experiment. The
actual App-native reference matches the original37 caller cases,42 resources
and305-entry/453-read catalog. Its real Wasm full-catalog call reaches Rust but
still exhausts the original2B fuel. The small keyword call-preparation change
reduces the recorded contract checkpoint by about0.565%, including changed cold
metadata; it does not solve the full operation.

Astra high distinguished the real App contract from every implementation detail
of a third-party Python library. A separate App-owned backend is a legitimate
migration direction. Removing public generators from the existing generic
kernel is a different change and breaks its required child-lifecycle contract;
that is not the route to this backend.

## Explicit selection and one actual call

The App must select an explicit App-owned default callable. It must not leave
the original class selected and silently omit its execution. The currently live
selection, dependency refusal and actual callable lookup/call remain observable.
An actual custom selection is called as selected; its code, descriptors and
exceptions must not be inspected or bypassed to guess an equivalent builtin.

The current import/global named `Draft202012Validator` is a dependency seam,
not a new user-facing validation setting. A class-compatible App-owned provider
is an implementation option, but the exact derived source change and its effect
on each caller observation must be reviewed before implementation. API-key,
model/provider configuration and the original Setup UI do not gain a new step.

Compatibility dispatch must occur before validation, on a declared supported
provider/capability boundary. A native success shortcut followed by original
validation on an invalid/unsupported result is rejected. Native failure,
capacity refusal or a trap must not initiate original validation or retry.
The selected normal backend must directly implement both success and failure.

## Caller observations that remain required

| Existing observation | Required backend behavior |
|---|---|
| Reader/strict parser | Actual source owner/accessor, bytes, parse order, strict rejection and same caught cause. No new read/cache at a hit. |
| Live selection | None refuses; lookup/call failures and non-Exception escape retain their existing treatment. |
| Returned value | The actual parsed dictionary returns unchanged; checker return remains ignored. Recursive key order is retained. |
| Failure graph | Genuine public error classes, messages, cause/context and reachable schema/instance/context references; not a message-only native substitute. |
| Cache | Alias/name/keyword-call key distinctions, mutable object identity, failed-call retry and separate retained store cache. |
| Schema definition | Actual validation of the current input, not a source-hash success or prepared valid answer. |
| Document validation | Current cached schema/store values, reference semantics, necessary format callbacks, error ordering and subsequent semantic diagnostics. |

The37-case reference contains22 independent library-boundary inputs passed
through the actual App loader, four malformed parser fixtures, eight dependency/
lookup/reader/parser/checker/format/BaseException/ignored-return controls, and
three cache/retry/store cases. Six boundary values are rejected before the
library checker in the App. The standalone22 library outcomes must not be
substituted for these earlier App outcomes.

A backend default change affects at least the admissible object-schema cases and
the full catalog; it must retain the controlled custom-selection cases and
parser/cache behavior. Existing reference files are inputs to differential
comparison, not outputs to embed in the new guest.

## Native algorithm obligations

Schema checks and instance validation are separate operations. Neither may be
replaced by trusting the other. Schema/store dictionaries are mutable; an object
identity is not an immutability proof. No plan may continue using stale decisions
after an observable callback changes relevant state.

A Rust implementation may use genuine original error classes/constructors.
It may not run the original validator again to manufacture matching errors.
Error source locations must bind to the actual retained objects rather than
copies reconstructed only for comparison.

Reference resolution and format callbacks need explicit coverage. Eager schema
compilation must not perform a reference fetch or callback that the original
operation would not reach. Callback order, real argument references, exception
identity and reachable schema mutations need a supported-policy decision and
regressions before adoption. Strict JSON alone does not remove those concerns.

No specific Rust JSON Schema crate, IR representation, error converter or schema
cache is selected here. Choose those against the actual requirements above;
matching successful fixed schemas alone cannot justify the choice. Construction
and validation costs remain in the original invocation. There is no validation
result or workload preparation during producer preinit.

## First implementation slice: schema definition checks

Astra high's follow-up selects an App-owned `check_schema` entry with an actual
Rust execution context. A thin PyClass replacing the original constructor is
insufficient: retain no per-child Python Validator construction or Python calls
to `validator.descend` / `evolve` as the internal recursion mechanism.

The Rust context owns frames containing the actual schema/instance handles,
paths, resolver scope and branch state. Handles retained across a suspended
step must be owned; do not store GIL-bound temporary borrows. Reuse the36
handlers' validation rules with direct Rust control transitions and type tests.
Python boundaries are the required actual resolver/format callbacks and genuine
error construction, rather than a recreated public third-party traversal API.

The first slice must complete valid AND invalid schema definition checks, use
the actual META/resource objects, stop at the original first error, and create
the genuine `SchemaError`. This is separate from document validation. During
that slice, document validation remains an explicitly selected original operation
BEFORE evaluation; a schema-check success does not qualify document validation.

Crucially, do not invent dispatch based on the input schema's `$schema`.
The pinned `Draft202012Validator.check_schema` chooses its validator from
`cls.META_SCHEMA`, not from the supplied schema declaration. The outer App
provider is the selected actual callable; original/custom compatibility dispatch
must reproduce the original operation before validation starts. Invalid results,
exceptions, capacity refusal and traps never initiate a second validator.

Source review: the [pinned original check_schema method](https://github.com/python-jsonschema/jsonschema/blob/v4.26.0/jsonschema/validators.py#L284) selects from the class META,
constructs that selected validator with the actual META and format checker, and
raises from the first yielded error. Its child reference operation and lazy
branch/error rules need matching Rust transitions. Native64/App37 comparison and
actual Wasm costs remain required. The default-off experimental Rust frame
backend and separate native lane now exist, but product selection and qualification
remain pending. Unsupported META rules currently refuse; metadata mutations and
custom numeric values need further implementation/verification before adoption.

## Evidence and scope

Keep the original/current/native caller reference and add a separately identified
App-backend lane. Required evidence includes the complete45 cold calls and302
hits, all305 entries/453 reads, independent store behavior, malformed data,
callback/refusal/error graphs and the complete original operation corpus. Expand
negative and mutation cases where the new boundary creates a real difference.

The App Wasm operation must run once under one original2B-fuel/64MiB/absolute30s
Store, with real source ownership, semantic result, deadline and physical cleanup
evidence. Separate compatibility or diagnostic runs cannot be combined into
that success. Full original GUI Setup/recovery/handover, native review,
API/model configuration, conversations, real1B and local provider/API acceptance
remain required after backend parity.

The generic64/API19 library experiment and its failed one-shot results stay
recorded. If the generic kernel is not adopted, say so explicitly and preserve
its results; an App result does not qualify that kernel. Native product Python,
VMs and legacy mechanisms are removed only after the actual replacement meets
the full product contract.


Native prototype evidence (`7c0f5a265`): CI41159026652 passed the existing generic64/
API19 lane and its App comparison, plus the separate App backend64,11 boundary
probes and full App37/42/305-entry/453-read contract. The App backend entered Rust
0→45→87→110 times across the cold catalog/schema/caller phases. This establishes
only that tested native subset. Unimplemented META operators, broader mutation/
custom numeric behavior, real Wasm limits and actual product adoption remain open.
The next explicit Wasm artifact uses the new provider import while preserving the
original document operation; it does not replace the generic candidate or budget.

## App ownership versus the prototype's forwarding

The `AppSchemaProvider._Metadata.__getattr__` forwarding to `_Original` is
prototype code, not evidence that all globals of an unselected implementation
belong to the App contract. Astra high reviewed this distinction after the URI
experiment regressed from19 to15 completed schemas. Further shared-URI porting
was withdrawn. No closed Rust value-model operation is qualified by that review.

An App-owned provider may own its source-admitted META and dependency state.
It must retain the actual selected provider's configuration, callbacks, live
input changes, first-error ordering and data/error references. This does not
automatically require following changes to a different, unselected Python
implementation. A source digest identifies DATA; it does not grant execution
authority or certify a closed operation. No existing policy token proves that
the current shared-Python prototype is closed or immutable.

### Original-source evidence

The pinned original is `54e7da008997a3eb45876cbe18459dba9902cc9d`.

- [`validation.load_schema`](https://github.com/harupipipipi/tobkiri/blob/54e7da008997a3eb45876cbe18459dba9902cc9d/tobkiri_runtime/tobkiri_protocol/validation.py#L96-L116)
  parses the actual bytes, refuses an absent selected dependency, calls its
  `check_schema(payload)`, preserves the caught exception as cause, and returns
  the actual payload. It does not pass a META or format-checker argument.
- Its separate document operation constructs an actual `FormatChecker` and
  `RefResolver`, then passes them to the selected validator. Those arguments are
  actual App configuration; provider independence cannot discard them.
- [`PackSdk.render`](https://github.com/harupipipipi/tobkiri/blob/54e7da008997a3eb45876cbe18459dba9902cc9d/tobkiri_runtime/core_runtime/pack_sdk.py#L77-L86)
  calls the selected definition checker. The SDK manifest operation explicitly
  supplies a fresh checker and consumes ordered error paths/messages.
- [`Workflow integration`](https://github.com/harupipipipi/tobkiri/blob/54e7da008997a3eb45876cbe18459dba9902cc9d/tobkiri_runtime/core_runtime/workflow_v4/integration.py#L114-L161)
  extends the validator with a real Sound value rule and supplies an explicit
  captured-resource registry which prevents network discovery. This is a real
  extension/configuration requirement, distinct from rewriting private globals.
- A source search for literal `META_SCHEMA` and `FORMAT_CHECKER` across that
  revision's `tobkiri_runtime` (including units) and `docs` returned no matches.
  This bounds the evidence; it is not exhaustive dynamic alias analysis or a
  proof that no other public/shared-object contract exists.

### Preserve case intent before changing a fixture

| Existing cases | Observation to retain in an independent provider |
|---|---|
| App37, parser/refusal/custom selected callable/cache cases,42 resources and full305-entry/453-read catalog | All recorded reader/parser/selection/cache/cause/result observations remain required unchanged. |
| Backend11 META/branch/callback/error cases | Apply a configuration change to the actually selected provider in each lane. Preserve valid/invalid outcomes, live input/META changes, real error references and stop order. Do not silently keep testing only `_Original` forwarding. |
| Reference29 and fragment14 | Preserve actual selected resolver/registry/META resource ownership, descriptors, retrieval, scope and live mutation. Retargeting to an independently owned resource graph requires explicit source/ownership and error-node mappings; a copied URI or equal contents is insufficient. |
| New default-callout `live-input` and `exception` | Keep actual default checker configuration, callback argument identity, later input reads, supplied exception identity and no retry. |
| New default-callout `existing-import`, `rebind-import`, `rebind-exporter` | Currently required by the shared-Python/translated-library prototype, whose scalar implementation uses those actual bindings. Preserve these tests there. They do not by themselves establish a product obligation for an independent backend to consult an unused `_keywords` implementation. |
| Generic64/API19 | Keep as evidence for that separate unadopted library translation. They do not qualify the App backend, and are not silently replaced by native App success. |
| Shipped SDK/Workflow checker, resolver and extension arguments | Preserve actual App-owned callbacks/configuration and reference boundaries; document-operation migration is separate and remains incomplete. |

No existing case has been removed or retargeted by this checkpoint. A future
change must identify its selected provider state, map every affected case to the
original observation, and add an independent-owner isolation check. Sharing a
META object with an unselected implementation cannot be inferred solely from
the prototype's metaclass. Conversely, an actual original shared-object
contract discovered later must be retained rather than categorized away.

The next implementation boundary is an independently owned App definition and
an explicit compatibility operation, not another loop of public-library helper
ports. A Rust value-model snapshot cannot cover admitted live callbacks by
itself. Before that model executes, its actual callback/configuration policy and
stability must be established, with both valid and invalid cases and no fallback
after validation, refusal or trap. The unchanged one-shot2B/64MiB/30s gate remains
unmet. The final original GUI/839-unit/1B/API/VM-retirement requirements remain
unchanged.

## Ownership slice implementation (native parity; guest budget unmet)

The provider now declares its META/checker/type/registry fields explicitly;
wildcard forwarding is removed. Its model reads the eight declared Draft202012
definition documents from the artifact's already lock-pinned source DATA. It
constructs an independent reference graph and checker registration dictionary,
retaining the explicitly selected real callbacks and exception classes. The
class-root dictionary shares children with its own registry root, matching the
original root-view relationship, without sharing Original's containers.

Only definition DATA/resources are constructed during ordinary initialization;
no App schema/catalog/result is loaded or validated and no producer warming is
added. The new native metadata factory retains that actual graph's crawled
Resource/anchor objects. It grants no source trust or execution authority.
External retrieval still does not extend ownership.

The existing11/29/14/5 comparison groups now target the selected lane's state;
their names and behavior assertions remain. Four additional ownership groups
cover source reconstruction, absence of shared containers, actual callback/class
retention, isolation from unselected Original changes and live selected META
replacement/error references. All execution semantics remain in the existing
reference engine. Callback bodies/globals, the explicit retrieval callable and
type-checker dependency are not declared independently implemented by this
slice.

Native CI41176050740 at71f785246 passed generic64/API19, backend64+11,
reference29, fragment14, default-callout5, the4 ownership groups and complete
App37/42/full305-entry453-read comparisons. The caller observer indexes the
actual selected provider's graph, retaining its error-reference assertions.

Actual Wasm CI41176239148 built and entered the new source-owned model and Rust
reference path, but independent cold/full operations both exhausted the
unchanged2B fuel. The separate one-Store cumulative diagnostic41176241136
completed19 schemas and exhausted fuel on20th profile_lock_v4, with928 native
pointers/857 dynamic anchors/0 adapters. Both modes retained64MiB/30s and physical
cleanup to0; neither supplies a completed one-shot semantic result. Source
ownership parity is not budget or final product qualification.

The next selected slice is an actual-handle value view in the Rust frames,
starting with fixed App scalar type/numeric rules. Preserve real dict/list
iterators and reference resolution. Derived mutable views cannot survive a
reentrant callback/subscription/comparison/iterator/descriptor operation, but
already-bound child handles must survive a parent replacement as in the source.
Create errors at the same frame/point from those actual handles. Nonstandard
operations dispatch at their operation boundary, never by retrying validation.
The value slice is implemented at6e23f4bc0 and hardened at3b7190893. Node
handles own actual objects without retaining mutable contents or decisions.
Exact built-in scalar type checks and four numeric bounds run in Rust; custom
policies/objects and unsafe numeric pairs dispatch at their actual operation
boundary. Existing iterators, reference algorithms and error-frame timing remain.
Native CI41180279130 passed all16 value groups plus the unchanged generic,
reference, ownership and complete App comparisons.

The same source's actual Wasm cumulative diagnostic41180460640 observed16,671
native type checks and76 native numeric comparisons with zero Python value
boundaries, but completed only19 schemas before the20th exhausted2B fuel. The
separate unchanged cold/full gate41180458589 also exhausted fuel in both modes.
All retained64MiB/30s and cleanup to0. Diagnostic success proves actual native
value reach and valid observation handling, not sequence completion, one-shot
parity, performance improvement or final product qualification.


## App execution-control slice (native parity; guest pending)

Astra's next selected boundary is the App frame's own control state: frozen root
keyword work rows, enumerate counters, fixed ranges and prefix zip coordination.
Sol6.1 xhigh implemented that engine and12 groups/26 differential pairs; Root
integrated the actual foreign-key/pair operations, source inventory and CI. Real dict/list iterators and foreign
protocol operations remain live. Public error/context lists and child binding
points stay observable; this is neither a builtin-reference cache nor a schema
plan/result cache. The original root comprehension unpacks/selects each pair
while collecting; it does not call a foreign length hint through list(pairs).
Foreign error keys retain actual set-membership/hash operations at the original
decoration point. Native CI41182341650 at1ebc977d1 passed all12 groups/26 pairs and every prior
comparison, including complete App37/42/full305-entry453-read. The initial
foreign-root iterator-acquisition difference was corrected without relaxing its
case. Actual guest CI remains pending; no fuel improvement or final product
qualification is claimed. A subsequent selected-callback StopIteration boundary
correction is implemented: root construction stays outside the generator
boundary; descendant preparation and keyword execution wrap only unhandled
StopIteration, retaining its actual cause/context. Normal iterator termination
and prior catches run first. The extended18 groups/48 pairs retain every prior
case; this new source requires its own native proof.
