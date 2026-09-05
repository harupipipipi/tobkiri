"""Finite migration adapter from known defaultspack prompt stores.

The new owner never reads sibling source or private storage. This adapter reads
only fixed legacy roots, sends normalized records through the migration
contract, and never modifies the legacy source.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from blocks._common import error, ok
from core_runtime.global_contract_dispatch import (
    GlobalContractInvocationError,
    GlobalContractUnavailable,
    invoke_global_contract,
)
from core_runtime.profile_workspace import ProfileWorkspaceManager
from core_runtime.paths import USER_DATA_DIR
from core_runtime.resolved_profile_scope import active_resolved_profile

_CONTRACT = "rumi.action.prompt.migrate.v1"


def _records(profile_id: str) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    shared_root = Path(__file__).resolve().parents[2] / "user_data" / "shared" / "prompts"
    if shared_root.is_dir():
        for path in sorted(shared_root.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            prompt_id = str(payload.get("name") or payload.get("id") or path.stem).strip()
            if prompt_id:
                records[prompt_id] = _record(
                    prompt_id,
                    payload.get("body", payload.get("content", "")),
                    payload,
                    "defaultspack-shared-json",
                )
    profile_root = ProfileWorkspaceManager(USER_DATA_DIR).paths_for_profile(
        profile_id
    ).root
    prompt_root = profile_root / "prompts"
    if prompt_root.is_dir():
        for path in sorted(prompt_root.glob("*")):
            if not path.is_file() or path.suffix not in {".md", ".txt"}:
                continue
            prompt_id = _prompt_id_from_path(path)
            records[prompt_id] = _record(
                prompt_id,
                path.read_text(encoding="utf-8"),
                {},
                "profile-prompt-file",
            )
    return [records[key] for key in sorted(records)]


def _record(
    prompt_id: str,
    body: Any,
    payload: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    raw_variables = payload.get("variables")
    variables = []
    if isinstance(raw_variables, list):
        for item in raw_variables:
            value = item.get("name") if isinstance(item, dict) else item
            if str(value or "").strip():
                variables.append(str(value).strip())
    return {
        "prompt_id": prompt_id,
        "body": str(body or ""),
        "description": str(payload.get("description") or ""),
        "variables": variables,
        "enabled": bool(payload.get("enabled", True)),
        "source": source,
    }


def _prompt_id_from_path(path: Path) -> str:
    name = path.name
    for suffix in (".system.md", ".prompt.md", ".md", ".txt"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _edge_states(profile_id: str) -> dict[str, bool]:
    profile_file = ProfileWorkspaceManager(USER_DATA_DIR).paths_for_profile(
        profile_id
    ).profile_file
    try:
        profile = yaml.safe_load(profile_file.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    metadata = profile.get("metadata") if isinstance(profile, dict) else None
    ai_input = metadata.get("ai_input") if isinstance(metadata, dict) else None
    disabled = ai_input.get("disabled_edges") if isinstance(ai_input, dict) else None
    if not isinstance(disabled, list):
        return {}
    return {
        str(edge_id): False
        for edge_id in disabled
        if str(edge_id or "").strip()
    }


def _source_hash(
    records: list[dict[str, Any]],
    edge_states: dict[str, bool],
) -> str:
    raw = json.dumps(
        {"records": records, "edge_states": edge_states},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def run(input_data: dict, context: dict) -> dict:
    """Inspect, apply, or rollback the fixed-root legacy migration."""
    data = dict(input_data) if isinstance(input_data, dict) else {}
    operation = str(data.pop("_migration_operation", "inspect")).strip()
    plan = active_resolved_profile()
    registry = context.get("v4_dispatch_session") if isinstance(context, dict) else None
    if plan is None or registry is None:
        return error("Prompt migration is unavailable", "PROMPT_MIGRATION_UNAVAILABLE")
    requested_profile = str(data.get("profile_id") or "").strip()
    if requested_profile and requested_profile != plan.profile_id:
        return error("Prompt migration profile is not active", "PROMPT_MIGRATION_DENIED")
    records = _records(plan.profile_id)
    edge_states = _edge_states(plan.profile_id)
    source_hash = _source_hash(records, edge_states)
    if operation == "inspect":
        return ok({
            "profile_id": plan.profile_id,
            "prompt_ids": [item["prompt_id"] for item in records],
            "source_hash": source_hash,
            "count": len(records),
            "edge_ids": sorted(edge_states),
        })
    if context.get("_tool_server_approved") is not True:
        return error("Prompt migration requires approval", "PROMPT_MIGRATION_DENIED")
    try:
        if operation == "apply":
            expected = str(data.get("expected_source_hash") or "")
            if expected != source_hash:
                return error("Legacy prompt source changed", "PROMPT_WRITE_CONFLICT")
            value = invoke_global_contract(
                registry,
                _CONTRACT,
                "migration.import",
                {
                    "profile_id": plan.profile_id,
                    "records": records,
                    "edge_states": edge_states,
                    "expected_source_hash": expected,
                },
            )
        elif operation == "rollback":
            value = invoke_global_contract(
                registry,
                _CONTRACT,
                "migration.rollback",
                {
                    "profile_id": plan.profile_id,
                    "migration_id": str(data.get("migration_id") or ""),
                },
            )
        else:
            return error("Unknown prompt migration operation", "INVALID_MIGRATION_OPERATION")
        return ok(value)
    except GlobalContractUnavailable as exc:
        return error(str(exc), "PROMPT_MIGRATION_UNAVAILABLE")
    except GlobalContractInvocationError as exc:
        return error(str(exc), exc.code or "PROMPT_MIGRATION_FAILED")
