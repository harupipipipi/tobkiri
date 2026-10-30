# Private original-object Rust schema port

This is an experimental Rust/PyO3 port of jsonschema 4.26.0 traversal and all
36 Draft 2020-12 keyword handlers. It is not connected to product Setup or the
product validator. Current engine code has not yet passed compilation/parity;
source completion is not a successful runtime observation.

## Boundary

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
22 boundary inputs. Only case names/schemas enter the guest. Expected results and
expected meta-registry objects stay outside the guest; it uses its genuine
original classes and registry. Its 64 actual selfchecks run in one real Core
NativeData invocation under unchanged 64 MiB / 2-billion fuel / original absolute
30-second limits. Compare first errors, ordered context, actual references,
parent/cause, paths and complete rendering hashes, plus the real meta-registry
and class format-checker membership. Twelve additional actual public API probes
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
The corrected checks have not yet passed.

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
