set windows-shell := ["cmd.exe", "/C"]

# Display available commands.
help:
    just -l

# Run the package health check.
health:
    python -m rumi_ai --health

# Run root-level contract tests.
root-test *args:
    pytest tests/ {{args}}

# Run tobkiri_runtime tests. Pass pytest selectors after the recipe name.
test *args:
    cd tobkiri_runtime && python -m pytest {{args}}

# Run the focused defaultspack coding/tooling regression cluster.
tooling-test:
    cd tobkiri_runtime && python -m pytest \
        tests/test_defaultspack_provider_tool_schema.py \
        tests/test_defaultspack_tool_protocol_v2.py \
        tests/test_defaultspack_terminal_policy.py \
        tests/test_defaultspack_coding_hardening.py -q

# Run Python static checks over the backend surfaces guarded in CI.
lint:
    cd tobkiri_runtime && python -m ruff check core_runtime backend_core ecosystem/defaultspack/domain/coding ecosystem/defaultspack/domain/tool ecosystem/defaultspack/blocks/coding app.py
    cd tobkiri_runtime && python -m mypy --check-untyped-defs core_runtime backend_core ecosystem/defaultspack/domain/coding ecosystem/defaultspack/domain/tool ecosystem/defaultspack/blocks/coding app.py

# Run defaultspack frontend checks.
frontend-check:
    cd tobkiri_runtime/ecosystem/defaultspack/webapp && npm test
    cd tobkiri_runtime/ecosystem/defaultspack/webapp && npm run lint
    cd tobkiri_runtime/ecosystem/defaultspack/webapp && npm run build

# Run the defaultspack integrity scan used by CI.
integrity:
    cd tobkiri_runtime && python scripts/quality/scan_defaultspack_integrity.py --strict

# Run the Wave 1 repository-wide pack architecture boundary gate.
pack-architecture:
    python scripts/quality/scan_pack_architecture.py

# Validate v4 schemas, provenance, migration guards, scanners, and inventory.
pack-architecture-v4:
    python scripts/quality/validate_pack_architecture.py

# Exercise one Defaults-independent Profile through the canonical v4 Host path.
pack-v4-minimal-profile:
    cd tobkiri_runtime && python -m pytest tests/test_minimal_profile_vertical_slice.py -q

# Check the checked-in Launcher presentation projection against canonical manifests.
presentation-catalog:
    python scripts/quality/generate_presentation_catalog.py --check

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
