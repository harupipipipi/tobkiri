# WIT parser review follow-up, 2026-10-15

SWE review `swe-wit-parser-guard-002.log` reported no confirmed defect in the
supplied syntax guard and seven missing verification items. Primary-source
inspection closes items 1–4 and 6–7 below. Item 5 exposed an actual directory
admission/build aggregate gap; its repair is source prepared and requires a new
Root-run Rust receipt. The earlier 27 passing tests do not validate that repair.
This review does not establish complete migration or whole-process allocation
coverage.

## Primary dependency identity

`Cargo.toml:68` pins `wit-parser = =0.252.0`. `Cargo.lock:3559–3562` records the
registry package with SHA-256
`4266bea110371c620ccf3201c5023676046bc4556e5c7cfb5d500bda5ebc162d`.
The existing cached `.crate` archive is 183,381 bytes and has that exact hash;
no download was performed. Every inspected source below was compared
byte-for-byte with its member in that authenticated archive. Its
`.cargo_vcs_info.json` identifies upstream revision
`d66d4364c441d8b0122628509be52bd53c352a6e`, path `crates/wit-parser`.

The local primary-source paths have this prefix:
`<cargo-home>/registry/src/index.crates.io-1949cf8c6b5b557f/wit-parser-0.252.0/`.

| Source | SHA-256 |
| --- | --- |
| `src/ast/lex.rs` | `ad47cc89777997f11758f759af33613a270fbd0b33b784ab4fd6dcd8629ffd13` |
| `src/ast.rs` | `98433c6b0547a39542a36be6c71dbfbddefda60eaecc86be87d5c32ce0cbbf98` |
| `src/ast/resolve.rs` | `a18b886297f6109ca53b0352849c85ad9df37015721635bfe910561bc6b8f944` |
| `src/ast/toposort.rs` | `bdd603c88de7d168260ae3f25e070aadea957595de5dfaacf18af57d0da7c158` |
| `src/lib.rs` | `0056656289201243b1898531c7612c7bd53a348b4ee4bf125bc73cd6b3b35172` |
| `src/resolve/mod.rs` | `b8e0a270e29ac2213678aacc3e3bdbb5adcc9d46658242f921e582a9d346ba9a` |

## Seven review questions

1. **Line-comment termination matches.** `src/ast/lex.rs:248–255` stops `//`
   only on LF. `CrlfFold:447–456` folds CRLF to LF and leaves bare CR unchanged.
   U+2028 is not a line-comment terminator. The guard's LF-only scan therefore
   cannot conceal syntax that the pinned lexer resumes parsing after bare CR
   or U+2028. A bare CR outside comments is not accepted as lexer whitespace;
   permissive preflight whitespace handling there still ends in parser denial.
2. **Recursive AST productions are delimited.** Nested package recursion
   consumes `{` before parsing another declaration list (`src/ast.rs:65–79,
   128–147,266–280`). Recursive tuple/list/map/option/result/future/stream type
   parsing consumes `<` (`1393–1523`). Record/resource/variant bodies and
   function/constructor parameters use braces or parentheses (`826–878,
   994–1020,1109–1198`). Named type references are leaves. `use`/`include`
   parsing is nonrecursive (`623–728`); parse-time include resolution records
   world IDs (`src/ast/resolve.rs:1013–1030`). Type dependencies are processed
   in topological order (`908–950`) by an iterative heap-based sort
   (`src/ast/toposort.rs:44–130`). `collect_deps` and type resolution recurse
   through the syntactically nested AST; alias names use table lookup rather
   than recursively substituting definitions. Stability lookup explicitly
   avoids recursive alias traversal (`src/ast/resolve.rs:1354–1450`). This is
   a statement about the inspected grammar/parse paths, not a proof about every
   future upstream parser or all resolver resource costs.
3. **The reject set matches pinned tokens.** The enum/tokenizer
   (`src/ast/lex.rs:91–164,235–383`) has no quoted-string/character, backslash
   or square-bracket token. `String_` is keyword `string`, not a literal.
   `%` identifiers consume identifier characters, without escape syntax
   (`295–307`). Attributes parse unquoted semantic versions or IDs
   (`src/ast.rs:1657–1696`), so quoted attribute arguments are not supported.
4. **All guest-text parse entry points use the guard.** The only invocation
   of `UnresolvedPackageGroup::parse` in Manifest/WIT host code is
   `wit_preflight.rs:19`. Its callers in `shapes.rs` are import-closure WIT
   (`214`), declared shape WIT (`395`) and error-provenance WIT (`506`). Each
   later `push_group` receives a guarded group. The import path scans every
   packaged `.wit` file, including dependency/world files and extra WIT absent
   from shape declarations. Exact copies of the committed Core constants are
   skipped; guest redefinitions of Core are denied. Package verification
   calls `load_shapes`, while Wasm loading calls `allowed_imports_for_pack`
   (`crates/tobkiri-wasm/src/host.rs:393`). Thus unused extra WIT is not parsed
   by `verify_tpk` itself, but cannot bypass the guard when it is parsed for
   runtime import closure. `tobkiri-wit/src/lib.rs:44–55` separately parses
   committed Core directories, not caller-selected guest directories.
5. **Aggregate bounds required a repair.** Before this follow-up, `.tpk`
   input had a 512 MiB byte cap, 16,384 archive entries and 128 MiB members;
   directory admission had individual 128 MiB reads and a StrictV1 manifest
   cap, without an explicit aggregate member count/byte budget. `build_tpk`
   also cloned input content before checking an aggregate. The source repair
   below applies the existing archive budgets to all these paths. Its tests
   have not yet run at this documentation checkpoint.
6. **The API signature is confirmed.** `src/lib.rs:331` declares
   `parse(path: impl AsRef<Path>, contents: &str)`. It creates a source map,
   pushes the supplied text and parses it (`331–340`). The path is for error
   reporting; this call does not read the filesystem.
7. **The positive error-provenance expectation is correct.** The fixture in
   `tests/shape_expansion.rs:175–197` returns
   `result<list<string>, string>`. `core_error_result_operations` follows
   aliases and accepts only the exact committed Core error identity
   (`shapes.rs:537–600`). Primitive `string` does not qualify, so the empty
   operation set is expected.

## Measured evidence and source-only aggregate repair

The parent workspace retains
`.work/evidence/snapshot-manifest-wit-preflight-nominal3-20261015.json` and
its matching `.log`. The receipt records the offline locked
`cargo test -p tobkiri-manifest --no-fail-fast` run from
`2026-10-15T08:33:14.097902+00:00` to
`2026-10-15T08:33:16.767709+00:00`: exit 0, no capacity guard stop, identical
before/after sources. The actual log reports 17 contract, three nominal error
provenance and seven shape-expansion tests passed; zero failed or ignored.

That receipt's relevant source SHA-256 values are:

| Source at the 27-test checkpoint | SHA-256 |
| --- | --- |
| `src/wit_preflight.rs` | `1945ea02e095ea34102a81b4df03612591772dcd505a48fa5516c31519761238` |
| `src/shapes.rs` | `51ebaf228ce96ded4220f95320de0815d676b003e06f28d0d3bdfbeb75edbe7a` |
| `src/package.rs` | `3ae985d8e755f98ca5f24c47fd47faa02987503688d9e580701ab7ebadb66f9a` |
| `tests/shape_expansion.rs` | `a3e96496e0291455eeb363ce5e2c3d236a0ac612c96654aacce12a6860f4cc26` |

The aggregate source repair changes `src/package.rs` and adds
`tests/directory_bounds.rs`. Its shared checked budget charges a 512-byte
header plus payload rounded to 512 bytes for every entry, and 1,024 final zero
bytes, including `pack.json` and any `signature.json`. The ordinary ustar writer
recipe is unchanged. Directory admission checks the declared aggregate before
reading artifact contents, limits manifest reads to the existing 4 MiB
StrictV1 cap, limits each artifact read to its declared size, and gives the
signature only the remaining framed-byte budget. It retains the existing
descriptor-relative no-follow reads and stable-file identity checks. The
reader's existing one-byte growth probe and native buffer capacity behavior do
not imply exact heap/RSS accounting.

`build_tpk` checks count and actual supplied content lengths before cloning
artifact contents. Archive verification and writing use the same checked
ustar budget. A valid package's manifest canonicalization, content hashes,
signatures, order, headers and padding are preserved; no Pack artifact or pin
was regenerated by this change.

Three new private-budget unit tests cover exact framing, the 512 MiB boundary,
member/count limits and checked overflow using size arithmetic and tiny buffers.
Four new public-route tests cover directory aggregate refusal before nonexistent
artifact reads, count refusal with only one on-disk manifest, a 64 KiB sparse
metadata fixture rejected against a zero-byte declaration, and unchanged
ustar bytes/pins for a good small package. The 16,384 descriptors in the count
case are in memory; it creates no thousands-file disk tree. Directory cases are
Unix-only because the existing source reader fails closed elsewhere.

Only explicit-file rustfmt and read-only source/hash inspection were performed
for this repair. Root must obtain a new capacity-controlled receipt with:

```sh
cargo test --offline --locked -p tobkiri-manifest --no-fail-fast
```

Neither the earlier 27 passes nor formatting establishes execution of the seven
new tests, current full-workspace success, native platform acceptance, final
Python replay or migration completion.
