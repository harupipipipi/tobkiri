# Tobkiri guest Packs

See [DeepThink development Packs](DEEPTHINK.md) for the resumable Goal workflow,
OpenRouter MiMo Profiles, new artifact tools and current platform limitations.

Unsigned development Packs use `local.*`. The Rust guest workspace is separate
from the host workspace, targets `wasm32-wasip2`, and pins wit-bindgen 0.62.0
(published 2026-09-10; 0.63.0 was too new under the reviewed dependency rule).

Build real components and refresh their manifest/Profile hashes:

```sh
python3 -B packs/build.py
python3 -B packs/publish_presets.py
```

The script uses the shared `.work/cargo-target`, two build jobs, no debug symbols
or incremental builds, and the agreed 5 GiB free-space floor. It never invents
component digests. Every guest's `runtime.config` is bound by `files[]`; lifecycle
initialization receives those artifact bytes. Host/CLI configuration maps are
not used. Derived preset Packs publish their own immutable prompt defaults.

`profiles/harness-live.profile.json` uses the external OpenAI-compatible provider
at the explicitly declared origin `http://127.0.0.1:18080`. Its model and `/v1`
URL live in the provider's config; its only secret reference is `api-key`. Set the
secret through `tobkiri secrets set --profile <id> --name api-key --from-file <file>`.
The key is never part of a Pack or published Profile. `harness-mock` uses a real
wasm deterministic provider. `echo` uses an entirely different set of Packs,
shapes and Flow with no Harness Pack.

All chat processing is load → prompt → generation stream tee → append, expressed
in each domain Pack's Flow. Terminal input and output are separate components;
persistence is a guest operation against the private core store.
The provider's SSE framing handles split UTF-8, LF/CRLF, comments, usage and DONE;
dropping a cancelled response closes the external request. Core imports enforce
the gate, cancellation, original deadline, exact origin and opaque secret binding.

pi extensions are byte-identical recorded copies at a pinned upstream commit.
Install development build tools with `npm ci --prefix sdk/js`, then build:

```sh
node sdk/js/wrap-pi.mjs packs/local.pi-example-hello
node sdk/js/wrap-pi.mjs packs/local.pi-example-protected-paths
```

The compiler uses build-time esbuild aliases and componentize-js, with direct
WASI HTTP disabled. It ships a wasm component, unchanged upstream source and
provenance, with no native Node runtime. Unsupported pi APIs throw explicitly.
The adapter and wrapped extension are admitted as two ordinary Packs.

`local.shell-policy-python` is a Python-on-wasm pilot built with the existing
componentize-py 0.25.0 / wasmtime-py 48.0.0 development pins. Its captured legacy
classifier source is SHA-256 pinned; the new pipeline records all sources, WIT,
tool versions, imports and component bytes. Its 91 frozen legacy inspection
fixtures must replay through the Rust host before any handler parity claim.

These are new units and migration pilots. They do not replace the whole old
Defaults webapp, tools, browser, workers or mediators. The seven published style
profiles preserve their legacy specifications in adjacent provenance files;
their capability parity remains pending. `migration/inventory.json` is the
authority for migration status and deliberately keeps the final deletion gate
closed while any old feature lacks evidence.
