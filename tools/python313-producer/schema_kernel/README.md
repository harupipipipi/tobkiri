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
Thin generator methods delegate to the Rust lazy traversal. Rust dispatches the
36 original builtin callables by actual identity; explicit extension and older
draft-specific callables retain their normal dispatch inside the same Wasm
budget. A failed native builtin is never retried through the old Python handler.
No standalone validator class, serde values, reconstructed references or parallel
resolver/cache is introduced. Regex, Python arithmetic/Fraction, original errors
and evaluated-member helpers remain genuine Python operations inside Wasm.

The Rust iterators start work on first next, preserve branch consumption/order,
close after errors, expose GC traversal/clear and release retained references on
completion/close. All public API/lifetime equivalence remains to be verified.

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
and class format-checker membership. Seven additional actual public API probes
cover lazy resume/close, interleaving, PEP479, false-schema early paths and evolve.
These checks are prepared but have not passed for this new source.

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
