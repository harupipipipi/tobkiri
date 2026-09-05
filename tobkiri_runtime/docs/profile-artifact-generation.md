# Named Profile artifact generation

Profile source and release artifacts have separate roles:

- `ecosystem/defaultspack/v4/defaults.profile.intent.v1.json` is the only
  author-edited Defaults Profile definition.
- `ecosystem/defaultspack/v4/defaults.profile.lock.v5.json` is the immutable
  Profile v5 source-release lock. It pins the selected Base, Shell,
  Application, recursive Pack closure, executable variants, catalog inputs,
  and their digests. Its `activation_authority` is always `unbound`; it is not
  an activation lock and grants no runtime authority.
- `ecosystem/defaultspack/v4/defaults.release.provenance.json` binds the exact
  intent and catalog inputs, generator bytes, compatibility projection, and
  source-release lock.
- `ecosystem/defaultspack/v4/defaults.profile.v4.json` remains a generated
  compatibility projection for consumers that still load the historical
  filename. Despite the filename, its document API is Profile v5. Its embedded
  legacy provenance remains byte-compatible with the old core bundle check;
  it is not release authority. Only `defaults.release.provenance.json` binds
  the intent and generated outputs.

Run the compiler from `tobkiri_runtime/`:

```bash
python scripts/generate_profile_artifacts.py
python scripts/generate_profile_artifacts.py --check
```

`--check` does not repair files. It fails if the compatibility projection,
its `bundle.lock.json` entry, the source-release lock, or release provenance
differs from a fresh render. It also fails before rendering if any non-Profile
bundle input differs from its locked digest. Generated artifacts must never be
edited by hand.

Intent, bundle inputs, and release outputs reject symlink components, and all
outputs must remain inside the selected bundle root. Publication copies links
without following them, revalidates the staged catalog and every bound digest,
then atomically exchanges the complete bundle directory. If the host lacks an
atomic directory-exchange primitive, publication fails before changing the
authoritative bundle.

The compiler is not Defaults-specific. Use `--intent`,
`--compatibility-profile`, `--lock`, and `--provenance` for another Named
Profile whose compatibility path has one `profile` entry in the selected
`--bundle-root` lock.

## Compatibility migration

The compatibility projection is supported through **2026-12-31**. It may be
removed no earlier than **2027-01-01**, and only after repository search and
packaging tests prove that no runtime, launcher, presentation-catalog, or
distribution consumer reads `defaults.profile.v4.json` directly. Until that
gate passes, generators must update the projection and its bundle-lock digest
as one operation.

The intended migration order is:

1. Readers consume the intent only for authoring and the source-release lock
   plus release provenance for build verification.
2. Runtime activation continues to create its authority-bound ProfileLock v5
   locally; it must not treat the source-release lock as approval.
3. Compatibility readers move to an explicit generated-artifact interface.
4. After the deadline and consumer audit, remove the projection and its bundle
   entry in a dedicated migration.

When the older defaultspack bundle generator is needed, run it before this
Profile compiler. The final `--check` is authoritative for Profile artifacts;
this ordering prevents the compatibility generator from becoming a source of
truth again.

## Packaged bundles

`defaults.profile.intent.v1.json`, `defaults.profile.lock.v5.json`, and
`defaults.release.provenance.json` are source-checkout artifacts. The packaged
bundle generator removes all three from its staged output before publication:
they describe the source release and must not claim to describe a bundle whose
Shell and Pack artifacts were rewritten for one platform. A packaged bundle
retains `defaults.profile.v4.json` and `bundle.lock.json`; the latter is
rewritten after the compatibility projection so its Profile entry remains
byte-exact. Runtime consumers must not rely on the excluded source artifacts.
