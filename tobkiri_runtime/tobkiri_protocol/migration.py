"""One-way, non-authorizing migration of legacy startup profiles."""

from __future__ import annotations

import json
import importlib
import re
from pathlib import Path
from typing import Any, Mapping

from .canonical import canonical_digest, strict_loads
from .errors import MigrationBlockedError, MigrationError, ProtocolError
from .ids import validate_canonical_id
from .provenance import make_provenance
from .validation import validate_document

yaml: Any = None
try:
    yaml = importlib.import_module("yaml")
except ImportError:  # pragma: no cover - PyYAML is a project dependency.
    pass


LEGACY_MIGRATION_VERSION = "io.tobkiri.profile-migration.v1"
KNOWN_PACK_ALIASES = {
    "defaultspack": "defaults-basepack",
    "rumi.defaultspack": "defaults-basepack",
    "rumi_defaultspack": "defaults-basepack",
}
AUTHORITY_KEY_MARKERS = (
    "approval",
    "approved",
    "credential",
    "grant",
    "lease",
    "permission",
    "secret",
    "token",
    "authority",
    "host_execution",
)
AUTHORITY_KEY_COMPACT_MARKERS = tuple(marker.replace("_", "") for marker in AUTHORITY_KEY_MARKERS)


def migrate_legacy_profile(
    document: Mapping[str, Any],
    *,
    source_path: str = "legacy/profile.json",
    repository_root: Path | None = None,
) -> dict[str, Any]:
    """Convert one legacy profile into a review-only v4 profile document.

    The return value always contains ``activation_eligible=False`` and
    ``authority_minted=False``.  Ambiguous input returns ``profile=None`` and
    ``status='blocked'``.  No legacy permission, token, command, or approval
    value is copied into the v4 document.
    """
    if not isinstance(document, Mapping):
        return _blocked("legacy profile must be an object")
    root = (repository_root or Path.cwd()).resolve()
    diagnostics: list[dict[str, str]] = []
    blocked: list[str] = []

    try:
        source_digest = canonical_digest(dict(document))
    except ProtocolError as exc:
        return _blocked(f"legacy profile is not strictly serializable: {exc}")

    profile_id = _first_value(document, "profile_id", "id")
    profile_id = _migrate_id(profile_id, "profile_id", diagnostics, blocked)
    base_pack = _extract_base_pack(document, diagnostics, blocked)
    migrated_base = _migrate_id(base_pack, "base_pack", diagnostics, blocked)
    pack_ids = _extract_pack_ids(document, diagnostics, blocked)
    migrated_pack_ids = [
        _migrate_id(item, "packs", diagnostics, blocked) for item in pack_ids
    ]
    migrated_pack_ids = [item for item in migrated_pack_ids if item is not None]
    if migrated_base and migrated_base not in migrated_pack_ids:
        migrated_pack_ids.insert(0, migrated_base)
    if len(set(migrated_pack_ids)) != len(migrated_pack_ids):
        blocked.append("duplicate pack identity after legacy alias normalization")

    command, command_kind = _extract_legacy_command(document, diagnostics, blocked)
    dropped_fields = sorted(_find_authority_fields(document))

    if profile_id is None:
        blocked.append("profile_id is required and cannot be guessed from display name")
    if migrated_base is None:
        blocked.append("base_pack is required; no default Base Pack is inferred")
    if not migrated_pack_ids:
        blocked.append("at least one pack selection is required")

    legacy_version = str(document.get("version") or document.get("profile_version") or "unknown")
    legacy_ids = sorted(
        {
            str(value)
            for value in [base_pack, *pack_ids]
            if isinstance(value, str) and value
        }
    )
    if blocked:
        return _blocked(
            "; ".join(sorted(set(blocked))),
            diagnostics=diagnostics,
            source_digest=source_digest,
            dropped_fields=dropped_fields,
        )

    assert profile_id is not None
    assert migrated_base is not None
    profile: dict[str, Any] = {
        "profile_api_version": "io.tobkiri.profile.v4",
        "profile_id": profile_id,
        "state": "needs_resolution",
        "mode": "headless" if command_kind == "headless" else "interactive",
        "catalog_revision": None,
        "display_name": _display_name(document, profile_id),
        "base": {
            "pack_id": migrated_base,
            "artifact_digest": None,
            "definition_revision": None,
            "resolution": "verified_exact_artifact_required",
        },
        "shell": None,
        "packs": [
            {
                "pack_id": pack_id,
                "artifact_digest": None,
                "role": "base" if pack_id == migrated_base else "backend",
            }
            for pack_id in migrated_pack_ids
            if pack_id != migrated_base
        ],
        "requested_edges": [],
        "authority_references": [],
        "profile_authority_snapshot_digest": None,
        "legacy_migration": {
            "source_digest": source_digest,
            "legacy_version": legacy_version,
            "classification": (
                "legacy_opaque_packaged_process"
                if command is not None
                else "legacy_profile"
            ),
            "requires_review": True,
            "dropped_fields": dropped_fields,
            "legacy_ids": legacy_ids,
            "command_digest": canonical_digest(command) if command is not None else None,
        },
        "provenance": make_provenance(
            root=root,
            source_path=source_path,
            payload=dict(document),
            source_kind="migration",
            normative=False,
        ).to_dict(),
    }
    if command_kind:
        diagnostics.append(
            {
                "code": "legacy_presentation_requires_review",
                "severity": "warning",
                "message": f"legacy launch kind {command_kind!r} is retained as inventory only",
            }
        )
    if dropped_fields:
        diagnostics.append(
            {
                "code": "legacy_authority_discarded",
                "severity": "warning",
                "message": "legacy authority-bearing fields were discarded; no Grant was minted",
            }
        )

    try:
        validate_document(profile, "profile")
    except Exception as exc:
        return _blocked(
            f"generated v4 profile failed validation: {exc}",
            diagnostics=diagnostics,
            source_digest=source_digest,
            dropped_fields=dropped_fields,
        )
    return {
        "migration_api_version": LEGACY_MIGRATION_VERSION,
        "status": "review_required",
        "activation_eligible": False,
        "authority_minted": False,
        "profile": profile,
        "diagnostics": diagnostics,
        "dropped_authority_fields": dropped_fields,
    }


def migrate_legacy_profile_or_raise(
    document: Mapping[str, Any],
    *,
    source_path: str = "legacy/profile.json",
    repository_root: Path | None = None,
) -> dict[str, Any]:
    """Migrate a legacy profile and raise if the result is blocked."""
    result = migrate_legacy_profile(
        document,
        source_path=source_path,
        repository_root=repository_root,
    )
    if result["status"] == "blocked":
        raise MigrationBlockedError("; ".join(result.get("blocked_reasons", [])))
    return result


def load_and_migrate_legacy_profile(
    path: Path,
    *,
    repository_root: Path | None = None,
) -> dict[str, Any]:
    """Read JSON/YAML legacy input with duplicate-key rejection and migrate it."""
    try:
        raw = _load_legacy_file(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ProtocolError, MigrationError) as exc:
        return _blocked(f"cannot read legacy profile {path}: {exc}")
    except Exception as exc:  # pragma: no cover - parser-specific YAML errors.
        # A migration tool must never turn an unrecognized parser failure into
        # an activation candidate.  Keep the result machine-readable and
        # fail closed even when a parser reports an implementation-specific
        # exception type.
        return _blocked(f"cannot read legacy profile {path}: {exc}")
    return migrate_legacy_profile(
        raw,
        source_path=path.as_posix(),
        repository_root=repository_root,
    )


def _load_legacy_file(path: Path) -> Mapping[str, Any]:
    raw = path.read_bytes()
    if path.suffix.lower() == ".json":
        value = strict_loads(raw)
    else:
        if yaml is None:
            raise MigrationError("YAML support is unavailable; refusing migration")
        value = yaml.load(raw.decode("utf-8"), Loader=_UniqueKeyLoader)
    if not isinstance(value, Mapping):
        raise MigrationError("legacy profile must be an object")
    return value


def _extract_base_pack(
    document: Mapping[str, Any],
    diagnostics: list[dict[str, str]],
    blocked: list[str],
) -> str | None:
    candidates: list[tuple[str, str]] = []
    value = document.get("base_pack")
    if isinstance(value, str) and value.strip():
        candidates.append(("base_pack", value.strip()))
    elif value is not None:
        blocked.append("base_pack must be a string")
    structured = document.get("base")
    if isinstance(structured, Mapping):
        pack_id = structured.get("pack_id") or structured.get("id")
        if isinstance(pack_id, str) and pack_id.strip():
            candidates.append(("base.pack_id", pack_id.strip()))
        elif pack_id is not None:
            blocked.append("base.pack_id must be a string")
    elif structured is not None:
        blocked.append("base must be an object when present")
    standard = document.get("standard_pack_id")
    if isinstance(standard, str) and standard.strip():
        candidates.append(("standard_pack_id", standard.strip()))
        diagnostics.append(
            {
                "code": "legacy_standard_pack_alias",
                "severity": "warning",
                "message": "standard_pack_id is accepted only as an explicit migration alias",
            }
        )
    elif standard is not None:
        blocked.append("standard_pack_id must be a string")
    # Compare after the explicit, closed alias map.  Two spellings of the
    # same known legacy pack are not ambiguous; anything else remains a hard
    # conflict and is never guessed.
    values = {KNOWN_PACK_ALIASES.get(value, value) for _, value in candidates}
    if len(values) > 1:
        blocked.append("conflicting legacy Base Pack selections")
        return None
    return candidates[0][1] if candidates else None


def _extract_pack_ids(
    document: Mapping[str, Any],
    diagnostics: list[dict[str, str]],
    blocked: list[str],
) -> list[str]:
    raw = document.get("packs", [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        blocked.append("packs must be a list")
        return []
    values: list[str] = []
    for index, item in enumerate(raw):
        if isinstance(item, str) and item.strip():
            values.append(item.strip())
            continue
        if isinstance(item, Mapping):
            value = item.get("pack_id") or item.get("id")
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
                continue
        blocked.append(f"packs[{index}] is not an unambiguous pack ID")
    return values


def _extract_legacy_command(
    document: Mapping[str, Any],
    diagnostics: list[dict[str, str]],
    blocked: list[str],
) -> tuple[Any | None, str | None]:
    candidates: list[tuple[str, Any]] = []
    launch = document.get("launch")
    if isinstance(launch, Mapping):
        if "command" in launch:
            candidates.append(("launch.command", launch["command"]))
        if launch.get("kind") is not None and not isinstance(launch.get("kind"), str):
            blocked.append("launch.kind must be a string")
    elif launch is not None:
        blocked.append("launch must be an object")
    desktop = document.get("desktop_app")
    if isinstance(desktop, Mapping) and "command" in desktop:
        candidates.append(("desktop_app.command", desktop["command"]))
    elif desktop is not None and not isinstance(desktop, Mapping):
        blocked.append("desktop_app must be an object")
    commands = [item for _, item in candidates]
    if len(commands) > 1 and commands[0] != commands[1]:
        blocked.append("conflicting legacy launch commands")
        return None, None
    command = commands[0] if commands else None
    if command is not None and not _safe_command_shape(command):
        blocked.append("legacy command must be a non-empty string or argv list")
        return None, None
    kind = launch.get("kind") if isinstance(launch, Mapping) else None
    if kind is not None and kind not in {"desktop_app", "cli", "headless"}:
        blocked.append(f"unsupported legacy launch kind: {kind}")
    return command, str(kind) if kind is not None else None


def _safe_command_shape(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip()) and len(value) <= 4096
    if isinstance(value, list):
        return bool(value) and len(value) <= 128 and all(
            isinstance(item, str) and bool(item) and len(item) <= 4096 for item in value
        )
    return False


def _migrate_id(
    value: str | None,
    field: str,
    diagnostics: list[dict[str, str]],
    blocked: list[str],
) -> str | None:
    if value is None:
        return None
    mapped = KNOWN_PACK_ALIASES.get(value, value)
    if mapped != value:
        diagnostics.append(
            {
                "code": "legacy_id_alias",
                "severity": "warning",
                "message": f"{field} {value!r} mapped to canonical ID {mapped!r}",
            }
        )
    try:
        return validate_canonical_id(mapped, field=field)
    except ProtocolError:
        blocked.append(f"{field} is incompatible with canonical v4 IDs: {value!r}")
        return None


def _find_authority_fields(value: Any, path: str = "$") -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
            compact = normalized.replace("_", "")
            if normalized in AUTHORITY_KEY_MARKERS or any(
                marker in normalized or marker in compact
                for marker in AUTHORITY_KEY_MARKERS + AUTHORITY_KEY_COMPACT_MARKERS
            ):
                found.add(path + "." + str(key))
            found.update(_find_authority_fields(child, path + "." + str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.update(_find_authority_fields(child, f"{path}[{index}]"))
    return found


def _display_name(document: Mapping[str, Any], fallback: str) -> str:
    value = document.get("name") or document.get("display_name")
    if isinstance(value, str) and value.strip():
        return value.strip()[:256]
    return fallback


def _first_value(document: Mapping[str, Any], *keys: str) -> str | None:
    values = [document.get(key) for key in keys if document.get(key) is not None]
    if not values:
        return None
    if any(not isinstance(value, str) or not value.strip() for value in values):
        return None
    if len({str(value).strip() for value in values}) > 1:
        return None
    return str(values[0]).strip()


def _blocked(
    reason: str,
    *,
    diagnostics: list[dict[str, str]] | None = None,
    source_digest: str | None = None,
    dropped_fields: list[str] | None = None,
) -> dict[str, Any]:
    reasons = [reason] if reason else ["legacy profile migration was blocked"]
    return {
        "migration_api_version": LEGACY_MIGRATION_VERSION,
        "status": "blocked",
        "activation_eligible": False,
        "authority_minted": False,
        "profile": None,
        "blocked_reasons": reasons,
        "diagnostics": diagnostics or [],
        "source_digest": source_digest,
        "dropped_authority_fields": dropped_fields or [],
    }


if yaml is not None:

    class _UniqueKeyLoader(yaml.SafeLoader):
        """PyYAML loader that rejects duplicate mapping keys."""


    def _construct_unique_mapping(loader: Any, node: Any, deep: bool = False) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise MigrationError(f"duplicate YAML key: {key!r}")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping


    _UniqueKeyLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        _construct_unique_mapping,
    )
