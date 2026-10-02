# Tobkiri Voice Agent Pack source migration

This directory preserves the provider-neutral coordinator, declarative staging
UI, and speech protocol from PR #1493 without copying generated catalogs,
provenance, bundles, or migration receipts from its old stack.

**Conversation voice agent remains unavailable.** These source assets are not
registered in the pinned Profile. Capture, STT, TTS, session lifecycle, approved
tool dispatch, and settings bindings have not been validated together. The
staging descriptor displays this state and only links to Host Permissions.
It does not grant Tobkiri or OS microphone permission or begin recording.

Composer dictation is separate: it uses Defaults' existing in-memory recorder
and narrow transcription route, then requires editable transcript review and
explicit insertion. No second recorder, HTTP server, model store, or credential
configuration is introduced here.

The speech parser accepts at most 16,384 input characters and at most six total
playback segments. Delays are bounded to 120 seconds, invalid/negative durations
become zero, and the first ASK/CONTINUE/FINISH directive ends the plan. Directives
are playback data; they cannot authorize a tool action.

Integration must resolve only published contracts from the admitted Profile:
`tobkiri.action.media.capture.v1`, `tobkiri.service.ai.audio.transcribe.v1`,
`tobkiri.service.ai.audio.speech.v1`, `tobkiri.action.media.output.v1`, and the
existing turn lifecycle. An absent/ambiguous provider must keep the session
unavailable. In particular, a media HostIntent is a request awaiting the normal
Host authority/broker path, not proof of completed capture or playback.
