# Inactive libc ABI oracle sources

These sources are not built, selected, or executed by a current product or CI
workflow. They prepare the independent runtime comparison required by
`ui_empty_poll_source.py`; no C ABI or schema success is claimed yet.

The extension calls actual external `poll`, errno access, clocks and the target's
supported synchronous signal implementation. It does not call Python's poll
wrapper, reset an error after observing it, or implement a replacement poll.
Its exported `observe()` takes no caller-supplied FD counts or timeouts. The
eleven finite cases include five incoming errno values, a zero-FD sentinel,
a positive timeout, nonzero invalid/negative FDs and priority-only validation.
It records actual ABI layout and synchronous signal results. Raw timing values
remain separate from the old/candidate behavior comparison.

The remaining builder must use the pinned SDK33 and target CPython headers,
produce one shared core-module helper, and use exactly those helper bytes in
both components. Require dynamic imports for `poll` and the real errno accessor,
the Python initializer export, and `dylink.0`/memory/table evidence. Inspect every
input DSO and the final link record to bind both functions uniquely to the
selected old/candidate libc. Header or pointer sizes alone do not prove this.

Use separate **non-stub** oracle components and the pinned Wasmtime48 genuine
WASIp2 context, with no preopens, inherited environment/arguments, network or DNS.
The normal schema component must remain cold, stubbed and restricted to its
existing clock interfaces; its ordinary non-clock denial probes remain required.
No general WASI linker may be added to that component to make the probe pass.

Actual SDK version/source correspondence, exact libc build inputs and flags,
shared-library/CLI/component hashes, independent deadlines, full C results and
cold schema replay are still pending. Negative timeout is excluded by the exact
source branch; unsupported asynchronous cancellation is a target limitation,
not a fabricated successful test.
