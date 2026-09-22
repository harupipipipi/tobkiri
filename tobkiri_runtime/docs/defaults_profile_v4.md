# Canonical Defaults Profile bundle

The bundled `defaults` Profile is the sole canonical product Profile currently
defined by the finite Defaultspack v4 bundle. `shell.tauri.default` and
`shell.cli.default` are distinct Shell providers admitted by the bundle. They
are not Profile personas: a named Profile exists only when its entire Base,
Shell, Application, Pack set, requested edges, and presentation artifacts have
one generated authoritative definition. The current CLI Shell therefore does
not create a second selector row by itself.

## Authoritative composition

Resolution starts only from `ecosystem/defaultspack/v4/bundle.lock.json`. The
locked Profile source selects:

- `defaults-basepack` as the Base definition;
- `shell.tauri.default` as the exact `app.shell.v1` provider;
- `runtime.tauri.application.default` as the Application Pack;
- the explicit Defaults provider Pack set and its deterministic dependency
  closure;
- caller-specific requested Contract/Operation edges and opaque Authority
  references.

The resulting ProfileLock and ResolvedPlan both bind the source definition
digest, selected catalog revision, bundle-lock byte digest, Base and Shell
definition digests, Shell executable artifact digest, Application manifest
digest, requested-edge digest,
constraint digest, closure digest, provenance digest, Authority snapshot, and
SecurityEpoch. Each Plan binding includes the exact caller Function ID, target
FunctionPrincipal, Contract/Operation, opaque Authority reference, requested
scope digest, execution domain kind, and adapter chain.

Activation adds the exact Profile revision, ProfileLock digest, ResolvedPlan
digest, catalog and bundle digests, closure digest, Authority snapshot,
SecurityEpoch, and monotonically increasing fencing token. Restart reloads the
atomic activation envelope and rejects any stale or independently re-digested
Profile, Lock, Plan, activation, Authority reservation, epoch, or fence.

The current record chain is Profile v5, ProfileLock v5, ResolvedPlan v2, and
ActivationRecord v2. The v4/v4/v1/v1 schemas remain frozen readers for
pre-e853 activation envelopes. On restart, ActivationStore validates the full
predecessor and its committed Authority reservation, re-resolves it from the
signed current bundle, verifies that no edge or normalized scope changed, and
publishes a successor envelope through the normal fenced atomic transaction.
It never copies new trust fields from the old record. Missing catalog evidence,
digest drift, scope drift, stale Authority, or an interrupted transaction fails
closed; retry after an Authority-committed interruption is idempotent.

## Requested authority scope

Every requested edge uses a closed `requested_scope_template`. An empty source
template means only `operation.invoke` for that edge's exact Contract,
Operation, and semantics digest. Unknown fields, wildcards, a different
Contract/Operation, opaque scope, or a mismatched semantics digest are rejected.
Normalized templates and their digests are committed into the Profile and
ResolvedPlan. Authority ceilings, grants, authorization, and dispatch all use
that committed scope, so a narrower reviewed dimension or quota cannot be
expanded later.

## Platform artifact selection

The tracked source catalog deliberately marks Tauri and CLI Shell artifacts as
`build_required`; it contains no launch variants or placeholder executable
digests and cannot be activated. A packaging build stages its actual
Application/Shell bytes and then runs the formal source-package command
`python -m scripts.generate_packaged_defaultspack_v4_bundle` from the
`tobkiri_runtime` source root. The outer Launcher
release staging path is the sole owner of this step: after it verifies the
signed Presentation release, it passes that exact selected artifact to the
generator, which copies it under `platform-artifacts`, writes the packaged
Profile successor, and only then allows the runtime resource manifest to be
sealed. The generic resource-preparation script cannot inject or overwrite a
Profile artifact. The generator rejects
missing paths, symlinks, sentinel or mismatched digests, wrong platform or CPU
architecture, and a mismatched macOS bundle identifier. It writes one verified
variant to the staged Shell definition. A directory artifact such as a macOS
`.app` binds two typed digests: `artifact_digest` covers its deterministic,
symlink-free whole tree, while `entrypoint_digest` covers the exact executable
bytes. File artifacts on Linux and Windows retain the same distinction even
when both values derive from one file. The setup confirmation carries the
entrypoint binding as `confirmation.shell.executable_artifact_digest`; the
server requires it and compares the complete confirmation with a fresh
catalog-and-lock resolution before writing state. The Application Pack, launch Function,
signed release index/lock, Python verifier, and Rust authority resolver preserve
and independently verify both bindings. Production capture consumes the
selected definition variant rather than synthesizing one from Function
metadata.

The serialized `GET /api/setup/packs` response is defined by the single
fail-closed schema
`tobkiri_protocol/schemas/defaults_setup_v4.schema.json`. The backend validates
the complete response against that schema before writing it to HTTP, while the
Launcher exact-key tables are generated from the same schema. In particular,
every confirmation binding carries the resolved executable placement fields
`executable_catalog_digest`, `variant_id`, `platform`, `architecture`,
`runtime_abi`, `backend`, and `execution_kind` in addition to its Pack,
Function, Contract, scope, and Authority bindings. Missing, unknown, duplicate,
wrong-type, stale, or digest-unbound fields are rejected; they are never made
optional and never fall back to a legacy setup or Registry representation.

Production callers launch the generator through the shared isolated launcher:
the child environment is rebuilt from a small neutral allowlist, Python is
started with `-I -B -c`, and only the canonical `tobkiri_runtime` root is
added before `runpy.run_module`. `PYTHON*`, `LD_*`, and `DYLD_*` inputs are not
inherited. The exact source closure is recorded in
`tobkiri_runtime/packaged_defaultspack_source_manifest.v1.json`; missing,
extra, changed, linked, or special entries fail closed in both Python and the
Rust sparse authoritative-source fixture.

## Transactions and settings

Named Profile selection is the four-step server ceremony:

```text
resolve -> review -> Authority approval -> activation
```

The candidate is session-, predecessor-, catalog-, definition-, bundle-, and
digest-bound, expires server-side, and is consumed once. Named selection must
match the canonical Profile Pack set exactly. Optional Pack installation,
approval, enablement, and disablement remain the separate Defaults Pack-set
transaction; those operations derive a new immutable Profile closure on the
server and never trust client `approved` or `enabled` fields.

An unresolved named definition may leave artifact, definition, catalog, and
Authority capture fields `null`. Any non-null field is an exact pin rather than
a hint: the Resolver rejects stale Base, Shell, Application, Pack, executable,
catalog, mode, presentation-family, Contract, or dependency bindings instead
of rewriting them. Source definitions cannot contain resolved Authority state,
and duplicate Pack or requested-edge identities are rejected before review.
Optional Pack IDs and dependency traversal use canonical ordering, so the same
approved set always produces the same Profile, closure, Plan, and Authority
review material.

User Settings are a separate Launcher-local projection. Profile activation can
change only runtime Profile settings and cannot mutate User Settings.

## Generation and verification

The canonical bundle is generated only by:

```bash
python scripts/generate_defaultspack_v4_bundle.py --source-commit <trusted-commit>
python scripts/generate_defaultspack_v4_bundle.py --check --source-commit <trusted-commit>
```

Generation must be deterministic across consecutive runs. The complete-v4,
architecture, integrity, boundary, and checked-in evidence generators remain
the release authority; runtime Registry or installed-Pack discovery is never a
Profile source.

The source generator renders into a private sibling directory, validates the
complete locked catalog (including dependency versions, Base/Shell
compatibility, exact target platform/architecture, Application kind, and
duplicate identities), and publishes it as a rollback-safe directory
transaction. A validation or publication failure restores the prior bundle;
symlinked or traversal-capable destinations are rejected before any write.

Normative provenance v2 is derived from a canonical payload that excludes its
own provenance field. Its trust root is `content_root_digest`, which binds the
source payload digest, exact generator bytes, generator path, and canonical
input-inventory digest without a self-referential cycle. `repository_commit` is
explicitly informational (`repository_commit_trusted: false`): shallow builds
do not need the parent object, and dirty builds remain deterministic because
trust comes from content rather than worktree state. Explicit placeholder or
sentinel commit metadata is rejected. Release verification must use the v2
content bindings and the signed runtime resource manifest, never the commit
label as authority.
