# Issue 1520 checkpoint: 2026-10-18

This checkpoint continues PR #1521 from its preserved `7f79668d8` WIP tree.
It is **not completion of the migration**. The [20-item acceptance checklist](acceptance-status.md)
remains open and the inventory remains 1/837 closed, 836 TODO.

## Changes

- Atomically publish native stream close-control and receive the first close
  cause under the existing intent mutex. Callbacks and cancellation notifications
  remain outside custody locks. Five focused regressions include a deterministic
  snapshot/receipt interleaving, concurrent publication and panic custody. This
  does not establish a repair of the historical two-second cleanup timeout.
- Make the final migration gate use the inventory validator, including current
  source digests, full discovered coverage and contained evidence/fixture paths.
  Historical TODO evidence remains historical; no inventory status is promoted.
- Repair invalid job-level `runner.temp` expressions and add a standard
  `macos-15` runtime job with explicit arm64/Darwin24.6.0/APFS preflight. A changed
  runner image must fail, not widen production platform support.
- Keep Pack-specific CSP fixtures outside generic host source; run their source
  tests in CI. Bind the Python SDK fixture to canonical checked-in WIT rather
  than an ignored build copy. Point legacy approval contracts at `legacy.rs`.
- Test current Input handle semantics: exactly one frozen Flow route, rather
  than the obsolete assertion that all stream/callback forwarding is absent.
- Apply rustfmt to nine preserved Audit/Census files without semantic changes.

## Local evidence

All results below are local Linux/x86_64 checks, not native macOS acceptance.

| Check | Result |
| --- | --- |
| Locked full workspace compilation (`--no-run`) | Passed; no runtime acceptance inferred |
| Actual typed Wasm test guests and 16 Pack fixtures | Built successfully; generated platform-dependent manifest pins not committed |
| Canon/WIT/Manifest/Profile/Flow/Gate tests | 251 passed |
| Native close-handoff regressions | 5 passed |
| Full workspace execution | Failed at Core: 119 passed, 56 failed; not green |
| Foundation boundary checker | 0 failures |
| Boundary / inventory / CI-helper regressions | 13 / 9 / 9 passed |
| Python / JavaScript SDK | 54 / 12 passed |
| Native Pack UI source probes | 20 passed, VM-model/source evidence only |
| Core-host frontend / production build | 30 passed / built |
| Legacy ambient approval source contracts | 23 passed, 5 existing skips |
| Strict full-workspace all-targets Clippy | Passed (`--no-deps -D warnings`); vendored Wasmtime dependency emitted 8 warnings |
| Formatting / diff whitespace | Passed |

The initial full-workspace execution reached a deliberate platform restriction:
`crates/tobkiri-store/src/owner.rs` permits native allocation/DATA ownership only
on local APFS, macOS arm64, Darwin exactly 24.6.0. Linux returns `Unavailable`.
This guard was not relaxed, and failed tests were not skipped or relabeled.
Current standard `macos-15` CI is intended to obtain actual supported-platform
results; configuration alone is not proof of success. Original Linux runtime
and final-completion gates remain visible and may fail.

## Remaining work

- Obtain supported-platform full-workspace, owner/DATA, compiler/parity and
  native UI evidence on the current sources. No paid model request was made.
- CoreHost StreamCancel still uses late nonretrying close-control publication;
  contention handling needs the same custody discipline as Invoke/Pull while
  retaining the original source context and permitting expired-source cleanup.
- Physical Proc/PTY, ordinary Harness tool migration, all remaining legacy units,
  native GUI Send/save/Stop, model-selected tool effects and distribution without
  Python remain unaccepted. See the complete checklist rather than interpreting
  one green test selection as epic completion.
