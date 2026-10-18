# DeepThink development Packs

DeepThink adds a resumable workflow to the Rust/Wasm Harness introduced in
PR #1521. It is an ordinary Pack overlay, selected by Profile bindings. User
input and continuation enter Core Flow; the product accesses models, tools and
persistence through typed ports and Gate.

## Workflow

The workflow first turns the request into a Goal. It defines each abstract
quality term separately, then establishes acceptance criteria before preflight,
planning, capability assessment, tool/skill selection, working environment,
Profile checks, references, evidence, individual sections, integration and
review. Each step produces a short public decision summary. Private reasoning
is neither requested nor recorded.

Before ordinary work, a separate Goal-contract gate reviews the candidate with
field-level evidence pointers. It allows at most two semantic reviews and one
corrected candidate, retaining the original contract and all paid attempts.
Existing Runs require explicit adoption at the selected revision and snapshot;
continuation cannot reset the limits. Model approval is evidence, not a proof
of arbitrary natural-language consistency. See the
[Goal-contract review contract](../docs/migration/1520/deepthink-goal-contract-review.md).

The Goal and checkpoint are logical artifacts in the Pack's private Core store,
including `goal.json`, `goal.md`, drafts, accepted answers and quarantined
candidates. These names do not grant access to arbitrary host files.

`local.deepthink-input` provides `local.deepthink.continuation-input@1.0`;
DeepThink owns its WIT and private SQL store. Status and reconciliation use a
control Flow, while continuation and regeneration prepare history in their
first worker step. This preserves the Core instance re-entry guard. The input
adapter has no storage, network or secret authority.

Continuation retains the original Goal, completed generations, tool receipts and
usage. Clarification answers re-evaluate the stopped phase. An eligible,
confirmed failed generation can be retried without discarding earlier work.
An unknown generation, tool outcome or checkpoint commit requires reconciliation
and never authorizes automatic paid replay. Review limits still apply when
resuming. Each invocation completes at most one workflow phase and saves its
paused transition atomically; explicit continuation selects the same Run.

Malformed JSON and schema-invalid public responses are preserved separately from
the successful generation cache, together with their request digest and usage.
The rejection, error and quarantined state share one atomic checkpoint. Recovery
from a confirmed saved rejection retains the old response and billing receipt.
An unconfirmed save must be reconciled before a new generation is allowed.

A public invocation stops after a format rejection rather than starting another
provider attempt in the same request. Within the captured correction limit it
first saves the rejection and then a prepared `awaiting_retry` checkpoint. The
stream emits usage, progress and the distinct retry boundary before EOF. It
does not publish an answer or count a completed phase. Explicit continuation
of the same Run validates that saved preparation and claims its next attempt
through the ordinary Core path. UI Resume remains a manual action. Repeated
status reads and implicit workflow calls cannot consume the preparation.

The DeepThink Pack UI can restore a saved Run by manually entered Run ID and
conversation. “保存状態を読む” calls only the ordinary read-only STATUS port;
it never submits a model request or appends history. Browser storage is optional
and cached references stay unverified until this explicit read. Resume is a
separate manual action and is disabled for Core-pending, reconciliation, running,
quarantined or completed state. Unknown accounting stays unknown and alone does
not forbid a continuation that Core permits. The UI does not display a rejected
candidate as an answer. STATUS does not return the original conversation, so the
entered conversation is not proven by a successful read; Core validates it on
continuation. UI call/pull waits explicitly use the existing 300,000 ms transport
bound without changing Core's original deadline. The existing SDK cancel wait is
unchanged. Goal-review controls remain a separate UI gap.

Pending attempts and unknown checkpoint/tool outcomes cannot become retry
boundaries. Installing this version does not repair an older pending attempt
or authorize its replay. See the
[explicit contract-retry boundary](../docs/migration/1520/explicit-contract-retry-boundary.md).

## OpenRouter and usage

Build the existing components and then compose the DeepThink Profiles:

```sh
cargo test --manifest-path packs/Cargo.toml -p tobkiri-guest-sdk -p tobkiri-pack-harness-tools -p tobkiri-pack-deepthink -p tobkiri-pack-deepthink-input
cargo build --manifest-path packs/Cargo.toml --workspace --locked --target wasm32-wasip2 --target-dir <target>
python3 -B packs/build.py --skip-build --target-dir <target>
python3 -B packs/build_deepthink_profile.py --target-dir <target>
node --test packs/tests/test_ui_accounting.mjs packs/tests/test_ui_continuation.mjs packs/tests/test_ui_restore.mjs
```

The build uses Rust 1.94.0, `wasm32-wasip2`, and build-time Python with PyYAML.
It creates `deepthink-mimo.profile.json`, its UI variant and DeepThink variants
of all seven existing style presets. Published documents and provenance are
written beside them. Existing local/mock Profiles keep their own providers.

The new provider uses `https://openrouter.ai/api/v1` and
`xiaomi/mimo-v2.6-flash`. Its only credential reference is the Profile-scoped
`api-key`; configure it through Core's secret API after creating and approving
the Profile on a supported host. Credentials are not part of Pack config or
published Profiles.

The MiMo DeepThink provider explicitly enables JSON mode because every workflow
phase exchanges a JSON envelope. This requests valid JSON from the provider;
the workflow still validates the phase schema and semantic acceptance criteria.
Other provider Profiles retain their ordinary text format.

`max_tokens: 0` means omit the output token parameter from the provider request.
Positive explicit limits are preserved. A successful stream requires an
explicit normal finish and `[DONE]`; a disconnect is not a successful answer.
Provider-reported input, output, cached, reasoning and total tokens, plus cost,
are retained and displayed. Missing receipts remain unknown, and partial totals
identify unconfirmed attempts rather than reporting their usage as zero.

The native `profile session` host accepts sequential typed User requests and an
explicit close. Session/invoke protocol JSON uses stdout; Pack console display
uses stderr. Keep the normal terminal approval path and do not treat merged
stderr as protocol authority.

## Tools

`local.harness-tools` provides nine operations:

- `artifact.write/read/stat/list/delete`: private UTF-8 text artifacts, SHA-256,
  metadata pagination and deletion tombstones. Text is limited to 256 KiB.
- `calculator`: exact integer add/subtract/multiply/divide within
  +/-9,007,199,254,740,991. Division must have no remainder.
- `json.validate`: JSON syntax, type and error location.
- `clock.now`: Core clock in Unix milliseconds.
- `random.bytes`: 1 through 256 bytes from Core randomness.

Artifact mutation and its receipt are stored atomically in one SQL row. Stable
call IDs prevent replay from reapplying an earlier mutation; reuse with
different arguments is rejected. Artifact paths are relative logical names,
not host filesystem paths. Historical content and receipts remain after delete.

These are new tools. The legacy Defaults tool catalog, terminal, browser,
Computer use and 3D editing software are not migrated by these Packs.

## Verification and current platform limits

The input adapter and worker have focused transport, persistence, contract and
review tests. The [dedicated CI for d81aaec3d](https://github.com/harupipipipi/tobkiri/actions/runs/39080716090)
passed on Linux and Windows; the broad repository workflow was not accepted.
The nine new tools have actual Core evidence for 20 successes, nine expected
errors and five negative cases. Those tools cover private text artifacts and
utilities; they do not establish execution of the 154 legacy tool definitions
or model-selected tool use. Artifact, Profile and ABI checks must be rerun after
changes to WIT, bindings or staged components.

Actual Windows test-DATA Core sessions have exercised the SQL repository,
provider interruption, retained progress and typed User recovery operations.
The optimized test CLI explicitly enables the existing test-DATA constructor;
this does not certify an ordinary production release or every supported host.
An early real SQL MiMo run retained a 2,025-byte prefix and confirmed 3,067
tokens, but reached the unchanged 300-second deadline before accepting its
first phase. Provider metadata was terminal and public completion returned 404.
A later controlled Goal review returned invalid evidence pointers and missed
an inconsistent calculation criterion, then failed during stream cleanup.
Read-only diagnosis retained its original Goal, phases and receipts, with six
attempts and one pending claim. Known accounting was 20,992 tokens and
USD0.0035605272 in received receipts; tokens were incomplete and total cost was
unknown. These observations establish retained state, not a completed workflow,
permission to replay, or proof of earlier physical cleanup. The pointer prompt
repair and generic cleanup/output repairs still require separate live acceptance.

Control results use Core's shared 1 MiB event transport budget. The adapter
also refuses more than 128 frames or 8 MiB examined bytes; it does not promise
the former generic 4 MiB output capacity. Completed is not success until EOF;
unknown cleanup or commit errors remain failures without an automatic replay.

Follow [Issue 1520 integration and recovery](../docs/migration/1520/deepthink-run-ledger.md)
when transplanting this work into `codex/issue-1520-rust-core`. Unpublished host
changes, all Defaults host/Profile behavior, full UI, legacy tools and complete
MiMo execution remain separate acceptance items. Terminal-Bench has not run.
