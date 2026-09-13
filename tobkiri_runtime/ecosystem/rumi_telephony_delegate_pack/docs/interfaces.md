# Interfaces

## Inputs

- Local artifacts supplied by the user or by an adjacent owner pack.
- Schema-bound records listed in `ecosystem.json`.
- Evidence IDs, source spans, and explicit uncertainty notes.
- Phone target aliases only; raw phone numbers belong in an external owner system.

## Outputs

- Evidence-linked drafts.
- Review checklist results.
- Handoff packets with owner pack, reason, and artifact path.
- Blocked packets for never-call matches, missing approval, declined consent, disallowed intent, or takeover-required states.

## Call Handoff Contract

- Handoff packets are not calls and must not be represented as completed dialing.
- A provider handoff requires approved number alias, approved script ID, consent disclosure review, never-call evidence, and explicit human approval.
- Transcript handoffs require reviewed redaction records for configured PII classes.

## Handoff Owners

- `defaultspack`: Owns provider and tooling handoffs for dialing, ASR/TTS-adjacent runtime, transcript/media handling, and contact or calendar lookup outside this pack.
- `rumi_meeting_intelligence_pack`: Can consume reviewed, redacted transcript packets for meeting recap and follow-up artifacts.
- `rumi_operations_company_pack`: Owns approval-aware escalation, real-world action risk review, and downstream business workflow execution after human approval.

## Required Secrets

None.

## Does Not Provide

- actual dialing
- ASR/TTS runtime
- contact lookup
- calendar mutation
- payment or purchase execution
- external connector writes
- emergency services
