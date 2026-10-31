# App validation backend boundary

Design checkpoint: 2026-10-31. This is an implementation boundary, not a new
product requirement or acceptance receipt. No new backend is implemented by
this document. Issue1520, the original GUI flow, all20 acceptance items and all
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
