# Two separate Python Packs through native Core/Gate

On 2026-10-21 the documented caller/bridge route passed on arm64 macOS 15.6,
Darwin 24.6.0, with the existing local APFS owner checks enabled. This is a
test-Host Core/Gate replay, not Launcher GUI or production Gate acceptance.

The execution tree is exactly commit `42dfc6e02d7477ccbbf0e3692256d6c6e781c9a8`
plus `execution-source.patch`; its SHA-256 is recorded in both supervision and
`receipt-index.json`. Later source changes are not retroactively covered by
this receipt. The patch adds bounded package-file capture and makes the native
receipt's reference and observed runtimes explicit; it changes no guest,
source-oracle, graph index, expected result or resource-limit pin.

The input graph was prepared by `stage_preserved_gateway_tool_caller.py` from
the checked-in compressed fixtures. Its index SHA-256 is
`0441bde565b5e1765a8fdc262e61db00a5fff0c160624d99d5d841f65c737d96`.
The two component sources total 35,798,851 bytes under the unchanged 64 MiB
catalog ceiling. Both actual guests reported CPython 3.13.16 / Unicode 15.1.0;
the preserved source reference is CPython 3.13.5 / Unicode 15.1.0. This is not
a claim that the source oracle and guest used identical interpreter versions.

The exact Cargo command is in `supervision.json`. One test passed, none failed
or were ignored; 38 unrelated tests were filtered out. Cargo and the test
completed in 335.510 seconds, without a supervisor stop or source change.
The raw Cargo log and execution patch retain their exact original whitespace;
the receipt index authenticates those bytes without normalizing either record.

`native-caller.json` records all 30 matching cases, runtime initialization for
both Packs, exact caller permission removal with zero provider dispatches, and
shutdown admission rejection with zero user/guest dispatches. All four created
Sessions were closed and their closes awaited. Shutdown admission is not
deterministic in-flight guest cancellation or CPython lifecycle shutdown.

Production enablement, five-Pack finalization, full Gateway migration,
retry/billing parity, complete legacy HTTP-error behavior, live model generation,
original Setup/settings/chat GUI parity and Python/PackVM retirement remain
outside this proof. No migration inventory unit or full issue acceptance item
is closed by this trial.
