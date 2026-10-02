# Rumi AI Tool Bridge Pack

This pure bridge converts provider tool-call payloads into stable operation
descriptors. Every descriptor explicitly has no authority, approval, or
execution. Wave 6 tool policy and authority contracts decide whether and how an
intent may proceed.

Validation was not executed by the implementation agent. Independent testing
is required before merge, including malformed JSON, invalid names, replay IDs,
human handoff intents, stream intents, and proof that no tool runs.

