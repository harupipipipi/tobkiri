from __future__ import annotations

import importlib
import json
import re
from pathlib import Path
from typing import Any

from ..capability.catalog import CapabilityCatalog
from .renderer import render
from .resolver import PromptResolver
from .studio_client import authored_prompt, prompt_owner_available
from .trust import is_trusted_prompt_pack


_VARIABLE_PATTERN = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_or_empty(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
_BRACED_PATTERN = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_VALID_VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_SAFE_PROMPT_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def _read_mapping(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        yaml_module = importlib.import_module("yaml")
    except ImportError:
        return {}
    safe_load = getattr(yaml_module, "safe_load", None)
    yaml_error = getattr(yaml_module, "YAMLError", ValueError)
    if not callable(safe_load):
        return {}
    try:
        data = safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError):
        return {}
    except yaml_error:
        return {}
    return data if isinstance(data, dict) else {}


def _clean_id(value: Any) -> str:
    prompt_id = str(value or "").strip()
    if not prompt_id or not _SAFE_PROMPT_ID.match(prompt_id):
        return ""
    if prompt_id in {".", ".."} or not prompt_id.strip("."):
        return ""
    return prompt_id


def _prompt_id_aliases(value: Any) -> list[str]:
    prompt_id = _clean_id(value)
    if not prompt_id:
        return []
    aliases = [prompt_id]
    suffix = prompt_id.rsplit(".", 1)[-1]
    if "." in prompt_id and suffix and suffix != prompt_id and _SAFE_PROMPT_ID.match(suffix):
        aliases.append(suffix)
    return aliases


def _path_from_workspace(workspace: dict[str, Any], key: str) -> Path | None:
    raw = str(workspace.get(key) or "").strip()
    return Path(raw) if raw else None


def _prompt_ids(data: dict[str, Any], profile: dict[str, Any]) -> list[str]:
    ids = [
        data.get("prompt_id"),
        data.get("system_prompt_id"),
        profile.get("system_prompt_id"),
        profile.get("default_prompt_id"),
        profile.get("prompt_id"),
        "default_chat",
    ]
    seen: set[str] = set()
    result: list[str] = []
    for item in ids:
        for prompt_id in _prompt_id_aliases(item):
            if prompt_id and prompt_id not in seen:
                seen.add(prompt_id)
                result.append(prompt_id)
    return result


def _profile_prompt_candidates(prompts_dir: Path | None, prompt_ids: list[str]) -> list[Path]:
    if prompts_dir is None:
        return []
    candidates: list[Path] = []
    for prompt_id in prompt_ids:
        candidates.extend(
            [
                prompts_dir / f"{prompt_id}.system.md",
                prompts_dir / f"{prompt_id}.prompt.md",
                prompts_dir / f"{prompt_id}.md",
                prompts_dir / prompt_id / "prompt.md",
            ]
        )
    candidates.extend([prompts_dir / "default.system.md", prompts_dir / "system.md"])
    return candidates


def _snapshot_prompt_candidates(
    snapshots_dir: Path | None,
    base_pack: str,
    prompt_ids: list[str],
) -> list[Path]:
    if snapshots_dir is None:
        return []
    prompt_root = snapshots_dir / base_pack / "prompts"
    candidates: list[Path] = []
    for prompt_id in prompt_ids:
        candidates.extend(
            [
                prompt_root / prompt_id / "prompt.md",
                prompt_root / prompt_id / "template.md",
                prompt_root / f"{prompt_id}.system.md",
                prompt_root / f"{prompt_id}.prompt.md",
                prompt_root / f"{prompt_id}.md",
            ]
        )
    candidates.extend([prompt_root / "default.system.md", prompt_root / "system.md"])
    return candidates


def _select_existing_file(candidates: list[Path]) -> Path | None:
    for path in candidates:
        if path.is_file():
            return path
    return None


def _read_prompt_file(path: Path) -> str | None:
    """Read a prompt source without allowing a broken source to abort fallback."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _catalog_profile(profile_id: Any) -> dict[str, Any]:
    candidate = str(profile_id or "").strip()
    if not candidate:
        return {}
    try:
        profile = CapabilityCatalog().profile(candidate)
    except Exception:
        return {}
    return dict(profile) if isinstance(profile, dict) else {}


def _source_pack_id(data: dict[str, Any], profile: dict[str, Any]) -> str:
    metadata = _mapping_or_empty(profile.get("metadata"))
    raw_pack = (
        data.get("source_pack_id")
        or profile.get("source_pack_id")
        or profile.get("_source_pack_id")
        or metadata.get("pack_id")
        or data.get("base_pack")
        or profile.get("base_pack")
    )
    return str(raw_pack or "").strip()


def _chain_entry(
    *,
    source_type: str,
    layer: str,
    selected: bool,
    source: str = "",
    candidates: list[Path] | None = None,
    prompt_id: str = "",
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "source_type": source_type,
        "layer": layer,
        "selected": selected,
    }
    if source:
        entry["source"] = source
    if prompt_id:
        entry["prompt_id"] = prompt_id
    if candidates is not None:
        entry["candidates"] = [str(path) for path in candidates]
    return entry


def resolve_effective_prompt(input_data: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve the passive effective prompt layer for a profile/conversation."""
    data = input_data if isinstance(input_data, dict) else {}
    workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
    profile_file = _path_from_workspace(workspace, "profile_file") if workspace else None
    prompts_dir = _path_from_workspace(workspace, "prompts_dir") if workspace else None
    snapshots_dir = _path_from_workspace(workspace, "snapshots_dir") if workspace else None
    profile = _read_mapping(profile_file)
    catalog_profile = _catalog_profile(data.get("profile_id") or profile.get("profile_id"))
    merged_profile = dict(catalog_profile)
    merged_profile.update(profile)
    prompt_ids = _prompt_ids(data, merged_profile)
    source_pack_id = _source_pack_id(data, merged_profile)
    base_pack = str(data.get("base_pack") or merged_profile.get("base_pack") or source_pack_id or "defaultspack").strip() or "defaultspack"
    source_chain: list[dict[str, Any]] = []

    studio_prompt = authored_prompt(
        str(data.get("profile_id") or merged_profile.get("profile_id") or ""),
        prompt_ids,
    )
    if studio_prompt is not None:
        prompt_id = str(studio_prompt.get("prompt_id") or prompt_ids[0])
        source_chain.append(
            _chain_entry(
                source_type="global_contract",
                layer="prompt_authoring_owner",
                selected=True,
                source="rumi.resource.prompt.studio.v1",
                prompt_id=prompt_id,
            )
        )
        return _effective_payload(
            data,
            prompt_id,
            "global_contract",
            "rumi.resource.prompt.studio.v1",
            str(studio_prompt.get("body") or ""),
            source_chain,
            metadata={
                "body_hash": studio_prompt.get("body_hash"),
                "revision": studio_prompt.get("revision"),
            },
        )

    owner_active = prompt_owner_available()
    profile_candidates = (
        [] if owner_active else _profile_prompt_candidates(prompts_dir, prompt_ids)
    )
    profile_candidate = _select_existing_file(profile_candidates)
    if profile_candidate is not None:
        content = _read_prompt_file(profile_candidate)
        if content is not None:
            source_chain.append(
                _chain_entry(
                    source_type="profile_override",
                    layer="workspace_prompt_file",
                    selected=True,
                    source=str(profile_candidate),
                    candidates=profile_candidates,
                    prompt_id=prompt_ids[0],
                )
            )
            return _effective_payload(
                data,
                prompt_ids[0],
                "profile_override",
                str(profile_candidate),
                content,
                source_chain,
            )
    source_chain.append(
        _chain_entry(
            source_type="profile_override",
            layer="workspace_prompt_file",
            selected=False,
            candidates=profile_candidates,
            prompt_id=prompt_ids[0],
        )
    )

    snapshot_candidates = _snapshot_prompt_candidates(snapshots_dir, base_pack, prompt_ids)
    snapshot_candidate = _select_existing_file(snapshot_candidates)
    if snapshot_candidate is not None:
        trusted, trust_reason = is_trusted_prompt_pack(base_pack)
        content = _read_prompt_file(snapshot_candidate)
        if trusted and content is not None:
            source_chain.append(
                _chain_entry(
                    source_type="profile_snapshot",
                    layer="profile_snapshot",
                    selected=True,
                    source=str(snapshot_candidate),
                    candidates=snapshot_candidates,
                    prompt_id=prompt_ids[0],
                )
            )
            return _effective_payload(
                data,
                prompt_ids[0],
                "profile_snapshot",
                str(snapshot_candidate),
                content,
                source_chain,
                source_pack_id=base_pack,
                source_pack_trusted=True,
                source_pack_trust_reason=trust_reason,
            )
        source_chain.append(
            _chain_entry(
                source_type="profile_snapshot",
                layer="profile_snapshot",
                selected=False,
                source=str(snapshot_candidate),
                candidates=snapshot_candidates,
                prompt_id=prompt_ids[0],
            )
        )
    else:
        source_chain.append(
            _chain_entry(
                source_type="profile_snapshot",
                layer="profile_snapshot",
                selected=False,
                candidates=snapshot_candidates,
                prompt_id=prompt_ids[0],
            )
        )

    resolver = PromptResolver()
    requested_sources: list[str | None] = [source_pack_id or None]
    if source_pack_id:
        requested_sources.append(None)
    for prompt_id in prompt_ids:
        for requested_source in requested_sources:
            content, resolved_pack_id = resolver.resolve_prompt(
                prompt_id,
                source_pack_id=requested_source,
            )
            if content is None:
                continue
            prompt_source_pack_id = resolved_pack_id or requested_source or base_pack
            trusted, trust_reason = is_trusted_prompt_pack(prompt_source_pack_id)
            if not trusted:
                continue
            source = f"{prompt_source_pack_id}.{prompt_id}"
            source_chain.append(
                _chain_entry(
                    source_type="pack_default",
                    layer="pack_default_prompt",
                    selected=True,
                    source=source,
                    prompt_id=prompt_id,
                )
            )
            return _effective_payload(
                data,
                prompt_id,
                "pack_default",
                source,
                content,
                source_chain,
                metadata=_prompt_manifest_metadata(resolver.get_manifest(prompt_id)),
                source_pack_id=prompt_source_pack_id,
                source_pack_trusted=True,
                source_pack_trust_reason=trust_reason,
            )

    source_chain.append(
        _chain_entry(
            source_type="pack_default",
            layer="pack_default_prompt",
            selected=False,
            prompt_id=prompt_ids[0],
        )
    )
    return _effective_payload(data, prompt_ids[0], "empty", "defaultspack.empty", "", source_chain)


def _effective_payload(
    data: dict[str, Any],
    prompt_id: str,
    source_type: str,
    source: str,
    content: str,
    source_chain: list[dict[str, Any]],
    *,
    metadata: dict[str, Any] | None = None,
    source_pack_id: str = "",
    source_pack_trusted: bool | None = None,
    source_pack_trust_reason: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "profile_id": data.get("profile_id"),
        "conversation_id": data.get("conversation_id"),
        "prompt_id": prompt_id,
        "source": source,
        "source_type": source_type,
        "source_chain": list(source_chain),
        "content": content,
        "final_content": content,
        "metadata": dict(metadata or {}),
    }
    if source_pack_id:
        payload["source_pack_id"] = source_pack_id
        payload["source_pack_trusted"] = bool(source_pack_trusted)
        if source_pack_trust_reason:
            payload["source_pack_trust_reason"] = source_pack_trust_reason
    return payload


def _prompt_manifest_metadata(manifest: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        return {}
    config = _mapping_or_empty(manifest.get("config"))
    metadata = _mapping_or_empty(manifest.get("metadata"))
    output = dict(metadata)
    for key in ("allow_disable", "safety_boundary", "owner", "source_path"):
        if key in manifest:
            output[key] = manifest.get(key)
        if key in config:
            output[key] = config.get(key)
    if "allow_disable" not in output and manifest.get("read_only") is True:
        output["allow_disable"] = False
    return output


def resolve_prompt_for_conversation(
    input_data: dict[str, Any] | None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve and render the effective prompt for a conversation.

    This stays prompt-only: it never selects tools, providers, permissions, or
    policy. Downstream flow steps decide how to use the returned text.
    """
    data = input_data if isinstance(input_data, dict) else {}
    effective = resolve_effective_prompt(data)
    variables = _conversation_variables(data, context or {}, effective)
    template_content = str(effective.get("content") or "")
    final_content = render(template_content, variables)
    validation = validate_prompt_template({"template": template_content, "variables": data.get("template_variables")})
    missing_variables = [
        name
        for name in validation.get("user_variables", [])
        if name not in variables
    ]
    return {
        **effective,
        "template_content": template_content,
        "content": final_content,
        "final_content": final_content,
        "render_variables": variables,
        "missing_variables": missing_variables,
        "validation": validation,
    }


def _conversation_variables(
    data: dict[str, Any],
    context: dict[str, Any],
    effective: dict[str, Any],
) -> dict[str, Any]:
    variables: dict[str, Any] = {}
    raw_variables = data.get("variables")
    if isinstance(raw_variables, dict):
        variables.update(raw_variables)

    raw_messages = data.get("messages")
    messages = (
        _list_or_empty(raw_messages)
        if isinstance(raw_messages, list)
        else _list_or_empty(context.get("messages"))
    )
    messages_text = json.dumps(messages, ensure_ascii=False) if messages else ""
    variables.update(
        {
            "context.profile_id": data.get("profile_id") or context.get("profile_id") or "",
            "context.conversation_id": data.get("conversation_id") or context.get("conversation_id") or "",
            "context.message_count": len(messages),
            "context.messages": messages_text,
            "context.system_prompt": effective.get("content") or "",
        }
    )
    for key in ("knowledge", "memory", "project_context", "user_snapshot"):
        value = data.get(key, context.get(key))
        if value is not None:
            variables[f"context.{key}"] = value
    return variables


def validate_prompt_template(input_data: dict[str, Any] | None) -> dict[str, Any]:
    data = input_data if isinstance(input_data, dict) else {}
    template = data.get("template", data.get("content", data.get("body")))
    prompt_id = str(data.get("prompt_id") or "").strip()
    if template is None and prompt_id:
        template = PromptResolver().resolve_prompt_text(prompt_id)
    if template is None:
        return {
            "valid": False,
            "errors": [
                {
                    "code": "MISSING_TEMPLATE",
                    "message": "template, content, body, or prompt_id is required",
                }
            ],
            "warnings": [],
            "variables": [],
            "user_variables": [],
            "context_variables": [],
            "declared_variables": [],
            "missing_declared_variables": [],
            "undeclared_variables": [],
        }

    template = str(template)
    declared_variables = _declared_variables(data.get("variables"))
    found_variables = _unique(_VARIABLE_PATTERN.findall(template))
    errors = _template_syntax_errors(template)
    user_variables = [name for name in found_variables if not name.startswith("context.")]
    context_variables = [name for name in found_variables if name.startswith("context.")]
    declared_names = {item["name"] for item in declared_variables}
    missing_declared = [
        item["name"]
        for item in declared_variables
        if item.get("required") and item["name"] not in found_variables
    ]
    undeclared = [
        name
        for name in user_variables
        if declared_names and name not in declared_names
    ]
    warnings: list[dict[str, str]] = []
    if undeclared:
        warnings.append(
            {
                "code": "UNDECLARED_VARIABLES",
                "message": "Template references variables not present in the declarations.",
            }
        )
    if missing_declared:
        warnings.append(
            {
                "code": "MISSING_DECLARED_VARIABLES",
                "message": "Required declared variables are not referenced by the template.",
            }
        )

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "variables": found_variables,
        "user_variables": user_variables,
        "context_variables": context_variables,
        "declared_variables": declared_variables,
        "missing_declared_variables": missing_declared,
        "undeclared_variables": undeclared,
    }


def _declared_variables(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    items = raw.values() if isinstance(raw, dict) else raw
    if not isinstance(items, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            name = item.strip()
            if name:
                result.append({"name": name, "required": False, "type": "string"})
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if name:
                result.append(
                    {
                        "name": name,
                        "required": bool(item.get("required", False)),
                        "type": str(item.get("type") or "string"),
                    }
                )
    return result


def _template_syntax_errors(template: str) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if template.count("{{") != template.count("}}"):
        errors.append(
            {
                "code": "UNBALANCED_BRACES",
                "message": "Template has an unmatched '{{' or '}}'.",
            }
        )
    for match in _BRACED_PATTERN.finditer(template):
        raw = match.group(1).strip()
        if not raw:
            errors.append(
                {
                    "code": "EMPTY_VARIABLE",
                    "message": "Template contains an empty variable reference.",
                }
            )
        elif not _VALID_VARIABLE_NAME.match(raw):
            errors.append(
                {
                    "code": "INVALID_VARIABLE_NAME",
                    "message": f"Invalid variable name: {raw}",
                }
            )
    return errors


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
