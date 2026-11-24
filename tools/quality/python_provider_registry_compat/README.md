# Original Python provider registry stateful oracle

This source-only generator observes the original Python `ProviderRegistry`
through `snapshot`, `save`, `prepare_save`, `set_model_access`, `delete`, and
`health`. It also captures the original profile-bound
`ProviderRegistryHostFactoryV4` read and manage contributions, including the UI
projection that redacts the raw provider record. Every result and exception
comes from the captured Python implementation; this generator does not create
Rust-derived expectations.

The original Python binding is `HostProviderCaptureContextV4.profile_id` passed
to `ProviderRegistryHostFactoryV4.capture`. There is no Python source type named
`HostFactoryProfile`; the receipt records the concrete context class, profile
value, factory/function/contract/operation identity, principal, domain, and
synthetic artifact digests used by the capture fixture.

## Generate a private receipt

Run from the repository root with the intended CPython 3.13 interpreter. Pick a
new absolute private output path for every attempt; its parent directory must
already exist. Existing output paths are rejected and the generator does not
overwrite files.

```sh
python3 tools/quality/python_provider_registry_compat/generate.py \
  --output-dir /absolute/private/path/provider-registry-NEW
```

The output directory is created with mode `0700`; files are created with mode
`0600`. The generator captures a static import closure for the original
registry and Host factory, the three original test files, this generator, the
input corpus, and this README. It copies the captured runtime source bytes into
a private temporary import tree before loading the original classes, disables
bytecode writes, verifies loaded project modules came from that tree, and checks
the captured source files remained unchanged. The receipt pins exact CPython
micro version, Unicode database, integer conversion limit, recursion limit,
platform, executable hash, and `unicodedata` module hash when available.

The generator blocks network entry points while importing and observing the
source. It gives each case a fresh private `user_data_root`; no user profile or
credential store is selected. Opaque handles and metadata markers in fixtures
are synthetic strings only.

## Receipt contents

`receipt.json` uses schema
`io.tobkiri.original-python-provider-registry-stateful.v1`. Its source manifest
is SHA-256 bound and includes repository-relative paths, byte counts, roles,
and hashes. Each case binds its exact input JSON and observation JSON. Per-step
sidecars retain the actual Python return value as ordered JSON and exact
before/after bytes of `provider_registry.store.json`; observations also include
the source exception class, message and arguments, profile-root file hashes,
provider/store revisions, local file additions/removals/changes, and blocked
network attempt counts. The non-JSON `CapturedHostProviderV4` return is recorded
as its source class name and captured contribution identities. Generated paths
are relative to the receipt.

The corpus covers repeated identical saves, stale revisions, preflight without
reservation, revision/existence/policy validation order, model-access retention
and route namespace changes, strict booleans, malformed stores and profile
IDs, local endpoint canonicalization, health evidence, safe metadata filtering,
deletion state changes, UI redaction versus raw readers, and captured Host
profile binding. The inputs were derived from the unchanged tests:

- `tobkiri_runtime/tests/test_provider_registry_pack.py`
- `tobkiri_runtime/tests/test_provider_registry_host.py`
- `tobkiri_runtime/tests/test_provider_model_access_configuration.py`

Timestamp strings are retained in full. Consumers should compare their actual
identity relationships across the observed writes: for example, a rewrite
retains the original `created_at`, advances `record_revision` and store
revision, and records the source's actual `updated_at`. The source clock has
second precision, so strict timestamp inequality is not assumed. Migration and
rollback are outside this corpus; it does not generate or suppress UUID-bearing
results.

Each step records the local profile-root tree before and after invocation.
Preflight and validation failures therefore retain any observed lock or store
effects instead of being labeled write-free by assumption. Delete observations
cover the registry store and the class's available call surface only; the
registry has no credential revocation port, so this does not establish how a
separate credential owner handles revocation.

The artifacts are original Python observations only. They do not prove Rust
Core or Wasm parity, production import order, concurrent-writer behavior,
complete Provider Registry Pack coverage, external credential revocation,
Setup UI or GUI approval behavior, or any production activation state. A failed
or interrupted source run must be retained privately and retried to a new
output path.
