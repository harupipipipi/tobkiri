# Rumi Media Analysis Adapter Pack

This adapter connects immutable workspace media references to replaceable image
analysis and transcription contracts. It first resolves metadata through the
read-only media inspection contract, then calls `rumi_ai_modality_pack`.

It owns no capture device, provider, credential, file reader, model catalog, or
storage. Inline media bytes and approval material are rejected. Missing modality
providers return `unavailable` rather than a fabricated empty result.

Validation was not executed by the implementation agent.
Independent testing is required before merge.

