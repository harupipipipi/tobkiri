# Two separate Python Packs: static admission evidence

This archive records Linux source/static validation, **not** native Core/Gate
execution. No Core or Session was opened, and neither guest was instantiated.
See [the invocation guide](../../TWO_PACK_NATIVE_CHECK.md) for the separate,
still-required native replay and its scope.

The first actual static loader failed in production WIT inspection because a
world-only document could not resolve the caller interface. Its unchanged log
and graph index are retained as `failed-split-*`, not relabeled as successful.
The correction packages the original caller and canonical bridge definitions
into one reversibly constructed WIT document. Component bytes, original source
pins, permissions and production limits remain unchanged.

Observed checks:

- Production-parser regression: one pass, including negative old-layout,
  missing-dependency and duplicate-dependency checks.
- Corrected actual component/WIT/Profile static loader: one pass in 80.47 s.
- Final focused source/unit suite: 11 passes, two explicitly ignored tests.
- Materializer regression suite: 107 passes.
- Focused target Clippy with `--no-deps -- -D warnings`: exit zero.

The complete copied command output is retained. `static-result.json` is the
exact JSON observation extracted from the successful loader output. Final
source bindings are recorded separately: two constant test assertions and two
redundant borrows were adjusted for Clippy after the static run; final units and
Clippy then passed. Those style changes did not change artifacts or packaging.

No ignored native test was run. In-flight guest cancellation, production
activation, five-Pack admission, complete Gateway migration and live model
behavior remain unproven. `receipt-index.json` binds the copied evidence files;
it is an evidence index, not an authority grant or runtime acceptance receipt.
