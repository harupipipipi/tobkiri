set windows-shell := ["cmd.exe", "/C"]

# Display available commands.
help:
    just -l

# Run the package health check.
health:
    python -m rumi_ai --health

# Run root-level contract tests.
root-test *args:
    python -c "from pathlib import Path; Path('.test-logs').mkdir(exist_ok=True)"
    python scripts/quality/compact_test_runner.py --log-dir .test-logs --log-file "root-test-{run}.log" -- pytest tests/ {{args}}

# Run tobkiri_runtime tests. Pass pytest selectors after the recipe name.
test *args:
    python -c "from pathlib import Path; Path('.test-logs').mkdir(exist_ok=True)"
    python scripts/quality/compact_test_runner.py --log-dir .test-logs --log-file "runtime-test-{run}.log" --cwd tobkiri_runtime -- python -m pytest {{args}}

# Run the focused defaultspack coding/tooling regression cluster.
tooling-test:
    python -c "from pathlib import Path; Path('.test-logs').mkdir(exist_ok=True)"
    python scripts/quality/compact_test_runner.py --log-dir .test-logs --log-file "tooling-test-{run}.log" --cwd tobkiri_runtime -- python -B -m pytest \
        tests/test_defaultspack_provider_tool_schema.py \
        tests/test_defaultspack_tool_protocol_v2.py \
        tests/test_defaultspack_terminal_policy.py \
        tests/test_defaultspack_coding_hardening.py -q

# Test the compact runner directly; never wrap this recipe with the runner itself.
compact-runner-test:
    python -m pytest scripts/quality/test_compact_test_runner.py -q

# Run Python static checks over the backend surfaces guarded in CI.
lint:
    cd tobkiri_runtime && python -m ruff check core_runtime backend_core ecosystem/defaultspack/domain/coding ecosystem/defaultspack/domain/tool ecosystem/defaultspack/blocks/coding app.py
    cd tobkiri_runtime && python -m mypy --check-untyped-defs core_runtime backend_core ecosystem/defaultspack/domain/coding ecosystem/defaultspack/domain/tool ecosystem/defaultspack/blocks/coding app.py

# Run defaultspack frontend checks.
frontend-check:
    python -c "from pathlib import Path; Path('.test-logs').mkdir(exist_ok=True)"
    python scripts/quality/compact_test_runner.py --log-dir .test-logs --log-file "frontend-test-{run}.log" --cwd tobkiri_runtime/ecosystem/defaultspack/webapp -- npm test
    cd tobkiri_runtime/ecosystem/defaultspack/webapp && npm run lint
    cd tobkiri_runtime/ecosystem/defaultspack/webapp && npm run build

# Run the defaultspack integrity scan used by CI.
integrity:
    cd tobkiri_runtime && python scripts/quality/scan_defaultspack_integrity.py --strict

# Run the debt scan plus the no-baseline Python structural boundary gate.
pack-architecture:
    # Compare the working-tree candidate with the committed, reviewed baseline.
    reference="$(mktemp)"; trap 'rm -f "$reference"' EXIT; git show HEAD:scripts/quality/pack_architecture_baseline.json > "$reference"; python scripts/quality/scan_pack_architecture.py --reference-baseline "$reference"
    python scripts/quality/check_core_no_favoritism.py
    python tobkiri_runtime/scripts/quality/check_pack_boundary_assessment.py

# Validate v4 schemas, provenance, migration guards, scanners, and inventory.
pack-architecture-v4:
    python scripts/quality/validate_pack_architecture.py

# Check Pack boundary debt against the reviewed shrink-only baseline.
pack-boundary-lint:
    python scripts/quality/scan_pack_boundaries.py

# Explicitly refresh Pack boundary debt after review.
pack-boundary-baseline:
    python scripts/quality/scan_pack_boundaries.py --update-baseline

# Exercise one Defaults-independent Profile through the canonical v4 Host path.
pack-v4-minimal-profile:
    cd tobkiri_runtime && python -m pytest tests/test_minimal_profile_vertical_slice.py -q

# Check the checked-in Launcher presentation projection against canonical manifests.
presentation-catalog:
    python scripts/quality/generate_presentation_catalog.py --check

# Check generated artifacts together without rewriting them; this is not full CI.
generated-check python="python":
    {{python}} -B -c 'import runpy; from pathlib import Path; runpy.run_path(".github/scripts/prepare_tauri_resources.py")["canonical_host_files"](Path("tobkiri_runtime"))'
    node tobkiri_launcher/frontend/scripts/generate-frontend-contract-map.mjs --check
    node tobkiri_launcher/frontend/scripts/generate-defaults-setup-contract.mjs --check
    {{python}} -B tobkiri_runtime/scripts/migrate_pack_artifacts_v4.py --check
    {{python}} -B tobkiri_runtime/scripts/migrate_manifest_authority.py --check
    {{python}} -B tobkiri_runtime/scripts/generate_executable_source_registry_v1.py --check
    {{python}} -B tobkiri_runtime/scripts/generate_executable_catalogs_v4.py --check
    {{python}} -B -m pytest tobkiri_runtime/tests/test_complete_v4_migration_gate.py::test_executable_source_registry_covers_every_executable_operation -q
    {{python}} -B tobkiri_runtime/scripts/generate_defaultspack_v4_bundle.py --check
    {{python}} -B tobkiri_runtime/scripts/quality/scan_command_protocol.py --inventory tobkiri_runtime/generated/pack_sdk/command_inventory.json --check-inventory
    {{python}} -B tobkiri_runtime/scripts/tobkiri_pack.py generate tobkiri_runtime/generated/pack_sdk --check
    {{python}} -B tobkiri_runtime/scripts/tobkiri_pack.py project-legacy tobkiri_runtime/examples/pack_v3/minimal_service.json tobkiri_runtime/examples/pack_v3/minimal_service.ecosystem.json --check
    {{python}} -B tobkiri_runtime/scripts/quality/scan_defaultspack_integrity.py --strict
    {{python}} -B scripts/quality/generate_presentation_catalog.py --check
    {{python}} -B scripts/quality/validate_pack_architecture.py
    {{python}} -B scripts/quality/scan_pack_boundaries.py
    {{python}} -B tobkiri_runtime/scripts/quality/check_pack_boundary_assessment.py
    {{python}} -B tobkiri_runtime/scripts/quality/run_independent_migration_proof.py --check
    {{python}} -B tobkiri_runtime/scripts/quality/scan_complete_v4_migration.py --check --freshness-only
    {{python}} -B tobkiri_runtime/scripts/generator_source_manifest.py --check

# Migrate one legacy profile to a review-only v4 document.
migrate-legacy-profile source output:
    python scripts/quality/migrate_legacy_profile.py {{source}} --output {{output}}

# Validate Command Protocol v1 coverage and generated multi-client Pack SDK.
command-protocol:
    cd tobkiri_runtime && python scripts/quality/scan_command_protocol.py --inventory generated/pack_sdk/command_inventory.json --check-inventory
    cd tobkiri_runtime && python scripts/tobkiri_pack.py generate generated/pack_sdk --check

# Verify compatibility views are generated only from their canonical v3 source.
manifest-projections:
    cd tobkiri_runtime && python scripts/tobkiri_pack.py project-legacy examples/pack_v3/minimal_service.json examples/pack_v3/minimal_service.ecosystem.json --check
