# Deferred acceptance matrix

Run only after the parent releases the compiler/Cargo/capacity hold and approves
the runtime staging design. First close FS-01, DECL-01, ACT-01, BUILD-01/02 and
compile the C guard. A source-only or test-double receipt cannot replace these
checks. Preserve all prior failed attempts and frozen expected bytes.

1. Pin original full source, source patches, CPython/runtime/dependency locks,
   Rust/LLVM/WASI compiler, all flags, WAC and validation tools. Capture source
   hashes before and after each real build. Use the one approved shared target,
   two jobs, incremental off and debug info off; check the 5 GiB floor before
   each stage. Build sync/async runtimes sequentially outside the outer compiler
   Cargo invocation and verify their hashes at the original packaging hook.
2. In clean environment A, run at least three fresh producer processes on one
   immutable canonical declaration. In independent clean environment B, repeat.
   Record inner and outer hashes and exact sanitized commands/exits. All six
   outputs must match. A cache hit alone is not independent environment proof.
3. Perturb physical source paths, environment/HOME/TZ/locale, wall time, ambient
   entropy, inode/mtime/metadata, directory creation order and scheduling. Verify
   exact bytes remain unchanged. Probe undeclared filesystem, symlink escape,
   mutation after capture, writes, network/name lookup and ambient package
   discovery: all must fail. Verify guest hash seed flags before app imports.
4. Exercise no-random subset activation without an artificial random permission.
   For random-using subsets, test tracked implicit instances, random._inst,
   SystemRandom/secrets, cached imports/default arguments and saved aliases.
   Require live Core random dispatch/quota/audit evidence with the correct owner
   and epoch. Different bytes alone do not prove provenance. Never put actual
   entropy/secret bytes, headers, keys or keychain identifiers in receipts.
5. Deny activation, fail midway through multiple reseeds, reenter ACTIVATING,
   change epoch, cancel, shut down and use aliases after deactivation. No public
   handler may run before completed activation, and no state may return to
   BUILD_PREINIT. A partial failure discards/quarantines the instance. Every
   primitive denies time/PID/public-seed/ambient-WASI fallback.
6. Preserve runtime explicit application seeds for noncrypto reproducibility;
   seed(None) must use live Core only. Build explicit seeds and restored/unknown
   states stay unavailable until their own provenance proof. Reject custom C
   RNGs, alternate CSPRNGs and unreviewed cache packages. Audit retained seed
   buffers, caches and RNG instances independently of the public hash seed.
7. Retake typed own/borrow/drop/cross-interface alias tests and foreign nominal
   resource rejection. Verify the outer exports are only declared lifecycle and
   Pack interfaces. Inner initializer, private guard/activation entry points and
   ambient WASI imports must be inaccessible from the outer component.
8. Replay the unchanged 91 legacy cases through actual Rust Core/Pack loading,
   and record each result. Do not change the oracle, call native Python as a
   substitute, modify ABI allowlists, or mark unrelated inventory rows ported.

Receipts separate source checks, native SDK tests, compiler reproducibility,
resource ABI, actual Core randomness, and legacy parity. One success does not
stand in for the other gates.
