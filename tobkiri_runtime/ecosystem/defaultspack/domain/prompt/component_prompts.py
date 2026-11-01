from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..components import get_domain_component_registry
from .trust import prompt_pack_source_is_trusted


def _component_file(manifest: dict[str, Any], entrypoint: str) -> Path | None:
    entrypoints = manifest.get("entrypoints")
    rel_path = entrypoints.get(entrypoint) if isinstance(entrypoints, dict) else None
    if not isinstance(rel_path, str) or not rel_path.strip():
        return None
    entrypoint_path = Path(rel_path)
    if entrypoint_path.is_absolute():
        return None
    source_path = manifest.get("source_path")
    if not isinstance(source_path, str) or not source_path:
        return None
    try:
        component_dir = Path(source_path).parent.resolve()
        candidate = (component_dir / entrypoint_path).resolve()
        candidate.relative_to(component_dir)
    except (OSError, ValueError):
        return None
    return candidate


def _load_rules(manifest: dict[str, Any]) -> dict[str, Any]:
    path = _component_file(manifest, "rules")
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def component_prompt_manifests() -> dict[str, dict[str, Any]]:
    prompts: dict[str, dict[str, Any]] = {}
    registry = get_domain_component_registry()
    for component in registry.list("prompts"):
        manifest = component.as_dict()
        prompt_id = str(manifest.get("prompt_id") or manifest.get("id") or "").strip()
        if not prompt_id:
            continue
        source_pack_id = str(manifest.get("source_pack_id") or "").strip()
        if not prompt_pack_source_is_trusted(source_pack_id, manifest.get("source_path", "")):
            continue
        prompts[prompt_id] = manifest
    return prompts


def component_prompt_text(prompt_id: str) -> str | None:
    manifest = component_prompt_manifests().get(str(prompt_id or "").strip())
    if manifest is None:
        return None
    path = _component_file(manifest, "prompt")
    if path is None or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def component_prompt_records() -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for prompt_id, manifest in component_prompt_manifests().items():
        body = component_prompt_text(prompt_id)
        if body is None:
            continue
        rules = _load_rules(manifest)
        records[prompt_id] = {
            "id": prompt_id,
            "name": prompt_id,
            "content": body,
            "body": body,
            "description": str(manifest.get("description") or ""),
            "variables": list(rules.get("variables", [])) if isinstance(rules.get("variables"), list) else [],
            "metadata": {
                "source": "component",
                "source_pack_id": manifest.get("source_pack_id", ""),
                "component_id": manifest.get("id", ""),
                "manifest_path": manifest.get("source_path", ""),
                "rules": rules,
            },
            "created_at": "",
            "updated_at": "",
            "read_only": True,
            "source_pack_id": manifest.get("source_pack_id", ""),
        }
    return records
