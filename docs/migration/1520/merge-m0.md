# M0: preserve the complete legacy source union

This merge preserves `legacy/signed-gateway` (`cd7c20e9d52535674c36cccac4d03ef70e9f809b`) and `legacy/pr-1519` (`6eb9704e573135771d85e160be6d3bfdd8fb9701`) on `codex/issue-1520-rust-core`. The five frozen legacy heads remain reachable; no original checkout, PR, native runtime, stored user data or execution authority was switched. This is the M0 source baseline, not completion of Issue #1520.

## Provenance and scope

| Tag | Commit | Relationship before M0 |
|---|---|---|
| legacy/pr-1322 | 273aed66b50190b640c6b93e03cea9519b21b7f1 | Ancestor of both parents |
| legacy/pr-1516 | 526f08c83f2555ad39d652d684fbc9405ca07fc5 | Common ancestor; 257 commits after PR1322 |
| legacy/pr-1518 | 925fca59d74b37d9480a527dfe87b6141f772bde | Ancestor of signed gateway; 48 commits after PR1516 |
| legacy/pr-1519 | 6eb9704e573135771d85e160be6d3bfdd8fb9701 | 16 commits absent from signed gateway |
| legacy/signed-gateway | cd7c20e9d52535674c36cccac4d03ef70e9f809b | 227 commits after PR1518 |

Live remote refs matched these commits before integration. All 15 non-merge PR1519 commits were absent by patch identity; M0 is a real two-parent union. The old local PR1322 branch at `26e116ad3` is 367 commits behind the frozen PR head and was not used as the baseline. PR1519's PR-body checkpoint `7124ee666` is older than its actual frozen head. The optional revision experiment `b5695d62e` remains reverted by `5b8bc5aa5`.

The merge initially conflicted in 275 paths. Hand-written sources and tests were reconciled semantically; generated catalogs, seals, UI assets and evidence were recreated with their existing producers. No ours/theirs whole-file resolution discarded a public API. The two independently added authoring APIs retain their own input formats and bounds. Source hash inputs were refreshed from measured merged bytes; metadata seals were computed only by the existing producers.

The signed gateway top commit is an explicitly unfinished native owner/failure-stage investigation. Its original tests-not-run status remains in the ledger. Retaining its source does not establish native or security acceptance.

## Hand resolutions and required lint repairs

| File | Resolution |
|---|---|
| `tobkiri_launcher/frontend/scripts/generate-frontend-contract-map.mjs` | Initial pin retained temporarily; updated to the generated merged map digest after its ordinary producer. |
| `tobkiri_launcher/frontend/scripts/generate-frontend-contract-map.test.mjs` | Exact producer digest expectation follows the merged output; no digest validation removed. |
| `tobkiri_launcher/frontend/src/components/advanced/ProfileCatalogSelector.tsx` | Retain independent Pack version and Pack editing busy states. Allow inspection/editing of unavailable definitions; stale or busy selection remains disabled and effect ceremony guards remain authoritative. |
| `tobkiri_launcher/frontend/src/components/advanced/ProfileCeremonyPanel.tsx` | Retain exact successor/mandatory Pack review checks alongside the automatically merged selected Profile editor context. |
| `tobkiri_launcher/frontend/src/lib/profileCeremony.ts` | Retain candidate-generation validation and selection binding. Give activation its bounded 60-second transition budget and other ceremony calls the bounded 30-second Host verification budget. |
| `tobkiri_launcher/frontend/src/lib/routes.test.ts` | Retain both inactive/denied Setup admission tests and execution Profile versus Account navigation test. |
| `tobkiri_launcher/frontend/src/lib/routes.ts` | Execution Profile remains in Home; Account is personal settings, and ordinary Setup remains reachable before activation. |
| `tobkiri_launcher/frontend/src/pages/Settings.tsx` | Retain native development handover and new runtime settings details; drop imports whose markup was replaced by the details component. |
| `tobkiri_runtime/core_runtime/api/packvm_capacity_errors.py` | Required mypy repair: preserve existing fail-closed checks while exposing validated types; no permission, digest or approval validation is removed. |
| `tobkiri_runtime/core_runtime/bootstrap/production_v4.py` | Retain evidence registry capture and full failed-bootstrap resource revocation together with signed-update budget and authenticated captured module fields. |
| `tobkiri_runtime/core_runtime/bootstrap/profile_capture.py` | Required mypy repair: preserve existing fail-closed checks while exposing validated types; no permission, digest or approval validation is removed. |
| `tobkiri_runtime/core_runtime/bootstrap/profile_pack_pin_recovery.py` | Required mypy repair: preserve existing fail-closed checks while exposing validated types; no permission, digest or approval validation is removed. |
| `tobkiri_runtime/core_runtime/bootstrap/profile_registry.py` | Renew using exact admitted artifact pins; require an available active plan before binding renewal, while successor recovery remains separate. |
| `tobkiri_runtime/core_runtime/bootstrap/saved_bridge.py` | Delegate pure message assembly to protocol extraction, retaining Host-only workspace resolution and the captured timing context override. |
| `tobkiri_runtime/core_runtime/bundled_artifact_retention_v4.py` | Required mypy repair: preserve existing fail-closed checks while exposing validated types; no permission, digest or approval validation is removed. |
| `tobkiri_runtime/core_runtime/credential_transport.py` | Retain sanitized transport value and finite denial diagnostics; automatically merged numeric data codec checks remain in the read boundary. Thread the JSON request diagnostics into the newly shared JSON/multipart/binary credential pipeline; return its decoder-sanitized value. |
| `tobkiri_runtime/core_runtime/debug_prebuilt_shell_v4.py` | Required mypy repair: preserve existing fail-closed checks while exposing validated types; no permission, digest or approval validation is removed. |
| `tobkiri_runtime/core_runtime/existing_host_extension_admission_v4.py` | Required mypy repair: preserve existing fail-closed checks while exposing validated types; no permission, digest or approval validation is removed. |
| `tobkiri_runtime/core_runtime/external_pack_catalog_v4.py` | Required mypy repair: preserve existing fail-closed checks while exposing validated types; no permission, digest or approval validation is removed. |
| `tobkiri_runtime/core_runtime/host_extension_authoring.py` | Required lint repair: equivalent typed def replaces inherited lambda assignment; immutable comparison keys and denial semantics are unchanged. Required mypy repair: preserve existing fail-closed checks while exposing validated types; no permission, digest or approval validation is removed. |
| `tobkiri_runtime/core_runtime/host_provider_backend_v4.py` | Retain Native generation budget port and authenticated outbound catalog/Workflow attempt ports as separate Host-private fields. |
| `tobkiri_runtime/core_runtime/interactive_effect_coordinator.py` | Require both exact generation budget validation and declared credential scope validation; native review shows scopes and budget together. |
| `tobkiri_runtime/core_runtime/pack_api_server.py` | Retain typed capacity and Workflow ownership errors; exact Pack revision and catalog routes share authenticated request capture scope. |
| `tobkiri_runtime/core_runtime/pack_authoring.py` | Keep both public authoring entrypoints: directory-source build_authored_pack and captured-byte build_python_pack/UI _build_pack. Separate their private size/generated-file limits; share public error and actual artifact compiler. No code execution, authority promotion or installation is added. |
| `tobkiri_runtime/core_runtime/pack_control_v4.py` | Required mypy repair: preserve existing fail-closed checks while exposing validated types; no permission, digest or approval validation is removed. |
| `tobkiri_runtime/core_runtime/publisher_key_versions.py` | Required mypy repair: preserve existing fail-closed checks while exposing validated types; no permission, digest or approval validation is removed. |
| `tobkiri_runtime/core_runtime/workflow_v4/binding.py` | Retain exact declared step parsing, provenance and ambiguity checks while allowing the captured-byte selected Workflow whole-output reference ${steps.<id>.output} as well as field references. |
| `tobkiri_runtime/core_runtime/workflow_v4/engine.py` | Use PR1519 authenticated run ownership, declared dependency output bindings, committed provenance and retry-pinned inputs. Keep signed-update selected-run provider/API and templates module reachable without duplicate weaker execution resolver. |
| `tobkiri_runtime/core_runtime/workflow_v4/provider.py` | Retain selected Workflow execution and read-only run observation operations. |
| `tobkiri_runtime/ecosystem/defaultspack/defaultspack/desktop_app.py` | Retain authenticated startup plus signal handling in the existing try/finally capture-drain lifecycle; the other side duplicate launch block is already represented before the signals. |
| `tobkiri_runtime/ecosystem/defaultspack/webapp/src/App.tsx` | Retain saved Coding context/preflight, Profile tool control preferences and terminal publication with approval-command draft ownership. |
| `tobkiri_runtime/ecosystem/defaultspack/webapp/src/lib/api.test.ts` | Retain model-profile envelope validation alongside attachment content and declared route tests. |
| `tobkiri_runtime/ecosystem/rumi_model_registry_pack/runtime/process.py` | Validate preserve-existing update mode separately; both create and save validate the selected Provider connection. |
| `tobkiri_runtime/ecosystem/rumi_model_registry_pack/runtime/registry.py` | Preserve saved max_tokens validation and non-secret provider token parameters; require strict boolean enabled and preserve disabled route/update identity guards. |
| `tobkiri_runtime/ecosystem/rumi_provider_adapters_pack/runtime/adapter.py` | Retain failure diagnostics, budget denial handling and chat parameter compiler, plus modality payload and shared saved connection compiler. |
| `tobkiri_runtime/ecosystem/rumi_provider_registry_pack/domain/ai_client/provider_compiler/native_filters.py` | Move signed-update exact decimal price, zero-policy and wire-price checks into the pure protocol implementation; retain PR1519 Pack compatibility reexport. |
| `tobkiri_runtime/scripts/migrate_pack_artifacts_v4.py` | Required mypy repair: preserve existing fail-closed checks while exposing validated types; no permission, digest or approval validation is removed. |
| `tobkiri_runtime/scripts/packaging_cleanup.py` | Required mypy repair: preserve existing fail-closed checks while exposing validated types; no permission, digest or approval validation is removed. |
| `tobkiri_runtime/tests/test_ai_gateway_pack.py` | Retain saved output ceiling/multiple-completion denial tests and disabled-model refusal tests. |
| `tobkiri_runtime/tests/test_model_registry_pack.py` | Retain both preserved route activation tests and strict enabled/token filtering/exclusive persistence tests; nonboolean activation now fails earlier at strict owner validation. |
| `tobkiri_runtime/tests/test_pack_authoring.py` | Retain all tests from both independent authoring APIs and the _build helper imported by CLI/Profile SDK tests. |
| `tobkiri_runtime/tests/test_workflow_v4.py` | Union Workflow catalog includes selected-run, run.observe and separate authenticated stop contract; regenerate and verify exact union counts. |

The Profile ceremony preserves both exact executable matching and Profile separation: browsed/inactive Profiles cannot borrow approval flags from the active Profile, while their executable digests must still match their captured closure. Active mandatory successor exceptions remain bound to the authenticated current Profile/revision/plan. Resolve selection metadata is finite, paired, tied to the candidate Profile and consistent with the predecessor; malformed or substituted records remain denied.

The Workflow catalog contains both `run.selected` and `run.observe`, plus the separately authenticated stop contract: two Contracts and 23 Operations. Whole-output references remain supported with exact declared step parsing. PR1519's committed provenance, dependency checks and retry-pinned input remain the execution path.

Canonical input reconciliation also covered `schemas/pack_v4_catalog.v1.json`, the owning executable-source fixture, the declarative Profile/base/shell inputs, and the closed Host file inventory. Named list entries were merged by identity, preserving independent additions. Conflicting primitive values were source hashes/definition revisions, replaced by measured raw input fingerprints and the normal generators. No permission, approval or trust baseline was relaxed. The webapp test command retains the union of both branches' test selectors.

## Producers

The normal source-registry, Workflow, Pack v4, authority and executable producers passed. Both frontend builds passed through their ordinary bundle/copy/vendored-asset checks. `compiled_ui_artifacts_v4.py` declared the actual merged UI bytes with the existing version; the Defaultspack bundle/profile/lock and Launcher presentation producers passed. Generated frontend contract-map pins match the measured raw merged map.

| Evidence producer | Exit code | Meaning |
|---|---:|---|
| `command-inventory` | 0 | Normal producer completed; generation does not establish product acceptance. |
| `pack-sdk` | 0 | Normal producer completed; generation does not establish product acceptance. |
| `architecture-inventory` | 0 | Normal producer completed; generation does not establish product acceptance. |
| `boundary-assessment` | 0 | Normal producer completed; generation does not establish product acceptance. |
| `migration-proof` | 0 | Normal producer completed; generation does not establish product acceptance. |
| `complete-migration` | 1 | Strict migration gate remains RED; no exemption or acceptance claim. |
| `source-manifest` | 0 | Normal producer completed; generation does not establish product acceptance. |

Architecture/boundary inventory and independent migration proof generation preserve their existing review/acceptance states. The strict complete migration gate is RED with no evidence drift. The final source closure manifest was regenerated with its existing writer. A first freshness pass correctly detected executable-source drift after the authority producer refreshed v3 projections; rerunning the ordinary source-registry writer after those inputs converged repaired the drift. No freshness check was suppressed.

## Verification checkpoint

- `just lint`: Ruff passed; mypy passed over 571 source files. Required inherited type issues were repaired without changing the validated authority boundaries.
- Defaults `npm run lint`: TypeScript, layer/boundary/local-font checks and attachment security guardrails passed.
- Launcher `npm run lint`: TypeScript and both generated contract checks passed.
- Focused Launcher tests: 61 passed, zero failures/skips. They retain both exact executable matching and inactive Profile review isolation.
- Focused Defaults API/draft/tool-preference/terminal-observation tests: 191 passed, zero failures/skips.
- The initial 288-case Python source cluster had 287 setup errors and one existing native Windows skip: no test body ran. First the normal source closure guard required manifest regeneration; the rerun then correctly refused the uncommitted checkout. The clean-checkout fixture is unchanged. Real Python verification is run after M0 in one source-only clean verification checkout, with exact commit/log receipts retained separately. This checkpoint does not claim those Python tests passed.

No Tauri build, VM build/provisioning, native application launch, model download, npm dependency install or live approval was performed. Existing ignored dependency runtimes were reused. The verification checkout uses shared Git objects and is removed after retaining its receipts.

## Open requirements

`legacy-ledger.json` contains 69 feature/evidence rows, including all eight known PR1519 browser failures. Every acceptance mapping is explicitly planned and unimplemented; no row is migration-complete or retired. Native handover and Host slot mechanisms remain present until parity and any required human retirement approval. The existing PRs remain open drafts.

Rust/WIT/wasm/gate/storage/Profile/Flow/CLI work, Pack conversion, isolated UI transport, byte parity, real local model execution, mobile/P2P, published presets and the removal of Python/PackVM/old engines remain separate Issue #1520 milestones. M0 establishes the legacy source union only.
