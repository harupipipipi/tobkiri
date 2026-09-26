# Tobkiri Voice Agent Pack

This Pack stages the realtime voice conversation prototype for the Pack v4
Defaults profile. It contributes an edge rail entry, a model-neutral coordinator
prompt, and the bounded speech directive parser. The active Tobkiri conversation
chooses its model; this Pack does not pin a provider, model, or credential.

The coordinator is intended to answer briefly and delegate substantial work
through the existing `subagent` tool. `voice/coordinator.json` stages that tool
binding without granting tool authority. A separate Jev decision model may
later judge barge-in and turn completion. It is not required by this Pack.

**Current status: the voice session is not runnable in Tobkiri yet.** The rail
entry exposes the staged Pack, but capture, transcription, speech output,
session lifecycle, coordinator/tool dispatch, and settings bindings are pending. The source prototype lives
outside this repository and must not be started as a second HTTP server or given
its own model configuration inside Defaults.

The protocol code contains no network calls or secret handling. Voice transport
will use Tobkiri's approved microphone and AI routes when wired later.
