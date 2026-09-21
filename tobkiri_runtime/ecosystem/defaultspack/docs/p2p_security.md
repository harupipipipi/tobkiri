# P2P Security

P2P is treated as an optional ingress surface, not a trust boundary and not a
tool transport. The safe default is no P2P listener, no peer discovery, and no
internet relay.

## Defaults

- P2P is disabled by default.
- The local HTTP/runtime control plane binds to loopback by default
  (`127.0.0.1`/`localhost`/`::1`).
- There is no LAN discovery, multicast, mDNS, DHT, STUN/TURN relay, or hosted
  internet relay in defaultspack.
- Enabling any future peer intake must be explicit local configuration and must
  preserve the same local-admin route guards used by sensitive routes.

## Ingress Boundary

A peer message is equivalent to an external input event:

```text
peer payload
  -> normalized external event
  -> audience/input policy
  -> chat or agent message
  -> normal response/tool planning
```

The peer can add user-visible input. It cannot directly execute a tool, call a
handler, write a file, run a terminal command, push git, install a pack, change
settings, or create a local approval token.

If a peer asks Rumi to perform a sensitive action, that request becomes chat
context. The local runtime may then propose a tool call, but execution still
passes through local policy, risk classification, approval token binding, and
audit.

## Approval Rules

- Remote peers cannot approve local actions.
- Approval tokens are local, one-time, signed, and bound to operation plus
  argument hash.
- Changing the path, command, git target, content, destination, or other
  protected argument after approval invalidates execution.
- The local user's policy decision wins over peer metadata, peer identity, and
  model output.

## Data Handling

Peer identifiers and payload metadata should be logged only after redaction.
Secrets, bearer tokens, cookies, API keys, and provider credentials must never be
accepted from a peer as authority. If a credential is needed, Rumi should ask the
local user through the existing local secret and approval flows.

## Mobile Pairing Contracts

Rumi Mobile uses P2P pairing state to bootstrap scoped split mobile API tokens.
The recommended QR payload is `kind: "rumi_mobile_pair_v1"` with `pairingId`,
claim `code`, one-time `pickupSecret`, mobile-reachable `baseUrls`, role
metadata, and `expiresAt`.

- The phone claims a pending pairing with
  `POST /api/mobile/v1/pairings/{id}/claim`, including the pairing code,
  `device_id`, label, public key, and requested scopes. The PC operator must
  still approve.
- Client device tokens use the `dtk_` prefix and are accepted only on
  `/api/mobile/v1/...` routes whose contract declares the required client
  scope. Approver tokens are separate and accepted only on `/api/authority/*`
  request list/read/challenge/approve/deny routes. A mobile token must not
  authenticate PC/admin, pack, file, terminal, git, browser, or generic
  defaultspack routes.
- `GET /api/mobile/v1/pairings/{id}/status` reveals only public pairing state.
  Split device token pickup is a separate `POST
  /api/mobile/v1/pairings/{id}/token/pickup` JSON-body request containing the
  QR-only pickup secret and claimed `device_id`.
- PC approval UI reads claim details from the admin-only
  `GET /api/mobile/v1/pairings/{id}/review` route. That route is local-only,
  requires panel/local auth, and returns fingerprints plus a `claim_hash`
  rather than raw keys, pairing code, or pickup secrets. The approve call must
  echo that `claim_hash`; a changed claim is rejected before token issuance.
- Credential transfer is disabled by default behind
  `RUMI_MOBILE_CREDENTIAL_TRANSFER=1` until encrypted device-bound delivery is
  complete. Plaintext or wrapper-only payloads fail closed.
- Mobile PC controls are catalog-driven. The phone reads the paired PC's mobile
  capabilities response for selectable profiles, runtime model settings, and
  public slash command manifest entries; it must not ship a separate hard-coded
  list of PC commands. Command execution goes through
  `POST /api/mobile/v1/commands/execute`, which is a scoped mobile facade over
  the PC slash command registry and requires the route's mobile device scope.
- LAN HTTP is for trusted private-network development only. Mobile pairing base
  URLs must not advertise loopback hosts; Android release builds keep cleartext
  disabled, while debug/profile builds may permit it for LAN testing. Internet
  exposure should use HTTPS or an explicit reverse proxy design.

## Non-Goals

P2P is not a remote desktop protocol, a distributed tool bus, a remote approval
system, a LAN service discovery feature, or a replacement for provider-specific
webhook verification.
