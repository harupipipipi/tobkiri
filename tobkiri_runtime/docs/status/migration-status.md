# Migration Status

Last updated: 2026-07-10

## `defaults` -> `defaultspack`

- Canonical prefix: `defaultspack.`
- Compatibility prefix: `defaults.`
- Compatibility aliases are tracked in `ecosystem/defaultspack/compat_aliases.yaml`.
- Canonical replacements and the inventory/warning/enforcement/removal process are documented in `docs/compat-alias-migration.md`.

Status:
- Canonical naming exists across generated function manifests.
- Actual `defaults.*` resolution emits privacy-safe local audit telemetry, with structured warnings for non-internal callers.
- Generation and integrity checks require the explicit allowlist and a migration note; new functions do not receive `defaults.*` aliases automatically.
- The verified-unused `defaults.model_runtime.*` group has been removed while canonical `defaultspack.*` aliases remain.

## Handwritten API -> `api_routes`

- Control-panel API routes are manifest-driven.
- Shared system GET routes are now declared in `core_runtime/core_pack/core_system_api/ecosystem.json`.
- `PackAPIHandler.do_GET()` now relies on route dispatch for core system routes before pack-route fallback.
- Remaining verb-handler and mixin families are inventoried in `docs/status/handwritten-route-inventory.md`.

Status:
- Major route families are already table-driven.
- Some transport-specific and migration-specific branches still remain in verb handlers.

## HTTP block route -> function route

- defaultspack transport route specs now expose canonical `function_id` / `legacy_block_module` metadata.
- Legacy HTTP fallbacks are tracked in `ecosystem/defaultspack/docs/legacy_http_routes.yaml`.
- Integrity scanning now checks function artifacts and legacy fallback allowlisting together.
- Allowlist metadata now resolves auth mode, principal, CSRF/origin, rate-limit, audit category, replacement `function_id`, and `legacy_until`; the security CI job rejects missing metadata.
- The chat-channel family has moved from compatibility block fallback to manifest-declared direct function dispatch.

Status:
- Many block-backed routes can now resolve through a function boundary first.
- Some routes remain explicit legacy fallbacks until replacement functions exist.

## Implicit domain imports -> declared boundaries

- defaultspack domain import policy now lives in `ecosystem/defaultspack/domain_boundaries.yaml`.
- `scripts/quality/scan_defaultspack_boundaries.py` checks cross-domain imports against that policy.

Status:
- Baseline policy is captured and enforced.
- Tightening the policy is a follow-up migration, not finished work.
