# Original Python Model Registry read observations

The source revision is `6818f8ef7fae92685794acceccf71924d6eafd6b`, the
`codex/issue-1520-native-shared-state` head at the start of this slice.
[`original-python-read.json`](original-python-read.json) binds the 16 staged
source files by repository path and SHA-256. Each case records the synthetic
input document and the original return value or exception class and message.
A null document means the store file was absent.

From the repository root, the exact capture command on macOS arm64 was:

```sh
/opt/homebrew/bin/python3 -ISB tools/quality/python_model_registry_compat/generate.py > packs/local.model-registry-rust/evidence/original-python-read.json
```

The capture ran on CPython 3.14.0. Replays on CPython 3.14.0 and 3.11.15 matched
the fixture byte for byte. To replay without overwriting the fixture, run the
generator with an installed CPython and compare standard output:

```sh
python3 -ISB tools/quality/python_model_registry_compat/generate.py |
  cmp - packs/local.model-registry-rust/evidence/original-python-read.json
shasum -a 256 packs/local.model-registry-rust/evidence/original-python-read.json
```

The 43,035-byte fixture has SHA-256
`f1dffc0f877d15d24692b565b998aa6a292ec1666c37a27846a6012259234129`.
The 25 cases cover an absent store, unsorted profiles and aliases, exact
reads, alias precedence and one-hop resolution, missing targets, a disabled
route, legacy provider binding, invalid store data and identifiers, and
Settings validation. Raw reads retain a fractional number, signed zero, an
integer above the IEEE 754 safe integer range, nested values, and synthetic
opaque handles. Settings projects the output limit and configured routes and
filters the disabled profile and private fields. The `settings-saved-projection`
case uses profile `prof_model_fixture` for replay with the same legacy document.

The generator stages the original Git blobs in a temporary tree and invokes
`ModelRegistry.snapshot`, `get` and `resolve` on disposable roots. Settings
passes the original snapshot to the original `present_model_profiles`.
The generator compares the exact store bytes before and after each read,
including continued file absence. This captures the original Python read and
presentation boundary. Registry mutations, host dispatch, Rust and Wasm,
Core storage, and UI integration require separate evidence.
