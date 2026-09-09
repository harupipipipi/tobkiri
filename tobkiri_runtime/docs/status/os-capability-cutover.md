# OS capability cutover — follow-up B, Draft

This is a bounded clipboard/broker follow-up to #1322, not a claim that the
repository's OS migration or release proof is complete. Approval management is
owned by follow-up A; this work uses the parent's Authority records and ports.

## Recorded bases

- Initial branch: `codex/os-capability-cutover`, created at
  `3119f34329437c009f68a8c212c2a4487c3b3d13` in a separate checkout.
- First inspected parent: `05715d43dc4c6fd94329e706e5532087d636505a`.
  Three commits / 23 files changed, mainly conversation/settings reads. The
  branch was fast-forwarded only after that comparison.
- Next inspected parent: `50dbe99f` (after observing `be8f06ac`), merged with
  recorded history. Parent changes include caller-edge selection, inherited
  request deadlines and rejection of late completed results; these are retained.
- Latest inspected parent: `351aa93f7911b022de6df0de099da31a8a6fdf52`.
  Compared 479 changed files against `50dbe99f`; merged with recorded history.
  Shared cancellation and retention of execution resources until actual worker
  completion are preserved alongside the worker-time lease recheck.
- Follow-up: https://github.com/harupipipipi/tobkiri/pull/1464 . While #1322 is
  unmerged, its head branch is the explicit base. After parent merge, retarget
  to `soon` and verify the diff contains only this follow-up. Never `master`.

## Fixed-source inventory and scope

The starting observations below were traced in `05715d43` with `git show` /
`git grep`; parent PR prose and names alone are not implementation evidence.

| Function | Formal operation / route | Execution / OS resource | Starting state and this follow-up | Evidence still required |
|---|---|---|---|---|
| Clipboard read | `tobkiri.resource.clipboard.v1` / `rumi_clipboard_host_service_pack.clipboard-read` | Normal caller → canonical Broker → exact Host Extension hook → Host transport → `/usr/bin/pbpaste`; global clipboard | Contract and HostIntent existed, OS runner was disconnected. Hook/transport added; finite macOS native adapter. Read preview remains a presentation choice, not narrower OS scope. | Real granted normal-Pack invocation; macOS native and packaged app |
| Clipboard write / clear | `tobkiri.action.clipboard.v1` / `rumi_clipboard_host_service_pack.clipboard-write`, clear is text `""` | Same route, `/usr/bin/pbcopy`; global clipboard | Same missing hook. Existing operation retained, no new implicit permission. Legacy boolean runner retired; controller and media projections call formal contracts. | Native success, denial, cancellation, process cleanup and packaged approval |
| Workspace read | `tobkiri.service.file.inspect.v1` / `rumi_file_inspect_pack.file-inspect` | File inspection service resolves Host workspace binding and descriptor-relative no-follow reads | Existing implementation in `rumi_file_inspect_pack/runtime/inspect.py`; not replaced by clipboard work. Full normal-Pack route not newly certified here. | Full file provider capture/invocation on each OS |
| Workspace mutations | Existing shell/Git interactive-effect contracts; `workspace_mutation.py` / `resources.py` | Host-owned handles, workspace lease, CAS/batch journal, pinned root; underlying Host filesystem | Existing parent implementation and tests retained. Broker queue boundary strengthened. No replacement filesystem database or raw-path provider added. | Native reparse/root-swap tests and full normal-Pack vertical |
| User-selected resource | `host.file.read_user_selected` / `host.file.write_user_selected` are permission inventory entries | Requires authenticated OS selection → Host-issued resource handle → scope revalidation | Inventory/declaration is not proof of a connected selector/provider. This follow-up does not invent a raw-path substitute. | **Unresolved:** complete selection-to-handle/provider route and native evidence |
| File/URL/application open/reveal | Desktop/browser contracts; Launcher `open_external_url`; Mac `finder_reveal` implementation | Launcher process / OS application or browser, not a workspace-contained effect | Native implementations exist in separate paths. Launcher URL opening has panel-specific ACL and is not indiscriminately removed. Full formal route not certified. | All caller/IPC routes, exact target identity and native boundary |
| Notification | Shell metadata / application notification service | App notification records versus OS notification permission | App record management does not establish native notification delivery. No new notification provider added. | Native provider registration, OS grant, delivery evidence |
| Process/terminal | `tobkiri.service.shell.execute.v1`, separately prepared/executed operation | Existing constrained Host process runner; ordinary Pack remains isolated | Parent's signed interactive-effect route preserved. No general shell/AppleScript/FFI escape added. | Existing debug/CLI regressions, real constrained runner and PackVM proof |
| Browser/computer | Browser observe/control and desktop observe/control contracts | OS/browser/session state; native adapters vary | Browser Host Extension runner exists; desktop Pack is **normal_sandbox**, despite its name. Neither is promoted to Host Extension. | Browser legacy bool runner and other controller direct effects remain **outside this cutover**, with bypass risk requiring follow-up |

Clipboard adapter scope is deliberately macOS text only. Windows and Linux
return `unsupported_os`; missing/untrusted macOS utilities return
`provider_unavailable`; spawn access denial returns `os_permission_denied`.
Invocation authorization denial remains distinct from these native results.
Native subprocess errors cannot reliably identify every OS privacy denial and
are not relabeled as one. Availability reads executable/platform metadata only,
does not read clipboard, prompt, grant permission, or attest OS privacy consent.

## Retired entries and retained boundaries

- `rumi_clipboard_host_service_pack/runtime/runner.py` retains its importable
  signature but always returns `legacy_clipboard_execution_retired`. Even
  `viewer_host_approved=True` does not reach an executor.
- `BrowserComputerController._system_clipboard_read/write` deny directly.
  Its public clipboard actions use the captured contract projection; `yolo_mode`
  no longer turns those actions into raw clipboard access. No fallback occurs
  when the session/provider/Grant is missing.
- Existing media clipboard blocks no longer import removed processor stubs.
  The existing media/UI projection consumes actual canonical results instead
  of expecting another HostIntent and another approval.
- The old computer helper already fails closed without a captured V4 session.
  Its bool is not treated as a sufficient authorization proof. Its legacy
  metadata/session integration is **not** certified by this follow-up.
- Launcher approval, debug diagnostics, CLI approval and VM diagnostics are not
  removed. No client `approved`/flag or workspace configuration issues authority.
- Legacy browser runner and non-clipboard controller effects remain unreviewed
  for complete cutover. Their existence is disclosed, not marked migrated by a
  clipboard test or a baseline change.

## TCB before and after

| Trust component / process | Authority and why trusted | Compromise impact / OS enforcement | Change |
|---|---|---|---|
| Host Python interpreter, Authority store/kernel, canonical Broker | Own identities, Grants, leases, revocation and audit | Host-account resources; ordinary Pack isolation is a separate enforced domain | Existing TCB; final queue check moved into actual worker |
| Exact Host provider loader/backend | Imports verified Host Extension hook and pins contribution identity | Shares Host address space; Python namespaces/domain IDs alone are **not OS process isolation** | Existing limitation; new finite clipboard hook uses this substrate |
| Clipboard transport / Host | Revalidates dispatched lease, exact payload/caller/target/profile/domain, expiry, revocation, one-shot entry | Direct Host OS clipboard authority remains outside app lease enforcement if Host itself is compromised | New TCB code, no second permission DB/broker |
| Native clipboard adapter / Host plus OS utility child | Fixed root-owned, non-writable, no-symlink `/usr/bin/pbcopy` or `pbpaste`; no PATH, shell or arbitrary argv; bounded streams/deadline | Clipboard is global; child/Host account can exercise OS authority without Tobkiri's Grant if compromised. OS utilities and system libraries remain TCB | New mediated use, **not a TCB reduction** |
| Existing approval/presentation UI | Integrity of what the user approves | A compromised approval presentation could induce unwanted consent | Still integrity-critical; not declared outside TCB |
| Normal Pack / PackVM | Untrusted computation and finite contract request | Depends on authenticated bridge and OS sandbox; must not see Host filesystem, secrets or raw clipboard | No Host execution fallback or production gate relaxation |

The parent's dedicated-process metadata for the shared Host Python substrate
does not supply physical separation evidence. This remains a **Draft blocker**
for any stronger isolation claim. This change does not set new production gates
or reinterpret conformance-only tests as production proof.

## Validation distinctions and native procedure

The PR body records current commit/test results. New tests distinguish fake
Authority/native unit tests, real pipe I/O with a disposable test child, real
Authority/Broker tests, and native/packaged tests. A real pipe fixture is **not**
a real clipboard or real isolated Pack invocation.

This execution environment reports no `/proc/<pid>/stat` and process identity
`unknown`; the existing Authority process-ownership guard therefore fails
closed. Tests requiring that proof must run on a suitable runner. Do not patch
the guard, skip failing tests, or label fake-store tests as real integration.

Native acceptance must use a fresh dedicated macOS test account/session with no
user secrets or existing clipboard contents. Use a temporary workspace and an
installed, signature-verified non-built-in test Pack declaring the exact two
contracts above. Its Profile must request explicit caller→provider edges;
merely placing a Pack in a bundle does not create edges or Grants. The default
Profile's complete clipboard caller routing remains to be exercised.

1. Start the packaged Host and capture exact caller/target artifact and domain
   identities, build SHA, OS version and relevant OS access state.
2. Through the normal Pack's authenticated contract client, request write with
   `{"text":"tobkiri-native-fixture-<unique-id>"}`. Without its exact Grant,
   confirm no native invocation; approve through the parent's regular one-shot
   Authority flow, then confirm the actual OS clipboard and response.
3. Request read with `{}` under its separate Grant and compare exact UTF-8 text.
   A write Grant must not permit read. Clear uses another authorized write of
   empty text, only in this disposable test session.
4. Repeat denied/cancelled/expired/revoked leases, wrong caller/profile/domain,
   helper unavailable and OS-denied cases. After helper start, uncertain writes
   must return `ambiguous_effect` and must not auto-retry or fall back.
5. Exercise public controller/media projection, direct retired runner, old
   HTTP/IPC/helper/CLI entrypoints and non-built-in Pack update/revocation.
6. Run the same sequence against the packaged app and retain audit/receipt and
   native outcome evidence separately. Test A+B in an isolated worktree only
   once A's PR exists; do not claim an untested combination succeeded.

Diagnostic CLI examples remain `tobkiri approvals list`, `tobkiri approvals show
<request_id>` and `tobkiri approvals approve <request_id> --expected-digest
<snapshot_digest>` under the existing explicitly delegated debug session.
Debug qualification is not production user consent. There is no new command
which sets `approved=true` to exercise the native adapter.
