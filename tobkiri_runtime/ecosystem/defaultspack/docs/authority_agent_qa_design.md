# Authority Agent QA Design

Authority and computer-use flows should be testable by LLM/agent QA without
weakening production safety. This design extends
[safety_permission_audit_design.md](safety_permission_audit_design.md) and
[browser_computer_use_optional_design.md](browser_computer_use_optional_design.md):
approval remains explicit, scoped, one-time, and audited.

## Regression Targets

Observed Atlas/Gemma4 runs exposed these cases that the harness should make
easy to reproduce:

- A fresh run approved model/API access, then stalled at
  `authority_approval_required`. Manual API approval without the hidden
  Authority resume left the chat stuck. Authority requests and resume state must
  be visible as first-class pending work, separate from provider/API approval.
- After hidden Authority resume, the model/API call did continue and Gemma4
  started toward the task, but the next tool was `job_resume` and it required
  normal runtime approval (`apr_a4447601eded4846a8dbe4c530d1e9d2`). Approving it
  and sending `approval_followup` still did not reach `computer_use`; the turn
  ended as `paused_progress_loop` after progress-only messages.
- An older long-history run hit a Cerebras `429` token quota. QA should prefer
  clean-run isolation, visible context age, provider quota state, and token
  budget guards before an agent burns a large history on a debug attempt.
- A computer-use approval compared nested payload arguments with flattened
  execution arguments and rejected with `APPROVAL_ARGUMENTS_CHANGED`. Approval
  requests and execution must use the same canonical argument form, with a
  redacted normalized diff when the stable hash changes.
- Target-window drift is hard to diagnose after the fact. The debug view should
  show selected app/window, foreground app/window, target window, driver/seat,
  latest screenshot, and latest input event with timestamps.

## Resume Replay Helper

The QA harness should provide an atomic, auditable helper/API to settle an
Authority request and resume the original interrupted turn. It should not
require an LLM to choose or reason about a resume pseudo-tool. Resume replay
status and failure reason should be exposed separately from provider approval
and computer-use approval.

## Test Authority Mode

The test harness may provide an explicit test authority mode, but it must fail
closed outside tests.

- Opt in only through a test profile, harness flag, or test-only environment
  variable. Packaged production builds must reject it.
- Grants are ephemeral and scoped to operation, canonical arguments, target
  window/session, request id, and expiry.
- Test approvals still use the production verifier path where practical, write
  audit records, and mark transcripts/audit as `authority_mode:test` or
  `audit:test_authority`.
- Fixtures must cover approve, deny, timeout, duplicate approval, request
  expiry, concurrent/racing approvals, argument mutation, and foreground-window
  drift.
- There is no global YOLO mode, no production bypass, no reusable token, no
  hidden test authority, and no auto-grant for secrets or broad local execution.

## Agent-Friendly Debug Surface

The UI or run artifact should show enough state for an agent to debug approval
and computer-use failures without guessing:

- Authority request id, operation, risk, policy decision, expiry, approval
  state, resume state, and failure code.
- Provider/model, run isolation status, context age, token estimate, token
  budget, and quota/429 state.
- Submitted arguments, canonical arguments, execution arguments, stable
  argument hash, and redacted argument-change diff.
- Selected app/window, foreground app/window, intended target window,
  driver/seat, latest screenshot, latest input event, and target mismatch
  reason.
- Timeline entries for ask, approve, deny, timeout, resume, tool execution,
  tool result, and audit write.

## Issue 555 Debug Notes

The Atlas/Gemma4 QA run showed that agent debugging gets much easier when the
environment exposes the exact runtime state instead of only final chat text.
The most useful setup was:

- Latest UI repro: Atlas/Gemma4 stayed at
  `cerebras/gemma-4-31b が思考中` for more than two minutes with no pending
  Authority request for the new conversation, no provider trace, and no
  computer-use/tool log. On a clean standalone port `8785`, SIGUSR1
  `faulthandler` showed the stream thread still before any Cerebras API call or
  `computer_use`, in `prepare_chat_run -> route_model_request ->
  get_model_capabilities -> build_profile_catalog -> get_provider_catalog`,
  then provider OAuth status, client config, and connection-registry JSON
  loads. An earlier sample showed high CPU in `scandir`/`open`/`stat` during
  the same phase; aborting the browser client then produced
  `BodyStreamBuffer was aborted` and `BrokenPipe` in `transport/http.py`.
- After fixing model/profile catalog OAuth metadata and cache-key recursion, a
  fresh server on port `8784` with local UI approval credentials could approve
  model/API access through the signed UI-operator path. The previous recursive
  provider-credential directory cache-key scan caused conversation API
  timeouts/high CPU; the fix uses a bounded provider-key marker instead.
- In that fresh run, Gemma4 (`cerebras/gemma-4-31b`) reached tool calls and
  `browser.open_url` successfully opened ChatGPT Atlas to `google.com`.
  Runtime approvals for `browser.open_url`, `computer.observe`, and
  `computer.screenshot` could be approved and resumed, so approval plumbing
  worked.
- The end-to-end goal still did not complete. Gemma4 looped on
  observe/screenshot at the Google page, with the search box focused, and did
  not progress to typing `youtube`, opening YouTube, or playing a video under
  the intentionally vague prompt. This points remaining work toward
  model/tool feedback or action selection for vision computer-use, not Cerebras
  API format, model catalog, or approval plumbing.
- Start defaultspack with `RUMI_DEFAULTSPACK_PROVIDER_TRACE=full` so each run
  writes the actual provider input chain, including synthetic tool calls/results
  created after approval replay.
- Use the debug harness pieces that made this visible: isolated port,
  local UI approval credentials, SIGUSR1 stack dumps, per-chat isolated
  conversations/tags, and explicit checks of pending Authority requests,
  provider traces, and tool logs.
- Use a clean, isolated conversation for each provider attempt. Long inherited
  histories hide the current failure and can burn Cerebras quota before the
  computer-use step is reached.
- Keep approval-followup messages hidden from the model but visible in run
  metadata, with request id, permission id, operation, canonical arguments, and
  replay status.
- Record both the originally requested browser/app alias and the resolved
  target app/window. In this scenario `Google Chrome` style model aliases had
  to resolve to the requested Atlas target so the model could use ordinary
  browser vocabulary without drifting to another app.
- Preserve the latest screenshot, foreground window, target window, and the
  previous computer-use result in the provider-visible chain. Without this, a
  model can open YouTube and then repeat `open_url` because it cannot observe
  the browser state it just created.
- Surface `429`/quota details alongside prompt size and request count before a
  rerun. This makes it clear whether the failure is model planning, transport
  shape, approval replay, or provider rate limiting.

### Codex/Subagent Debug Env Note

For live computer-use QA, Codex and subagents would have isolated failures
faster with a broker-wired standalone defaultspack port instead of the
Viewer-integrated `8765` chat path, which can hang before a provider call or
tool log is created. The missing debug environment for issue555 was:

- A one-command harness that starts defaultspack and the Rumi Viewer host broker
  together, wires `RUMI_USER_DATA`, `RUMI_VIEWER_HOST_BROKER_CONNECTION`,
  isolated provider-credential/secret storage, local UI approval credentials,
  and `RUMI_DEFAULTSPACK_PROVIDER_TRACE=full`, then blocks model requests until
  `/api/desktop-system-info` proves `source: viewer_broker`, `reliable: true`,
  and `host_broker.available: true`.
- The harness should log and write an artifact with the actual bound localhost
  URL/port after bind, plus the live chat/provider/tool timeline URL. Agents
  should not have to guess or rely on a hardcoded `8766` when the server chooses
  another free port.
- A localhost monitor policy, or one-click UI URL artifact, that lets Codex and
  subagents watch the live chat/provider/tool timeline in a browser during the
  run.
- Truly isolated QA chat, user-data, provider-credential, and secret
  directories. `RUMI_USER_DATA` alone is not sufficient if defaultspack still
  points at a shared chat database or shared provider credential cache.
- Approval-token fixtures should mint scoped synthetic credentials for the
  isolated run and expose only redacted identifiers, hashes, expiry, scope, and
  replay state in logs/artifacts. They must never print reusable local tokens,
  bootstrap secrets, provider API keys, or raw bearer values.
- Target-app launch evidence for Atlas: requested alias, resolved bundle id,
  exact `open` command, active foreground app/window, and whether Atlas has a
  usable window or is only running without a window.
- Target-app dry-runs must show the targeted macOS launch plan. A request for
  Atlas should show `open -b com.openai.atlas ...`, not a stale managed Chrome
  profile plan, otherwise agents debug the wrong browser path.
- Atlas resolution must map to bundle id `com.openai.atlas`, not
  `com.openai.atlas.web`.
- The harness should fail early when free disk space is too low for Viewer,
  WebKit, screenshots, and edge-haze lease files. In the issue555 repro, both
  Viewer startup and Computer Use screenshots failed once available space fell
  below a few hundred MiB.
- Edge-haze is only an operator visibility aid. Disk or lease-write failures
  must be recorded as debug metadata, not allowed to fail an otherwise
  successful `browser.open_url`, key, click, or type action. A scoped
  `RUMI_EDGE_HAZE_DISABLED=1` debug mode is useful for low-disk smoke tests.
- A clearly labeled local-controller smoke mode (`RUMI_COMPUTER_HOST_INTERNAL=1`)
  helps distinguish Viewer-broker failures from core targeting/input failures.
  It must stay debug-only because the production path should keep Viewer
  approval, audit, and host-permission boundaries.
- Approval replay state should distinguish fresh, consumed, invalid, expired,
  and replayed approvals at the point where the original tool resumes.
- Runtime approval should be surfaced consistently in both stream events and
  persisted assistant metadata (`pending_approval`), with a broker-dispatch
  marker so QA can tell whether an approved `computer.context`/screenshot/click
  reached the Viewer host broker.
- Browser/computer-use evidence should include the requested tool, canonical
  arguments, approved arguments, target app/bundle id, broker dispatch id,
  host-side receipt, resulting foreground app/window, and screenshot or input
  event timestamp. This lets Codex tell whether failure happened in approval,
  broker dispatch, target resolution, app focus, or model action selection.
- Stack-dump diagnostics should be an explicit HTTP/debug endpoint or a
  guaranteed faulthandler setup; ad hoc `SIGUSR1` is not safe when a standalone
  server was not started with that signal handler.
- A Cerebras Gemma4 runner should be rate-limit-aware: show 429/quota state
  before reruns, apply backoff, and avoid burning quota on stale long-history
  retries.
- The `smoke-computer-use` and supervised `viewer-smoke-computer-use` harnesses
  pace direct `cerebras/gemma-4-31b` streams by at least 35 seconds by default.
  Each request can carry about 13.8k tokens, while the provider allowance is
  30k tokens/minute and 5 requests/minute; 35 seconds provides deterministic
  headroom for the token limit as well as the request limit. Approval-only API
  calls do not consume a model-call slot, so the wait is applied immediately
  before the next real stream rather than around approval polling or decisions.
  `--min-stream-interval-seconds` can override the interval for controlled QA.
  Pacing logs contain only the model name, turn, interval, and rounded wait—not
  prompts, approval payloads, credentials, or chat content. A typical 7–8 turn
  acceptance run therefore has 6–7 paced gaps (210–245 seconds) plus provider,
  tool, and approval latency, and should be expected to take roughly 4–5
  minutes rather than being retried as a stalled or duplicated chat.
- Provider/model catalog construction should use one extension-registry snapshot
  for the run. Re-reading or rebuilding the registry during profile/catalog
  setup can cause pre-provider stalls before any API request or tool log exists.
- Model selection should expose provider routing as first-class state, not only
  model thinking/select-run settings. QA needs to distinguish the outer access
  path and the upstream model provider, for example
  `openrouter/cerebras/gemma-4`, `aigateway/cerebras/gemma-4`, or
  `aigateway/google/gemma-4`, with per-layer provider options, base URL,
  allowed models, quota labels, and request-shape quirks visible in the run
  metadata. Otherwise a Gateway-backed Gemma run can be mislabeled as direct
  Cerebras, making approval, quota, and tool-format debugging ambiguous.
- One-shot Authority scope can interrupt again after each runtime tool replay.
  In the latest Atlas/Gemma4 run, switching the approval fixture to
  conversation scope let the sequence continue far enough to expose the next
  model/action failure.
- Latest Atlas/Gemma4 behavior: Gemma4 opened Google in Atlas and typed
  `youtube`, but then reopened `google.com` instead of pressing Enter or
  opening YouTube. This points to missing next-action guidance after text input,
  not to browser launch or text-entry failure.
- A follow-up live run proved the hidden Authority resume path can now return
  to provider tool calls: after approval, Gemma4 called `browser.open_url`,
  `computer.screenshot`, `computer.type`, and `computer.key`. The remaining
  failure was action-content selection, not approval: Gemma4 typed the current
  URL and later the app name instead of the user-requested search term. Tool
  schema and tool results should say explicitly that `computer.type.text` is
  the literal user-requested URL, query, or form text, not the current URL, app
  name, or window title unless the user asked for that exact text.
- The broker-backed debug harness exposed the next visibility failure:
  defaultspack launched outside Viewer lacked `RUMI_VIEWER_HOST_BROKER_CONNECTION`,
  so no Viewer broker dispatch or edge-haze lease could occur. Viewer-launched
  Defaultspack must pass that env var, and agent QA should fail fast when it is
  missing.
- After wiring the broker env, Gemma4 reached Viewer host broker audit entries.
  `computer.type` arrived as an approved broker action, but with
  `background: true` and `result_ok: false`; no edge-haze lease was created.
  QA should distinguish "not dispatched to Viewer" from "dispatched, but hidden
  background route failed or produced no visible haze".
- Gemma4 also naturally used `app: Atlas`; Viewer broker audit showed
  `computer.show_app` with `app: Atlas` and `result_ok: false`. Browser/computer
  app aliasing should normalize `Atlas`/`OpenAI Atlas` to the installed
  `ChatGPT Atlas` app before dispatch.
- The new `rumi_viewer/scripts/defaultspack_debug.py` harness should be the
  default path for agent smoke tests. It launches defaultspack with the Viewer
  broker connection, edge-haze enabled, provider trace enabled, faulthandler on,
  isolated chat/user-data directories, and a debug foreground preference so
  `computer.type`/`computer.key` are visible instead of silently falling into a
  failing background route.
- `defaultspack_debug.py status` should be enough for an agent to continue a
  run: it reports broker health, defaultspack health, the chat URL, edge-haze
  lease state, and the latest pending approval with sensitive values redacted.
  Agents should not have to scrape minified UI state or guess the exact
  approval replay payload from `/api/coding/approvals`.

Viewer broker-backed `computer.screenshot` and `computer.observe` results should
be normalized exactly like local results, including recommended text-input
actions; otherwise models can loop on screenshot/open_url even after the target
page is visible. Approval-followup and tool replay logs should also expose
redacted structured summaries of action, URL, and typed text, so agents can
decide what to approve or replay without exposing sensitive values in
transcripts.

### Agent Smoke Runner Workflow

Run the broker-backed smoke from the repository root in a real foreground PTY.
This one command supervises the normal Viewer dev process, waits for its host
broker on `127.0.0.1:8770`, launches isolated defaultspack through the existing
debug launch path, runs the bounded smoke, and stops only the processes it
started when the run finishes:

```console
python rumi_viewer/scripts/defaultspack_debug.py viewer-smoke-computer-use --max-turns 12
```

The Viewer must remain attached to a live PTY. Do not run this through `nohup`,
a detached shell, or an agent background process: in issue #555 those launch
forms reproducibly panicked Wry WKWebView at `wkwebview/mod.rs:1349`, while
`cargo tauri dev` with a live terminal and GUI context worked. The supervisor
rejects a non-PTY invocation, writes a redacted Viewer log, and reports the
specific Wry panic if it appears. It also reports a stale or malformed broker
connection file instead of silently trusting it. Use `--keep-running` only
when the attached Viewer and isolated defaultspack should stay up for manual
inspection after the smoke.

The supervised Viewer debug build uses a 4 GiB (`4096` MiB) free-space
preflight by default. The observed Rust target is roughly 3 GiB, leaving about
1 GiB for WebKit, screenshots, logs, and edge-haze artifacts. The existing
build preflight still fails hard below the selected threshold; this does not
disable the guard. For a verified environment, pass
`--viewer-min-free-mb <MB>` to `viewer-smoke-computer-use` to set that command's
threshold explicitly.

The run creates a redacted supervisor event log and Viewer log under
`.tmp/rumi-viewer-defaultspack-debug/viewer-smoke-*/`, while the existing
launch artifact identifies the isolated defaultspack run directory, server
log, chat store, token-file paths (not token values), provider traces, and
chat URL. Terminal output and artifacts must never contain reusable tokens,
API keys, or typed text.

The harness is a process supervisor and diagnostic surface only. Per #1084,
native host behavior, broker dispatch, tool semantics, approval enforcement,
and Authority policy remain in their native/runtime boundaries; this command
uses the existing UI approval APIs and does not bypass them.

For a separately managed Viewer, launch isolated defaultspack and inspect it
before spending Cerebras quota:

```console
python rumi_viewer/scripts/defaultspack_debug.py launch
python rumi_viewer/scripts/defaultspack_debug.py status
```

`status` must report both the Viewer broker and defaultspack as healthy. It also
reports the actual port and chat URL, edge-haze lease state, latest isolated run,
and any pending approval with sensitive values redacted. If the default port is
occupied, pass the same `--port PORT` to `launch`, `status`, and the smoke
command. Do not use `--allow-no-broker` for the computer-use acceptance run; it
is only useful for separating a core local-controller failure from a Viewer
broker failure.

Run the built-in acceptance prompt with a bounded number of model/resume turns:

```console
python rumi_viewer/scripts/defaultspack_debug.py smoke-computer-use --max-turns 12
```

An ordinary positional prompt can replace the built-in one; it does not require
a JSON request file or a special prompt flag:

```console
python rumi_viewer/scripts/defaultspack_debug.py smoke-computer-use --max-turns 12 \
  "Open Google, type youtube in the search box, go to youtube.com, and start playing a video."
```

The runner creates a new isolated chat for each invocation, selects Cerebras
`gemma-4-31b`, and streams turns until the task finishes or `--max-turns` is
reached. The limit includes initial and approval-resume turns, so raise it only
after checking for a planning loop or provider `429`; do not reuse a stale chat
to get more turns.

The smoke runner deliberately follows the same secure paths as the UI:

1. It reads pending Authority work from `/api/authority/requests`, obtains a
   signed UI-operator context through `/api/authority/browser-ui-operator`, and
   posts the decision to `/api/authority/requests/{request_id}/approve`.
2. It resumes the same conversation with hidden `authority_followup` and
   `chat_display.reason: authority_followup` metadata. The approval returned by
   Authority is supplied only to the runtime resume request.
3. It reads runtime browser/computer/coding work from
   `/api/coding/approvals`, posts each decision to
   `/api/coding/approvals/approve`, and resumes the same conversation with the
   UI-compatible `approval_followup` metadata.

These are approval and replay operations, not an approval bypass. The runner
does not set or forge client-side `approved` flags, and the original operation
still passes Authority, local guard, workspace jail, capability trust, the
Viewer broker, and audit. Request ids, tool names, operations, and bounded
argument summaries may appear in compact events; API keys, local bearer values,
browser approval credentials, returned approval tokens, and text-entry content
must not.

Automatic decisions are limited to pending requests belonging to the newly
created smoke conversation. Runtime decisions accept only browser/computer
tools and the bounded `job_resume` replay path; an unexpected tool is left
pending and the runner stops. Provider Authority decisions are limited to
`model.invoke`, `api_key.use`, and `network.egress`; a host Authority decision
must be one of the explicit screen capture, accessibility read/mutate,
pointer/keyboard input, URL-open, or app-launch permissions needed by this
computer-use smoke. Requests requiring typed confirmation must be completed in
the UI and are never synthesized by the runner.

`launch` stores reusable debug credentials in mode-`0600` token files under the
isolated run directory and records only their paths/presence in
`.tmp/rumi-viewer-defaultspack-debug/latest.json`. `smoke-computer-use` reads the
server URL and those token-file paths from that artifact. Do not print, `cat`,
copy into a shell argument, or attach the token files. Terminal output is a
compact redacted event stream followed by the chat URL/id and evidence paths;
use those printed locations instead of reconstructing paths or reading secrets.

For a successful acceptance run, preserve evidence for this exact sequence:

1. Google is open in the intended Atlas window, with the resolved app/window
   and resulting URL or screenshot recorded.
2. A successful `computer.type` action enters `youtube` in Google's focused
   search field, followed by a screenshot or result that proves the input took
   effect. The terminal event remains redacted even though this fixed QA string
   is harmless.
3. Navigation reaches a URL whose host is `youtube.com`, and the resulting page
   is visible in the same intended target window.
4. A video is selected and playback starts. Prefer two time-separated
   screenshots showing that the playhead advanced, or equivalent player-state
   evidence; a model message claiming success is not sufficient.

Also retain the redacted Authority and runtime approval/resume events, Viewer
broker dispatch/result state, foreground and target window state, edge-haze
lease timestamps, and any screenshot/artifact paths. The final runner summary
and `status` output identify the chat URL, isolated `chat_store`, run log, and
run directory. Per-chat `history.json` and `provider_traces/*.json` live below
the isolated store at `chat/conversations/{conversation_id}/history.json` and
`chat/conversations/{conversation_id}/workspace/provider_traces/*.json`;
provider traces are enabled by `launch` for diagnosing request shape, quota, or
replay failures.

After an approved tool completes, the provider may end a turn with
`ai_error_after_tool_use`. The smoke runner starts a new explicit continuation
turn only when the final event/metadata marks `transient_ai_error` or its
sanitized error matches a timeout, queue/capacity, or temporary provider
condition. It continues from the current visually verified state, inspects
before acting, and does not replay the prior request or completed tool action.
Normal stream pacing applies to this model call. Recovery is bounded to two
turns by default and can be changed with `--max-transient-resumes`; zero disables
it. Authentication, malformed/wrong-format, and other non-transient errors are
never recovered. Logs contain only `count` and `reason_class`, not provider error
text.

Do not run the live smoke in unit-test or pre-submit automation because it uses
Cerebras quota and controls the desktop. Verify the runner itself without a
provider call:

```console
python -m py_compile rumi_viewer/scripts/defaultspack_debug.py
python -m pytest rumi_viewer/tests/test_defaultspack_debug.py -q
python rumi_viewer/scripts/defaultspack_debug.py smoke-computer-use --help
```

## Autonomy Modes

Default QA should preserve agent autonomy. In rough prompt mode, the harness
only observes and approves or denies according to its fixture policy. It should
not provide coordinate hints, manual navigation, or hidden app guidance.

Diagnosis mode is a separate, visible switch. It may expose screenshot
annotations, coordinate hints, target-window drilldown, and manual navigation
notes, but the transcript should record that the run changed modes so the result
is not confused with an autonomous agent run.

## Acceptance Checks

- Provider/API approval cannot accidentally satisfy an Authority request.
- Hidden Authority resume state is visible and testable.
- Authority approval plus resume replay reaches the original pending tool, or
  fails with a structured diagnostic, instead of ending in a progress loop.
- Approval canonicalization is shared between request creation and execution.
- `APPROVAL_ARGUMENTS_CHANGED` includes a redacted normalized diff.
- Target-window drift is visible before and after computer-use actions.
- Clean-run isolation and token-budget guards are visible before long-history
  provider calls.
- Test authority mode is noisy, scoped, audited, and impossible to enable as a
  production bypass.
