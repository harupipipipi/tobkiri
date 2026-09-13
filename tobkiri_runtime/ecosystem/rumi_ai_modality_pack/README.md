# Rumi AI Modality Pack

This pack owns provider-neutral embedding, image, transcription, and speech
gateway contracts. Each operation invokes one explicitly selected provider
contract and validates the global result shape. It does not import providers,
read catalogs or credentials, or treat an absent provider as an empty result.

Validation was not executed by the implementation agent. Independent testing
is required before merge, including missing/ambiguous providers, malformed
vectors and artifacts, transcription segments, credential scope, pack removal,
and independently replaced modality providers.

