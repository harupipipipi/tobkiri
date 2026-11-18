"""Replay Model Registry Settings mutations from pinned Python source."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
from typing import Any
from unittest.mock import patch

from generate import (
    PROFILE,
    REGISTRY,
    SETTINGS,
    SOURCE_COMMIT,
    STORE_VERSION,
    _block_network,
    _json_bytes,
    _stage_sources,
)


SCHEMA = "io.tobkiri.original-python-model-registry-save.v1"
NOW = "2026-10-21T12:34:56Z"


def _cases() -> list[dict[str, Any]]:
    request = {
        "model_profile_id": "daily-model",
        "model_id": "models/example",
        "provider_instance_id": "example-provider",
        "display_name": " 日常のモデル ",
        "expected_revision": 0,
        "provider_registry_revision": 7,
    }
    saved = {
        "version": STORE_VERSION,
        "profile_id": PROFILE,
        "revision": 12,
        "profiles": {
            "daily-model": {
                "model_profile_id": request["model_profile_id"],
                "display_name": request["display_name"],
                "model_id": request["model_id"],
                "requirements": {"minimum_context": 8192},
                "credential_handle": "opaque:synthetic-model-fixture",
                "parameters": {
                    "max_tokens": 4096,
                    "temperature": 0.125,
                    "offset": -0.0,
                    "nested": {"threshold": 1e-07, "counter": 9007199254740993},
                },
                "enabled": True,
                "metadata": {
                    "provider_connection_id": request["provider_instance_id"],
                    "private_marker": "synthetic-model-fixture",
                },
                "created_at": "2026-01-02T03:04:05Z",
                "updated_at": "2026-08-09T10:11:12Z",
                "record_revision": 4,
            },
        },
        "aliases": {"daily": "daily-model"},
        "migration": None,
    }
    current = {**request, "expected_revision": saved["revision"]}
    cap_update = {**current, "max_output_tokens": 131072}
    return [
        _case("create-first-route", None, request),
        _case("create-existing-route", saved, current),
        _case("create-stale-model-revision", saved, request),
        _case("save-updated-cap", saved, cap_update),
        _case("save-existing-cap", saved, {**current, "max_output_tokens": 4096}),
        _case(
            "save-changed-identity", saved, {**cap_update, "model_id": "models/other"}
        ),
        _case(
            "save-invalid-float-cap", saved, {**current, "max_output_tokens": 4096.0}
        ),
    ]


def _case(
    case_id: str, document: dict[str, Any] | None, request: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": case_id,
        "profile_id": PROFILE,
        "document": document,
        "request": request,
        "now": NOW,
    }


def _observe(
    case: dict[str, Any],
    user_data_root: Path,
    owner: ModuleType,
    settings: ModuleType,
) -> dict[str, Any]:
    registry = owner.ModelRegistry(case["profile_id"], user_data_root=user_data_root)
    document = case["document"]
    before = None
    if document is not None:
        before = _json_bytes(document)
        registry.path.parent.mkdir(parents=True)
        registry.path.write_bytes(before)
    observation = dict(case)
    try:
        normalized = settings.normalize_model_profile_save(case["request"])
        observation["normalized"] = normalized
        record = normalized["record"]
        revision = normalized["expected_revision"]
        with patch.object(owner, "_now", return_value=case["now"]):
            if normalized["operation"] == "create":
                result = registry.create(record, expected_revision=revision)
            else:
                result = registry.save(
                    record,
                    expected_revision=revision,
                    preserve_existing=normalized["preserve_existing"],
                )
        observation["result"] = result
        # The host omits existing configuration before presenting create.
        # A newly created route has no existing output cap to omit.
        if normalized["operation"] == "save" or document is None:
            observation["presented"] = settings.present_model_profile_saved(result)
    except (ValueError, owner.ModelRegistryConflict) as error:
        observation["error"] = {"class": type(error).__name__, "message": str(error)}
    after = registry.path.read_bytes() if registry.path.is_file() else None
    if "error" in observation and after != before:
        raise RuntimeError(f"source error changed store in {case['id']}")
    if before is not None and after is None:
        raise RuntimeError(f"source save removed store in {case['id']}")
    observation["updated"] = (
        json.loads(after) if after is not None and after != before else None
    )
    return observation


def main() -> None:
    """Print the reproducible original Python save fixture to standard output."""
    argparse.ArgumentParser(description=__doc__).parse_args()
    sys.dont_write_bytecode = True
    sys.addaudithook(_block_network)
    with tempfile.TemporaryDirectory(prefix="tobkiri-model-registry-save-") as temp:
        root = Path(temp)
        runtime_root = root / "tobkiri_runtime"
        sources = _stage_sources(runtime_root)
        os.environ["RUMI_USER_DATA"] = str(root / "default-user-data")
        sys.path.insert(0, str(runtime_root))
        owner = importlib.import_module(REGISTRY)
        settings = importlib.import_module(SETTINGS)
        cases = [
            _observe(case, root / case["id"], owner, settings) for case in _cases()
        ]
    fixture = {
        "schema": SCHEMA,
        "source_commit": SOURCE_COMMIT,
        "sources": sources,
        "cases": cases,
    }
    sys.stdout.buffer.write(_json_bytes(fixture))


if __name__ == "__main__":
    main()
