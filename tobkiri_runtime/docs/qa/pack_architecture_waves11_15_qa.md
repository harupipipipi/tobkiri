# Pack Architecture Waves 11–15 QA

## Required automated evidence

- Backend:
  `test_defaultspack_command_protocol.py`,
  `test_defaultspack_command_legacy_cutover.py`,
  `test_defaultspack_command_protocol_stream.py`,
  `test_defaultspack_invocation_events.py`, and
  `test_defaultspack_offline_queue.py`.
- Distribution:
  `test_pack_sdk.py`, `test_pack_signature.py`, and the Wave 7 activation
  regression cluster.
- Contract gates:
  `scripts/quality/scan_command_protocol.py`,
  `scripts/tobkiri_pack.py generate generated/pack_sdk --check`, and
  `scripts/quality/scan_pack_architecture.py`.
- Clients:
  defaultspack `npm test`, `npm run lint`, and `npm run build`; Flutter analyze
  and tests for the mobile API surface when the Flutter SDK is available.

## Security scenarios

| Scenario | Expected result |
| --- | --- |
| Client supplies an approval boolean | No authority is granted |
| Approval token is replayed | `APPROVAL_TOKEN_USED` |
| Invocation ID is reused with different input | `INVOCATION_CONFLICT` |
| Offline request is a toggle or host action | Queue rejection |
| Offline request contains a secret-shaped field | Queue rejection |
| SSE reconnects with `Last-Event-ID` | Only later ordered events are emitted |
| Progress payload contains a bearer/API key | Value is redacted |
| Pack file changes or an extra file appears | Signature verification fails |
| Pack contains a symlink | Signing or verification fails |
| Signing key is revoked | Activation fails closed |
| Publisher trust store is inside the Pack | Activation fails closed |

## Manual acceptance

1. Open the command palette and exercise each visible command family.
2. Confirm high-risk commands stop at an approval request before doing work.
3. Approve once, resume, and confirm a second resume is rejected.
4. Disconnect, queue an explicit desired-state mutation, reconnect, and verify
   replay or an explicit revision conflict.
5. Reconnect to an invocation event stream and verify it resumes after the last
   rendered sequence without duplicate terminal UI.
