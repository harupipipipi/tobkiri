# Architecture

Voice wake and camera pinch triggers produce an `AmbientTriggerEvent`.

```text
voice / gesture
  -> AmbientTriggerEvent
  -> Rumi permission check
  -> debounce / cooldown
  -> ambient_trigger_router
  -> RumiInputEnvelope
  -> submit_input / dispatch_input
```

The router enforces `ambient_monitor.enabled`; mic and camera events are ignored
while monitoring is off.

Camera pinch is a hold-to-record gesture: thumb/index contact emits
`record_audio_start`, records microphone audio in memory only, and the release
stops capture and opens a local review. Only an explicit Send from that review
emits `dispatch_audio` with ephemeral audio or the reviewed transcript alone.
Raw audio is not persisted to workspace attachments or ambient audit logs.
