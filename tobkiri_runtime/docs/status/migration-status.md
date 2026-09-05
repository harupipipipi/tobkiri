# Migration Status

Last updated: 2026-08-10

## `defaults` -> `defaultspack`

- Canonical prefix: `defaultspack.`
- Compatibility prefix: `defaults.`
- Protocol v4 Pack and executable catalogs are the only runtime function authority.
- Legacy alias vocabulary and its migration history are documented in `docs/compat-alias-migration.md`.

Status:
- Canonical naming exists across generated function manifests.
- Legacy `defaults.*` resolution is outside the v4 runtime boundary.
- v4 generation and integrity checks use canonical Function identities and real implementation hashes.
- The verified-unused `defaults.model_runtime.*` group is not a v4 Function identity.

## Handwritten API -> v4 contract routes

- Control-panel API routes are manifest-driven.
- Shared system GET routes are now declared in `core_runtime/core_pack/core_system_api/ecosystem.json`.
- `PackAPIHandler` exposes a finite, authenticated frontend contract map and
  PackVM lifecycle API.
- Direct Pack-specific legacy URLs do not become runtime authority and return a
  closed response outside the selected contract map.

Status:
- Production Pack operations are contract-selected and Broker-dispatched.
- Remaining verb handlers are transport adapters, authentication/bootstrap, or
  explicitly bounded lifecycle endpoints; they are not legacy execution fallback.

## HTTP block route -> function route

- The v4 executable catalog exposes canonical Function identities and operation bindings.
- Legacy HTTP route metadata is an offline migration surface, not a v4 authority.
- Strict integrity scanning checks the v4 Pack, contracts, artifact index, executable catalog, bundle lock, and implementation hashes.
- The chat-channel compatibility family is not a v4 executable identity.

Status:
- Production v4 routes resolve to one exact Function principal and backend.
- Unknown, stale, replayed, or unselected calls fail closed.
- Legacy HTTP metadata is accepted only by the offline projection generator and
  is unreachable from production dispatch.

## Implicit domain imports -> declared boundaries

- defaultspack domain import policy now lives in `ecosystem/defaultspack/domain_boundaries.yaml`.
- `scripts/quality/scan_defaultspack_boundaries.py` checks cross-domain imports against that policy.

Status:
- The declared boundary policy and zero-finding complete-v4 reachability gate are
  enforced in CI.
- Future policy narrowing is ordinary hardening, not unfinished v4 cutover work.
