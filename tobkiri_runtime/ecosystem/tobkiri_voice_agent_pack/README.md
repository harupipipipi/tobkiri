# Tobkiri Voice Agent Pack source migration

This directory preserves the provider-neutral coordinator, declarative staging
UI, and speech protocol from PR #1493 without copying generated catalogs,
provenance, bundles, or migration receipts from its old stack.

**Installed conversation voice agent remains unavailable.** These source assets
are not registered in the pinned Profile. A bounded session controller is now
implemented and tested with injected public ports, but its admitted Host media
completion/cancellation adapter, frontend binding, and installed execution are
unfinished. The staging descriptor displays this state and only links to Host
Permissions. It does not grant Tobkiri or OS microphone permission or record.

Composer dictation is separate: it uses Defaults' existing in-memory recorder
and narrow transcription route, then requires editable transcript review and
explicit insertion. No second recorder, HTTP server, model store, or credential
configuration is introduced here.

The speech parser accepts at most 16,384 UTF-8 input bytes and at most six total
playback segments. Delays are bounded to 120 seconds, invalid/negative durations
become zero, and the first ASK/CONTINUE/FINISH directive ends the plan. Directives
are playback data; they cannot authorize a tool action.

Integration must resolve only published contracts from the admitted Profile:
`tobkiri.action.media.capture.v1`, `tobkiri.service.ai.audio.transcribe.v1`,
`tobkiri.service.ai.audio.speech.v1`, `tobkiri.action.media.output.v1`, and the
existing turn lifecycle. An absent/ambiguous provider must keep the session
unavailable. In particular, a media HostIntent is a request awaiting the normal
Host authority/broker path, not proof of completed capture or playback.

## Bounded session source

`voice/session.py` captures one Profile, activation digest, and conversation.
`voice/ports.py` defines the finite injected Host adapter ABI, documented in
`voice/session.ports.v1.json`. This metadata is a staged port declaration; it is
not a generated catalog, an admitted Host Function, or an installed provider.
The adapter must resolve exactly one compatible public v1 route per requirement
and retain the normal policy, approval, audit, and OS permission paths.

The session requests a 45-second capture through the existing media contract,
waits for an actual Host completion, and sends only an opaque artifact ID to
STT. A cumulative STT result replaces the separate editable transcript. Explicit
review confirmation dispatches it once through `tobkiri.action.turn.saved.v1`.
The saved conversation owner resolves the conversation model. Captured tool
discovery preferences pass through the public saved-turn validator; they grant
no authority. A waiting or lost result is recovered by an owner read, never by
re-sending the utterance.

`voice/receipts.py` requires the exact saved input digest, request/turn identity,
completed result reference, and owned complete assistant message before speech.
TTS and media output use their existing public contracts. Limits are four turns,
20 voice calls, six segments per reply, 16,384 UTF-8 text bytes, a 10-minute
session, and finite per-operation deadlines. Explicit user stop is available
after the voice-call budget is exhausted. CONTINUE cannot start an automatic
capture, model, or tool loop.

Cancel/interrupt invalidates the generation before requesting Host drain. Late
STT, saved results, and TTS cannot update the session or start output. Capture,
playback, and transport drain must be confirmed; active saved turns additionally
require authenticated `tobkiri.action.turn.stop.v1` confirmation. An unconfirmed
stop stays `cancelling` and blocks further work. Interruption does not rewrite
conversation history or create a synthetic silence message.

## Local reference adaptation

The user supplied `/Users/haru/Desktop/puroguramukei/voice` as a read-only design
reference. Its `voice_io.py`, `agent.py`, `brain.py`, `services.py`, and `server.py`
were inspected without running them or accessing recordings/conversation logs.
No license declaration was found. The implementation here is fresh code.

Adopted concepts: cumulative STT replacement, finite capture windows, bounded
speech directives, per-session generation binding, and cancellation of speech
and pending generation. The reference's optional decision model stays optional;
automatic turn-end and interrupt classification are not wired. Its speculative
reply flow is omitted because unconfirmed speech must not commit tools/history.
Direct hardware/calibration, provider HTTP requests, environment credentials,
global conversation state, debug audio/history, and local HTTP routes are omitted.

Run source acceptance without credentials or devices from `tobkiri_runtime/`:

```sh
PYTHONDONTWRITEBYTECODE=1 python -B -m pytest --noconftest \
  tests/test_tobkiri_voice_session.py tests/test_tobkiri_voice_agent_protocol.py -q
```

These tests invoke the actual controller with a simulated trusted adapter. They
prove source state transitions, not installed Pack/Host or actual microphone,
STT/TTS, approval, or playback acceptance. Those remain unverified.
