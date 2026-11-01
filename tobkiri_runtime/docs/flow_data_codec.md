# Finite JSON data in Flow

Flow data admits JSON null, booleans, strings, safe integers, arrays, objects,
and finite IEEE754 binary64 numbers. Fractions, integral floats, negative zero,
and subnormals retain their bits in the supported Python persistence paths.
Nominal values such as inline Sound retain their existing JSON contracts.
Binary buffers, arbitrary Python objects, NaN, infinities, unsafe integers,
duplicate JSON keys, cycles, invalid Unicode, and oversized values are rejected.

## Data and authority are separate

`tobkiri_protocol.data_codec` is a generic data-only codec. It does not replace
`canonical.py`, Workflow `models.digest`, schema/catalog/activation hashes, or
Broker's established request-digest formula. Prepared snapshots use the data
clone only for `normalized_payload`; binding/context fingerprints stay strict.

Workflow records select data fields using a trusted record kind supplied by
store code: definition/compiled step inputs, Run inputs, attempt request input
and outcome. Checkpoint and authority fields remain strict. Each producer and
verifier uses the same explicitly named payload/mixed-record digest helper.

The generic HTTP request envelope selects only `payload` as data. Generic
control-operation provider results are application data; their result writer
and replay/restart verifier use the same data digest. Route identity, control
record references, Profile ceremonies, and authority records remain canonical.
No generic HTTP/control module recognizes a Flow contract or operation name.

Verified Host evidence contributions may declare explicit top-level
`data_fields`. Legacy declarations default to no data fields. This declaration
is supplied during verified capture, never by request JSON or evidence output.
Only those fields use data clones; other evidence metadata stays strict and
all before/after lifetime, identity and receiving-owner fences remain intact.

## Compatibility and bounds

For previously supported canonical-only values within the new admission bounds,
serialized bytes and SHA-256 digests are unchanged. Existing HMAC records with
no encoding marker load unchanged. Finite floats previously failed these data
paths; they now use an explicit, domain-separated data identity. `1`, `1.0`,
`true`, `0.0` and `-0.0` have distinct data identities.

The codec has a new conservative 100,000-node limit, counting object keys, and
uses exact built-in JSON types. This tightens admission compared with old
canonicalization and must not be described as unrestricted compatibility.
The default remains 4 MiB encoded bytes and 64 logical levels. Snapshot data
and raw HTTP parsing retain their previous 10-MiB byte ceilings; downstream
mixed records still have their own 4-MiB envelope bound. HTTP raw parsing now
rejects duplicate keys, unsafe/nonfinite numbers and invalid Unicode instead
of allowing standard-library parser ambiguities. Mixed records nominate at
most 4,096 data paths; definition and compiled input paths share that limit.

Float-bearing data uses a versioned flat prefix-token stream. Type tokens and
network-order float bit strings keep tag-shaped user objects ordinary data.
Hash preimages are NUL-prefixed/domain-separated from legacy canonical JSON.
Workflow's runtime-owned root encoding marker and selected paths are covered
by the existing HMAC. Authentication precedes parsing and path-policy selection;
unknown versions, unapproved fields and malformed/noncanonical tokens fail.
User code cannot supply the reserved root marker in runtime-owned records.
The same key inside a data object is ordinary data and is never auto-decoded.

The existing private attempt journal already uses Fernet-authenticated JSON
and supports finite native floats. It retains its exact v1 snapshot shape,
ordinary JSON transport and authentication-before-load behavior. No marker is
inferred from user-shaped snapshot payloads. Snapshot restoration still checks
binding, context, input validation, request digest, idempotency and deadlines.

## Remaining boundaries and verification

This does not change PackVM bridge continuation/request/result protocols,
which have their own canonical authority/data formats and require a separate
paired protocol change before claiming float support on those paths. The new
codec is not an implicit change to every artifact, backend, or semantic type.

Ordinary PackVM invocation now uses a paired, data-only wire adapter. Canonical
requests retain `payload`; float-bearing requests instead carry the exact
`payload_encoding` / `payload_tokens` variant. Removing either field cannot
produce a valid legacy request. The Host encodes already-Broker-normalized data
without changing the logical request or its digest. Swift and QEMU preserve
the canonical carrier, and the guest validates it before artifact execution.
Existing protected-channel and launch-binding checks remain in force; the
guest only syntax-checks `request_digest`, so it is not a cryptographic request
field binding. The private child parses the same sealed zipapp's codec and
passes ordinary Python values to the Pack ABI.

Float-bearing child terminal data uses the reserved private-result kind. The
root treats all stdout as untrusted, validates the full token grammar, object
shape, version and logical budgets, rejects decoded reserved control kinds,
and rebuilds `tobkiri.packvm.invoke.result.v2` itself. The existing guest
Ed25519 signature and helper HMAC cover that public encoding. Host decoding
occurs only after both checks and the existing cancellation fence. Canonical
terminals retain their v1 shape and bytes. The new unauthenticated token parser
is a private-ABI data validator, never an authority assertion.

The finite guest zipapp includes exactly `data_codec.py` and
`packvm_data_wire.py` in addition to its previous closure. Token expansion counts
against existing byte ceilings: 1280 KiB helper payload fields, 1 MiB whole
child request, 16 MiB child stdout, and the existing smaller outer transport /
authentication envelope limits. The adapter reserves the enclosing record's
logical depth within canonical 64: requests allow 62 data levels and outcomes 61,
with the 100,000-node data limit. MCP and saved-turn encoded requests fail closed until their
paired protocols are adapted; an encoded ordinary request cannot enter a
bridge continuation. These are local serialized protocol and sealed archive
checks, not evidence of a real VM boot or native helper execution.

A browser's `JSON.stringify(-0)` emits `0`; Python persistence cannot recover
sign bits already removed by browser serialization. Browser authoring needs an
explicit agreed data transport to preserve that distinction end to end.

Pure codec, Workflow store/engine/evidence, prepared Broker replay and generic
HTTP/control tests cover finite values, old hashes, authentication, tampering,
resource limits and strict authority metadata. The captured numeric HTTP test
uses real packaged tool owners and Broker with the normal packaged-profile
fixtures; it must be run on a resealed source tree. Isolated overlay tests are
provisional unit/integration evidence, not sealed production acceptance.
