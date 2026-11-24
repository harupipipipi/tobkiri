# Formats checkpoint (2026-10-15)

The oracle is the unchanged legacy source at
`cd7c20e9d52535674c36cccac4d03ef70e9f809b`. `generate.py` imports the
original Python algorithms. `generate_extra.py` executes selected original
Python AST functions and verbatim Rust functions in an IO-free harness. Source
file and callable hashes are in `vectors/canon/provenance.json` and
`vectors/seals/extra-provenance.json`. Keys, clocks, UUIDs, records and identities
are public, deterministic fixtures; no runtime, VM, server, live approval,
user-data store or vault constructor is run.

| Corpus | Cases | Verification |
| --- | ---: | --- |
| StrictV1 / LegacyFloat / LegacyPackSig | 885 each | Original expected bytes, digests and rejection cases |
| Workflow HMAC, authority lease, authority audit, Pack signature, artifact tree, continuation | 300 each | Source-specific bytes/hash/HMAC/signature recipes |
| UI operator v1/v2/v3, approval token, approval argument hash, sealed environment, PackVM bridge | 300 each | Exact newline, base64 transport, ignored-key and insertion-order rules |
| Launcher response, debug CLI operator | 300 each | Original Rust body; exact bytes observed at the signer boundary |
| Release signature preimage | 300 in each language | Original Python and Rust NUL-joined 12 fields agree |
| PackVM deadline | 331 | Original positive/finite `.17g` outputs and rejection results |
| Pack signature adversarial verification | 300 | Original acceptance/error results, including weak keys |
| New WIT float mapping | 1023 | Actual Rust declared-width formatter bit/string vectors from SDK, copied exactly |

Codec inputs include duplicate keys (including escaped duplicates), non-BMP key
ordering, controls, surrogate errors, malformed UTF-8, wide integers, finite and
nonfinite floats, negative zero and depth boundaries. Rust tests separately
exercise exact 4 MiB input/output limits, actual publisher key binding, inventory
tampering, symlink denial, hierarchical names/mapping collisions, WIT minor
compatibility/projection, Profile conflict compilation and overlay ordering.

## Reproduce

Use the existing legacy checkout's Python environment, then the pinned root
Rust workspace. No new installation or Tauri/VM build is required.

```sh
/Users/haru/.codex/worktrees/e8bc/tobikiri-pr-1322/.venv/bin/python -I -B tools/legacy-vectors/generate.py --check
/Users/haru/.codex/worktrees/e8bc/tobikiri-pr-1322/.venv/bin/python -I -B tools/legacy-vectors/generate_extra.py --check
python3 tools/legacy-vectors/freeze_wit_floats.py --check
CARGO_BUILD_JOBS=2 CARGO_INCREMENTAL=0 CARGO_TARGET_DIR=../.work/cargo-target cargo test --locked -p tobkiri-canon -p tobkiri-manifest -p tobkiri-profile
```

All three generator checks passed at this checkpoint. The final focused Rust
command exited 0 with **41 tests passed**: Canon 16, Manifest 13, Profile 12.
The typed-Serde regression rejects nonfinite values instead of silently turning
them into JSON null. Owned crates also passed `cargo fmt --check`. These checks
are format and compiler tests, not full runtime, platform or migration acceptance.

## Explicit gaps

- **Legacy crypto acceptance differs.** The original `verify_signed_manifest`
  accepts an identity-point key with R=identity/S=0 and returns
  `verified: true, authority_granted: false`. The Rust strict verifier rejects
  that fixture. Expected legacy results remain unchanged. The regression test
  explicitly records this security/parity gap; it does not weaken verification.
  Legacy acceptance/migration policy awaits the architecture decision. R11.4
  is not fully complete.
- **Rust `.17g` parity is not implemented.** The original deadline corpus is
  frozen and roundtrip checked; Rust's declared-width scientific formatter is
  a different approved codec and must not be substituted for this legacy rule.
- **Windows source-directory admission is unavailable.** Unix source reads use
  descriptor-relative `openat` with `O_NOFOLLOW` on every child and stable regular
  file identity checks. Other platforms fail closed; in-memory `.tpk`
  verification is portable. A reviewed Windows handle backend remains required.
- Audit ciphertext is outside this oracle: the original hash routine runs
  with an identity-encryption fixture sink and in-memory SQLite. Live lease
  consumption, nonce enforcement and approval/state transitions belong to the
  gate/runtime tests.
- Exhaustive classification of legacy `isoformat`/`default=str` digest
  boundaries and Swift `JSONSerialization` acceptance behavior remains open.
- Compiler minor binding requires verified WIT snapshots for the requested and
  provided revisions. Unknown revisions are denied rather than guessed from a
  nearby same-major schema. Known compatible minors bind by name and major.

The Python tools remain preparatory migration evidence. Keep the frozen vectors
when the authorized final Python deletion gate is reached.
