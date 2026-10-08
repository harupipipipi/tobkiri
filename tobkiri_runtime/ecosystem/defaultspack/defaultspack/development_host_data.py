"""Finite data-only export/import for a development Host successor.

Registries import through their existing owners into a fresh namespace. Old
authority, secrets, credential handles, health evidence, mounts, queues and
saved-turn continuations are never included in the migration payload.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
from core_runtime.profile_workspace import validate_profile_id
from ecosystem.rumi_conversation_store_pack.runtime.store import (
    ConversationStore,
    _hash as conversation_hash,
)
from ecosystem.rumi_model_registry_pack.runtime.registry import (
    ModelRegistry,
    _hash as model_hash,
    _profile_record,
)
from ecosystem.rumi_provider_registry_pack.runtime.registry import (
    ProviderRegistry,
    _hash as provider_hash,
    _provider_record,
)
from ecosystem.rumi_provider_registry_pack.runtime.local_endpoint import local_openai_endpoint
from tobkiri_protocol.canonical import canonical_bytes, canonical_digest
from tobkiri_protocol.secure_persistence import SecureDirectory
from tobkiri_protocol.validation import validate_document

_MAX_FILE = 32 * 1024 * 1024
_MAX_PROFILES = 128
_PARAMETERS = {"max_tokens", "temperature", "top_p", "frequency_penalty", "presence_penalty"}
_CONVERSATION_FIELDS = {
    "id",
    "title",
    "created_at",
    "updated_at",
    "model_reference",
    "tags",
    "is_starred",
    "is_pinned",
    "is_archived",
    "parent_conversation_id",
    "child_conversation_ids",
    "current_node_id",
    "conversation_kind",
    "group_id",
}
_MESSAGE_FIELDS = {
    "id",
    "role",
    "content",
    "raw_text",
    "created_at",
    "updated_at",
    "sequence",
    "sequence_number",
    "parent_id",
    "children_ids",
    "finish_reason",
    "usage",
}


def prepare_host_data(source: Path, *, bootstrap_profile_id: str) -> dict[str, Any]:
    """Return bounded sanitized owner records, with exact source CAS fences."""
    SecureDirectory(source, create=False)
    definition_store = ProfileDefinitionStore(source)
    index = definition_store.snapshot()
    definitions = definition_store.list_profiles()
    ids = {validate_profile_id(bootstrap_profile_id), *(item.profile_id for item in definitions)}
    # Only the three declared data owners contribute additional Profile IDs.
    # Unknown Pack directories and arbitrary workspace trees are not explored.
    for pack in (
        "rumi_provider_registry_pack",
        "rumi_model_registry_pack",
        "rumi_conversation_store_pack",
    ):
        profiles = source / "packs" / pack / "profiles"
        if profiles.exists() or profiles.is_symlink():
            SecureDirectory(profiles, create=False)
            for child in profiles.iterdir():
                SecureDirectory(child, create=False)
                ids.add(validate_profile_id(child.name))
                if len(ids) > _MAX_PROFILES:
                    raise ValueError("Host handover has too many data Profiles")
    if len(ids) > _MAX_PROFILES:
        raise ValueError("Host handover has too many data Profiles")
    memberships = _owner_memberships(source)
    if any(set(record["children"]) - ids for record in memberships.values() if record is not None):
        raise ValueError("Host handover data Profile membership changed")
    rows = []
    fences = []
    for profile_id in sorted(ids):
        providers = ProviderRegistry(profile_id, user_data_root=source)
        models = ModelRegistry(profile_id, user_data_root=source)
        conversations = ConversationStore(profile_id, user_data_root=source)
        for owner in (providers, models, conversations):
            fences.append(_file_fence(source, owner.path))
        provider_view = providers.snapshot()
        model_view = models.snapshot()
        conversation_view = conversations.snapshot()
        normalized_providers = [_sanitize_provider(row) for row in provider_view["providers"]]
        normalized_models = [_sanitize_model(row) for row in model_view["profiles"]]
        normalized_conversations = [
            _sanitize_conversation(row) for row in conversation_view["conversations"]
        ]
        _unique(normalized_providers, "provider_instance_id")
        _unique(normalized_models, "model_profile_id")
        _unique(normalized_conversations, "id")
        provider_ids = {row["provider_instance_id"] for row in normalized_providers}
        for model in normalized_models:
            selected = model["metadata"].get("provider_connection_id")
            if selected is not None and selected not in provider_ids:
                raise ValueError("Host handover model connection is missing")
        model_ids = {row["model_profile_id"] for row in normalized_models}
        aliases = dict(model_view["aliases"])
        if any(target not in model_ids for target in aliases.values()):
            raise ValueError("Host handover model alias target is missing")
        rows.append(
            {
                "profile_id": profile_id,
                "providers": normalized_providers,
                "models": normalized_models,
                "aliases": aliases,
                "conversations": normalized_conversations,
            }
        )
    payload = {
        "schema": "io.tobkiri.development-host-data.v1",
        "source_profile_generation": index["generation"],
        "definitions": [_sanitize_definition(item.profile) for item in definitions],
        "profiles": rows,
        "source_files": fences,
        "source_owner_memberships": memberships,
        "history_policy": "Text parts retained as inert history; attachments, widgets, events, tool logs, continuations and Profile content projections remain in the original namespace. Destination definitions become immutable successors; recovery retains them behind the migration fence",
    }
    if len(canonical_bytes(payload)) > _MAX_FILE:
        raise ValueError("Host handover data exceeds the finite staging bound")
    # Re-read the bytes after owner normalization to exclude torn snapshots.
    if (
        fences != [_file_fence(source, source / row["relative_path"]) for row in fences]
        or definition_store.snapshot() != index
        or memberships != _owner_memberships(source)
    ):
        raise ValueError("Host handover data changed while preparing")
    return {**payload, "data_digest": canonical_digest(payload)}


def import_host_data(target: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Import an exact sanitized payload through fresh owner stores only."""
    facts = {key: value for key, value in payload.items() if key != "data_digest"}
    if (
        canonical_digest(facts) != payload.get("data_digest")
        or payload.get("schema") != "io.tobkiri.development-host-data.v1"
    ):
        raise ValueError("Host handover staged data changed")
    _validate_payload(payload)
    SecureDirectory(target, create=False)
    store = ProfileDefinitionStore(target)
    if store.snapshot()["generation"] != 0:
        raise ValueError("Host handover destination definitions are not empty")
    for row in payload["profiles"]:
        for owner in (ProviderRegistry, ModelRegistry, ConversationStore):
            if owner(row["profile_id"], user_data_root=target).path.exists():
                raise ValueError("Host handover destination owner is initialized")
            if row[
                {
                    ProviderRegistry: "providers",
                    ModelRegistry: "models",
                    ConversationStore: "conversations",
                }[owner]
            ]:
                SecureDirectory(owner(row["profile_id"], user_data_root=target).root, create=True)
    for definition in payload["definitions"]:
        if _sanitize_definition(definition) != definition:
            raise ValueError("Host handover definition contains old authority")
        store.create_profile(definition, expected_store_generation=store.snapshot()["generation"])
    counts = {
        "definitions": len(payload["definitions"]),
        "providers": 0,
        "models": 0,
        "conversations": 0,
        "messages": 0,
    }
    for row in payload["profiles"]:
        profile_id = validate_profile_id(row["profile_id"])
        providers = [_sanitize_provider(value) for value in row["providers"]]
        models = [_sanitize_model(value) for value in row["models"]]
        conversations = [_sanitize_conversation(value) for value in row["conversations"]]
        if (
            providers != row["providers"]
            or models != row["models"]
            or conversations != row["conversations"]
        ):
            raise ValueError("Host handover payload contains excluded data")
        _unique(providers, "provider_instance_id")
        _unique(models, "model_profile_id")
        _unique(conversations, "id")
        if providers:
            ProviderRegistry(profile_id, user_data_root=target).migrate(
                providers,
                expected_source_hash=provider_hash(
                    {"providers": sorted(providers, key=lambda item: item["provider_instance_id"])}
                ),
            )
        if models:
            ModelRegistry(profile_id, user_data_root=target).migrate(
                models,
                row["aliases"],
                expected_source_hash=model_hash(
                    {
                        "profiles": sorted(models, key=lambda item: item["model_profile_id"]),
                        "aliases": dict(sorted(row["aliases"].items())),
                    }
                ),
            )
        if conversations:
            result = ConversationStore(profile_id, user_data_root=target).migrate(
                conversations,
                expected_source_hash=conversation_hash(
                    {"conversations": sorted(conversations, key=lambda item: item["id"])}
                ),
            )
            counts["messages"] += result["messages"]
        counts["providers"] += len(providers)
        counts["models"] += len(models)
        counts["conversations"] += len(conversations)
    return {
        "data_digest": payload["data_digest"],
        "counts": counts,
        "activation_required": True,
        "provider_credentials_required": True,
    }


def _file_fence(root: Path, path: Path) -> dict[str, Any]:
    relative = path.relative_to(root)
    if not path.exists() and not path.is_symlink():
        return {"relative_path": str(relative), "digest": None}
    raw = SecureDirectory(path.parent, create=False).read_bytes_bounded(
        path.name, max_bytes=_MAX_FILE
    )
    return {"relative_path": str(relative), "digest": canonical_digest({"bytes": raw.hex()})}


def _sanitize_definition(value: Mapping[str, Any]) -> dict[str, Any]:
    definition = deepcopy(dict(value))
    definition.update(
        state="needs_resolution",
        catalog_revision=None,
        authority_references=[],
        profile_authority_snapshot_digest=None,
    )
    definition.pop("content_projections", None)
    for edge in definition["requested_edges"]:
        edge.pop("authority_reference", None)
    return dict(validate_document(definition, "profile"))


def _sanitize_provider(value: Mapping[str, Any]) -> dict[str, Any]:
    # An endpoint path/query can contain a secret. Re-enter it in the new
    # connection ceremony instead of guessing whether it is public.
    return _provider_record(
        {
            "provider_instance_id": value["provider_instance_id"],
            "adapter_id": value["adapter_id"],
            "display_name": value.get("display_name"),
            "data_residency": value.get("data_residency"),
            "credential_handle": None,
            "endpoint": local_openai_endpoint(value.get("endpoint"))
            if value["adapter_id"] == "local-openai-compatible"
            else None,
            "enabled": False,
            "metadata": {
                key: value.get("metadata", {})[key]
                for key in ("catalog_provider_id",)
                if key in value.get("metadata", {})
            },
        }
    )


def _sanitize_model(value: Mapping[str, Any]) -> dict[str, Any]:
    return _profile_record(
        {
            "model_profile_id": value["model_profile_id"],
            "model_id": value["model_id"],
            "display_name": value.get("display_name"),
            "requirements": value.get("requirements", {}),
            "credential_handle": None,
            "enabled": False,
            "parameters": {
                key: item for key, item in value.get("parameters", {}).items() if key in _PARAMETERS
            },
            "metadata": {
                key: value.get("metadata", {})[key]
                for key in ("provider_connection_id",)
                if key in value.get("metadata", {})
            },
        }
    )


def _sanitize_conversation(value: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: deepcopy(item) for key, item in value.items() if key in _CONVERSATION_FIELDS}
    result["messages"] = []
    for message in value.get("messages", []):
        row = {key: deepcopy(item) for key, item in message.items() if key in _MESSAGE_FIELDS}
        row["status"] = "complete" if message.get("status") == "complete" else "cancelled"
        row["parts"] = [
            {"type": "text", "text": part.get("text", part.get("content", ""))}
            for part in message.get("parts", [])
            if isinstance(part, Mapping)
            and part.get("type") in {"text", "input_text", "output_text"}
            and isinstance(part.get("text", part.get("content", "")), str)
        ]
        result["messages"].append(row)
    return result


def _unique(rows: list[dict[str, Any]], key: str) -> None:
    ids = [row[key] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Host handover owner has duplicate identities")


def _owner_memberships(root: Path) -> dict[str, Any]:
    result = {}
    for pack in (
        "rumi_provider_registry_pack",
        "rumi_model_registry_pack",
        "rumi_conversation_store_pack",
    ):
        path = root / "packs" / pack / "profiles"
        if path.exists() or path.is_symlink():
            SecureDirectory(path, create=False)
            info = path.stat()
            result[pack] = {
                "device": info.st_dev,
                "inode": info.st_ino,
                "children": sorted(child.name for child in path.iterdir()),
            }
        else:
            result[pack] = None
    return result


def _validate_payload(payload: Mapping[str, Any]) -> None:
    if (
        set(payload)
        != {
            "schema",
            "source_profile_generation",
            "definitions",
            "profiles",
            "source_files",
            "source_owner_memberships",
            "history_policy",
            "data_digest",
        }
        or len(canonical_bytes(payload)) > _MAX_FILE
        or len(payload["profiles"]) > _MAX_PROFILES
        or len(payload["definitions"]) > _MAX_PROFILES
    ):
        raise ValueError("Host handover payload exceeds its finite schema")
    ids = []
    for definition in payload["definitions"]:
        if _sanitize_definition(definition) != definition:
            raise ValueError("Host handover definition contains old authority")
    _unique(payload["definitions"], "profile_id")
    for row in payload["profiles"]:
        if set(row) != {"profile_id", "providers", "models", "aliases", "conversations"}:
            raise ValueError("Host handover owner payload fields are invalid")
        ids.append(validate_profile_id(row["profile_id"]))
        for field, sanitizer, key in (
            ("providers", _sanitize_provider, "provider_instance_id"),
            ("models", _sanitize_model, "model_profile_id"),
            ("conversations", _sanitize_conversation, "id"),
        ):
            normalized = [sanitizer(value) for value in row[field]]
            if normalized != row[field]:
                raise ValueError("Host handover owner payload contains excluded data")
            _unique(normalized, key)
        provider_ids = {item["provider_instance_id"] for item in row["providers"]}
        for model in row["models"]:
            selected = model["metadata"].get("provider_connection_id")
            if selected is not None and selected not in provider_ids:
                raise ValueError("Host handover model connection is missing")
        model_ids = {item["model_profile_id"] for item in row["models"]}
        if any(target not in model_ids for target in row["aliases"].values()):
            raise ValueError("Host handover alias target is missing")
    if len(ids) != len(set(ids)):
        raise ValueError("Host handover has duplicate data Profiles")
