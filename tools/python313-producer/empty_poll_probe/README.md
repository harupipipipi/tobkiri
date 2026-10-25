# Inactive libc ABI oracle sources

These sources remain inactive in the product. The separate
`issue-1520-ui-empty-poll-helper.yml` workflow compiles the shared helper against
headers from a genuine original WASI CPython build and inspects its actual
dynamic imports. It does not execute the C cases or claim C ABI/schema success.
The independent old/candidate runtime comparison remains required.

The extension calls actual external `poll`, errno access, clocks and the target's
supported synchronous signal implementation. It does not call Python's poll
wrapper, reset an error after observing it, or implement a replacement poll.
Its exported `observe()` takes no caller-supplied FD counts or timeouts. The
eleven finite cases include five incoming errno values, a zero-FD sentinel,
a positive timeout, nonzero invalid/negative FDs and priority-only validation.
It records actual ABI layout and synchronous signal results. Raw timing values
remain separate from the old/candidate behavior comparison.

`build_ui_poll_oracle.py` uses pinned SDK33 and installed target CPython headers
whose `pyconfig.h` must match the actual successful WASI build. Its networkless
supervised stage records the compiler's real link command and produces one
shared core-module helper. The selected recipe explicitly excludes default
CRT/libc inputs with `-nostdlib`/`--no-entry` and exports the real Python
initializer. A separate compiler-default variant is recorded only to diagnose
the actual default-link behavior; it is never selected as a fallback.
Compilation and inspection have separate supervised outcomes.
`inspect_helper.py` records actual metadata before rejecting any failed guard,
and requires dynamic imports for
`poll` and `__errno_location`, the Python initializer export,
`dylink.0`/non-shared wasm32 imported memory/table evidence, matching core
function signatures and real exports in the stock SDK libc. It neither
instantiates nor executes those modules.

The later runtime builder must use exactly the same helper bytes in both
components. Inspect every input DSO and the final link record to bind both
functions uniquely to the selected old/candidate libc; that complete provider
proof is still pending. Header or pointer sizes alone do not prove this.

Use separate **non-stub** oracle components and the pinned Wasmtime48 genuine
WASIp2 context, with no preopens, inherited environment/arguments, network or DNS.
The normal schema component must remain cold, stubbed and restricted to its
existing clock interfaces; its ordinary non-clock denial probes remain required.
No general WASI linker may be added to that component to make the probe pass.

The separate libc-build receipt records actual SDK33 source/version
correspondence and build inputs. Shared helper/CLI/component linkage,
independent runtime deadlines, full C results and cold schema replay are still
pending. Negative timeout is excluded by the exact source branch; unsupported
asynchronous cancellation is a target limitation, not a fabricated pass.
