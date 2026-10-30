# App schema caller contract

Source audit at `6356ef6af`, 2026-10-31. No product/dependency import, runtime
execution or local build was used for this audit. This document neither changes
the agreed product scope nor certifies a replacement implementation.

Astra high selected this caller audit before further library microports or
stack-sampling code. An App implementation must preserve its complete behavior;
plain JSON input does not permit skipping or weakening validation.

## Actual loading operation

The operation is `tobkiri_protocol.validation.load_schema(schema_name)`:

| Boundary | Current source and observable behavior |
|---|---|
| Cache | `validation.py:128`: unbounded `lru_cache`, keyed by the received name. Alias and filename keys are distinct. A hit returns the same mutable dictionary without a new read/selfcheck. Exceptions are not cached. |
| Reader selection | `validation.py:96-126`: the executed module's captured-resource accessor must validate its actual owner. Broken/absent captured ownership cannot silently become native filesystem access. Ordinary native Path behavior remains separate. |
| Name/path | `validation.py:132-141`: live alias lookup; native `schema_path` or captured `joinpath`/name/is_file operations. Captured TypeError/ValueError becomes the existing unknown-schema error. Loader/path callbacks remain real operations. |
| Bytes/parse | `validation.py:142-145`: one actual `path.read_bytes()` passed to the live imported `strict_loads`. OSError/ProtocolError is wrapped with the path and actual exception text, chained from that same exception. |
| Strict JSON | `canonical.py:24-82,116`: 4MiB/64-depth limits, strict UTF-8, duplicate/nonfinite/float/unsafe-integer rejection and recursive value validation precede schema checking. The standard decoder creates built-in JSON values; the caller's explicit guard is `isinstance(payload, dict)`, not `type(payload) is dict`. |
| Object requirement | `validation.py:146-147`: even valid boolean JSON Schemas are rejected here as non-object package schema resources. |
| Missing dependency | `validation.py:148-149`: the live imported Validator alias being None refuses validation. No dependency absence may become an unchecked success. |
| Actual selfcheck | `validation.py:151`: live `Draft202012Validator.check_schema(payload)`, with one positional payload and no registry/formatter/callback parameter. Its return value is ignored; successful loading returns the same payload dictionary. |
| Failure | `validation.py:152-153`: an Exception from lookup/call enters the existing formatting/wrap path and is chained with `from exc` if wrapping completes. Formatting itself can execute Python code or fail; those failures must not be sanitized into a different result. Non-Exception BaseException is not caught. |

`strict_loads`, the Validator alias and resource accessors are actual runtime
references. A source audit that found ordinary behavior is not proof that arbitrary
replacement objects may be silently bypassed. A future optimization needs an
explicit dispatch contract; this audit authorizes no identity shortcut or retry.

The dependency/path/reader checks above occur on cache misses. A replacement
cannot add them to successful cache hits and call that unchanged behavior.
Equal resource bytes under different name keys must not silently merge returned
objects. Clearing load_schema's cache does not also clear `_schema_store`'s
separate retained dictionary; the cold oracle deliberately clears both.

## Errors and retained objects

`tobkiri_protocol/errors.py:18-26` defines `SchemaValidationError(ProtocolError)`
with code `schema_validation_failed` and diagnostics defaulting to `()`. This
loader wrapper supplies no diagnostics argument. Its outer message incorporates
the actual original exception's formatting, and, when wrapping succeeds, its
cause is that same exception. A failed exception formatter can itself escape.
Cause/context, nested jsonschema errors and their schema/instance references are
therefore reachable by Python callers. No message-only substitute is proved.

Downstream `bundle_catalog.py:250-251` wraps the protocol error as the cause of
`BundleIntegrityError`; `core_runtime/pack_sdk.py:314-318` similarly preserves
ProtocolError under PackSdkError. The shipped cache regression at
`tests/test_bundle_catalog_validation_cache.py:246-254` explicitly checks typed
cause/diagnostics and repeated rejection. That case exercises document validation,
not a direct malformed meta-schema fixture; it is not whole-cause parity proof.

`_schema_store()` (`validation.py:158-177`) uses sorted resource filenames, calls
`load_schema` for each, skips SchemaValidationError, and indexes string `$id`
values. It retains the actual cached dictionaries. It does not copy, freeze or
revalidate them. `validate_document` later passes the actual selected schema and
this store to `RefResolver.from_schema`, constructs the live Validator with a
fresh `FormatChecker()`, sorts all errors and adds semantic diagnostics
(`validation.py:180-227`). That separate operation must not be collapsed into
the selfcheck or treated as covered by selfcheck success.

## Dependency and callback boundary

The current load_schema signature accepts no Validator/registry/formatter
injection parameter. The bounded search of current protocol/App/bundle, Pack SDK,
SDK/docs and related tests found no shipped registration/replacement path for
this particular dependency. Generic jsonschema create/extend/type/format/registry
APIs remain supported library capabilities; their existence alone does not prove
that load_schema exposes each as an App parameter.

That absence result is not exhaustive alias analysis or a historical-corpus
proof. Live global replacement, captured-resource ownership callbacks, exception
formatting and returned mutable objects remain observable. General-library
customization must remain intact wherever that library is still offered.

`migration/inventory.json` still marks this entire module TODO, provisional
Python-on-Wasm. Neither the search nor a proposed selfcheck optimization closes
the module, its semantic validation functions, SDK or product integration.

## The existing corpora are different operations

| Corpus | Actual entry/meaning | What it does not establish |
|---|---|---|
| 42 original schema files | `schema_kernel_oracle.py:64-95` authenticates the original complete resource inventory, strictly decodes bytes and then invokes the real library selfcheck. | Cache/name/reader behavior of load_schema, instance-document validation, or full bundle execution. |
| 22 boundary values | `schema_kernel_oracle.py:95-135` directly invokes the library selfcheck on curated Python JSON-like values, observing typed errors and reference relations. | These are not all admissible load_schema payloads. |
| Combined 64 | The private kernel's native and original one-shot Wasm comparison invokes all 42+22 directly, retaining its own exact metadata/error/API checks and original limits. | An App-only optimization does not repair this library-probe budget failure. |
| 45 cold calls | The original full `BundledCatalog.load` profile counts real selfchecks through its actual aliases/cache behavior; 42 unique files do not justify reducing it to 42 calls. | It cannot be replaced by prefilled cache or a loop over unique filenames. No new full-catalog execution was performed in this audit. |
| Independent cold store | `bundle_catalog_oracle.py:211-238` clears both caches and separately observes actual `_schema_store`, schema values and cache counters; the native Pack exposes the matching command in `bundle_catalog/native.py:22-45`. | The source explicitly retains the separate full bundle case and its 45 cold calls. |
| 305 entries /453 reads | `bundle_catalog_oracle.py:137-210` uses the unchanged native loader and actual read occurrences, including changed-reread refusal. `materialize_bundle_catalog.py:53-60` requires the exact pinned counts. | Source identity, schema-store-only, a utility fixture or a smaller subset cannot certify this complete catalog. |

The direct library values `boolean-true`, `boolean-false` and `null-schema` never
reach check_schema through the object-only App loader. `fractional-count`,
`integral-float-count` and `large-exact-count` are rejected by strict parsing
before that call. These six distinctions follow from inert AST/literal inspection
of the 22 fixtures and the parser source, not local product execution.
An App reference corpus must preserve those earlier errors rather than reusing
the direct-library outcomes. The existing library vectors must remain separate.

## Gate relationship and next decision

`issue-1520-schema-kernel-probe.yml` is the private experimental library gate;
it still fails original one-shot fuel qualification. The final
`issue-1520-rust.yml:757-761` migration-completion job runs
`tools/quality/issue_1520_gate.py --stage complete`, whose code combines final
host/mechanism checks with source-bound inventory/deletion validation
(`issue_1520_gate.py:157-180`). It does not invoke the kernel-probe workflow.
This source fact is not permission to waive either requirement or claim that an
App-only success fixes a generic-kernel failure. No gate, oracle or expected
outcome was changed in this audit.

The evidence identifies a narrower real App operation worth designing, while
leaving these decisions open:

1. Can its validation implementation be moved while preserving actual live
   dispatch, error chains, dictionary identities, aliases/cache and reads?
2. If a new path leaves the general library untouched, which actual App and
   library entries does it execute, and which standalone qualification remains
   failed/unadopted? Retain and label the separate results.
3. What evidence will cover malformed resource bytes, dependency refusal,
   captured-owner failure, mutable-cache behavior, all 45 cold calls and the full
   305/453 catalog? The new caller reference must be additional, not a rewrite of
   the old 64 reference to fit a convenient implementation.

Further implementation needs an explicit design answer to those questions.
Core/Host remain generic; any App semantics belong to the App Pack. Full original
Setup/recovery/handover, native approval, settings, conversation, real1B and local
provider/API acceptance remain open.


## First additional reference slice

The additional `app_schema_oracle.py` runs the pinned original and current native
caller in fresh CI processes against the same real-file fixtures and original
full bundle. It records recursive returned-object order, actual selected-call
argument identity, strict parser cause identity, controlled lookup/read/parser/
checker failures, ignored return values, mutable cache identity and the separate
store cache. The workflow compares reports, not a replacement validator.
Captured-resource ownership and Wasm budget/GUI/OS integration are explicitly
outside this first native reference slice. The original 64-vector probe and all
completion gates remain unchanged. No META_SCHEMA compiled plan or new App
checker is implemented by this slice.
