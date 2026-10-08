"""Regress exact production Function ownership across canonical Pack records."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.migrate_pack_artifacts_v4 import (
    PackV4MigrationError,
    _explicit_function_sources,
    _function_sources,
)

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_PACK = "rumi_workspace_mount_pack"
TURN_PACK = "rumi_turn_runtime_pack"


def records():
    """Read the canonical catalog owned by this test's runtime checkout."""
    catalog = json.loads((ROOT / "schemas/pack_v4_catalog.v1.json").read_text())
    return {record["pack_id"]: record for record in catalog["packs"]}


def test_all_explicit_functions_validate_against_their_own_pack_contracts():
    for record in records().values():
        _explicit_function_sources(record)


def test_workspace_has_preserved_owner_functions_and_three_distinct_picker_hooks():
    projected = _function_sources(records()[WORKSPACE_PACK])
    assert {(f["function_id"], tuple(f["operation_ids"])) for f in projected} == {
        (
            WORKSPACE_PACK + ".workspace-mount.manage",
            (WORKSPACE_PACK + ".workspace-mount",),
        ),
        (
            WORKSPACE_PACK + ".workspace-mount.resource",
            (WORKSPACE_PACK + ".workspace-resource",),
        ),
        (
            WORKSPACE_PACK + ".project-directory.service",
            ("workspace.directory.acquire",),
        ),
        (
            WORKSPACE_PACK + ".project-workspace-prepare.service",
            ("workspace.mount.prepare",),
        ),
        (
            WORKSPACE_PACK + ".project-workspace-execute.service",
            ("workspace.mount.execute",),
        ),
    }


def test_turn_keeps_all_original_function_operation_bindings():
    projected = _function_sources(records()[TURN_PACK])
    assert {(f["function_id"], tuple(f["operation_ids"])) for f in projected} == {
        (TURN_PACK + ".turn-runtime." + function, (TURN_PACK + ".turn-" + operation,))
        for function, operation in [
            ("lifecycle", "lifecycle"),
            ("guidance", "guidance"),
            ("events", "events"),
            ("resource", "resource"),
            ("stop", "stop"),
            ("reconcile", "reconcile"),
            ("saved", "saved"),
            ("progress", "progress"),
            ("progress-resource", "progress-resource"),
        ]
    }


def test_misplacing_workspace_functions_under_turn_is_rejected_by_real_generator():
    source = records()
    corrupted = deepcopy(source[TURN_PACK])
    corrupted["functions"] = deepcopy(source[WORKSPACE_PACK]["functions"])
    with pytest.raises(PackV4MigrationError, match="explicit Function identity"):
        _explicit_function_sources(corrupted)
