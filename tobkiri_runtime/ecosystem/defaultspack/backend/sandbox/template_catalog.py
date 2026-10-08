from __future__ import annotations

import logging
import importlib
from copy import deepcopy
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


def sandbox_template_catalog(defaultspack_root: str | Path | None = None) -> list[dict[str, Any]]:
    raw_templates = _sandbox_template_contributions(defaultspack_root)
    by_id = {
        str(template.get("id") or template.get("template_id") or "").strip(): template
        for template in raw_templates
        if str(template.get("id") or template.get("template_id") or "").strip()
    }
    resolved: list[dict[str, Any]] = []
    for template_id in sorted(by_id):
        template = _resolve_extends(template_id, by_id, seen=())
        status = str(template.get("status") or "active").strip().lower()
        if status and status != "active":
            continue
        resolved.append(template)
    return resolved


def sandbox_template_by_id(
    template_id: str | None,
    *,
    defaultspack_root: str | Path | None = None,
) -> dict[str, Any]:
    clean_id = str(template_id or "").strip()
    if not clean_id:
        return {}
    for template in sandbox_template_catalog(defaultspack_root):
        if str(template.get("id") or template.get("template_id") or "").strip() == clean_id:
            return deepcopy(template)
    return {}


def _sandbox_template_contributions(defaultspack_root: str | Path | None) -> list[dict[str, Any]]:
    catalog = _template_catalog(defaultspack_root)
    items = catalog.get("source_adapter_contributions")
    if not isinstance(items, list):
        return []
    templates: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("bucket") != "sandbox_templates":
            continue
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            continue
        template = _strip_untrusted_flags(deepcopy(metadata))
        template["trust_level"] = str(item.get("trust_level") or "user")
        template["source_pack_id"] = str(item.get("source_pack_id") or "rumi_sandbox_runtime_pack")
        template["_source"] = str(item.get("source_path") or "")
        template["_catalog_projected_id"] = str(item.get("projected_id") or "")
        templates.append(template)
    return templates


def _template_catalog(defaultspack_root: str | Path | None) -> dict[str, Any]:
    try:
        catalog_runtime = importlib.import_module(
            "ecosystem.defaultspack.domain.templates.catalog_runtime"
        )
    except Exception as exc:
        logger.warning("Unable to import sandbox template catalog runtime via ecosystem path.", exc_info=exc)
        try:
            catalog_runtime = importlib.import_module("domain.templates.catalog_runtime")
        except Exception as fallback_exc:
            logger.warning("Unable to import sandbox template catalog runtime via local path.", exc_info=fallback_exc)
            return {}
    get_template_catalog_snapshot = getattr(catalog_runtime, "get_template_catalog_snapshot", None)
    if not callable(get_template_catalog_snapshot):
        return {}
    try:
        snapshot = get_template_catalog_snapshot(defaultspack_root=defaultspack_root)
    except Exception as exc:
        logger.warning("Unable to load sandbox template catalog snapshot.", exc_info=exc)
        return {}
    catalog = getattr(snapshot, "catalog", {})
    return catalog if isinstance(catalog, dict) else {}


def _resolve_extends(
    template_id: str,
    by_id: dict[str, dict[str, Any]],
    *,
    seen: tuple[str, ...],
) -> dict[str, Any]:
    template = deepcopy(by_id.get(template_id) or {})
    parent_id = str(template.get("extends") or "").strip()
    if not parent_id or parent_id in seen or parent_id not in by_id:
        return template
    parent = _resolve_extends(parent_id, by_id, seen=(*seen, template_id))
    merged = _merge_template(parent, template)
    merged["source_template_ids"] = _unique_strings(
        [
            *(parent.get("source_template_ids") or [parent_id]),
            *(template.get("source_template_ids") or [template_id]),
        ]
    )
    return merged


def _merge_template(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(parent)
    for key, value in child.items():
        if key in {"runtime", "policy"} and isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        elif key == "allowed_operations":
            merged[key] = _unique_strings([*(merged.get(key) or []), *(value or [])])
        elif key == "user_overrides" and isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _merge_dict(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(parent)
    for key, value in child.items():
        current = merged.get(key)
        if key in {"packages", "provider_requirements", "capabilities", "allowlist"} and isinstance(value, list):
            merged[key] = _merge_list(current, value, key=key)
        elif isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_dict(current, value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _merge_list(current: Any, value: list[Any], *, key: str) -> list[Any]:
    if key == "packages":
        by_name: dict[str, dict[str, Any]] = {}
        ordered: list[str] = []
        for item in [*(current if isinstance(current, list) else []), *value]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            if name not in by_name:
                ordered.append(name)
            by_name[name] = deepcopy(item)
        return [by_name[name] for name in ordered]
    return _unique_strings([*(current if isinstance(current, list) else []), *value])


def _unique_strings(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _strip_untrusted_flags(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _strip_untrusted_flags(item)
            for key, item in value.items()
            if str(key) not in {"trust_level", "trusted", "approved"}
        }
    if isinstance(value, list):
        return [_strip_untrusted_flags(item) for item in value]
    return value
