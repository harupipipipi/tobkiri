# Installed-defaults Bundle Catalog Pack

This ordinary Python Wasm Pack calls the original
`tobkiri_protocol.bundle_catalog.BundledCatalog.load_from_reader` method. Its
immutable v2 config carrier contains the defaults lock bytes, unique raw-byte
blobs, and every ordered entry-read occurrence, including executable-catalog
rereads. Occurrences reference blob ordinals only; sharing requires exact byte
equality and never removes a read or its original validation. Different reread
bytes retain different blobs. Unreferenced blobs are rejected. Both unique
owned bytes and the complete logical read sequence stay within four MiB,
including the lock; serialized config and result each retain their four MiB
limits. Lifecycle
initialization checks only that carrier's exact fields and bounded Base64
bytes; `catalog.read` leaves lock, digest, schema, identity, and pin decisions
to the original loader.

The loader receives the non-filesystem `BundleDefaultsOwner` as its `root` and
a deferred artifact-declaration reference to that same owner. Neither view has
path conversion, existence, approval, or IO behavior. Raw source bytes are
passed to the original reader unchanged. The port returns only the original
`packs`, `bases`, `shells`, `profiles`, and `executable_catalogs` collections.

This Pack does not perform platform-artifact IO, active-Profile resolution,
currentness checks, or full Setup/GUI wiring. It requests no host filesystem,
network, storage, process, terminal, or native capability. Actual admission, Gate
verification, and the dedicated Setup review remain Core responsibilities.
